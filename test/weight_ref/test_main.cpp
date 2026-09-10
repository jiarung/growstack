// Host regression test for the weight-ref cache (src/weight_ref.cpp) — the
// four-tier ingest matrix against the anchor fields.
//
// Two failures it exists to keep caught:
//   * a payload must NEVER be dropped over its display name, because a
//     name-only ref is a tier DEMOTION carrier — dropping it leaves a re-bound
//     tag showing the previous plant's stale %;
//   * a malformed field must leave the CACHED entry untouched, because the
//     alternative is a half-updated ref that reads like a measurement.
//
// Compiled by run.sh against the REAL weight_ref.cpp (Arduino stubbed away),
// with ASan and UBSan, so it also catches the parse bugs that would otherwise
// only surface on-device.
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "../../src/weight_ref.h"

// log.h's implementations live in log.cpp (not compiled here) — printf stand-ins
void logTimeBegin() {}
bool logTimeSynced() { return true; }
void logf(const char* fmt, ...) {
    va_list a; va_start(a, fmt); vprintf(fmt, a); va_end(a);
}
void logln(const char* msg) { printf("%s\n", msg); }

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
    printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

// Float equality against a decimal literal is NOT safe here, and the failure is
// silent: ArduinoJson parses with its own compact number parser, which lands one
// ULP off the compiler's literal for values that binary cannot represent —
// 188.4 arrives as 0x433C6667 while the literal 188.4f is 0x433C6666. Values
// like 432.0 happen to be exact and would compare fine, which is precisely how
// this trap stays hidden until someone adds a fractional one. Compare with a
// tolerance far tighter than a gram and the question never arises.
static bool near(float a, float b) { float d = a - b; return d < 0.001f && d > -0.001f; }

static void ingest(const char* uid, const char* json) {
    weightRefOnMessage(uid, (const uint8_t*)json, (unsigned)strlen(json));
}

