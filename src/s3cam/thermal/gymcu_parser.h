#pragma once

// GY-MCU90640 UART frame parser — mlx90640 roadmap Phase 1.
//
// PURE C++ (stdint only, no Arduino): the same translation unit runs inside
// the firmware and inside the host fixture runner (test/thermal/), which is
// the whole point — the byte-level contract is frozen offline before the
// module ever arrives.
//
// Frame layout (1544 bytes total; per the module's serial protocol as used by
// the widely-mirrored GY-MCU90640 references — VERIFY AGAINST THE REAL MODULE
// in phase 2 and adjust the constants below if its stream disagrees; the
// fixtures protect everything downstream of them):
//
//   [0]      0x5A            sync
//   [1]      0x5A            sync
//   [2]      0x02            frame type
//   [3]      0x06            subtype / length marker
//   [4..1539]   768 x int16 LE   pixel temperature * 100 (deg C), row-major 24x32
//   [1540..1541] int16 LE        ambient (Ta) * 100
//   [1542..1543] uint16 LE       checksum: sum of bytes [0..1541] & 0xFFFF
//
// The parser is a byte-fed state machine: arbitrary chunking (DMA bursts,
// byte-at-a-time) MUST produce identical output — the fixture runner feeds
// every stream both ways and diffs. Recovery: a failed checksum or broken
// header rescans the buffered bytes for the next sync pair instead of
// discarding blindly, so one corrupt byte costs one frame, not the stream.
#include <stddef.h>
#include <stdint.h>

namespace gymcu {

// The layout is ONE arithmetic statement — every offset derives from it, and
// test/thermal/gen_fixtures.py parses these constants out of this header, so a
// phase-2 correction regenerates correct fixtures instead of invalidating them.
constexpr size_t ROWS = 24, COLS = 32;
constexpr size_t PIXELS = ROWS * COLS;          // 768
constexpr size_t HDR = 4;                       // sync(2) + type + subtype
constexpr size_t OFF_TA = HDR + 2 * PIXELS;     // 1540
constexpr size_t FRAME_LEN = OFF_TA + 2 + 2;    // + Ta + checksum = 1544
constexpr uint8_t SYNC0 = 0x5A, SYNC1 = 0x5A;
constexpr uint8_t TYPE = 0x02, SUBTYPE = 0x06;  // VERIFY-ON-HARDWARE (phase 2)

struct ThermalFrame {
    float pixels[ROWS][COLS]; // deg C; [row][col], row-major off the wire
    float ambient_c;          // Ta, deg C
    uint32_t seq;             // parser-assigned: nth good frame since reset
};

class Parser {
public:
    Parser() { reset(); }
    void reset();

    // Feed any number of bytes (any chunking). Frames become available via
    // take(); only the LATEST completed frame is held (thermal is a live
    // signal — a stale queue would be a lie about "now").
    void feed(const uint8_t* data, size_t len);

    // Move the held frame out. False when none is pending.
    bool take(ThermalFrame& out);

    // The timeout contract (roadmap Phase 1): the parser never reads a clock.
    // The UART layer measures idle time and calls this when a frame has gone
    // quiet mid-body — the partial is dropped and counted, stats/seq survive
    // (reset() is the nuke; this is the routine recovery).
    void discardPartial();

    struct Stats {
        uint32_t frames_ok;       // checksum-verified frames decoded
        uint32_t bad_checksum;    // full frames rejected by checksum
        uint32_t bad_header;      // sync found but type/subtype mismatched
        uint32_t resyncs;         // recovery scans after a rejected frame
        uint32_t bytes_dropped;   // bytes discarded while hunting for sync
        uint32_t overwritten;     // good frames replaced before being taken
        uint32_t timeouts;        // partial frames abandoned via discardPartial()
    };
    const Stats& stats() const { return stats_; }

private:
    void accept();               // buf_ holds FRAME_LEN bytes: verify + decode
    void resync();               // drop buf_[0], rescan buffer for sync

    uint8_t buf_[FRAME_LEN];
    size_t pos_;
    ThermalFrame slot_;
    bool have_;
    uint32_t seq_;
    Stats stats_;
};

}  // namespace gymcu
