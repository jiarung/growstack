#include "servo.h"

#include <Arduino.h>
#include <Adafruit_PWMServoDriver.h>
#include <Wire.h>

#include "cam_pins.h"   // pin allocation authority + camera-collision guards

namespace servo {
namespace {

constexpr uint8_t SDA_PIN = RANGE_PIN_SDA, SCL_PIN = RANGE_PIN_SCL;
constexpr uint8_t PCA9685_ADDR = 0x40;   // A0-A5 all open
constexpr float   PWM_HZ = 50.0f;        // analogue servo standard
constexpr uint32_t SETTLE_MS = 400;

Adafruit_PWMServoDriver pca(PCA9685_ADDR, Wire);
bool ok = false;

// Indexed by AXIS (0 = pan, 1 = tilt), never by raw channel number — the
// channels are wherever the connectors physically went, and an array indexed
// by channel would either be 16 long or silently wrong the moment they move.
uint16_t commanded[2] = {US_UNKNOWN, US_UNKNOWN};
uint8_t  lastCh = 0xFF;      // 0xFF = nothing commanded yet
uint32_t lastMoveMs = 0;

// -1 = not one of the two wired channels.
int axisOf(uint8_t ch) {
    if (ch == CH_PAN) return 0;
    if (ch == CH_TILT) return 1;
    return -1;
}

}  // namespace

uint32_t settleMs() { return SETTLE_MS; }
bool present() { return ok; }
uint16_t lastUs(uint8_t ch) {
    const int ax = axisOf(ch);
    return ax < 0 ? US_UNKNOWN : commanded[ax];
}

bool begin() {
    // Idempotent: rangefinderBegin() has usually already opened this bus, and
    // re-begin with the SAME pins is harmless. Calling it here as well means
    // the servos still work if the VL53L0X is absent or fails to init.
    Wire.begin(SDA_PIN, SCL_PIN);
    ok = pca.begin();
    if (!ok) {
        Serial.printf("[servo] PCA9685 NOT found @ 0x%02X (SDA %u / SCL %u)\n",
                      PCA9685_ADDR, SDA_PIN, SCL_PIN);
        return false;
    }
    // Note a transient the library imposes: its begin() ends with
    // setPWMFreq(1000), so between that and the line below the outputs run at
    // the wrong frequency for a few ms. A held 1500us count becomes a ~75us
    // pulse there — far below the ~500us floor a servo will act on, so it
    // holds rather than jumping. Worth knowing before blaming a twitch on the
    // mechanics.
    pca.setPWMFreq(PWM_HZ);
    // Invariant 2: do NOT touch the outputs here, in either direction.
    Serial.printf("[servo] PCA9685 ok @ 0x%02X (SDA %u / SCL %u) %.0fHz, "
                  "outputs UNTOUCHED (ch%u=PAN ch%u=TILT)\n",
                  PCA9685_ADDR, SDA_PIN, SCL_PIN, PWM_HZ, CH_PAN, CH_TILT);
    return true;
}

bool setUs(uint8_t ch, uint16_t us) {
    const int ax = axisOf(ch);
    // An unwired channel is refused rather than driven: a typo'd number would
    // otherwise emit pulses on a header with nothing on it, look like success,
    // and leave you watching a servo that was never being addressed.
    if (!ok || ax < 0) return false;
    if (us != 0 && (us < US_MIN || us > US_MAX)) return false;

    // Invariant 1 — one axis in motion at a time. Only a command aimed at the
    // OTHER channel has to wait: re-commanding the axis already moving cannot
    // add a second stall current, it redirects the one already in flight.
    if (lastCh != 0xFF && ch != lastCh) {
        uint32_t since = millis() - lastMoveMs;
        if (since < SETTLE_MS) delay(SETTLE_MS - since);
    }

    if (us == 0) {
        pca.setPWM(ch, 0, 4096);
    } else {
        // The library derives counts from its configured oscillator frequency,
        // nominally 25 MHz but +-5% per chip — so the emitted pulse is not
        // exactly `us`. Constant offset: harmless for repeatability (Phase 3's
        // actual metric), wrong for absolute angle. See servo-wiring.md.
        pca.writeMicroseconds(ch, us);
    }
    commanded[ax] = us;
    lastCh = ch;
    lastMoveMs = millis();
    return true;
}

}  // namespace servo
