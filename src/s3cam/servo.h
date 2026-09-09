#pragma once

#include <stdint.h>

// PCA9685 + 2x MG996R pan/tilt for the imaging head (roadmap Phase 3).
// Wiring, power rail and the discovered mechanical travel live in
// docs/mlx90640/servo-wiring.md — that file is the authority for the physical
// side; this one only encodes what the firmware must not get wrong.
//
// Bus: shares the rangefinder's I2C pins (GPIO41/42) at address 0x40. No new
// GPIO — the VL53L0X answers at 0x29, so the two coexist.
//
// TWO INVARIANTS, both enforced here rather than left to callers:
//
//  1. NEVER two axes at once. Both servos hang off one 5V rail and one strip
//     of board copper; the current budget assumes the peak only ever belongs
//     to a single MG996R. setUs() therefore BLOCKS a command aimed at the
//     other channel until the previous move's settle window has passed.
//     A convention would be broken by the first caller in a hurry.
//
//  2. Nothing moves at boot — including limp. begin() does NOT command the
//     outputs, in either direction. A cold PCA9685 emits no pulses (its LED
//     registers power up at zero), and when only the ESP32 reboots the chip
//     keeps holding whatever it held, so the head does not droop. Releasing
//     at boot would look like the safe choice and is not: an axis actively
//     holding against gravity DROPS when the pulses stop, which is motion,
//     and the mass on this bracket is a camera and a thermal module.
//     Consequence: after a reboot the firmware does not know where the axes
//     are, and says so (lastUs() == US_UNKNOWN) instead of guessing 0.
namespace servo {

// WHICH PCA9685 OUTPUT EACH AXIS IS PLUGGED INTO. Electrically all 16 are
// identical, so this is a wiring convenience, not a constraint — which makes
// it a fact about the physical build, and the build is what these must match.
// Measured on the bench: PAN on 5, TILT on 6.
constexpr uint8_t CH_PAN  = 5;
constexpr uint8_t CH_TILT = 6;

// MG996R's ELECTRICAL span. The mechanical span of the bracket is narrower and
// unknown until measured — see the travel table in servo-wiring.md. Driving
// past a mechanical end stop is a stall: ~2.5 A, heat, and gear wear.
constexpr uint16_t US_MIN    = 600;
constexpr uint16_t US_MAX    = 2400;
constexpr uint16_t US_CENTER = 1500;

// Not a width: "this boot has never commanded this channel". The servo may
// well be holding a position from before the reboot — reporting 0 (released)
// would claim knowledge we do not have.
constexpr uint16_t US_UNKNOWN = 0xFFFF;

bool begin();
bool present();          // begin() found the chip (no live re-probe)

// Command one channel. `us` = 0 releases it (pulses stop, the servo goes limp
// and stops drawing holding current); otherwise US_MIN..US_MAX.
//
// Out-of-range is REJECTED, not clamped: a silently clamped typo looks like a
// successful move and sends you hunting for a mechanical fault that is not
// there. Returns false on a bad channel, a bad width, or an absent chip.
//
// May block up to settleMs() enforcing invariant 1.
bool setUs(uint8_t ch, uint16_t us);

// Last width commanded IN THIS BOOT on CH_PAN or CH_TILT; 0 = released,
// US_UNKNOWN = never commanded here (invariant 2 — it may still be holding).
// Any other channel number returns US_UNKNOWN: nothing else is wired.
uint16_t lastUs(uint8_t ch);

// Mechanical settling allowance for one move — also the enforced gap between
// commands to different channels. MG996R spec is ~0.17 s/60deg at 4.8 V; this
// covers a large move plus overshoot ringing. Phase 3's stop-settle-capture
// measures the real number (roadmap: capture only after settling).
uint32_t settleMs();

}  // namespace servo
