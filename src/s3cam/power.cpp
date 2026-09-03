#include "power.h"

#include <Arduino.h>
#include <WiFi.h>

namespace power {
namespace {

// wifi_power_t counts QUARTER dBm (WIFI_POWER_19_5dBm == 78), so the enum and
// the human unit differ by 4x. Converting in one place keeps that off-by-four
// out of every call site.
constexpr int DBM_TO_ENUM = 4;

// The radio only accepts specific levels; offering the full integer range would
// invite silent rejection. These are the useful spread, strongest first.
constexpr wifi_power_t LEVELS[] = {
    WIFI_POWER_19_5dBm, WIFI_POWER_15dBm, WIFI_POWER_11dBm, WIFI_POWER_8_5dBm,
};

}  // namespace

bool setCpuMhz(int mhz) {
    // 80 is included but is NOT a free win: at QSXGA the JPEG encode and the
    // DVP drain share this clock, and starving them shows up as stalled or
    // torn frames rather than as an error. Offered, and labelled.
    if (mhz != 80 && mhz != 160 && mhz != 240) return false;
    if (!setCpuFrequencyMhz(mhz)) return false;
    return getCpuFrequencyMhz() == (uint32_t)mhz;
}

int cpuMhz() { return (int)getCpuFrequencyMhz(); }

bool setWifiTxDbm(int dbm) {
    for (wifi_power_t lvl : LEVELS) {
        // integer compare against the level's own dBm, derived from the enum
        // rather than repeated as a literal
        if ((int)lvl / DBM_TO_ENUM != dbm) continue;
        if (!WiFi.setTxPower(lvl)) return false;
        return WiFi.getTxPower() == lvl;
    }
    return false;
}

int wifiTxDbm() { return (int)WiFi.getTxPower() / DBM_TO_ENUM; }

}  // namespace power
