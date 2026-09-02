#!/usr/bin/env bash
# Record a calibrated Photone PPFD ground-truth reading into InfluxDB, paired with
# the co-timed sensor values, so the AS7341 spectrum CAL can be validated/corrected
# per light source (daylight vs grow lamp). Writes ONE point to measurement `photone`
# and prints a 3-way comparison (photone / spectrum PPFD / lux÷54).
# Design: broker/PHOTONE-CAL-PLAN.md + the k-model pipeline blueprint (Phase A).
#
#   ./record-photone.sh --ppfd 42 --lux 2100
#   ./record-photone.sh --ppfd 26 --lux 1350 --at 2026-07-20T11:30:00Z --note "cactus-03 canopy"
#   ./record-photone.sh --ppfd 42 --dry-run     # queries InfluxDB, previews, writes nothing
#   ./record-photone.sh --self-check            # internal math self-test only — no InfluxDB
#
# v2 (k-model Phase A): `source` is DERIVED from the actual lamp state at the
# measurement instant (Influx light.on lookback; seed rows are trusted plug
# observations) × solar altitude — not typed, not inferred from the clock.
# `--source` must match the derivation, with exactly two exceptions:
#   (a) lamp state UNKNOWN — --source is then required and flagged
#       source_override=1;
#   (b) derived mixed + --source daylight — the REF-ANCHOR refinement (not an
#       override, not flagged): the daylight field measured at the lux_ref spot,
#       which never sees the grow lamp. The row is tagged daylight with
#       lamp_state=1 kept truthful; its lamp-lit fields (lux_at, channels)
#       become -1 sentinels, so it carries lux_ref evidence only.
# Any other mismatch aborts.
#
# Placement SOP by source. lamp/mixed = at the marked canopy spot, unshielded.
# daylight = the daylight field at the plant with the LAMP'S CONTRIBUTION
# EXCLUDED — either because the lamp is off, or (lamp on) via the ref-anchor
# above, measured at the lux_ref spot which never sees it. Both are normal:
# `source=daylight` together with `lamp_state=1` is NOT a contradiction, and
# this line used to read as if every daylight row were the second case.
#
# What is load-bearing is not the geography but what the number CONTAINS. A
# daylight numerator has no lamp in it, so it pairs with lux_ref (also
# lamp-free) and NEVER with lux_at (lamp-lit). Pairing it with lux_at is how
# bh1750_lux_main's daylight bucket earned a k of 0.006: 28 lux of blocked
# skylight over 4634 lux of lamp-lit sensor. kmodels.py therefore fails closed
# when lamp_state is absent — "nobody recorded it" is not "the lamp was off".
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
record-photone.sh — record a calibrated Photone ground-truth reading (measurement
`photone`), paired with co-timed AS7341 spectrum + BH1750 lux. The light source
class (daylight|lamp|mixed) is derived from the lamp state at the measurement
instant plus solar altitude; you normally never pass it.

USAGE:
  ./record-photone.sh --ppfd <µmol/m²/s> [--lux <v>] [options]
  ./record-photone.sh --self-check          # internal math self-test (no InfluxDB)

REQUIRED:
  --ppfd <v>        Photone PPFD reading (µmol/m²/s)

OPTIONAL:
  --lux <v>         Photone lux reading (preferred estimator input for luxScale)
  --source <s>      daylight | lamp | mixed. Normally derived — pass it only for:
                    (a) lamp state UNKNOWN: required, flagged source_override=1;
                    (b) REF ANCHOR under a lit lamp: `--source daylight` while the
                    derivation says mixed = "Photone at the lux_ref spot, measuring
                    the daylight field". lux_ref never sees the grow lamp (system
                    invariant: sensors.h / FIRMWARE.md / 遮燈 DLI), so this pairing
                    is valid any daytime; the lamp-lit streams (lux_at + channels)
                    are stored as -1 sentinels and the row carries ref evidence
                    only (needs --lux; lamp_state=1 on the row keeps the room
                    context truthful). Any other mismatch with the derivation
                    aborts.
  --device <name>   sensor device to pair with           (default: livingroom)
  --at <t>          when taken (window ±2m): now | -8m | "YYYY-MM-DD HH:MM" (Taipei)
                    | RFC3339 with Z (UTC). bare/no-zone time = Taipei.  (default: now)
  --note "<text>"   free note stored on the point
  --gain <str>      OVERRIDE legacy gain tag — flags config_override=1; the row is
                    then excluded from as7341_ppfd estimation      (default: 4x)
  --tint-ms <v>     OVERRIDE integration ms — flags config_override=1 (default: 280.78)
  --cal <v>         CAL used to compute spectrum PPFD    (default: 0.0017469)
  --window <min>    pairing half-window, minutes         (default: 2)
  --dry-run         query + preview, write nothing (still needs a DB token)
  --self-check      run the math self-test and exit (no InfluxDB, no token)
  -h, --help        this help

