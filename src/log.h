#pragma once

#include <Arduino.h>

// Arm SNTP so the system clock syncs in the background once WiFi is up. Safe to
// call before the network is connected — the actual sync happens whenever it
// becomes reachable. Until then, logs fall back to uptime.
void logTimeBegin();

// True once SNTP has actually delivered a real wall clock. Before that the
// system clock sits near 1970, so any age computed from it would be decades
// wrong while looking perfectly plausible — callers that subtract timestamps
// (the OLED's "how long since watering") must ask this first and simply omit
// the age when it is false, rather than print a confident lie.
bool logTimeSynced();

// printf-style log, prefixed with a timestamp. Caller supplies the trailing
// '\n' (mirrors Serial.printf), so swapping an existing call site is a rename.
void logf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));

// println-style log, prefixed with a timestamp and terminated with a newline.
void logln(const char* msg);
