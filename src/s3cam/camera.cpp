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
static bool camInitOk = false;   // see cameraPresent()
static bool camIdle = false;
static bool raisedForCapture = false;

// --- automatic standby -------------------------------------------------------
// The bench settled the question manual idling only hinted at: with the sensor
// in software standby the module is not hot to the touch, and with it awake it
// is. The heat is duty cycle, on the OV5640 itself — which also strikes the
// regulator and the S3 off the candidate list.
//
// So the default is now to go back to standby on its own. Leaving it awake is
// not a neutral choice: it is a decision to run full-resolution readout plus
// in-sensor JPEG compression forever, for frames nobody reads, and it is the
// one that cooks the part.
//
// 120 s is a STARTING VALUE, not a measurement. It has to be longer than the
// gaps inside a working session — the viewer captures every few seconds, a
// person adjusting a target takes tens of seconds between shots — so that
// ordinary use never pays the 1.2 s wake twice in a row; and short enough that
// a bench left alone cools within a few minutes. Tune it against actual use
// via /power?autoidle=, which is why it is not a compile-time constant.
//
// Waking stays automatic and honest (wakeIfIdle pays the settle), so nothing a
// caller does can end up talking to a sleeping sensor.
static uint32_t autoIdleMs = 120000;
static uint32_t lastActivityMs = 0;
static bool streaming = false;

// loop() runs on the main task and every HTTP handler runs on the httpd task,
// so a flag read on one and written on the other is not a guard — it is a
// window. Without this lock the tick could pass its checks, the httpd task
// could begin a capture, and the standby write would still land: the sensor
// asleep underneath a caller holding a framebuffer.
//
// A mutex rather than portENTER_CRITICAL because what it protects includes an
// SCCB write and, on the wake path, a 1.2 s settle. Spinning with interrupts
// masked for that long would be worse than the race.
//
// `inUse` counts callers holding the sensor — from cameraCapture() until the
// matching cameraRelease(). It is NOT the same as raisedForCapture: when the
// resting size already equals STILL_SIZE nothing is raised, so that flag stays
// false while a framebuffer is very much outstanding.
static SemaphoreHandle_t camLock = nullptr;
static int inUse = 0;

static inline void camLockTake() {
    if (camLock) xSemaphoreTake(camLock, portMAX_DELAY);
}
static inline bool camLockTry() {
    return camLock ? xSemaphoreTake(camLock, 0) == pdTRUE : true;
}
static inline void camLockGive() {
    if (camLock) xSemaphoreGive(camLock);
}

// Scoped, because most of the sensor-mutating functions below have several
// early returns and a hand-placed give on each is one edit away from being
// forgotten on the one path that matters.
struct CamLock {
    CamLock() { camLockTake(); }
    ~CamLock() { camLockGive(); }
    CamLock(const CamLock&) = delete;
    CamLock& operator=(const CamLock&) = delete;
};

void cameraNoteActivity() { lastActivityMs = millis(); }

bool cameraSetAutoIdleMs(uint32_t ms) {
    // 0 disables. Anything under the wake settle would spend more time waking
    // than sleeping, so it is refused rather than quietly clamped. (The
    // seconds-to-milliseconds overflow guard lives at the endpoint, where the
    // conversion happens.)
    if (ms != 0 && ms < 2000) return false;
    // Under the lock, and the two writes together. Racing the tick, a new
    // timeout could be recorded a moment after the tick had already read the
    // OLD one and decided to sleep — so setting a longer timeout would be
    // followed immediately by the standby it was meant to postpone.
    camLockTake();
    autoIdleMs = ms;
    lastActivityMs = millis();
    camLockGive();
    return true;
}

uint32_t cameraAutoIdleMs() { return autoIdleMs; }

