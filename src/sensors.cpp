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
#include "as7341_diag.h"    // serial 'd' — failure-state forensic over direct Wire
#include "mqtt_client.h"    // reflectBusy(): the forensic must not preempt a reflect read
#include "measure.h"        // measureBusy(): nor stall a weigh onto a stale HX711 window
#include "leaf_probe.h"     // serial 'p' — leaf-absorption checker TIA readout (mV)
#include <AS726X.h>
#include <HX711.h>
#include <Preferences.h>

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
constexpr uint8_t AS7341_ADDR      = 0x39;

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
uint8_t bmeAddr = 0;       // I2C addr BME680 actually answered on (0 = never found); for live re-probe
float lastSpectrumReadMs = NAN;  // duration of the last ambient AS7341 read — a bus-health canary

HX711 hx711;                              // load cell ADC (weight); NOT on I2C (GPIO DT/SCK)
constexpr uint8_t HX711_DT = 4, HX711_SCK = 5;
constexpr uint32_t HX711_STALE_MS = 1000; // no fresh sample for this long -> stale (10 SPS)
constexpr int HX_N = 24;                  // ~2.4 s window @ 10 SPS (long enough to judge settling)
long     hxRing[HX_N]   = {0};            // raw counts
uint32_t hxRingMs[HX_N] = {0};            // per-sample millis() (for the window span)
int      hxCount = 0, hxIdx = 0;          // filled count + write index
uint32_t hxLastMs = 0;                    // millis() of the last accepted sample (freshness)

long  hxTare  = 0;                        // raw at zero load (NVS / hx711Tare)
float hxScale = 1.0f;                     // counts per gram (NVS / hx711Calibrate); 1.0 = uncalibrated
bool  hxCalibrated = false;
Preferences hxPrefs;                      // NVS store for tare/scale (survives a cell swap 1kg->5kg)

