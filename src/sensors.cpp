#include "sensors.h"

#include "log.h"

// Portable finite check: NaN fails (v == v), and the bounds reject +/-Inf.
// Avoids the newlib isfinite() macro vs std::isfinite ambiguity on xtensa.
static inline bool finiteF(float v) {
    return v == v && v < 3.4e38f && v > -3.4e38f;
}

#include <Wire.h>
#include <BH1750.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME680.h>
#include <Adafruit_AS7341.h>
#include <AS726X.h>

namespace {

// All sensors share one I2C bus (Wire). Addresses don't collide: BME680 0x76/0x77,
// BH1750 #1 0x23, BH1750 #2 0x5C — so a single SDA/SCL pair serves all of them.
constexpr uint8_t I2C_SDA = 17;
constexpr uint8_t I2C_SCL = 18;

// Two BH1750s by ADDR strap: floating/low -> 0x23 (primary, at the plant, lit by
// the grow lamp -> lux), tied to 3V3 -> 0x5C (reference ~10cm away, same ambient
// but shielded from the grow lamp -> lux_ref; lux - lux_ref ≈ the lamp's share).
constexpr uint8_t BH1750_ADDR_MAIN = 0x23;
constexpr uint8_t BH1750_ADDR_REF  = 0x5C;

Adafruit_BME680 bme;
BH1750 lightMeter;     // primary    @ 0x23 -> lux
BH1750 lightMeterRef;  // reference  @ 0x5C -> lux_ref
Adafruit_AS7341 as7341;  // visible spectral @ 0x39 -> spectrum topic
AS726X as7263;           // NIR spectral @ 0x49 -> joins the reflect read (red-edge/NIR)

bool bmeOk = false;
bool bh1750Ok = false;     // primary present
bool bh1750RefOk = false;  // reference present
bool as7341Ok = false;     // visible spectral present
bool as7263Ok = false;     // NIR spectral present

// BME680 sits at 0x77 on most Adafruit-style modules, 0x76 on some clones.
bool beginBme() {
    for (uint8_t addr : {0x77, 0x76}) {
        if (bme.begin(addr)) {
            bme.setTemperatureOversampling(BME680_OS_8X);
            bme.setHumidityOversampling(BME680_OS_2X);
            bme.setPressureOversampling(BME680_OS_4X);
            bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
            bme.setGasHeater(320, 150);  // 320 degC for 150 ms
            logf("[sensors] BME680 ok @ 0x%02X\n", addr);
            return true;
        }
    }
    logln("[sensors] BME680 NOT found (0x77/0x76)");
    return false;
}

// Each BH1750 is bound to a FIXED address (no scan) so the two never get confused:
// 0x23 -> lux, 0x5C -> lux_ref. A missing one just leaves its flag false (fail-open).
bool beginBh1750At(BH1750& meter, uint8_t addr, const char* label) {
    if (meter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, addr, &Wire)) {
        logf("[sensors] BH1750 %s ok @ 0x%02X\n", label, addr);
        return true;
    }
    logf("[sensors] BH1750 %s NOT found @ 0x%02X\n", label, addr);
    return false;
}

}  // namespace

bool sensorsBegin() {
    Wire.begin(I2C_SDA, I2C_SCL);
    bmeOk = beginBme();
    bh1750Ok    = beginBh1750At(lightMeter,    BH1750_ADDR_MAIN, "#1 (lux)");
    bh1750RefOk = beginBh1750At(lightMeterRef, BH1750_ADDR_REF,  "#2 (lux_ref)");

    // AS7341 spectral @ fixed 0x39. LED stays off — ambient readings only; the LED
    // would contaminate the spectrum (it's for reflectance, a later phase).
    as7341Ok = as7341.begin();
    if (as7341Ok) {
        as7341.setATIME(100);
        as7341.setASTEP(999);             // ~280 ms integration
        as7341.setGain(AS7341_GAIN_64X);  // tune if it saturates in direct sun
        as7341.enableLED(false);
        logln("[sensors] AS7341 ok @ 0x39");
    } else {
        logln("[sensors] AS7341 NOT found @ 0x39");
    }

    // AS7263 NIR spectral @ 0x49 — joins the reflect read (red-edge/NIR). Gate begin() on an
    // I2C probe first: its virtual-register begin() would otherwise hang if the sensor is absent.
    Wire.beginTransmission(0x49);
    if (Wire.endTransmission() == 0) {
        as7263Ok = as7263.begin(Wire);  // default gain 3, one-shot mode 3
        logf("[sensors] AS7263 %s @ 0x49\n", as7263Ok ? "ok" : "begin FAILED");
    } else {
        as7263Ok = false;
        logln("[sensors] AS7263 NOT found @ 0x49");
    }

    return bmeOk || bh1750Ok || bh1750RefOk || as7341Ok || as7263Ok;
}

