#!/usr/bin/env bash
# Configure the Telegram contact point + notification route for the deadman alert
# via Grafana's API. We use the API (not file provisioning) because file
# provisioning coerces an env-interpolated numeric chat id into a number and
# Grafana's telegram integration rejects it. Idempotent — safe to re-run after
# changing the token. Reads secrets from broker/.env (gitignored). Needs jq.
set -euo pipefail
command -v jq >/dev/null || { echo "needs jq" >&2; exit 1; }
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # broker/
get() { grep -E "^$1=" "$DIR/.env" | head -1 | cut -d= -f2-; }

GRAFANA="${GRAFANA_URL:-http://localhost:3001}"
PW="$(get GF_SECURITY_ADMIN_PASSWORD)"
TOKEN="$(get TELEGRAM_BOT_TOKEN)"
CHATID="$(get TELEGRAM_CHAT_ID)"
UID_CP="telegram-deadman"
[ -n "$TOKEN" ] && [ -n "$CHATID" ] || { echo "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in broker/.env first." >&2; exit 1; }

# Auth via header (keeps the password out of argv); body via stdin (out of argv).
AUTH_HDR="Authorization: Basic $(printf 'admin:%s' "$PW" | base64 | tr -d '\n')"

# fail loudly on any non-2xx (except the PUT's 404, which means "create instead")
api() { # method path  (body on stdin)
  local method="$1" path="$2" code
  code=$(curl -s -o /tmp/gf_resp -w '%{http_code}' -X "$method" \
    -H "$AUTH_HDR" -H 'Content-Type: application/json' -H 'X-Disable-Provenance: true' \
    --data @- "$GRAFANA$path")
  echo "$code"
}

# The notification body. Without this Grafana falls back to `default.message`,
# which dumps every label as `- k = v` and prints `Value: B=22, C=1` — C being
# the threshold expression's own boolean. The reader's first 100 characters (all
# Telegram shows in a push preview) were metadata, and the one actionable line
# came last. So: render the rule's own sentences and nothing else.
#
# The rules own the prose — summary is the headline, description carries the
# number, its unit and what to do — because only the rule knows the unit. This
# template stays dumb on purpose.
#
# Deliberately absent: the label block, the folder, and the Silence link (400
# characters of URL-encoded matchers, for the one action you least want to hit by
# accident on a phone). Also no timestamp — Telegram already stamps every message,
# and Grafana's container has no TZ set, so .StartsAt would print UTC.
#
# GeneratorURL comes from GF_SERVER_ROOT_URL (broker/.env). Unset, it is
# http://localhost:3000/ — wrong port, and "localhost" on a phone is the phone.
read -r -d '' MSG <<'EOF' || true
{{ range .Alerts.Firing }}{{ if eq .Labels.severity "critical" }}🔴{{ else }}🟡{{ end }} {{ .Annotations.summary }}
{{ .Annotations.description }}
{{ .GeneratorURL }}

{{ end }}{{ range .Alerts.Resolved }}✅ 已恢復：{{ .Annotations.summary }}

{{ end }}
EOF

cp_body=$(jq -n --arg uid "$UID_CP" --arg token "$TOKEN" --arg chatid "$CHATID" --arg msg "$MSG" \
  '{uid:$uid, name:"telegram", type:"telegram", settings:{bottoken:$token, chatid:$chatid, message:$msg}, disableResolveMessage:false}')

code=$(printf '%s' "$cp_body" | api PUT "/api/v1/provisioning/contact-points/$UID_CP")
if [ "$code" = "404" ]; then
  code=$(printf '%s' "$cp_body" | api POST "/api/v1/provisioning/contact-points")
fi
case "$code" in 2*) ;; *) echo "contact point setup failed (HTTP $code): $(cat /tmp/gf_resp)" >&2; exit 1;; esac

# group_by used to list `_field`, which is not a label on anything: the deadman
# rule deliberately carries the field name as plain `field` (carried as `_field`
# Grafana reads it as the frame's field NAME and the rule dies — there is a
# comment about it in rules.yaml.tmpl). So that entry silently did nothing.
# Dropping it rather than correcting it to `field`: grouping by device means one
# message listing every dead field on a board, instead of six messages for one
# unplugged sensor. The template ranges over the group, so they all show.
pol_body='{"receiver":"telegram","group_by":["alertname","device"],"group_wait":"30s","group_interval":"5m","repeat_interval":"6h"}'
code=$(printf '%s' "$pol_body" | api PUT "/api/v1/provisioning/policies")
case "$code" in 2*) ;; *) echo "policy setup failed (HTTP $code): $(cat /tmp/gf_resp)" >&2; exit 1;; esac

rm -f /tmp/gf_resp
echo "OK: telegram contact point + route configured."
echo "Test delivery:  curl -s \"https://api.telegram.org/bot\$TELEGRAM_BOT_TOKEN/sendMessage\" -d chat_id=\$TELEGRAM_CHAT_ID -d text=monitor-air-test"
