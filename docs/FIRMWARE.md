# Firmware — ESP32-S3 sensor node / measurement station

One PlatformIO project builds every node. What a given board *does* is decided by
which peripherals are wired to it and by `PUBLISH_AMBIENT_SPECTRUM` in
`src/secrets.h` — not by a different firmware. A board with only BME680 + BH1750
is an environment node; add the load cell, NFC reader and OLED and the same
binary is a weigh station.

Server side: [`../broker/README.md`](../broker/README.md). What talks to what, and
whether it is live: [`../broker/FLOWS.md`](../broker/FLOWS.md).

## Hardware

| Item | Detail | Bus / pins |
|---|---|---|
| Board | ESP32-S3-WROOM-1 **N16R8** (16 MB flash + 8 MB OPI PSRAM) | — |
| Environment | **BME680** temp / humidity / pressure / gas | I²C `0x77`, falls back to `0x76` |
| Light (plant) | **BH1750** at the plant, lit by the grow lamp → `lux` | I²C `0x23` (ADDR floating/low) |
| Light (reference) | **BH1750** ~10 cm away, shielded from the lamp → `lux_ref` | I²C `0x5C` (ADDR tied to 3V3) |
| Visible spectrum | **AS7341** 8-channel (415–680 nm) + clear + NIR | I²C |
| NIR spectrum | **AS7263** 6-band (610/680/730/760/810/860 nm) | I²C |
| Weight | **HX711** load-cell ADC | GPIO — `DT = 4`, `SCK = 5` (not I²C) |
| Tag reader | **PN532** NFC | I²C |
| Display | **SSD1306** OLED | I²C |

Everything except the HX711 shares one I²C bus (`Wire`, `SDA = GPIO17`,
`SCL = GPIO18`); the addresses do not collide.

**Why two BH1750s.** `lux` sits where the plant is and therefore sees the grow
lamp; `lux_ref` sits beside it but shielded, so it sees ambient only.
`lux - lux_ref` is the lamp's own contribution — the only way this rig can
separate artificial from natural light, since the lamp covers the whole daylight
window.

### Wiring

```
Sensor VCC  -> 3V3          HX711 DT   -> GPIO4
Sensor GND  -> GND          HX711 SCK  -> GPIO5
Sensor SDA  -> GPIO17
Sensor SCL  -> GPIO18
```

## What it publishes

