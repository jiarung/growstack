#!/usr/bin/env python3
"""k-model pipeline orchestrator (Phase B): I/O around the pure core kmodels.py.

Two modes, ONE computation path:

  --fixture <dir>   fully offline: read line-protocol + registry files from the
                    dir, write {k_model, light_context}.json artifacts next to
                    them (out/), diff against <dir>/expected/ -> exit 0/1.
                    --write-expected refreshes expected/ instead of diffing.
  (no --fixture)    live: query InfluxDB (docker exec, like record-photone.sh),
                    write k_model + light_context points back to Influx FIRST,
                    then publish retained MQTT per bucket (idempotent; a failed
                    MQTT publish heals on the next round).

Phase B publishes the {"model": ...} node only — the adoption engine (Phase C)
adds the "adopted" node to the same topics.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import kadopt   # noqa: E402
import kmodels  # noqa: E402
from kmodels import UTC, rfc3339, parse_rfc3339  # noqa: E402

_sn_spec = importlib.util.spec_from_file_location("solarnoon", os.path.join(DIR, "solar-noon.py"))
solarnoon = importlib.util.module_from_spec(_sn_spec)
_sn_spec.loader.exec_module(solarnoon)

EPOCH_FIELD_KEYS = {"bh1750_lux_main": "lux_main",
                    "bh1750_lux_ref": "lux_ref",
                    "as7341_ppfd": "as7341"}


def die(m):
    print(m, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- line protocol

def parse_lp(path):
    """Minimal line-protocol reader for fixture files (measurement, tags, fields,
    trailing timestamp in s or ns). Fixture data avoids escaped separators; a
    quoted string field may contain spaces/commas only via this simple scan."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            head, fields_ts = line.split(" ", 1)
            if " " in fields_ts:
                fields_part, ts_part = fields_ts.rsplit(" ", 1)
            else:
                die(f"{path}: fixture lines must carry an explicit timestamp: {line!r}")
            parts = head.split(",")
            meas, tags = parts[0], {}
            for kv in parts[1:]:
                k, v = kv.split("=", 1)
                tags[k] = v
            fields = {}
            for kv in _split_fields(fields_part):
                k, v = kv.split("=", 1)
                if v.startswith('"') and v.endswith('"'):
                    fields[k] = v[1:-1]
                else:
                    fields[k] = float(v.rstrip("i"))
            ts_i = int(ts_part)
            if ts_i > 10 ** 11:          # ns precision
                ts_i //= 10 ** 9
            rows.append((meas, tags, fields, datetime.fromtimestamp(ts_i, tz=UTC)))
    return rows


def _split_fields(s):
    out, cur, quoted = [], [], False
    for ch in s:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


# ---------------------------------------------------------------- computation (shared)

def compute(photone_rows, light_rows_by_loc, ref_samples_by_loc, registry,
            computed_at, station_device, sun_alt):
    """Everything Phase B produces, from plain data. Returns (k_model, light_context)."""
    contribs = []
    for row in photone_rows:
        contribs.extend(kmodels.row_contributions(row, registry, station_device))
    k_model = kmodels.bucket_estimates(
        contribs, computed_at, universe=kmodels.universe_keys(registry, computed_at))

    epochs_by_key = {EPOCH_FIELD_KEYS[t]: kmodels.epoch_intervals(registry, t)
                     for t in kmodels.TARGETS}
    cells_by_loc = {}
    for loc, samples in sorted(ref_samples_by_loc.items()):
        if not samples:
            continue
        grid_start = samples[0][0]
        grid_stop = samples[-1][0] + timedelta(seconds=kmodels.CELL_S)
        cells_by_loc[loc] = kmodels.light_context_cells(
            light_rows_by_loc.get(loc, []), samples, sun_alt, epochs_by_key,
            grid_start, grid_stop, computed_at)
    return k_model, cells_by_loc


