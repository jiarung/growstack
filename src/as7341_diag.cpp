#include "as7341_diag.h"
#include "log.h"
#include "mqtt_client.h"   // mqttPublishDiagLine(): mirror every line to the diag topic

#include <Arduino.h>
#include <Wire.h>
#include <stdarg.h>

namespace {

// Every forensic line goes to BOTH sinks: serial (works with WiFi down) and the MQTT
// diag topic (works with the station remote — this failure mode leaves WiFi alive).
// One line per call, no trailing newline.
//
// The MQTT sink is forensically honest about itself (Codex): each line carries a
// sequence number so a gap on the remote side is VISIBLE, and the first publish
// failure disables the sink for the rest of the run — a wedged TCP socket can stall
// one publish, not ~45 of them (each can block seconds inside WiFiClient::write,
// during which measure/reflect loops are frozen). Serial always has every line.
int  mqttSeq = 0, mqttLost = 0;
bool mqttSinkUp = false;

void dlog(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
void dlog(const char* fmt, ...) {
    char line[200];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    logf("%s\n", line);
    if (mqttSinkUp) {
        char out[208];
        snprintf(out, sizeof(out), "%02d %s", ++mqttSeq, line);
        if (!mqttPublishDiagLine(out)) { mqttLost++; mqttSinkUp = false; }
    } else mqttLost++;
}

// Register map — mirrors Adafruit_AS7341.h so the two can be cross-checked, but this
// module talks raw Wire on purpose: the driver's swallowed errors are under suspicion,
// so the forensic must not share one line of its I/O path.
constexpr uint8_t ADDR      = 0x39;
constexpr uint8_t R_CONFIG  = 0x70;   // bank1 window: INT_MODE (0 = SPM) — comparability gate
constexpr uint8_t R_ENABLE  = 0x80;   // bit0 PON, bit1 SP_EN, bit4 SMUXEN
constexpr uint8_t R_ATIME   = 0x81;
constexpr uint8_t R_WHOAMI  = 0x92;
constexpr uint8_t R_STATUS  = 0x93;   // interrupt status
constexpr uint8_t R_ASTATUS = 0x94;   // read LATCHES all 12 channel bytes; bits3:0 = gain used
constexpr uint8_t R_STATUS2 = 0xA3;   // bit6 AVALID
constexpr uint8_t R_STATUS6 = 0xA7;   // OVTEMP / SAI_ACTIVE / INT_BUSY
constexpr uint8_t R_CFG0    = 0xA9;   // bit4 REG_BANK, bit5 LOW_POWER, WLONG
constexpr uint8_t R_CFG1    = 0xAA;   // gain
constexpr uint8_t R_CFG3    = 0xAC;   // SAI (sleep-after-interrupt)
constexpr uint8_t R_CFG6    = 0xAF;   // SMUX command: 0x10 write RAM->chain, 0x08 read chain->RAM
constexpr uint8_t R_CFG8    = 0xB1;   // SP_AGC — if set, CFG1 gain is NOT what measures
constexpr uint8_t R_CFG9    = 0xB2;
constexpr uint8_t R_CFG10   = 0xB3;
constexpr uint8_t R_CFG12   = 0xB5;
constexpr uint8_t R_ASTEP_L = 0xCA;
constexpr uint8_t R_ASTEP_H = 0xCB;
constexpr uint8_t R_AGC_MAX = 0xCF;
constexpr uint8_t R_AZ_CFG  = 0xD6;

// The firmware's ambient config: ATIME=100, ASTEP=999, gain 4x -> ~281ms/integration.
constexpr uint8_t  CFG_ATIME   = 100;
constexpr uint16_t CFG_ASTEP   = 999;
constexpr uint8_t  CFG_GAIN_4X = 0x03;   // must match ASTATUS bits3:0 of each measurement

// SMUX maps copied verbatim from Adafruit setup_F1F4_Clear_NIR / setup_F5F8_Clear_NIR
// (regs 0x00..0x13) — same routing the ambient path uses, so results are comparable.
constexpr uint8_t SMUX_LOW[20] = {   // F1-F4 + Clear + NIR
    0x30, 0x01, 0x00, 0x00, 0x00, 0x42, 0x00, 0x00, 0x50, 0x00,
    0x00, 0x00, 0x20, 0x04, 0x00, 0x30, 0x01, 0x50, 0x00, 0x06};
constexpr uint8_t SMUX_HIGH[20] = {  // F5-F8 + Clear + NIR
    0x00, 0x00, 0x00, 0x40, 0x02, 0x00, 0x10, 0x03, 0x50, 0x10,
    0x03, 0x00, 0x00, 0x00, 0x24, 0x00, 0x00, 0x50, 0x00, 0x06};

int failCount = 0, txnOk = 0;

// One register write, ACK-checked with the exact Wire error code — the whole point is
// that nothing gets swallowed or blurred (2=addr NACK, 3=data NACK, 5=timeout).
bool wr(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(ADDR);
    Wire.write(reg);
    Wire.write(val);
    uint8_t e = Wire.endTransmission();
    if (e != 0) { dlog("[diag]   WR 0x%02X<=0x%02X FAIL err=%u", reg, val, e); failCount++; }
    else txnOk++;
    return e == 0;
}

bool rd(uint8_t reg, uint8_t* val) {
    Wire.beginTransmission(ADDR);
    Wire.write(reg);
    uint8_t e = Wire.endTransmission();
    if (e != 0) { dlog("[diag]   RD 0x%02X ptr-write FAIL err=%u", reg, e); failCount++; return false; }
    uint8_t got = Wire.requestFrom((int)ADDR, 1);
    if (got != 1) { dlog("[diag]   RD 0x%02X short read got=%u want=1", reg, got); failCount++; return false; }
    *val = (uint8_t)Wire.read();
    txnOk++;
    return true;
}

// Write + read back + compare — a clean ACK with a wrong readback is the signature
// of a chip that answers I2C but isn't holding state.
bool wrv(uint8_t reg, uint8_t val, const char* name) {
    if (!wr(reg, val)) return false;
    uint8_t got = 0;
    if (!rd(reg, &got)) return false;
    bool ok = got == val;
    dlog("[diag]   %s: wrote 0x%02X read 0x%02X %s", name, val, got, ok ? "OK" : "MISMATCH");
    if (!ok) failCount++;
    return ok;
}

// Run one SMUX engine command (0x10 write RAM->chain / 0x08 read chain->RAM) and wait
// for SMUXEN to self-clear — the chip's own "done" signal. A timeout here means the
// SMUX engine is wedged: the strongest possible "not a software problem" evidence.
bool smuxExec(uint8_t cmd, const char* what) {
    wr(R_CFG6, cmd);
    wr(R_ENABLE, 0x11);                    // PON | SMUXEN
    uint32_t t0 = millis();
    uint8_t en = 0;
    while (millis() - t0 < 1000) {
        if (rd(R_ENABLE, &en) && !(en & 0x10)) return true;
        delay(1);
    }
    dlog("[diag]   SMUX %s TIMEOUT (ENABLE=0x%02X after 1000ms)", what, en);
    failCount++;
    return false;
}

// Load one SMUX bank AND verify it landed: write the 20-byte map into RAM, execute
// RAM->chain, then execute chain->RAM and read the RAM back. SMUXEN self-clearing only
// proves the command RAN — without the read-back pass, a chain that silently didn't
// update would masquerade as "clean writes, garbage data" and be misfiled as analog.
bool smuxLoadVerified(const uint8_t* map, const char* name) {
    wr(R_ENABLE, 0x01);                    // PON only (integration off)
    wr(R_CFG6, 0x10);                      // command must be set BEFORE the RAM writes
    int wrote = 0;
    for (uint8_t i = 0; i < 20; i++) if (wr(i, map[i])) wrote++;
    dlog("[diag]   SMUX %s RAM writes %d/20", name, wrote);
    if (!smuxExec(0x10, name)) return false;

    if (!smuxExec(0x08, "chain->RAM")) return false;   // chain unchanged by the read
    int match = 0;
    for (uint8_t i = 0; i < 20; i++) {
        uint8_t got = 0;
        if (rd(i, &got) && got == map[i]) match++;
        else dlog("[diag]   SMUX %s chain[0x%02X]=0x%02X want 0x%02X", name, i, got, map[i]);
    }
    dlog("[diag]   SMUX %s chain-verify %d/20 %s", name, match, match == 20 ? "OK" : "MISMATCH");
    if (match != 20) { failCount++; return false; }
    return true;
}

// Integrate on the current SMUX bank with a bounded wait (config predicts ~281ms), then
// read ASTATUS + all 12 channel bytes in ONE transaction: the ASTATUS read latches the
// channel registers, so the 6 values are one consistent snapshot even with SP_EN live —
// and ASTATUS bits3:0 reveal the gain that ACTUALLY measured (AGC-proof: must be 0x3).
bool measureBank(const char* name, uint16_t out[6]) {
    wr(R_ENABLE, 0x03);                    // PON | SP_EN — start integration
    uint32_t t0 = millis();
    uint8_t st = 0;
    while (millis() - t0 < 2000) {
        if (rd(R_STATUS2, &st) && (st & 0x40)) break;   // AVALID
        delay(2);
    }
    uint32_t dt = millis() - t0;
    if (!(st & 0x40)) {
        dlog("[diag]   %s: AVALID TIMEOUT (STATUS2=0x%02X after %lums)", name, st, (unsigned long)dt);
        failCount++;
        wr(R_ENABLE, 0x01);
        return false;
    }

    Wire.beginTransmission(ADDR);
    Wire.write(R_ASTATUS);
    uint8_t e = Wire.endTransmission();
    uint8_t got = (e == 0) ? Wire.requestFrom((int)ADDR, 13) : 0;
    wr(R_ENABLE, 0x01);                    // integration off; leave PON for the driver
    if (e != 0 || got != 13) {
        dlog("[diag]   %s: latched read FAIL err=%u got=%u/13 ch=UNAVAILABLE", name, e, got);
        failCount++;
        return false;
    }
    txnOk++;
    uint8_t astatus = (uint8_t)Wire.read();
    for (int i = 0; i < 6; i++) {
        uint16_t lo = (uint8_t)Wire.read(), hi = (uint8_t)Wire.read();
        out[i] = (uint16_t)(hi << 8 | lo);
    }
    uint8_t gainUsed = astatus & 0x0F;
    dlog("[diag]   %s: AVALID in %lums (expect ~281) STATUS2=0x%02X ASTATUS=0x%02X "
         "gain-used=%u(%s) sat=%u ch=[%u %u %u %u %u %u]",
         name, (unsigned long)dt, st, astatus,
         gainUsed, gainUsed == CFG_GAIN_4X ? "4x OK" : "NOT 4x!",
         (unsigned)((astatus >> 7) & 1), out[0], out[1], out[2], out[3], out[4], out[5]);
    if (gainUsed != CFG_GAIN_4X) failCount++;   // AGC or gain corruption — data not comparable
    return true;
}

// Read + log a register list verbatim. Cheap, zero-risk observability: anomalies here
// (AGC on, SAI active, OVTEMP, non-SPM mode) are concrete causes — each one caught in a
// snapshot is a misattribution avoided.
void snapshot(const char* label) {
    struct { uint8_t reg; const char* name; } regs[] = {
        {R_WHOAMI, "WHOAMI"}, {R_ENABLE, "ENABLE"}, {R_ATIME, "ATIME"},
        {R_ASTEP_L, "ASTEP_L"}, {R_ASTEP_H, "ASTEP_H"}, {R_CFG0, "CFG0"},
        {R_CFG1, "CFG1"}, {R_CFG3, "CFG3"}, {R_CFG6, "CFG6"},
        {R_CFG8, "CFG8(AGC)"}, {R_CFG9, "CFG9"}, {R_CFG10, "CFG10"}, {R_CFG12, "CFG12"},
        {R_AGC_MAX, "AGC_GAIN_MAX"}, {R_AZ_CFG, "AZ_CONFIG"},
        {R_STATUS, "STATUS"}, {R_STATUS2, "STATUS2"}, {R_STATUS6, "STATUS6"}};
    dlog("[diag] -- %s --", label);
    char line[100];
    int n = 0;
    for (auto& r : regs) {
        uint8_t v = 0;
        if (!rd(r.reg, &v)) continue;
        int w = snprintf(line + n, sizeof(line) - n, "%s=0x%02X ", r.name, v);
        if (w < 0 || (size_t)w >= sizeof(line) - n || n + w > 78) {
            dlog("[diag]   %s", line); n = 0;
            snprintf(line, sizeof(line), "%s=0x%02X ", r.name, v);
            n = (int)strlen(line);
        } else n += w;
    }
    if (n > 0) dlog("[diag]   %s", line);

    // CONFIG lives in the 0x60-0x74 window: raise REG_BANK, read, restore as-found CFG0.
    uint8_t cfg0 = 0;
    if (rd(R_CFG0, &cfg0) && wr(R_CFG0, cfg0 | 0x10)) {
        uint8_t cfg = 0;
        if (rd(R_CONFIG, &cfg))
            dlog("[diag]   CONFIG=0x%02X (INT_MODE=%u, 0=SPM expected)", cfg, cfg & 0x03);
        wr(R_CFG0, cfg0);
    }
}

}  // namespace

void as7341DiagRun() {
    failCount = 0; txnOk = 0;
    mqttSeq = 0; mqttLost = 0; mqttSinkUp = mqttConnected();
    dlog("[diag] === AS7341 failure-state forensic (direct Wire, no driver) ===");

    snapshot("pre-state (as found)");

    dlog("[diag] -- normalize + verify measurement-gating config --");
    wrv(R_CFG0, 0x00, "CFG0(bank0,no LP/WLONG)");
    if (wr(R_CFG0, 0x10)) {                       // bank1 for CONFIG
        wrv(R_CONFIG, 0x00, "CONFIG(SPM mode)");
        wr(R_CFG0, 0x00);
    }
    wrv(R_ENABLE, 0x01, "ENABLE(PON)");
    wrv(R_CFG3, 0x00, "CFG3(SAI off)");
    wrv(R_ATIME, CFG_ATIME, "ATIME");
    wrv(R_ASTEP_L, (uint8_t)(CFG_ASTEP & 0xFF), "ASTEP_L");
    wrv(R_ASTEP_H, (uint8_t)(CFG_ASTEP >> 8), "ASTEP_H");
    wrv(R_CFG1, CFG_GAIN_4X, "CFG1(gain4x)");

    uint16_t lo[6] = {0}, hi[6] = {0};
    dlog("[diag] -- SMUX low bank (F1-F4+Clear+NIR): load, verify chain, measure --");
    if (smuxLoadVerified(SMUX_LOW, "low")) measureBank("low", lo);
    dlog("[diag] -- SMUX high bank (F5-F8+Clear+NIR): load, verify chain, measure --");
    if (smuxLoadVerified(SMUX_HIGH, "high")) measureBank("high", hi);

    snapshot("post-state (as left)");
    dlog("[diag] === done: %d failure(s), %d clean transaction(s) ===", failCount, txnOk);
    if (failCount == 0)
        dlog("[diag] config, SMUX chain, mode, and per-measurement gain all verified "
              "healthy. If the channel values are still nonsense for the current light, "
              "the fault is past the registers (analog/electrical) — pure-driver theory "
              "is dead. If this run RECOVERED the sensor, compare the pre-state snapshot "
              "against the values written here: the difference is what init leaves wrong.");
    else
        dlog("[diag] failures above name the exact step. NACK/short-read = bus/silicon; "
              "readback or chain-verify MISMATCH = chip not holding state; gain-used!=4x "
              "= AGC/gain corruption (data was never comparable). All point away from "
              "the driver call path — but check pre-state before blaming electronics.");
    // Sent LAST on purpose: its absence (or a gap in the NN prefixes) tells the remote
    // reader the capture is truncated and the full record is on serial.
    dlog("[diag] mqtt sink: %d line(s) not delivered%s", mqttLost,
         mqttLost ? " — full record on serial only" : "");
}
