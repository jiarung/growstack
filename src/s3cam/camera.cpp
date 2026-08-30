#include "camera.h"

#include <Arduino.h>

#include "cam_pins.h"

// The observation dataset wants full stills; QSXGA JPEG at quality 14 runs
// ~500KB-1MB per frame — two framebuffers need PSRAM, which is why init hard-
// fails without it instead of silently degrading the dataset to SVGA.
// NOTE: this esp32-camera defines QSXGA as 2560x1920 (not the OV5640 datasheet
// 2592x1944) — the JSON reports fb->width/height, which is the truth.
static constexpr framesize_t STILL_SIZE  = FRAMESIZE_QSXGA;
static constexpr framesize_t STREAM_SIZE = FRAMESIZE_VGA;    // aiming/focus only
static constexpr int JPEG_QUALITY = 14;   // 0-63, lower = better; 14 is safe at 5MP

static uint16_t sensorPid = 0;

bool cameraInit() {
    if (!psramFound()) {
        Serial.println("[cam] NO PSRAM — cannot hold 5MP framebuffers, aborting init");
        return false;
    }
    camera_config_t c = {};
    c.ledc_channel = LEDC_CHANNEL_0;
    c.ledc_timer   = LEDC_TIMER_0;
    c.pin_pwdn  = CAM_PIN_PWDN;   c.pin_reset = CAM_PIN_RESET;
    c.pin_xclk  = CAM_PIN_XCLK;
    c.pin_sccb_sda = CAM_PIN_SIOD; c.pin_sccb_scl = CAM_PIN_SIOC;
    c.pin_d7 = CAM_PIN_D7; c.pin_d6 = CAM_PIN_D6; c.pin_d5 = CAM_PIN_D5;
    c.pin_d4 = CAM_PIN_D4; c.pin_d3 = CAM_PIN_D3; c.pin_d2 = CAM_PIN_D2;
    c.pin_d1 = CAM_PIN_D1; c.pin_d0 = CAM_PIN_D0;
    c.pin_vsync = CAM_PIN_VSYNC; c.pin_href = CAM_PIN_HREF; c.pin_pclk = CAM_PIN_PCLK;
    c.xclk_freq_hz = 20000000;
    c.pixel_format = PIXFORMAT_JPEG;
    c.frame_size   = STILL_SIZE;
    c.jpeg_quality = JPEG_QUALITY;
    c.fb_count     = 2;
    c.fb_location  = CAMERA_FB_IN_PSRAM;
    c.grab_mode    = CAMERA_GRAB_LATEST;   // stills want the freshest frame, not a queue

    esp_err_t err = esp_camera_init(&c);
    if (err != ESP_OK) {
        Serial.printf("[cam] init failed: 0x%x — wrong pin map? see cam_pins.h alternates\n", err);
        return false;
    }
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        sensorPid = s->id.PID;
        // mild defaults; real tuning is out of scope for phase 1B, but the
        // bring-up scene (hotel room, bare lamp in frame) blows out at the
        // stock AE target — bias it down two notches so aiming is usable
        s->set_vflip(s, 0);
        s->set_hmirror(s, 0);
        s->set_ae_level(s, -2);        // AE target: darker end of the range
        s->set_gainceiling(s, GAINCEILING_8X);   // cap AGC noise-pumping in dim corners
    }
    Serial.printf("[cam] up: sensor=%s  still=QSXGA q=%d  fb=PSRAM x2 "
                  "(actual WxH rides in every capture's JSON)\n",
                  cameraSensorName(), JPEG_QUALITY);
    return true;
}

const char* cameraSensorName() {
    switch (sensorPid) {
        case OV5640_PID: return "OV5640";
        case OV2640_PID: return "OV2640";   // would mean the wrong module is on the FPC
        default: {
            static char buf[20];
            snprintf(buf, sizeof(buf), "unknown(0x%04x)", sensorPid);
            return buf;
        }
    }
}

camera_fb_t* cameraCapture() {
    // Freshness contract: the returned frame was exposed AFTER this call
    // started. Dropping "one stale buffer" does NOT guarantee that (a second
    // queued frame can predate the request) — so drain by the driver's own
    // frame timestamp until one postdates the request, bounded by a timeout.
    // BOTH sides of the comparison must live in the boot-monotonic domain:
    // fb->timestamp is "µs since boot at first DMA buffer", so the request
    // start is esp_timer_get_time() — NEVER gettimeofday(), which jumps to
    // wall-clock at NTP sync and would make every frame look stale forever.
    const int64_t start_us = esp_timer_get_time();
    const uint32_t t0 = millis();
    while (millis() - t0 < 3000) {          // QSXGA runs a few fps; 3s is generous
        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) return nullptr;
        const int64_t fb_us = (int64_t)fb->timestamp.tv_sec * 1000000LL
                              + fb->timestamp.tv_usec;
        if (fb_us >= start_us) return fb;
        esp_camera_fb_return(fb);           // predates the request: not ours
    }
    Serial.println("[cam] no fresh frame within 3s — sensor stalled?");
    return nullptr;
}

void cameraRelease(camera_fb_t* fb) {
    if (fb) esp_camera_fb_return(fb);
}

bool cameraSetStreaming(bool on) {
    sensor_t* s = esp_camera_sensor_get();
    if (!s) return false;
    return s->set_framesize(s, on ? STREAM_SIZE : STILL_SIZE) == 0;
}
