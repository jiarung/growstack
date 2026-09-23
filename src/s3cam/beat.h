#pragma once

#include <stdint.h>

// A heartbeat on the existing telemetry topic, so the thing that already
// watches every other device watches this one too.
//
// WHY THIS EXISTS AT ALL
// The main station's silence is noticed: it publishes, and broker's deadman
// alerts per (device, field) when a series goes 15 minutes stale. s3cam
// published nothing, so its death was invisible — and on 2026-09-23 the board
// was off the network for an unknown length of time, discovered only because
// somebody happened to curl it. The firmware now recovers on its own (30 s
// re-associate, 10 min restart), but a recovery that FAILS is still silent,
// which is the state this closes.
//
// Nothing on the broker needs changing. telegraf subscribes to
// monitor-air/+/telemetry, renames the measurement to `air`, and turns the
// topic's second segment into the `device` tag; the deadman watches every
// device on `air` except `sim` and `staging-01`. Publishing here is therefore
// the whole integration — which is also why the device id must be neither of
// those two names, and must be distinct from the station's.
//
// EVERY FIELD PUBLISHED BECOMES A SEPARATELY WATCHED SERIES. A field that can
// legitimately stop reporting would be a permanent false alarm, and alert
// fatigue is how a deadman dies (rules.yaml.tmpl says so in as many words), so
// only continuously-available numbers go out: uptime, die temperature, link
// quality, memory, and counters that only ever rise.
namespace beat {

// Starts a dedicated task. Reads MQTT_* and S3CAM_DEVICE_ID from secrets.h; a
// broker that is not there simply means no heartbeat, not a board that will
// not boot.
//
// ITS OWN TASK, NOT loop(). PubSubClient's connect() blocks until the socket
// times out, and a broker that is down is exactly when it blocks longest. The
// thermal module streams about 11.5 kB/s into a 4 kB driver ring — roughly
// 350 ms of headroom — so a two-second connect on the main loop would drop
// nearly 19 kB of UART and corrupt several frames, every retry, for as long as
// the outage lasted. A heartbeat that costs thermal data has made the board
// less observable, not more.
//
// Everything it reads is already safe to read from another task:
// thermal::statsSnapshot() copies under a critical section by contract, and
// the rest are single-word loads.
void begin();

bool connected();
uint32_t published();      // successful publishes since boot
uint32_t failures();       // publishes attempted while disconnected or refused

}  // namespace beat
