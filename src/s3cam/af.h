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
// which is also the recovery if a load goes wrong.
//
// This IS done at boot now (main.cpp, after the server is up so the seconds it
// blocks do not make the board unreachable). The earlier note here said it was
// not, because whether the upload worked was the open question and doing it
// automatically would have buried the answer in the boot log. That question is
// settled: it loads and runs. What is left is a lens that cannot move until it
// has, so loading it is part of being ready rather than an experiment.
//
// STILL OPEN: /power's auto-idle puts the sensor into software standby
// (0x3008 bit6), and whether that resets the 8051 this firmware runs on has
// not been measured. status() reads fw_state live, so /cam/af answers it
// honestly at any moment; ?load=1 restores it. Nothing here pretends to know.
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
    bool     sensor_idle = false; // the sensor is in software standby, so the
                                  // 8051 is powered down and every field above
                                  // describes a part that is not running
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
