# Publishing to this broker from another project

**Contract version 1 — 2026-10-06.** Changes to this contract are recorded in
[`docs/incidents/`](../docs/incidents/README.md) with the date; a publisher
written against version 1 keeps working until an entry there says otherwise.

This is the whole integration. If you are an agent working on a project that
wants its readings to land in this stack, you need this file and nothing else.

## The 30-second version

Publish a flat JSON object of floats, once every 15–60 s, to:

```
monitor-air/<device>/telemetry
```

```json
{"temp": 23.4, "hum": 48.2, "soil_vwc": 18.5, "soil_temp": 21.0}
```

That is it. Nothing in this repository has to change: Telegraf already
subscribes to `monitor-air/+/telemetry`, writes every message to the InfluxDB
measurement `air`, turns the topic's second segment into a `device` tag, and
stores each JSON key as one float field. The deadman alert covers your device
the moment it first reports. Queries and dashboards select on `device`, so
your readings sit beside the existing sensors without touching their panels.

Verify it landed (from the host running the stack):

```bash
cd broker
docker exec monitor-air-mqtt mosquitto_pub -t 'monitor-air/shed-01/telemetry' \
  -m '{"temp":23.4,"hum":48.2,"soil_vwc":18.5,"soil_temp":21.0}'
./influx-q.sh 'from(bucket:"sensors") |> range(start:-5m)
  |> filter(fn:(r)=> r._measurement=="air" and r.device=="shed-01")'
```

Four rows, one per field, `device=shed-01`. If you see them, you are done.

## The six rules, and why each one exists

Every rule below has broken something here at least once. The "why" is not
decoration: it is what tells you whether a situation you did not anticipate
falls under the rule or not.

**1. One device id per physical unit, `[A-Za-z0-9_-]`, 1–30 characters, and
not one that already exists.** The id is the ONLY thing that separates your
readings from everyone else's in the `air` measurement — there is no other
namespace. Reusing an existing id (`livingroom`, `s3cam-01`, `staging-01`,
`sim`) would interleave your series with theirs and the dashboards would show
both as one. Lowercase with a hyphen and a number (`shed-01`) matches the
convention; the character set is what the firmware here validates, so staying
inside it means your id survives any tooling written against that rule.

**2. Every value is a float. No ints, no strings, no nulls, no nesting.**
InfluxDB fixes a field's type on the first write. If your first `soil_temp` is the
integer `21` and the next is `21.5`, the second write fails — silently, for
that field, forever, until someone notices the series stopped. `21.0` every
time. A sensor that failed to read **omits its key**; `null` is not a float
and the whole message is dropped.

**3. Field names are lowercase `snake_case`, carry no unit, and never change.**
A renamed field is a new series: the old one goes silent, the deadman alert
fires on it, and every query that named it returns nothing. Units live in this
document, not in the name — `soil_vwc` is % by volume, `soil_temp` and `temp` are °C,
`hum` is %RH, `pressure` is hPa. If you measure the same quantity the existing
sensors do, **use the same name** (`temp`, `hum`): the `device` tag already
says whose it is, and `shed_temp` would be a third temperature field that no
existing comparison panel knows about.

Names already in use, so you can pick matching ones — environment: `temp`,
`hum`, `pressure`, `gas`, `lux`, `lux_ref`, `temp_sht`, `hum_sht`; device
health: `rssi`, `uptime_s`, `heap_free`, `psram_free`, `die_c`. Suggested for a soil
probe at a second spot: `soil_vwc`, `soil_temp`, `temp`, `hum`. Check the live list before choosing, it is the truth and this
paragraph is a copy:
`./influx-q.sh 'import "influxdata/influxdb/schema" schema.measurementFieldKeys(bucket:"sensors", measurement:"air")'`

**4. Publish at a steady cadence between 15 and 60 seconds, or tell us it is
intermittent.** The deadman alert pages the owner on Telegram when any
`(device, field)` has been silent for 15 minutes. That rule covers new devices
automatically — which is exactly what you want for a sensor that is supposed
to be always on, and exactly what you do not want for one that is not. A
bench board that runs only while someone is developing on it is on the
exclusion list for that reason (`grafana/provisioning/alerting/rules.yaml.tmpl`);
if yours is like that, say so and it goes on the list. An alert that always
fires is an alert nobody reads, and that kills the deadman for everyone.

**5. Not retained, QoS 0.** Telegraf consumes live. A retained message replays
on every reconnect and writes a stale reading with a fresh timestamp — the
chart shows a sensor that is alive when it is not. QoS 0 because a lost
telemetry sample is replaced by the next one 15 s later; QoS 1 only adds a
duplicate on reconnect.

**6. `telemetry` is the only topic you publish to.** The others under
`monitor-air/` are control surfaces: `light/cmd` drives a mains plug,
`measure/*` and `ref/*` are the weigh station's two-way protocol, `spectrum`
has its own schema. Publishing there does things. [`FLOWS.md`](FLOWS.md) lists
every topic with what reads it; read that before touching any of them.

## What you get, and what you do not

You get: storage in `air` under your `device` tag, the deadman alert, and the
ability to query with `./influx-q.sh` or in Grafana's Explore view. The
`temp`/`hum` fields line up with the existing sensors' by name, so a panel
comparing two spots is one `group(columns: ["device"])` away.

You do not get a dashboard. The existing panels filter on their own device
ids; yours will not appear on them. Add a `.json` to
`grafana/provisioning/dashboards/` — the provider watches the directory, no
restart — and read [`QUERYING.md`](QUERYING.md) first: it lists eight ways a
Flux query here has returned a confident wrong answer.

You do not get any control loop. The plant-light controller reads one named
device's `lux`; nothing reacts to `soil_vwc`. If something should, that
is a service of its own, not a change to a publisher.

## Things that look reasonable and are wrong

- **Prefixing field names with the location** (`shed_temp`). The device tag
  is the location. See rule 3.
- **Sending `0` or `-1` for a failed read.** It is a float, so it is stored,
  and it is now a data point on the chart. Omit the key.
- **Publishing a status or heartbeat field** (`"ok": 1.0`). Your presence IS
  the heartbeat — that is what the deadman is for. Device-health numbers that
  carry information (`rssi`, `uptime_s`, `heap_free`) are a different thing and
  `s3cam-01` does publish them; just know that each one is a field the deadman
  watches, and that `uptime_s` resetting to zero is how a reboot shows up.
- **Batching several readings into one message** (`{"samples": [...]}`).
  Nested. Rule 2. One message per sample.
- **Sending a timestamp in the payload.** Telegraf stamps arrival time. A
  payload timestamp becomes a float field named `timestamp`.
- **Using the hostname as the device id.** Hostnames change and often contain
  dots. Pick an id that names the unit's role.

## Network and trust

The broker listens on port 1883, plain MQTT, anonymous, on a **trusted LAN**.
Your publisher must be on that LAN. Do not expose 1883 beyond it, and do not
add authentication on the client side expecting the broker to check it — it
does not. The lockdown path (auth, TLS) is in [`README.md`](README.md#security-note)
and applies to every publisher at once when it happens.

Nothing in your project should contain this stack's InfluxDB token, Grafana
password, or any value from `broker/.env`. A publisher needs the broker's
address and nothing else.

## If this document and the stack disagree

The stack wins, and this document is wrong — file an incident. The contract
is what Telegraf does (`telegraf/telegraf.conf`, the `[[inputs.mqtt_consumer]]`
block for `telemetry`), not what this page says it does.
