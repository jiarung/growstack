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
        const int got = cameraRegRead((uint16_t)(OV5640_AF_LOAD_ADDR + i));
        if (got != (int)OV5640_AF_CONFIG[i]) return (int32_t)(OV5640_AF_LOAD_ADDR + i);
        if ((i & 0x7F) == 0) delay(1);
    }
    return -1;
}

}  // namespace

size_t blobBytes() { return OV5640_AF_CONFIG_LEN; }

Status status() {
    Status st;
    st.loaded = loaded;
    st.fw_state = cameraRegRead(REG_FW_STATE);
    st.cmd_ack = cameraRegRead(REG_CMD_ACK);
    st.sys_reset = cameraRegRead(REG_SYS_RESET);
    st.clk_en0 = cameraRegRead(0x3004);
    st.clk_en1 = cameraRegRead(0x3005);
    st.load_ms = loadMs;
    st.write_fails = writeFails;
    st.verify_fail_at = verifyFailAt;
    st.sent = sentBytes;
    st.note = note;
    return st;
}

bool load(size_t limit) {
    loaded = false;
    writeFails = 0;
    verifyFailAt = -1;
    const size_t n = (limit == 0 || limit > OV5640_AF_CONFIG_LEN) ? OV5640_AF_CONFIG_LEN
                                                                  : limit;
    sentBytes = (uint16_t)n;

    const int sys0 = cameraRegRead(REG_SYS_RESET);
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
        state = cameraRegRead(REG_FW_STATE);
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

bool command(uint8_t cmd, uint32_t timeout_ms) {
    if (!wr(REG_CMD_ACK, 0x01)) return false;
    if (!wr(REG_CMD_MAIN, cmd)) return false;
    const uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
        const int ack = cameraRegRead(REG_CMD_ACK);
        if (ack == 0x00) return true;      // firmware consumed it
        delay(5);
    }
    return false;
}

bool focus() {
    if (!loaded) {
        note = "focus requested before the firmware was loaded";
        return false;
    }
    if (!command(0x08)) {
        note = "command 0x08 not acknowledged";
        return false;
    }
    if (!command(0x04)) {
        note = "command 0x04 not acknowledged";
        return false;
    }
    note = "focus commands acknowledged";
    return true;
}

}  // namespace af
