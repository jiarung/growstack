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
// NEWEST complete frame, once, IN HEAD ORIENTATION — see orientation() below.
bool take(gymcu::ThermalFrame& out);

// How the pixels in a frame from take() are arranged, as a short tag that goes
// into every JSON a consumer sees ("rot180" or "wire").
//
// The head carries both sensors upside down, so the camera is corrected in the
// OV5640's own hmirror/vflip registers (camera.cpp) and the thermal frame is
// rotated here. Correcting both on the device means every consumer — /thermal,
// /observation, the viewer, the registration fit — sees one orientation and
// none of them needs a flag. Orientation is a property of how the head is
// MOUNTED, which is a fact about this device, so it belongs on the device;
// leaving half of it to the host is what left the viewer with two coordinate
// systems that agreed only while both flips were off.
//
// The tag exists so a host can TELL. A recording made before this correction
// carries its own flip flags, and a tool that applied both would rotate twice
// — the second rotation being invisible, since a doubly-rotated frame is a
// perfectly ordinary-looking frame of the wrong pixels.
//
// The rotation is applied in take(), NOT in the parser: gymcu::Parser is a
// pure byte machine pinned by fixtures whose expected values were derived by
// hand from the wire format, and re-ordering inside it would invalidate every
// one of them to no purpose.
const char* orientation();

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

// The module's own ambient (Ta) from the most recent frame; false until one has
// decoded. Tracked SEPARATELY from the frame slot on purpose: take() is
// consume-once, so answering a status query through it would let a status
// endpoint silently steal a frame from a data endpoint. Ta is status; the
// pixel frame is payload; they get different plumbing.
bool lastAmbientC(float& out);

// ---- raw capture: ground truth for the VERIFY-ON-HARDWARE constants --------
// The parser can only report that a frame failed; it cannot say what the
// module actually sent. This tees the incoming bytes into a plain buffer so
// the real layout can be read off the wire — sync spacing gives the true frame
// length, and the bytes after the payload give the real checksum field.
// Cross-task safe: arm, wait for !rawBusy(), then copy out.
void rawArm();                       // start (or restart) filling the buffer
bool rawBusy();                      // still filling
size_t rawCopy(uint8_t* dst, size_t cap);   // snapshot into the caller's buffer

}  // namespace thermal
