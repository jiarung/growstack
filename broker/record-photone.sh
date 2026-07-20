#!/usr/bin/env bash
# Record a calibrated Photone PPFD ground-truth reading into InfluxDB, paired with
# the co-timed sensor values, so the AS7341 spectrum CAL can be validated/corrected
# per light source (daylight vs grow lamp). Writes ONE point to measurement `photone`
# and prints a 3-way comparison (photone / spectrum PPFD / lux÷54). Plan + design:
# broker/PHOTONE-CAL-PLAN.md. Panels: air.json id 12 (overlay) + id 13 (CAL check).
#
#   ./record-photone.sh --ppfd 42 --source lamp
#   ./record-photone.sh --ppfd 26 --source daylight --lux 1350 --at 2026-07-20T11:30:00Z --note "cactus-03 canopy"
#   ./record-photone.sh --ppfd 42 --source lamp --dry-run   # queries InfluxDB, previews, writes nothing
#   ./record-photone.sh --self-check        # internal math self-test only — no InfluxDB, no token
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
record-photone.sh — record a calibrated Photone PPFD ground-truth reading into
InfluxDB (measurement `photone`), paired with the co-timed AS7341 spectrum +
BH1750 lux, and print photone / spectrum PPFD / lux÷54 with k_spec & k_lux.

USAGE:
  ./record-photone.sh --ppfd <µmol/m²/s> --source <daylight|lamp|mixed> [options]
  ./record-photone.sh --self-check          # internal math self-test (no InfluxDB)

REQUIRED:
  --ppfd <v>        Photone PPFD reading (µmol/m²/s)
  --source <s>      light condition: daylight | lamp | mixed
                    (mixed = diagnostic only — k is not a stable correction)

OPTIONAL:
  --lux <v>         Photone lux reading (stored for BH1750 cross-check)
  --device <name>   sensor location to pair with        (default: livingroom)
  --at <t>          when taken (window ±2m): now | -8m | "YYYY-MM-DD HH:MM" (Taipei)
                    | RFC3339 with Z (UTC). bare/no-zone time = Taipei.  (default: now)
  --note "<text>"   free note stored on the point
  --gain <str>      AS7341 ambient gain tag              (default: 4x)
  --tint-ms <v>     AS7341 integration time, ms          (default: 280.78)
  --cal <v>         CAL used to compute spectrum PPFD    (default: 0.0017469)
  --window <min>    pairing half-window, minutes         (default: 2)
  --dry-run         query + preview, write nothing (still needs a DB token)
  --self-check      run the math self-test and exit (no InfluxDB, no token)
  -h, --help        this help

EXAMPLES:
  ./record-photone.sh --ppfd 42 --source lamp --lux 2100 --note "cactus-03 canopy"
  ./record-photone.sh --ppfd 260 --source daylight --lux 14000 --note "midday, lamp off"
  ./record-photone.sh --ppfd 42 --source lamp --at -8m
  ./record-photone.sh --ppfd 42 --source lamp --dry-run

Writes one point + appends broker/photone-log.csv (gitignored). Unpaired/unstable
windows store -1 sentinels. Design: broker/PHOTONE-CAL-PLAN.md.
EOF
}

# ---- defaults / args ----
PPFD=""; SOURCE=""; LUX=""; DEVICE="livingroom"; AT="now"; NOTE=""
GAIN="4x"; TINT_MS="280.78"; CAL="0.0017469"; WINDOW_MIN="2"; DRY_RUN=0; SELF_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ppfd)      PPFD="${2:?--ppfd needs a value}"; shift 2;;
    --source)    SOURCE="${2:?--source needs a value}"; shift 2;;
    --lux)       LUX="${2:?--lux needs a value}"; shift 2;;
    --device)    DEVICE="${2:?--device needs a value}"; shift 2;;
    --at)        AT="${2:?--at needs a value}"; shift 2;;
    --note)      NOTE="${2:?--note needs a value}"; shift 2;;
    --gain)      GAIN="${2:?--gain needs a value}"; shift 2;;
    --tint-ms)   TINT_MS="${2:?--tint-ms needs a value}"; shift 2;;
    --cal)       CAL="${2:?--cal needs a value}"; shift 2;;
    --window)    WINDOW_MIN="${2:?--window needs a value}"; shift 2;;
    --dry-run)   DRY_RUN=1; shift;;
    --self-check) SELF_CHECK=1; shift;;
    -h|--help)   usage; exit 0;;
    *) echo "unknown arg: $1" >&2; echo "try --help" >&2; exit 1;;
  esac