Writes one point + appends broker/photone-log.csv (gitignored; header v3 — a file
with an older header is rotated aside as photone-log.pre-<stamp>.csv, never
clobbered). Unpaired/unstable windows store -1 sentinels. gain_x/tint_ms prefer
the paired spectrum telemetry (firmware ≥ the Phase A build); rows without
telemetry config are e0-legacy for the estimator.
EOF
}

# ---- defaults / args ----
PPFD=""; SOURCE=""; LUX=""; DEVICE="livingroom"; AT="now"; NOTE=""
GAIN="4x"; TINT_MS="280.78"; CAL="0.0017469"; WINDOW_MIN="2"; DRY_RUN=0; SELF_CHECK=0
CONFIG_OVERRIDE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ppfd)      PPFD="${2:?--ppfd needs a value}"; shift 2;;
    --source)    SOURCE="${2:?--source needs a value}"; shift 2;;
    --lux)       LUX="${2:?--lux needs a value}"; shift 2;;
    --device)    DEVICE="${2:?--device needs a value}"; shift 2;;
    --at)        AT="${2:?--at needs a value}"; shift 2;;
    --note)      NOTE="${2:?--note needs a value}"; shift 2;;
    --gain)      GAIN="${2:?--gain needs a value}"; CONFIG_OVERRIDE=1; shift 2;;
    --tint-ms)   TINT_MS="${2:?--tint-ms needs a value}"; CONFIG_OVERRIDE=1; shift 2;;
    --cal)       CAL="${2:?--cal needs a value}"; shift 2;;
    --window)    WINDOW_MIN="${2:?--window needs a value}"; shift 2;;
    --dry-run)   DRY_RUN=1; shift;;
    --self-check) SELF_CHECK=1; shift;;
    -h|--help)   usage; exit 0;;
    *) echo "unknown arg: $1" >&2; echo "try --help" >&2; exit 1;;
  esac
done

# token only needed for real writes/queries (not for --self-check)
TOKEN="$(grep -E '^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
# .env LIGHT_LOCATION is only the bootstrap fallback when the station-map has no entry
ENV_LIGHT_LOCATION="$(grep -E '^LIGHT_LOCATION=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"

PPFD="$PPFD" SOURCE="$SOURCE" LUX="$LUX" DEVICE="$DEVICE" AT="$AT" NOTE="$NOTE" \
GAIN="$GAIN" TINT_MS="$TINT_MS" CAL="$CAL" WINDOW_MIN="$WINDOW_MIN" \
DRY_RUN="$DRY_RUN" SELF_CHECK="$SELF_CHECK" CONFIG_OVERRIDE="$CONFIG_OVERRIDE" \
ENV_LIGHT_LOCATION="$ENV_LIGHT_LOCATION" DIR="$DIR" INFLUX_TOKEN="$TOKEN" python3 - <<'PY'
import os, sys, json, math, subprocess, csv, io, statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

# --- AS7341 constants — CANONICAL SOURCE is the PPFD panel in air.json (id 9/11/12).
#     If those divisors / CAL change, change them THERE and mirror here. ---
CH  = ["f415","f445","f480","f515","f555","f590","f630","f680"]
R   = {"f415":55,"f445":110,"f480":210,"f515":390,"f555":590,"f590":840,"f630":1350,"f680":1070}
LAM = {"f415":415,"f445":445,"f480":480,"f515":515,"f555":555,"f590":590,"f630":630,"f680":680}
LUX_TO_PPFD = 54.0
MIN_N   = 3      # min paired spectrum samples in the window to trust the pairing
MAX_CV  = 0.25   # lux coefficient-of-variation above this = unstable window
SAT     = 65535  # ADC full scale; a channel at/above this = clipped
LAMP_STALE_H = 26   # newest light row older than this before `at` = UNKNOWN
                    # (the daily controller checkpoint guarantees ≥1 row/day)

def S_of(counts):
    return sum(counts[c] / R[c] * LAM[c] for c in CH)

def spec_ppfd(counts, cal):
    return cal * S_of(counts)

