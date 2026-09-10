#!/usr/bin/env bash
# Offline test for publish-weight-ref.sh's plan generation (the three-tier
# full/provisional/name-only logic). Extracts the script's own embedded python — never a
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
assert "|> filter(fn: (r) => r.afirst == 0.0)" in q, "panel 10 first-anchor filter changed — script contract broken"
assert "anchor_g" in q and "first_anchor" in q, "panel 10 no longer outputs anchor_g/first_anchor"

m = re.search(r"python3 - \"\$TMP\" \"\$TAG_MAP\" \"\$PREFIX\" > \"\$TMP/plan\" <<'PY'\n(.*?)\nPY\n",
              src, re.S)
assert m, "plan heredoc not found in publish-weight-ref.sh"
plan_py = m.group(1)

HDR = ["plant_id", "sat_g", "span_g", "days", "basis", "anchor_g", "first_anchor"]

d = tempfile.mkdtemp()
with open(os.path.join(d, "rows.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["#group", "x"])
    # sat_g stays in the INPUT header: the planner still reads it as a presence
    # check (an empty one means panel schema drift, not a new pot). It is simply
    # never published. anchor_g is the weighing AT the watering, which differs
    # from sat_g only when a later reading was higher — cactus-16 is that case.
    w.writerow(HDR)
    w.writerow(["cactus-03b", "432.0", "187.0", "3.5", "循環", "432.0", "0"])    # earned cycle -> FULL
    w.writerow(["cactus-15b", "380.0", "2.0", "1.2", "暫用 p10", "380.0", "0"])  # tiny span -> provisional
    w.writerow(["cactus-16", "500.0", "40.0", "2.0", "暫用 p10", "492.0", "0"])  # BIG span but p10 basis -> provisional
    w.writerow(["cactus-20", "432.04", "5.03", "2.0", "循環", "432.04", "0"])    # raw 5.03 rounds to 5.0 -> provisional
    w.writerow(["cactus-99", "500.0", "100.0", "2.0", "循環", "500.0", "0"])     # no tag -> informational note
    w.writerow(["cactus-25", "188.4", "31.0", "12.1", "循環", "188.4", "1"])     # FIRST-weighing anchor
tagmap = os.path.join(d, "tag-map.json")
json.dump({"AABBCCDD": "cactus-03b", "11223344": "cactus-15b",
           "22334455": "cactus-16", "55667788": "cactus-20",
           "99AABBCC": "cactus-05b", "CCDDEEFF": "cactus-25"},
          open(tagmap, "w"))   # 05b: mapped, NO panel row; 25: first-weighing anchor

r = subprocess.run(["python3", "-", d, tagmap, "monitor-air/ref/weight"],
                   input=plan_py, capture_output=True, text=True)
# untagged plants are NORMAL (repot retires ids) — informational, exit 0
assert r.returncode == 0, f"expected exit 0, got {r.returncode}: {r.stderr}"
lines = dict(l.split("\t") for l in r.stdout.strip().splitlines())

full = json.loads(lines["monitor-air/ref/weight/AABBCCDD"])
# The legacy keys are GONE as of 2026-09-10 — asserting their ABSENCE is the
# point, not an oversight: a payload that still carried sat_g/dry_g would mean
# the follow-up half-landed, and the only symptom on the station would be a
# silently larger message creeping back toward PAYLOAD_MAX.
assert "provisional" not in full, full
for gone in ("sat_g", "dry_g", "anchor_day"):
    assert gone not in full, (gone, full)
assert full["anchor_g"] == 432.0 and full["span_g"] == 187.0, full
assert isinstance(full["anchor_ts"], int) and full["anchor_ts"] > 1600000000, full

# FIRST-weighing anchor: carries anchor_g/anchor_ts and the marker, and NO span_g.
# The anchor is the pot's own first weighing, which was not necessarily saturated,
# so a percentage against it would be fiction — the station shows "1st" instead.
fa = json.loads(lines["monitor-air/ref/weight/CCDDEEFF"])
assert fa["first_anchor"] is True and "span_g" not in fa, fa
assert fa["anchor_g"] == 188.4 and isinstance(fa["anchor_ts"], int), fa
assert "first-weighing anchor (no watering on record): cactus-25" in r.stderr, r.stderr

# anchor_g is the watering reading, NOT the peak that followed it: cactus-16's
# input row carries sat_g 500.0 and anchor_g 492.0, and only the latter ships.
p16 = json.loads(lines["monitor-air/ref/weight/22334455"])
assert p16["anchor_g"] == 492.0 and "sat_g" not in p16, p16

# nothing may exceed the firmware's PAYLOAD_MAX; over it the station drops the
# message silently and keeps its stale cached ref
for topic, payload in lines.items():
    assert len(payload.encode()) <= 192, (topic, len(payload.encode()), payload)
# Dropping the legacy keys took the full tier from ~150 B to ~90 B. Asserting the
# NEW ceiling is what makes that a guarantee rather than a note: a legacy key
# creeping back would still pass the 192 B check and only show up much later, as
# a station silently dropping refs once a plant name grew long enough.
_full_b = len(lines["monitor-air/ref/weight/AABBCCDD"].encode())
assert _full_b <= 100, ("full tier payload grew", _full_b,
                        lines["monitor-air/ref/weight/AABBCCDD"])
for uid, why in (("11223344", "tiny span"), ("22334455", "p10 basis despite big span"),
                 ("55667788", "rounding boundary")):
    p = json.loads(lines[f"monitor-air/ref/weight/{uid}"])
    assert p.get("provisional") is True and "span_g" not in p, (why, p)
    assert isinstance(p["anchor_g"], float), (why, p)
assert "cactus-99" in r.stderr and "no ref published" in r.stderr  # informational note
# name-only tier: a mapped plant with NO watering anchor (absent from the
# panel CSV entirely) still gets a ref carrying just the name
no = json.loads(lines["monitor-air/ref/weight/99AABBCC"])
assert no == {"plant_id": "cactus-05b", "name_only": True}, no
assert "name-only (no watering anchor yet): cactus-05b" in r.stderr

# ZERO anchored plants: a real state (all anchors aged out) — every mapped
# uid downgrades to name-only, loudly, exit 0
with open(os.path.join(d, "rows.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["#group", "x"])
    w.writerow(HDR)
r0 = subprocess.run(["python3", "-", d, tagmap, "monitor-air/ref/weight"],
                    input=plan_py, capture_output=True, text=True)
assert r0.returncode == 0, (r0.returncode, r0.stderr)
assert "ZERO anchored plants" in r0.stderr
lines0 = dict(l.split("	") for l in r0.stdout.strip().splitlines())
assert len(lines0) == 6, lines0          # all six mapped uids
for uid in ("AABBCCDD", "11223344", "22334455", "55667788", "99AABBCC", "CCDDEEFF"):
    nn = json.loads(lines0[f"monitor-air/ref/weight/{uid}"])
    assert nn.get("name_only") is True and "anchor_g" not in nn, (uid, nn)

# restore the normal CSV for the bad-basis case below
with open(os.path.join(d, "rows.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["#group", "x"])
    w.writerow(HDR)
    w.writerow(["cactus-03b", "432.0", "187.0", "3.5", "循環", "432.0", "0"])

# a malformed basis must abort the WHOLE round (schema drift detection)
with open(os.path.join(d, "rows.csv"), "a", newline="") as f:
    csv.writer(f).writerow(["cactus-x", "100.0", "10.0", "1.0", "surprise", "100.0", "0"])
r2 = subprocess.run(["python3", "-", d, tagmap, "monitor-air/ref/weight"],
                    input=plan_py, capture_output=True, text=True)
assert r2.returncode == 1 and "bad basis" in r2.stderr, (r2.returncode, r2.stderr)

print("pass weight-ref plan (full/provisional/first-anchor/name-only tiers, p10 demotion,\n"
      "      rounding, legacy keys absent, payload size, untagged, bad-basis bail)")
EOF
