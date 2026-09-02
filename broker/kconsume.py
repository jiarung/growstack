#!/usr/bin/env python3
"""Canonical consumer join, pure reference implementation (Phase D of
docs/photone-cal-pipeline.md).

THE join order is fixed by the blueprint and not negotiable:
  (1) context_time = truncate(_time, 5m) UTC
  (2) location     = station-map(device, _time)
  (3) join light_context on {location, context_time} -> (source, regime,
      epoch_<target>)
  (4) join k_adopted on the four tags -> the adopted value
  (5) multiply at the RAW _time, and only then aggregate / integrate DLI
      (swapping (5) with aggregation changes DLI and lamp-decision numbers)

Fallbacks multiply the per-target SEED constant and carry an `uncorrected`
flag: unknown | mixed | out-of-matrix (context says none/lamp for a target
whose legal matrix has no such bucket) | seed (the bucket exists but has never
earned a real adoption — numerically identical to uncorrected, the flag lets a
panel tell "being calibrated" from "not calibrated yet").

Grafana panels re-express this join in Flux (k-migration dashboard); THIS file
is the contract they mirror, frozen by the fixture's corrected.json.

    ./kconsume.py --selftest
"""
import sys
from datetime import datetime, timezone

from kadopt import SEEDS
from kmodels import UNIVERSE, floor_cell, station_location

UTC = timezone.utc

# which light_context epoch field each target reads (blueprint schema)
EPOCH_FIELD = {"bh1750_lux_main": "epoch_lux_main",
               "bh1750_lux_ref": "epoch_lux_ref",
               "as7341_ppfd": "epoch_as7341"}

# the blueprint's per-consumer bucket-selection table
CONSUMERS = {
    "lux_main":  ("bh1750_lux_main", "air.json DLI-lux / daily.json 總 lux / canary 分母"),
    "lux_ref":   ("bh1750_lux_ref", "daily.json 遮燈 DLI"),
    "spec_ppfd": ("as7341_ppfd", "air.json Spectrum PPFD(×S)"),
}


def adopted_lookup(rows):
    """Post-round adopted rows -> {(target, source, regime, epoch): row}."""
    return {(r["target"], r["source"], r["regime"], r["epoch"]): r for r in rows}


def cells_lookup(cells_by_loc):
    """light_context cells -> {(location, cell_ts): cell}."""
    out = {}
    for loc, cells in cells_by_loc.items():
        for c in cells:
            out[(loc, c["ts"])] = c
    return out


def correct_sample(ts, value, target, device, station_map, cells, adopted):
    """One raw sample -> (corrected_value, k, flag|None). Pure."""
    seed = SEEDS[target]
    loc = station_location(station_map, device, ts)
    cell = cells.get((loc, floor_cell(ts))) if loc else None
    if cell is None or cell["source"] == "unknown":
        return value * seed, seed, "unknown"
    if cell["source"] == "mixed":
        return value * seed, seed, "mixed"       # v1 is honest about mixed light
    if (cell["source"], cell["regime"]) not in UNIVERSE[target]:
        return value * seed, seed, "out-of-matrix"
    key = (target, cell["source"], cell["regime"], cell[EPOCH_FIELD[target]])
    row = adopted.get(key)
    if row is None:
        # a legal-matrix bucket always has a k_adopted row (Phase C invariant);
        # reaching here means the context and the adoption set disagree — treat
        # as unknown rather than invent a value
        return value * seed, seed, "unknown"
    k = row["value"]
    if row["adoption_state"] == "seed":
        return value * k, k, "seed"
    return value * k, k, None


def correct_series(samples, target, device, station_map, cells, adopted):
    """[(ts, value)] -> [{ts, raw, k, corrected, flag}] — step (5) happens on
    each raw timestamp; the caller aggregates afterwards, never before."""
    out = []
    for ts, v in samples:
        corrected, k, flag = correct_sample(ts, v, target, device, station_map,
                                            cells, adopted)
        out.append({"ts": ts, "raw": v, "k": k, "corrected": corrected,
                    "flag": flag})
    return out