SensorReading sensorsRead() {
    SensorReading r;

    if (bmeOk && bme.performReading()) {
        if (finiteF(bme.temperature))   { r.temp = bme.temperature;            r.tempValid = true; }
        if (finiteF(bme.humidity))      { r.hum = bme.humidity;                r.humValid = true; }
        if (finiteF(bme.pressure))      { r.pressure = bme.pressure / 100.0f;  r.pressureValid = true; }  // Pa -> hPa
        if (finiteF(bme.gas_resistance)){ r.gas = bme.gas_resistance / 1000.0f; r.gasValid = true; }      // Ohm -> kOhm
    }

    if (bh1750Ok) {
        float lux = lightMeter.readLightLevel();
        if (lux >= 0.0f && finiteF(lux)) {  // BH1750 returns -1/-2 on error
            r.lux = lux;
            r.luxValid = true;
        }
    }

    if (bh1750RefOk) {
        float lux = lightMeterRef.readLightLevel();
        if (lux >= 0.0f && finiteF(lux)) {
            r.lux_ref = lux;
            r.lux_refValid = true;
        }
    }

    return r;
}

namespace {

// AS7341 getAllChannels fills 12 slots; [4]/[5] are duplicate ADCs. The 10 real channels
// in f415..f680, clear, nir order.
constexpr uint8_t CH10[10] = {0, 1, 2, 3, 6, 7, 8, 9, 10, 11};
constexpr uint8_t CLEAR_SLOT = 10;

constexpr uint16_t REFLECT_LED_MA           = 10;   // AS7341 LED drive during the lit read
constexpr uint32_t REFLECT_PHASE_TIMEOUT_MS = 2500; // deadline for BOTH sensors to finish a phase
constexpr uint16_t SAT_LEVEL                = 65000;// near the 16-bit ADC ceiling
constexpr uint16_t AMBIENT_LEAK_CLEAR       = 2000; // dark clear above this = ambient leaking in

// The AS7341 gain register is shared, so each read path sets its OWN gain before starting
// integration: ambient daylight needs a LOW gain (64x saturates by ~10k lux); reflect's close
// LED wants 64x. 4x gives ~16x headroom over the 64x saturation point (~160k lux) for full sun.
constexpr as7341_gain_t AMBIENT_GAIN = AS7341_GAIN_4X;
constexpr as7341_gain_t REFLECT_GAIN = AS7341_GAIN_64X;

// Dual-sensor reflect: a dark phase then a lit phase. The AS7341 (visible, primary) is read
// NON-blocking (start/poll/get). The AS7263 (NIR, best-effort) is read with the library's
// BLOCKING takeMeasurements() at the correct LED state — its non-blocking mode returned stale
// (dark==lit) or garbage (all-0xFFFF) data when interleaved, so we take the bounded block.
enum RPhase { RP_IDLE, RP_DARK, RP_LIT };
RPhase rphase = RP_IDLE;
uint16_t darkRaw[12] = {0};    // AS7341
uint16_t litRaw[12]  = {0};
uint16_t nirDark[6]  = {0};    // AS7263: R610 S680 T730 U760 V810 W860
uint16_t nirLit[6]   = {0};
bool nirDarkOk = false;        // AS7263 dark read succeeded
bool nirLitOk  = false;        // AS7263 lit read succeeded
uint32_t rPhaseStart = 0;
uint32_t rStart      = 0;

// Blocking AS7263 read (bounded by the library's internal timeout). Rejects the all-0xFFFF
// garbage read (an I2C error) as a failure so it doesn't reach the wire as a real value.
bool readNirBlocking(uint16_t* buf) {
    if (!as7263Ok) return false;
    for (int attempt = 0; attempt < 2; attempt++) {   // retry once on a garbage read
        as7263.takeMeasurements();
        buf[0] = (uint16_t)as7263.getR();  buf[1] = (uint16_t)as7263.getS();
        buf[2] = (uint16_t)as7263.getT();  buf[3] = (uint16_t)as7263.getU();
        buf[4] = (uint16_t)as7263.getV();  buf[5] = (uint16_t)as7263.getW();
        for (int i = 0; i < 6; i++) if (buf[i] != 0xFFFF) return true;  // at least one real value
    }
    return false;  // still all 0xFFFF -> garbage / NACK
}

}  // namespace

