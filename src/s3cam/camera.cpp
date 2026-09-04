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

// The RESTING framesize — what the sensor sits at when nobody wants a picture.
//
// This is the board's biggest heat lever, and it used to be QSXGA by accident:
// the driver free-runs the sensor from init to forever, and PIXFORMAT_JPEG on
// an OV5640 means the SENSOR does the compression. So "idle" was full 5MP
// readout plus 5MP JPEG encode, continuously, for frames nobody reads — which
// is why the board measured HOTTER at rest (70C) than while streaming (54C,
// where cameraSetStreaming had dropped it to VGA). Resting at VGA is ~1/17 the
// pixels. /capture raises to QSXGA for the shot and drops back on release.
static framesize_t restSize = FRAMESIZE_VGA;

// A framesize change is not instant: AE/AWB re-converge over the next few
// frames, and at QSXGA those frames are slow. A starting value, to be tuned
// against actual capture quality rather than left at whatever felt safe.
static constexpr uint32_t FRAMESIZE_SETTLE_MS = 500;

static uint16_t sensorPid = 0;
static bool camIdle = false;
static bool raisedForCapture = false;

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
    // ALLOCATION size, not the resting size: the driver sizes its PSRAM
    // framebuffers from this, so it must be the LARGEST we will ever ask for.
    // Initialising at VGA to rest cool would allocate VGA buffers and leave
    // nothing for a QSXGA still. We claim the big buffers here and drop to
    // restSize immediately after init instead.
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
        // Buffers are allocated; now drop to the resting size so the sensor is
        // not free-running at 5MP for the entire time nobody is asking.
        if (restSize != STILL_SIZE) s->set_framesize(s, restSize);
    }
    Serial.printf("[cam] up: sensor=%s  still=QSXGA q=%d  fb=PSRAM x2  rest=%s "
                  "(actual WxH rides in every capture's JSON)\n",
                  cameraSensorName(), JPEG_QUALITY, cameraRestSizeName());
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

// Waking is not instant, and the sensor's AE/AWB restart from defaults — the
// first frames after a wake are badly exposed. Idling is manual (see camera.h),
// but WAKING must not be: leaving it manual would mean /capture and /stream
// silently time out into a 500 whenever the operator forgot, which reads as a
// broken sensor rather than as a mode. So wake automatically, and pay the
// settle honestly instead of returning a fast, badly exposed frame.
static constexpr uint32_t WAKE_SETTLE_MS = 1200;

static void wakeIfIdle() {
    if (!camIdle) return;
    if (!cameraSetIdle(false)) {
        Serial.println("[cam] wake FAILED — sensor may not answer this capture");
        return;
    }
    Serial.printf("[cam] woken from idle; settling %lums for AE/AWB\n",
                  (unsigned long)WAKE_SETTLE_MS);
    delay(WAKE_SETTLE_MS);
}

// Raise to full resolution for one shot. Returns whether a restore is owed —
// the caller must not guess, because dropping back when we never raised would
// reconfigure the sensor for nothing.
static bool raiseToStill() {
    if (restSize == STILL_SIZE) return false;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || s->set_framesize(s, STILL_SIZE) != 0) {
        Serial.println("[cam] could not raise to QSXGA — capturing at rest size");
        return false;
    }
    delay(FRAMESIZE_SETTLE_MS);
    return true;
}

static void dropToRest() {
    sensor_t* s = esp_camera_sensor_get();
    if (s) s->set_framesize(s, restSize);
}

static camera_fb_t* captureFresh();

camera_fb_t* cameraCapture() {
    wakeIfIdle();
    raisedForCapture = raiseToStill();
    camera_fb_t* fb = captureFresh();
    if (!fb && raisedForCapture) {
        // Nothing is held, so it is safe to drop right now. On the success path
        // the restore waits for cameraRelease — see the note there.
        dropToRest();
        raisedForCapture = false;
    }
    return fb;
}

