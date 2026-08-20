#!/usr/bin/env bash
# Weekly nudge: is there enough clean data yet to adopt a new PPFD CAL?
#
#   ./cal-review-reminder.sh            # send to Telegram
#   ./cal-review-reminder.sh --dry-run  # print, send nothing
#
# The daily job writes a candidate CAL every morning, but adopting one is a human
# decision with a precondition that is easy to forget: the AS7341's throughput
# ratio has to be back at its healthy baseline. A uniform attenuation scales every
# sample equally, so the fit stays clean, R² stays high, valid stays 1 — and the
# optical loss gets baked into CAL. That is why this reminder carries the ratio
# next to each value instead of just saying "go look".
#
# Weekly rather than one-shot on purpose: the blocker is waiting for clear days,
# and that is not guaranteed to resolve in one week. It keeps reporting until the
# criteria below are met, and says how to stop it.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

DAYS="${DAYS:-10}"
RATIO_MIN="${RATIO_MIN:-6.0}"     # healthy baseline is 6.3-6.6; below this the optics are still down
SPREAD_MAX="${SPREAD_MAX:-5.0}"   # percent, peak-to-peak around the mean
NEED_DAYS="${NEED_DAYS:-4}"       # clean days before the series is worth deciding on

envval() { sed -nE "s/^$1=[[:space:]]*\"?([^\"#[:space:]]+).*/\1/p" "$DIR/.env" 2>/dev/null | head -1 || true; }
TZ_NAME="$(envval LIGHT_TZ)"; TZ_NAME="${TZ_NAME:-Asia/Taipei}"
DEVICE="${DEVICE:-livingroom}"

# One query, two facts per day: the candidate CAL and the optical health it was
# measured under. They are only meaningful together.
FLUX=$(cat <<FLUXEOF
import "date"
import "timezone"
option location = timezone.location(name: "$TZ_NAME")
inW = (t) => { m = date.hour(t: t) * 60 + date.minute(t: t)
               return m >= 360 and m < 480 }
cal = from(bucket: "sensors") |> range(start: -${DAYS}d)
  |> filter(fn: (r) => r._measurement == "ppfd_cal" and r.device == "$DEVICE")
  |> filter(fn: (r) => exists r.window_mode and r.window_mode == "pre-lamp")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({_time: r._time, cal: (if exists r.cal_ols then r.cal_ols else 0.0),
                      valid: r.valid, n: r.n_kept}))
  |> aggregateWindow(every: 1d, fn: last, column: "cal", timeSrc: "_start", createEmpty: false)
c = from(bucket: "sensors") |> range(start: -${DAYS}d)
  |> filter(fn: (r) => r._measurement == "spectrum" and r.mode == "ambient" and r.device == "$DEVICE"
       and (r._field == "clear" or r._field == "saturated"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.clear and r.saturated == 0.0 and r.clear < 65535.0)
  |> filter(fn: (r) => inW(t: r._time))
  |> map(fn: (r) => ({_time: r._time, _value: r.clear}))
  |> aggregateWindow(every: 1d, fn: median, timeSrc: "_start", createEmpty: false)
  |> rename(columns: {_value: "clear"}) |> keep(columns: ["_time", "clear"])
l = from(bucket: "sensors") |> range(start: -${DAYS}d)
  |> filter(fn: (r) => r._measurement == "air" and r.device == "$DEVICE" and r._field == "lux")
  |> filter(fn: (r) => inW(t: r._time))
  |> aggregateWindow(every: 1d, fn: median, timeSrc: "_start", createEmpty: false)
  |> rename(columns: {_value: "lux"}) |> keep(columns: ["_time", "lux"])
ratio = join(tables: {a: c, b: l}, on: ["_time"])
  |> map(fn: (r) => ({_time: r._time, ratio: r.clear / r.lux}))
join(tables: {a: cal, b: ratio}, on: ["_time"])
  |> keep(columns: ["_time", "cal", "valid", "n", "ratio"])
  |> sort(columns: ["_time"])
FLUXEOF
)

CSV="$(docker exec -i monitor-air-influxdb influx query --org monitor-air --raw -f /dev/stdin <<<"$FLUX" 2>/dev/null || true)"

