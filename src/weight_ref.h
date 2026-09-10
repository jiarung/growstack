#pragma once
#include <stdint.h>

// Per-plant watering references, published RETAINED by the broker
// (broker/publish-weight-ref.sh) at monitor-air/ref/weight/<uid>, one per tag UID.
// The station caches them so the OLED can show how far a pot has drawn down since
// its last watering, and how long ago that was.
//
// The broker owns all the statistics; the device only ever subtracts. That
// sentence used to be aspirational — sat and dry were defined in a Grafana panel's
// Flux while the percentage was computed here in C++, one definition in two
// places. The anchor form makes it literal: the broker sends the point to measure
// from and the width of the span, and every arithmetic step here is a subtraction.

// A cached reference, in the only shape the display needs.
//
// anchor_g is the weighing taken AT the watering — not the peak that followed it.
// The two are the same reading 157 times in 166 cycles, and moving to the anchor
// makes the age, the grams and the percentage all measure from ONE point, so the
// OLED and the dashboard cannot drift apart.
struct WeightRef {
    float    anchor_g;      // grams at the anchoring weighing
    float    span_g;        // observed dry-down width; only when has_span
    uint32_t anchor_ts;     // epoch seconds of the anchor; only when has_ts
    bool     has_ts;        // false = broker had no timestamp; omit the age
    bool     has_span;      // false = provisional; absolute drawdown, NO percentage
    bool     first_anchor;  // the anchor IS the pot's first weighing, not a watering
};

// Ingest one ref message for a tag UID (the topic's last segment). Called from the
// MQTT callback — parse-on-arrival, because a (re)connect delivers every retained
// ref in one client.loop() batch and a single-slot mailbox would drop all but one.
// An EMPTY payload deletes the entry (that is how the broker clears a stale ref).
// Invalid input of any kind — bad UID, oversized/malformed payload, non-finite or
// inverted values — is dropped without touching the cache.
void weightRefOnMessage(const char* uid, const uint8_t* payload, unsigned int len);

// The cached ref for a UID. Returns false when there is no entry, or only a
// name-only one — i.e. false means "draw no line", which is exactly what the
// caller needs to decide and the only thing it has to check.
//
// Name-only = the tag is mapped but the plant has never been watered on the
// scale. There is nothing honest to say about water, but the ref still exists
// so weightRefPlant() can greet the pot by name instead of a raw UID.
bool weightRefGet(const char* uid, WeightRef* out);

// The plant's human name ("cactus-03b") from the same retained ref, or nullptr
// when unknown/unnamed — the OLED then falls back to the raw UID. Points into
// the cache: valid until the next message for this UID (single-threaded loop).
// Names longer than the OLED line are stored truncated (display data only).
const char* weightRefPlant(const char* uid);

// Drop every cached entry. Call on each MQTT (re)connect before the retained
// replay: a topic cleared while the station was offline sends no tombstone on
// resubscribe, so stale entries can only die by starting from empty.
void weightRefClearAll();
