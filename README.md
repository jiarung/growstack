# monitor-air

A self-hosted rig for keeping cacti alive on purpose rather than by luck: ESP32-S3
nodes measure the environment, the light the plants actually receive, and the
plants themselves; a Docker stack stores it, charts it, drives the grow lamp, and
shouts when something goes quiet.

**Daytime — natural light**

![Daytime: balcony cam + Grafana dashboard](docs/media/demo-daytime.gif)

**Evening — grow light on**

![Evening: grow light on, balcony cam + Grafana dashboard](docs/media/demo-growlight.gif)

## What it does

- **Environment** — temperature, humidity, pressure, gas (VOC proxy), every 15 s.
- **Light, two ways** — a BH1750 at the plant and a shielded reference beside it,
  so the grow lamp's contribution can be separated from daylight; plus an AS7341
  for spectral composition and a PPFD estimate.
- **Closed-loop grow lamp** — a controller reads lux, drives a Tapo P110M, and in
  the evening keeps supplementing until the day reaches a target DLI.
- **Plant weight** — tap an NFC tag on a pot, the station weighs it and the server
  resolves the tag to a plant id. Weight over time is the water/growth signal.
- **Leaf reflectance** — on-demand spectral reads per plant, intended to feed
  vegetation indices (NDVI / NDRE / CIre). *Blocked: see status below.*
- **It tells you when it breaks** — deadman alerts to Telegram for silent sensors
  and for the weather feed, plus a dashboard panel that makes a stalled pipeline
  visible.

## Layout

| Where | What |
|---|---|
| `src/`, `platformio.ini` | ESP32-S3 firmware — one project, every node |
| `broker/` | the whole server side, Docker Compose |
| `docs/` | firmware manual + media |

## Where to go

**Start here for the system as a whole:**

- [`broker/FLOWS.md`](broker/FLOWS.md) — **every data path, and whether it is
  actually running right now.** One diagram, a `Live?` column, and the commands to
  re-check it yourself. If you read one file, read this one; it is deliberately the
  only place the whole picture is drawn, so it cannot drift out of sync with three
  copies of the same diagram.

- [`docs/incidents/`](docs/incidents/README.md) — **every failure this rig has had, dated,
  with its evidence.** One file per month behind a single index. The other documents keep
  the narrative ("why the lamp must not fail off"); the dates and numbers live here, because
  they used to live in three places at once and one of them had already drifted.

**By subsystem:**

| Doc | Covers |
|---|---|
| [`docs/FIRMWARE.md`](docs/FIRMWARE.md) | hardware, wiring, build/flash (USB + OTA), secrets, what each topic carries |
| [`broker/README.md`](broker/README.md) | server stack: setup, operation, data contract, plant-light control, backups, security |
| [`broker/node-red/README.md`](broker/node-red/README.md) | the phone control surface and the weigh-station flow |
| [`broker/MEASUREMENT-STATION.md`](broker/MEASUREMENT-STATION.md) | weigh-station contract: MQTT topics, ack/dedup rules, tag→plant map |
| [`broker/QUERYING.md`](broker/QUERYING.md) | asking InfluxDB directly — query shapes, schema discovery, and the four traps that have each caused a wrong conclusion |
| [`broker/MAINTENANCE.md`](broker/MAINTENANCE.md) | keeping it running: the failure classes that recur here, what a change needs redeployed, routine checks, and the repot/retire procedure |

**Calibration and analysis** — these form one chain, in order:

| Doc | Question it answers | State |
|---|---|---|
| [`docs/photone-cal-pipeline.md`](docs/photone-cal-pipeline.md) | **the contract** — bucket universe, estimator spec, epoch registry, adoption brake | authoritative; cited by `kmodels.py`, `kadopt.py`, `kconsume.py`, `mark-epoch.sh`, `analyze-photone.sh` |
| [`broker/PPFD-CALIBRATION.md`](broker/PPFD-CALIBRATION.md) | is the PPFD scale real µmol·m⁻²·s⁻¹? how is `CAL` measured? | daylight calibrated; the lamp case unresolved; the current `CAL` is declared stale |
| [`broker/PPFD-CAL-ROUTINE-PLAN.md`](broker/PPFD-CAL-ROUTINE-PLAN.md) | the routine that actually runs daily, and when a `CAL` may be adopted | live — `ppfd-cal-daily.sh` + `cal-review-reminder.sh` |
| [`broker/PPFD-CAL-DAILY-PLAN.md`](broker/PPFD-CAL-DAILY-PLAN.md) | — | **superseded 2026-08-17** by the row above; kept only for its reasoning |
| [`broker/PHOTONE-CAL-PLAN.md`](broker/PHOTONE-CAL-PLAN.md) | design of `record-photone.sh`, the ground-truth recorder | built |
| [`broker/VEGETATION-INDICES-PLAN.md`](broker/VEGETATION-INDICES-PLAN.md) | turn reflectance into a plant-health metric | blocked on sensor aim |
| [`broker/SENSOR-DRIFT-DESIGN.md`](broker/SENSOR-DRIFT-DESIGN.md) | make every sensor liveness- and drift-checkable | design only, not built |

Six documents on one subject, so: the **blueprint** is the contract and wins any
disagreement; `PPFD-CALIBRATION.md` owns how `CAL` is measured; `PPFD-CAL-ROUTINE-PLAN.md`
owns what runs today. Individual failures belong in
[`docs/incidents/`](docs/incidents/README.md), not in any of them.

## Status

| Subsystem | State |
|---|---|
| Environment + light telemetry | running |
| Grow-lamp control, DLI-target evening top-up | running |
| Weigh station (NFC → weight → plant id) | running; 16 plants registered |
| Deadman alerts (sensors, weather feed) | running |
| Nightly InfluxDB backup to HDD | running |
| Leaf reflectance → vegetation indices | **blocked** — the AS7263 does not aim at the leaf, and the bands NDRE/CIre need are not on the AS7341 |
| Absolute PPFD under the grow lamp | **open** — daylight is calibrated, the lamp is not |

Known gaps, each with the evidence behind it, live at the bottom of
[`broker/FLOWS.md`](broker/FLOWS.md#known-gaps).

## Quick start

```bash
# server side
cd broker && cp .env.example .env    # then edit: passwords + a strong influx token
docker compose up -d
#   Grafana -> http://<host>:3001   (two dashboards: overview, and daily)
#   no hardware yet? docker compose --profile sim up -d sim   # synthetic data

# firmware
cp src/secrets.h.example src/secrets.h   # then edit WiFi / MQTT / OTA
pio run -t upload --upload-port /dev/cu.usbmodem2101
```

Details in [`broker/README.md`](broker/README.md) and
[`docs/FIRMWARE.md`](docs/FIRMWARE.md).

## Security

This targets a **trusted LAN**: the MQTT broker is anonymous-open, Grafana is
plain HTTP, and Node-RED's editor has no auth. Before exposing anything, enable
MQTT auth, put Grafana behind TLS, and rotate the InfluxDB token — the lockdown
steps are in [`broker/README.md`](broker/README.md#security-note). Real
credentials live only in `src/secrets.h` and `broker/.env`, both gitignored.
