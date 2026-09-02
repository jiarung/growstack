#pragma once

#include <stdint.h>

// VL53L0X time-of-flight rangefinder on the sensor head (mlx90640 phase 1B+).
// It answers "how far is what the camera is pointed at" — the measurement that
// turns a pile of stills into a focus CURVE (and later feeds Phase 5's
// per-distance registration, where a single global homography does not hold).
//
// Wiring: its own I2C bus on GPIO 41 (SDA) / 42 (SCL) — NOT the camera's SCCB
// pins (4/5), which the esp32-camera driver owns. Address 0x29, XSHUT unused.
//
// Every reading carries its own validity: the sensor reports out-of-range,
// signal-too-weak and other failures per-measurement, and those must never
// reach a dataset as if they were distances.

bool rangefinderBegin();     // true = sensor answered on the bus
bool rangefinderPresent();   // begin() succeeded (no live re-probe)

// A single ranging measurement. `valid` false = no trustworthy distance this
// round (out of range, weak signal, sensor absent); mm is then meaningless.
struct Range {
    uint16_t mm = 0;
    bool valid = false;
    uint8_t status = 255;    // sensor RangeStatus — ONLY meaningful when api_err == 0
    int8_t api_err = 0;      // driver's own error (0 = the call itself worked);
                             // when nonzero the measurement struct is untouched
                             // garbage, so `status` must not be reported as if
                             // the sensor had said something
};
Range rangefinderRead();

// Minimum gap between reads, derived from the configured timing budget: poll
// faster than this and every other measurement is simply not ready yet.
// Callers that loop (the /range burst, a scan) must pace themselves by it
// rather than by a hand-picked delay.
uint32_t rangefinderIntervalMs();