bool cameraInit() {
    // Before anything can be captured, and before the HTTP server exists.
    if (!camLock) camLock = xSemaphoreCreateMutex();
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

    camInitOk = false;
    esp_err_t err = esp_camera_init(&c);
    if (err != ESP_OK) {
        Serial.printf("[cam] init failed: 0x%x — wrong pin map? see cam_pins.h alternates\n", err);
        return false;
    }
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        sensorPid = s->id.PID;
        // Orientation is pinned to a known state; exposure is NOT.
        //
        // This used to force ae_level -2 and GAINCEILING_8X. Both were tuned
        // for one bring-up scene (a hotel room with a bare lamp in frame that
        // blew out at the stock AE target) and then quietly became permanent —
        // which is why every frame on a dim bench came back nearly black, with
        // the gain ceiling refusing to make up the difference. A value chosen
        // for a scene that no longer exists is worse than no value: it is an
        // opinion nobody remembers holding.
        //
        // So the sensor now keeps the driver's own defaults, and /cam/tune
        // makes these adjustable at runtime — same reasoning as /power's
        // cooling knobs: which setting is right is an empirical question, and
        // baking in a guess is how you stop asking it.
        // Both on: the module is mounted upside down on the pan/tilt head, so
        // the sensor's own frame arrives rotated 180 degrees. Correcting it
        // HERE, in the sensor's registers, means every consumer — /capture,
        // /stream, /observation, the viewer, the registration fit — sees one
        // orientation and none of them needs a flag. The alternative, each
        // tool flipping for itself, is the shape of bug this tooling has
        // already been caught by twice.
        //
        // Runtime-adjustable through /cam/tune?hmirror=&vflip= while a
        // mounting is still being decided; these are the values that survive a
        // reboot. If the head is ever remounted the right way up, change them
        // back rather than compensating downstream.
        s->set_vflip(s, 1);
        s->set_hmirror(s, 1);
        // Buffers are allocated; now drop to the resting size so the sensor is
        // not free-running at 5MP for the entire time nobody is asking.
        if (restSize != STILL_SIZE) s->set_framesize(s, restSize);
    }
    Serial.printf("[cam] up: sensor=%s  still=QSXGA q=%d  fb=PSRAM x2  rest=%s "
                  "(actual WxH rides in every capture's JSON)\n",
                  cameraSensorName(), JPEG_QUALITY, cameraRestSizeName());
    camInitOk = true;
    return true;
}

bool cameraPresent() { return camInitOk; }

// --- runtime sensor tuning (see /cam/tune) ---------------------------------
// Every setter REJECTS an out-of-range value rather than clamping it: a
// silently clamped typo reads as success and sends you looking for a fault in
// the optics that is not there.

bool cameraSetAeLevel(int level) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_ae_level || level < -2 || level > 2) return false;
    return s->set_ae_level(s, level) >= 0;
}

bool cameraSetGainCeiling(int x) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    // Accepts the multiplier the datasheet talks in (2..128), not the enum
    // index, so the query string says what it means.
    int n = -1;
    for (int i = 0; i <= 6; i++) if (x == (1 << (i + 1))) n = i;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_gainceiling || n < 0) return false;
    return s->set_gainceiling(s, (gainceiling_t)n) >= 0;
}

bool cameraSetBrightness(int level) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_brightness || level < -2 || level > 2) return false;
    return s->set_brightness(s, level) >= 0;
}

bool cameraSetMirror(int on) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_hmirror || (on != 0 && on != 1)) return false;
    return s->set_hmirror(s, on) >= 0;
}

bool cameraSetFlip(int on) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_vflip || (on != 0 && on != 1)) return false;
    return s->set_vflip(s, on) >= 0;
}

