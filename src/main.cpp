#include <Arduino.h>
#include <WiFi.h>

#include "secrets.h"
#include "sensors.h"
#include "mqtt_client.h"
#include "log.h"

// A fixed AS7341 node streams ambient spectrum; a roving reflectance-only probe sets
// PUBLISH_AMBIENT_SPECTRUM to 0 in its secrets.h to suppress the mode:"ambient" stream.
#ifndef PUBLISH_AMBIENT_SPECTRUM
#define PUBLISH_AMBIENT_SPECTRUM 1
#endif

// Publish cadence — change this one line to retune.
static const uint32_t PUBLISH_INTERVAL_MS = 15000;  // 15 seconds
static const uint32_t WIFI_RETRY_MS       = 10000;

static uint32_t lastPublish = 0;       // 0 = never published yet (publish ASAP once connected)
static uint32_t lastWifiAttempt = 0;

// Non-blocking WiFi: kick off a connect, retry on a timer, never spin-wait.
static void wifiEnsure() {
    if (WiFi.status() == WL_CONNECTED) return;
    uint32_t now = millis();
    if (lastWifiAttempt != 0 && now - lastWifiAttempt < WIFI_RETRY_MS) return;
    lastWifiAttempt = now;
    logf("[wifi] connecting to %s ...\n", WIFI_SSID);
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void setup() {
    Serial.begin(115200);
    // USB-CDC on boot: a reset drops the USB device and the host re-enumerates
    // (~0.5-2s) before the monitor reattaches. Wait for it, otherwise the boot
    // logs below print into that dead window and are lost. Cap the wait so a
    // headless boot (no monitor attached) still proceeds.
    while (!Serial && millis() < 3000) delay(10);
    delay(100);
    Serial.println();  // separate from boot-ROM chatter
    logln("[boot] monitor-air starting");

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    logTimeBegin();  // arm SNTP; syncs in the background once WiFi is up

    if (!sensorsBegin()) {
        logln("[sensors] none available — continuing (will publish nothing useful)");
    }
    mqttSetup();
    wifiEnsure();  // start the first (non-blocking) connection attempt
}

void loop() {
    wifiEnsure();  // non-blocking reconnect
    mqttLoop();    // pump client + rate-limited MQTT reconnect
    reflectLoop(); // drain reflect/cmd + advance the reflectance state machine

    uint32_t now = millis();
    bool due = (lastPublish == 0) || (now - lastPublish >= PUBLISH_INTERVAL_MS);
    // Skip the ambient tick while a reflectance measurement holds the AS7341.
    if (due && mqttConnected() && !reflectBusy()) {
        // Claim this slot unconditionally: advancing only on success would hot-loop
        // (re-reading sensors every iteration) whenever a read yields no valid
        // fields or a publish fails. A failed/empty cycle just waits for the next.
        lastPublish = now;
        SensorReading r = sensorsRead();
        mqttPublish(r);  // logs success / failure / empty-skip internally

#if PUBLISH_AMBIENT_SPECTRUM
        SpectrumReading sp = spectrumRead();
        mqttPublishSpectrum(sp);  // own topic; no-op + no log when AS7341 absent
#endif
    }
}