def adoption_stage(k_model, prior_rows, registry, computed_at):
    """Phase C: run the adoption state machine over the universe buckets of
    each target's current epoch. prior_rows: {(t,s,r,e): latest adopted row}.
    Returns (rows, written_flags, events) aligned by index."""
    model_by_key = {(b["target"], b["source"], b["regime"], b["epoch"]): b
                    for b in k_model}
    entries = {e["epoch_id"]: e for e in registry.get("epochs", [])}
    rows, written_flags, events = [], [], []
    for key in kmodels.universe_keys(registry, computed_at):
        target, source, regime, eid = key
        model = model_by_key[key]        # universe keys are always materialized
        entry = entries.get(eid)
        epoch_start = parse_rfc3339(entry["start"]) if entry else None
        prev_row = (prior_rows.get((target, source, regime, entry["prev"]))
                    if entry and entry.get("prev") else None)
        row, written, evs = kadopt.adopt_step(
            prior_rows.get(key), prev_row, model, epoch_start, computed_at)
        rows.append(row)
        written_flags.append(written)
        events.extend(evs)
    return rows, written_flags, events


def k_adopted_artifact(rows, written_flags, computed_at):
    out = []
    for row, written in zip(rows, written_flags):
        d = dict(row, adopted_at=rfc3339(row["adopted_at"]),
                 state_since=rfc3339(row["state_since"]), written=written)
        for k in ("value", "prev_value", "pending_value"):
            if d[k] is not None:
                d[k] = round(d[k], 9)
        out.append(d)
    return {"computed_at": rfc3339(computed_at), "buckets": out}


def k_event_artifact(events, computed_at):
    out = []
    for e in events:
        d = dict(e, ts=rfc3339(computed_at))
        if d["estimate"] is not None:
            d["estimate"] = round(d["estimate"], 9)
        d["adopted_value"] = round(d["adopted_value"], 9)
        out.append(d)
    return {"computed_at": rfc3339(computed_at), "events": out}


def k_model_artifact(k_model, computed_at):
    out = []
    for b in k_model:
        d = dict(b)
        for k in ("estimate", "ci_lo", "ci_hi", "n_eff", "coverage", "last_ref_age_d"):
            if d[k] is not None:
                d[k] = round(d[k], 9)
        out.append(d)
    return {"computed_at": rfc3339(computed_at), "buckets": out}


def light_context_artifact(cells_by_loc):
    out = {}
    for loc, cells in sorted(cells_by_loc.items()):
        out[loc] = [dict(c, ts=rfc3339(c["ts"])) for c in cells]
    return out


# ---------------------------------------------------------------- fixture mode

def load_registry(path):
    reg = json.load(open(path, encoding="utf-8"))
    if set(reg) != {"epochs", "station_map"}:
        die(f"{path}: top level must be exactly {{\"epochs\":[...],\"station_map\":[...]}}")
    return reg


def photone_rows_from_lp(rows):
    out = []
    for meas, tags, fields, ts in rows:
        if meas != "photone":
            continue
        r = dict(fields)
        r.update(tags)          # device, source, gain, light_location
        r["ts"] = ts
        out.append(r)
    return sorted(out, key=lambda r: r["ts"])


def light_by_location(rows):
    by = {}
    for meas, tags, fields, ts in rows:
        if meas != "light" or "on" not in fields:
            continue
        by.setdefault(tags.get("location", ""), []).append((ts, float(fields["on"])))
    return {loc: sorted(v) for loc, v in by.items()}


def ref_samples_by_location(rows, station_map):
    by = {}
    for meas, tags, fields, ts in rows:
        if meas != "air" or "lux_ref" not in fields:
            continue
        loc = kmodels.station_location(station_map, tags.get("device", ""), ts)
        if loc:
            by.setdefault(loc, []).append((ts, float(fields["lux_ref"])))
    return {loc: sorted(v) for loc, v in by.items()}


