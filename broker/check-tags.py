#!/usr/bin/env python3
"""Which plant ids have two tags, and is that a retag in progress or a mistake?

    ./check-tags.py          # report; exit 1 if anything needs a human
    ./check-tags.py --days 7

add-tag.sh allows two tags on one id because a retag needs it: bind the new
one, then remove the old. Nothing noticed when the second half was forgotten,
or when the "new tag" was bound to the wrong id — on 2026-10-03 two physical
pots shared cactus-30 for three days. The scan history tells the two cases
apart, and that is what this reads:

  both uids scanned in the window   -> two pots, one id. Wrong now.
  only the newer one scanned        -> a retag; the old line is dead weight.
  only the older one scanned        -> the new tag was bound and never used.

Called by plateau-review-reminder.sh so it lands in Telegram weekly.
"""
import argparse, collections, datetime, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


SESSION_S = 3600   # two uids within this of each other were on the bench together


def scans_per_uid():
    """uid -> [(time, plant_id)] over 90 days, newest last."""
    flux = '''from(bucket: "sensors") |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "plant_weight" and r._field == "uid")
  |> keep(columns: ["_time", "_value", "plant_id"])'''
    out = subprocess.run(["docker", "exec", "-i", "monitor-air-influxdb", "influx", "query",
                          "--org", "monitor-air", "--raw", "-f", "/dev/stdin"],
                         input=flux, capture_output=True, text=True, check=True).stdout
    import csv, io
    hdr, res = None, collections.defaultdict(list)
    for r in csv.reader(io.StringIO(out)):
        if not r or r[0].startswith("#"):
            hdr = None if r and r[0].startswith("#") else hdr
            continue
        if hdr is None:
            hdr = r
            continue
        d = dict(zip(hdr, r))
        if d.get("_value"):
            t = d["_time"].replace("Z", "+00:00")
            t = t[:26] + t[-6:] if "." in t else t      # 9 -> 6 fractional digits (py3.8)
            res[d["_value"]].append((datetime.datetime.fromisoformat(t), d.get("plant_id")))
    for v in res.values():
        v.sort()
    return res


def together(a, b, since):
    """Were uids a and b ever scanned within SESSION_S of each other after `since`?
    That is the signature of two physical pots: one person, one bench, two tags.
    A retag never produces it — the old tag is off the pot before the new one
    is scanned."""
    ta = [t for t, _ in a if t >= since]
    tb = [t for t, _ in b if t >= since]
    return any(abs((x - y).total_seconds()) <= SESSION_S for x in ta for y in tb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--map", default=os.path.join(HERE, "node-red/tag-map.json"),
                    help="a tag-map to check (tests replay the 2026-10-03 state)")
    a = ap.parse_args()
    with open(a.map) as f:
        tagmap = json.load(f)
    by_plant = collections.defaultdict(list)
    for uid, p in tagmap.items():
        by_plant[p].append(uid)
    dups = {p: u for p, u in by_plant.items() if len(u) > 1}
    if not dups:
        print("每個 id 一顆 tag。")
        return 0

    scans = scans_per_uid()
    now = datetime.datetime.now(datetime.timezone.utc)
    cut = now - datetime.timedelta(days=a.days)
    bad = 0
    for p, uids in sorted(dups.items()):
        last = {u: scans[u][-1] for u in uids if scans.get(u)}
        age = lambda u: f"{(now - last[u][0]).days} 天前" if u in last else "從未掃過"
        pairs = [(x, y) for i, x in enumerate(uids) for y in uids[i + 1:]
                 if scans.get(x) and scans.get(y) and together(scans[x], scans[y], cut)]
        recent = [u for u in uids if u in last and last[u][0] >= cut]
        if pairs:
            print(f"✗ {p}: 兩顆 tag 在同一場次都掃到 —— 兩個盆共用一個 id，其中一顆綁錯了")
            bad += 1
        elif len(recent) == 1:
            old = [u for u in uids if u not in recent]
            print(f"△ {p}: 換過 tag，舊的該從 tag-map 移除")
            bad += 1
        elif len(recent) >= 2:
            print(f"△ {p}: 兩顆 tag 都在 {a.days} 天內掃過但從未同場 —— 剛換過 tag，舊的該移除")
            bad += 1
        else:
            print(f"? {p}: {len(uids)} 顆 tag，{a.days} 天內都沒掃")
        for u in uids:
            print(f"    {u}  最後 {age(u)}"
                  + (f"，當時記為 {last[u][1]}" if u in last and last[u][1] != p else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
