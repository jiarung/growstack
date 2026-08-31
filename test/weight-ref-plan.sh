#!/usr/bin/env bash
# Offline test for publish-weight-ref.sh's plan generation (the two-tier
# full/provisional logic). Extracts the script's own embedded python — never a
# copy — and replays a synthetic panel CSV + tag map through it.
#
#   ./test/weight-ref-plan.sh      # PASS or loud diff; nonzero exit on FAIL
#
# Also asserts the panel-10 contract the script depends on: the span display
# filter it strips must still exist verbatim in daily.json.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$DIR" <<'EOF'
import csv, json, os, re, subprocess, sys, tempfile

repo = sys.argv[1]
src = open(os.path.join(repo, "broker/publish-weight-ref.sh")).read()

# contract: the panel still carries the exact filter line the script strips
panel = json.load(open(os.path.join(repo, "broker/grafana/provisioning/dashboards/daily.json")))
q = next(p for p in panel["panels"] if p["id"] == 10)["targets"][0]["query"]
assert "|> filter(fn: (r) => r.span > 5.0)" in q, "panel 10 span filter changed — script contract broken"
assert '"basis"' in q or "basis" in q, "panel 10 no longer outputs basis — tier logic broken"

m = re.search(r"python3 - \"\$TMP\" \"\$TAG_MAP\" \"\$PREFIX\" > \"\$TMP/plan\" <<'PY' \|\| PLAN_RC=\$\?\n(.*?)\nPY\n",
              src, re.S)
assert m, "plan heredoc not found in publish-weight-ref.sh"
plan_py = m.group(1)

d = tempfile.mkdtemp()
with open(os.path.join(d, "rows.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["#group", "x"])
    w.writerow(["plant_id", "sat_g", "trig_g", "days", "basis"])
    w.writerow(["cactus-03b", "432.0", "245.0", "3.5", "循環"])      # earned cycle -> FULL
    w.writerow(["cactus-15b", "380.0", "378.0", "1.2", "暫用 p10"])  # tiny span -> provisional
    w.writerow(["cactus-16", "500.0", "460.0", "2.0", "暫用 p10"])   # BIG span but p10 basis -> provisional
    w.writerow(["cactus-20", "432.04", "427.01", "2.0", "循環"])     # raw 5.03 rounds to 5.0 -> provisional
    w.writerow(["cactus-99", "500.0", "400.0", "2.0", "循環"])       # no tag -> warning, exit 4
tagmap = os.path.join(d, "tag-map.json")
json.dump({"AABBCCDD": "cactus-03b", "11223344": "cactus-15b",
           "22334455": "cactus-16", "55667788": "cactus-20"}, open(tagmap, "w"))

r = subprocess.run(["python3", "-", d, tagmap, "monitor-air/ref/weight"],
                   input=plan_py, capture_output=True, text=True)
assert r.returncode == 4, f"expected exit 4 (untagged warning), got {r.returncode}: {r.stderr}"
lines = dict(l.split("\t") for l in r.stdout.strip().splitlines())

full = json.loads(lines["monitor-air/ref/weight/AABBCCDD"])
assert full["dry_g"] == 245.0 and "provisional" not in full, full
for uid, why in (("11223344", "tiny span"), ("22334455", "p10 basis despite big span"),
                 ("55667788", "rounding boundary")):
    p = json.loads(lines[f"monitor-air/ref/weight/{uid}"])
    assert p.get("provisional") is True and "dry_g" not in p, (why, p)
    assert isinstance(p["sat_g"], float), (why, p)
assert "cactus-99" in r.stderr and "no ref published" in r.stderr

# a malformed basis must abort the WHOLE round (schema drift detection)
with open(os.path.join(d, "rows.csv"), "a", newline="") as f:
    csv.writer(f).writerow(["cactus-x", "100.0", "90.0", "1.0", "surprise"])
r2 = subprocess.run(["python3", "-", d, tagmap, "monitor-air/ref/weight"],
                    input=plan_py, capture_output=True, text=True)
assert r2.returncode == 1 and "bad basis" in r2.stderr, (r2.returncode, r2.stderr)

print("pass weight-ref plan (full/provisional tiers, p10 demotion, rounding, untagged, bad-basis bail)")
EOF
