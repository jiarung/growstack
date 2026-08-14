#!/usr/bin/env bash
# Derive the PPFD panel's CAL constant. READ-ONLY — queries InfluxDB and prints,
# changes nothing. Two independent estimates:
#
#   (1) daylight × lux cross-calibration  (the quantitative one)
#       Over a daytime daylight window, the AS7341 spectrum and a co-located
#       BH1750 must agree: fit CAL so CAL·S ≈ lux/54 (the sunlight PPFD factor).
#       S = Σ(countᵢ/Rᵢ · λᵢ) — the exact inner sum of the PPFD panel.
#
#   (2) datasheet first-principles       (an order-of-magnitude cross-check)
#       CAL_datasheet = 0.00836·R_F1 / (ρ_F1 · gain · t_int_ms), where ρ_F1 comes
#       from the AS7341 F1 absolute anchor (3200 counts @ 57 µW/cm² 420nm, AGAIN
#       512×, 100 ms). Needs the firmware's gain + integration. No sky, no meter.
#
# The two agreeing (~within 2×) is a confidence check; a big gap = investigate.
#
#   ./calibrate-ppfd.sh --start -90m --gain 4 --tint-ms 280.78
#   ./calibrate-ppfd.sh --start 2026-07-11T00:00:00Z --stop 2026-07-11T09:00:00Z
#   ./calibrate-ppfd.sh --datasheet-only --gain 64 --tint-ms 27.8
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- defaults / args ----
# Default device was `staging-01` — the bench board the AS7341 lived on until it
# moved to the plant. It has not been the right answer for a while: over the 30
# days to 2026-08-14, livingroom published 157,601 spectrum points and staging-01
# 707, so the old default made the tool answer INSUFFICIENT DATA on a system that
# had a month of perfectly good data.
DEVICE="livingroom"; START="-24h"; STOP="now()"; LUX_MIN=3000; LUX_MAX=45000
DATASHEET_ONLY=0; GAIN=""; TINT_MS=""; SAT_COUNT=65535   # set to firmware full-scale (ATIME+1)*(ASTEP+1)
while [ $# -gt 0 ]; do
  case "$1" in
    --device)   DEVICE="${2:?--device needs a value}"; shift 2;;
    --start)    START="${2:?--start needs a value}"; shift 2;;
    --stop)     STOP="${2:?--stop needs a value}"; shift 2;;
    --lux-min)  LUX_MIN="${2:?--lux-min needs a value}"; shift 2;;
    --lux-max)  LUX_MAX="${2:?--lux-max needs a value}"; shift 2;;
    --gain)     GAIN="${2:?--gain needs a value}"; shift 2;;
    --tint-ms)  TINT_MS="${2:?--tint-ms needs a value}"; shift 2;;
    --sat-count) SAT_COUNT="${2:?--sat-count needs a value}"; shift 2;;
    --datasheet-only) DATASHEET_ONLY=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

# start/stop are Flux range() literals (bare -24h / now() / RFC3339 all valid)
TOKEN="$(grep -E '^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
[ -n "$TOKEN" ] || { echo "no DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in $DIR/.env" >&2; exit 1; }

DEVICE="$DEVICE" START="$START" STOP="$STOP" LUX_MIN="$LUX_MIN" LUX_MAX="$LUX_MAX" \
DATASHEET_ONLY="$DATASHEET_ONLY" GAIN="$GAIN" TINT_MS="$TINT_MS" SAT_COUNT="$SAT_COUNT" \
INFLUX_TOKEN="$TOKEN" python3 - <<'PY'
import os, subprocess, csv, io, statistics, sys

# --- AS7341 constants (must match the PPFD panel in air.json) ---
CH  = ["f415","f445","f480","f515","f555","f590","f630","f680"]
R   = {"f415":55,"f445":110,"f480":210,"f515":390,"f555":590,"f590":840,"f630":1350,"f680":1070}
LAM = {"f415":415,"f445":445,"f480":480,"f515":515,"f555":555,"f590":590,"f630":630,"f680":680}
LUX_TO_PPFD = 54.0        # Apogee sunlight factor: PPFD ≈ lux/54
PHOTON = 0.00836          # W/m² → µmol/m²/s, per nm (= λ·1e-9/(hc·NA)·1e6)

