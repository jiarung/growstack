#!/usr/bin/env python3
"""k-model pipeline, pure computation core (Phase B of docs/photone-cal-pipeline.md).

Everything here is deterministic and I/O-free: rows in, artifacts out. The
orchestrator (compute-k-models.py) owns Influx/MQTT/files and feeds this module
plain dicts, so the SAME code path runs live and against offline fixtures.

Spec anchors (the blueprint is the contract; section names in comments):
- estimator Step 1-4 + status decision table -> bucket_estimates()
- evidence_rev canonicalization -> evidence lines are frozen by fixtures
- light_context 5-min grid -> light_context_cells()

    ./kmodels.py --selftest     # pure math + contract cases, no I/O
"""
import hashlib
import math
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN

# --- AS7341 spectrum constants — CANONICAL SOURCE is calibrate-ppfd.sh / the
#     PPFD panels in air.json. If those change, change them THERE and mirror. ---
CH  = ["f415", "f445", "f480", "f515", "f555", "f590", "f630", "f680"]
R   = {"f415": 55, "f445": 110, "f480": 210, "f515": 390,
       "f555": 590, "f590": 840, "f630": 1350, "f680": 1070}
LAM = {"f415": 415, "f445": 445, "f480": 480, "f515": 515,
       "f555": 555, "f590": 590, "f630": 630, "f680": 680}
SAT_COUNT = 65535        # same constant as calibrate-ppfd.sh

# --- blueprint v1 constants (changing any = versioned migration event) ---
DIRECT_MIN   = 20000.0   # lux_ref at/above -> direct
DIFFUSE_MAX  = 10000.0   # lux_ref at/below -> diffuse; between = ambiguous guard band
CHAIN_GAP_S  = 30 * 60   # adjacent rows <= 30 min apart chain into one session
HALF_LIFE_D  = 30.0      # recency weight half-life
BOOT_B       = 1000      # bootstrap replicates
BOOT_SEED    = 42        # per-bucket PRNG seed (random.Random(BOOT_SEED))
CI_MAX_RELW  = 0.20      # CI relative width above this -> provisional
STALE_D      = 90.0      # last_ref_age beyond this -> stale
COVER_WIN_D  = 90.0      # coverage window; denominator 13 ISO weeks
MIN_SESS_VALID = 5
LAMP_STALE_S = 26 * 3600 # light row older than this -> state UNKNOWN
CELL_S       = 300       # light_context grid: UTC 5-minute cells
LAG_S        = 15 * 60   # telemetry lag: cells ending after computed_at-15min stay unwritten

TARGETS = ("bh1750_lux_main", "bh1750_lux_ref", "as7341_ppfd")
E0 = "e0-legacy"

# Bucket universe — the per-target legal matrix (blueprint Step 5). k_model
# materializes these for each target's CURRENT epoch even when empty, so a new
# epoch starts as an honest unvalidated row instead of silently not existing.
UNIVERSE = {
    "bh1750_lux_ref":  (("daylight", "diffuse"), ("daylight", "direct")),
    "bh1750_lux_main": (("daylight", "diffuse"), ("daylight", "direct"), ("lamp", "none")),
    "as7341_ppfd":     (("daylight", "diffuse"), ("daylight", "direct"), ("lamp", "none")),
}
EMPTY_REV = hashlib.sha256(b"").hexdigest()[:12]   # the evidence_rev of no evidence

UTC = timezone.utc


