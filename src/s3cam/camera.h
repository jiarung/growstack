#pragma once

#include "esp_camera.h"

// OV5640 bring-up for the imaging head (phase-1b.md steps 2-5).
// Init at full still resolution (QSXGA — 2560x1920 as this esp32-camera
// defines it; PSRAM framebuffer); the stream endpoint drops the sensor to VGA
// while streaming and restores after.

bool cameraInit();                    // true = sensor probed + configured

// Did cameraInit() succeed? Asked by the endpoints, which now start whether or
// not the camera did: a dead sensor must not take the thermal UART, the
// rangefinder, the servos and /health down with it — they share nothing with it
// but a board.
bool cameraPresent();
const char* cameraSensorName();       // "OV5640" / "OV2640" / "unknown(0x..)"
camera_fb_t* cameraCapture();         // full-res capture; caller MUST return it
void cameraRelease(camera_fb_t* fb);  // esp_camera_fb_return wrapper
bool cameraSetStreaming(bool on);     // VGA for stream, QSXGA for stills

// ---- cooling knobs (docs/mlx90640/phase-1b.md 過熱追查) ---------------------
// The RESTING framesize: "vga" (default), "svga", or "qsxga" (the old
// behaviour). The single biggest lever on this board — the driver free-runs the
// sensor forever, and at QSXGA that is a continuous 5MP readout plus the
// OV5640's own JPEG encode for frames nobody reads. Stills are unaffected:
// /capture raises to QSXGA for the shot and drops back when the frame is
// released. Kept switchable so the delta can be measured, not assumed.
bool cameraSetRestSize(const char* name);

// --- automatic standby -------------------------------------------------------
// Measured on the bench: in software standby the module is not hot to the
// touch; awake it is. The heat is the OV5640's own duty cycle, so staying
// awake is not a neutral default — it is a decision to run full-resolution
// readout plus in-sensor JPEG compression indefinitely for frames nobody
// reads. cameraTickAutoIdle() must therefore be called from loop().
//
// Waking remains automatic: /capture and /stream pay the settle honestly, so
// no caller can end up talking to a sleeping sensor.
void cameraTickAutoIdle();          // call every loop pass; cheap, non-blocking
void cameraNoteActivity();          // "somebody used the camera just now"
bool cameraSetAutoIdleMs(uint32_t ms);   // 0 disables; under 2000 is refused
uint32_t cameraAutoIdleMs();
uint32_t cameraIdleInMs();          // ms until standby, 0 when not counting
const char* cameraRestSizeName();

// The sensor's master clock. OV5640 power scales with it, and the DVP/DMA rate
// it drives scales the ESP32 side too — the one knob that cools BOTH chips.
// Halving it halves the frame rate, which costs nothing in a stop-settle-
// capture workflow that takes one photo every few minutes.
bool cameraSetXclkMhz(int mhz);
// The value actually in force, read back off the driver rather than remembered:
// set_xclk's unit is not documented in the header we build against, so the
// readback is what makes the setting trustworthy instead of assumed.
int cameraXclkHz();

// OV5640 software standby (register 0x3008 bit 6). The sensor free-runs from
// init to forever — between captures it fills framebuffers nobody reads. This
// stops that. MANUAL on purpose: waking needs AE/AWB frames to re-converge, so
// a capture taken right after a wake is badly exposed. Automatic idling would
// trade heat for silently bad data; that trade is the operator's to make.
bool cameraSetIdle(bool idle);

// --- sharing the sensor with code outside camera.cpp -------------------------
// Everything that touches the OV5640 over SCCB shares one mutex, because the
// auto-idle tick writes the standby register from the main loop while HTTP
// handlers run on another task. af.cpp uploads 4 KB and then reads every byte
// back; a standby landing in the middle of that verify would fail it for a
// reason having nothing to do with the upload.
//
// Hold this for the WHOLE operation, not per register: a guard taken and
// released 4077 times leaves 4077 gaps.
class CameraSensorLock {
public:
    CameraSensorLock();
    ~CameraSensorLock();
    CameraSensorLock(const CameraSensorLock&) = delete;
    CameraSensorLock& operator=(const CameraSensorLock&) = delete;
};

