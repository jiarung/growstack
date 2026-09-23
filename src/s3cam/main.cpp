// s3cam — Goouuu ESP32-S3-CAM + OV5640 bring-up (docs/mlx90640/phase-1b.md).
// The RGB side of the plant thermal-imaging head: live MJPEG for aiming, pull-
// model still capture, proto-observation JSON. Thermal/servo/broker come in
// later phases; this build's whole job is the capture flow and the inventory
// numbers (PSRAM, sensor, GPIO budget) that feed the phase-0 board decision.
#include <Arduino.h>
#include <WiFi.h>

#include "../secrets.h"
#include "af.h"
#include "camera.h"
#include "endpoints.h"
#include "health.h"
#include "rangefinder.h"
#include "servo.h"
#include "thermal/thermal_uart.h"

// Away-from-home bring-up: put the hotspot's creds in secrets.h as
//   #define S3CAM_WIFI_SSID "..."
//   #define S3CAM_WIFI_PASS "..."
// and they take precedence; otherwise the station's WIFI_SSID/PASSWORD apply.
#ifndef S3CAM_WIFI_SSID
#define S3CAM_WIFI_SSID WIFI_SSID
#define S3CAM_WIFI_PASS WIFI_PASSWORD
#endif

static bool camOk = false;

void setup() {
    Serial.begin(115200);
    delay(300);
    // Kill the board's attention-seeking LEDs (both pins are free in our camera
    // map). GPIO2 = the usual flash-LED spot on S3-CAM clones; GPIO48 = the
    // usual WS2812 status pixel. The tiny red POWER led is hardwired — tape it.
    pinMode(2, OUTPUT);
    digitalWrite(2, LOW);
    neopixelWrite(48, 0, 0, 0);
    Serial.println("\n[s3cam] phase-1B bring-up build");
    Serial.printf("[s3cam] flash=%uMB psram=%u bytes\n",
                  ESP.getFlashChipSize() / (1024 * 1024), ESP.getPsramSize());

    WiFi.mode(WIFI_STA);
    // Not a substitute for the supervisor in loop() — the driver's own retry
    // gives up in cases the supervisor still recovers from — but it costs
    // nothing and handles the easy half.
    WiFi.setAutoReconnect(true);
    WiFi.persistent(false);
    WiFi.begin(S3CAM_WIFI_SSID, S3CAM_WIFI_PASS);
    Serial.printf("[wifi] connecting to %s", S3CAM_WIFI_SSID);
    for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) {
        delay(500);
        Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] connected, IP %s\n", WiFi.localIP().toString().c_str());
        // capture ids want wall-clock; a hotspot without NTP just falls back to
        // boot-millis ids — the endpoints handle both
        configTime(0, 0, "pool.ntp.org", "time.google.com");
    } else {
        Serial.println("[wifi] NOT connected — endpoints will start anyway; "
                       "check S3CAM_WIFI_SSID in secrets.h (AP isolation? see phase-1b.md risks)");
    }

    // rangefinder before the camera: it owns its own I2C pins, and knowing
    // whether it answered belongs in the same boot log as the sensor probe.
    // Its absence is never fatal — distance simply reports null.
    rangefinderBegin();
    // Same bus, different address (0x40). Leaves the outputs UNTOUCHED — see
    // servo.h invariant 2: on an ESP32-only reboot the PCA9685 is still driving
    // the axes, and releasing them here is what would drop a mounted head.
    servo::begin();
    // the thermal module streams on its own as soon as it is powered; opening
    // the port early means the parser sees the stream from the first frame
    thermal::begin();

    // The camera's success does NOT gate the server any more. It used to, and the
    // consequence was that a sensor this board can no longer probe took the
    // thermal UART, the rangefinder, the servos and /health offline with it —
    // subsystems that share nothing with the camera except a PCB. The symptom
    // was a bare "Connection refused" with no way to ask the board why, which is
    // the worst possible failure for a board whose job is now to be diagnosable.
    camOk = cameraInit();
    if (!camOk) {
        Serial.println("[s3cam] camera init FAILED — /capture /stream /observation "
                       "will report it; everything else still serves. Wrong pin map? "
                       "see cam_pins.h alternates");
    }
    if (endpointsStart()) {
        Serial.printf("[s3cam] ready: http://%s/   camera=%s thermal=on range=%s servo=%s\n",
                      WiFi.localIP().toString().c_str(),
                      camOk ? "ok" : "ABSENT",
                      rangefinderPresent() ? "ok" : "absent",
                      servo::present() ? "ok" : "absent");
        // AUTOFOCUS, after the server is up on purpose. The upload is 4 KB over
        // SCCB and the verify reads every byte back, so this blocks for seconds
        // — done before endpointsStart() the board would simply be unreachable
        // for that time, with no way to ask it why.
        //
        // af.h used to say nothing was loaded at boot, because whether it
        // worked was the open question and doing it automatically would bury
        // the answer in the boot log. That question is answered: the firmware
        // loads and runs on this board. What remains is a lens that only moves
        // once it has been loaded, so loading it is now part of being ready.
        //
        // Failure is not fatal. Every other subsystem, and the camera itself,
        // work without autofocus; a fixed lens is worse than a focused one and
        // far better than a board that refuses to boot.
        if (camOk) {
            if (!af::load()) {
                const af::Status st = af::status();
                Serial.printf("[af] load FAILED (%s) — capture still works, the "
                              "lens just will not move; retry with /cam/af?load=1\n",
                              st.note);
            } else if (!af::focus()) {
                Serial.println("[af] loaded, but the focus command was not "
                               "acknowledged — see /cam/af");
            } else {
                Serial.println("[af] loaded and focused");
            }
            // OPEN QUESTION, deliberately not guessed at: /power's auto-idle
            // puts the sensor into software standby (0x3008 bit6) after two
            // minutes, and whether that resets the 8051 holding this firmware
            // has not been measured. If focus stops working after the board has
            // been left alone, that is the first thing to check — /cam/af reads
            // fw_state live, so it will say so, and ?load=1 restores it.
        }
    } else {
        Serial.println("[s3cam] httpd start FAILED — nothing is reachable");
    }
}

