#pragma once

#include <Arduino.h>

// One snapshot of all sensor values, returned fresh per read (no shared mutable
// state). A failed/garbage read leaves the matching `*Valid` flag false so the
// publisher can omit that field entirely.
struct SensorReading {
    float temp     = NAN;  // degC
    float hum      = NAN;  // %RH
    float pressure = NAN;  // hPa
    float gas      = NAN;  // kOhm
    float lux      = NAN;  // lux      — primary BH1750 @ 0x23 (plant location, lit by the grow lamp)
    float lux_ref  = NAN;  // lux      — reference BH1750 @ 0x5C (same spot ~10cm away, shielded from the grow lamp → ambient only; lux - lux_ref ≈ the lamp's contribution)

    bool tempValid     = false;
    bool humValid      = false;
    bool pressureValid = false;
    bool gasValid      = false;
    bool luxValid      = false;
    bool lux_refValid  = false;
};

// One ambient spectral snapshot from the AS7341 (10 channels). Kept SEPARATE from
// SensorReading so the `air` telemetry contract is untouched — published on its own
// topic. valid=false when the AS7341 is absent or a read fails (fail-open).
struct SpectrumReading {
    float f415 = NAN, f445 = NAN, f480 = NAN, f515 = NAN, f555 = NAN;
    float f590 = NAN, f630 = NAN, f680 = NAN;
    float clear = NAN, nir = NAN;
    float read_ms = NAN;  // how long the (blocking) read took — a bus-health signal
    // The configuration this reading was taken under — published with the data so a
    // calibration is anchored to a measured fact, not to what a CLI claims was set.
    float gain_x  = NAN;  // ADC gain as a multiplier (ambient = 4.0)
    float tint_ms = NAN;  // integration time, ms ((ATIME+1)·(ASTEP+1)·2.78µs)
    bool saturated = false;  // a channel hit the ADC ceiling → PPFD sum untrustworthy
    bool valid = false;
};

// Live per-sensor presence/health, RE-PROBED on demand (not the boot-time flags) so a
// sensor that drops mid-run becomes visible — weight + WiFi can keep working while a
// sensor is silently gone. I2C sensors: a fresh address ACK right now. HX711: a fresh
// sample within the stale window. Consumed by the `health` topic (Phase 2) for host alerts.
struct SensorHealth {
    bool bme     = false;  // BME680  @ 0x77/0x76
    bool lux     = false;  // BH1750  @ 0x23 (primary)
    bool lux_ref = false;  // BH1750  @ 0x5C (reference)
    bool as7341  = false;  // AS7341  @ 0x39 (visible spectral)
    bool as7263  = false;  // AS7263  @ 0x49 (NIR spectral)
    bool hx711   = false;  // load cell: a fresh sample within HX711_STALE_MS
    int  i2c_n   = 0;      // devices ACKing on the bus right now (0 = bus wedged)
    float spectrum_read_ms = NAN;  // last ambient AS7341 read duration — a bus-health canary
};
// Re-probes live each call: ~6 I2C address probes (known devices only, no full bus scan).
// Cheap on a healthy bus; bounded to ~6×Wire-timeout if the bus is wedged. Callers on the
// periodic path should gate it behind !reflectBusy() (like the ambient-spectrum tick).
SensorHealth sensorsHealth();

// Initialise the I2C bus and both sensors. Returns true if at least one sensor
// came up; never blocks or halts on failure (a network device must keep running).
bool sensorsBegin();

// Take a fresh reading. Returns a new value each call (no shared mutable state).
SensorReading sensorsRead();

// --- HX711 load cell (weight) — NON-blocking, NOT on I2C (own GPIOs DT=4/SCK=5) ----
// hx711Poll() runs every loop and reads ONE raw only when is_ready() (never the lib's blocking
// averaging). hx711WindowStats() is the station's stability signal over the recent sample buffer.
void hx711Begin();
void hx711Poll();   // call every loop iteration (samples when the HX711 signals ready)

// Windowed stats over the recent sample buffer — the measurement station's stability signal.
// Weights are grams once calibrated (raw counts if not). Judge "stable" on range_g, not the mean.
struct WeightStats {
    float mean_g = NAN, min_g = NAN, max_g = NAN, range_g = NAN;
    int   sample_count = 0;
    uint32_t window_ms = 0;   // span of the samples in the buffer
    bool  stale = true;       // no fresh sample recently (loose lead / dead cell)
    bool  calibrated = false; // scale set via hx711Calibrate()
    bool  valid = false;      // false if no samples or stale
};
WeightStats hx711WindowStats();

void hx711Tare();             // zero: tare_offset = current mean raw (persisted to NVS)
void hx711Calibrate(float g); // scale = (mean raw - tare)/g from a known mass (persisted to NVS)
void hx711SerialCmd();        // bench calibration over serial: 't' = tare, 'c<grams>' = calibrate

// Take a fresh ambient spectral reading (AS7341, LED off). valid=false if no AS7341.
SpectrumReading spectrumRead();

// --- Reflectance (AS7341 LED) — Phase 4b1, non-blocking dark→LED→lit read --------
enum ReflectStatus { REFLECT_NA, REFLECT_BUSY, REFLECT_DONE, REFLECT_TIMEOUT };

// One reflectance measurement: raw dark (LED off) + lit (LED on) + net (lit-dark),
// 10 channels each (f415..f680, clear, nir order). Raw values kept for recompute/debug.
struct ReflectReading {
    // Visible (AS7341) — 10 channels f415..f680, clear, nir. `valid` = the AS7341 read
    // succeeded; it gates the whole publish (the primary sensor). dark/lit/net raw counts.
    float dark[10] = {0};
    float lit[10]  = {0};
    float net[10]  = {0};
    float read_ms  = NAN;
    bool saturated    = false;
    bool ambient_leak = false;
    bool valid = false;

    // NIR (AS7263) — 6 channels n610 n680 n730 n760 n810 n860. Best-effort: a missing/timed-out
    // AS7263 leaves nir_valid=false (+ nir_status) but the visible reflect is still published.
    float nir_dark[6] = {0};
    float nir_lit[6]  = {0};
    float nir_net[6]  = {0};
    float nir_read_ms = NAN;
    bool nir_saturated = false;
    bool nir_valid = false;
    char nir_status[12] = "absent";   // "ok" | "timeout" | "nack" | "absent"
};

// Begin a dark→LED→lit read. Returns false if no AS7341 (nothing started).
bool reflectStart();

// Advance the read (call each loop iteration). DONE fills `out`; TIMEOUT if a bank read
// didn't become ready in time; BUSY while in progress; NA when idle/no sensor.
ReflectStatus reflectPoll(ReflectReading* out);

// --- Burst reflect (visible-only, streaming lit) ---------------------------------
// One dark0 baseline (LED off) then repeated non-blocking AS7341 lit reads with the LED
// held on; net = lit - dark0. Skips the AS7263 (nir_valid=0, nir_status="skip"). Drive:
// Start -> Poll (returns DONE once per sample) -> Next -> ... (until done) -> End.
bool reflectBurstStart();
ReflectStatus reflectBurstPoll(ReflectReading* out);
void reflectBurstNext();
void reflectBurstEnd();

// Abort any in-progress one-shot/burst measurement (LED off) — for disconnect cleanup.
void reflectAbort();
