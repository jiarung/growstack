#pragma once

// Failure-state forensic for the AS7341 — two triggers, one implementation:
//   serial 'd'                                        (works with WiFi down)
//   MQTT   monitor-air/<dev>/diag/cmd <- "as7341"     (works with the station remote;
//          every line mirrors to diag/out — this failure mode leaves WiFi alive)
// The decisive test from the
// Codex-arbitrated root-cause review: while the chip is IN the bad state (high nonsense
// data, short read_ms), rewrite the full config and both SMUX banks over DIRECT Wire
// (no Adafruit driver, whose swallowed errors are one of the suspects), read every
// register back, run one bounded measurement, and log every ACK/mismatch.
//
// Beyond raw ACKs it verifies what a clean ACK alone cannot prove (Codex review):
// SMUX chain content read back via the 0x08 command, the mode/bank/sleep registers
// that gate comparability (CFG0/CONFIG/CFG3), and ASTATUS's gain-used field — so an
// AGC override or a silently-unwritten SMUX chain cannot masquerade as "healthy
// registers, garbage data" and be misfiled as electrical.
//
// How to read the verdict it prints:
//   - any NACK / readback or chain-verify MISMATCH / gain-used != 4x -> the chip is
//     not holding or honoring config: electrical, and the failing step is named.
//   - everything verifies clean, data still nonsense for the light level -> the fault
//     is past the registers (analog front end): pure-driver theory is dead.
//   - this sequence RECOVERS the sensor -> driver/state theory wins: diff the
//     pre-state snapshot against the written values to see what init left wrong.
//
// Blocking ~1s. Run it any time; only meaningful evidence when the failure is live.
void as7341DiagRun();
