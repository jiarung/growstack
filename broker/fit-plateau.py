#!/usr/bin/env python3
"""Each pot's own drying curve — how much it loses before it stops, and how fast.

    ./fit-plateau.py              # fit every live plastic pot, write plant_plateau
    ./fit-plateau.py --dry-run    # fit and print, write nothing

The number the OLED and panel 10 divide by is `span`: the largest drop a pot has
ever recovered from. Measured 2026-09-22 across 20 pots, span is 1.7x larger
than the loss a pot actually reaches before it STOPS losing water — so a small
pot sits at its plateau showing 47%, the 80/100% thresholds never fire, and the
gardener waits: 16 of 20 pots were watered a median 1.1 days after the plateau.
That is the whole reason for this file.

Per pot, every drying run since a watering is one curve of (days, grams lost).
All of a pot's runs are fitted together as

    L(t) = A * (1 - exp(-t / tau))

A is the plateau loss — the denominator that makes 100% mean "stopped" — and
tau is how fast the pot gets there (90% at 2.3 tau). The fit is a grid over
tau with A solved in closed form, because two parameters on 20-60 points do not
deserve an optimiser that can wander.

Written to InfluxDB as `plant_plateau` so that panel 10 and the publisher read
ONE denominator instead of each computing their own — the drift this repo has
been bitten by before (MAINTENANCE.md). Refit weekly; a repotted plant is a new
id with its own curve, so lineage is respected for free.

Not touched: firmware. The OLED already shows loss/span; when span becomes A,
100% starts meaning what it says.
"""
import argparse, collections, datetime, importlib.util, math, os, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("et", os.path.join(HERE, "analyze-et.py"))
et = importlib.util.module_from_spec(spec)
spec.loader.exec_module(et)

MAX_DAYS = 8.0        # past this a run is into territory the model does not describe
MIN_PTS = 8           # fewer and A is a guess wearing two decimals
MIN_CYCLES = 3
TAU_GRID = [x / 10 for x in range(3, 80)]   # 0.3 .. 7.9 days
MEASUREMENT = "plant_plateau"


def cycles(series):
    """[(days since watering, grams lost)] per drying run, first reading after
    each watering as the origin — the same anchor the OLED and panel 10 use."""
    s = sorted(series)
    wet = [i for i, ((t0, v0), (t, v)) in enumerate(zip(s, s[1:])) if v - v0 > et.JUMP_G]
    out = []
    for k, a in enumerate(wet):
        b = wet[k + 1] if k + 1 < len(wet) else len(s) - 1
        seg = s[a + 1:b + 1]
        if len(seg) < 3:
            continue
        t0, w0 = seg[0]
        out.append([((t - t0).total_seconds() / 86400, w0 - w) for t, w in seg])
    return out


def fit(pts):
    """(A, tau, rmse) or None. Grid over tau; A is the least-squares scale."""
    best = None
    for tau in TAU_GRID:
        f = [1 - math.exp(-t / tau) for t, _ in pts]
        den = sum(v * v for v in f)
        if den < 1e-9:
            continue
        A = sum(v * l for v, (_, l) in zip(f, pts)) / den
        if A <= 0:
            continue
        rmse = (sum((A * v - l) ** 2 for v, (_, l) in zip(f, pts)) / len(pts)) ** 0.5
        if best is None or rmse < best[2]:
            best = (A, tau, rmse)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--band", default="150,2600")
    a = ap.parse_args()
    lo, hi = (float(x) for x in a.band.split(","))

    rows = et.query()
    by, reg = et.registry(rows)
    ctrl = "cactus-nature-evapotranspiration"
    pots = sorted(p for p, d in reg.items()
                  if not d["retired"] and d["material"] == "plastic"
                  and lo <= d["recent_g"] <= hi and p != ctrl)

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    sat = {p: max(v for _, v in by[p]) for p in pots}
    span = {p: sat[p] - min(v for _, v in by[p]) for p in pots}

    print(f"{'plant_id':14s} {'cyc':>4s} {'pts':>4s} {'A g':>6s} {'tau d':>6s} "
          f"{'90% at':>7s} {'rmse':>5s} {'span':>5s} {'A/span':>6s}")
    lines, skipped = [], []
    for p in pots:
        cy = cycles(by[p])
        pts = [x for c in cy for x in c if x[0] <= MAX_DAYS]
        if len(cy) < MIN_CYCLES or len(pts) < MIN_PTS:
            skipped.append((p, len(cy), len(pts), "too little history"))
            continue
        r = fit(pts)
        if not r:
            skipped.append((p, len(cy), len(pts), "no positive fit"))
            continue
        A, tau, rmse = r
        # A tau on the grid's edge is the grid talking, not the pot: the curve
        # has not plateaued inside MAX_DAYS, so A is the loss at day 8 wearing
        # a plateau's name. cactus-14 (1.8 kg) does this — its real curve takes
        # weeks. It keeps panel 10's own span rather than getting a wrong A.
        if tau >= TAU_GRID[-1]:
            skipped.append((p, len(cy), len(pts), f"tau hit the grid edge ({tau:.1f} d) — "
                                                  "still drying at day 8, no plateau to fit"))
            continue
        if rmse > 0.5 * A:
            skipped.append((p, len(cy), len(pts), f"rmse {rmse:.0f} g is half of A {A:.0f} g"))
            continue
        print(f"{p:14s} {len(cy):4d} {len(pts):4d} {A:6.1f} {tau:6.2f} {2.3*tau:7.1f} "
              f"{rmse:5.1f} {span[p]:5.0f} {A/span[p]:6.2f}")
        lines.append(f"{MEASUREMENT},plant_id={p} A_g={A:.2f},tau_d={tau:.2f},"
                     f"rmse_g={rmse:.2f},n_cycles={len(cy)}i,n_pts={len(pts)}i "
                     f"{int(now.timestamp())}")
    if skipped:
        print("\nnot fitted — panel 10 keeps its own span for these:")
        for p, nc, npts, why in skipped:
            print(f"  {p:14s} {nc:2d} cycles, {npts:2d} pts   {why}")

    if a.dry_run:
        print(f"\n--dry-run: {len(lines)} row(s) not written")
        return 0
    if not lines:
        print("nothing to write")
        return 0

    token = None
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="):
            token = line.split("=", 1)[1].strip()
    if not token:
        sys.exit("need DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in broker/.env")
    # `docker exec` needs -i or `influx write` reads EOF and exits 0 having
    # written nothing — compute-k-models.py:398 learned this the hard way
    w = subprocess.run(["docker", "exec", "-i", "-e", "INFLUX_TOKEN", "monitor-air-influxdb",
                        "influx", "write", "--bucket", "sensors", "--org", "monitor-air",
                        "--precision", "s"],
                       input="\n".join(lines), capture_output=True, text=True,
                       env={**os.environ, "INFLUX_TOKEN": token})
    if w.returncode != 0:
        sys.exit("influx write failed:\n" + w.stderr[:800])
    print(f"\nwrote {len(lines)} row(s) to {MEASUREMENT} @ {now:%Y-%m-%dT%H:%M:%SZ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
