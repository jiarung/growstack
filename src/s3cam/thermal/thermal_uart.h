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
// into every JSON a consumer sees. It comes from head::tag() in head_mount.h,
// which is also where the camera's correction is declared — one bracket, one
// fact, and the two tags are therefore the same words on both sides.
//
// Orientation is a property of how the head is MOUNTED, so it belongs on the
// device. Leaving half of it to the host is what left the viewer with two
// coordinate systems that agreed only while both flips happened to be off.
//
// The tag exists so a host can TELL. A recording made before this correction
// carries its own flip flags, and a tool that applied both would transform
// twice — the second transform being invisible, since a doubly-flipped frame
// is a perfectly ordinary-looking frame of the wrong pixels.
//
// The flip is applied in take(), NOT in the parser: gymcu::Parser is a pure
// byte machine pinned by fixtures whose expected values were derived by hand
// from the wire format, and re-ordering inside it would invalidate every one
// of them to no purpose.
const char* orientation();

// True once a frame has ever been decoded — "the module is talking".
bool everSawFrame();
// Milliseconds since the last complete frame (UINT32_MAX before the first).
uint32_t sinceLastFrameMs();
// Bytes seen on the wire since begin(), whether or not they formed frames:
// separates "nothing is connected" from "something is talking gibberish".
uint32_t bytesSeen();

// THE INVARIANT, MEASURED. poll() must run often enough that the driver's RX
// ring never fills; these say by how much that held, not merely whether it
// broke. Compare pollGapMaxMs() against the ring's own slack (rxBufferBytes()
// at the wire rate) and ringPeakBytes() against rxBufferBytes() — a worst gap
// of 40 ms out of a few hundred available, with the ring peaking at a fraction
// of its size, is margin. Peaks, because nobody watches the console at the
// moment it happens.
//
// rxErrorCount() is the definitive one: the UART driver reporting that it lost
// bytes (ring full or FIFO overrun). Every other counter in this module infers
// loss from the wreckage downstream; this one comes from the layer that
// dropped them. Nonzero means at least one frame in the log is built from an
// incomplete stream — and under ChecksumPolicy::REPORT that frame was
// published as real temperatures.
uint32_t pollGapMaxMs();
uint32_t ringPeakBytes();
uint32_t rxBufferBytes();
uint32_t rxErrorCount();

// The boot window, quarantined. begin() opens the port in setup() but loop()
// does not run until setup() finishes, so the ring overflows before anything
// has ever drained it — measured on hardware, every boot. These two say how
// much that cost; the four counters above start from zero once draining
// begins, so they describe RUNTIME and nothing else. A large
// bootDiscardedBytes() is normal. A large bootRxErrorCount() is normal too.
// Either one CHANGING between boots is worth a look — setup() got slower.
uint32_t bootDiscardedBytes();
uint32_t bootRxErrorCount();

// THE CANONICAL STATEMENT of what the checksum counters mean — "strict" or
// "report", the policy the parser is running under, shipped as a word in the
// stats JSON. Without it `bad_checksum` is uninterpretable: under REPORT it
// tracks frames_ok one-for-one because this module's checksum CONVENTION is
// still unknown (roadmap "未解"), so a reader sees thousands of "bad" frames
// and cannot tell that from a stream that is actually breaking. It also says
// what frames_ok means — decoded, not verified.
//
// Other sites point HERE rather than restate this: the day
// tools/s3cam/thermal_checksum.py identifies the convention, one paragraph
// goes stale instead of five.
const char* checksumPolicyName();

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
