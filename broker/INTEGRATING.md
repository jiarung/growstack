# Publishing to this broker from another project

**Contract version 1 — 2026-10-06.** Changes to this contract are recorded in
[`docs/incidents/`](../docs/incidents/README.md) with the date; a publisher
written against version 1 keeps working until an entry there says otherwise.

This is the whole integration. If you are an agent working on a project that
wants its readings to land in this stack, this file is the contract. Two things
it cannot give you: the broker's LAN address (ask the operator), and the live
list of field names already in use (the command to fetch it is under rule 3).

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
the moment it first reports. The balcony dashboard's panels are pinned to the
balcony device, so your readings sit beside them in the database without
drawing on them.

Verify it landed (from the host running the stack):

```bash
cd broker
docker exec monitor-air-mqtt mosquitto_pub -t 'monitor-air/shed-01/telemetry' \
  -m '{"temp":23.4,"hum":48.2,"soil_vwc":18.5,"soil_temp":21.0}'
./influx-q.sh 'from(bucket:"sensors") |> range(start:-5m)
  |> filter(fn:(r)=> r._measurement=="air" and r.device=="shed-01")'
```

Four rows, one per field, each tagged `device=shed-01` and
`topic=monitor-air/shed-01/telemetry` (Telegraf adds the topic tag by
default). If you see them, you are done.

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
convention. Nothing on the broker side enforces either limit — Telegraf will
ingest any single topic segment — but this repo's firmware refuses ids outside
`[A-Za-z0-9_-]{1,30}`, and tooling here is written to that rule, so an id that
breaks it works today and fails the first time something assumes it. An id
containing `/` does not match `monitor-air/+/telemetry` at all and is silently
never stored.

**2. Every value is a JSON number. Strings and booleans vanish; `null` drops
that key; nesting flattens.** Measured against this Telegraf (2026-10-06):
`21` and `21.5` both land as float — the JSON parser converts every number,
so you do not have to write `21.0` (this repo's firmware does, and the
firmware's README says why; it is good discipline, not a requirement of this
path). A string or boolean value is dropped without error — `{"status":"ok"}`
stores nothing, `{"ok":true}` stores nothing — because the consumer is
configured with no string fields and no tag keys. `null` drops only that key;
the other keys in the message still land. A nested object or array is
flattened with underscores (`{"pm":{"25":7.0}}` becomes field `pm_25`,
`{"x":[3.0,4.0]}` becomes `x_0` and `x_1`), which is a field name you did
not choose.
Keep the object flat so the names are yours. A sensor that failed to read
**omits its key**: `0` and `-1` are numbers and will be charted.

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
`(device, field)` has been silent for more than 15 minutes and stayed that way
through a 2-minute confirmation (so about 17 minutes after the last sample).
The rule is a query over whatever is in `air`, so it covers a device from its
first sample on — it cannot watch a device that has never reported. That is
exactly what you want for a sensor that is supposed to be always on, and
exactly what you do not want for one that is not. A
bench board that runs only while someone is developing on it is on the
exclusion list for that reason (`grafana/provisioning/alerting/rules.yaml.tmpl`);
if yours is like that, say so and it goes on the list. An alert that always
fires is an alert nobody reads, and that kills the deadman for everyone.

**5. Not retained, QoS 0.** Telegraf consumes live. A retained message replays
on every reconnect and writes a stale reading with a fresh timestamp — the
chart shows a sensor that is alive when it is not. QoS 0 because a lost
telemetry sample is replaced by the next one 15 s later and nothing downstream
needs every sample; QoS 1 buys at-least-once delivery, which this path does
not need, at a PUBACK round-trip per message.

**6. `telemetry` is the only topic you publish to.** The others under
`monitor-air/` are control surfaces: `light/cmd` drives a mains plug,
`measure/*` and `ref/*` are the weigh station's two-way protocol, `spectrum`
has its own schema. Publishing there does things. [`FLOWS.md`](FLOWS.md) lists
every topic with what reads it; read that before touching any of them.

## What you get, and what you do not

You get: storage in `air` under your `device` tag, the deadman alert, a row
per field in the **Sensor freshness** table on the overview dashboard (that
table is deliberately per-device — it is where a silent sensor shows), and
the ability to query with `./influx-q.sh` or in Grafana's Explore view. The
`temp`/`hum` fields line up with the existing sensors' by name, so a panel
comparing two spots is one `group(columns: ["device"])` away.

You do not get a chart on the plant dashboards. The overview's temperature,
humidity, pressure, gas and light panels are the balcony's and filter
`device == "livingroom"`. A device you keep off the project's dashboards gets its own
file in `grafana/provisioning/dashboards/` — the provider watches the
directory, no restart — named `local-*.json`, **which the repo ignores**: a
dashboard for your own deployment is yours, not this project's, and stays
out of a public repository. One section per device,
every panel filtered to its own `device`; read [`QUERYING.md`](QUERYING.md)
first.

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
  It is not rejected — it is flattened into fields named `samples_0_temp`,
  `samples_1_temp`, … all stamped with one arrival time. One message per sample.
- **Sending a timestamp in the payload.** Telegraf stamps arrival time. A
  numeric timestamp becomes a float field named `timestamp` (measured); a
  string one is dropped. Neither sets the row's time.
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
block for `telemetry`), not what this page says it does. The behaviours in
rule 2 were measured by publishing each case to a throwaway device and reading
back what landed; do that again before trusting this page after a Telegraf
upgrade.
