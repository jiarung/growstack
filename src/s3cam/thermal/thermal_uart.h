#pragma once

#include <stdint.h>

#include "gymcu_parser.h"

// The clocked layer between Serial1 and the (deliberately clockless) frame
// parser — mlx90640 roadmap Phase 2.
//
// The parser is a pure byte machine that never reads a clock; SOMETHING has to
// decide when a half-received frame is dead. That policy lives here, derived
// from the module's own cadence rather than guessed: at the configured refresh
// rate a frame arrives every 1/rate seconds, so a gap of several frame periods
// mid-body means the stream broke, not that the module is slow.
//
// Wiring (see cam_pins.h for the pin budget): module TX -> ESP32 RX, module RX
// -> ESP32 TX. Crossed. Getting this backwards yields a parser that never sees
// a byte — bytes_dropped stays 0 while frames_ok stays 0, which is its own
// distinctive symptom.

namespace thermal {

bool begin();          // opens Serial1, resets the parser; true if the port opened
// Drain whatever arrived; call EVERY loop pass and keep the pass short. The
// driver's RX ring is finite: bytes that arrive while the loop is elsewhere
// are lost inside the UART driver, and lost bytes look exactly like a shorter
// frame — a failure that reads as data rather than as an error.
void poll();
bool take(gymcu::ThermalFrame& out);   // NEWEST complete frame, once

// True once a frame has ever been decoded — "the module is talking".
bool everSawFrame();
// Milliseconds since the last complete frame (UINT32_MAX before the first).
uint32_t sinceLastFrameMs();
// Bytes seen on the wire since begin(), whether or not they formed frames:
// separates "nothing is connected" from "something is talking gibberish".
uint32_t bytesSeen();

// A COPY, not a live reference: poll() runs on the main task and callers run
// on the httpd task, so handing out a pointer into mutating state would be a
// data race dressed as an accessor.
gymcu::Parser::Stats statsSnapshot();

// ---- raw capture: ground truth for the VERIFY-ON-HARDWARE constants --------
// The parser can only report that a frame failed; it cannot say what the
// module actually sent. This tees the incoming bytes into a plain buffer so
// the real layout can be read off the wire — sync spacing gives the true frame
// length, and the bytes after the payload give the real checksum field.
// Cross-task safe: arm, wait for !rawBusy(), then copy out.
void rawArm();                       // start (or restart) filling the buffer
bool rawBusy();                      // still filling
size_t rawCopy(uint8_t* dst, size_t cap);   // snapshot into the caller's buffer
size_t rawLen();
size_t rawCapacity();

}  // namespace thermal