static camera_fb_t* captureFresh() {
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
    // ONLY after the buffer is back with the driver. set_framesize stops and
    // restarts the capture engine, which can free and reallocate the PSRAM
    // framebuffers — doing it while the caller still holds an fb would turn
    // their pointer into a dangling one mid-response. Every cameraCapture()
    // success path in endpoints.cpp reaches exactly one cameraRelease(), so
    // this is the one correct place for the restore.
    if (raisedForCapture) {
        dropToRest();
        raisedForCapture = false;
    }
}

bool cameraSetStreaming(bool on) {
    if (on) wakeIfIdle();   // a stream into a standby sensor is a blank page
    sensor_t* s = esp_camera_sensor_get();
    if (!s) return false;
    // ending a stream returns to REST, not to full resolution — going back to
    // QSXGA here is exactly the accident that made idle the hottest state
    return s->set_framesize(s, on ? STREAM_SIZE : restSize) == 0;
}

bool cameraSetRestSize(const char* name) {
    framesize_t want;
    if      (!strcmp(name, "vga"))   want = FRAMESIZE_VGA;
    else if (!strcmp(name, "svga"))  want = FRAMESIZE_SVGA;
    else if (!strcmp(name, "qsxga")) want = STILL_SIZE;
    else return false;
    restSize = want;
    // apply now unless a capture is mid-flight holding the sensor at QSXGA;
    // its cameraRelease will pick up the new resting size
    if (!raisedForCapture) dropToRest();
    return true;
}

const char* cameraRestSizeName() {
    switch (restSize) {
        case FRAMESIZE_VGA:  return "vga";
        case FRAMESIZE_SVGA: return "svga";
        case STILL_SIZE:     return "qsxga";
        default:             return "other";
    }
}

// ---- cooling knobs ---------------------------------------------------------

bool cameraSetXclkMhz(int mhz) {
    // Below ~6 MHz the OV5640's internal PLL cannot reach a usable pixel clock;
    // above the init value there is no thermal reason to go. Refuse rather than
    // let a typo brick the stream until the next reboot.
    if (mhz < 6 || mhz > 20) return false;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_xclk) return false;
    // The header does not document set_xclk's unit; the drivers take MHz and
    // store Hz in xclk_freq_hz. Verify rather than trust: if the readback is
    // not the Hz we asked for, the call did something else and we say so.
    if (s->set_xclk(s, LEDC_TIMER_0, mhz) != 0) return false;
    return s->xclk_freq_hz == mhz * 1000000;
}

int cameraXclkHz() {
    sensor_t* s = esp_camera_sensor_get();
    return s ? s->xclk_freq_hz : 0;
}

bool cameraSetIdle(bool idle) {
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_reg) return false;
    // 0x3008 is an OV5640 register. Writing it on another sensor would poke
    // something unrelated, so the guard is not politeness — it is correctness.
    if (sensorPid != OV5640_PID) return false;
    // bit 6 = software power down. Touch ONLY that bit: a full-byte write would
    // also set bit 7 (software reset) and the low bits to whatever we assumed,
    // clobbering however esp32-camera left this register. The vendor table
    // (rt-thread k210 BSP, ov5640cfg.h) confirms the semantics —
    // "0x42 // software power down, bit[6]" / "0x02 // wake up from standby" —
    // but it writes the whole byte because it owns the whole configuration.
    // We do not, so we mask.
    if (s->set_reg(s, 0x3008, 0x40, idle ? 0x40 : 0x00) < 0) return false;
    camIdle = idle;
    return true;
}

bool cameraIsIdle() { return camIdle; }

int cameraRegRead(int reg) {
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->get_reg) return -1;
    // mask 0xFF: we want the byte as it stands, not a field of it
    int v = s->get_reg(s, reg, 0xFF);
    return (v < 0 || v > 0xFF) ? -1 : v;
}

bool cameraRegWrite(int reg, int mask, int value) {
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_reg) return false;
    if (sensorPid != OV5640_PID) return false;   // addresses are part-specific
    return s->set_reg(s, reg, mask, value) >= 0;
}
