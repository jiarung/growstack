#include "thermal_uart.h"
#include "../head_mount.h"

#include <Arduino.h>
#include <algorithm>
#include <string.h>

#include "../cam_pins.h"

namespace thermal {
namespace {

// GY-MCU90640 default UART settings. The module streams continuously once
// powered; nothing needs to be sent to it for the MVP's 4 Hz.
constexpr uint32_t BAUD = 115200;
constexpr uint8_t REFRESH_HZ = 4;      // module default; 8 Hz needs 460800 baud

// The driver's RX ring must outlast the gap between poll() calls, or bytes are
// lost INSIDE the UART driver and never reach the parser — silently, and in a
// regular pattern that looks exactly like a shorter frame. At 115200/8N1 the
// wire delivers ~11.5 kB/s, so the stock 256-byte ring holds only ~22 ms.
// 4 KB buys ~350 ms of slack: enough that a slow loop pass degrades throughput
// instead of corrupting the stream.
constexpr size_t RX_BUFFER = 4096;

// A frame period is 1/REFRESH_HZ; a body that stalls for several of them is a
// broken stream, not a slow module. Three periods is loose enough to survive
// one dropped frame and tight enough that the parser never carries a corpse
// into the next frame's bytes.
constexpr uint32_t IDLE_TIMEOUT_MS = 3 * 1000 / REFRESH_HZ;

// ONE definition, so the policy and the word reported for it cannot drift.
// Flip this the day the convention is identified; see checksumPolicyName() in
// the header for what it means downstream.
constexpr gymcu::ChecksumPolicy POLICY = gymcu::ChecksumPolicy::REPORT;

gymcu::Parser parser;
uint32_t lastByteMs = 0;      // last time ANY byte arrived (drives the timeout)

// The invariant this module actually depends on is "poll() runs often enough
// that the driver's RX ring never fills" — and nothing measured it. The
// parser's `overwritten` was the closest proxy, but it only moves once two
// whole frames have completed inside one pass, i.e. after the gap already ate
// ~70% of the slack, and it says nothing at all about bytes lost INSIDE the
// driver, which the parser never sees.
//
// These two are the margin itself: the worst gap between passes, and the most
// bytes ever found standing in the ring. Peaks rather than instantaneous
// values, for the reason health::dieMaxC() keeps a peak — nobody is watching
// the console at the moment it happens.
uint32_t lastPollMs = 0;
uint32_t pollGapMax = 0;
uint32_t ringPeak = 0;

// EVERYTHING BEFORE THE FIRST poll() IS QUARANTINED.
//
// begin() opens the port in setup(), and the module starts streaming at once —
// but loop() does not run until setup() has also brought up the camera (which
// pushes 4077 bytes of AF firmware over SCCB), WiFi and NTP. That is seconds,
// and the ring holds 356 ms. So the ring overflows before anyone has ever
// drained it, every single boot.
//
// Measured on hardware 2026-09-29: ringpk pinned at 4072/4096 with rxerr=285
// and dropped=1164 — all four frozen thereafter, while gap sat at 7 ms. The
// peaks were reporting a boot artefact and could never report runtime margin
// again, which is the one job they were added for.
//
// So the first pass discards what accumulated instead of feeding it (those
// bytes are a fragment by construction — the ring dropped the middle of them),
// and the runtime counters start from zero at the moment draining actually
// begins. The boot figures are kept, not hidden: they are their own two
// fields, and a boot that loses bytes is still worth seeing.
bool firstPoll = true;
uint32_t bootDiscarded = 0;
uint32_t bootRxErrors = 0;
// Bytes the UART DRIVER reports losing: ring full, or hardware FIFO overrun.
// This is the definitive signal, straight from the layer that dropped them —
// every other counter here is downstream inference about the wreckage.
volatile uint32_t rxErrors = 0;
uint32_t lastFrameMs = 0;
uint32_t totalBytes = 0;
bool sawFrame = false;
bool started = false;
gymcu::ThermalFrame slot;
bool slotFull = false;
float lastAmbient = 0.0f;     // status mirror of slot.ambient_c — see header
bool haveAmbient = false;

// Raw tee: big enough to hold two frame periods, so a sync-to-sync distance is
// always visible no matter where arming lands in the stream.
constexpr size_t RAW_CAP = 3600;
uint8_t rawBuf[RAW_CAP];
size_t rawFill = 0;
bool rawArmed = false;

// poll() runs on the main task; the HTTP handlers run on the httpd task. Every
// shared field above is written by one and read by the other, so each side
// takes this. It is held only for pointer/counter shuffling and memcpy of at
// most one UART read — never across a network send.
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;

}  // namespace

bool begin() {
    // setRxBufferSize BEFORE begin(): afterwards the driver has already
    // allocated the ring and the call is ignored.
    Serial1.setRxBufferSize(RX_BUFFER);
    Serial1.begin(BAUD, SERIAL_8N1, THERMAL_PIN_RX, THERMAL_PIN_TX);
    // The driver telling us, definitively, that bytes were lost. Everything
    // else in this file infers loss from the wreckage downstream of it; this
    // is the only counter that cannot be fooled by a corrupt frame that
    // happens to decode. Counting only — deciding to DISTRUST a frame on the
    // strength of it is a policy change, and belongs with the ChecksumPolicy
    // work rather than riding along here (tasks/todo.md).
    Serial1.onReceiveError([](hardwareSerial_error_t e) {
        if (e == UART_BUFFER_FULL_ERROR || e == UART_FIFO_OVF_ERROR) rxErrors++;
    });
    parser.reset();
    // BRING-UP: this module's payload verifies by inspection (768 plausible
    // temperatures, a sane Ta) but its trailing two bytes match none of the
    // obvious sum conventions, so STRICT would discard every good frame over
    // an unknown convention — see checksumPolicyName() for what that costs.
    parser.setChecksumPolicy(POLICY);
    lastByteMs = millis();
    lastPollMs = 0;              // 0 = "no previous pass", so the first gap is not counted
    pollGapMax = 0;
    ringPeak = 0;
    rxErrors = 0;
    firstPoll = true;
    bootDiscarded = 0;
    bootRxErrors = 0;
    lastFrameMs = 0;
    totalBytes = 0;
    sawFrame = false;
    slotFull = false;
    haveAmbient = false;
    rawFill = 0;
    rawArmed = false;
    started = true;
    Serial.printf("[thermal] Serial1 %lu baud on RX %u / TX %u, rx buffer %u B, "
                  "idle timeout %lums\n",
                  (unsigned long)BAUD, THERMAL_PIN_RX, THERMAL_PIN_TX,
                  (unsigned)RX_BUFFER, (unsigned long)IDLE_TIMEOUT_MS);
    return true;
}

void poll() {
    if (!started) return;
    uint8_t buf[256];
    const uint32_t now = millis();
    // Measured BEFORE draining: `avail` after the loop is zero by construction,
    // so the high-water mark only means anything sampled here.
    const uint32_t gap = now - lastPollMs;
    if (lastPollMs && gap > pollGapMax) pollGapMax = gap;
    lastPollMs = now;
    const uint32_t standing = (uint32_t)Serial1.available();
    if (standing > ringPeak) ringPeak = standing;

    if (firstPoll) {
        firstPoll = false;
        while (int avail = Serial1.available()) {
            size_t want = (size_t)avail < sizeof(buf) ? (size_t)avail : sizeof(buf);
            size_t got = Serial1.readBytes(buf, want);
            if (!got) break;
            bootDiscarded += got;
        }
        parser.discardPartial();     // no-op; the parser has been fed nothing yet
        bootRxErrors = rxErrors;
        rxErrors = 0;
        ringPeak = 0;
        pollGapMax = 0;
        lastPollMs = millis();       // the drain itself is not a gap
        return;
    }

    while (int avail = Serial1.available()) {
        size_t want = (size_t)avail < sizeof(buf) ? (size_t)avail : sizeof(buf);
        size_t got = Serial1.readBytes(buf, want);
        if (!got) break;
        totalBytes += got;
        lastByteMs = now;
        if (rawArmed) {                               // tee, before parsing
            portENTER_CRITICAL(&mux);
            if (rawArmed && rawFill < RAW_CAP) {
                size_t room = RAW_CAP - rawFill;
                size_t take = got < room ? got : room;
                memcpy(rawBuf + rawFill, buf, take);
                rawFill += take;
                if (rawFill == RAW_CAP) rawArmed = false;   // frozen for reading
            }
            portEXIT_CRITICAL(&mux);
        }
        parser.feed(buf, got);          // bulk feed — the parser is chunk-agnostic
    }
    // The timeout is about a STALLED BODY, not about silence in general: with
    // nothing buffered there is no corpse to discard, and calling it anyway
    // would inflate the timeout counter once per loop while the module is
    // simply unplugged.
    if (totalBytes && now - lastByteMs > IDLE_TIMEOUT_MS) {
        parser.discardPartial();        // no-op unless a partial is buffered
        lastByteMs = now;               // one timeout per gap, not per loop
    }
    // LATEST-wins, not first-unread: drain every frame the parser has and keep
    // the newest. Holding the first one until somebody reads it would serve a
    // stale frame — and a stale ms_since_frame with it — for as long as nobody
    // asked, which is the opposite of what this slot promises.
    gymcu::ThermalFrame f;
    bool got = false;
    while (parser.take(f)) got = true;
    if (got) {
        portENTER_CRITICAL(&mux);
        slot = f;
        slotFull = true;
        lastAmbient = f.ambient_c;   // survives take(); status, not payload
        haveAmbient = true;
        sawFrame = true;
        lastFrameMs = now;
        portEXIT_CRITICAL(&mux);
    }
}

// The mounting correction, from head_mount.h — the same declaration
// camera.cpp reads. One bracket, one fact, one place to change it.
//
// This was a 180-degree rotation until 2026-09-28, calibrated against an RGB
// image that was itself mirrored. See head_mount.h for how both halves were
// measured and why a mirror, unlike a rotation, is something no downstream
// stage can absorb.
//
// Applied in take(), NOT in the parser: gymcu::Parser is a pure byte machine
// pinned by fixtures whose expected values were derived by hand from the wire
// format, and re-ordering inside it would invalidate every one of them to no
// purpose.
// Row order reversed; each row's own order untouched. That last part is the
// whole difference from the rotation this replaced — reversing the flat pixel
// list would mirror horizontally as well.
static void flipVertical(gymcu::ThermalFrame& f) {
    for (size_t top = 0, bot = gymcu::ROWS - 1; top < bot; ++top, --bot)
        for (size_t c = 0; c < gymcu::COLS; ++c)
            std::swap(f.pixels[top][c], f.pixels[bot][c]);
}

// Columns reversed within each row. Not reachable from the current mounting,
// and present so that a remount only ever edits head_mount.h.
static void flipHorizontal(gymcu::ThermalFrame& f) {
    for (size_t r = 0; r < gymcu::ROWS; ++r)
        for (size_t a = 0, b = gymcu::COLS - 1; a < b; ++a, --b)
            std::swap(f.pixels[r][a], f.pixels[r][b]);
}

bool take(gymcu::ThermalFrame& out) {
    portENTER_CRITICAL(&mux);
    bool have = slotFull;
    if (have) {
        out = slot;
        slotFull = false;
    }
    portEXIT_CRITICAL(&mux);
    // Outside the critical section on purpose: 768 floats is far too much work
    // to do with interrupts masked, and `out` is the caller's own copy by now.
    if (have) {
        if (head::MIRRORED_V) flipVertical(out);
        if (head::MIRRORED_H) flipHorizontal(out);
    }
    return have;
}

// head::tag(), not a second spelling of it. The camera's tag comes from the
// same function, so a host comparing the two is comparing like with like —
// and the translation table the host used to need for "wire" against "none"
// is gone with it.
const char* orientation() { return head::tag(); }

bool everSawFrame() { return sawFrame; }

uint32_t sinceLastFrameMs() {
    return sawFrame ? millis() - lastFrameMs : UINT32_MAX;
}

uint32_t pollGapMaxMs() { return pollGapMax; }
uint32_t ringPeakBytes() { return ringPeak; }
uint32_t rxBufferBytes() { return RX_BUFFER; }
uint32_t rxErrorCount() { return rxErrors; }
uint32_t bootDiscardedBytes() { return bootDiscarded; }
uint32_t bootRxErrorCount() { return bootRxErrors; }

const char* checksumPolicyName() {
    return POLICY == gymcu::ChecksumPolicy::STRICT ? "strict" : "report";
}

uint32_t bytesSeen() { return totalBytes; }

bool lastAmbientC(float& out) {
    portENTER_CRITICAL(&mux);
    bool have = haveAmbient;
    if (have) out = lastAmbient;
    portEXIT_CRITICAL(&mux);
    return have;
}

gymcu::Parser::Stats statsSnapshot() {
    portENTER_CRITICAL(&mux);
    gymcu::Parser::Stats s = parser.stats();   // copy, not a live reference
    portEXIT_CRITICAL(&mux);
    return s;
}

void rawArm() {
    portENTER_CRITICAL(&mux);
    rawFill = 0;
    rawArmed = true;
    portEXIT_CRITICAL(&mux);
}

bool rawBusy() {
    portENTER_CRITICAL(&mux);
    bool b = rawArmed;
    portEXIT_CRITICAL(&mux);
    return b;
}

size_t rawCopy(uint8_t* dst, size_t cap) {
    portENTER_CRITICAL(&mux);
    size_t n = rawFill < cap ? rawFill : cap;
    memcpy(dst, rawBuf, n);
    portEXIT_CRITICAL(&mux);
    return n;
}

}  // namespace thermal
