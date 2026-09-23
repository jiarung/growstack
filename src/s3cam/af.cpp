#include "af.h"

#include <Arduino.h>
#include <esp_camera.h>

#include "camera.h"
#include "ov5640af_blob.h"

// esp32-camera exports these but does not ship sccb.h in the include path.
// We want the RAW 16-bit-address / 8-bit-value write, not the sensor driver's
// set_reg(): that one does a read-modify-write whenever the mask is partial,
// and the blob's destination is firmware SRAM, where a read-back is not
// promised to return what was written.
extern "C" {
uint8_t SCCB_Write16(uint8_t slv_addr, uint16_t reg, uint8_t data);
}

namespace af {
namespace {

constexpr uint16_t REG_SYS_RESET = 0x3000;
constexpr uint8_t  MCU_RESET_BIT = 0x20;   // bit5
constexpr uint8_t  MCU_PGM_BIT   = 0x40;   // bit6 — program memory in reset
constexpr uint16_t REG_CMD_MAIN  = 0x3022;
constexpr uint16_t REG_CMD_ACK   = 0x3023;
constexpr uint16_t REG_FW_STATE  = 0x3029;
constexpr uint8_t  FW_READY      = 0x70;

bool loaded = false;
uint32_t loadMs = 0;
uint16_t writeFails = 0;
int32_t verifyFailAt = -1;
uint16_t sentBytes = 0;
const char* note = "not loaded";

uint8_t slaveAddr() {
    sensor_t* s = esp_camera_sensor_get();
    return s ? s->slv_addr : 0x3C;   // OV5640's SCCB address
}

bool wr(uint16_t reg, uint8_t val) {
    return SCCB_Write16(slaveAddr(), reg, val) == 0;
}

// Read back what we sent. An I2C ACK only says the sensor heard the byte; it
// says nothing about whether a cell exists at that address to keep it. On this
// part they are very different claims — see load()'s `limit`.
int32_t verifyUpload(size_t n) {
    for (size_t i = 0; i < n; i++) {
        const int got = cameraRegReadLocked((uint16_t)(OV5640_AF_LOAD_ADDR + i));
        if (got != (int)OV5640_AF_CONFIG[i]) return (int32_t)(OV5640_AF_LOAD_ADDR + i);
        if ((i & 0x7F) == 0) delay(1);
    }
    return -1;
}

}  // namespace

size_t blobBytes() { return OV5640_AF_CONFIG_LEN; }

Status status() {
    // One lock for the WHOLE operation. This uploads 4 KB and reads every
    // byte back; the auto-idle tick writes the standby register from the
    // main loop, and landing between two of those reads would fail the
    // verify for a reason that has nothing to do with the upload.
    CameraSensorLock lk;

    Status st;
    st.loaded = loaded;
    st.fw_state = cameraRegReadLocked(REG_FW_STATE);
    st.cmd_ack = cameraRegReadLocked(REG_CMD_ACK);
    st.sys_reset = cameraRegReadLocked(REG_SYS_RESET);
    st.clk_en0 = cameraRegReadLocked(0x3004);
    st.clk_en1 = cameraRegReadLocked(0x3005);
    // Deliberately does NOT wake. A status query that powered the sensor up
    // would change the thing it was asked to observe — and would also reset
    // the idle timer every time somebody looked. Instead it says so, because
    // fw_state read from a sleeping part means nothing without that context.
    st.sensor_idle = cameraIsIdle();
    st.load_ms = loadMs;
    st.write_fails = writeFails;
    st.verify_fail_at = verifyFailAt;
    st.sent = sentBytes;
    st.note = note;
    return st;
}

bool load(size_t limit) {
    // EXCLUSIVE, not merely locked. This resets the sensor's MCU and rewrites
    // its program memory; a capture already in flight would be corrupted, and
    // cameraCapture() releases the plain mutex before acquiring its frame, so
    // holding that alone would not have excluded it.
    CameraExclusive lk;
    if (!lk.ok()) {
        note = "the camera was busy for 15 s — nothing loaded, nothing disturbed";
        return false;
    }
    // In standby the 8051 is powered down: the upload cannot reach ready and
    // a command cannot be consumed. Waking is part of using it, not an
    // optimisation — auto-idle would otherwise make /cam/af?load=1, the
    // documented recovery, the one thing that cannot recover anything.
    cameraWakeLocked();

    loaded = false;
    writeFails = 0;
    verifyFailAt = -1;
    const size_t n = (limit == 0 || limit > OV5640_AF_CONFIG_LEN) ? OV5640_AF_CONFIG_LEN
                                                                  : limit;
    sentBytes = (uint16_t)n;

    const int sys0 = cameraRegReadLocked(REG_SYS_RESET);
    if (sys0 < 0) {
        note = "cannot read 0x3000 — sensor not answering on SCCB";
        return false;
    }
    // The reference driver writes 0x3000 = 0x20 wholesale. That happens to be
    // right there because everything else is out of reset at that point in ITS
    // init — the same "correct by luck" shape as the old whole-byte standby
    // write to 0x3008. Here the camera is already running, so set ONLY bit5 and
    // put the other bits back exactly as they were.
    if (sys0 & MCU_PGM_BIT) {
        // Program memory in reset means the upload would go nowhere, and the
        // failure would look like "firmware never became ready" three steps later.
        note = "0x3000 bit6 set: MCU program memory is in reset";
        Serial.printf("[af] refusing to upload: 0x3000 = 0x%02X (bit6 set)\n", sys0);
        return false;
    }

    Serial.printf("[af] uploading %u of %u bytes to 0x%04X (0x3000 was 0x%02X)\n",
                  (unsigned)n, (unsigned)OV5640_AF_CONFIG_LEN, OV5640_AF_LOAD_ADDR, sys0);
    const uint32_t t0 = millis();

    if (!wr(REG_SYS_RESET, (uint8_t)(sys0 | MCU_RESET_BIT))) {
        note = "failed to hold the MCU in reset";
        return false;
    }

    for (size_t i = 0; i < n; i++) {
        if (!wr((uint16_t)(OV5640_AF_LOAD_ADDR + i), OV5640_AF_CONFIG[i])) writeFails++;
        // The upload is thousands of transactions on a bus the streaming task
        // also uses; yield often enough that the watchdog and the cam task both
        // keep breathing.
        if ((i & 0x7F) == 0) delay(1);
    }

    // Verify BEFORE releasing the MCU: a truncated program is not worth
    // running, and finding out here names the address instead of leaving a
    // silent "firmware never became ready" three steps downstream.
    verifyFailAt = verifyUpload(n);
    if (verifyFailAt >= 0) {
        loadMs = millis() - t0;
        note = "upload did not read back — program memory is smaller than the blob";
        Serial.printf("[af] VERIFY FAILED at 0x%04X (sent %u bytes, %u SCCB errors)\n",
                      (unsigned)verifyFailAt, (unsigned)n, writeFails);
        return false;
    }

    // Command interface cleared, then 0x3029 seeded with 0x7F so that reading
    // 0x70 back later means the FIRMWARE wrote it, not that it was already there.
    for (uint16_t r = REG_CMD_MAIN; r <= 0x3028; r++) wr(r, 0x00);
    wr(REG_FW_STATE, 0x7F);

    if (!wr(REG_SYS_RESET, (uint8_t)(sys0 & ~MCU_RESET_BIT))) {
        note = "failed to release the MCU from reset";
        return false;
    }

    // 5 s at 5 ms, the reference driver's budget.
    int state = -1;
    for (int i = 0; i < 1000; i++) {
        state = cameraRegReadLocked(REG_FW_STATE);
        if (state == FW_READY) break;
        delay(5);
    }
    loadMs = millis() - t0;

    if (state != FW_READY) {
        note = "firmware never reported ready (0x3029 != 0x70)";
        Serial.printf("[af] FAILED after %lums: 0x3029 = 0x%02X, %u write errors\n",
                      (unsigned long)loadMs, state, writeFails);
        return false;
    }
    loaded = true;
    note = writeFails ? "ready, but some SCCB writes reported errors" : "ready";
    Serial.printf("[af] firmware ready after %lums (%u write errors)\n",
                  (unsigned long)loadMs, writeFails);
    return true;
}

// Assumes CameraSensorLock is HELD. focus() issues two of these back to back
// and the mutex is not recursive, so a guard here would deadlock against the
// one focus() already holds — and it would deadlock on the main task, at boot,
// with the watchdog as the only thing left to notice.
static bool commandLocked(uint8_t cmd, uint32_t timeout_ms) {

    if (!wr(REG_CMD_ACK, 0x01)) return false;
    if (!wr(REG_CMD_MAIN, cmd)) return false;
    const uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
        const int ack = cameraRegReadLocked(REG_CMD_ACK);
        if (ack == 0x00) return true;      // firmware consumed it
        delay(5);
    }
    return false;
}

