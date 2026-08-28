#!/usr/bin/env bash
# LEGACY QUICK-LOOK ONLY — eyeball the Photone pairing CSV in ten seconds.
# NOT part of the k-model pipeline (tasks/photone-cal-pipeline.md): the pipeline's
# canonical input is the Influx `photone` measurement, its estimator spec differs
# (log-space, session-level, evidence_rev), and nothing is ever baked into
# firmware. Use compute-k-models.sh (Phase B) for anything that feeds a decision.
#
#   ./analyze-photone.sh              # analyze broker/photone-log.csv
#   ./analyze-photone.sh file.csv     # analyze a specific audit CSV
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="${1:-$DIR/photone-log.csv}"
[ -f "$CSV" ] || { echo "no audit csv at $CSV — run record-photone.sh first" >&2; exit 1; }

python3 - "$CSV" <<'PY'
import csv, statistics, sys

LUX_TO_PPFD = 54.0
CV_SINGLE   = 0.10   # CV under this -> one stable coefficient
SPLIT_GAP   = 0.15   # daylight vs lamp medians apart by more -> split by source

rows = []
with open(sys.argv[1]) as f:
    for r in csv.DictReader(f):
        try:
            if float(r.get("paired", 0)) != 1.0: continue
        except ValueError:
            continue
        # Ref-anchor rows recorded under a lit lamp arrive tagged daylight with
        # lamp-lit fields as -1 sentinels — they feed r_ref and nothing else,
        # no special-casing needed here.
        rows.append(r)
if not rows:
    print("no paired rows — nothing to analyze"); sys.exit(0)

def num(r, k):
    try:
        v = float(r.get(k, ""))
        return v if v > 0 else None       # -1 sentinels and zeros drop out
    except ValueError:
        return None

def ratios(rs, fn):
    out = []
    for r in rs:
        v = fn(r)
        if v is not None: out.append(v)
    return out

def stat_line(name, xs, note=""):
    if not xs:
        print(f"  {name:<8} n=0   (no usable rows{': ' + note if note else ''})")
        return None
    med = statistics.median(xs)
    cv  = (statistics.stdev(xs) / statistics.mean(xs)) if len(xs) > 1 else 0.0
    print(f"  {name:<8} n={len(xs):<3} median={med:.3f}  mean={statistics.mean(xs):.3f}"
          f"  CV={cv*100:.1f}%  range={min(xs):.3f}-{max(xs):.3f}")
    return {"n": len(xs), "median": med, "cv": cv}

est = {
    "r_lux":  lambda r: (lambda p, l: p / l if p and l else None)(num(r, "lux_in"), num(r, "lux_at")),
    "r_ref":  lambda r: (lambda p, l: p / l if p and l else None)(num(r, "lux_in"), num(r, "lux_ref_at")),
    "k_lux":  lambda r: (lambda p, l: p / (l / LUX_TO_PPFD) if p and l else None)(num(r, "ppfd"), num(r, "lux_at")),
    "k_spec": lambda r: (lambda p, s: p / s if p and s else None)(num(r, "ppfd"), num(r, "spec_ppfd_at")),
}

by_src, results = {}, {}
for r in rows: by_src.setdefault(r.get("source", "?"), []).append(r)
for src in sorted(by_src):
    rs = by_src[src]
    print(f"\nsource={src}  ({len(rs)} paired reading(s))")
    if src == "mixed": print("  (diagnostic only — mixed light never yields a coefficient)")
    for name, fn in est.items():
        note = "needs record-photone.sh --lux" if name in ("r_lux", "r_ref") else ""
        s = stat_line(name, ratios(rs, fn), note)
        if s: results[(src, name)] = s

print("\n--- verdict hints ---")
for name, label in (("r_lux", "luxScale (main 0x23)"), ("r_ref", "luxScale (ref 0x5C)")):
    day, lamp = results.get(("daylight", name)), results.get(("lamp", name))
    best = day or lamp
    if not best:
        kd = results.get(("daylight", "k_lux"))
        if name == "r_lux" and kd:
            print(f"* {label}: no direct lux pairs — k_lux(daylight) median {kd['median']:.3f} is the"
                  f" fallback estimate, but this tool never bakes anything — see the pipeline.")
        else:
            print(f"* {label}: no data — record sessions with --lux next to that sensor.")
        continue
    if day and lamp and abs(day["median"] - lamp["median"]) / day["median"] > SPLIT_GAP:
        print(f"* {label}: daylight {day['median']:.3f} vs lamp {lamp['median']:.3f} differ >"
              f" {SPLIT_GAP*100:.0f}% — bake the DAYLIGHT factor only; lamp lives in k_lux/k_spec.")
    elif best["cv"] <= CV_SINGLE and best["n"] >= 5:
        print(f"* {label}: median {best['median']:.3f}, CV {best['cv']*100:.1f}%, n={best['n']}"
              f" — stable (diagnostic only; the k_model pipeline decides).")
    else:
        print(f"* {label}: median {best['median']:.3f} but CV {best['cv']*100:.1f}% / n={best['n']}"
              f" — need more rows (target n>=5, CV<={CV_SINGLE*100:.0f}%) before baking.")
ks = results.get(("daylight", "k_spec"))
if ks:
    print(f"* CAL residual (daylight k_spec): median {ks['median']:.3f} — refit CAL via"
          f" calibrate-ppfd.sh AFTER luxScale ships; never hand-multiply.")
PY
