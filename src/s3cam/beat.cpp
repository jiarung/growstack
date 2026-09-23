#include "beat.h"

#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "../secrets.h"
#include "camera.h"
#include "health.h"
#include "thermal/thermal_uart.h"

// The station's id names the station. Giving this board the same one would put
// two devices' fields into one series, and the deadman — which alerts per
// (device, field) — would then be unable to tell which of them went quiet.
#ifndef S3CAM_DEVICE_ID
#define S3CAM_DEVICE_ID "s3cam-01"
#endif

namespace beat {
namespace {

WiFiClient net;
PubSubClient mqtt(net);
char topic[96];

constexpr uint32_t PUBLISH_EVERY_MS   = 30000;
constexpr uint32_t RECONNECT_EVERY_MS = 30000;

uint32_t lastPublishMs = 0;
uint32_t lastConnectMs = 0;
uint32_t okCount = 0;
uint32_t failCount = 0;
bool     configOk = false;

bool validId(const char* id) {
    if (!id || !*id) return false;
    for (const char* p = id; *p; ++p) {
        const bool allowed = (*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
                             (*p >= '0' && *p <= '9') || *p == '-' || *p == '_';
        if (!allowed) return false;
    }
    // Names the deadman deliberately ignores. Publishing under one would look
    // like it worked and be watched by nothing — the exact failure this whole
    // module exists to remove, reintroduced by a typo in secrets.h.
    return strcmp(id, "sim") != 0 && strcmp(id, "staging-01") != 0;
}

void task(void*);   // defined below, after the work it runs

}  // namespace

void begin() {
    configOk = validId(S3CAM_DEVICE_ID);
    if (!configOk) {
        Serial.printf("[beat] DISABLED: S3CAM_DEVICE_ID \"%s\" is empty, has odd "
                      "characters, or is a name the deadman ignores (sim / "
                      "staging-01) — a heartbeat nobody watches is worse than "
                      "none, because it looks like coverage\n", S3CAM_DEVICE_ID);
        return;
    }
    // Checked against the RETURN VALUE, not a hand-computed maximum length: the
    // limit then cannot drift from the buffer it protects. A truncated topic is
    // the worst available outcome — the client still connects with the full id
    // and every publish reports success, onto a topic telegraf does not
    // subscribe to. Silent non-coverage, from the module whose whole job is to
    // remove exactly that.
    const int tn = snprintf(topic, sizeof(topic), "monitor-air/%s/telemetry",
                            S3CAM_DEVICE_ID);
    if (tn < 0 || tn >= (int)sizeof(topic)) {
        configOk = false;
        Serial.printf("[beat] DISABLED: S3CAM_DEVICE_ID is too long — the topic "
                      "needs %d bytes and only %u are available. A truncated "
                      "topic publishes successfully to nowhere.\n",
                      tn, (unsigned)sizeof(topic));
        return;
    }
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    // Seconds, and deliberately short. PubSubClient's connect() blocks, and
    // this loop must revisit thermal::poll() often enough that the UART ring
    // cannot overflow between passes — a heartbeat that costs thermal frames
    // has made the board less observable, not more.
    mqtt.setSocketTimeout(2);
    net.setTimeout(2);
    // Low priority and a modest stack: it must never preempt the main loop's
    // UART draining, and it only ever holds one small JSON body.
    if (xTaskCreatePinnedToCore(task, "beat", 4096, nullptr, 1, nullptr,
                                xPortGetCoreID()) != pdPASS) {
        configOk = false;
        Serial.println("[beat] could not start its task — no heartbeat. Running "
                       "it on loop() instead is not the fallback: a blocking "
                       "connect there costs thermal frames.");
        return;
    }
    Serial.printf("[beat] telemetry -> %s every %lus (own task)\n",
                  topic, (unsigned long)(PUBLISH_EVERY_MS / 1000));
}

namespace {

void step() {
    if (WiFi.status() != WL_CONNECTED) return;

    const uint32_t now = millis();
    if (!mqtt.connected()) {
        if (now - lastConnectMs < RECONNECT_EVERY_MS) return;
        lastConnectMs = now;
        // Blocks. That is why this runs on its own task — see beat.h.
        mqtt.connect(S3CAM_DEVICE_ID, MQTT_USER, MQTT_PASS);
        if (!mqtt.connected()) {
            Serial.printf("[beat] broker unreachable (state %d); retry in %lus\n",
                          mqtt.state(), (unsigned long)(RECONNECT_EVERY_MS / 1000));
            return;
        }
        Serial.println("[beat] connected");
    }
    mqtt.loop();

    if (now - lastPublishMs < PUBLISH_EVERY_MS) return;
    lastPublishMs = now;

    // Floats by contract — telegraf's note about int/float field conflicts is
    // not advice, it is what keeps a field from changing type mid-series.
    // Counters are published as the running total rather than a rate so that a
    // reboot is visible as a reset rather than as a plausible small number.
    const gymcu::Parser::Stats ts = thermal::statsSnapshot();
    char body[256];
    const int n = snprintf(body, sizeof(body),
        "{\"uptime_s\":%.0f,\"die_c\":%.1f,\"rssi\":%.0f,"
        "\"heap_free\":%.0f,\"psram_free\":%.0f,"
        "\"thermal_frames\":%.0f,\"cam_idle\":%.0f}",
        (double)(millis() / 1000), (double)health::dieC(), (double)WiFi.RSSI(),
        (double)ESP.getFreeHeap(), (double)ESP.getFreePsram(),
        (double)ts.frames_ok, cameraIsIdle() ? 1.0 : 0.0);

    if (n < 0 || n >= (int)sizeof(body) || !mqtt.publish(topic, body)) {
        failCount++;
        Serial.printf("[beat] publish failed (state %d)\n", mqtt.state());
        return;
    }
    okCount++;
}

void task(void*) {
    for (;;) {
        step();
        // Coarse on purpose: nothing here is urgent, and a task that wakes
        // rarely cannot starve the loop it was moved off.
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

}  // namespace

bool connected() { return mqtt.connected(); }
uint32_t published() { return okCount; }
uint32_t failures() { return failCount; }

}  // namespace beat
