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
