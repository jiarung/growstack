#include "mqtt_client.h"

#include <string.h>

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#include "secrets.h"
#include "log.h"

namespace {

// PubSubClient's buffer holds the whole MQTT packet (CONNECT included: client id
// + user + pass + headers), not just the publish payload — keep it roomy.
constexpr uint16_t MQTT_BUFFER_SIZE   = 512;
constexpr uint16_t MQTT_KEEPALIVE_S   = 60;
constexpr uint16_t MQTT_SOCKET_TMO_S  = 5;   // bound how long a blocking connect() can stall
constexpr uint32_t RECONNECT_EVERY_MS = 5000;
constexpr size_t   MAX_ID_LEN         = 30;  // keeps CONNECT packet well within the buffer
constexpr size_t   MAX_CRED_LEN       = 64;  // clientId(~45)+user+pass+headers must fit MQTT_BUFFER_SIZE

WiFiClient espClient;
PubSubClient client(espClient);

char topic[64] = {0};
char spectrumTopic[72] = {0};
char clientId[48] = {0};
bool configValid = false;
uint32_t lastReconnectAttempt = 0;

// --- Reflectance command/state protocol (Phase 4a) -------------------------------
// reflect/cmd     : device subscribes QoS1; publisher sends non-retained.
// reflect/state   : QoS0 RETAINED — current device state ONLY (idle|measuring).
// reflect/result  : QoS0 non-retained — one ack/result per cmd.
// reflect/availability : retained + LWT (broker-sent online/offline).
char reflectCmdTopic[96]    = {0};
char reflectStateTopic[96]  = {0};
char reflectResultTopic[96] = {0};
char reflectAvailTopic[96]  = {0};

enum ReflectState { REFLECT_IDLE, REFLECT_MEASURING };
ReflectState reflectState = REFLECT_IDLE;

volatile bool cmdPending = false;
char pendRaw[200] = {0};     // raw cmd payload, bounded-copied in the callback

char curReqId[48] = {0};     // in-flight request during MEASURING
char curPlant[40] = {0};
uint32_t measureStart = 0;

char lastReqId[48] = {0};    // last handled request + its result (for dedup)
char lastStatus[16] = {0};

// Device id must be a single safe topic segment: server-side topic_parsing splits
// on '/', and '+'/'#' are MQTT wildcards — any of those would break the contract.
bool isValidDeviceId(const char* s) {
    if (s == nullptr) return false;
    size_t n = strlen(s);
    if (n == 0 || n > MAX_ID_LEN) return false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
        if (!ok) return false;
    }
    return true;
}

// MQTT callback — runs inside client.loop(). Stays tiny: bounded-copy the raw payload
// out of PubSubClient's reused internal buffer, set a flag, return. No parse, no publish,
// no measurement, no pointer kept. (PubSubClient exposes no retained flag to the callback.)
void onReflectCmd(char* t, uint8_t* payload, unsigned int len) {
    if (strcmp(t, reflectCmdTopic) != 0) return;
    if (cmdPending) return;  // one already queued; reflectLoop() drains it within a tick
    size_t n = len < sizeof(pendRaw) - 1 ? len : sizeof(pendRaw) - 1;
    memcpy(pendRaw, payload, n);
    pendRaw[n] = '\0';
    cmdPending = true;
}

// reflect/state: QoS0 RETAINED, current device state only (idle|measuring).
void publishReflectState() {
    char p[80];
    if (reflectState == REFLECT_MEASURING) {
        snprintf(p, sizeof(p), "{\"state\":\"measuring\",\"request_id\":\"%s\"}", curReqId);
    } else {
        snprintf(p, sizeof(p), "{\"state\":\"idle\",\"request_id\":null}");
    }
    client.publish(reflectStateTopic, p, true);
    logf("[reflect] state %s\n", p);
}

// reflect/result: QoS0 non-retained, one per cmd. Caches (id,status) for dedup.
void publishReflectResult(const char* reqId, const char* plant, const char* status,
                          const char* code) {
    char p[160];
    snprintf(p, sizeof(p), "{\"request_id\":\"%s\",\"status\":\"%s\",\"plant\":\"%s\",\"code\":\"%s\"}",
             reqId, status, plant, code);
    client.publish(reflectResultTopic, p, false);
    logf("[reflect] result %s\n", p);
    strncpy(lastReqId, reqId, sizeof(lastReqId) - 1);   lastReqId[sizeof(lastReqId) - 1] = '\0';
    strncpy(lastStatus, status, sizeof(lastStatus) - 1); lastStatus[sizeof(lastStatus) - 1] = '\0';
}