# datasheet F1 absolute anchor
F1_COUNTS, F1_IRR_W_M2, F1_GAIN, F1_TINT_MS = 3200.0, 0.57, 512.0, 100.0
RHO_F1 = F1_COUNTS / F1_IRR_W_M2 / (F1_GAIN * F1_TINT_MS)   # counts per (W/m² · gain · ms)

def cal_datasheet(gain, tint_ms):
    # per-channel ρᵢ∝Rᵢ cancels against S's 1/Rᵢ, leaving F1 as the sole absolute anchor
    return PHOTON * R["f415"] / (RHO_F1 * gain * tint_ms)

def S_of(counts):
    return sum(counts[c] / R[c] * LAM[c] for c in CH)

gain = float(os.environ["GAIN"]) if os.environ["GAIN"] else None
tint = float(os.environ["TINT_MS"]) if os.environ["TINT_MS"] else None
sat_count = float(os.environ["SAT_COUNT"])

print(f"ρ_F1 (F1 absolute responsivity) = {RHO_F1:.5g} counts/(W/m²·gain·ms)")
if gain and tint:
    cds = cal_datasheet(gain, tint)
    print(f"CAL_datasheet = {cds:.5g}   (gain={gain:g}, t_int={tint:g} ms)")
else:
    cds = None
    print("CAL_datasheet: (supply --gain and --tint-ms — the firmware AS7341 settings)")

if os.environ["DATASHEET_ONLY"] == "1":
    print("(datasheet-only mode — no data window queried)")
    sys.exit(0)

# --- pull 1-min means of lux + 8 channels for the device/window ---
# Saturation is inferred from raw channel counts at/above --sat-count rather than
# read from the payload. The comment here used to claim ambient carries no
# `saturated` flag — it does, and has since firmware c9e0690 (10,883 ambient
# points carried it in the two days to 2026-08-14). Inference is kept because it
# is equivalent at the default full-scale AND it survives --sat-count being set
# to a different ATIME/ASTEP, which the flag would not reflect. If you ever want
# the flag instead, note it would have to survive the 1-min mean below: a window
# where one reading in sixty saturates averages to 0.017, not 0.
dev, start, stop = os.environ["DEVICE"], os.environ["START"], os.environ["STOP"]
lux_min, lux_max = float(os.environ["LUX_MIN"]), float(os.environ["LUX_MAX"])
chan_filter = " or ".join(f'r._field == "{c}"' for c in CH)
flux = f'''
from(bucket: "sensors")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r.device == "{dev}" and (
       (r._measurement == "air" and r._field == "lux") or
       (r._measurement == "spectrum" and r.mode == "ambient" and ({chan_filter}))))
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time","lux","{'","'.join(CH)}"])
  |> sort(columns: ["_time"])
'''
raw = subprocess.run(
    ["docker","exec","-e","INFLUX_TOKEN","monitor-air-influxdb",
     "influx","query","--org","monitor-air","--raw",flux],
    capture_output=True, text=True, env={**os.environ})
if raw.returncode != 0:
    print("influx query failed:\n"+raw.stderr[:800], file=sys.stderr); sys.exit(1)

# parse annotated CSV (skip #-annotation lines and blanks)
lines = [ln for ln in raw.stdout.splitlines() if ln and not ln.startswith("#")]
rows = list(csv.DictReader(io.StringIO("\n".join(lines)))) if lines else []

def fnum(r, k):
    try: return float(r.get(k, ""))
    except (TypeError, ValueError): return None

kept = []   # (time, lux, S, ratio)
n_total = n_sat = n_clamp = n_range = n_partial = 0
for r in rows:
    n_total += 1
    lux = fnum(r, "lux")
    counts = {c: fnum(r, c) for c in CH}
    if lux is None or any(counts[c] is None for c in CH): n_partial += 1; continue
    if any(counts[c] >= sat_count for c in CH): n_sat += 1; continue   # inferred AS7341 saturation
    if lux >= lux_max: n_clamp += 1; continue                          # BH1750 near clamp
    if lux < lux_min: n_range += 1; continue
    S = S_of(counts)
    if S <= 0: continue
    kept.append((r.get("_time",""), lux, S, (lux/LUX_TO_PPFD)/S))

