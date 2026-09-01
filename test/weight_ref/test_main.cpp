// Host regression test for the weight-ref cache (src/weight_ref.cpp) — the
// three-tier ingest matrix, and specifically the failure that review round 3
// found: a payload must NEVER be dropped over its display name, because a
// name-only ref is a tier DEMOTION carrier — dropping it leaves a re-bound
// tag showing the previous plant's stale %.
// Compiled by run.sh against the REAL weight_ref.cpp (Arduino stubbed away).
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "../../src/weight_ref.h"

// log.h's implementations live in log.cpp (not compiled here) — printf stand-ins
void logTimeBegin() {}
void logf(const char* fmt, ...) {
    va_list a; va_start(a, fmt); vprintf(fmt, a); va_end(a);
}
void logln(const char* msg) { printf("%s\n", msg); }

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
    printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static void ingest(const char* uid, const char* json) {
    weightRefOnMessage(uid, (const uint8_t*)json, (unsigned)strlen(json));
}

int main() {
    const char* UID = "AABBCCDD";
    float sat, dry;

    // full ref: % path opens, name cached
    ingest(UID, "{\"plant_id\":\"cactus-03b\",\"sat_g\":432.0,\"dry_g\":245.0}");
    CHECK(weightRefLookup(UID, &sat, &dry) && sat == 432.0f && dry == 245.0f);
    CHECK(weightRefSat(UID, &sat));
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-03b"));

    // malformed payloads must PRESERVE the cached full ref, not demote it
    ingest(UID, "{\"plant_id\":\"x\",\"sat_g\":400.0,\"dry_g\":\"oops\"}");
    ingest(UID, "{\"plant_id\":\"x\",\"sat_g\":400.0,\"dry_g\":null}");
    ingest(UID, "{\"plant_id\":\"x\",\"dry_g\":100.0}");               // dry without sat
    ingest(UID, "{\"plant_id\":\"x\",\"sat_g\":300.0,\"dry_g\":296.0}"); // span <= 5g
    CHECK(weightRefLookup(UID, &sat, &dry) && sat == 432.0f && dry == 245.0f);
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-03b"));

    // THE round-3 regression: a re-bind to a legally long (>20 char) name
    // arrives as name-only — it must OVERWRITE the full ref (demotion), with
    // the name truncated for display, never be dropped for its length
    ingest(UID, "{\"plant_id\":\"epiphyllum-oxypetalum-01\",\"name_only\":true}");
    CHECK(!weightRefLookup(UID, &sat, &dry));           // stale % is GONE
    CHECK(!weightRefSat(UID, &sat));                    // no anchor either
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "epiphyllum-oxypetalu"));

    // bad-charset name: demotion still ingests, name falls back to nothing
    ingest(UID, "{\"plant_id\":\"caçtus \\u00e9\",\"name_only\":true}");
    CHECK(weightRefPlant(UID) == nullptr);
    CHECK(!weightRefSat(UID, &sat));

    // provisional: absolute line only
    ingest(UID, "{\"plant_id\":\"cactus-05b\",\"sat_g\":380.0,\"provisional\":true}");
    CHECK(!weightRefLookup(UID, &sat, &dry));
    CHECK(weightRefSat(UID, &sat) && sat == 380.0f);
    CHECK(weightRefPlant(UID) && !strcmp(weightRefPlant(UID), "cactus-05b"));

    // empty retained payload = the broker's tombstone
    weightRefOnMessage(UID, (const uint8_t*)"", 0);
    CHECK(!weightRefSat(UID, &sat) && weightRefPlant(UID) == nullptr);

    // reconnect semantics: clearAll drops everything so the retained replay
    // (not stale RAM) decides what exists
    ingest(UID, "{\"plant_id\":\"cactus-09b\",\"name_only\":true}");
    ingest("11223344", "{\"plant_id\":\"cactus-21\",\"sat_g\":500.0,\"dry_g\":400.0}");
    weightRefClearAll();
    CHECK(weightRefPlant(UID) == nullptr);
    CHECK(!weightRefLookup("11223344", &sat, &dry));

    if (failures) { printf("%d FAILURE(S)\n", failures); return 1; }
    printf("pass weight-ref cache (tier matrix, long-name demotion, malformed "
           "preservation, tombstone, reconnect clear)\n");
    return 0;
}