Every topic is `monitor-air/<MQTT_DEVICE_ID>/...`. The full contract lives in
[`../broker/README.md`](../broker/README.md#data-contract-the-firmware-must-follow-this)
and, for the weigh station, in
[`../broker/MEASUREMENT-STATION.md`](../broker/MEASUREMENT-STATION.md).

| Topic | When | Payload |
|---|---|---|
| `telemetry` | every 15 s | flat float JSON: `temp`, `hum`, `pressure`, `gas`, `lux`, `lux_ref` |
| `spectrum` | every publish (if `PUBLISH_AMBIENT_SPECTRUM 1`) | AS7341 counts, `mode:"ambient"` |
| `reflect/cmd` | subscribed | server asks for a leaf reflectance read |
| `reflect/state` \| `result` \| `availability` | on change / on completion | measurement lifecycle |
| `measure/event_raw` | on an NFC tap | `{event_id, uid, weight_g}`, retried up to 5× |
| `measure/ack` | subscribed | server echoes `event_id`; this is what stops the retries |

**Fail-open telemetry:** a sensor that fails to read omits its field rather than
blocking the message — an unwired BH1750 just drops `lux`.

## Toolchain

PlatformIO with the Arduino framework for Espressif32. Two environments:
`esp32-s3-devkitc-1` (USB) and `ota` (WiFi), the latter extending the former.

## Getting started

```bash
# 1. Config (secrets.h is gitignored)
cp src/secrets.h.example src/secrets.h
#    edit: WIFI_SSID / WIFI_PASSWORD, MQTT_HOST (broker LAN IP),
#    MQTT_DEVICE_ID (this unit's name), OTA_PASSWORD

# 2. Build
pio run

# 3. Flash over USB (adjust the port)
pio run -t upload --upload-port /dev/cu.usbmodem2101

# 4. Serial monitor (115200 baud)
pio device monitor --port /dev/cu.usbmodem2101 --baud 115200
```

Expected log after a reset:

```
[boot] monitor-air starting
[sensors] BME680 ok @ 0x77
[sensors] BH1750 ok @ 0x23
[mqtt] topic=monitor-air/sensor-01/telemetry
[wifi] connecting to <ssid> ...
[mqtt] connected as monitor-air-sensor-01 -> 192.168.x.x:1883
[mqtt] published monitor-air/sensor-01/telemetry {"temp":24.8,...,"lux":350.0}
```

> This board uses native USB-Serial/JTAG, which does **not** auto-reset when the
> monitor connects — press **RST/EN** with the monitor open to see the boot log.

### Flashing over WiFi (OTA)

The board must already be running an OTA-capable build, so the first flash is
always over USB.

```bash
export OTA_PASS='<the OTA_PASSWORD from secrets.h>'
pio run -e ota -t upload --upload-port 192.168.50.XX
```

Two traps, both baked into `platformio.ini` as comments:

- **Always pass `--upload-port <device-ip>`.** Without it PlatformIO auto-detects
  the USB serial port and espota tries to invite `/dev/cu.*` → `Host Not Found`.
  mDNS is unreliable on this AP, so use the IP — it is printed in the boot log.
- **Run the env alone (`-e ota`).** A bare `pio run -t upload` also USB-flashes
  the other environment.

## Secrets

`src/secrets.h` is gitignored and never committed; `src/secrets.h.example` is the
template. `MQTT_DEVICE_ID` must be `[A-Za-z0-9_-]` (no spaces, `+` or `#`) — it
becomes a topic segment and the InfluxDB `device` tag, and is validated at boot.
Flash the same firmware to several units and change only this value.

`OTA_PASSWORD` is **required**: `main.cpp` calls `ArduinoOTA.setPassword()`
unconditionally, so a `secrets.h` without it will not compile.

ESP32-S3 supports **2.4 GHz only** — a 5 GHz SSID will never connect.

## Flash mode note

This unit ships with an off-brand flash chip (manufacturer `0x46`) that does not
boot in DIO/QIO mode — it fails with `Invalid image block, can't boot`. The fix is
already in `platformio.ini`:

```ini
board_build.flash_mode = dout   ; header mode byte -> DOUT
board_build.boot       = dio    ; only qio/dio/opi bootloaders ship prebuilt
```

On a board with a mainstream flash chip you can switch back to `qio` for faster
execution. Diagnose with `esptool flash_id` (check the manufacturer ID) if a fresh
board won't boot.

## Adding libraries

PlatformIO does not auto-download a header just because you `#include` it —
declare it in `lib_deps` first, or you get `No such file or directory`:

```bash
# 1. add the package to lib_deps in platformio.ini
# 2. build (downloads the dep)
pio run
# 3. refresh the clang index so editor highlighting works
pio run -t compiledb
```

Current dependencies: `claws/BH1750`, `adafruit/Adafruit BME680 Library`,
`adafruit/Adafruit AS7341`, `sparkfun/SparkFun AS726X`, `bogde/HX711`,
`adafruit/Adafruit PN532`, `adafruit/Adafruit SSD1306` (+ GFX),
`knolleary/PubSubClient`, `bblanchon/ArduinoJson`.

## Source layout

```
src/
├── main.cpp          # non-blocking orchestration, WiFi/OTA, publish schedule
├── sensors.h/.cpp    # BME680 + 2×BH1750 + AS7341 + AS7263 -> reading structs
├── measure.h/.cpp    # weigh station: NFC tap -> HX711 -> OLED -> event/ack/retry
├── mqtt_client.h/.cpp# PubSubClient wrapper: connect, publish, subscribe
├── log.h/.cpp        # serial logging
├── secrets.h         # WiFi / MQTT / OTA config (gitignored)
└── secrets.h.example # template
```

## Known limitations

- **BME680 temperature reads high** — the gas-sensor heater self-heats the die, so
  readings run ~3–5 °C above ambient. No offset compensation yet.
- **Absolute PPFD is not trustworthy yet** — the spectrum→PPFD constant is
  calibrated for daylight; the lamp case is unresolved. See
  [`../broker/PPFD-CALIBRATION.md`](../broker/PPFD-CALIBRATION.md).
- **The AS7263 does not currently aim at the leaf**, so the vegetation indices it
  is meant to feed cannot be computed. See
  [`../broker/VEGETATION-INDICES-PLAN.md`](../broker/VEGETATION-INDICES-PLAN.md).
- **`event_id` is session-scoped** — it restarts at 1 after a reboot, so it is not
  a long-term primary key. The server dedups on `(device, event_id)` within a
  60 s window.
- **WiFi is 2.4 GHz only.**
