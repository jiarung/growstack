#pragma once

#include "esp_camera.h"

// OV5640 bring-up for the imaging head (phase-1b.md steps 2-5).
// Init at full still resolution (QSXGA — 2560x1920 as this esp32-camera
// defines it; PSRAM framebuffer); the stream endpoint drops the sensor to VGA
// while streaming and restores after.

bool cameraInit();                    // true = sensor probed + configured
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