def rfc3339(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def S_of(counts):
    """Blueprint: S = counts spectral integral, gain/tint NOT part of the value."""
    return sum(counts[c] / R[c] * LAM[c] for c in CH)


def dec_fmt(x, places):
    """Fixed-point, round-half-even — the evidence_rev number canonicalization."""
    q = Decimal(1).scaleb(-places)
    return str(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------- epoch registry

def epoch_intervals(registry, target):
    """epochs.json entries for one target -> sorted [(start_dt, epoch_id, entry)].
    Intervals are half-open [start, next start); before the first -> e0-legacy."""
    rows = [e for e in registry.get("epochs", []) if e.get("target") == target]
    rows.sort(key=lambda e: e["start"])
    return [(parse_rfc3339(e["start"]), e["epoch_id"], e) for e in rows]


def epoch_of(ts, intervals):
    """-> (epoch_id, entry|None). Pre-registry data is e0-legacy (usable, capped)."""
    best = None
    for start, eid, entry in intervals:
        if start <= ts:
            best = (eid, entry)
        else:
            break
    return best if best else (E0, None)


def station_location(station_map, device, ts):
    """device + instant -> light_location (latest valid_from <= ts), else None."""
    best = None
    for m in station_map:
        if m.get("device") != device:
            continue
        vf = parse_rfc3339(m["valid_from"])
        if vf <= ts and (best is None or vf > best[0]):
            best = (vf, m["light_location"])
    return best[1] if best else None


def regime_of(mean_ref):
    if mean_ref >= DIRECT_MIN:
        return "direct"
    if mean_ref <= DIFFUSE_MAX:
        return "diffuse"
    return "ambiguous"


# ---------------------------------------------------------------- Step 1+2: rows -> contributions

def _f(row, key):
    v = row.get(key)
    return float(v) if v is not None else None


def pos(v):
    """The ratio-input contract in one place: present, FINITE, and > 0.
    Sentinels (-1), absent fields, NaN and inf all fail it."""
    return v is not None and math.isfinite(v) and v > 0


def row_contributions(row, registry, station_device):
    """One photone row -> [(target, source, regime, epoch_id, ts, k_i, flags)].

    Implements estimator Step 1 (per-row) + Step 2. The sentinel contract: any
    field entering a ratio must be > 0 — sentinels (-1) and absent fields drop
    the row for that target only. `source` is trusted from the row tag (the
    record tool derived and human-reconciled it; re-deriving here would break
    ref-anchor rows). Regime for daylight rows comes from the row's own
    lux_ref_at window mean; without it a daylight row cannot be bucketed for
    regime-keyed targets. Device binding: a declared epoch admits only its
    declared device; e0-legacy admits only `station_device` (single-station
    bootstrap rule — staging boards must never mix in).
    """
    if _f(row, "paired") != 1.0:
        return []
    ts, source, device = row["ts"], row.get("source"), row.get("device")
    if source not in ("daylight", "lamp", "mixed"):
        return []
    flags = tuple(sorted(
        name for name in ("config_override", "source_override")
        if _f(row, name) == 1.0))
    # ref-anchor row (daylight recorded under a lit lamp): its plant-position
    # streams are lamp-lit BY DEFINITION. The recorder sentinels them, but the
    # estimator must not depend on that — enforce the routing here too, the
    # same defense-in-depth as the saturation re-check.
    ref_only = source == "daylight" and _f(row, "lamp_state") == 1.0

    lux, lux_at, ref_at, ppfd = (_f(row, "lux"), _f(row, "lux_at"),
                                 _f(row, "lux_ref_at"), _f(row, "ppfd"))
    counts = {c: _f(row, c) for c in CH}

    def day_regime():
        if not pos(ref_at):
            return None
        r = regime_of(ref_at)
        return None if r == "ambiguous" else r

    out = []
    for target in TARGETS:
        eid, entry = epoch_of(ts, epoch_intervals(registry, target))
        want_device = entry["device"] if entry else station_device
        if device != want_device:
            continue

        if target == "bh1750_lux_ref":
            if source != "daylight" or not pos(lux) or not pos(ref_at):
                continue
            regime = day_regime()
            if regime is None:
                continue
            out.append((target, source, regime, eid, ts, lux / ref_at, flags))

        elif target == "bh1750_lux_main":
            if ref_only or not pos(lux) or not pos(lux_at):
                continue
            regime = day_regime() if source == "daylight" else "none"
            if regime is None:
                continue
            out.append((target, source, regime, eid, ts, lux / lux_at, flags))

        else:  # as7341_ppfd
            if ref_only or not pos(ppfd):
                continue
            if "config_override" in flags:        # CLI-claimed config is not a fact
                continue
            if not all(pos(counts[c]) for c in CH):
                continue
            if any(counts[c] >= SAT_COUNT for c in CH):   # defensive re-check
                continue
            if entry:   # declared epoch: config identity must match, as measured
                gain_x, tint = _f(row, "gain_x"), _f(row, "tint_ms")
                cfg = entry.get("config", {})
                if gain_x is None or tint is None:
                    continue    # no telemetry identity -> not admissible here
                if gain_x != float(cfg.get("gain", -1)) or tint != float(cfg.get("tint_ms", -1)):
                    continue
            regime = day_regime() if source == "daylight" else "none"
            if regime is None:
                continue
            s = S_of(counts)
            if not pos(s):
                continue
            out.append((target, source, regime, eid, ts, ppfd / s, flags))
    return out


# ---------------------------------------------------------------- Step 3: sessions

def chain_sessions(contribs):
    """Contributions of ONE bucket -> sessions (adjacent gap <= 30 min chains).

    z_j = median(ln k_i) (even count -> arithmetic mean of the two middles,
    which is what statistics.median does); k_j = exp(z_j); session ts = the
    median row ts (even count -> the EARLIER of the two middles). session_id =
    target/source/regime/epoch/<first row ts, RFC3339 UTC seconds>.
    """
    rows = sorted(contribs, key=lambda c: c[4])
    sessions, cur = [], []
    for c in rows:
        if cur and (c[4] - cur[-1][4]).total_seconds() > CHAIN_GAP_S:
            sessions.append(cur)
            cur = []
        cur.append(c)
    if cur:
        sessions.append(cur)

    out = []
    for chunk in sessions:
        target, source, regime, eid = chunk[0][:4]
        zs = [math.log(c[5]) for c in chunk]
        z_j = statistics.median(zs)
        tss = sorted(c[4] for c in chunk)
        mid = tss[(len(tss) - 1) // 2]          # even count -> earlier middle
        sid = "/".join([target, source, regime, eid, rfc3339(chunk[0][4])])
        digest = hashlib.sha256("\n".join(
            f"{rfc3339(c[4])}|{dec_fmt(c[5], 9)}|{','.join(c[6])}"
            for c in sorted(chunk, key=lambda c: c[4])).encode()).hexdigest()
        out.append({"session_id": sid, "target": target, "source": source,
                    "regime": regime, "epoch": eid, "z": z_j,
                    "k": math.exp(z_j), "ts": mid, "n_rows": len(chunk),
                    "evidence_digest": digest})
    return out


def evidence_rev(sessions):
    """Bucket-level canonical revision: content-only — recency/time never moves it."""
    lines = sorted(
        f"{s['session_id']}|{s['evidence_digest']}|{dec_fmt(s['k'], 6)}|{rfc3339(s['ts'])}"
        for s in sessions)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- Step 4: bucket estimate

def weighted_median_z(zs, ws):
    """Sort z ascending (stable), first cumulative NORMALIZED weight >= 0.5."""
    order = sorted(range(len(zs)), key=lambda i: zs[i])
    total = sum(ws)
    acc = 0.0
    for i in order:
        acc += ws[i] / total
        if acc >= 0.5:
            return zs[i]
    return zs[order[-1]]


def bootstrap_ci(zs, ws, n):
    """Session-level bootstrap in log space. Draw n with replacement, sampling
    probability = normalized weights (weights are NOT reused inside the
    replicate — the replicate statistic is the unweighted median). B=1000,
    per-bucket random.Random(42); two-tailed 95%, nearest-rank (1-based,
    rank = ceil(p*B), no interpolation)."""
    rng = random.Random(BOOT_SEED)
    reps = sorted(
        statistics.median(rng.choices(zs, weights=ws, k=n))
        for _ in range(BOOT_B))
    lo = reps[math.ceil(0.025 * BOOT_B) - 1]
    hi = reps[math.ceil(0.975 * BOOT_B) - 1]
    return math.exp(lo), math.exp(hi)


def status_of(n_sessions, epoch, n_eff, ci, estimate, last_ref_age_d):
    """Blueprint status decision table — top-down, first hit wins."""
    if n_sessions <= 1:
        return "unvalidated"
    if epoch == E0:
        return "provisional"                      # hard cap: never valid
    if n_sessions < MIN_SESS_VALID or n_eff < 3 or ci is None:
        return "provisional"
    if (ci[1] - ci[0]) / estimate > CI_MAX_RELW:
        return "provisional"
    if last_ref_age_d > STALE_D:
        return "stale"
    return "valid"


def universe_keys(registry, computed_at):
    """The legal-matrix bucket keys for each target's CURRENT epoch — these must
    exist in k_model even with zero evidence (an honest unvalidated row)."""
    keys = []
    for target in TARGETS:
        eid, _ = epoch_of(computed_at, epoch_intervals(registry, target))
        keys.extend((target, s, r, eid) for s, r in UNIVERSE[target])
    return keys


def empty_bucket(key, computed_at):
    """A bucket with no sessions: status table row 1, values honestly absent."""
    return {
        "target": key[0], "source": key[1], "regime": key[2], "epoch": key[3],
        "estimate": None, "ci_lo": None, "ci_hi": None,
        "n_sessions": 0, "n_eff": None, "coverage": 0.0,
        "last_ref_age_d": None, "status": "unvalidated",
        "model_id": "/".join(key) + "@" + rfc3339(computed_at),
        "evidence_rev": EMPTY_REV,
    }


def bucket_estimates(all_contribs, computed_at, universe=()):
    """Contributions (all buckets mixed) -> [bucket dicts], sorted by key.
    `universe` keys with no contributions are materialized as empty buckets."""
    buckets = {}
    for c in all_contribs:
        buckets.setdefault(c[:4], []).append(c)

    out = [empty_bucket(k, computed_at) for k in universe if k not in buckets]
    for key in sorted(buckets):
        target, source, regime, eid = key
        sessions = chain_sessions(buckets[key])
        n = len(sessions)
        zs = [s["z"] for s in sessions]
        ws = [0.5 ** (((computed_at - s["ts"]).total_seconds() / 86400.0) / HALF_LIFE_D)
              for s in sessions]
        est = math.exp(weighted_median_z(zs, ws))
        n_eff = (sum(ws) ** 2) / sum(w * w for w in ws)
        ci = bootstrap_ci(zs, ws, n) if n >= 3 else None
        newest = max(s["ts"] for s in sessions)
        last_ref_age_d = (computed_at - newest).total_seconds() / 86400.0
        horizon = computed_at - timedelta(days=COVER_WIN_D)
        weeks = {s["ts"].isocalendar()[:2] for s in sessions if s["ts"] >= horizon}
        coverage = len(weeks) / 13.0
        status = status_of(n, eid, n_eff, ci, est, last_ref_age_d)
        out.append({
            "target": target, "source": source, "regime": regime, "epoch": eid,
            "estimate": est,
            "ci_lo": ci[0] if ci else None, "ci_hi": ci[1] if ci else None,
            "n_sessions": n, "n_eff": n_eff, "coverage": coverage,
            "last_ref_age_d": last_ref_age_d, "status": status,
            "model_id": "/".join(key) + "@" + rfc3339(computed_at),
            "evidence_rev": evidence_rev(sessions),
        })
    out.sort(key=lambda b: (b["target"], b["source"], b["regime"], b["epoch"]))
    return out


# ---------------------------------------------------------------- light_context grid

def light_state_at(light_rows, t):
    """state(t) = last row (seed/checkpoint included) with ts <= t, right-
    continuous; older than 26h or absent -> -1 (UNKNOWN). light_rows sorted.
    A non-finite `on` cannot define a state (NaN comparisons would silently
    read as OFF) — such rows are ignored here; the cell classifier separately
    poisons any cell containing one."""
    last = None
    for ts, on in light_rows:
        if ts <= t:
            if math.isfinite(on):
                last = (ts, on)
        else:
            break
    if last is None or (t - last[0]).total_seconds() > LAMP_STALE_S:
        return -1
    return 1 if last[1] >= 0.5 else 0


def floor_cell(ts):
    e = int(ts.timestamp())
    return datetime.fromtimestamp(e - e % CELL_S, tz=UTC)


def light_context_cells(light_rows, ref_samples, sun_alt, epochs_by_target,
                        grid_start, grid_stop, computed_at):
    """The 5-min context grid for ONE location.

    light_rows: sorted [(ts, on_float)]; ref_samples: sorted [(ts, lux_ref)]
    already resolved to this location; sun_alt(dt)->degrees; epochs_by_target:
    {target: intervals}. Cells cover [ts, ts+5min); a cell is emitted only if
    it ends at or before computed_at-15min (telemetry lag stays unwritten, a
    later idempotent recompute fills it).

    UNKNOWN whenever the cell cannot be classified honestly: lamp state
    UNKNOWN/stale, a state change inside the cell, a sunrise/sunset crossing
    inside the cell, a daylight cell with no lux_ref samples, or a daylight
    mean in the guard band. Lamp-off night cells are source="none" (no light
    field — consumers fall back uncorrected).
    """
    out = []
    si = 0
    t = floor_cell(grid_start)
    lag_limit = computed_at - timedelta(seconds=LAG_S)
    while t < grid_stop:
        end = t + timedelta(seconds=CELL_S)
        if end > lag_limit:
            break
        cell = {"ts": t}
        for target, ivs in epochs_by_target.items():
            cell["epoch_" + target] = epoch_of(t, ivs)[0]

        # collect this cell's lux_ref samples (ref_samples sorted; si advances)
        while si < len(ref_samples) and ref_samples[si][0] < t:
            si += 1
        j = si
        vals = []
        while j < len(ref_samples) and ref_samples[j][0] < end:
            vals.append(ref_samples[j][1])
            j += 1

        state = light_state_at(light_rows, t)
        # a FINITE row exactly at t defines state(t) (right-continuous) and is
        # not a change; a NON-finite row anywhere in [t, end) is corruption in
        # this cell and poisons it — hence the different left bounds
        changed = any(
            (t <= ts < end and not math.isfinite(on))
            or (t < ts < end and math.isfinite(on)
                and (1 if on >= 0.5 else 0) != state)
            for ts, on in light_rows)
        # the cell is [t, end): a sun-zero crossing exactly AT `end` belongs to
        # the next cell, so probe the last included second, not the boundary
        day0 = sun_alt(t) > 0.0
        day1 = sun_alt(end - timedelta(seconds=1)) > 0.0

        # 0 lux_ref samples -> unknown for EVERY source (the blueprint scopes the
        # guard band to daylight but the zero-sample rule to the cell itself: no
        # telemetry in the cell means the station's view of it is a hole, and a
        # hole is not classified — no forward-fill, no exception for lamp/none).
        # A non-finite sample is corruption, not evidence: the cell is unknown
        # (an inf would otherwise classify as direct through the mean).
        source = regime = "unknown"
        if (vals and all(math.isfinite(v) for v in vals)
                and state != -1 and not changed and day0 == day1):
            if state == 1:
                source, regime = ("mixed", "none") if day0 else ("lamp", "none")
            elif day0:
                r = regime_of(statistics.fmean(vals))
                if r != "ambiguous":
                    source, regime = "daylight", r
            else:
                source, regime = "none", "none"
        cell["source"], cell["regime"] = source, regime
        out.append(cell)
        t = end
    return out


# ---------------------------------------------------------------- selftest

def selftest():
    D = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)

    # S_of identity (unit counts -> sum of wavelengths)
    unit = {c: float(R[c]) for c in CH}
    assert abs(S_of(unit) - sum(LAM.values())) < 1e-9

    # epoch intervals: half-open, pre-registry -> e0-legacy
    reg = {"epochs": [
        {"target": "bh1750_lux_ref", "epoch_id": "bh1750_lux_ref-e1",
         "start": "2026-09-01T00:00:00Z", "device": "livingroom",
         "config": {"address": "0x5C", "position": "p", "optics": "o"}},
        {"target": "bh1750_lux_ref", "epoch_id": "bh1750_lux_ref-e2",
         "start": "2026-09-08T00:00:00Z", "device": "livingroom",
         "config": {"address": "0x5C", "position": "p", "optics": "o"}}],
        "station_map": [{"device": "livingroom", "light_location": "livingroom",
                         "valid_from": "1970-01-01T00:00:00Z"}]}
    ivs = epoch_intervals(reg, "bh1750_lux_ref")
    assert epoch_of(datetime(2026, 8, 30, tzinfo=UTC), ivs)[0] == E0
    assert epoch_of(datetime(2026, 9, 1, tzinfo=UTC), ivs)[0] == "bh1750_lux_ref-e1"
    assert epoch_of(datetime(2026, 9, 8, tzinfo=UTC), ivs)[0] == "bh1750_lux_ref-e2"

    # regime thresholds incl. boundaries (>= / <=)
    assert regime_of(20000.0) == "direct" and regime_of(10000.0) == "diffuse"
    assert regime_of(15000.0) == "ambiguous"

    # row -> contributions: the sentinel and routing contract
    base = {"ts": D, "device": "livingroom", "source": "daylight", "paired": 1.0,
            "lux": 6000.0, "lux_at": 5500.0, "lux_ref_at": 5000.0, "ppfd": 100.0,
            "gain_x": 4.0, "tint_ms": 280.78}
    base.update({c: 1000.0 for c in CH})
    reg2 = {"epochs": [
        {"target": "as7341_ppfd", "epoch_id": "as7341_ppfd-e1",
         "start": "2026-09-01T00:00:00Z", "device": "livingroom",
         "config": {"gain": 4.0, "tint_ms": 280.78}}], "station_map": []}
    got = {c[0]: c for c in row_contributions(dict(base), reg2, "livingroom")}
    assert set(got) == set(TARGETS), "healthy daylight row feeds all three targets"
    assert abs(got["bh1750_lux_ref"][5] - 6000.0 / 5000.0) < 1e-12
    assert got["bh1750_lux_ref"][2] == "diffuse"
    # ref-only row (daylight, lamp on): sentinels leave only the ref target
    ro = dict(base, lux_at=-1.0, lamp_state=1.0, **{c: -1.0 for c in CH})
    got = [c[0] for c in row_contributions(ro, reg2, "livingroom")]
    assert got == ["bh1750_lux_ref"], "ref-only row must feed exactly the ref target"
    # ...and the routing must NOT depend on the recorder's sentinels: a ref-only
    # row carrying POSITIVE lamp-lit fields (bad data, bypass writes) still
    # feeds only the ref target
    ro_bad = dict(base, lamp_state=1.0)     # lux_at + channels all positive
    got = [c[0] for c in row_contributions(ro_bad, reg2, "livingroom")]
    assert got == ["bh1750_lux_ref"], "ref-only enforcement must not rely on sentinels"
    # non-finite ratio inputs fail the >0 contract
    for k, v in (("lux", float("nan")), ("lux_at", float("inf")), ("ppfd", float("nan"))):
        bad = dict(base, **{k: v})
        tgts = [c[0] for c in row_contributions(bad, reg2, "livingroom")]
        if k == "lux":
            assert "bh1750_lux_ref" not in tgts and "bh1750_lux_main" not in tgts
        elif k == "lux_at":
            assert "bh1750_lux_main" not in tgts
        else:
            assert "as7341_ppfd" not in tgts
    # saturation kills as7341 only
    sat = dict(base, f680=65535.0)
    got = [c[0] for c in row_contributions(sat, reg2, "livingroom")]
    assert "as7341_ppfd" not in got and "bh1750_lux_ref" in got
    # config mismatch / config_override / missing telemetry identity in a
    # declared epoch all exclude as7341
    for bad in (dict(base, gain_x=64.0), dict(base, config_override=1.0),
                {k: v for k, v in base.items() if k not in ("gain_x", "tint_ms")}):
        assert "as7341_ppfd" not in [c[0] for c in row_contributions(bad, reg2, "livingroom")]
    # ...but a pre-epoch (e0-legacy) row without gain_x is admissible
    old = {k: v for k, v in base.items() if k not in ("gain_x", "tint_ms")}
    old["ts"] = datetime(2026, 8, 30, tzinfo=UTC)
    assert "as7341_ppfd" in [c[0] for c in row_contributions(old, reg2, "livingroom")]
    # device binding: declared epoch rejects staging; e0-legacy rejects non-station
    stg = dict(base, device="staging")
    assert row_contributions(stg, reg2, "livingroom") == []
    # guard band drops daylight bucketing entirely
    gb = dict(base, lux_ref_at=15000.0)
    assert row_contributions(gb, reg2, "livingroom") == []
    # unpaired row contributes nothing
    assert row_contributions(dict(base, paired=0.0), reg2, "livingroom") == []
    # mixed row (canopy): main + as7341 in regime none, never the ref target
    mx = dict(base, source="mixed")
    got = {c[0]: c[2] for c in row_contributions(mx, reg2, "livingroom")}
    assert got == {"bh1750_lux_main": "none", "as7341_ppfd": "none"}

    # session chaining: 25-min gaps chain (A-B-C one session despite A-C 50min);
    # a 31-min gap splits
    def contrib(ts, k):
        return ("bh1750_lux_ref", "daylight", "diffuse", "e1", ts, k, ())
    times = [D, D + timedelta(minutes=25), D + timedelta(minutes=50)]
    ss = chain_sessions([contrib(t, k) for t, k in zip(times, (1.1, 1.2, 1.3))])
    assert len(ss) == 1 and ss[0]["n_rows"] == 3
    assert abs(ss[0]["k"] - 1.2) < 1e-12, "odd count -> middle value"
    assert ss[0]["ts"] == times[1]
    ss = chain_sessions([contrib(D, 1.0), contrib(D + timedelta(minutes=31), 2.0)])
    assert len(ss) == 2, "31-min gap must split sessions"
    # even count: z = mean of the two middle logs, ts = earlier middle
    ss = chain_sessions([contrib(D, 1.0), contrib(D + timedelta(minutes=10), 4.0)])
    assert abs(ss[0]["k"] - 2.0) < 1e-12 and ss[0]["ts"] == D

    # evidence_rev: value changes move it; pure time passage does not
    s1 = chain_sessions([contrib(D, 1.1)])
    s2 = chain_sessions([contrib(D, 1.1000001)])
    assert evidence_rev(s1) != evidence_rev(s2)
    assert len(evidence_rev(s1)) == 12
    # adding a row that does NOT move the median still moves the rev
    s3 = chain_sessions([contrib(D, 1.1), contrib(D + timedelta(minutes=5), 1.1)])
    assert evidence_rev(s1) != evidence_rev(s3)

    # weighted median: normalized cumulative weight, first >= 0.5
    assert weighted_median_z([1.0, 2.0, 3.0], [1.0, 1.0, 1.0]) == 2.0
    assert weighted_median_z([1.0, 2.0, 3.0], [10.0, 1.0, 1.0]) == 1.0
    assert weighted_median_z([3.0, 1.0], [1.0, 1.0]) == 1.0, "ties: smaller index after sort"

    # bootstrap determinism + sanity
    ci_a = bootstrap_ci([0.1, 0.2, 0.3, 0.4, 0.5], [1.0] * 5, 5)
    ci_b = bootstrap_ci([0.1, 0.2, 0.3, 0.4, 0.5], [1.0] * 5, 5)
    assert ci_a == ci_b, "seed=42 must make the CI reproducible"
    assert ci_a[0] <= math.exp(0.3) <= ci_a[1]

    # empty-bucket materialization: universe keys with no evidence exist as
    # honest unvalidated rows; keys with evidence are computed normally
    ca = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    uni = universe_keys(reg, ca)
    assert ("bh1750_lux_ref", "daylight", "direct", "bh1750_lux_ref-e2") in uni, \
        "universe follows each target's CURRENT epoch"
    ebs = bucket_estimates([], ca, universe=uni)
    assert len(ebs) == len(uni)
    eb = ebs[0]
    assert (eb["status"], eb["n_sessions"], eb["estimate"], eb["evidence_rev"]) == \
        ("unvalidated", 0, None, EMPTY_REV), "empty bucket: honest absence, no fallback"

    # status table order
    assert status_of(1, "x-e1", 9, (1.0, 1.01), 1.0, 1) == "unvalidated"
    assert status_of(9, E0, 9, (1.0, 1.01), 1.0, 1) == "provisional"
    assert status_of(4, "x-e1", 9, (1.0, 1.01), 1.0, 1) == "provisional"
    assert status_of(9, "x-e1", 9, (1.0, 1.5), 1.0, 1) == "provisional"
    assert status_of(9, "x-e1", 9, (1.0, 1.01), 1.0, 91) == "stale"
    assert status_of(9, "x-e1", 9, (1.0, 1.01), 1.0, 89) == "valid"

    # light state: right-continuous, seed rows trusted, 26h staleness
    L = [(D, 1.0), (D + timedelta(hours=2), 0.0)]
    assert light_state_at(L, D) == 1, "row exactly at t counts"
    assert light_state_at(L, D + timedelta(hours=1)) == 1
    assert light_state_at(L, D + timedelta(hours=3)) == 0
    assert light_state_at(L, D + timedelta(hours=2) + timedelta(hours=27)) == -1
    assert light_state_at(L, D - timedelta(seconds=1)) == -1, "no row before t"

    # light_context: one classified cell of each kind
    def sun(ts):        # day until 10:00Z, night after (synthetic, crossing at 10:00)
        return 10.0 if ts < datetime(2026, 9, 9, 10, 0, tzinfo=UTC) else -10.0
    t0 = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
    ca = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    lr = [(datetime(2026, 9, 9, 8, 0, tzinfo=UTC), 0.0),        # off through the morning
          (datetime(2026, 9, 9, 10, 30, tzinfo=UTC), 1.0),      # ON at a cell boundary
          (datetime(2026, 9, 9, 10, 42, 30, tzinfo=UTC), 0.0)]  # OFF mid-cell
    # every cell gets samples EXCEPT 09:15 (zero-sample daylight) and 10:35
    # (zero-sample lamp-on) — the zero-sample rule is source-independent
    special = {0: 5000.0, 5: 25000.0, 10: 15000.0, 55: 5000.0}
    rs = []
    for m in range(0, 120, 5):
        if m in (15, 95):        # 09:15 and 10:35
            continue
        v = special.get(m, 100.0)
        rs.extend((t0 + timedelta(minutes=m, seconds=15 * i), v) for i in range(20))
    rs.sort()
    ivs = {"lux_ref": epoch_intervals(reg, "bh1750_lux_ref")}
    cells = light_context_cells(lr, rs, sun, ivs, t0,
                                datetime(2026, 9, 9, 11, 0, tzinfo=UTC), ca)
    by = {c["ts"].strftime("%H:%M"): c for c in cells}
    assert by["09:00"]["source"] == "daylight" and by["09:00"]["regime"] == "diffuse"
    assert by["09:05"]["regime"] == "direct"
    assert by["09:10"]["source"] == "unknown", "guard-band mean -> unknown cell"
    assert by["09:15"]["source"] == "unknown", "no lux_ref samples -> unknown daylight cell"
    assert by["09:55"]["source"] == "daylight", \
        "sun crossing exactly AT the cell end belongs to the NEXT cell"
    assert by["09:20"]["source"] == "daylight" and by["09:20"]["regime"] == "diffuse"
    assert by["10:00"]["source"] == "none", "lamp off at night = no light field"
    assert by["10:30"]["source"] == "lamp", "transition AT the boundary keeps the cell clean"
    assert by["10:35"]["source"] == "unknown", "zero lux_ref samples -> unknown even lamp-on"
    assert by["10:40"]["source"] == "unknown", "state change inside the cell"
    assert by["10:45"]["source"] == "none"
    assert by["09:00"]["epoch_lux_ref"] == "bh1750_lux_ref-e2"
    # lag: nothing at/after computed_at-15min
    assert all(c["ts"] + timedelta(seconds=CELL_S) <= ca - timedelta(seconds=LAG_S)
               for c in cells)
    # non-finite light row: cannot define state (NaN would read as OFF) and
    # poisons the cell containing it
    nanrow = [(datetime(2026, 9, 9, 8, 0, tzinfo=UTC), 0.0),
              (datetime(2026, 9, 9, 9, 2, tzinfo=UTC), float("nan"))]
    assert light_state_at(nanrow, datetime(2026, 9, 9, 9, 30, tzinfo=UTC)) == 0, \
        "non-finite row must not define a state"
    cells3 = light_context_cells(nanrow, rs, sun, ivs, t0, t0 + timedelta(minutes=10), ca)
    assert cells3[0]["source"] == "unknown", "cell containing a non-finite row -> unknown"
    assert cells3[1]["source"] == "daylight", "cells after it classify from the last finite row"
    # ...including a non-finite row exactly AT the cell start ([t, end) owns it)
    nan_at_start = [(datetime(2026, 9, 9, 8, 0, tzinfo=UTC), 0.0),
                    (t0, float("nan"))]
    cells3b = light_context_cells(nan_at_start, rs, sun, ivs, t0, t0 + timedelta(minutes=10), ca)
    assert cells3b[0]["source"] == "unknown", "non-finite row AT t must poison the cell"
    assert cells3b[1]["source"] == "daylight"
    # non-finite lux_ref sample: corruption -> unknown (inf would read as direct)
    rs_inf = [(t0 + timedelta(seconds=15 * i), 5000.0) for i in range(19)] \
        + [(t0 + timedelta(seconds=15 * 19), float("inf"))]
    cells4 = light_context_cells(lr, rs_inf, sun, ivs, t0, t0 + timedelta(minutes=5), ca)
    assert cells4[0]["source"] == "unknown", "non-finite sample poisons the cell"

    # sunrise/sunset crossing cell is unknown: sun flips at 10:00 exactly ->
    # boundary, so probe a mid-cell flip instead
    def sun2(ts):
        return 10.0 if ts < datetime(2026, 9, 9, 9, 2, tzinfo=UTC) else -10.0
    cells2 = light_context_cells(lr, rs, sun2, ivs, t0, t0 + timedelta(minutes=5), ca)
    assert cells2[0]["source"] == "unknown", "sun-zero crossing inside the cell"

    print("kmodels selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("pure library — run with --selftest", file=sys.stderr)
        sys.exit(1)
