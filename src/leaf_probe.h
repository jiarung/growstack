#pragma once
#include <stdint.h>

// Leaf-absorption checker, Phase 1 core: the BPW34+LM358 TIA output read on an ADC
// pin, so the station doubles as the voltmeter (no multimeter on this bench).
// Serial 'p' prints one reading. Wiring: TIA out (LM358 pin1) -> GPIO8, common GND.
// GPIO8 = ADC1_CH7 — ADC1 on purpose: ADC2 is unusable while WiFi is up.

// The 940nm LED (150R in series) hangs off GPIO7 instead of a power rail, so the
// firmware owns illumination: serial 'L1'/'L0', and later the measurement command
// can do dark -> lit -> net without anyone touching a wire.

// Idempotent pin setup; call from sensorsBegin. LED starts OFF.
void leafProbeBegin();

// Median of 64 calibrated samples, in millivolts (analogReadMilliVolts applies the
// factory ADC calibration; the median kills WiFi-burst noise spikes).
uint32_t leafProbeMilliVolts();

// Drive the checker LED (serial 'L1' on / 'L0' off).
void leafLedSet(bool on);
