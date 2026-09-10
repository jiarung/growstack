#include "weight_ref.h"
#include "log.h"

#include <string.h>
#include <math.h>
#include <ArduinoJson.h>

namespace {

constexpr int      CAP         = 40;    // EVERY mapped uid now gets a retained
                                        // ref (name-only floor): 26 uids as of
                                        // 2026-09-01, doubled for growth —
                                        // ~2 KB of statics, cheap insurance
constexpr size_t   UID_HEX_MIN = 8;     // 4-byte NFC uid -> 8 hex chars
constexpr size_t   UID_HEX_MAX = 14; // 7-byte uid -> 14
constexpr float    MIN_SPAN_G  = 5.0f;  // same floor as the broker/panel span filter
constexpr unsigned PAYLOAD_MAX = 192;   // real payloads are ~110 B

constexpr size_t PLANT_MAX = 20;   // "cactus-03b" style ids; OLED line is 21 chars

struct Entry {
    char     uid[UID_HEX_MAX + 1];
    char     plant[PLANT_MAX + 1];  // "" when the payload had no usable plant_id
    float    anchor_g, span_g;      // meaningless when the matching has_* is false
    uint32_t anchor_ts;
    bool     has_anchor;            // false = name-only ref (no watering anchor)
    bool     has_span;              // false = provisional or name-only
    bool     has_ts;
    bool     first_anchor;
    bool     used;
};
Entry cache[CAP] = {};

// plant_id is broker-authored but still crosses MQTT: display only a sane id,
// never arbitrary bytes on the OLED. The DISPLAY is a subset of what
// add-plant.sh allows ([A-Za-z0-9_-], up to 40 chars) — same charset, capped
// at the OLED's 20-char line — but the name must NEVER decide whether a
// payload is ingested: a name-only ref is a TIER DEMOTION carrier, and
// dropping it over a long name would leave a re-bound tag showing the old
// plant's stale %. So: a legal-charset name longer than the line is
// TRUNCATED; a bad-charset name becomes "" (OLED falls back to the UID);
// the payload itself is ingested either way.
void sanitizePlant(char* dst, size_t dstsz, const char* s) {
    dst[0] = '\0';
    if (s[0] == '\0') return;
    for (size_t i = 0; s[i]; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) {
            dst[0] = '\0';        // weird bytes: no name at all, not a prefix
            return;
        }
        if (i < dstsz - 1) { dst[i] = c; dst[i + 1] = '\0'; }  // truncate long
    }
}

// Full-segment check: uppercase hex, 8-14 chars — exactly what readTag() emits.
// Anything else (lowercase, wrong length, stray chars) is rejected outright, never
// truncated: a truncated UID could collide with a real one and poison its ref.
bool validUid(const char* s) {
    size_t n = strlen(s);
    if (n < UID_HEX_MIN || n > UID_HEX_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) return false;
    }
    return true;
}

int find(const char* uid) {
    for (int i = 0; i < CAP; i++)
        if (cache[i].used && strcmp(cache[i].uid, uid) == 0) return i;
    return -1;
}

}  // namespace

