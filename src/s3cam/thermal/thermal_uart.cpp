#include "thermal_uart.h"

#include <Arduino.h>

#include "../cam_pins.h"

namespace thermal {
namespace {

// GY-MCU90640 default UART settings. The module streams continuously once
// powered; nothing needs to be sent to it for the MVP's 4 Hz.
constexpr uint32_t BAUD = 115200;
constexpr uint8_t REFRESH_HZ = 4;      // module default; 8 Hz needs 460800 baud

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

}  // namespace

bool begin() {
    Serial1.begin(BAUD, SERIAL_8N1, THERMAL_PIN_RX, THERMAL_PIN_TX);
    parser.reset();
    lastByteMs = millis();
    lastFrameMs = 0;
    totalBytes = 0;
    sawFrame = false;
    slotFull = false;
    started = true;
    Serial.printf("[thermal] Serial1 %lu baud on RX %u / TX %u, idle timeout %lums\n",
                  (unsigned long)BAUD, THERMAL_PIN_RX, THERMAL_PIN_TX,
                  (unsigned long)IDLE_TIMEOUT_MS);
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
        if (rawArmed && rawFill < RAW_CAP) {          // tee, before parsing
            size_t room = RAW_CAP - rawFill;
            size_t take = got < room ? got : room;
            memcpy(rawBuf + rawFill, buf, take);
            rawFill += take;
            if (rawFill == RAW_CAP) rawArmed = false;
        }
        parser.feed(buf, got);          // bulk feed — the parser is chunk-agnostic
    }
    // The timeout is about a STALLED BODY, not about silence in general: with
    // nothing buffered there is no corpse to discard, and calling it anyway
    // would inflate the timeout counter once per loop while the module is
    // simply unplugged.
    if (parser.stats().frames_ok || totalBytes) {
        if (now - lastByteMs > IDLE_TIMEOUT_MS) {
            parser.discardPartial();    // no-op unless a partial is buffered
            lastByteMs = now;           // one timeout per gap, not per loop
        }
    }
    if (!slotFull && parser.take(slot)) {
        slotFull = true;
        sawFrame = true;
        lastFrameMs = now;
    }
}

bool take(gymcu::ThermalFrame& out) {
    if (!slotFull) return false;
    out = slot;
    slotFull = false;
    return true;
}

bool everSawFrame() { return sawFrame; }

uint32_t sinceLastFrameMs() {
    return sawFrame ? millis() - lastFrameMs : UINT32_MAX;
}

uint32_t bytesSeen() { return totalBytes; }

const gymcu::Parser::Stats& stats() { return parser.stats(); }

void rawArm() { rawFill = 0; rawArmed = true; }
const uint8_t* rawData() { return rawBuf; }
size_t rawLen() { return rawFill; }
size_t rawCapacity() { return RAW_CAP; }

}  // namespace thermal