int main() {
    const char* UID = "AABBCCDD";
    WeightRef r;

    // ---- FULL: anchor + span + ts. The only tier that yields a percentage.
    ingest(UID, "{\"plant_id\":\"cactus-03b\",\"anchor_g\":432.0,\"span_g\":187.0,"
                "\"anchor_ts\":1788094790}");
    CHECK(weightRefGet(UID, &r));
    CHECK(near(r.anchor_g, 432.0f) && near(r.span_g, 187.0f));
    CHECK(r.has_span && r.has_ts && !r.first_anchor);
    CHECK(r.anchor_ts == 1788094790u);
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-03b"));

    // ---- malformed payloads must PRESERVE the cached full ref, not demote it
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":400.0,\"span_g\":\"oops\"}");
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":400.0,\"span_g\":null}");
    ingest(UID, "{\"plant_id\":\"x\",\"span_g\":100.0}");                // span without anchor
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":300.0,\"span_g\":4.0}"); // span <= MIN_SPAN_G
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":\"nope\"}");           // anchor not a number
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":null}");               // explicit null
    CHECK(weightRefGet(UID, &r) && near(r.anchor_g, 432.0f) && near(r.span_g, 187.0f));
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-03b"));

    // a span exactly AT the floor is refused too — the guard is <=, matching
    // the broker's own filter, so the two can never disagree about one number
    ingest(UID, "{\"plant_id\":\"x\",\"anchor_g\":300.0,\"span_g\":5.0}");
    CHECK(weightRefGet(UID, &r) && near(r.anchor_g, 432.0f));

    // ---- the round-3 regression: a re-bind to a legally long (>20 char) name
    // arrives as name-only — it must OVERWRITE the full ref (demotion), with
    // the name truncated for display, never be dropped for its length
    ingest(UID, "{\"plant_id\":\"epiphyllum-oxypetalum-01\",\"name_only\":true}");
    CHECK(!weightRefGet(UID, &r));                      // stale % and anchor are GONE
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "epiphyllum-oxypetalu"));

    // bad-charset name: demotion still ingests, name falls back to nothing
    ingest(UID, "{\"plant_id\":\"caçtus \\u00e9\",\"name_only\":true}");
    CHECK(weightRefPlant(UID) == nullptr);
    CHECK(!weightRefGet(UID, &r));

    // ---- PROVISIONAL: anchor only. Absolute drawdown, never a percentage.
    ingest(UID, "{\"plant_id\":\"cactus-05b\",\"anchor_g\":380.0,\"provisional\":true,"
                "\"anchor_ts\":1788000000}");
    CHECK(weightRefGet(UID, &r) && near(r.anchor_g, 380.0f));
    CHECK(!r.has_span && !r.first_anchor && r.has_ts);
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-05b"));

    // ---- FIRST: the anchor is the pot's own first weighing, not a watering.
    // It carries no span and must never be scored, which is what "1st" says.
    ingest(UID, "{\"plant_id\":\"cactus-25\",\"anchor_g\":188.4,"
                "\"first_anchor\":true,\"anchor_ts\":1788090000}");
    CHECK(weightRefGet(UID, &r) && near(r.anchor_g, 188.4f));
    CHECK(r.first_anchor && !r.has_span && r.has_ts);

    // ---- a ref with no timestamp is legal: the age is omitted, the rest holds
    ingest(UID, "{\"plant_id\":\"cactus-07\",\"anchor_g\":500.0,\"span_g\":60.0}");
    CHECK(weightRefGet(UID, &r) && !r.has_ts && r.has_span);
    CHECK(r.anchor_ts == 0);                            // never a garbage timestamp

    // a non-integer anchor_ts is not a timestamp — take the ref, drop the age
    ingest(UID, "{\"plant_id\":\"cactus-07\",\"anchor_g\":500.0,\"span_g\":60.0,"
                "\"anchor_ts\":\"yesterday\"}");
    CHECK(weightRefGet(UID, &r) && r.has_span && !r.has_ts);

    // ---- unknown keys are ignored, which is what lets the server ship first
    ingest(UID, "{\"plant_id\":\"cactus-07\",\"anchor_g\":501.0,\"span_g\":61.0,"
                "\"sat_g\":999.0,\"dry_g\":111.0,\"anchor_day\":\"2026-08-30\"}");
    CHECK(weightRefGet(UID, &r) && near(r.anchor_g, 501.0f) && near(r.span_g, 61.0f));

    // ---- empty retained payload = the broker's tombstone
    weightRefOnMessage(UID, (const uint8_t*)"", 0);
    CHECK(!weightRefGet(UID, &r) && weightRefPlant(UID) == nullptr);

    // ---- reconnect semantics: clearAll drops everything so the retained replay
    // (not stale RAM) decides what exists
    ingest(UID, "{\"plant_id\":\"cactus-09b\",\"name_only\":true}");
    ingest("11223344", "{\"plant_id\":\"cactus-21\",\"anchor_g\":500.0,\"span_g\":100.0}");
    weightRefClearAll();
    CHECK(weightRefPlant(UID) == nullptr);
    CHECK(!weightRefGet("11223344", &r));

    // ---- a bad UID never reaches the cache (a truncated one could collide)
    ingest("aabbccdd", "{\"plant_id\":\"cactus-99\",\"anchor_g\":1.0}");   // lowercase
    ingest("AABB", "{\"plant_id\":\"cactus-99\",\"anchor_g\":1.0}");       // too short
    CHECK(!weightRefGet("aabbccdd", &r) && !weightRefGet("AABB", &r));

    if (failures) { printf("%d FAILURE(S)\n", failures); return 1; }
    printf("pass weight-ref cache (four-tier anchor matrix, span floor, absent/bad ts, "
           "long-name demotion, malformed preservation, unknown keys, tombstone, "
           "reconnect clear, uid validation)\n");
    return 0;
}