void weightRefOnMessage(const char* uid, const uint8_t* payload, unsigned int len) {
    if (!validUid(uid)) return;

    if (len == 0) {                       // broker cleared the retained ref
        int i = find(uid);
        if (i >= 0) { cache[i].used = false; logf("[ref] %s cleared\n", uid); }
        return;
    }
    if (len > PAYLOAD_MAX) { logf("[ref] %s dropped: %u B\n", uid, len); return; }

    char buf[PAYLOAD_MAX + 1];
    memcpy(buf, payload, len); buf[len] = '\0';
    JsonDocument doc;
    if (deserializeJson(doc, buf)) { logf("[ref] %s dropped: bad json\n", uid); return; }
    // Four tiers off the same topic, keyed by which fields exist:
    //   anchor+span = FULL (may show a %) · anchor only = PROVISIONAL (absolute
    //   drawdown) · anchor + first_anchor = FIRST (the anchor is the pot's own
    //   first weighing, which is not a watering, so never a %) · none = NAME-ONLY
    //   (a brand-new pot: nothing honest to say about water, but the OLED can
    //   still greet it by name).
    // Present-but-not-a-number is neither tier — malformed, dropped.
    bool anchorPresent = !doc["anchor_g"].isUnbound();
    bool hasAnchor = doc["anchor_g"].is<float>();
    if (anchorPresent && !hasAnchor) {
        logf("[ref] %s dropped: malformed anchor_g\n", uid);
        return;
    }
    // isUnbound() (not isNull()!) distinguishes "key absent" from an explicit
    // JSON null — ArduinoJson reports isNull()==true for BOTH, and an explicit
    // null must be dropped as malformed, never silently demote a cached ref.
    float anchor = hasAnchor ? (float)doc["anchor_g"] : 0.0f;
    bool spanPresent = !doc["span_g"].isUnbound();
    bool hasSpan = doc["span_g"].is<float>();
    if ((spanPresent && !hasSpan) || (hasSpan && !hasAnchor)) {
        logf("[ref] %s dropped: malformed span_g\n", uid);  // incl. span without anchor
        return;
    }
    float span = hasSpan ? (float)doc["span_g"] : 0.0f;
    // The broker never publishes these, so seeing one means somebody else wrote to
    // the topic — keep whatever good entry we already have rather than overwrite.
    // A span at or under the floor gets the same refusal the broker and the panel
    // make: a % against a span that narrow reads "drier than reality" and nudges
    // overwatering, the cactus-killing direction.
    if ((hasAnchor && !isfinite(anchor)) ||
        (hasSpan && (!isfinite(span) || span <= MIN_SPAN_G))) {
        logf("[ref] %s dropped: anchor=%.1f span=%.1f\n", uid, anchor, span);
        return;
    }
    // Absent is legal and simply omits the age: the grams and the % stay true
    // without it, so a missing timestamp must not gate ingest either.
    bool hasTs = doc["anchor_ts"].is<uint32_t>();
    uint32_t ts = hasTs ? (uint32_t)doc["anchor_ts"] : 0;
    bool firstAnchor = doc["first_anchor"] | false;
    // the name is best-effort display data, never an ingest gate (see
    // sanitizePlant) — a name-only ref with an unusable name still ingests,
    // because its real job may be DEMOTING a stale full/provisional entry
    char plant[PLANT_MAX + 1];
    sanitizePlant(plant, sizeof(plant), doc["plant_id"] | "");

    int i = find(uid);
    if (i < 0) for (int j = 0; j < CAP; j++) if (!cache[j].used) { i = j; break; }
    if (i < 0) { logf("[ref] cache full, %s dropped\n", uid); return; }

    strncpy(cache[i].uid, uid, sizeof(cache[i].uid) - 1);
    cache[i].uid[sizeof(cache[i].uid) - 1] = '\0';
    cache[i].anchor_g = anchor;
    cache[i].span_g = span;
    cache[i].anchor_ts = ts;
    cache[i].has_anchor = hasAnchor;
    cache[i].has_span = hasSpan;
    cache[i].has_ts = hasTs;
    cache[i].first_anchor = firstAnchor;
    strncpy(cache[i].plant, plant, sizeof(cache[i].plant) - 1);
    cache[i].plant[sizeof(cache[i].plant) - 1] = '\0';
    cache[i].used  = true;
    const char* name = cache[i].plant[0] ? cache[i].plant : "unnamed";
    if (hasSpan)          logf("[ref] %s (%s) anchor=%.1f span=%.1f ts=%lu\n",
                               uid, name, anchor, span, (unsigned long)ts);
    else if (firstAnchor) logf("[ref] %s (%s) anchor=%.1f FIRST ts=%lu\n",
                               uid, name, anchor, (unsigned long)ts);
    else if (hasAnchor)   logf("[ref] %s (%s) anchor=%.1f PROVISIONAL ts=%lu\n",
                               uid, name, anchor, (unsigned long)ts);
    else                  logf("[ref] %s (%s) NAME-ONLY\n", uid, name);
}

bool weightRefGet(const char* uid, WeightRef* out) {
    int i = find(uid);
    if (i < 0 || !cache[i].has_anchor) return false;   // name-only: nothing to draw
    out->anchor_g     = cache[i].anchor_g;
    out->span_g       = cache[i].span_g;
    out->anchor_ts    = cache[i].anchor_ts;
    out->has_ts       = cache[i].has_ts;
    out->has_span     = cache[i].has_span;
    out->first_anchor = cache[i].first_anchor;
    return true;
}

const char* weightRefPlant(const char* uid) {
    int i = find(uid);
    if (i < 0 || !cache[i].plant[0]) return nullptr;
    return cache[i].plant;
}

void weightRefClearAll() {
    // Called on every MQTT (re)connect BEFORE the retained refs replay: a ref
    // whose topic was cleared while we were offline never sends a tombstone on
    // resubscribe, so the only way to drop it is to start empty and let the
    // replay rebuild the truth. The gap lasts one retained-replay batch.
    for (int i = 0; i < CAP; i++) cache[i].used = false;
    logln("[ref] cache cleared for retained replay");
}
