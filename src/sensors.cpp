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

namespace {

// All sensors share one I2C bus (Wire). Addresses don't collide: BME680 0x76/0x77,
// BH1750 #1 0x23, BH1750 #2 0x5C — so a single SDA/SCL pair serves all of them.
constexpr uint8_t I2C_SDA = 17;
constexpr uint8_t I2C_SCL = 18;

// Two BH1750s by ADDR strap: floating/low -> 0x23 (primary, plant location -> lux),
// tied to 3V3 -> 0x5C (reference, no-natural-light spot -> lux_ref).
constexpr uint8_t BH1750_ADDR_MAIN = 0x23;
constexpr uint8_t BH1750_ADDR_REF  = 0x5C;

Adafruit_BME680 bme;
BH1750 lightMeter;     // primary    @ 0x23 -> lux
BH1750 lightMeterRef;  // reference  @ 0x5C -> lux_ref
Adafruit_AS7341 as7341;  // spectral @ 0x39 (fixed addr) -> spectrum topic

bool bmeOk = false;
bool bh1750Ok = false;     // primary present
bool bh1750RefOk = false;  // reference present
bool as7341Ok = false;     // spectral present

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

    return bmeOk || bh1750Ok || bh1750RefOk || as7341Ok;
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

// AS7341 readAllChannels/getAllChannels fill 12 slots; [4] and [5] are duplicate ADCs.
// These are the 10 real channels in f415..f680, clear, nir order.
constexpr uint8_t CH10[10] = {0, 1, 2, 3, 6, 7, 8, 9, 10, 11};
constexpr uint8_t CLEAR_SLOT = 10;

constexpr uint16_t REFLECT_LED_MA         = 10;    // LED drive during the lit read
constexpr uint32_t REFLECT_STEP_TIMEOUT_MS = 2000; // per-bank data-ready bound
constexpr uint16_t SAT_LEVEL              = 65000; // near the 16-bit ADC ceiling
constexpr uint16_t AMBIENT_LEAK_CLEAR     = 2000;  // dark clear above this = ambient leaking in

enum RPhase { RP_IDLE, RP_DARK_WAIT, RP_LIT_WAIT };
RPhase rphase = RP_IDLE;
uint16_t darkRaw[12] = {0};
uint16_t litRaw[12]  = {0};
uint32_t rPhaseStart = 0;
uint32_t rStart      = 0;

}  // namespace

bool reflectStart() {
    if (!as7341Ok) { rphase = RP_IDLE; return false; }
    rStart = millis();
    as7341.enableLED(false);   // dark read: LED off
    as7341.startReading();
    rphase = RP_DARK_WAIT;
    rPhaseStart = millis();
    return true;
}

ReflectStatus reflectPoll(ReflectReading* out) {
    if (!as7341Ok || rphase == RP_IDLE) return REFLECT_NA;

    if (rphase == RP_DARK_WAIT) {
        if (as7341.checkReadingProgress()) {
            as7341.getAllChannels(darkRaw);
            as7341.setLEDCurrent(REFLECT_LED_MA);  // now the lit read: LED on
            as7341.enableLED(true);
            as7341.startReading();
            rphase = RP_LIT_WAIT;
            rPhaseStart = millis();
            return REFLECT_BUSY;
        }
    } else if (rphase == RP_LIT_WAIT) {
        if (as7341.checkReadingProgress()) {
            as7341.getAllChannels(litRaw);
            as7341.enableLED(false);  // done — LED off
            for (int i = 0; i < 10; i++) {
                out->dark[i] = darkRaw[CH10[i]];
                out->lit[i]  = litRaw[CH10[i]];
                out->net[i]  = (float)litRaw[CH10[i]] - (float)darkRaw[CH10[i]];
                if (litRaw[CH10[i]] >= SAT_LEVEL) out->saturated = true;
            }
            out->ambient_leak = darkRaw[CLEAR_SLOT] > AMBIENT_LEAK_CLEAR;
            out->read_ms = (float)(millis() - rStart);
            out->valid = true;
            rphase = RP_IDLE;
            return REFLECT_DONE;
        }
    }

    // timeout guard so a wedged bus can't hang the measurement forever
    if (millis() - rPhaseStart > REFLECT_STEP_TIMEOUT_MS) {
        as7341.enableLED(false);
        rphase = RP_IDLE;
        return REFLECT_TIMEOUT;
    }
    return REFLECT_BUSY;
}

SpectrumReading spectrumRead() {
    SpectrumReading s;
    if (!as7341Ok) return s;  // fail-open: valid stays false

    // Blocking ~0.5s (two SMUX cycles). Fine at the publish cadence; bounding it
    // is a hardening item (matters mainly if the bus wedges or for reflectance).
    // Time it: a creeping read_ms is an early warning of a degrading I2C connection.
    uint16_t ch[12];
    uint32_t t0 = millis();
    bool ok = as7341.readAllChannels(ch);
    s.read_ms = (float)(millis() - t0);
    if (!ok) return s;  // read failed -> invalid

    // readAllChannels fills 12 slots; [4] and [5] are duplicate ADCs — skip them.
    s.f415 = ch[0];  s.f445 = ch[1];  s.f480 = ch[2];  s.f515 = ch[3];
    s.f555 = ch[6];  s.f590 = ch[7];  s.f630 = ch[8];  s.f680 = ch[9];
    s.clear = ch[10]; s.nir = ch[11];
    s.valid = true;
    return s;
}
