#include "mqtt_client.h"

#include <string.h>

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#include "secrets.h"
#include "log.h"
#include "measure.h"
#include "weight_ref.h"
#include "as7341_diag.h"

namespace {

// PubSubClient's buffer holds the whole MQTT packet (topic + payload + header), not
// just the payload. Sized for the largest publish — the reflect result: dark/lit/net for
// 10 visible + 6 NIR channels + flags (~1.2 kB worst case) — plus the CONNECT packet.
constexpr uint16_t MQTT_BUFFER_SIZE   = 1792;
constexpr uint16_t MQTT_KEEPALIVE_S   = 60;
constexpr uint16_t MQTT_SOCKET_TMO_S  = 5;   // bound how long a blocking connect() can stall
constexpr uint32_t RECONNECT_EVERY_MS = 5000;
constexpr size_t   MAX_ID_LEN         = 30;  // keeps CONNECT packet well within the buffer
constexpr size_t   MAX_CRED_LEN       = 64;  // clientId(~45)+user+pass+headers must fit MQTT_BUFFER_SIZE

WiFiClient espClient;
PubSubClient client(espClient);

char topic[64] = {0};
char spectrumTopic[72] = {0};
char healthTopic[72]   = {0};   // monitor-air/<dev>/health — live per-sensor presence
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

// Measurement station: ESP publishes event_raw; Node-RED (Phase 3) enriches + acks.
char measureEventTopic[96]  = {0};   // ESP -> monitor-air/<dev>/measure/event_raw
char measureAckTopic[96]    = {0};   // Node-RED -> monitor-air/<dev>/measure/ack  (device subscribes)

// Per-plant watering references (publish-weight-ref.sh -> retained, GLOBAL topic —
// not per-device, so staging and production boards share the same refs).
constexpr char WEIGHT_REF_PREFIX[] = "monitor-air/ref/weight/";
constexpr char WEIGHT_REF_FILTER[] = "monitor-air/ref/weight/+";

// Remote forensics: publish "as7341" to diag/cmd and every [diag] line comes back on
// diag/out — so a failure can be captured without touching the station (this failure
// mode leaves WiFi alive; power must NOT be cycled before the evidence is taken).
char diagCmdTopic[96] = {0};   // monitor-air/<dev>/diag/cmd  (device subscribes)
char diagOutTopic[96] = {0};   // monitor-air/<dev>/diag/out
volatile bool diagPending = false;
char diagCmd[24] = {0};        // requested diag name, bounded-copied in the callback

enum ReflectState { REFLECT_IDLE, REFLECT_MEASURING };
ReflectState reflectState = REFLECT_IDLE;

volatile bool cmdPending = false;
char pendRaw[200] = {0};     // raw cmd payload, bounded-copied in the callback

char curReqId[48] = {0};     // in-flight request during MEASURING
char curPlant[40] = {0};

// Burst session (reflect/cmd with duration_s>0). One-shot leaves burstActive false.
constexpr uint32_t BURST_MAX_DURATION_S = 120;
constexpr int32_t  BURST_MAX_SAMPLES    = 400;   // hard cap so a burst can't run away
bool burstActive = false;
uint32_t burstStartMs  = 0;
uint32_t burstDeadline = 0;
int32_t  burstSeq       = 0;
int32_t  burstFailCount = 0;

char lastReqId[48] = {0};        // last FINISHED request id (for dedup replay)
char lastResultJson[200] = {0};  // its exact reflect/result payload, replayed verbatim

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
// Single dispatcher (PubSubClient has ONE callback) — route by topic. Stays tiny: bounded-copy + flag,
// no parse/publish/measurement here.
void onMqttMessage(char* t, uint8_t* payload, unsigned int len) {
    if (strcmp(t, reflectCmdTopic) == 0) {
        if (cmdPending) return;  // one already queued; reflectLoop() drains it within a tick
        size_t n = len < sizeof(pendRaw) - 1 ? len : sizeof(pendRaw) - 1;
        memcpy(pendRaw, payload, n);
        pendRaw[n] = '\0';
        cmdPending = true;
        return;
    }
    if (strcmp(t, measureAckTopic) == 0) {
        measureOnAck(payload, len);  // bounded-copy + flag; matched in measureLoop()
        return;
    }
    if (strncmp(t, WEIGHT_REF_PREFIX, sizeof(WEIGHT_REF_PREFIX) - 1) == 0) {
        // Parse-on-arrival, like the acks: a (re)connect delivers ~17 retained refs
        // in one client.loop() batch, so a single-slot mailbox would drop 16 of them.
        weightRefOnMessage(t + sizeof(WEIGHT_REF_PREFIX) - 1, payload, len);
        return;
    }
    if (strcmp(t, diagCmdTopic) == 0) {
        // Flag only — the forensic blocks ~1s and must NOT run inside client.loop().
        if (diagPending) return;
        size_t n = len < sizeof(diagCmd) - 1 ? len : sizeof(diagCmd) - 1;
        memcpy(diagCmd, payload, n);
        diagCmd[n] = '\0';
        diagPending = true;
        return;
    }
}

// Drain a queued diag request. Runs from mqttLoop (main loop context, connected).
// A forensic is evidence-at-a-moment, not a job: if the sensor is busy the request is
// REFUSED (told to retry), never queued — a run minutes after the trigger would rewrite
// config/SMUX and capture a state that no longer matches what the operator saw.
void diagDrain() {
    if (!diagPending) return;
    diagPending = false;
    auto reply = [](const char* msg) { logf("%s\n", msg); client.publish(diagOutTopic, msg, false); };

    if (strcmp(diagCmd, "as7341") != 0) {
        char msg[64];
        snprintf(msg, sizeof(msg), "[diag] unknown cmd \"%s\" (try: as7341)", diagCmd);
        reply(msg);
        return;
    }
    if (reflectBusy())  { reply("[diag] busy: reflect measurement in progress — retry"); return; }
    if (measureBusy())  { reply("[diag] busy: weigh in progress — retry"); return; }

    // Cooldown: the trigger topic is open on the LAN and the run blocks the main loop —
    // repeat-fire must not be able to starve measurement.
    static uint32_t lastRun = 0;
    uint32_t now = millis();
    if (lastRun != 0 && now - lastRun < 30000) { reply("[diag] cooldown (30s) — retry"); return; }
    lastRun = now;

    as7341DiagRun();   // blocking ~1s normally; the MQTT sink self-disables on first failure
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

// reflect/result: QoS0 non-retained, one per cmd. count/publish_fail_count are emitted only
// when >=0 (burst). A terminal result (done/error) is cached verbatim for dedup replay.
void publishReflectResult(const char* reqId, const char* plant, const char* status,
                          const char* code, int32_t count = -1, int32_t failCount = -1) {
    char p[200];
    int n = snprintf(p, sizeof(p),
        "{\"request_id\":\"%s\",\"status\":\"%s\",\"plant\":\"%s\",\"code\":\"%s\"",
        reqId, status, plant, code);
    if (count >= 0 && n > 0 && (size_t)n < sizeof(p))
        n += snprintf(p + n, sizeof(p) - n, ",\"count\":%ld", (long)count);
    if (failCount >= 0 && n > 0 && (size_t)n < sizeof(p))
        n += snprintf(p + n, sizeof(p) - n, ",\"publish_fail_count\":%ld", (long)failCount);
    if (n > 0 && (size_t)n < sizeof(p))
        snprintf(p + n, sizeof(p) - n, "}");

    client.publish(reflectResultTopic, p, false);
    logf("[reflect] result %s\n", p);

    // Cache a terminal result of a real request for dedup replay (not busy/malformed).
    if (reqId[0] && (strcmp(status, "done") == 0 || strcmp(status, "error") == 0)) {
        strncpy(lastReqId, reqId, sizeof(lastReqId) - 1);       lastReqId[sizeof(lastReqId) - 1] = '\0';
        strncpy(lastResultJson, p, sizeof(lastResultJson) - 1); lastResultJson[sizeof(lastResultJson) - 1] = '\0';
    }
}

// Parse + dispatch one queued cmd. Single-flight: a 2nd cmd while measuring -> busy;
// a resend of an already-answered request_id -> re-publish its result, never re-measure.
void handleReflectCmd(const char* raw) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, raw);
    const char* reqId  = doc["request_id"] | "";
    const char* plant  = doc["plant"]      | "";
    const char* action = doc["action"]     | "";
    // 0 -> one-shot; >0 -> burst window (s). Accept `duration` as an alias for `duration_s`.
    int durationS      = doc["duration_s"] | (doc["duration"] | 0);