def station_lookup(rows, device, at):
    """station_map rows + device + instant -> light_location (latest valid_from <= at).
    Pure (selftested); the file/env plumbing wraps this."""
    best = None
    for m in rows:
        if m.get("device") != device: continue
        vf = datetime.strptime(m["valid_from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if vf <= at and (best is None or vf > best[0]):
            best = (vf, m["light_location"])
    return best[1] if best else None

def lamp_state_from(value, row_ts, at, stale_h=26):
    """Latest light.on row (value, ts) vs the instant -> 1/0/-1. Pure (selftested)."""
    if value is None or row_ts is None: return -1
    if (at - row_ts).total_seconds() > stale_h * 3600: return -1
    return 1 if value >= 0.5 else 0

def config_from(pairs):
    """(gain,tint) pairs seen in the window -> (gain_x, tint_ms, split). Pure."""
    if not pairs: return None, None, False
    uniq = set(pairs)
    if len(uniq) > 1: return None, None, True
    g, t = pairs[0]
    return g, t, False

def streams_of(n_spec, n_lux, lux_cv, config_split, n_ref, ref_cv, ref_only=False):
    """Per-stream quality -> (paired, spec_ok, lux_ok, ref_ok). Pure (selftested).

    Three evidence streams, each standing on its own: a dead AS7341 must not
    sink a lux pairing, a missing BH1750 must not sink a spectrum pairing, and
    an unstable PLANT-position field must not sink the ref stream — the ref
    spot is its own light field, gated by its own CV. main-lux CV doubles as
    the stability proxy for the plant-position field, which the colocated
    AS7341 shares (spectrum has no CV of its own), so instability there
    refuses the spectrum stream too. A config change mid-window poisons only
    the spectrum identity. ref_only (daylight recorded under a lit lamp — the
    ref-anchor row): the lamp-lit streams carry no daylight evidence
    regardless of their quality. paired = any stream stands."""
    lux_stable = lux_cv <= MAX_CV
    spec_ok = n_spec >= MIN_N and not config_split and lux_stable
    lux_ok  = n_lux >= MIN_N and lux_stable
    ref_ok  = n_ref >= MIN_N and ref_cv <= MAX_CV
    if ref_only:
        spec_ok = lux_ok = False
    return (spec_ok or lux_ok or ref_ok), spec_ok, lux_ok, ref_ok

def reconcile(derived, source_arg, has_lux):
    """(derived source, --source arg, has Photone lux) -> (source, ref_only, err).

    source names the MEASURED FIELD; placement is its function (blueprint:
    daylight = Photone at the ref spot — no separate position/flag exists).
    One refinement is legal: derived mixed + --source daylight = the daylight
    field measured at the lamp-free ref spot (lux_ref never sees the grow lamp
    — system invariant: sensors.h:14, FIRMWARE.md, the 遮燈 DLI ledger). Such a
    row is demoted to ref-only evidence; the room context stays truthful via
    lamp_state=1 on the row. Any other mismatch aborts (err="contradiction").
    err in {None, "need-lux", "contradiction"}. Pure (selftested)."""
    if not source_arg or source_arg == derived:
        return derived, False, None
    if derived == "mixed" and source_arg == "daylight":
        if not has_lux: return None, False, "need-lux"
        return "daylight", True, None
    return None, False, "contradiction"

def derive_source(lamp_state, sun_alt):
    """Blueprint source table. Returns (source|None, reject_reason|None).
    lamp_state: 1 on / 0 off / -1 unknown; day = sun_alt > 0."""
    if lamp_state == -1: return None, None            # UNKNOWN: caller needs --source
    day = sun_alt > 0.0
    if lamp_state == 0 and day:     return "daylight", None
    if lamp_state == 1 and not day: return "lamp", None
    if lamp_state == 1 and day:     return "mixed", None
    return None, "lamp off at night — no light source to measure"

# ---------- self-check: pure math, no I/O ----------
if os.environ["SELF_CHECK"] == "1":
    unit = {c: R[c] for c in CH}
    assert abs(S_of(unit) - sum(LAM.values())) < 1e-6, "S_of unit-count identity failed"
    cal = 0.002
    assert abs(spec_ppfd(unit, cal) - cal*sum(LAM.values())) < 1e-9, "spec_ppfd scaling failed"
    photone, spec = 14.0, 9.1
    assert abs((photone/spec) - 1.5384615384615385) < 1e-9, "k_spec math failed"
    # source-derivation table, all six cells
    assert derive_source(0, 10.0)  == ("daylight", None)
    assert derive_source(1, -5.0)  == ("lamp", None)
    assert derive_source(1, 10.0)  == ("mixed", None)
    assert derive_source(0, -5.0)[1] is not None,  "off+night must reject"
    assert derive_source(-1, 10.0) == (None, None), "unknown must defer to --source"
    assert derive_source(0, 0.0)[1] is not None,   "alt==0 is night (day = alt > 0)"
    # station-map boundary: two mappings, the instant picks the latest valid_from <= at
    smap = [{"device":"livingroom","light_location":"livingroom","valid_from":"1970-01-01T00:00:00Z"},
            {"device":"livingroom","light_location":"balcony","valid_from":"2026-09-01T00:00:00Z"}]
    aug = datetime(2026, 8, 30, tzinfo=timezone.utc); sep = datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert station_lookup(smap, "livingroom", aug) == "livingroom", "pre-move maps to old location"
    assert station_lookup(smap, "livingroom", sep) == "balcony",    "post-move maps to new location"
    assert station_lookup(smap, "staging", aug) is None,            "unknown device -> None"
    # lamp-state contract: latest row wins; >26h stale -> UNKNOWN; boundary at t counts
    t0 = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    assert lamp_state_from(1.0, t0, t0) == 1, "row exactly at t counts (right-continuous)"
    assert lamp_state_from(0.0, t0 - timedelta(hours=25), t0) == 0, "25h-old row still trusted"
    assert lamp_state_from(1.0, t0 - timedelta(hours=27), t0) == -1, ">26h -> UNKNOWN"
    assert lamp_state_from(None, None, t0) == -1, "no row -> UNKNOWN"
    # config identity: complete-pair mode; a mid-window change refuses the pairing
    assert config_from([(4.0, 280.78), (4.0, 280.78)]) == (4.0, 280.78, False)
    assert config_from([(4.0, 280.78), (64.0, 280.78)])[2] is True, "config change -> split"
    assert config_from([]) == (None, None, False), "no telemetry config -> legacy"
    # per-stream pairing: each evidence stream stands or falls on its own
    assert streams_of(0, 5, 0.05, False, 5, 0.05) == (True, False, True, True), \
        "dead AS7341 + good lux must still pair (anchor-model rows survive)"
    assert streams_of(5, 0, 0.0, False, 0, 0.0) == (True, True, False, False), \
        "missing BH1750 + good spectrum must still pair"
    assert streams_of(5, 5, 0.05, False, 5, 0.05) == (True, True, True, True), "all streams good"
    assert streams_of(5, 5, 0.50, False, 5, 0.02) == (True, False, False, True), \
        "plant-position instability refuses spec+lux but must NOT sink the ref stream"
    assert streams_of(5, 5, 0.05, True, 0, 0.0) == (True, False, True, False), \
        "config split poisons spectrum only; lux pairing survives"
    assert streams_of(2, 2, 0.0, False, 2, 0.0)[0] is False, "too few samples everywhere"
    assert streams_of(5, 5, 0.05, False, 5, 0.05, ref_only=True) == (True, False, False, True), \
        "ref-only demotes the lamp-lit streams regardless of their quality"
    assert streams_of(5, 5, 0.05, False, 2, 0.0, ref_only=True)[0] is False, \
        "ref-only with a bad ref stream has no evidence at all"
    # source reconciliation — the FULL 3×4 decision table (derived × --source):
    # matching or absent --source passes through; the ONE legal refinement is
    # derived mixed + --source daylight (= ref anchor, the ref spot never sees
    # the lamp); everything else contradicts and aborts. The UNKNOWN branch
    # lives before reconcile (derive_source(-1,·)=(None,None), asserted above;
    # the inline require---source/override path is integration-tested).
    CONTRA = (None, False, "contradiction")
    RECON_TABLE = {
        ("daylight", ""):         ("daylight", False, None),
        ("daylight", "daylight"): ("daylight", False, None),
        ("daylight", "lamp"):     CONTRA,
        ("daylight", "mixed"):    CONTRA,
        ("lamp", ""):             ("lamp", False, None),
        ("lamp", "lamp"):         ("lamp", False, None),
        ("lamp", "daylight"):     CONTRA,   # no daylight field at night
        ("lamp", "mixed"):        CONTRA,
        ("mixed", ""):            ("mixed", False, None),
        ("mixed", "mixed"):       ("mixed", False, None),
        ("mixed", "daylight"):    ("daylight", True, None),  # ref-anchor refinement
        ("mixed", "lamp"):        CONTRA,
    }
    for (d, a), want in RECON_TABLE.items():
        assert reconcile(d, a, True) == want, f"reconcile({d!r}, {a!r}) != {want}"
    assert reconcile("mixed", "daylight", False) == (None, False, "need-lux"), \
        "ref anchor needs --lux"
    print("self-check OK (S_of, spec_ppfd, k math, source table, station-map, "
          "lamp-state, config identity, per-stream pairing, source reconciliation)")
    sys.exit(0)

# ---------- validate ----------
def die(m): print(m, file=sys.stderr); sys.exit(1)
try: ppfd = float(os.environ["PPFD"])
except ValueError: die("--ppfd must be a number")
if not os.environ["PPFD"]: die("--ppfd is required")
if not math.isfinite(ppfd) or ppfd <= 0: die("--ppfd must be a finite number > 0")
source_arg = os.environ["SOURCE"]
if source_arg and source_arg not in ("daylight","lamp","mixed"):
    die("--source must be one of: daylight | lamp | mixed")
device = os.environ["DEVICE"]
import re
if not re.match(r"^[A-Za-z0-9_.-]+$", device):
    die("--device must be a simple tag id [A-Za-z0-9_.-] (interpolated into Flux)")
def farg(name, env):
    v = os.environ[env]
    if not v: return None
    try: return float(v)
    except ValueError: die(f"{name} must be a number")
lux_in = farg("--lux","LUX")
if lux_in is not None and (not math.isfinite(lux_in) or lux_in <= 0):
    die("--lux must be a finite number > 0 (omit it if you didn't take a lux reading)")
cal = farg("--cal","CAL"); tint = farg("--tint-ms","TINT_MS"); win = farg("--window","WINDOW_MIN")
gain = os.environ["GAIN"]
config_override = os.environ["CONFIG_OVERRIDE"] == "1"
note = os.environ["NOTE"].replace("\n"," ").replace("\r"," ")
dry = os.environ["DRY_RUN"] == "1"

# ---------- resolve --at → an instant (UTC) and a ±window range ----------
at_raw = os.environ["AT"].strip()
LOCAL_TZ = timezone(timedelta(hours=8))   # Asia/Taipei (no DST)
def parse_at(s):
    if s == "now": return datetime.now(timezone.utc)
    if s and s[0] == "-" and s[-1] in "smh":
        n = float(s[1:-1]); unit = {"s":1,"m":60,"h":3600}[s[-1]]
        return datetime.now(timezone.utc) - timedelta(seconds=n*unit)
    dt = datetime.fromisoformat(s.replace("Z","+00:00"))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)
try: at = parse_at(at_raw)
except Exception as e: die(f"--at not understood ({at_raw!r}): {e}")
start = (at - timedelta(minutes=win)).strftime("%Y-%m-%dT%H:%M:%SZ")
stop  = (at + timedelta(minutes=win)).strftime("%Y-%m-%dT%H:%M:%SZ")
epoch = int(at.timestamp())

# ---------- station-map: device + at → light_location ----------
def station_location(device, at):
    reg_path = os.path.join(os.environ["DIR"], "epochs.json")
    try:
        rows = json.load(open(reg_path)).get("station_map", [])
    except (OSError, json.JSONDecodeError):
        rows = []
    loc = station_lookup(rows, device, at)
    if loc: return loc, "station-map"
    env_loc = os.environ["ENV_LIGHT_LOCATION"]
    if env_loc: return env_loc, ".env bootstrap (station-map has no entry — add one)"
    return None, None

light_location, loc_src = station_location(device, at)
if light_location is None:
    die(f"no light_location for device {device!r}: station_map empty and no "
        f"LIGHT_LOCATION in .env — add a mapping via mark-epoch.sh map")
if not re.match(r"^[A-Za-z0-9_.-]+$", light_location):
    die(f"resolved light_location {light_location!r} is not a simple tag id")

if not os.environ["INFLUX_TOKEN"]:
    die("need DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in .env — both a real record and --dry-run "
        "query InfluxDB (only --self-check needs no token)")

def influx_query(flux):
    raw = subprocess.run(
        ["docker","exec","-e","INFLUX_TOKEN","monitor-air-influxdb",
         "influx","query","--org","monitor-air","--raw",flux],
        capture_output=True, text=True, env={**os.environ})
    if raw.returncode != 0:
        die("influx query failed:\n"+raw.stderr[:800])
    lines = [ln for ln in raw.stdout.splitlines() if ln and not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)))) if lines else []