// Parse + dispatch one queued cmd. Single-flight: a 2nd cmd while measuring -> busy;
// a resend of an already-answered request_id -> re-publish its result, never re-measure.
void handleReflectCmd(const char* raw) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, raw);
    const char* reqId  = doc["request_id"] | "";
    const char* plant  = doc["plant"]      | "";
    const char* action = doc["action"]     | "";

    if (err || reqId[0] == '\0' || plant[0] == '\0' || strcmp(action, "measure") != 0) {
        publishReflectResult(reqId, plant, "malformed", "bad_cmd");  // state stays idle
        return;
    }
    if (strcmp(reqId, lastReqId) == 0) {              // dedup
        publishReflectResult(reqId, plant, lastStatus, "dedup");
        return;
    }
    if (reflectState == REFLECT_MEASURING) {          // single-flight reject
        publishReflectResult(reqId, plant, "busy", "measuring");
        return;
    }
    strncpy(curReqId, reqId, sizeof(curReqId) - 1); curReqId[sizeof(curReqId) - 1] = '\0';
    strncpy(curPlant, plant, sizeof(curPlant) - 1); curPlant[sizeof(curPlant) - 1] = '\0';
    reflectState = REFLECT_MEASURING;
    measureStart = millis();
    publishReflectState();                            // measuring
}

// Run on EVERY successful (re)connect: subscribe, announce online, and republish the
// CURRENT retained state (not only on transitions) so a broker restart can't leave it stale.
void onConnected() {
    client.subscribe(reflectCmdTopic, 1);
    client.publish(reflectAvailTopic, "online", true);  // retained
    publishReflectState();                              // current state (idle on boot)
}

bool tryConnect() {
    // LWT via connect() will-params (broker publishes "offline" if we drop). will QoS pinned
    // to 1 — broker-sent, so not bound by PubSubClient's QoS0-only publish.
    bool ok;
    if (MQTT_USER != nullptr && MQTT_USER[0] != '\0') {
        ok = client.connect(clientId, MQTT_USER, MQTT_PASS, reflectAvailTopic, 1, true, "offline");
    } else {
        ok = client.connect(clientId, reflectAvailTopic, 1, true, "offline");
    }
    if (ok) {
        logf("[mqtt] connected as %s -> %s:%u\n", clientId, MQTT_HOST, MQTT_PORT);
        onConnected();
    } else {
        logf("[mqtt] connect failed, state=%d (retry in %us)\n",
                      client.state(), (unsigned)(RECONNECT_EVERY_MS / 1000));
    }
    return ok;
}

}  // namespace

void mqttSetup() {
    if (!isValidDeviceId(MQTT_DEVICE_ID)) {
        logf("[mqtt] FATAL: invalid MQTT_DEVICE_ID \"%s\" — must be 1-%u of [A-Za-z0-9_-]. "
                      "MQTT disabled.\n", MQTT_DEVICE_ID ? MQTT_DEVICE_ID : "(null)", (unsigned)MAX_ID_LEN);
        configValid = false;
        return;
    }
    if ((MQTT_USER != nullptr && strlen(MQTT_USER) > MAX_CRED_LEN) ||
        (MQTT_PASS != nullptr && strlen(MQTT_PASS) > MAX_CRED_LEN)) {
        logf("[mqtt] FATAL: MQTT_USER/MQTT_PASS exceed %u chars (CONNECT packet would "
                      "overflow the %u-byte buffer). MQTT disabled.\n",
                      (unsigned)MAX_CRED_LEN, (unsigned)MQTT_BUFFER_SIZE);
        configValid = false;
        return;
    }
    snprintf(topic, sizeof(topic), "monitor-air/%s/telemetry", MQTT_DEVICE_ID);
    snprintf(spectrumTopic, sizeof(spectrumTopic), "monitor-air/%s/spectrum", MQTT_DEVICE_ID);
    snprintf(reflectCmdTopic,    sizeof(reflectCmdTopic),    "monitor-air/%s/reflect/cmd", MQTT_DEVICE_ID);
    snprintf(reflectStateTopic,  sizeof(reflectStateTopic),  "monitor-air/%s/reflect/state", MQTT_DEVICE_ID);
    snprintf(reflectResultTopic, sizeof(reflectResultTopic), "monitor-air/%s/reflect/result", MQTT_DEVICE_ID);
    snprintf(reflectAvailTopic,  sizeof(reflectAvailTopic),  "monitor-air/%s/reflect/availability", MQTT_DEVICE_ID);
    snprintf(clientId, sizeof(clientId), "monitor-air-%s", MQTT_DEVICE_ID);
    configValid = true;

    client.setServer(MQTT_HOST, MQTT_PORT);
    client.setBufferSize(MQTT_BUFFER_SIZE);
    client.setKeepAlive(MQTT_KEEPALIVE_S);
    client.setSocketTimeout(MQTT_SOCKET_TMO_S);
    client.setCallback(onReflectCmd);
    logf("[mqtt] topic=%s\n", topic);
}