bool reflectStart() {
    if (!as7341Ok) { rphase = RP_IDLE; return false; }  // no visible sensor -> can't measure
    rStart = millis();
    as7341.setGain(REFLECT_GAIN);         // reclaim 64x from ambient's low gain
    as7341.enableLED(false);              // dark: LED off (both sensors)
    nirDarkOk = readNirBlocking(nirDark); // AS7263 dark (blocking, LED off)
    as7341.startReading();                // AS7341 dark (non-blocking)
    rphase = RP_DARK;
    rPhaseStart = millis();
    return true;
}

ReflectStatus reflectPoll(ReflectReading* out) {
    if (!as7341Ok || rphase == RP_IDLE) return REFLECT_NA;
    bool timeout = (millis() - rPhaseStart > REFLECT_PHASE_TIMEOUT_MS);

    if (rphase == RP_DARK) {
        if (as7341.checkReadingProgress()) {
            as7341.getAllChannels(darkRaw);
            as7341.setLEDCurrent(REFLECT_LED_MA);
            as7341.enableLED(true);              // lit: LED on (both sensors)
            nirLitOk = readNirBlocking(nirLit);  // AS7263 lit (blocking, LED on)
            as7341.startReading();               // AS7341 lit (non-blocking)
            rphase = RP_LIT;
            rPhaseStart = millis();
            return REFLECT_BUSY;
        }
        if (timeout) { as7341.enableLED(false); rphase = RP_IDLE; return REFLECT_TIMEOUT; }
        return REFLECT_BUSY;
    }

    // RP_LIT
    if (as7341.checkReadingProgress()) {
        as7341.getAllChannels(litRaw);
        as7341.enableLED(false);
        for (int i = 0; i < 10; i++) {
            out->dark[i] = darkRaw[CH10[i]];
            out->lit[i]  = litRaw[CH10[i]];
            out->net[i]  = (float)litRaw[CH10[i]] - (float)darkRaw[CH10[i]];
            if (litRaw[CH10[i]] >= SAT_LEVEL) out->saturated = true;
        }
        out->ambient_leak = darkRaw[CLEAR_SLOT] > AMBIENT_LEAK_CLEAR;
        out->read_ms = (float)(millis() - rStart);
        out->valid = true;  // visible success gates the publish

        if (as7263Ok && nirDarkOk && nirLitOk) {
            for (int i = 0; i < 6; i++) {
                out->nir_dark[i] = nirDark[i];
                out->nir_lit[i]  = nirLit[i];
                out->nir_net[i]  = (float)nirLit[i] - (float)nirDark[i];
                if (nirLit[i] >= SAT_LEVEL) out->nir_saturated = true;
            }
            out->nir_read_ms = out->read_ms;
            out->nir_valid = true;
            snprintf(out->nir_status, sizeof(out->nir_status), "ok");
        } else {
            out->nir_valid = false;
            snprintf(out->nir_status, sizeof(out->nir_status), "%s", as7263Ok ? "nack" : "absent");
        }
        rphase = RP_IDLE;
        return REFLECT_DONE;
    }
    if (timeout) { as7341.enableLED(false); rphase = RP_IDLE; return REFLECT_TIMEOUT; }
    return REFLECT_BUSY;
}