# ---------- lamp state at `at` (blueprint light-state contract) ----------
# state(t) = last light.on row (seed included — a seed is a real plug poll) with
# _time <= t; older than 26h => UNKNOWN (the daily checkpoint guarantees rows).
lamp_start = (at - timedelta(hours=LAMP_STALE_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
lamp_stop  = (at + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
at_rfc = at.strftime("%Y-%m-%dT%H:%M:%SZ")
lamp_rows = influx_query(f'''
from(bucket: "sensors")
  |> range(start: {lamp_start}, stop: {lamp_stop})
  |> filter(fn: (r) => r._measurement == "light" and r._field == "on"
       and r.location == "{light_location}")
  |> filter(fn: (r) => r._time <= time(v: "{at_rfc}"))
  |> group()
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
''')
lamp_state, lamp_row_ts = -1, None
if lamp_rows:
    try:
        v = float(lamp_rows[0]["_value"])
        ts = datetime.fromisoformat(lamp_rows[0]["_time"].replace("Z", "+00:00"))
        lamp_state = lamp_state_from(v, ts, at, LAMP_STALE_H)
        lamp_row_ts = lamp_rows[0]["_time"]
    except (KeyError, ValueError):
        lamp_state, lamp_row_ts = -1, None

# ---------- solar altitude at `at` ----------
alt_out = subprocess.run(
    ["python3", os.path.join(os.environ["DIR"], "solar-noon.py"), "--alt-at", str(epoch)],
    capture_output=True, text=True)
if alt_out.returncode != 0:
    die("solar-noon.py --alt-at failed:\n" + alt_out.stderr[:400])
sun_alt = float(alt_out.stdout.strip())

# ---------- derive source; reconcile with --source ----------
derived, reject = derive_source(lamp_state, sun_alt)
if reject: die(f"measurement rejected: {reject} (lamp_state={lamp_state}, sun_alt={sun_alt:.1f})")
source_override, ref_only = 0, False
if derived is None:                    # lamp state UNKNOWN
    if not source_arg:
        die(f"lamp state UNKNOWN at {at:%Y-%m-%dT%H:%M:%SZ} (no light row within "
            f"{LAMP_STALE_H}h for location {light_location!r}) — pass an explicit "
            f"--source to override (it will be flagged source_override=1)")
    source, source_override = source_arg, 1
else:
    source, ref_only, rc_err = reconcile(derived, source_arg, lux_in is not None)
    if rc_err == "need-lux":
        die("--source daylight under a lit lamp is the ref-anchor recording "
            "(Photone at the lux_ref spot) — it needs --lux")
    if rc_err == "contradiction":
        die(f"--source {source_arg!r} contradicts the derived source {derived!r} "
            f"(lamp_state={lamp_state} @ {lamp_row_ts}, sun_alt={sun_alt:.1f}) — "
            f"if the derivation is wrong, fix the light data, don't overrule it")

# ---------- pull co-timed samples (raw) in the window ----------
chan_filter = " or ".join(f'r._field == "{c}"' for c in CH)
rows = influx_query(f'''
from(bucket: "sensors")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r.device == "{device}" and (
       (r._measurement == "air" and (r._field == "lux" or r._field == "lux_ref")) or
       (r._measurement == "spectrum" and r.mode == "ambient" and
        ({chan_filter} or r._field == "gain" or r._field == "tint_ms"))))
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
''')

def fnum(r, k):
    try: return float(r.get(k, ""))
    except (TypeError, ValueError): return None

spec_samples, lux_samples, ref_samples, chan_acc = [], [], [], {c: [] for c in CH}
gain_seen, tint_seen = [], []
clipped = False
for r in rows:
    lv = fnum(r, "lux");  rv = fnum(r, "lux_ref")
    if lv is not None: lux_samples.append(lv)
    if rv is not None: ref_samples.append(rv)
    counts = {c: fnum(r, c) for c in CH}
    if all(counts[c] is not None for c in CH):
        if any(counts[c] >= SAT for c in CH): clipped = True; continue
        gv = fnum(r, "gain"); tv = fnum(r, "tint_ms")
        if gv is not None and tv is not None:      # config identity = the COMPLETE pair
            gain_seen.append(gv); tint_seen.append(tv)
        for c in CH: chan_acc[c].append(counts[c])
        spec_samples.append(spec_ppfd(counts, cal))

# telemetry config identity (firmware >= Phase A build). Two distinct configs in
# one window = a config change mid-window -> refuse the pairing.
gain_x, tint_x, config_split = config_from(list(zip(gain_seen, tint_seen)))
if gain_x is not None: tint = tint_x

n = len(spec_samples)
def mean(xs): return sum(xs)/len(xs) if xs else None
lux_cv = (statistics.pstdev(lux_samples)/mean(lux_samples)) if len(lux_samples) >= 2 and mean(lux_samples) else 0.0

# ---------- window quality → paired? (per-stream: see streams_of) ----------
ref_cv = (statistics.pstdev(ref_samples)/mean(ref_samples)) if len(ref_samples) >= 2 and mean(ref_samples) else 0.0
ok, spec_ok, lux_ok, ref_ok = streams_of(n, len(lux_samples), lux_cv, config_split,
                                         len(ref_samples), ref_cv, ref_only)
paired = 1.0 if ok else 0.0

warns = []
if clipped: warns.append("a channel hit ADC full-scale (65535) — clipped samples dropped")
if ref_only:
    warns.append("daylight under a lit lamp (ref anchor): main lux + spectrum are "
                 "lamp-lit — stored as sentinels; this row carries lux_ref evidence only")
else:
    if not spec_ok:
        why = ("config changed inside the window" if config_split else
               f"only {n} spectrum samples in ±{win:g}m (< {MIN_N})" if n < MIN_N else
               f"plant-position field unstable (lux CV {lux_cv*100:.0f}%)")
        warns.append(f"spectrum stream refused ({why}) — as7341_ppfd gets sentinels")
    if not lux_ok:
        why = f"lux unstable (CV {lux_cv*100:.0f}% > {MAX_CV*100:.0f}%) — lamp transition / cloud edge?" \
              if lux_cv > MAX_CV else f"only {len(lux_samples)} lux samples in ±{win:g}m (< {MIN_N})"
        warns.append(f"lux stream refused ({why}) — lux targets get sentinels")
if not ref_ok and source == "daylight":
    why = f"lux_ref unstable (CV {ref_cv*100:.0f}% > {MAX_CV*100:.0f}%) — cloud edge?" \
          if ref_cv > MAX_CV else f"only {len(ref_samples)} lux_ref samples in ±{win:g}m (< {MIN_N})"
    warns.append(f"lux_ref stream refused ({why}) — bh1750_lux_ref gets sentinels")
if config_override: warns.append("gain/tint from CLI override — row excluded from as7341_ppfd estimation")
if source_override: warns.append("source from --source override (lamp state UNKNOWN)")
if loc_src and "bootstrap" in loc_src: warns.append(f"light_location via {loc_src}")

# Sentinels are per stream: a refused stream stores -1 (the estimator's >0 rule
# drops it per target) while every surviving stream keeps its evidence — a
# stream_ok flag already implies paired, so each value gates on its own flag.
if spec_ok:
    spec_at = mean(spec_samples)
    chan_at = {c: mean(chan_acc[c]) for c in CH}
    k_spec = ppfd/spec_at if spec_at and spec_at > 0 else float("nan")
else:
    spec_at = -1.0; chan_at = {c: -1.0 for c in CH}; k_spec = float("nan")
if lux_ok:
    lux_at = mean(lux_samples)
    k_lux  = ppfd/(lux_at/LUX_TO_PPFD) if lux_at and lux_at > 0 else float("nan")
else:
    lux_at = -1.0; k_lux = float("nan")
ref_at = mean(ref_samples) if ref_ok else -1.0
r_ref = (lux_in / ref_at) if (lux_in is not None and ref_at > 0) else float("nan")

# ---------- report ----------
lamp_txt = {1:"ON", 0:"OFF", -1:"UNKNOWN"}[lamp_state]
print(f"Photone reading @ {at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
      f"({at.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} Taipei)  device={device}")
print(f"  lamp={lamp_txt}"
      + (f" (last light row {lamp_row_ts})" if lamp_row_ts else "")
      + f"  sun_alt={sun_alt:.1f}°  → source={source}"
      + ("  [override]" if source_override else "")
      + ("  [ref-only]" if ref_only else "")
      + f"  location={light_location}")
print(f"  window ±{win:g}m: {n} spectrum sample(s), {len(lux_samples)} lux sample(s)"
      + (f", lux {min(lux_samples):.0f}–{max(lux_samples):.0f} (CV {lux_cv*100:.0f}%)" if lux_samples else "")
      + (f", telemetry gain={gain_x:g}/tint={tint:g}ms" if gain_x is not None else ", no telemetry config (e0-legacy)"))
for w in warns: print(f"  ⚠ {w}")
paired_txt = "yes" if paired else "NO (all context stored as -1 sentinels)"
if paired and not (spec_ok and lux_ok and ref_ok):
    paired_txt += (f"  (spectrum {'ok' if spec_ok else 'refused'}, "
                   f"lux {'ok' if lux_ok else 'refused'}, "
                   f"lux_ref {'ok' if ref_ok else 'refused'})")
print(f"  paired = {paired_txt}")
print()
print(f"  {'quantity':<22}{'value':>12}")
print(f"  {'Photone PPFD':<22}{ppfd:>12.2f}")
if paired:
    if spec_at > 0:
        print(f"  {'spectrum PPFD':<22}{spec_at:>12.2f}   (CAL={cal:g})")
    else:
        print(f"  {'spectrum PPFD':<22}{'(refused)':>12}")
    if lux_at > 0:
        print(f"  {'lux÷54':<22}{lux_at/LUX_TO_PPFD:>12.2f}   (lux={lux_at:.0f})")
        print(f"  {'k_lux  = Photone/lux54':<22}{k_lux:>12.3f}")
    else:
        print(f"  {'lux÷54':<22}{'(no lux)':>12}")
    if ref_at > 0:
        print(f"  {'lux_ref':<22}{ref_at:>12.0f}")
        if lux_in is not None:
            print(f"  {'r_ref = Photone/lux_ref':<22}{r_ref:>12.3f}   <- 0x5C anchor scale (daylight-only field)")
    if spec_at > 0:
        print(f"  {'k_spec = Photone/spec':<22}{k_spec:>12.3f}   <- spectrum CAL correction for this light")
else:
    print("  (no paired telemetry — comparison unavailable; point still recorded)")

# ---------- line protocol ----------
def esc_tag(s): return s.replace("\\","\\\\").replace(" ","\\ ").replace(",","\\,").replace("=","\\=")
def esc_str(s): return s.replace("\\","\\\\").replace('"','\\"')
fields = [f"ppfd={ppfd}"]
if lux_in is not None: fields.append(f"lux={lux_in}")
fields += [f"spec_ppfd_at={spec_at}", f"lux_at={lux_at}", f"lux_ref_at={ref_at}",
           f"cal_at={cal}", f"tint_ms={tint}", f"n={float(n)}", f"paired={paired}",
           f"lamp_state={float(lamp_state)}", f"sun_alt_deg={sun_alt}",
           f"source_override={float(source_override)}",
           f"config_override={1.0 if config_override else 0.0}"]
if gain_x is not None: fields.append(f"gain_x={gain_x}")
fields += [f"{c}={chan_at[c]}" for c in CH]
if note: fields.append(f'note="{esc_str(note)}"')
tags = (f"device={esc_tag(device)},source={source},gain={esc_tag(gain)}"
        f",light_location={esc_tag(light_location)}")
line = f"photone,{tags} {','.join(fields)} {epoch}"

# ---------- append-only CSV audit copy (v3 header; older files rotate aside) ----------
csv_path = os.path.join(os.environ["DIR"], "photone-log.csv")
csv_hdr = ["time","device","source","gain","ppfd","lux_in","spec_ppfd_at","lux_at",
           "lux_ref_at","k_spec","k_lux","n","paired","cal_at","tint_ms","note",
           "light_location","lamp_state","sun_alt_deg","source_override",
           "gain_x","config_override"]
csv_row = [at.strftime("%Y-%m-%dT%H:%M:%SZ"), device, source, gain, ppfd,
           lux_in if lux_in is not None else "", spec_at, lux_at, ref_at,
           f"{k_spec:.4f}" if paired else "", f"{k_lux:.4f}" if paired else "",
           n, int(paired), cal, tint, note,
           light_location, lamp_state, f"{sun_alt:.2f}", source_override,
           gain_x if gain_x is not None else "", int(config_override)]

if dry:
    print("\n[dry-run] would write line protocol:\n  " + line)
    print("[dry-run] would append CSV row to " + csv_path)
    sys.exit(0)

w = subprocess.run(
    ["docker","exec","-e","INFLUX_TOKEN","-i","monitor-air-influxdb",
     "influx","write","--bucket","sensors","--org","monitor-air","--precision","s"],
    input=line, capture_output=True, text=True, env={**os.environ})
if w.returncode != 0:
    die("influx write failed:\n"+w.stderr[:800])

new = not os.path.exists(csv_path)
if not new:   # an older header → rotate aside, never clobbering a previous rotation
    with open(csv_path) as f:
        first = f.readline().strip().split(",")
    if first != csv_hdr:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = os.path.join(os.environ["DIR"], f"photone-log.pre-{stamp}")
        target, i = base + ".csv", 0
        while os.path.exists(target):   # os.rename overwrites — pick an unused name
            i += 1; target = f"{base}-{i}.csv"
        os.rename(csv_path, target)
        new = True
with open(csv_path, "a", newline="") as f:
    wr = csv.writer(f)
    if new: wr.writerow(csv_hdr)
    wr.writerow(csv_row)

print(f"\n✓ wrote 1 point to measurement `photone` and appended {csv_path}")
print("  → see Grafana panels id 12 (overlay) & id 13 (CAL check)")
PY