// --- staying reachable -------------------------------------------------------
// An unattended board that loses Wi-Fi and does nothing about it is worse than
// one that crashes: a crash reboots and comes back, while this keeps running,
// keeps heating, and is indistinguishable from dead to everything that wants
// to talk to it. Until now loop() printed "wifi=DOWN" every 30 s and that was
// all — on a bench nobody is watching over serial, which is the situation this
// board exists for.
//
// Two stages, because they fail differently. Re-associating fixes an AP that
// rebooted or a roam that went wrong. A restart is for the states the radio
// cannot talk itself out of — and it is safe here specifically: the PCA9685
// keeps its own PWM registers across an ESP reset, so the head does not move,
// and servo.h's invariant is that nothing is commanded at boot.
static constexpr uint32_t WIFI_RETRY_AFTER_MS = 30000;    // down this long -> re-associate
static constexpr uint32_t WIFI_REBOOT_AFTER_MS = 600000;  // still down -> restart
static uint32_t wifiDownSinceMs = 0;
static uint32_t wifiLastRetryMs = 0;
uint32_t wifiReconnects = 0;   // reported by /health: a rising count is a flapping link

static void wifiSupervise() {
    if (WiFi.status() == WL_CONNECTED) {
        if (wifiDownSinceMs) {
            Serial.printf("[wifi] back after %lus, IP %s\n",
                          (unsigned long)((millis() - wifiDownSinceMs) / 1000),
                          WiFi.localIP().toString().c_str());
            wifiReconnects++;
            wifiDownSinceMs = 0;
            // On EVERY recovery, not only the first connect in setup(). A board
            // that boots before its AP does never runs setup()'s configTime, so
            // without this it would come back on the network and stay on
            // boot-millis capture ids for the rest of the session — every
            // observation stamped "unsynced" while the link was fine. Calling
            // it again on a link that already had time is harmless; not calling
            // it on one that never did is a whole dataset with no wall clock.
            configTime(0, 0, "pool.ntp.org", "time.google.com");
        }
        return;
    }
    const uint32_t now = millis();
    if (!wifiDownSinceMs) {
        wifiDownSinceMs = now;
        wifiLastRetryMs = now;
        Serial.println("[wifi] link DOWN — everything else keeps running; "
                       "re-associating in 30s, restarting after 10min");
        return;
    }
    if (now - wifiDownSinceMs >= WIFI_REBOOT_AFTER_MS) {
        Serial.println("[wifi] still down after 10 min — restarting. The servo "
                       "driver keeps its own PWM, so the head does not move.");
        Serial.flush();
        ESP.restart();
    }
    if (now - wifiLastRetryMs >= WIFI_RETRY_AFTER_MS) {
        wifiLastRetryMs = now;
        Serial.printf("[wifi] retry (down %lus)\n",
                      (unsigned long)((now - wifiDownSinceMs) / 1000));
        WiFi.disconnect();
        WiFi.begin(S3CAM_WIFI_SSID, S3CAM_WIFI_PASS);
    }
}

void loop() {
    wifiSupervise();   // an unreachable board is a dead board; see above
    thermal::poll();   // drain Serial1 every pass; never blocks
    health::poll();    // self-pacing at 1 Hz; tracks the die-temperature peak
    cameraTickAutoIdle();   // back to standby when nobody has captured; see camera.h

    static uint32_t last = 0;
    if (millis() - last > 30000) {
        last = millis();
        // The die sensor is the S3's own, NOT the camera's — the OV5640 has no
        // readable temperature. It tests the "hot board = stalling transfers"
        // hypothesis with a number instead of a fingertip, and the PEAK is what
        // survives the hours when no console is attached (also on /health).
        const gymcu::Parser::Stats ts = thermal::statsSnapshot();
        Serial.printf("[thermal] bytes=%lu frames=%lu bad_cs=%lu bad_hdr=%lu "
                      "resync=%lu dropped=%lu timeouts=%lu\n",
                      (unsigned long)thermal::bytesSeen(), (unsigned long)ts.frames_ok,
                      (unsigned long)ts.bad_checksum, (unsigned long)ts.bad_header,
                      (unsigned long)ts.resyncs, (unsigned long)ts.bytes_dropped,
                      (unsigned long)ts.timeouts);
        Serial.printf("[s3cam] up %lus  heap=%u psram_free=%u die=%.1fC "
                      "(peak %.1fC @%lus) wifi=%s rssi=%d\n",
                      (unsigned long)(millis() / 1000), ESP.getFreeHeap(),
                      ESP.getFreePsram(), health::dieC(), health::dieMaxC(),
                      (unsigned long)health::dieMaxAtS(),
                      WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str()
                                                    : "DOWN",
                      WiFi.RSSI());
    }
    // 5 ms, not 50: thermal::poll() must revisit the UART ring often enough
    // that a burst cannot overflow it between passes (see thermal_uart.h).
    delay(5);
}
