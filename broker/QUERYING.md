# Querying the raw data

Every panel on the dashboards is a Flux query against InfluxDB, and anything a
panel shows you can ask for directly. This is the reference for doing that by
hand — when a chart looks wrong, when you want a number a panel does not show, or
when you want to check a claim instead of trusting it.

## Use the helper

```bash
cd ~/monitor-air/broker

./influx-q.sh 'from(bucket:"sensors")
  |> range(start:-3d)
  |> filter(fn:(r) => r._measurement=="plant_weight" and r._field=="weight_g")
  |> filter(fn:(r) => r.plant_id=="cactus-01")
  |> keep(columns:["_time","_value"])'
```

```
_time           _value
--------------  ------
08-05 16:19:04  250.4
08-05 16:24:08  252.2
08-06 12:28:15  243
08-07 07:51:45  245.5
08-07 07:56:03  274.7

5 rows   (times in Asia/Taipei)
```

Also takes a file (`./influx-q.sh -f q.flux`) or stdin (`echo '<flux>' | ./influx-q.sh`).
It converts timestamps to local time, drops the bookkeeping columns, and flattens
the multi-block CSV — see [the traps](#four-traps) for why each of those matters.

The raw form, if you want the unprocessed output:

```bash
docker exec monitor-air-influxdb influx query --org monitor-air '<flux>'
```

## The shape of a query

Almost every query is the same four steps:

```flux
from(bucket: "sensors")                                    // where
  |> range(start: -7d)                                     // when   — REQUIRED
  |> filter(fn: (r) => r._measurement == "plant_weight")   // which table
  |> filter(fn: (r) => r._field == "weight_g")             // which column
  |> keep(columns: ["_time", "_value"])                    // what to show
```

`range` is mandatory; without it the query errors. Absolute ranges work too:
`|> range(start: 2026-08-06T00:00:00Z, stop: 2026-08-08T00:00:00Z)` — note those
are **UTC**.

## When you have forgotten what exists

```bash
# which tables
./influx-q.sh 'import "influxdata/influxdb/schema"
schema.measurements(bucket:"sensors")'
```

→ `air`, `light`, `photone`, `plant_weight`, `spectrum`, `weather`

```bash
# which columns, and which tags you can filter on
./influx-q.sh 'import "influxdata/influxdb/schema"
schema.measurementFieldKeys(bucket:"sensors", measurement:"plant_weight")'

./influx-q.sh 'import "influxdata/influxdb/schema"
schema.measurementTagKeys(bucket:"sensors", measurement:"plant_weight")'
```

## Recipes

```flux
// latest reading per plant
  |> group(columns: ["plant_id"]) |> last() |> keep(columns: ["plant_id","_value"])

// one value per plant per LOCAL day (see trap 1 about the timezone import)
import "timezone"
option location = timezone.location(name: "Asia/Taipei")
  ...
  |> group(columns: ["plant_id"])
  |> aggregateWindow(every: 1d, fn: min, createEmpty: false)

// change between consecutive readings — how a watering shows up
  |> group(columns: ["plant_id"]) |> difference(nonNegative: false)

// how many points arrived per day — is a pipeline still alive?
  |> aggregateWindow(every: 1d, fn: count, createEmpty: true)
```

## Four traps

Each of these has already produced a wrong conclusion in this project.

**1. Timestamps are UTC, and `option location` does not change that.** The import
only affects functions that reason about calendar time — `aggregateWindow`'s day
boundaries, `date.hour()`. Output is still UTC, so `08-06T23:51Z` is **08-07 07:51
Taipei**. `influx-q.sh` converts for you; raw `influx query` does not.

Getting this wrong in a panel is silent: a `1d` window without the import splits at
UTC midnight, i.e. 08:00 local, straight through a working day. The chart renders
perfectly and every number is misfiled.

**2. `group()` across mixed types fails.** `plant_weight` holds `weight_g` (float)
and `uid` (string). Grouping and aggregating without filtering `_field` first
returns nothing — which reads exactly like "there is no data". This produced a
false "the afternoon's records are gone" alarm.

**3. `keep()` after `yield()` does nothing.** `yield` ends the pipeline, so the
projection applies to a table nobody receives and the leaky one is returned. Put
`keep` before `yield`.

**4. `--raw` CSV can contain several header blocks.** Groups with different columns
each emit their own `#datatype`/header preamble. A parser that reads the first
header and then treats every later line as data will silently drop everything after
the first block — this is how a "there are zero reflectance measurements"
conclusion happened when there were 202. Reset the header whenever a line starts
with `#`.

## Related

- [`FLOWS.md`](FLOWS.md) — what each measurement is, its tags, and whether the
  pipeline feeding it is currently alive.
- [`MAINTENANCE.md`](MAINTENANCE.md) — the *write* side of the same coin: a
  `docker exec` missing `-i` stores nothing and exits 0, and an `influx delete`
  predicate that matches nothing does too.
- [`README.md`](README.md#data-contract-the-firmware-must-follow-this) — the
  telemetry contract the firmware writes against.