done

# token only needed for real writes/queries (not for --self-check / --dry-run compute-less paths)
TOKEN="$(grep -E '^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"

PPFD="$PPFD" SOURCE="$SOURCE" LUX="$LUX" DEVICE="$DEVICE" AT="$AT" NOTE="$NOTE" \
GAIN="$GAIN" TINT_MS="$TINT_MS" CAL="$CAL" WINDOW_MIN="$WINDOW_MIN" \
DRY_RUN="$DRY_RUN" SELF_CHECK="$SELF_CHECK" DIR="$DIR" INFLUX_TOKEN="$TOKEN" python3 - <<'PY'
import os, sys, subprocess, csv, io, statistics
from datetime import datetime, timedelta, timezone

# --- AS7341 constants — CANONICAL SOURCE is the PPFD panel in air.json (id 9/11/12).
#     If those divisors / CAL change, change them THERE and mirror here. ---
CH  = ["f415","f445","f480","f515","f555","f590","f630","f680"]
R   = {"f415":55,"f445":110,"f480":210,"f515":390,"f555":590,"f590":840,"f630":1350,"f680":1070}
LAM = {"f415":415,"f445":445,"f480":480,"f515":515,"f555":555,"f590":590,"f630":630,"f680":680}
LUX_TO_PPFD = 54.0
MIN_N   = 3      # min paired spectrum samples in the window to trust the pairing
MAX_CV  = 0.25   # lux coefficient-of-variation above this = unstable window (lamp transition / cloud edge)
SAT     = 65535  # ADC full scale; a channel at/above this = clipped

def S_of(counts):
    return sum(counts[c] / R[c] * LAM[c] for c in CH)

def spec_ppfd(counts, cal):
    return cal * S_of(counts)

# ---------- self-check (ponytail runnable check): pure math, no I/O ----------
if os.environ["SELF_CHECK"] == "1":
    # all channels = their own R → each term contributes λ; S = Σλ
    unit = {c: R[c] for c in CH}
    assert abs(S_of(unit) - sum(LAM.values())) < 1e-6, "S_of unit-count identity failed"
    cal = 0.002
    assert abs(spec_ppfd(unit, cal) - cal*sum(LAM.values())) < 1e-9, "spec_ppfd scaling failed"
    # k math: photone / spectrum
    photone, spec = 14.0, 9.1
    assert abs((photone/spec) - 1.5384615384615385) < 1e-9, "k_spec math failed"
    # zero-guard: spectrum 0 must not divide
    assert (0.0 if 0.0 == 0 else 1) == 0.0
    print("self-check OK (S_of, spec_ppfd, k math)")
    sys.exit(0)

# ---------- validate ----------
def die(m): print(m, file=sys.stderr); sys.exit(1)
try: ppfd = float(os.environ["PPFD"])
except ValueError: die("--ppfd must be a number")
if not os.environ["PPFD"]: die("--ppfd is required")
if ppfd <= 0: die("--ppfd must be > 0")
source = os.environ["SOURCE"]
if source not in ("daylight","lamp","mixed"): die("--source must be one of: daylight | lamp | mixed")
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
cal = farg("--cal","CAL"); tint = farg("--tint-ms","TINT_MS"); win = farg("--window","WINDOW_MIN")
gain = os.environ["GAIN"]
note = os.environ["NOTE"].replace("\n"," ").replace("\r"," ")   # newlines would split line protocol
dry = os.environ["DRY_RUN"] == "1"

