#include "leaf_probe.h"

#include <Arduino.h>

namespace {
constexpr uint8_t LEAF_ADC_PIN = 8;   // ADC1_CH7; free on both boards (HX711=4/5, PN532=10-13, I2C=17/18)
constexpr uint8_t LEAF_LED_PIN = 7;   // 940nm LED via 150R (~14mA, within GPIO drive); same header side as 8
constexpr int     N_SAMPLES    = 64;
}

void leafProbeBegin() {
    pinMode(LEAF_LED_PIN, OUTPUT);
    digitalWrite(LEAF_LED_PIN, LOW);   // LED off until asked — it must never pollute ambient reads
}

void leafLedSet(bool on) {
    digitalWrite(LEAF_LED_PIN, on ? HIGH : LOW);
}

uint32_t leafProbeMilliVolts() {
    uint32_t v[N_SAMPLES];
    for (int i = 0; i < N_SAMPLES; i++) v[i] = analogReadMilliVolts(LEAF_ADC_PIN);
    // insertion sort — 64 elements, runs in ~µs, no allocation
    for (int i = 1; i < N_SAMPLES; i++) {
        uint32_t x = v[i];
        int j = i - 1;
        while (j >= 0 && v[j] > x) { v[j + 1] = v[j]; j--; }
        v[j + 1] = x;
    }
    return (v[N_SAMPLES / 2 - 1] + v[N_SAMPLES / 2]) / 2;
}
