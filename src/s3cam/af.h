#pragma once

#include <stddef.h>
#include <stdint.h>

// OV5640 autofocus — uploading the sensor's own firmware so the lens can move.
//
// THE POINT, because it is easy to look for a shortcut that does not exist:
// the focus VCM is driven by the 8051 embedded in the OV5640, and that core is
// held in reset until this firmware is loaded into it. There is no register a
// host can write to nudge the lens instead. If a VCM were separately
// addressable, nobody would ship a 4 KB blob to move it.
//
// Loading is VOLATILE — a power cycle takes the sensor back to no-firmware,
// which is also the recovery if a load goes wrong. Nothing here is done at
// boot: whether it worked is exactly the question being asked, and doing it
// automatically would bury the answer in the boot log.
//
// The command handshake (attested by a shipping driver, not remembered):
// write 0x01 to 0x3023, write the command to 0x3022, then poll 0x3023 until it
// returns to 0x00 — that is the firmware saying it has taken the command.
namespace af {

struct Status {
    bool     loaded = false;      // blob uploaded AND firmware reported ready
    int      fw_state = -1;       // 0x3029; 0x70 = ready, 0x7F = just released
    int      cmd_ack = -1;        // 0x3023; 0x00 = idle / last command consumed
    int      sys_reset = -1;      // 0x3000 — bit5 MCU reset, bit6 program memory
    int      clk_en0 = -1;        // 0x3004 \  clock gating: firmware cannot run
    int      clk_en1 = -1;        // 0x3005 /  on a core whose clock is off
    uint32_t load_ms = 0;         // how long the upload took
    uint16_t write_fails = 0;     // SCCB writes that reported an error
    // Where a read-back first disagreed with what we sent, or -1 for none.
    // The ACK count above is NOT integrity: every one of the 4332 writes was
    // acknowledged while the last 237 bytes went into a single aliased cell.
    int32_t verify_fail_at = -1;
    uint16_t sent = 0;            // bytes actually uploaded this attempt
    const char* note = "";
};

// The full OV5640_Focus_Init sequence, then a READ-BACK of everything sent.
// Blocks for the upload and verify (seconds each) plus up to 5 s waiting for
// the firmware to report ready.
//
// `limit` caps how many bytes are uploaded, because how much program memory
// this particular sensor has is an empirical question, not a datasheet one:
// on the part in front of us, everything from 0x8FFF up aliases to ONE cell,
// so a 4332-byte blob silently becomes 4095 bytes plus 237 writes to the same
// address. Left at 0 it sends the whole blob.
bool load(size_t limit = 0);

// Single autofocus run: command 0x08, then 0x04. Both are what the reference
// driver issues; no others are guessed at here.
bool focus();

// Raw command escape, so a command byte can be tried without a rebuild — the
// same reasoning as /cam/reg: which commands this firmware honours is an
// empirical question, and writes are volatile so trying is cheap.
bool command(uint8_t cmd, uint32_t timeout_ms = 5000);

Status status();

// Size of the embedded blob, so callers need not pull in 4 KB of opaque bytes
// just to report how many there are.
size_t blobBytes();

}  // namespace af