def integral_lux_hours(series, key):
    """Trapezoidal area between consecutive samples, NO extension past the last
    one — the same rule as Flux `integral(unit: 1s)` (linear interpolation
    between points, nothing beyond the data), so the fixture's frozen integrals
    are a faithful oracle for the dashboard's daily-integral gate. Assumes
    samples sorted by ts."""
    total = 0.0
    for a, b in zip(series, series[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        total += (a[key] + b[key]) / 2.0 * dt / 3600.0
    return total


# ---------------------------------------------------------------- selftest

def selftest():
    T = datetime(2026, 9, 9, 9, 32, 10, tzinfo=UTC)     # NOT on the 5-min grid
    smap = [{"device": "livingroom", "light_location": "livingroom",
             "valid_from": "1970-01-01T00:00:00Z"}]
    cell_ts = datetime(2026, 9, 9, 9, 30, tzinfo=UTC)

    def cell(source, regime, **ep):
        c = {"ts": cell_ts, "source": source, "regime": regime,
             "epoch_lux_main": "e0-legacy", "epoch_lux_ref": "bh1750_lux_ref-e2",
             "epoch_as7341": "as7341_ppfd-e2"}
        c.update(ep)
        return {("livingroom", cell_ts): c}

    adopted = adopted_lookup([
        {"target": "bh1750_lux_main", "source": "daylight", "regime": "diffuse",
         "epoch": "e0-legacy", "value": 1.09, "adoption_state": "adopted"},
        {"target": "bh1750_lux_main", "source": "daylight", "regime": "direct",
         "epoch": "e0-legacy", "value": 1.0, "adoption_state": "seed"},
        {"target": "bh1750_lux_ref", "source": "daylight", "regime": "diffuse",
         "epoch": "bh1750_lux_ref-e2", "value": 1.2, "adoption_state": "adopted"},
    ])

    # normal correction: non-grid ts truncates into its cell, k applies at raw ts
    c, k, f = correct_sample(T, 1000.0, "bh1750_lux_main", "livingroom", smap,
                             cell("daylight", "diffuse"), adopted)
    assert (c, k, f) == (1090.0, 1.09, None)
    # a bucket that never earned adoption: seed value, flagged "seed"
    c, k, f = correct_sample(T, 1000.0, "bh1750_lux_main", "livingroom", smap,
                             cell("daylight", "direct"), adopted)
    assert (c, k, f) == (1000.0, 1.0, "seed")
    # unknown context / missing cell -> seed + unknown
    for cs in (cell("unknown", "unknown"), {}):
        c, k, f = correct_sample(T, 1000.0, "bh1750_lux_main", "livingroom",
                                 smap, cs, adopted)
        assert (c, k, f) == (1000.0, 1.0, "unknown")
    # mixed: honest non-correction with its own flag
    c, k, f = correct_sample(T, 1000.0, "bh1750_lux_main", "livingroom", smap,
                             cell("mixed", "none"), adopted)
    assert f == "mixed" and c == 1000.0
    # out-of-matrix: the ref target has no lamp bucket; "none" cells match nothing
    for src, reg in (("lamp", "none"), ("none", "none")):
        c, k, f = correct_sample(T, 1000.0, "bh1750_lux_ref", "livingroom",
                                 smap, cell(src, reg), adopted)
        assert f == "out-of-matrix" and c == 1000.0, (src, reg)
    # ...but the ref target in a daylight cell uses ITS epoch field (e2)
    c, k, f = correct_sample(T, 1000.0, "bh1750_lux_ref", "livingroom", smap,
                             cell("daylight", "diffuse"), adopted)
    assert (c, k, f) == (1200.0, 1.2, None)
    # as7341 seed is the CAL constant — the fallback multiply is NOT 1.0
    c, k, f = correct_sample(T, 17000.0, "as7341_ppfd", "livingroom", smap,
                             cell("unknown", "unknown"), adopted)
    assert k == 0.0017469 and f == "unknown" and abs(c - 29.6973) < 1e-4

    # multiply-then-integrate: with a uniform k the integrals scale exactly by k
    from datetime import timedelta
    s1 = [(cell_ts + timedelta(seconds=15 * i), 1000.0 + i) for i in range(4)]
    series = correct_series(s1, "bh1750_lux_main", "livingroom", smap,
                            cell("daylight", "diffuse"), adopted)
    ih_raw = integral_lux_hours(series, "raw")
    ih_cor = integral_lux_hours(series, "corrected")
    assert abs(ih_cor / ih_raw - 1.09) < 1e-9, "uniform k: integrals scale by k"

    print("kconsume selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("pure library — run with --selftest", file=sys.stderr)
        sys.exit(1)