bool command(uint8_t cmd, uint32_t timeout_ms) {
    // One lock for the WHOLE operation. This uploads 4 KB and reads every
    // byte back; the auto-idle tick writes the standby register from the
    // main loop, and landing between two of those reads would fail the
    // verify for a reason that has nothing to do with the upload.
    CameraSensorLock lk;
    // In standby the 8051 is powered down: the upload cannot reach ready and
    // a command cannot be consumed. Waking is part of using it, not an
    // optimisation — auto-idle would otherwise make /cam/af?load=1, the
    // documented recovery, the one thing that cannot recover anything.
    cameraWakeLocked();
    return commandLocked(cmd, timeout_ms);
}

bool focus() {
    // One lock for the WHOLE operation. This uploads 4 KB and reads every
    // byte back; the auto-idle tick writes the standby register from the
    // main loop, and landing between two of those reads would fail the
    // verify for a reason that has nothing to do with the upload.
    CameraSensorLock lk;
    // In standby the 8051 is powered down: the upload cannot reach ready and
    // a command cannot be consumed. Waking is part of using it, not an
    // optimisation — auto-idle would otherwise make /cam/af?load=1, the
    // documented recovery, the one thing that cannot recover anything.
    cameraWakeLocked();

    if (!loaded) {
        note = "focus requested before the firmware was loaded";
        return false;
    }
    if (!commandLocked(0x08, 5000)) {
        note = "command 0x08 not acknowledged";
        return false;
    }
    if (!commandLocked(0x04, 5000)) {
        note = "command 0x04 not acknowledged";
        return false;
    }
    note = "focus commands acknowledged";
    return true;
}

}  // namespace af