    if (err || reqId[0] == '\0' || plant[0] == '\0' || strcmp(action, "measure") != 0) {
        publishReflectResult(reqId, plant, "malformed", "bad_cmd");
        return;
    }
    // in-flight: the SAME request while measuring is already running.
    if (reflectState == REFLECT_MEASURING && strcmp(reqId, curReqId) == 0) {
        publishReflectResult(reqId, plant, "busy", "in_flight");
        return;
    }
    // finished dedup: replay that request's cached result verbatim (never re-measure).
    if (reqId[0] && strcmp(reqId, lastReqId) == 0 && lastResultJson[0]) {
        client.publish(reflectResultTopic, lastResultJson, false);
        logf("[reflect] result (dedup) %s\n", lastResultJson);
        return;
    }
    // a DIFFERENT request while measuring -> busy.
    if (reflectState == REFLECT_MEASURING) {
        publishReflectResult(reqId, plant, "busy", "measuring");
        return;
    }
    // start a new measurement.
    strncpy(curReqId, reqId, sizeof(curReqId) - 1); curReqId[sizeof(curReqId) - 1] = '\0';
    strncpy(curPlant, plant, sizeof(curPlant) - 1); curPlant[sizeof(curPlant) - 1] = '\0';

    if (durationS > 0) {                              // burst
        if (durationS > (int)BURST_MAX_DURATION_S) durationS = (int)BURST_MAX_DURATION_S;
        if (!reflectBurstStart()) { publishReflectResult(reqId, plant, "error", "no_sensor"); return; }
        burstActive    = true;
        burstStartMs   = millis();
        burstDeadline  = burstStartMs + (uint32_t)durationS * 1000;
        burstSeq       = 0;
        burstFailCount = 0;
    } else {                                          // one-shot
        if (!reflectStart()) { publishReflectResult(reqId, plant, "error", "no_sensor"); return; }
        burstActive = false;
    }
    reflectState = REFLECT_MEASURING;
    publishReflectState();                            // measuring (published once)
}