print(f"\nwindow {start} → {stop}, device {dev}")
print(f"minutes: {n_total} total | dropped: {n_sat} sat(≥{sat_count:g}), {n_clamp} ≥clamp, "
      f"{n_range} <lux-min, {n_partial} partial | kept {len(kept)}")

# --- guardrails ---
MIN_N, MIN_LUX_SPAN, MAX_DRIFT_PCT = 20, 2.0, 30.0
if len(kept) < MIN_N:
    print(f"\nINSUFFICIENT DATA: kept {len(kept)} < {MIN_N} usable minutes. "
          f"Need a daylight window (AS7341 outdoors, below clamp, plant light off)."); sys.exit(0)

luxes = [k[1] for k in kept]
lux_span = max(luxes) / max(min(luxes), 1e-9)
if lux_span < MIN_LUX_SPAN:
    print(f"\nLUX SPAN TOO NARROW: {min(luxes):.0f}–{max(luxes):.0f} ({lux_span:.1f}×) < {MIN_LUX_SPAN}×. "
          f"Gather a spread of brightness (partial cloud / a few hours)."); sys.exit(0)

# --- fit: OLS through origin + median ratio ---
xs = [k[2] for k in kept]; ys = [k[1]/LUX_TO_PPFD for k in kept]; ratios = [k[3] for k in kept]
cal_ols = sum(x*y for x,y in zip(xs,ys)) / sum(x*x for x in xs)
cal_med = statistics.median(ratios)
pct_diff = abs(cal_ols-cal_med)/cal_med*100
ss_res = sum((y-cal_ols*x)**2 for x,y in zip(xs,ys)); ss_tot = sum(y*y for y in ys)
r2 = 1 - ss_res/ss_tot if ss_tot else float("nan")
sr = sorted(ratios); p10 = sr[int(0.1*(len(sr)-1))]; p90 = sr[int(0.9*(len(sr)-1))]
spread_pct = (p90-p10)/cal_med*100

# --- drift check: bin ratio by lux (the real acceptance test) ---
bylux = sorted(kept, key=lambda k: k[1]); nb = 5; bins = []
for i in range(nb):
    seg = bylux[i*len(bylux)//nb:(i+1)*len(bylux)//nb]
    if seg: bins.append((statistics.mean(s[1] for s in seg), statistics.mean(s[3] for s in seg)))
drift_pct = (max(b[1] for b in bins)-min(b[1] for b in bins))/cal_med*100 if bins else 0.0

print(f"\n  CAL (OLS through origin) = {cal_ols:.5g}")
print(f"  CAL (median ratio)       = {cal_med:.5g}   ({pct_diff:.1f}% from OLS)")
print(f"  R² = {r2:.4f} | 10–90% spread = {spread_pct:.0f}% | N = {len(kept)} | lux {min(luxes):.0f}–{max(luxes):.0f}")
print(f"  ratio vs lux (mean ratio per brightness bin — should be FLAT):")
for lx, rt in bins: print(f"    lux~{lx:8.0f}:  ratio {rt:.5g}")
print(f"  ratio drift across lux = {drift_pct:.0f}%  ({'OK' if drift_pct<=MAX_DRIFT_PCT else 'TOO HIGH — likely geometry/spectral drift'})")
if cds:
    print(f"\n  CAL_daylight / CAL_datasheet = {cal_ols/cds:.2f}×  "
          f"({'consistent (<2×)' if 0.5<=cal_ols/cds<=2 else 'DIVERGENT — investigate'})")

if drift_pct > MAX_DRIFT_PCT:
    print(f"\nRATIO DRIFTS ({drift_pct:.0f}% > {MAX_DRIFT_PCT}%) — not a clean calibration. "
          f"Re-mount sensors co-planar/unobstructed and re-gather before trusting CAL.")
else:
    print(f"\n→ paste into the PPFD panel (air.json id 9): CAL = {cal_ols:.5g}")
PY