// BME680 sits at 0x77 on most Adafruit-style modules, 0x76 on some clones.
bool beginBme() {
    for (uint8_t addr : {0x77, 0x76}) {
        if (bme.begin(addr)) {
            bme.setTemperatureOversampling(BME680_OS_8X);
            bme.setHumidityOversampling(BME680_OS_2X);
            bme.setPressureOversampling(BME680_OS_4X);
            bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
            bme.setGasHeater(320, 150);  // 320 degC for 150 ms
            bmeAddr = addr;              // remember for the live health re-probe
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

    hx711Begin();  // load cell (own GPIOs; no presence check — is_ready() gates reads)
    leafProbeBegin();  // leaf-absorption checker: LED pin low, ADC needs no setup

    return bmeOk || bh1750Ok || bh1750RefOk || as7341Ok || as7263Ok;
}

// Live presence probe: does a device ACK this address right now? One cheap I2C start/stop.
static bool i2cPresent(uint8_t addr) {
    Wire.beginTransmission(addr);
    return Wire.endTransmission() == 0;
}

// Single source of truth for "is the AS7341 answering", shared by EVERY read path
// (ambient, reflect one-shot, burst) — a per-path static would let a recovery slip
// past whichever path wasn't the first to notice (e.g. a PUBLISH_AMBIENT_SPECTRUM=0
// build that only ever reflects). On the NACK->ACK transition the chip has been
// through a power event and its registers are NOT trusted: the ambient profile is
// re-asserted and the LED forced off — a reflect-lit LED must not survive recovery.
static bool as7341Responsive = true;
static bool as7341Gate() {
    if (!i2cPresent(AS7341_ADDR)) {
        if (as7341Responsive) logln("[sensors] AS7341 off the bus — reads gated until it answers");
        as7341Responsive = false;
        return false;
    }
    if (!as7341Responsive) {
        logln("[sensors] AS7341 back on the bus — re-asserting config");
        as7341.setATIME(100);
        as7341.setASTEP(999);       // ambient profile (mirrors sensorsBegin)
        as7341.enableLED(false);
        as7341Responsive = true;
    }
    return true;
}

// Re-probe every sensor's LIVE state (not the boot flags) so a mid-run dropout is visible.
SensorHealth sensorsHealth() {
    SensorHealth h;
    h.bme     = bmeAddr ? i2cPresent(bmeAddr) : (i2cPresent(0x77) || i2cPresent(0x76));
    h.lux     = i2cPresent(BH1750_ADDR_MAIN);
    h.lux_ref = i2cPresent(BH1750_ADDR_REF);
    h.as7341  = i2cPresent(AS7341_ADDR);
    h.as7263  = i2cPresent(0x49);
    h.hx711   = (hxCount > 0) && (millis() - hxLastMs) <= HX711_STALE_MS;
    // Count only KNOWN devices (5 sensors + OLED 0x3C), NOT a full 1..126 scan: on a wedged
    // bus every probe hits the ~50ms Wire timeout, so a full scan would block ~127×50ms ≈ 6s.
    // All-zero here still flags a wedge (every known device NACKs at once). Bounded to ~6 probes.
    h.i2c_n = (int)h.bme + (int)h.lux + (int)h.lux_ref + (int)h.as7341 + (int)h.as7263
              + (i2cPresent(0x3C) ? 1 : 0);
    h.spectrum_read_ms = lastSpectrumReadMs;
    return h;
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

// --- HX711 (weight) — ready-gated read + windowed grams stats (stability) + NVS calibration -------
void hx711Begin() {
    hx711.begin(HX711_DT, HX711_SCK);
    pinMode(HX711_DT, INPUT_PULLUP);  // bogde leaves DOUT plain INPUT; the pull-up makes a
                                      // disconnected DT read HIGH (=not ready), not float into garbage
    hxPrefs.begin("hx711", true);     // read-only load
    hxTare  = hxPrefs.getLong("tare", 0);
    hxScale = hxPrefs.getFloat("scale", 1.0f);
    hxPrefs.end();
    if (!isfinite(hxScale) || hxScale == 0.0f) hxScale = 1.0f;  // corrupt NVS -> treat as uncalibrated
    hxCalibrated = (hxScale != 1.0f);
    logf("[sensors] HX711 begin (DT=%u SCK=%u) tare=%ld scale=%.3f %s\n", HX711_DT, HX711_SCK,
         hxTare, hxScale, hxCalibrated ? "" : "(UNCALIBRATED: serial 't' then 'c<grams>')");
}

void hx711Poll() {
    if (!hx711.is_ready()) return;   // skip until DOUT goes low (a fresh sample is waiting)
    uint32_t now = millis();
    hxRing[hxIdx]   = hx711.read();  // one 24-bit read; short now that we're ready
    hxRingMs[hxIdx] = now;
    hxIdx = (hxIdx + 1) % HX_N;
    if (hxCount < HX_N) hxCount++;
    hxLastMs = now;
}

static double hxMeanRaw() {          // mean raw over the current buffer (for tare/calibrate)
    if (hxCount == 0) return 0;
    double sum = 0;
    for (int i = 0; i < hxCount; i++) sum += hxRing[i];
    return sum / hxCount;
}

static bool hxSamplesReady() {       // enough FRESH samples to tare/calibrate on (else reject)
    return hxCount >= 8 && (millis() - hxLastMs) <= HX711_STALE_MS;
}

WeightStats hx711WindowStats() {
    WeightStats s;
    s.calibrated = hxCalibrated;
    s.stale = (hxCount == 0) || (millis() - hxLastMs > HX711_STALE_MS);
    if (hxCount == 0 || s.stale) return s;   // valid stays false (fail-open)

    float lo = 3.4e38f, hi = -3.4e38f;
    double sum = 0;
    uint32_t tMin = 0xFFFFFFFF, tMax = 0;
    for (int i = 0; i < hxCount; i++) {
        float g = ((float)hxRing[i] - (float)hxTare) / hxScale;  // grams (= raw when uncal, scale=1)
        if (g < lo) lo = g;
        if (g > hi) hi = g;
        sum += g;
        if (hxRingMs[i] < tMin) tMin = hxRingMs[i];
        if (hxRingMs[i] > tMax) tMax = hxRingMs[i];
    }
    s.mean_g = (float)(sum / hxCount);
    s.min_g = lo; s.max_g = hi; s.range_g = hi - lo;
    s.sample_count = hxCount;
    s.window_ms = tMax - tMin;
    s.valid = true;
    return s;
}

void hx711Tare() {
    if (!hxSamplesReady()) { logln("[hx711] tare skipped: not enough fresh samples (let it settle)"); return; }
    hxTare = lround(hxMeanRaw());
    hxPrefs.begin("hx711", false);
    hxPrefs.putLong("tare", hxTare);
    hxPrefs.end();
    logf("[hx711] tare = %ld\n", hxTare);
}

void hx711Calibrate(float g) {
    if (g <= 0) { logln("[hx711] calibrate needs a positive gram value"); return; }
    if (!hxSamplesReady()) { logln("[hx711] calibrate skipped: not enough fresh samples (let it settle)"); return; }
    double net = hxMeanRaw() - (double)hxTare;
    if (net == 0) { logln("[hx711] calibrate: no load delta (tare empty, then load a known mass)"); return; }
    hxScale = (float)(net / g);
    hxCalibrated = true;
    hxPrefs.begin("hx711", false);
    hxPrefs.putFloat("scale", hxScale);
    hxPrefs.end();
    logf("[hx711] scale = %.4f counts/g (from %.1f g)\n", hxScale, g);
}

// Bench calibration over the serial monitor: 't' = tare (empty scale), 'c<grams>' = calibrate with a
// known mass, e.g. 'c500'. Re-run after swapping the load cell (1kg -> 5kg).
void hx711SerialCmd() {
    static char buf[16];
    static int  len = 0;
    static bool ovf = false;
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (ovf) logln("[hx711] command too long — ignored");
            else if (len > 0) {
                buf[len] = '\0';
                if (buf[0] == 't')      hx711Tare();
                else if (buf[0] == 'c') hx711Calibrate(atof(buf + 1));
                else if (buf[0] == 'i') {   // live I2C scan — bus-health check without rebooting
                    logln("[i2c] scan (expect 0x24 0x39 0x49 0x3C):");
                    int found = 0;
                    for (uint8_t a = 1; a < 127; a++) {
                        Wire.beginTransmission(a);
                        if (Wire.endTransmission() == 0) { logf("  found 0x%02X\n", a); found++; }
                    }
                    logf("[i2c] %d device(s)\n", found);
                }
                else if (buf[0] == 'w') {   // print current window stats (calibration/stability check)
                    WeightStats s = hx711WindowStats();
                    logf("[hx711] w=%.1fg range=%.1fg n=%d win=%ums stale=%d cal=%d valid=%d\n",
                         s.mean_g, s.range_g, s.sample_count, s.window_ms,
                         (int)s.stale, (int)s.calibrated, (int)s.valid);
                }
                else if (buf[0] == 'h') {   // live sensor-health snapshot (presence re-probe)
                    SensorHealth sh = sensorsHealth();
                    logf("[health] bme=%d lux=%d lux_ref=%d as7341=%d as7263=%d hx711=%d i2c_n=%d read_ms=%.0f\n",
                         (int)sh.bme, (int)sh.lux, (int)sh.lux_ref, (int)sh.as7341, (int)sh.as7263,
                         (int)sh.hx711, sh.i2c_n, sh.spectrum_read_ms);
                }
                else if (buf[0] == 'p') {   // leaf-probe TIA output (BPW34+LM358 on GPIO8), in mV
                    logf("[leaf] %lu mV\n", (unsigned long)leafProbeMilliVolts());
                }
                else if (buf[0] == 'L') {   // leaf-probe LED: L1 on / L0 off (GPIO7, 940nm via 150R)
                    bool on = buf[1] == '1';
                    leafLedSet(on);
                    logf("[leaf] LED %s\n", on ? "ON" : "off");
                }
                else if (buf[0] == 'd') {   // AS7341 failure-state forensic (run DURING the bad state)
                    if (reflectBusy())      logln("[diag] busy: reflect in progress — try again");
                    else if (measureBusy()) logln("[diag] busy: weigh in progress — try again");
                    else as7341DiagRun();
                }
            }
            len = 0; ovf = false;
        } else if (len < (int)sizeof(buf) - 1) {
            buf[len++] = c;
        } else {
            ovf = true;   // line too long -> reject (don't parse a truncated number)
        }
    }
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
    // Gate + recovery: a NACKing chip walked through the ~25-transaction reflect
    // sequence stalls the loop for tens of seconds per phase — fail as "no_sensor".
    if (!as7341Ok || !as7341Gate()) { rphase = RP_IDLE; return false; }
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
    // Mid-flight dropout: the driver's data-ready check misreads a NACK as "ready"
    // and would walk the channel-read + SMUX sequence on Wire timeouts. Probe first
    // and bail WITHOUT any as7341.* cleanup — on a NACKing chip there is no LED to
    // turn off, and every driver call is another walk through the timeouts.
    if (!as7341Gate()) { rphase = RP_IDLE; return REFLECT_NA; }
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
    if (!as7341Ok || !as7341Gate()) { bphase = BURST_IDLE; return false; }
    as7341.setGain(REFLECT_GAIN);   // reclaim 64x from ambient's low gain
    as7341.enableLED(false);   // dark0: LED off
    as7341.startReading();
    bphase = BURST_DARK0;
    bPhaseStart = millis();
    return true;
}

ReflectStatus reflectBurstPoll(ReflectReading* out) {
    if (!as7341Ok || bphase == BURST_IDLE) return REFLECT_NA;
    if (!as7341Gate()) { bphase = BURST_IDLE; return REFLECT_NA; }   // see reflectPoll

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
    // LED-off only if the chip answers: a NACKing chip has no LED on, and the driver
    // call would just walk Wire timeouts. Recovery re-asserts LED-off regardless.
    if (i2cPresent(AS7341_ADDR)) as7341.enableLED(false);
    bphase = BURST_IDLE;
}

void reflectAbort() {
    if (i2cPresent(AS7341_ADDR)) as7341.enableLED(false);
    rphase = RP_IDLE;
    bphase = BURST_IDLE;
}

// Containment for the two OBSERVED AS7341 failure signatures (root cause is electrical
// and under investigation — this only keeps a sick chip from poisoning the station):
//
// Mode B, off the bus (address NACK): readAllChannels must not run. The driver's
// data-ready check misreads an I2C failure as "ready" (BusIO returns 0xFFFFFFFF), so
// instead of hanging it walks the whole sequence with per-transaction timeouts —
// 2026-08-25: spectrum_read_ms=16190, freezing HX711/measure/OLED every publish cycle.
// A ~1ms address probe (same op the health topic uses) gates the read.
//
// Mode A, answering but reset/latched (nonsense values, read_ms 220-471 vs ~610
// healthy): the completed read's wall-time is checked against what ATIME=100/ASTEP=999
// predicts (~562ms integration + overhead). Off-window -> reading invalidated, and the
// ambient config is re-asserted once per offence: if the chip brownout-reset to POR
// defaults, that recovers it by the next cycle; if it is latched, writes are futile but
// each costs ~1ms and the bad data stays quarantined either way.
//
// The window judges data plausibility AFTER the read; the deadline bounds the read
// ITSELF — the driver's own readAllChannels waits on AVALID forever, so a chip that
// ACKs but never completes an integration would hang the loop for good. read_ms is
// wall-clock, so a busy system (OTA, WiFi) can push a legitimate read past the max:
// that costs one dropped-but-honest sample, the right trade for a quarantine.
constexpr float    SPECTRUM_READ_MIN_MS      = 500.0f;
constexpr float    SPECTRUM_READ_MAX_MS      = 1200.0f;
constexpr uint32_t SPECTRUM_READ_DEADLINE_MS = 3000;

SpectrumReading spectrumRead() {
    SpectrumReading s;
    if (!as7341Ok) return s;  // fail-open: valid stays false
    if (!as7341Gate()) {      // Mode B gate (+ config recovery on the way back up)
        lastSpectrumReadMs = NAN;   // health shows "no read", not a stale-looking number
        return s;
    }

    // Same two-SMUX-cycle sequence as the driver's readAllChannels, via its
    // non-blocking API: a deadline instead of the forever-wait, and a per-poll
    // address probe so a mid-read dropout exits on the next poll instead of
    // walking the rest of the sequence on Wire timeouts (the observed 16s stall).
    uint16_t ch[12];
    as7341.setGain(AMBIENT_GAIN);   // low gain: daylight would saturate reflect's 64x
    uint32_t t0 = millis();
    as7341.startReading();
    bool ok = false;
    while (millis() - t0 < SPECTRUM_READ_DEADLINE_MS) {
        if (!i2cPresent(AS7341_ADDR)) { as7341Responsive = false; break; }
        if (as7341.checkReadingProgress()) { ok = true; break; }
        delay(2);
    }
    s.read_ms = (float)(millis() - t0);
    lastSpectrumReadMs = s.read_ms;  // bus-health canary for the health topic (creeping = degrading I2C)
    if (!ok) {
        logf("[sensors] AS7341 ambient read aborted after %.0fms (%s)\n", s.read_ms,
             as7341Responsive ? "deadline" : "dropped off the bus");
        return s;
    }
    as7341.getAllChannels(ch);

    if (s.read_ms < SPECTRUM_READ_MIN_MS || s.read_ms > SPECTRUM_READ_MAX_MS) {   // Mode A gate
        logf("[sensors] AS7341 read_ms=%.0f outside [%.0f,%.0f] — reading dropped, config re-asserted\n",
             s.read_ms, SPECTRUM_READ_MIN_MS, SPECTRUM_READ_MAX_MS);
        as7341.setATIME(100);
        as7341.setASTEP(999);
        return s;   // valid stays false: wrongly-timed data never reaches the wire
    }

    // readAllChannels fills 12 slots; [4] and [5] are duplicate ADCs — skip them.
    s.f415 = ch[0];  s.f445 = ch[1];  s.f480 = ch[2];  s.f515 = ch[3];
    s.f555 = ch[6];  s.f590 = ch[7];  s.f630 = ch[8];  s.f680 = ch[9];
    s.clear = ch[10]; s.nir = ch[11];
    // Any clipped channel corrupts the PPFD sum — flag it so downstream doesn't trust the value.
    for (int i = 0; i < 10; i++) if (ch[CH10[i]] >= SAT_LEVEL) s.saturated = true;
    s.valid = true;
    return s;
}