def norm_adopted(row):
    """Wire sentinels -> in-memory Nones. k_adopted is ALWAYS written with every
    field (float None as -1, string None as "") because Influx `last()`-per-field
    reconstruction would otherwise resurrect a CLEARED pending_* from an older
    point — omitting a field does not delete its previous value."""
    for k in ("prev_value", "pending_value"):
        v = row.get(k)
        row[k] = None if v is None or v < 0 else v
    for k in ("pending_model_id", "pending_evidence_rev"):
        row[k] = row.get(k) or None
    # state_since arrives as an RFC3339 string field; rows from before the field
    # existed inherit adopted_at (the best available "since")
    ss = row.get("state_since")
    if isinstance(ss, str) and ss:
        row["state_since"] = parse_rfc3339(ss)
    elif not ss:
        row["state_since"] = row["adopted_at"]
    return row


def adopted_rows_from_lp(rows):
    """k_adopted.lp -> {(t,s,r,e): latest row} (latest ts per bucket wins)."""
    latest = {}
    for meas, tags, fields, ts in rows:
        if meas != "k_adopted":
            continue
        key = (tags.get("target"), tags.get("source"), tags.get("regime"),
               tags.get("epoch"))
        if key in latest and latest[key]["adopted_at"] >= ts:
            continue
        row = {"target": key[0], "source": key[1], "regime": key[2],
               "epoch": key[3], "adopted_at": ts,
               "value": fields.get("value"),
               "unit": fields.get("unit", kadopt.UNITS.get(key[0], "")),
               "adoption_state": fields.get("adoption_state", "seed"),
               "model_id": fields.get("model_id", ""),
               "prev_value": fields.get("prev_value"),
               "reason": fields.get("reason", ""),
               "pending_value": fields.get("pending_value"),
               "pending_model_id": fields.get("pending_model_id"),
               "pending_evidence_rev": fields.get("pending_evidence_rev"),
               "hold_ack_state": fields.get("hold_ack_state", "none"),
               "state_since": fields.get("state_since")}
        latest[key] = norm_adopted(row)
    return latest


def adopted_lines(rows, written_flags, station_device, ca_s):
    """Written k_adopted rows -> line protocol (kadopt.to_line: every field
    always present, see norm_adopted). Unchanged rows are skipped — the
    no-change-no-write rule protects adopted_at semantics."""
    return [kadopt.to_line(row, ca_s)
            for row, written in zip(rows, written_flags) if written]


def event_lines(events, station_device, ca_s):
    lines = []
    for e in events:
        tags = ",".join([f"target={esc_tag(e['target'])}", f"source={esc_tag(e['source'])}",
                         f"regime={esc_tag(e['regime'])}", f"epoch={esc_tag(e['epoch'])}",
                         f"event_type={e['event_type']}",
                         f"device={esc_tag(station_device)}"])
        f = [f'model_id="{esc_str(e["model_id"])}"',
             f"estimate={e['estimate'] if e['estimate'] is not None else -1.0}",
             f"adopted_value={e['adopted_value']}",
             f'message="{esc_str(e["message"])}"',
             f'evidence_rev="{e["evidence_rev"]}"']
        lines.append(f"k_event,{tags} {','.join(f)} {ca_s}")
    return lines


