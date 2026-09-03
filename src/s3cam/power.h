#pragma once

#include <stdint.h>

// Board-level power knobs — the ESP32-S3 half of the cooling question
// (docs/mlx90640/phase-1b.md 過熱追查). The camera's own knobs live in
// camera.h; this file deliberately owns only what belongs to the SoC.
//
// These exist as RUNTIME settings rather than baked-in constants because the
// question they answer is empirical. Baking in "eco" would mean guessing which
// knob mattered and never finding out; a knob plus /health's die_max_c makes it
// an A/B test the operator can run in one flash.
//
// Nothing here is persisted: a reboot returns to the built-in defaults, which
// keeps a bad experiment from becoming a permanent mystery.

namespace power {

// 240 (default) / 160 / 80 MHz. The SoC is the board's biggest continuous heat
// source; this is its largest single lever. 160 is the safe step — camera DMA
// and WiFi keep up. 80 can starve JPEG encode at full resolution.
bool setCpuMhz(int mhz);
int cpuMhz();

// WiFi transmit power in dBm (default ~19.5). Cutting it helps only while the
// radio is actually transmitting — which, during MJPEG streaming, is most of
// the time. Harmless at desk range; a weak link shows up as rssi in /health.
bool setWifiTxDbm(int dbm);
int wifiTxDbm();

}  // namespace power