// Run on EVERY successful (re)connect: subscribe, announce online, and republish the
// CURRENT retained state (not only on transitions) so a broker restart can't leave it stale.
void onConnected() {
    client.subscribe(reflectCmdTopic, 1);
    client.subscribe(measureAckTopic, 1);               // measurement-station acks
    client.subscribe(WEIGHT_REF_FILTER, 1);             // retained watering refs -> cache
    client.subscribe(diagCmdTopic, 1);                  // remote forensic trigger
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
    snprintf(healthTopic,   sizeof(healthTopic),   "monitor-air/%s/health", MQTT_DEVICE_ID);
    snprintf(reflectCmdTopic,    sizeof(reflectCmdTopic),    "monitor-air/%s/reflect/cmd", MQTT_DEVICE_ID);
    snprintf(reflectStateTopic,  sizeof(reflectStateTopic),  "monitor-air/%s/reflect/state", MQTT_DEVICE_ID);
    snprintf(reflectResultTopic, sizeof(reflectResultTopic), "monitor-air/%s/reflect/result", MQTT_DEVICE_ID);
    snprintf(reflectAvailTopic,  sizeof(reflectAvailTopic),  "monitor-air/%s/reflect/availability", MQTT_DEVICE_ID);
    snprintf(measureEventTopic,  sizeof(measureEventTopic),  "monitor-air/%s/measure/event_raw", MQTT_DEVICE_ID);
    snprintf(measureAckTopic,    sizeof(measureAckTopic),    "monitor-air/%s/measure/ack", MQTT_DEVICE_ID);
    snprintf(diagCmdTopic,       sizeof(diagCmdTopic),       "monitor-air/%s/diag/cmd", MQTT_DEVICE_ID);
    snprintf(diagOutTopic,       sizeof(diagOutTopic),       "monitor-air/%s/diag/out", MQTT_DEVICE_ID);
    snprintf(clientId, sizeof(clientId), "monitor-air-%s", MQTT_DEVICE_ID);
    configValid = true;

    client.setServer(MQTT_HOST, MQTT_PORT);
    client.setBufferSize(MQTT_BUFFER_SIZE);
    client.setKeepAlive(MQTT_KEEPALIVE_S);
    client.setSocketTimeout(MQTT_SOCKET_TMO_S);
    client.setCallback(onMqttMessage);
    logf("[mqtt] topic=%s\n", topic);
}

