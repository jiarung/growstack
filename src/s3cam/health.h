#pragma once

#include <stdint.h>

// The board's own thermal state — mlx90640 roadmap Phase 2 (overheating).
//
// The ESP32-S3's internal die sensor is the only temperature this board can
// measure about ITSELF. The OV5640 exposes no temperature register through the
// esp32-camera API (there is no such op in sensor.h), so "camera temperature"
// is not directly observable and nothing here should be read as if it were:
// die temperature is a proxy for the board's thermal state, no more.
//
// The PEAK is the point. A degradation that happens while nobody is attached
// to the serial console leaves no evidence otherwise, and "it felt hot" is not
// a measurement. Peak-since-boot plus the uptime it was set at turns a
// suspicion into something an HTTP GET can answer hours later.

namespace health {

void poll();             // cheap and self-pacing; call every loop pass
float dieC();            // most recent sample, NAN before the first
float dieMaxC();         // peak since boot, NAN before the first
uint32_t dieMaxAtS();    // uptime in seconds when that peak was set

// Start a new measurement window. A peak describes the configuration that
// produced it, so changing a cooling knob invalidates the old one: without
// this, the pre-change peak would stand forever and every new setting would
// look like it achieved nothing.
void resetPeak();

}  // namespace health