void mqttLoop() {
    if (!configValid) return;
    // connect() is blocking; only attempt once WiFi is up, and rate-limit retries.
    if (WiFi.status() != WL_CONNECTED) return;

    if (client.connected()) {
        client.loop();  // keeps keepalive/PINGREQ alive between publishes
        return;
    }

    // connect() is synchronous (bounded by setSocketTimeout); rate-limit retries.
    uint32_t now = millis();
    if (lastReconnectAttempt != 0 && now - lastReconnectAttempt < RECONNECT_EVERY_MS) return;
    lastReconnectAttempt = now;
    tryConnect();
}

bool mqttConnected() {
    return configValid && client.connected();
}

bool mqttPublish(const SensorReading& r) {
    if (!mqttConnected()) return false;

    char payload[200];
    const size_t cap = sizeof(payload);
    size_t n = 0;
    bool first = true;

    // Bounds-checked append: snprintf returns the length it WOULD write, so on
    // truncation n must not advance past cap (that would underflow cap-n and the
    // next write would be out of bounds). Returns false to abort the whole publish.
    auto append = [&](const char* fmt, const char* key, float v) -> bool {
        int ret = snprintf(payload + n, cap - n, fmt, key, v);
        if (ret < 0 || (size_t)ret >= cap - n) return false;
        n += (size_t)ret;
        return true;
    };
    auto addField = [&](const char* key, float v, bool valid) -> bool {
        // re-check finiteness so a NaN/Inf never reaches the wire as bad JSON
        if (!valid || v != v || v >= 3.4e38f || v <= -3.4e38f) return true;  // skip, not an error
        if (!append(first ? "\"%s\":%.1f" : ",\"%s\":%.1f", key, v)) return false;
        first = false;
        return true;
    };

    payload[n++] = '{';
    bool built = addField("temp", r.temp, r.tempValid) &&
                 addField("hum", r.hum, r.humValid) &&
                 addField("pressure", r.pressure, r.pressureValid) &&
                 addField("gas", r.gas, r.gasValid) &&
                 addField("lux", r.lux, r.luxValid) &&
                 addField("lux_ref", r.lux_ref, r.lux_refValid);
    if (!built || n + 2 > cap) {  // +2: closing '}' and NUL
        logln("[mqtt] publish aborted: payload overflow");
        return false;
    }
    payload[n++] = '}';
    payload[n] = '\0';

    if (first) {  // no valid fields — nothing worth sending
        logln("[mqtt] skip publish: no valid sensor fields");
        return false;
    }

    bool ok = client.publish(topic, payload, false);  // QoS 0, not retained
    if (ok) {
        logf("[mqtt] published %s %s\n", topic, payload);
    } else {
        logf("[mqtt] publish FAILED: state=%d connected=%d wifi=%d topicLen=%u payloadLen=%u\n",
                      client.state(), client.connected(), WiFi.status(),
                      (unsigned)strlen(topic), (unsigned)strlen(payload));
    }
    return ok;
}

bool mqttPublishSpectrum(const SpectrumReading& s) {
    if (!mqttConnected() || !s.valid) return false;  // fail-open: nothing to send

    // All channels as floats (".0") to keep InfluxDB field types stable, like telemetry.
    char payload[256];
    int n = snprintf(payload, sizeof(payload),
        "{\"mode\":\"ambient\",\"f415\":%.1f,\"f445\":%.1f,\"f480\":%.1f,\"f515\":%.1f,"
        "\"f555\":%.1f,\"f590\":%.1f,\"f630\":%.1f,\"f680\":%.1f,\"clear\":%.1f,\"nir\":%.1f,"
        "\"spectrum_read_ms\":%.1f}",
        s.f415, s.f445, s.f480, s.f515, s.f555, s.f590, s.f630, s.f680, s.clear, s.nir,
        s.read_ms);
    if (n < 0 || (size_t)n >= sizeof(payload)) {
        logln("[mqtt] spectrum publish aborted: payload overflow");
        return false;
    }

    bool ok = client.publish(spectrumTopic, payload, false);  // QoS 0, not retained
    if (ok) {
        logf("[mqtt] published %s %s\n", spectrumTopic, payload);
    } else {
        logf("[mqtt] spectrum publish FAILED: state=%d\n", client.state());
    }
    return ok;
}

void reflectLoop() {
    if (!mqttConnected()) return;

    if (cmdPending) {            // drain one queued command per tick
        cmdPending = false;
        handleReflectCmd(pendRaw);
    }

    // Phase 4a: STUB measurement — finish after ~1 s with a 'done'. Phase 4b1 replaces
    // this body with the real dark/LED/lit non-blocking read + timeout.
    if (reflectState == REFLECT_MEASURING && millis() - measureStart >= 1000) {
        reflectState = REFLECT_IDLE;
        publishReflectResult(curReqId, curPlant, "done", "stub");
        publishReflectState();  // back to idle
    }
}