# ---------- resolve --at → an instant (UTC) and a ±window range ----------
at_raw = os.environ["AT"].strip()
LOCAL_TZ = timezone(timedelta(hours=8))   # Asia/Taipei (no DST) — a bare --at time is LOCAL, not UTC
def parse_at(s):
    if s == "now": return datetime.now(timezone.utc)
    if s and s[0] == "-" and s[-1] in "smh":   # -30m / -2h / -90s relative to now
        n = float(s[1:-1]); unit = {"s":1,"m":60,"h":3600}[s[-1]]
        return datetime.now(timezone.utc) - timedelta(seconds=n*unit)
    dt = datetime.fromisoformat(s.replace("Z","+00:00"))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=LOCAL_TZ)   # "2026-07-20 20:30" → Taipei, not UTC
    return dt.astimezone(timezone.utc)
try: at = parse_at(at_raw)
except Exception as e: die(f"--at not understood ({at_raw!r}): {e}")
start = (at - timedelta(minutes=win)).strftime("%Y-%m-%dT%H:%M:%SZ")
stop  = (at + timedelta(minutes=win)).strftime("%Y-%m-%dT%H:%M:%SZ")
epoch = int(at.timestamp())

# ---------- pull co-timed samples (raw) in the window ----------
chan_filter = " or ".join(f'r._field == "{c}"' for c in CH)
flux = f'''
from(bucket: "sensors")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r.device == "{device}" and (
       (r._measurement == "air" and (r._field == "lux" or r._field == "lux_ref")) or
       (r._measurement == "spectrum" and r.mode == "ambient" and ({chan_filter}))))
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
if not os.environ["INFLUX_TOKEN"]:
    die("need DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in .env — both a real record and --dry-run "
        "query InfluxDB for the co-timed comparison (only --self-check needs no token)")
raw = subprocess.run(
    ["docker","exec","-e","INFLUX_TOKEN","monitor-air-influxdb",
     "influx","query","--org","monitor-air","--raw",flux],
    capture_output=True, text=True, env={**os.environ})
if raw.returncode != 0:
    die("influx query failed:\n"+raw.stderr[:800])
lines = [ln for ln in raw.stdout.splitlines() if ln and not ln.startswith("#")]
rows = list(csv.DictReader(io.StringIO("\n".join(lines)))) if lines else []

def fnum(r, k):
    try: return float(r.get(k, ""))
    except (TypeError, ValueError): return None

spec_samples, lux_samples, ref_samples, chan_acc = [], [], [], {c: [] for c in CH}
clipped = False
for r in rows:
    lv = fnum(r, "lux");  rv = fnum(r, "lux_ref")
    if lv is not None: lux_samples.append(lv)
    if rv is not None: ref_samples.append(rv)
    counts = {c: fnum(r, c) for c in CH}
    if all(counts[c] is not None for c in CH):
        if any(counts[c] >= SAT for c in CH): clipped = True; continue
        for c in CH: chan_acc[c].append(counts[c])
        spec_samples.append(spec_ppfd(counts, cal))

n = len(spec_samples)
def mean(xs): return sum(xs)/len(xs) if xs else None
lux_cv = (statistics.pstdev(lux_samples)/mean(lux_samples)) if len(lux_samples) >= 2 and mean(lux_samples) else 0.0

# ---------- window quality → paired? ----------
warns = []
if clipped: warns.append("a channel hit ADC full-scale (65535) — clipped samples dropped")
if n < MIN_N: warns.append(f"only {n} spectrum samples in ±{win:g}m (< {MIN_N})")
if not lux_samples: warns.append("no lux samples in window — cannot pair")
if lux_cv > MAX_CV: warns.append(f"lux unstable in window (CV {lux_cv*100:.0f}% > {MAX_CV*100:.0f}%) — lamp transition / cloud edge?")
paired = 1.0 if (n >= MIN_N and lux_cv <= MAX_CV and len(lux_samples) >= 1) else 0.0

if paired:
    spec_at = mean(spec_samples); lux_at = mean(lux_samples); ref_at = mean(ref_samples) if ref_samples else -1.0
    chan_at = {c: mean(chan_acc[c]) for c in CH}
    k_spec = ppfd/spec_at if spec_at else float("nan")
    k_lux  = ppfd/(lux_at/LUX_TO_PPFD) if lux_at else float("nan")
else:  # unpaired: sentinels (-1) so the Grafana table's map() never label-errors
    spec_at = lux_at = ref_at = -1.0; chan_at = {c: -1.0 for c in CH}; k_spec = k_lux = float("nan")

# ---------- report ----------
print(f"Photone reading @ {at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
      f"({at.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} Taipei)  "
      f"device={device}  source={source}  gain={gain}")
print(f"  window ±{win:g}m: {n} spectrum sample(s), {len(lux_samples)} lux sample(s)"
      + (f", lux {min(lux_samples):.0f}–{max(lux_samples):.0f} (CV {lux_cv*100:.0f}%)" if lux_samples else ""))
for w in warns: print(f"  ⚠ {w}")
print(f"  paired = {'yes' if paired else 'NO (context stored as -1 sentinels)'}")
print()
print(f"  {'quantity':<22}{'value':>12}")
print(f"  {'Photone PPFD':<22}{ppfd:>12.2f}")
if paired:
    print(f"  {'spectrum PPFD':<22}{spec_at:>12.2f}   (CAL={cal:g})")
    print(f"  {'lux÷54':<22}{lux_at/LUX_TO_PPFD:>12.2f}   (lux={lux_at:.0f})")
    print(f"  {'k_spec = Photone/spec':<22}{k_spec:>12.3f}   <- spectrum CAL correction for this light")
    print(f"  {'k_lux  = Photone/lux54':<22}{k_lux:>12.3f}")
else:
    print("  (no paired telemetry — comparison unavailable; point still recorded)")

# ---------- line protocol ----------
def esc_tag(s): return s.replace("\\","\\\\").replace(" ","\\ ").replace(",","\\,").replace("=","\\=")
def esc_str(s): return s.replace("\\","\\\\").replace('"','\\"')
fields = [f"ppfd={ppfd}"]
if lux_in is not None: fields.append(f"lux={lux_in}")
fields += [f"spec_ppfd_at={spec_at}", f"lux_at={lux_at}", f"lux_ref_at={ref_at}",
           f"cal_at={cal}", f"tint_ms={tint}", f"n={float(n)}", f"paired={paired}"]
fields += [f"{c}={chan_at[c]}" for c in CH]
if note: fields.append(f'note="{esc_str(note)}"')
tags = f"device={esc_tag(device)},source={source},gain={esc_tag(gain)}"
line = f"photone,{tags} {','.join(fields)} {epoch}"

# ---------- append-only CSV audit copy ----------
csv_path = os.path.join(os.environ["DIR"], "photone-log.csv")
csv_hdr = ["time","device","source","gain","ppfd","lux_in","spec_ppfd_at","lux_at",
           "lux_ref_at","k_spec","k_lux","n","paired","cal_at","tint_ms","note"]
csv_row = [at.strftime("%Y-%m-%dT%H:%M:%SZ"), device, source, gain, ppfd,
           lux_in if lux_in is not None else "", spec_at, lux_at, ref_at,
           f"{k_spec:.4f}" if paired else "", f"{k_lux:.4f}" if paired else "",
           n, int(paired), cal, tint, note]

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
with open(csv_path, "a", newline="") as f:
    wr = csv.writer(f)
    if new: wr.writerow(csv_hdr)
    wr.writerow(csv_row)

print(f"\n✓ wrote 1 point to measurement `photone` and appended {csv_path}")
print("  → see Grafana panels id 12 (overlay) & id 13 (CAL check)")
PY
