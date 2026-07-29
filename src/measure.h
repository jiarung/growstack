#pragma once

#include <Arduino.h>

// Plant measurement station: a PN532 NFC tag scan (software SPI) triggers a weight measurement.
// Runs its own non-blocking state machine beside the main loop — no I2C (the PN532 is on SPI).
//   IDLE → (tag scanned) → IDENTIFIED → STABILIZING (weight settles) → MEASURED → wait for tag removal.
// Phase 2a: on MEASURED it logs UID + grams (no MQTT/OLED yet — those are 2b/2c).
void measureBegin();
void measureLoop();

// Called by the MQTT dispatcher when a measure/ack arrives — bounded-copies the payload + flags it;
// the actual event_id match happens in measureLoop (keeps the MQTT callback tiny).
void measureOnAck(const uint8_t* payload, unsigned int len);
