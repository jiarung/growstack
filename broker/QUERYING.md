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
the multi-block CSV — see [the traps](#six-traps) for why each of those matters.

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

## Six traps

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

**5. Raw AS7341 counts are not a spectrum.** The eight channels differ by ~25x in
irradiance responsivity (`R` = 55, 110, 210, 390, 590, 840, 1350, 1070 for
f415..f680), so counts alone badly overweight yellow and red. Divide by `R` before
reading any shape out of them — the PPFD panels and `kmodels.py` already do.

The size of the error is not subtle. The same lamp, same samples:

| | 415–480 blue | 515–555 green | 590–680 red |
|---|---:|---:|---:|
| raw counts | 11.0% | 18.1% | **70.9%** |
| ÷ R | **49.4%** | 18.2% | 32.3% |

Raw says a red lamp. Normalised says a blue+red horticultural lamp with a green
notch — a different object, and only the second one explains why `lux/54`
underestimates it. This trap produced a wrong conclusion twice inside one session
on 2026-09-07, the second time *after* writing down that the channels differ 25x.

**6. `light_context.source == "daylight"` is not sunlight.** At this site it occurs
only between 05:00 and 07:00 Taipei, with a median of **54 lux** — dawn twilight
near the sensor's noise floor. The lamp window opens at 08:00 and the controller
does not switch off in bright sun
([`FLOWS.md` gap 8](FLOWS.md#known-gaps)), so every genuinely sunlit moment is
classified `mixed`, and the daylight bucket keeps only the dark edge of the day.

Median lux by cell, seven days: `daylight 54 · lamp 4145 · mixed 4394 · none 0`.
Anything averaged over daylight cells is therefore an average over twilight, and it
will look like a statement about sunlight. Check the hour distribution and the lux
level before drawing a conclusion from that bucket.

## Related

- [`FLOWS.md`](FLOWS.md) — what each measurement is, its tags, and whether the
  pipeline feeding it is currently alive.
- [`MAINTENANCE.md`](MAINTENANCE.md) — the *write* side of the same coin: a
  `docker exec` missing `-i` stores nothing and exits 0, and an `influx delete`
  predicate that matches nothing does too.
- [`README.md`](README.md#data-contract-the-firmware-must-follow-this) — the
  telemetry contract the firmware writes against.
