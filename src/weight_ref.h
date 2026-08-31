#pragma once
#include <stdint.h>

// Per-plant watering references — {sat_g, dry_g} — published RETAINED by the broker
// (broker/publish-weight-ref.sh) at monitor-air/ref/weight/<uid>, one per tag UID.
// The station caches them so the OLED can show "how far from the last full watering"
// while a pot sits on the scale. The broker owns all the statistics; the device only
// ever subtracts.

// Ingest one ref message for a tag UID (the topic's last segment). Called from the
// MQTT callback — parse-on-arrival, because a (re)connect delivers every retained
// ref in one client.loop() batch and a single-slot mailbox would drop all but one.
// An EMPTY payload deletes the entry (that is how the broker clears a stale ref).
// Invalid input of any kind — bad UID, oversized/malformed payload, non-finite or
// inverted values — is dropped without touching the cache.
void weightRefOnMessage(const char* uid, const uint8_t* payload, unsigned int len);

// Look up a FULL cached ref (sat + earned dry span). Returns false when the UID
// has no valid entry OR only a provisional one — the caller (the OLED) then
// falls back to weightRefSat() and never fabricates a percentage.
bool weightRefLookup(const char* uid, float* sat_g, float* dry_g);

// The sat anchor alone — true for full AND provisional refs. Provisional = the
// broker saw a watering anchor but no trustworthy span yet (new pot / repot):
// the honest display is the absolute drawdown, "-87g since wtr".
bool weightRefSat(const char* uid, float* sat_g);

// The plant's human name ("cactus-03b") from the same retained ref, or nullptr
// when unknown/unnamed — the OLED then falls back to the raw UID. Points into
// the cache: valid until the next message for this UID (single-threaded loop).
const char* weightRefPlant(const char* uid);
