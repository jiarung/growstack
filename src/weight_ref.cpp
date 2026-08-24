#include "weight_ref.h"
#include "log.h"

#include <string.h>
#include <math.h>
#include <ArduinoJson.h>

namespace {

constexpr int      CAP         = 20;    // 16 tags today + headroom
constexpr size_t   UID_MIN     = 8;     // 4-byte NFC uid -> 8 hex chars
constexpr size_t   UID_MAX     = 14;    // 7-byte uid -> 14
constexpr float    MIN_SPAN_G  = 5.0f;  // same floor as the broker/panel span filter
constexpr unsigned PAYLOAD_MAX = 192;   // real payloads are ~110 B

struct Entry {
    char  uid[UID_MAX + 1];
    float sat_g, dry_g;
    bool  used;
};
Entry cache[CAP] = {};

// Full-segment check: uppercase hex, 8-14 chars — exactly what readTag() emits.
// Anything else (lowercase, wrong length, stray chars) is rejected outright, never
// truncated: a truncated UID could collide with a real one and poison its ref.
bool validUid(const char* s) {
    size_t n = strlen(s);
    if (n < UID_MIN || n > UID_MAX) return false;
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
    if (!doc["sat_g"].is<float>() || !doc["dry_g"].is<float>()) {
        logf("[ref] %s dropped: missing fields\n", uid);
        return;
    }
    float sat = doc["sat_g"], dry = doc["dry_g"];
    // The broker never publishes these, so seeing one means somebody else wrote to
    // the topic — keep whatever good entry we already have rather than overwrite.
    if (!isfinite(sat) || !isfinite(dry) || sat - dry <= MIN_SPAN_G) {
        logf("[ref] %s dropped: sat=%.1f dry=%.1f\n", uid, sat, dry);
        return;
    }

    int i = find(uid);
    if (i < 0) for (int j = 0; j < CAP; j++) if (!cache[j].used) { i = j; break; }
    if (i < 0) { logf("[ref] cache full, %s dropped\n", uid); return; }

    strncpy(cache[i].uid, uid, sizeof(cache[i].uid) - 1);
    cache[i].uid[sizeof(cache[i].uid) - 1] = '\0';
    cache[i].sat_g = sat;
    cache[i].dry_g = dry;
    cache[i].used  = true;
    logf("[ref] %s sat=%.1f dry=%.1f\n", uid, sat, dry);
}

bool weightRefLookup(const char* uid, float* sat_g, float* dry_g) {
    int i = find(uid);
    if (i < 0) return false;
    *sat_g = cache[i].sat_g;
    *dry_g = cache[i].dry_g;
    return true;
}