// Register read for callers already holding CameraSensorLock. The public
// cameraRegRead() takes the lock itself, and this mutex is not recursive.
int cameraRegReadLocked(int reg);

// Clear software standby, paying the AE/AWB settle if it really was asleep.
// Caller must already hold CameraSensorLock or CameraExclusive.
//
// Anything talking to the sensor's embedded MCU has to call this first: in
// standby that core is powered down, so a firmware upload cannot reach ready
// and a focus command cannot be consumed. Holding the mutex is not enough —
// it excludes other callers, it does not turn the sensor on.
void cameraWakeLocked();

// EXCLUSIVE use of the sensor: waits until no capture is outstanding and no
// stream is running, then holds the same mutex until it goes out of scope.
//
// The plain lock is not enough for whole-sensor operations. cameraCapture()
// deliberately releases it before captureFresh() — otherwise every /power
// would block for the length of a 5 MP exposure — so a frame can be in flight
// while nothing holds the mutex. Uploading firmware into the sensor's MCU
// underneath that acquisition corrupts the frame, the upload, or both.
//
// ok() false means the wait timed out; the caller must not touch the sensor.
// Nothing is ever forced: a busy camera is a reason to give up and say so,
// not a reason to interrupt somebody's capture.
class CameraExclusive {
public:
    explicit CameraExclusive(uint32_t timeout_ms = 15000);
    ~CameraExclusive();
    bool ok() const { return held_; }
    CameraExclusive(const CameraExclusive&) = delete;
    CameraExclusive& operator=(const CameraExclusive&) = delete;
private:
    bool held_ = false;
};
bool cameraIsIdle();

// ---- raw register access (the instrument, not a knob) -----------------------
// Before writing a power register on rumour, read one. The heat question has
// reached claims we cannot check from here — is the MIPI PHY still powered on a
// DVP board, which internal clock domains are gated, is the sensor's own DVDD
// regulator fighting the board's LDO — and every one of them is a register the
// sensor will simply tell us about. Guessing an address instead would risk
// browning out a part whose PWDN and RESET pins are BOTH unwired (cam_pins.h),
// leaving a power cycle as the only recovery.
//
// -1 on a failed read (every valid byte is 0..255, so the sentinel cannot
// collide with data). Writes are masked, volatile, and survive nothing: a power
// cycle restores every default, which is what makes experimenting here safe.
int cameraRegRead(int reg);
bool cameraRegWrite(int reg, int mask, int value);

// --- runtime sensor tuning (/cam/tune) -------------------------------------
// Exposure is deliberately NOT fixed at init any more: the previous hard-coded
// ae_level -2 / GAINCEILING_8X were right for one bring-up scene and wrong
// everywhere since, which is exactly the failure a runtime knob prevents.
// All setters reject out-of-range input instead of clamping it.
struct CameraTune {
    bool ok = false;
    int  ae_level = 0;        // -2..2
    // The multiplier itself (2,4,8,...128), or 0 when it cannot be known —
    // see cameraTune(). Never a computed guess.
    int  gainceiling_x = 0;
    int  gainceiling_raw = -1;   // what the driver's status struct actually holds
    int  brightness = 0;      // -2..2
    int  hmirror = 0;
    int  vflip = 0;
};
CameraTune cameraTune();

bool cameraSetAeLevel(int level);      // -2..2
bool cameraSetGainCeiling(int x);      // 2,4,8,16,32,64,128 (not the enum index)
bool cameraSetBrightness(int level);   // -2..2
bool cameraSetMirror(int on);          // 0/1
bool cameraSetFlip(int on);            // 0/1