def run_fixture(fdir, write_expected):
    env = json.load(open(os.path.join(fdir, "env.json"), encoding="utf-8"))
    registry = load_registry(os.path.join(fdir, "epochs.json"))
    computed_at = parse_rfc3339(open(os.path.join(fdir, "computed_at.txt")).read().strip())
    lp, adopted_lp = [], []
    for name in ("photone.lp", "light.lp", "air.lp"):
        p = os.path.join(fdir, name)
        if os.path.exists(p):
            lp.extend(parse_lp(p))
    p = os.path.join(fdir, "k_adopted.lp")
    if os.path.exists(p):
        adopted_lp = parse_lp(p)

    def sun_alt(ts):
        return solarnoon.sun_alt_at(ts.timestamp(), env["lat"], env["lon"], env["tz"])

    k_model, cells = compute(
        photone_rows_from_lp(lp), light_by_location(lp),
        ref_samples_by_location(lp, registry["station_map"]),
        registry, computed_at, env["station_device"], sun_alt)
    rows, written, events = adoption_stage(
        k_model, adopted_rows_from_lp(adopted_lp), registry, computed_at)

    art = {"k_model.json": k_model_artifact(k_model, computed_at),
           "light_context.json": light_context_artifact(cells),
           "k_adopted.json": k_adopted_artifact(rows, written, computed_at),
           "k_event.json": k_event_artifact(events, computed_at)}
    outdir = os.path.join(fdir, "out")
    os.makedirs(outdir, exist_ok=True)
    for name, data in art.items():
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")

    exp_dir = os.path.join(fdir, "expected")
    if write_expected:
        os.makedirs(exp_dir, exist_ok=True)
        for name, data in art.items():
            with open(os.path.join(exp_dir, name), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.write("\n")
        print(f"expected/ refreshed from this run ({', '.join(art)})")
        return 0

    fail = 0
    for name in art:
        exp_path = os.path.join(exp_dir, name)
        if not os.path.exists(exp_path):
            print(f"FAIL {name}: no expected file — run --write-expected once, review, commit")
            fail = 1
            continue
        expected = json.load(open(exp_path, encoding="utf-8"))
        got = json.loads(json.dumps(art[name]))     # normalize tuples/keys like the file
        if got == expected:
            print(f"pass {name}")
        else:
            print(f"FAIL {name}: out/{name} differs from expected/{name} "
                  f"(diff <(jq -S . {exp_path}) <(jq -S . {os.path.join(fdir,'out',name)}))")
            fail = 1
    return fail


# ---------------------------------------------------------------- live mode

INFLUX = ["docker", "exec", "-e", "INFLUX_TOKEN", "monitor-air-influxdb",
          "influx"]
# same override convention as publish-weight-ref.sh
MQTT_CONTAINER = os.environ.get("MQTT_CONTAINER", "monitor-air-mqtt")


def influx_query(flux):
    import csv as _csv
    import io as _io
    raw = subprocess.run(INFLUX + ["query", "--org", "monitor-air", "--raw", flux],
                         capture_output=True, text=True, env={**os.environ})
    if raw.returncode != 0:
        die("influx query failed:\n" + raw.stderr[:800])
    lines = [ln for ln in raw.stdout.splitlines() if ln and not ln.startswith("#")]
    return list(_csv.DictReader(_io.StringIO("\n".join(lines)))) if lines else []


def esc_tag(s):
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def esc_str(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_live(station_device):
    token = None
    envp = os.path.join(DIR, ".env")
    for line in open(envp, encoding="utf-8"):
        if line.startswith("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="):
            token = line.split("=", 1)[1].strip()
    if not token:
        die(f"need DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in {envp}")
    os.environ["INFLUX_TOKEN"] = token

    registry = load_registry(os.path.join(DIR, "epochs.json"))
    computed_at = datetime.now(UTC).replace(microsecond=0)

    def fnum(r, k):
        try:
            return float(r.get(k, ""))
        except (TypeError, ValueError):
            return None

    # photone rows: full range, paired==1, pivot per (_time, device, source)
    ph = influx_query('''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "photone")
  |> pivot(rowKey: ["_time", "device", "source"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => r.paired == 1.0)
  |> group()
  |> sort(columns: ["_time"])
''')
    photone_rows = []
    for r in ph:
        row = {"ts": datetime.fromisoformat(r["_time"].replace("Z", "+00:00")),
               "device": r.get("device"), "source": r.get("source")}
        for k in ("paired", "lux", "lux_at", "lux_ref_at", "ppfd", "gain_x",
                  "tint_ms", "config_override", "source_override", "lamp_state",
                  *kmodels.CH):
            v = fnum(r, k)
            if v is not None:
                row[k] = v
        photone_rows.append(row)

    # light rows per location (full range; sparse by design)
    lr = influx_query('''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "light" and r._field == "on")
  |> group()
  |> sort(columns: ["_time"])
''')
    import math as _math
    light_by_loc = {}
    for r in lr:
        v = fnum(r, "_value")
        if v is None or not _math.isfinite(v):   # float("NaN") parses — reject here too
            continue
        ts = datetime.fromisoformat(r["_time"].replace("Z", "+00:00"))
        light_by_loc.setdefault(r.get("location", ""), []).append((ts, v))

    # lux_ref pre-aggregated to the 5-min grid: mean per cell, empty cells absent
    # (kmodels sees one synthetic sample per non-empty cell — same mean, same
    # 0-samples->unknown behavior, a fraction of the transfer volume)
    ar = influx_query('''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "air" and r._field == "lux_ref")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> group()
  |> sort(columns: ["_time"])
''')
    ref_by_loc = {}
    for r in ar:
        v = fnum(r, "_value")
        if v is None or not _math.isfinite(v):
            continue
        # aggregateWindow stamps the window END; the cell starts 5m earlier
        ts = datetime.fromisoformat(r["_time"].replace("Z", "+00:00")) - timedelta(
            seconds=kmodels.CELL_S)
        loc = kmodels.station_location(registry["station_map"], r.get("device", ""), ts)
        if loc:
            ref_by_loc.setdefault(loc, []).append((ts, v))
    ref_by_loc = {k: sorted(v) for k, v in ref_by_loc.items()}

    def sun_alt(ts):
        return solarnoon.sun_alt(ts.timestamp())

    k_model, cells_by_loc = compute(photone_rows, light_by_loc, ref_by_loc,
                                    registry, computed_at, station_device, sun_alt)

    # ---- adoption stage: latest k_adopted row per bucket, then the state machine
    pr = influx_query('''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "k_adopted")
  |> last()
  |> pivot(rowKey: ["_time", "target", "source", "regime", "epoch"], columnKey: ["_field"], valueColumn: "_value")
''')
    prior_rows = {}
    for r in pr:
        key = (r.get("target"), r.get("source"), r.get("regime"), r.get("epoch"))
        row = {"target": key[0], "source": key[1], "regime": key[2], "epoch": key[3],
               "adopted_at": datetime.fromisoformat(r["_time"].replace("Z", "+00:00"))
               if r.get("_time") else computed_at,
               "value": fnum(r, "value"),
               "unit": r.get("unit") or kadopt.UNITS.get(key[0], ""),
               "adoption_state": r.get("adoption_state") or "seed",
               "model_id": r.get("model_id") or "",
               "prev_value": fnum(r, "prev_value"),
               "reason": r.get("reason") or "",
               "pending_value": fnum(r, "pending_value"),
               "pending_model_id": r.get("pending_model_id"),
               "pending_evidence_rev": r.get("pending_evidence_rev"),
               "hold_ack_state": r.get("hold_ack_state") or "none",
               "state_since": r.get("state_since")}
        prior_rows[key] = norm_adopted(row)
    a_rows, a_written, events = adoption_stage(k_model, prior_rows, registry, computed_at)

    # dedupe: one alert per (bucket, event_type, evidence_rev) — model_id churns
    # every round and must never be the identity
    deduped = []
    for e in events:
        seen = influx_query(f'''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "k_event" and r._field == "evidence_rev"
       and r.target == "{e['target']}" and r.source == "{e['source']}"
       and r.regime == "{e['regime']}" and r.epoch == "{e['epoch']}"
       and r.event_type == "{e['event_type']}")
  |> filter(fn: (r) => r._value == "{e['evidence_rev']}")
  |> count()
''')
        if not any(fnum(r, "_value") for r in seen):
            deduped.append(e)

    # ---- Influx first (source of truth), MQTT after (retained, self-healing)
    lines = []
    ca_s = int(computed_at.timestamp())
    for b in k_model:
        tags = ",".join(f"{k}={esc_tag(b[k])}" for k in ("target", "source", "regime", "epoch"))
        # empty buckets carry honestly-absent numerics: those fields are OMITTED
        # (line protocol has no null), never faked
        fields = [f"{k}={b[k]}" for k in ("estimate", "ci_lo", "ci_hi", "n_eff",
                                          "last_ref_age_d") if b[k] is not None]
        fields += [f"n_sessions={float(b['n_sessions'])}", f"coverage={b['coverage']}",
                   f'status="{esc_str(b["status"])}"', f'model_id="{esc_str(b["model_id"])}"',
                   f'evidence_rev="{b["evidence_rev"]}"']
        lines.append(f"k_model,{tags} {','.join(fields)} {ca_s}")
    lines.extend(adopted_lines(a_rows, a_written, station_device, ca_s))
    lines.extend(event_lines(deduped, station_device, ca_s))
    for loc, cells in cells_by_loc.items():
        for c in cells:
            fields = [f'source="{c["source"]}"', f'regime="{c["regime"]}"',
                      f'epoch_lux_main="{c["epoch_lux_main"]}"',
                      f'epoch_lux_ref="{c["epoch_lux_ref"]}"',
                      f'epoch_as7341="{c["epoch_as7341"]}"']
            lines.append(f"light_context,location={esc_tag(loc)} "
                         f"{','.join(fields)} {int(c['ts'].timestamp())}")
    if lines:
        w = subprocess.run(INFLUX + ["write", "--bucket", "sensors", "--org",
                                     "monitor-air", "--precision", "s"],
                           input="\n".join(lines), capture_output=True, text=True,
                           env={**os.environ})
        if w.returncode != 0:
            die("influx write failed:\n" + w.stderr[:800])

    # retained MQTT: one topic per bucket, epoch INSIDE the payload; only the
    # UNIVERSE buckets of each target's CURRENT epoch are published (rollover
    # overwrites, and the universe set is stable so nothing lingers). mixed
    # buckets are diagnostics: Influx + panels only — publishing them retained
    # would strand a stale epoch's payload the first time a new epoch has no
    # mixed evidence to overwrite it with.
    current = {t: kmodels.epoch_of(computed_at, kmodels.epoch_intervals(registry, t))[0]
               for t in kmodels.TARGETS}
    adopted_by_key = {(r["target"], r["source"], r["regime"], r["epoch"]): (r, w)
                      for r, w in zip(a_rows, a_written)}
    published = 0
    for b in k_model:
        if b["epoch"] != current[b["target"]]:
            continue
        if (b["source"], b["regime"]) not in kmodels.UNIVERSE[b["target"]]:
            continue
        key = (b["target"], b["source"], b["regime"], b["epoch"])
        arow, _w = adopted_by_key[key]
        adopted_node = dict(arow, adopted_at=rfc3339(arow["adopted_at"]),
                            state_since=rfc3339(arow["state_since"]))
        topic = f"monitor-air/ref/k/{b['target']}/{b['source']}/{b['regime']}"
        payload = json.dumps({"model": k_model_artifact([b], computed_at)["buckets"][0],
                              "adopted": adopted_node},
                             sort_keys=True)
        p = subprocess.run(["docker", "exec", MQTT_CONTAINER, "mosquitto_pub",
                            "-q", "1", "-r", "-t", topic, "-m", payload],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(f"warn: mqtt publish failed for {topic} (next round heals): "
                  f"{p.stderr.strip()[:200]}", file=sys.stderr)
        else:
            published += 1

    n_cells = sum(len(v) for v in cells_by_loc.values())
    print(f"k_model: {len(k_model)} bucket(s) written @ {rfc3339(computed_at)}; "
          f"light_context: {n_cells} cell(s); mqtt: {published} retained topic(s)")
    return 0


def main():
    p = argparse.ArgumentParser(description="k-model Phase B compute")
    p.add_argument("--fixture", metavar="DIR", help="offline replay dir; diff vs expected/")
    p.add_argument("--write-expected", action="store_true",
                   help="with --fixture: refresh expected/ from this run")
    p.add_argument("--device", default="livingroom",
                   help="station device for e0-legacy row admission (default: livingroom)")
    a = p.parse_args()
    if a.fixture:
        return run_fixture(a.fixture, a.write_expected)
    if a.write_expected:
        die("--write-expected requires --fixture")
    return run_live(a.device)


if __name__ == "__main__":
    sys.exit(main())
