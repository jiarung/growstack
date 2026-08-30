// s3cam — Goouuu ESP32-S3-CAM + OV5640 bring-up (tasks/mlx90640/phase-1b.md).
// The RGB side of the plant thermal-imaging head: live MJPEG for aiming, pull-
// model still capture, proto-observation JSON. Thermal/servo/broker come in
// later phases; this build's whole job is the capture flow and the inventory
// numbers (PSRAM, sensor, GPIO budget) that feed the phase-0 board decision.
#include <Arduino.h>
#include <WiFi.h>

#include "../secrets.h"
#include "camera.h"
#include "endpoints.h"

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

    camOk = cameraInit();
    if (camOk && endpointsStart()) {
        Serial.printf("[s3cam] ready: http://%s/  (/stream /capture /observation /last.jpg)\n",
                      WiFi.localIP().toString().c_str());
    } else if (!camOk) {
        Serial.println("[s3cam] camera init FAILED — endpoints not started; "
                       "try the alternate pin maps in cam_pins.h");
    } else {
        Serial.println("[s3cam] httpd start FAILED");
    }
}

void loop() {
    static uint32_t last = 0;
    if (millis() - last > 30000) {
        last = millis();
        // temperatureRead(): the S3's internal die sensor — coarse but perfect
        // for testing the "hot board = stalling transfers" hypothesis with a
        // number instead of a fingertip
        Serial.printf("[s3cam] up %lus  heap=%u psram_free=%u die=%.1fC wifi=%s rssi=%d\n",
                      (unsigned long)(millis() / 1000), ESP.getFreeHeap(),
                      ESP.getFreePsram(), temperatureRead(),
                      WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str()
                                                    : "DOWN",
                      WiFi.RSSI());
    }
    delay(50);
}
