#include "thermal_uart.h"

#include <Arduino.h>
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

gymcu::Parser parser;
uint32_t lastByteMs = 0;      // last time ANY byte arrived (drives the timeout)
uint32_t lastFrameMs = 0;
uint32_t totalBytes = 0;
bool sawFrame = false;
bool started = false;
gymcu::ThermalFrame slot;
bool slotFull = false;

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
    parser.reset();
    lastByteMs = millis();
    lastFrameMs = 0;
    totalBytes = 0;
    sawFrame = false;
    slotFull = false;
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
        sawFrame = true;
        lastFrameMs = now;
        portEXIT_CRITICAL(&mux);
    }
}

bool take(gymcu::ThermalFrame& out) {
    portENTER_CRITICAL(&mux);
    bool have = slotFull;
    if (have) {
        out = slot;
        slotFull = false;
    }
    portEXIT_CRITICAL(&mux);
    return have;
}

bool everSawFrame() { return sawFrame; }

uint32_t sinceLastFrameMs() {
    return sawFrame ? millis() - lastFrameMs : UINT32_MAX;
}

uint32_t bytesSeen() { return totalBytes; }

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

size_t rawLen() {
    portENTER_CRITICAL(&mux);
    size_t n = rawFill;
    portEXIT_CRITICAL(&mux);
    return n;
}

size_t rawCapacity() { return RAW_CAP; }

}  // namespace thermal
