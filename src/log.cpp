#include "log.h"

#include <stdarg.h>
#include <stdio.h>
#include <time.h>

namespace {

// Taiwan is UTC+8, no daylight saving.
constexpr long GMT_OFFSET_SEC = 8 * 3600;
constexpr int  DST_OFFSET_SEC = 0;

// Epoch seconds at 2021-01-01. Before SNTP syncs, the clock sits near 1970, so
// anything below this means we don't have real wall-clock time yet.
constexpr time_t SYNC_THRESHOLD = 1609459200;

// Real wall-clock once NTP has synced, otherwise uptime since boot.
void formatStamp(char* buf, size_t cap) {
    time_t now = time(nullptr);
    if (now >= SYNC_THRESHOLD) {
        struct tm tm;
        localtime_r(&now, &tm);
        strftime(buf, cap, "%Y-%m-%d %H:%M:%S", &tm);
    } else {
        uint32_t ms = millis();
        snprintf(buf, cap, "up %lu.%03lus",
                 (unsigned long)(ms / 1000), (unsigned long)(ms % 1000));
    }
}

void emitPrefix() {
    char stamp[32];
    formatStamp(stamp, sizeof(stamp));
    Serial.printf("[%s] ", stamp);
}

}  // namespace

void logTimeBegin() {
    configTime(GMT_OFFSET_SEC, DST_OFFSET_SEC, "pool.ntp.org", "time.google.com");
}

// Same threshold formatStamp() decides on, deliberately not a second copy of
// the number: if "synced" ever means two different instants, the log prefix and
// the OLED age would disagree about whether the clock is real.
bool logTimeSynced() { return time(nullptr) >= SYNC_THRESHOLD; }

void logf(const char* fmt, ...) {
    emitPrefix();
    // Roomy enough for the longest line (mqtt publish: topic + ~200B payload).
    char buf[320];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    Serial.print(buf);
}

void logln(const char* msg) {
    emitPrefix();
    Serial.println(msg);
}
