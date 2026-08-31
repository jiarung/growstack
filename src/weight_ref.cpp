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

constexpr size_t PLANT_MAX = 20;   // "cactus-03b" style ids; OLED line is 21 chars

struct Entry {
    char  uid[UID_MAX + 1];
    char  plant[PLANT_MAX + 1];    // "" when the payload had no usable plant_id
    float sat_g, dry_g;            // dry_g meaningless when !has_dry
    bool  has_dry;                 // false = provisional ref (sat anchor only)
    bool  used;
};
Entry cache[CAP] = {};

// plant_id is broker-authored but still crosses MQTT: display only a sane id,
// never arbitrary bytes on the OLED. This is a DISPLAY-SAFE SUBSET of what
// add-plant.sh allows ([A-Za-z0-9_-], up to 40 chars): same charset, capped at
// the OLED's 20-char line. Too long or weird -> empty -> fall back to raw UID.
bool validPlant(const char* s) {
    size_t n = strlen(s);
    if (n == 0 || n > PLANT_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
    }
    return true;
}

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
    if (!doc["sat_g"].is<float>()) {
        logf("[ref] %s dropped: missing sat_g\n", uid);
        return;
    }
    // Two tiers off the same topic: dry_g present = a FULL ref (earned span,
    // the OLED may compute used%); dry_g ABSENT = a PROVISIONAL ref (new pot —
    // sat anchor only, the OLED shows the absolute drawdown and never a %).
    // Present-but-not-a-number (string, null) is neither tier: it is a
    // malformed payload and is dropped like any other invalid input — it must
    // never demote a cached full ref to provisional by accident.
    float sat = doc["sat_g"];
    // isUnbound() (not isNull()!) distinguishes "key absent" from an explicit
    // JSON null — ArduinoJson reports isNull()==true for BOTH, and an explicit
    // {"dry_g":null} must be dropped as malformed, never silently demote a
    // cached full ref to provisional.
    bool dryPresent = !doc["dry_g"].isUnbound();
    bool hasDry = doc["dry_g"].is<float>();
    if (dryPresent && !hasDry) {
        logf("[ref] %s dropped: malformed dry_g\n", uid);
        return;
    }
    float dry = hasDry ? (float)doc["dry_g"] : 0.0f;
    // The broker never publishes these, so seeing one means somebody else wrote to
    // the topic — keep whatever good entry we already have rather than overwrite.
    if (!isfinite(sat) || (hasDry && (!isfinite(dry) || sat - dry <= MIN_SPAN_G))) {
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
    cache[i].has_dry = hasDry;
    // the broker has ALWAYS published plant_id on this topic; the station just
    // never kept it — now the OLED can greet the plant by name
    const char* plant = doc["plant_id"] | "";
    if (validPlant(plant)) {
        strncpy(cache[i].plant, plant, sizeof(cache[i].plant) - 1);
        cache[i].plant[sizeof(cache[i].plant) - 1] = '\0';
    } else {
        cache[i].plant[0] = '\0';
    }
    cache[i].used  = true;
    if (hasDry) logf("[ref] %s (%s) sat=%.1f dry=%.1f\n", uid,
                     cache[i].plant[0] ? cache[i].plant : "unnamed", sat, dry);
    else        logf("[ref] %s (%s) sat=%.1f PROVISIONAL\n", uid,
                     cache[i].plant[0] ? cache[i].plant : "unnamed", sat);
}

bool weightRefLookup(const char* uid, float* sat_g, float* dry_g) {
    int i = find(uid);
    if (i < 0 || !cache[i].has_dry) return false;   // provisional refs never yield a %
    *sat_g = cache[i].sat_g;
    *dry_g = cache[i].dry_g;
    return true;
}

bool weightRefSat(const char* uid, float* sat_g) {
    int i = find(uid);
    if (i < 0) return false;
    *sat_g = cache[i].sat_g;
    return true;
}

const char* weightRefPlant(const char* uid) {
    int i = find(uid);
    if (i < 0 || !cache[i].plant[0]) return nullptr;
    return cache[i].plant;
}