# The python body goes in a variable: `python3 - <<'PY' <<<"$CSV"` has two stdin
# redirections and the last one wins, so the CSV arrives as the SCRIPT, not as input.
PY_SRC=$(cat <<'PY'
import csv, io, os, statistics, sys, time

# The raw CSV carries UTC. Flux's `option location` controls WINDOWING, not the
# rendered timestamp, so slicing the string gives the wrong day: these points sit
# at 06:00 local, which is 22:00 UTC the day before. Render in the real zone.
# tzset rather than zoneinfo — the system interpreter is 3.8.
os.environ["TZ"] = os.environ.get("TZ_NAME", "Asia/Taipei"); time.tzset()
def localday(iso):
    if not iso: return ""
    t = time.strptime(iso.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
    return time.strftime("%Y-%m-%d", time.localtime(calendar_timegm(t)))
def calendar_timegm(t):
    import calendar
    return calendar.timegm(t)
rows = [r for r in csv.reader(sys.stdin) if r and not r[0].startswith("#")]
hdr, data = None, []
for r in rows:
    if r and r[-1] == "ratio" or (hdr is None and "cal" in r):
        hdr = r; continue
    if hdr: data.append(dict(zip(hdr, r)))

rmin, smax, need = float(os.environ["RATIO_MIN"]), float(os.environ["SPREAD_MAX"]), int(os.environ["NEED_DAYS"])
def num(d, k):
    try: return float(d.get(k, ""))
    except ValueError: return None

lines, clean = [], []
for d in data:
    day = localday(d.get("_time") or "")
    cal, val, n, ratio = num(d, "cal"), num(d, "valid"), num(d, "n"), num(d, "ratio")
    if day == "": continue
    ok = val == 1.0 and cal and ratio and ratio >= rmin
    if ok: clean.append((day, cal))
    mark = "OK" if ok else ("光路未達標" if val == 1.0 else "資料不足")
    lines.append("%s  CAL %s  n=%-3s 通量比 %s  %s" % (
        day,
        ("%.7f" % cal) if cal else "   —     ",
        int(n) if n else 0,
        ("%.2f" % ratio) if ratio else " — ",
        mark))

out = ["PPFD CAL 週檢 — 可以決定了嗎？", ""]
out += lines or ["(近期沒有資料)"]
out.append("")
if len(clean) >= need:
    vals = [c for _, c in clean]
    mean = statistics.fmean(vals)
    spread = (max(vals) - min(vals)) / mean * 100
    out.append("乾淨的日子 %d 天（門檻 %d），平均 %.7f，離散 %.1f%%（門檻 %.0f%%）" % (
        len(clean), need, mean, spread, smax))
    out.append("→ 可以決定了。把 %.7f 貼進 air.json 面板 9/11/12 的 CAL。"
               % mean if spread <= smax else
               "→ 還在震盪，再收幾天。")
else:
    out.append("乾淨的日子只有 %d 天（要 %d 天：valid=1 且通量比 ≥ %.1f）。"
               % (len(clean), need, rmin))
    out.append("→ 還不能決定。陰天收不到，等晴天。")
out += ["", "細節見 broker/PPFD-CAL-ROUTINE-PLAN.md。",
        "停掉這個提醒：crontab -e 刪掉 cal-review-reminder 那行。"]
print("\n".join(out))
PY
)
TEXT="$(RATIO_MIN="$RATIO_MIN" SPREAD_MAX="$SPREAD_MAX" NEED_DAYS="$NEED_DAYS" TZ_NAME="$TZ_NAME" \
        python3 -c "$PY_SRC" <<<"$CSV")"

if [ "${1:-}" = "--dry-run" ]; then echo "$TEXT"; exit 0; fi

TOKEN="$(envval TELEGRAM_BOT_TOKEN)"; CHAT="$(envval TELEGRAM_CHAT_ID)"
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env" >&2; exit 1; }
curl -sS -o /dev/null --data-urlencode "text=$TEXT" --data "chat_id=$CHAT" \
  "https://api.telegram.org/bot$TOKEN/sendMessage"
echo "sent:"; echo "$TEXT"
