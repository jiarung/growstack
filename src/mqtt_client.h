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

// True while a reflectance measurement is in progress — the caller must NOT read the
// AS7341 (ambient spectrum) meanwhile, since both use the same sensor.
bool reflectBusy();

// True when the client currently has a live broker connection.
bool mqttConnected();

// Publish one reading as on-contract JSON to monitor-air/<device>/telemetry
// (QoS 0, not retained). Returns true if the publish was accepted by the client.
bool mqttPublish(const SensorReading& r);

// Publish one ambient spectral reading as JSON to monitor-air/<device>/spectrum
// (QoS 0, not retained), tagged "mode":"ambient". No-op if the reading is invalid
// (AS7341 absent). Returns true if the publish was accepted by the client.
bool mqttPublishSpectrum(const SpectrumReading& s);

// Publish a measurement-station event (already-built JSON) to monitor-air/<device>/measure/event_raw
// (QoS 0, not retained). Returns true if accepted by the client. The measure module holds a pending
// copy + retries until it sees a matching measure/ack.
bool mqttPublishMeasureEvent(const char* json);

// Publish a live sensor-health snapshot to monitor-air/<device>/health (QoS 0, not retained):
// per-sensor presence as 1.0/0.0 floats + i2c_n + spectrum_read_ms (omitted if NaN) + reset reason,
// so the monitoring host can alert on a silent sensor dropout. Returns true if accepted.
bool mqttPublishHealth(const SensorHealth& h, bool pn532, const char* resetReason);