// --- Burst reflect: one dark0 (LED off), then stream lit (LED on), visible-only -----
namespace {
enum BPhase { BURST_IDLE, BURST_DARK0, BURST_LIT };
BPhase bphase = BURST_IDLE;
uint16_t dark0Raw[12] = {0};
uint32_t bPhaseStart  = 0;   // per-read deadline base
uint32_t bSampleStart = 0;   // current lit read start (for read_ms)
}  // namespace

bool reflectBurstStart() {
    if (!as7341Ok) { bphase = BURST_IDLE; return false; }
    as7341.setGain(REFLECT_GAIN);   // reclaim 64x from ambient's low gain
    as7341.enableLED(false);   // dark0: LED off
    as7341.startReading();
    bphase = BURST_DARK0;
    bPhaseStart = millis();
    return true;
}

ReflectStatus reflectBurstPoll(ReflectReading* out) {
    if (!as7341Ok || bphase == BURST_IDLE) return REFLECT_NA;

    if (bphase == BURST_DARK0) {
        if (as7341.checkReadingProgress()) {
            as7341.getAllChannels(dark0Raw);
            as7341.setLEDCurrent(REFLECT_LED_MA);
            as7341.enableLED(true);          // lit: LED on for the whole burst
            as7341.startReading();
            bphase = BURST_LIT;
            bPhaseStart = millis();
            bSampleStart = millis();
            return REFLECT_BUSY;
        }
    } else {  // BURST_LIT
        if (as7341.checkReadingProgress()) {
            uint16_t lit[12];
            as7341.getAllChannels(lit);
            for (int i = 0; i < 10; i++) {
                out->dark[i] = dark0Raw[CH10[i]];
                out->lit[i]  = lit[CH10[i]];
                out->net[i]  = (float)lit[CH10[i]] - (float)dark0Raw[CH10[i]];
                if (lit[CH10[i]] >= SAT_LEVEL) out->saturated = true;
            }
            out->ambient_leak = dark0Raw[CLEAR_SLOT] > AMBIENT_LEAK_CLEAR;
            out->read_ms = (float)(millis() - bSampleStart);
            out->valid = true;
            out->nir_valid = false;
            snprintf(out->nir_status, sizeof(out->nir_status), "skip");
            return REFLECT_DONE;   // one sample; caller calls Next to continue, or End
        }
    }

    if (millis() - bPhaseStart > REFLECT_PHASE_TIMEOUT_MS) {
        as7341.enableLED(false);
        bphase = BURST_IDLE;
        return REFLECT_TIMEOUT;
    }
    return REFLECT_BUSY;
}

void reflectBurstNext() {
    as7341.startReading();   // next lit read; LED stays on
    bPhaseStart  = millis();
    bSampleStart = millis();
}

void reflectBurstEnd() {
    as7341.enableLED(false);
    bphase = BURST_IDLE;
}

void reflectAbort() {
    as7341.enableLED(false);
    rphase = RP_IDLE;
    bphase = BURST_IDLE;
}

SpectrumReading spectrumRead() {
    SpectrumReading s;
    if (!as7341Ok) return s;  // fail-open: valid stays false

    // Blocking ~0.5s (two SMUX cycles). Fine at the publish cadence; bounding it
    // is a hardening item (matters mainly if the bus wedges or for reflectance).
    // Time it: a creeping read_ms is an early warning of a degrading I2C connection.
    uint16_t ch[12];
    as7341.setGain(AMBIENT_GAIN);   // low gain: daylight would saturate reflect's 64x
    uint32_t t0 = millis();
    bool ok = as7341.readAllChannels(ch);
    s.read_ms = (float)(millis() - t0);
    if (!ok) return s;  // read failed -> invalid

    // readAllChannels fills 12 slots; [4] and [5] are duplicate ADCs — skip them.
    s.f415 = ch[0];  s.f445 = ch[1];  s.f480 = ch[2];  s.f515 = ch[3];
    s.f555 = ch[6];  s.f590 = ch[7];  s.f630 = ch[8];  s.f680 = ch[9];
    s.clear = ch[10]; s.nir = ch[11];
    // Any clipped channel corrupts the PPFD sum — flag it so downstream doesn't trust the value.
    for (int i = 0; i < 10; i++) if (ch[CH10[i]] >= SAT_LEVEL) s.saturated = true;
    s.valid = true;
    return s;
}