CameraTune cameraTune() {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    CameraTune t;
    sensor_t* s = esp_camera_sensor_get();
    if (!s) return t;
    t.ae_level = s->status.ae_level;
    // status.gainceiling is only meaningful once something has SET it through
    // the driver API: the struct records what was written, it does not read the
    // sensor back. Since init deliberately no longer forces a ceiling, the
    // field starts as whatever was in memory — 24 on this board, which the
    // obvious `1 << (n+1)` turns into a confident 33554432. Report 0 for
    // "not known" instead of a fabricated multiplier, and expose the raw value
    // so the reader can see WHY it is unknown.
    t.gainceiling_raw = s->status.gainceiling;
    t.gainceiling_x = (s->status.gainceiling <= 6) ? (1 << (s->status.gainceiling + 1)) : 0;
    t.brightness = s->status.brightness;
    t.hmirror = s->status.hmirror;
    t.vflip = s->status.vflip;
    t.ok = true;
    return t;
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

static bool setIdleLocked(bool idle);   // defined below; camLock must be held

// Called only with camLock held — see setIdleLocked().
static void wakeIfIdle() {
    if (!camIdle) return;
    if (!setIdleLocked(false)) {
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
    // Claim the sensor BEFORE waking or raising it. The auto-idle tick takes
    // the same lock, so from here until cameraRelease() it cannot put the
    // sensor to sleep underneath this capture.
    camLockTake();
    inUse++;
    cameraNoteActivity();
    wakeIfIdle();                       // may delay; the tick simply skips
    raisedForCapture = raiseToStill();
    camLockGive();

    camera_fb_t* fb = captureFresh();

    if (!fb) {
        // No buffer went out, so the claim ends here rather than at a
        // cameraRelease() the caller has no reason to make.
        camLockTake();
        if (raisedForCapture) {
            dropToRest();
            raisedForCapture = false;
        }
        inUse--;
        cameraNoteActivity();
        camLockGive();
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
    camLockTake();
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
    // The clock starts when the capture is FINISHED, not when it began: a slow
    // 5 MP shot must not spend its own duration counting towards the idle
    // timeout it is trying not to trip.
    cameraNoteActivity();
    if (inUse > 0) inUse--;
    camLockGive();
}

bool cameraSetStreaming(bool on) {
    camLockTake();
    if (on) wakeIfIdle();   // a stream into a standby sensor is a blank page
    cameraNoteActivity();
    // STOPPING is recorded before the reconfiguration, and unconditionally: if
    // set_framesize fails on the way down we still are not streaming, and
    // leaving the flag set would block auto-idle for the rest of the boot.
    if (!on) streaming = false;
    sensor_t* s = esp_camera_sensor_get();
    if (!s) { camLockGive(); return false; }
    // ending a stream returns to REST, not to full resolution — going back to
    // QSXGA here is exactly the accident that made idle the hottest state
    const bool ok = s->set_framesize(s, on ? STREAM_SIZE : restSize) == 0;
    // STARTING is recorded only once the sensor is actually reconfigured. Set
    // before the attempt, a failed start left `streaming` true with no handler
    // left alive to clear it — and cameraTickAutoIdle() skips on that flag, so
    // one failed /stream would have kept the sensor awake until reboot. The
    // failure mode of the cooling feature must not be "silently off forever".
    if (on) streaming = ok;
    camLockGive();
    return ok;
}

bool cameraSetRestSize(const char* name) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    framesize_t want;
    if      (!strcmp(name, "vga"))   want = FRAMESIZE_VGA;
    else if (!strcmp(name, "svga"))  want = FRAMESIZE_SVGA;
    else if (!strcmp(name, "qsxga")) want = STILL_SIZE;
    else return false;
    // No sensor means nothing to resize, and returning true here would have
    // /power answer "ok" AND reset the die-temperature peak for a change that
    // never reached any hardware — the same shape of lie as a silently clamped
    // typo. Every sibling setter already guards this; this one was the omission.
    sensor_t* s = esp_camera_sensor_get();
    if (!s) return false;
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
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
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
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    return s ? s->xclk_freq_hz : 0;
}

// Assumes camLock is HELD. Two callers already hold it — wakeIfIdle() inside a
// capture, and the auto-idle tick — and the mutex is not recursive, so taking
// it here would deadlock them. The public entry point below is the one that
// locks; keeping them separate is what stops /power?cam=active from being
// undone by a tick that read the timestamp a moment too early.
static bool setIdleLocked(bool idle) {
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
    // Waking IS activity. Without this, /power?cam=active after the timeout
    // has elapsed wakes the sensor and the very next loop pass puts it
    // straight back to sleep — the documented control would appear to do
    // nothing at all.
    if (!idle) lastActivityMs = millis();
    return true;
}

void cameraWakeLocked() {
    wakeIfIdle();
    cameraNoteActivity();
}

bool cameraSetIdle(bool idle) {
    camLockTake();
    const bool ok = setIdleLocked(idle);
    camLockGive();
    return ok;
}

bool cameraIsIdle() { return camIdle; }

void cameraTickAutoIdle() {
    // Non-blocking: the lock being held means somebody is mid-capture, which
    // is itself the answer. Waiting for it on the main loop would stall
    // thermal::poll(), and a stalled poll loses UART bytes.
    if (!camLockTry()) return;
    // Every check and the standby write happen while holding it, so a capture
    // starting on the httpd task blocks at cameraCapture()'s own take() until
    // this has finished deciding.
    if (autoIdleMs == 0 || camIdle || streaming || inUse > 0 ||
        raisedForCapture || millis() - lastActivityMs < autoIdleMs) {
        // Unsigned subtraction above, so the 49-day millis() wrap is a
        // non-event rather than a day the camera never sleeps again.
        camLockGive();
        return;
    }
    if (setIdleLocked(true)) {
        Serial.printf("[cam] auto-idle after %lus of no capture\n",
                      (unsigned long)(autoIdleMs / 1000));
    } else {
        // Refusing forever would retry every loop pass and fill the log. One
        // line, then behave as though it had worked: the next capture wakes
        // anyway, and a sensor that will not enter standby is not a fault that
        // stops anything else working.
        Serial.println("[cam] auto-idle refused by the sensor; not retrying");
        autoIdleMs = 0;
    }
    camLockGive();
}

uint32_t cameraIdleInMs() {
    // The same conditions the tick refuses on, INCLUDING inUse. Leaving that
    // one out let /power print a countdown that could not elapse: a slow
    // response still holding a framebuffer blocks standby until
    // cameraRelease(), and a status line that says "3 s" while the answer is
    // "not until this finishes" is a status line that has to be second-guessed.
    if (autoIdleMs == 0 || camIdle || streaming || raisedForCapture ||
        inUse > 0) {
        return 0;
    }
    const uint32_t since = millis() - lastActivityMs;
    return since >= autoIdleMs ? 0 : autoIdleMs - since;
}

CameraSensorLock::CameraSensorLock() { camLockTake(); }
CameraSensorLock::~CameraSensorLock() { camLockGive(); }

CameraExclusive::CameraExclusive(uint32_t timeout_ms) {
    const uint32_t deadline = millis() + timeout_ms;
    for (;;) {
        camLockTake();
        if (inUse == 0 && !streaming) {
            held_ = true;              // keep the lock; ~CameraExclusive gives it
            cameraNoteActivity();      // this IS use, so do not idle out from under it
            return;
        }
        camLockGive();
        // Signed compare against the deadline so the millis() wrap is a
        // non-event rather than an instant timeout every 49 days.
        if ((int32_t)(millis() - deadline) >= 0) return;
        delay(20);
    }
}

CameraExclusive::~CameraExclusive() {
    if (held_) {
        cameraNoteActivity();
        camLockGive();
    }
}

int cameraRegRead(int reg) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    return cameraRegReadLocked(reg);
}

int cameraRegReadLocked(int reg) {
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->get_reg) return -1;
    // mask 0xFF: we want the byte as it stands, not a field of it
    int v = s->get_reg(s, reg, 0xFF);
    return (v < 0 || v > 0xFF) ? -1 : v;
}

bool cameraRegWrite(int reg, int mask, int value) {
    // Same mutex as the auto-idle tick: it writes the standby register, and a
    // standby landing partway through another SCCB transaction leaves the
    // sensor half-configured with nothing reporting it.
    CamLock lk;
    sensor_t* s = esp_camera_sensor_get();
    if (!s || !s->set_reg) return false;
    if (sensorPid != OV5640_PID) return false;   // addresses are part-specific
    return s->set_reg(s, reg, mask, value) >= 0;
}
