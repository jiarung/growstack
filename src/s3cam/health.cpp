#include "health.h"

#include <Arduino.h>
#include <math.h>

namespace health {
namespace {

// The die sensor moves on a thermal time constant of tens of seconds; 1 Hz
// resolves a warm-up ramp with room to spare. It is deliberately NOT sampled
// every pass: the loop runs at 5 ms to keep the thermal UART ring from
// overflowing, and that budget belongs to the UART, not to a slow thermometer.
constexpr uint32_t PERIOD_MS = 1000;

uint32_t lastMs = 0;
bool sampled = false;
float die = NAN;
float dieMax = NAN;
uint32_t dieMaxAt = 0;

}  // namespace

void poll() {
    const uint32_t now = millis();
    if (sampled && now - lastMs < PERIOD_MS) return;
    lastMs = now;
    sampled = true;
    die = temperatureRead();
    // NAN-safe: `die > dieMax` is false when dieMax is NAN, so the first valid
    // sample would never be adopted without the isnan arm.
    if (isfinite(die) && (isnan(dieMax) || die > dieMax)) {
        dieMax = die;
        dieMaxAt = now / 1000;
    }
}

void resetPeak() {
    dieMax = NAN;
    dieMaxAt = 0;
}

float dieC() { return die; }
float dieMaxC() { return dieMax; }
uint32_t dieMaxAtS() { return dieMaxAt; }

}  // namespace health