void mqttLoop() {
    if (!configValid) return;
    // connect() is blocking; only attempt once WiFi is up, and rate-limit retries.
    if (WiFi.status() != WL_CONNECTED) return;

    if (client.connected()) {
        client.loop();  // keeps keepalive/PINGREQ alive between publishes
        diagDrain();    // AFTER loop: a queued forensic runs in main-loop context
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

bool mqttPublishDiagLine(const char* line) {
    if (!mqttConnected()) return false;   // serial sink still has the line
    return client.publish(diagOutTopic, line, false);  // QoS0, not retained
}

bool mqttPublishMeasureEvent(const char* json) {
    if (!mqttConnected()) return false;
    bool ok = client.publish(measureEventTopic, json, false);  // QoS0, not retained
    logf("[measure] publish %s %s\n", ok ? "ok" : "FAILED", json);
    return ok;
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
                 addField("lux_ref", r.lux_ref, r.lux_refValid) &&
                 // DIAG: WiFi signal (dBm) — correlate weak rssi with freezes/gaps on Grafana.
                 addField("rssi", (float)WiFi.RSSI(), WiFi.status() == WL_CONNECTED);
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
    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"mode\":\"ambient\",\"f415\":%.1f,\"f445\":%.1f,\"f480\":%.1f,\"f515\":%.1f,"
        "\"f555\":%.1f,\"f590\":%.1f,\"f630\":%.1f,\"f680\":%.1f,\"clear\":%.1f,\"nir\":%.1f,"
        "\"spectrum_read_ms\":%.1f,\"saturated\":%.1f,\"gain\":%.1f,\"tint_ms\":%.2f}",
        s.f415, s.f445, s.f480, s.f515, s.f555, s.f590, s.f630, s.f680, s.clear, s.nir,
        s.read_ms, s.saturated ? 1.0 : 0.0,
        // config identity as measured facts — the k-model CAL is anchored to these
        s.gain_x, s.tint_ms);
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

bool mqttPublishHealth(const SensorHealth& h, bool pn532, const char* resetReason) {
    if (!mqttConnected()) return false;

    // Presence as 1.0/0.0 floats (stable InfluxDB field types, like telemetry/spectrum).
    char payload[288];
    int n = snprintf(payload, sizeof(payload),
        "{\"bme\":%.1f,\"lux\":%.1f,\"lux_ref\":%.1f,\"as7341\":%.1f,\"as7263\":%.1f,"
        "\"hx711\":%.1f,\"pn532\":%.1f,\"i2c_n\":%d",
        h.bme ? 1.0 : 0.0, h.lux ? 1.0 : 0.0, h.lux_ref ? 1.0 : 0.0, h.as7341 ? 1.0 : 0.0,
        h.as7263 ? 1.0 : 0.0, h.hx711 ? 1.0 : 0.0, pn532 ? 1.0 : 0.0, h.i2c_n);
    if (n < 0 || (size_t)n >= sizeof(payload)) { logln("[mqtt] health publish aborted: overflow"); return false; }

    // spectrum_read_ms: OMIT when NaN (no ambient read yet) — bare `nan` is invalid JSON.
    if (h.spectrum_read_ms == h.spectrum_read_ms) {  // false only for NaN
        int m = snprintf(payload + n, sizeof(payload) - n, ",\"spectrum_read_ms\":%.1f", h.spectrum_read_ms);
        if (m < 0 || (size_t)(n + m) >= sizeof(payload)) { logln("[mqtt] health publish aborted: overflow"); return false; }
        n += m;
    }
    // reset reason as a STRING field (host: json_string_fields=["reset"]).
    int m = snprintf(payload + n, sizeof(payload) - n, ",\"reset\":\"%s\"}", resetReason ? resetReason : "");
    if (m < 0 || (size_t)(n + m) >= sizeof(payload)) { logln("[mqtt] health publish aborted: overflow"); return false; }

    bool ok = client.publish(healthTopic, payload, false);  // QoS 0, not retained
    if (ok) logf("[mqtt] published %s %s\n", healthTopic, payload);
    else    logf("[mqtt] health publish FAILED: state=%d\n", client.state());
    return ok;
}

// Publish one reflectance measurement to the spectrum topic: mode=reflect + plant +
// request_id + status + raw dark_*/lit_*/net_* (10 channels) + quality flags. QoS0,
// not retained. Internal (called from reflectLoop). Returns true if accepted.
static bool mqttPublishReflect(const ReflectReading& r, const char* reqId, const char* plant,
                               int32_t seq = -1, float elapsedMs = NAN) {
    if (!mqttConnected() || !r.valid) return false;
    const char* status = r.saturated ? "saturated" : (r.ambient_leak ? "ambient_leak" : "ok");
    static const char* const NAMES[10] =
        {"f415", "f445", "f480", "f515", "f555", "f590", "f630", "f680", "clear", "nir"};

    char payload[1600];
    size_t n = 0;
    int w = snprintf(payload, sizeof(payload),
        "{\"mode\":\"reflect\",\"plant\":\"%s\",\"request_id\":\"%s\",\"status\":\"%s\"",
        plant, reqId, status);
    if (w < 0 || (size_t)w >= sizeof(payload)) { logln("[mqtt] reflect aborted: overflow"); return false; }
    n = (size_t)w;

    for (int i = 0; i < 10; i++) {
        w = snprintf(payload + n, sizeof(payload) - n,
            ",\"dark_%s\":%.1f,\"lit_%s\":%.1f,\"net_%s\":%.1f",
            NAMES[i], r.dark[i], NAMES[i], r.lit[i], NAMES[i], r.net[i]);
        if (w < 0 || (size_t)w >= sizeof(payload) - n) { logln("[mqtt] reflect aborted: overflow"); return false; }
        n += (size_t)w;
    }

    // NIR (AS7263) — channels only when valid; the nir_* flags always ride along.
    if (r.nir_valid) {
        static const char* const NIR[6] = {"n610", "n680", "n730", "n760", "n810", "n860"};
        for (int i = 0; i < 6; i++) {
            w = snprintf(payload + n, sizeof(payload) - n,
                ",\"dark_%s\":%.1f,\"lit_%s\":%.1f,\"net_%s\":%.1f",
                NIR[i], r.nir_dark[i], NIR[i], r.nir_lit[i], NIR[i], r.nir_net[i]);
            if (w < 0 || (size_t)w >= sizeof(payload) - n) { logln("[mqtt] reflect aborted: overflow"); return false; }
            n += (size_t)w;
        }
    }

    w = snprintf(payload + n, sizeof(payload) - n,
        ",\"saturated\":%.1f,\"ambient_leak\":%.1f,\"spectrum_read_ms\":%.1f,"
        "\"nir_valid\":%.1f,\"nir_saturated\":%.1f,\"nir_status\":\"%s\",\"nir_read_ms\":%.1f",
        r.saturated ? 1.0 : 0.0, r.ambient_leak ? 1.0 : 0.0, r.read_ms,
        r.nir_valid ? 1.0 : 0.0, r.nir_saturated ? 1.0 : 0.0, r.nir_status,
        r.nir_valid ? r.nir_read_ms : 0.0);
    if (w < 0 || (size_t)w >= sizeof(payload) - n) { logln("[mqtt] reflect aborted: overflow"); return false; }
    n += (size_t)w;

    if (seq >= 0) {  // burst sample: session sequence + elapsed time
        w = snprintf(payload + n, sizeof(payload) - n, ",\"seq\":%ld,\"elapsed_ms\":%.1f",
                     (long)seq, elapsedMs);
        if (w < 0 || (size_t)w >= sizeof(payload) - n) { logln("[mqtt] reflect aborted: overflow"); return false; }
        n += (size_t)w;
    }
    w = snprintf(payload + n, sizeof(payload) - n, "}");
    if (w < 0 || (size_t)w >= sizeof(payload) - n) { logln("[mqtt] reflect aborted: overflow"); return false; }
    n += (size_t)w;

    bool ok = client.publish(spectrumTopic, payload, false);
    if (ok) logf("[mqtt] published %s (reflect, %uB)\n", spectrumTopic, (unsigned)n);
    else    logf("[mqtt] reflect publish FAILED: state=%d len=%u\n", client.state(), (unsigned)n);
    return ok;
}

bool reflectBusy() { return reflectState == REFLECT_MEASURING; }

void reflectLoop() {
    // Disconnected: run LOCAL cleanup only. We can't publish; onConnected() re-sends the
    // retained state on reconnect. Abort any in-flight measurement so the LED can't stay ON.
    if (!mqttConnected()) {
        if (reflectState == REFLECT_MEASURING) {
            reflectAbort();               // LED off; reset both state machines
            reflectState = REFLECT_IDLE;
            burstActive  = false;
        }
        return;                           // don't accept new cmds while offline
    }

    if (cmdPending) {                     // drain one queued command per tick
        cmdPending = false;
        handleReflectCmd(pendRaw);
    }

    if (reflectState != REFLECT_MEASURING) return;

    // ---- Burst: stream lit samples until the window closes (or the hard cap) ----
    if (burstActive) {
        ReflectReading rr;
        ReflectStatus st = reflectBurstPoll(&rr);
        if (st == REFLECT_DONE) {
            uint32_t elapsed = millis() - burstStartMs;
            if (!mqttPublishReflect(rr, curReqId, curPlant, burstSeq, (float)elapsed)) burstFailCount++;
            burstSeq++;
            if (millis() >= burstDeadline || burstSeq >= BURST_MAX_SAMPLES) {
                reflectBurstEnd();
                reflectState = REFLECT_IDLE;
                burstActive  = false;
                publishReflectState();    // idle
                publishReflectResult(curReqId, curPlant, "done", "burst", burstSeq, burstFailCount);
            } else {
                reflectBurstNext();       // kick the next lit read
            }
        } else if (st == REFLECT_TIMEOUT || st == REFLECT_NA) {
            reflectBurstEnd();
            reflectState = REFLECT_IDLE;
            burstActive  = false;
            publishReflectState();
            publishReflectResult(curReqId, curPlant, "error",
                                 st == REFLECT_NA ? "no_sensor" : "timeout", burstSeq, burstFailCount);
        }
        // REFLECT_BUSY: keep polling next iteration
        return;
    }

    // ---- One-shot (unchanged): a single dark/lit + AS7263 ----
    ReflectReading rr;
    ReflectStatus st = reflectPoll(&rr);
    if (st == REFLECT_DONE) {
        logf("[reflect] done read_ms=%.0f vis_clear(d=%.0f l=%.0f net=%.0f) sat=%d leak=%d "
             "| nir=%s n810(d=%.0f l=%.0f net=%.0f)\n",
             rr.read_ms, rr.dark[8], rr.lit[8], rr.net[8], (int)rr.saturated, (int)rr.ambient_leak,
             rr.nir_status, rr.nir_dark[4], rr.nir_lit[4], rr.nir_net[4]);
        mqttPublishReflect(rr, curReqId, curPlant);  // full dark/lit/net -> spectrum topic
        reflectState = REFLECT_IDLE;
        publishReflectResult(curReqId, curPlant, "done", "measured");
        publishReflectState();
    } else if (st == REFLECT_TIMEOUT) {
        reflectState = REFLECT_IDLE;
        publishReflectResult(curReqId, curPlant, "error", "timeout");
        publishReflectState();
    } else if (st == REFLECT_NA) {
        reflectState = REFLECT_IDLE;
        publishReflectResult(curReqId, curPlant, "error", "no_sensor");
        publishReflectState();
    }
    // REFLECT_BUSY: keep polling next iteration
}
