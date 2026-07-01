#pragma once

#include "sensors.h"

// Configure the MQTT client and validate config. Does not connect (non-blocking).
void mqttSetup();

// Pump the client and run rate-limited, non-blocking reconnection. Call every
// loop iteration so keepalive/PINGREQ stays alive between publishes.
void mqttLoop();

// Drive the reflectance command/state machine (drain a queued reflect/cmd, advance a
// measurement). Call every loop iteration, after mqttLoop().
void reflectLoop();

// True when the client currently has a live broker connection.
bool mqttConnected();

// Publish one reading as on-contract JSON to monitor-air/<device>/telemetry
// (QoS 0, not retained). Returns true if the publish was accepted by the client.
bool mqttPublish(const SensorReading& r);

// Publish one ambient spectral reading as JSON to monitor-air/<device>/spectrum
// (QoS 0, not retained), tagged "mode":"ambient". No-op if the reading is invalid
// (AS7341 absent). Returns true if the publish was accepted by the client.
bool mqttPublishSpectrum(const SpectrumReading& s);
