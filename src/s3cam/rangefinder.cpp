#include "rangefinder.h"

#include <Arduino.h>
#include <Adafruit_VL53L0X.h>
#include <Wire.h>

#include "cam_pins.h"   // pin allocation authority + camera-collision guards

namespace {

constexpr uint8_t SDA_PIN = RANGE_PIN_SDA, SCL_PIN = RANGE_PIN_SCL;
constexpr uint8_t VL53L0X_ADDR = 0x29;

Adafruit_VL53L0X lox;
bool ok = false;

}  // namespace

// The plant scan lives at 0.3-1.5 m, which is past the default profile's
// comfortable reach, so range is bought with the two knobs that pay for it:
// LONG_RANGE (lower signal-rate limit + longer VCSEL pulses, ~2 m on a bright
// target) and a fat timing budget (more integration per reading = more range
// and less noise). 200 ms is free here — a stop-settle-capture cycle waits
// far longer than that for the servos anyway.
constexpr uint32_t TIMING_BUDGET_US = 200000;

bool rangefinderBegin() {
    Wire.begin(SDA_PIN, SCL_PIN);
    ok = lox.begin(VL53L0X_ADDR, false, &Wire, Adafruit_VL53L0X::VL53L0X_SENSE_LONG_RANGE);
    if (!ok) {
        Serial.printf("[range] VL53L0X NOT found @ 0x%02X (SDA %u / SCL %u)\n",
                      VL53L0X_ADDR, SDA_PIN, SCL_PIN);
        return false;
    }
    bool budget = lox.setMeasurementTimingBudgetMicroSeconds(TIMING_BUDGET_US);
    Serial.printf("[range] VL53L0X ok @ 0x%02X (SDA %u / SCL %u) long-range, "
                  "budget %lums%s\n", VL53L0X_ADDR, SDA_PIN, SCL_PIN,
                  (unsigned long)(TIMING_BUDGET_US / 1000),
                  budget ? "" : " (budget REJECTED — using default)");
    return true;
}

bool rangefinderPresent() { return ok; }

uint32_t rangefinderIntervalMs() {
    // + margin: the device also needs its inter-measurement gap, and polling
    // right at the budget boundary is what produced alternating "not ready"
    // failures when this was a hand-picked 60 ms against a 200 ms budget.
    return TIMING_BUDGET_US / 1000 + 30;
}

Range rangefinderRead() {
    Range out;
    if (!ok) return out;
    VL53L0X_RangingMeasurementData_t m = {};
    // The driver's OWN return code comes first: on a failed call (device busy,
    // I2C hiccup) it leaves the measurement struct untouched, so reading
    // RangeStatus from it reports uninitialized memory as if the sensor had
    // spoken — that is how "status 220" appeared, a number the sensor cannot
    // produce (its statuses are 0..5 and 255).
    VL53L0X_Error err = lox.rangingTest(&m, false);   // false = no debug spew
    out.api_err = (int8_t)err;
    if (err != VL53L0X_ERROR_NONE) return out;
    out.status = m.RangeStatus;
    // RangeStatus 4 is the documented "out of range" code; anything non-zero
    // means the sensor itself does not trust the number, so neither do we —
    // a bogus distance silently attached to a still would be worse than none.
    if (m.RangeStatus == 0) {
        out.mm = m.RangeMilliMeter;
        out.valid = true;
    }
    return out;
}
