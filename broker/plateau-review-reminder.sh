#!/usr/bin/env bash
# Weekly: refit every pot's drying curve, then nudge the gardener to do the recall.
#
#   ./plateau-review-reminder.sh            # refit + send to Telegram
#   ./plateau-review-reminder.sh --dry-run  # refit in dry-run, print, send nothing
#
# Two things happen here and only the first is automatic. fit-plateau.py rewrites
# plant_plateau (each pot's A and tau — the denominator panel 10 and the OLED
# divide by), so the number the gardener sees tracks the pot as its curve
# changes. Then the message asks for the part a cron job cannot do: run
# `analyze-et.py --index`, read WATERING-INDEX.md, and decide whether what it
# says has changed. That analysis flipped its own conclusion once when two
# readings arrived (2026-09-22), which is why it is re-run on a schedule and
# not when someone remembers.
#
# The message carries the pots currently past their plateau and unwatered,
# because that is the one number the whole exercise exists to produce.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
envval() { sed -nE "s/^$1=[[:space:]]*\"?([^\"#[:space:]]+).*/\1/p" "$DIR/.env" 2>/dev/null | head -1 || true; }

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

if [ "$DRY" = 1 ]; then FIT="$(python3 fit-plateau.py --dry-run 2>&1)"
else                    FIT="$(python3 fit-plateau.py 2>&1)"; fi
NFIT="$(grep -cE '^cactus-[0-9]' <<<"$FIT" || true)"
SKIP="$(sed -n '/^not fitted/,/^$/p' <<<"$FIT" | grep -E '^  cactus' | awk '{print $1}' | tr '\n' ' ')"

# Pots past plateau right now: panel 10's own query, its own thresholds.
PANEL="$(python3 -c '
import json
q=next(p for p in json.load(open("grafana/provisioning/dashboards/daily.json"))["panels"] if p["id"]==10)["targets"][0]["query"]
print(q)' | docker exec -i monitor-air-influxdb influx query --org monitor-air --raw -f /dev/stdin 2>/dev/null \
  | python3 -c '
import csv,sys
r=[x for x in csv.reader(sys.stdin) if x and not x[0].startswith("#")]
if not r: sys.exit(0)
h=r[0]; rows=[dict(zip(h,x)) for x in r[1:] if len(x)==len(h)]
due=[(x["plant_id"],float(x["depl_pct"]),float(x["days"])) for x in rows
     if x.get("basis")=="平台" and float(x["depl_pct"])>=100.0]
due.sort(key=lambda t:-t[1])
for p,d,a in due: print(f"  {p}  {d:.0f}%  {a:.1f} 天")
')"
NDUE="$(grep -c . <<<"$PANEL" || true)"

TEXT="🌵 每週 recall：澆水指標

平台期曲線已重新擬合：${NFIT} 盆有自己的 A/τ。
未擬合（沿用舊分母）：${SKIP:-無}

已過平台期、還沒澆（分母=平台，≥100%）：${NDUE} 盆
${PANEL:-  無}

請做這三件事：
1. cd ~/monitor-air/broker && ./analyze-et.py --index
2. 看 D 節：control rate vs its OWN days-since-watering 的 r 有沒有掉到 |r|<0.5
   （沒有 = 對照盆還在自己的節奏上，蒸散修正仍不可用）
3. 對照 WATERING-INDEX.md 頂部的結論 —— 有變就改它。

停掉這個提醒：crontab -e 刪掉 plateau-review-reminder 那行。"

if [ "$DRY" = 1 ]; then echo "$TEXT"; exit 0; fi
TOKEN="$(envval TELEGRAM_BOT_TOKEN)"; CHAT="$(envval TELEGRAM_CHAT_ID)"
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env" >&2; exit 1; }
curl -sS -o /dev/null --data-urlencode "text=$TEXT" --data "chat_id=$CHAT" \
  "https://api.telegram.org/bot$TOKEN/sendMessage"
echo "sent:"; echo "$TEXT"
