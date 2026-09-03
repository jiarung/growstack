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
