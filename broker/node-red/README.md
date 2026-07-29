# Node-RED — phone control for leaf-reflectance measurements

A small control surface for triggering on-demand AS7341 reflectance measurements from a
phone, tagged with which plant. It talks to the firmware's reflect command seam
(`monitor-air/<dev>/reflect/cmd` + `.../reflect/state|result|availability`).

**Why Node-RED (and not a plain MQTT app):** the firmware **dedups by `request_id`** — a
resend of the same id returns `dedup` and does NOT re-measure. Node-RED generates a fresh
`request_id` **server-side** on every trigger and publishes a **non-retained** `reflect/cmd`,
which a fixed-payload app button can't do.

## Start

```bash
cd broker
docker compose up -d --build node-red
```

The editor + dashboard are bound to **localhost only** (`127.0.0.1:1880`). Reach them over
Tailscale / an SSH tunnel.

⚠️ **Do NOT expose port 1880.** The Node-RED **editor**, the `/ui` dashboard, and the
`/reflect/measure` endpoint all share port 1880 with **no auth**, and the broker is
anonymous-open — so anyone who reaches 1880 can not only trigger measurements but also open
the editor and rewrite the flow. If you must reach it remotely, front it with a reverse proxy
+ auth; longer term add Node-RED `adminAuth`/`httpNodeAuth` and lock the broker down
(`allow_anonymous false` + a password file).

Not one-command deployable: the service starts with an **empty** flow — you must import
`flows.json` once (below). It is not auto-loaded.

## Load the flow

Open `http://127.0.0.1:1880` → menu (☰) → **Import** → select `flows.json` from this folder →
**Deploy**. (The `node-red-dashboard` palette is baked into the image by the Dockerfile.)

> ⚠️ **Set your device id.** The starter flow targets `staging-01`. To point it at the balcony
> node, change `staging-01` in the two **function** nodes (`build cmd …`) and the three
> **mqtt in** topic fields (`reflect/state|result|availability`), then Deploy.

## Use it

**Dashboard** — `http://127.0.0.1:1880/ui` (add to your phone home screen for an app feel):
pick a **Plant**, press **Measure**. The page shows `Device` (availability), `State`
(idle/measuring), and the `Last result`.

**Shortcuts (roving, one tap per plant)** — POST to the HTTP endpoint; the server generates the
`request_id` and returns it:

```bash
curl -X POST 'http://127.0.0.1:1880/reflect/measure?plant=cactus-03'
# -> 202 {"status":"accepted","request_id":"r-...","plant":"cactus-03"}
```

Make one iOS Shortcut / Android shortcut per plant (each hits the URL with its own `plant`),
put them on the home screen, and tap the right one at each plant.

## Notes / caveats (from the design review)

- **request_id is server-generated** here; never let the phone send a fixed one (→ `dedup`).
- **`reflect/cmd` is non-retained**, QoS 0. Don't enable client offline buffering.
- **`reflect/result` is QoS 0, non-retained.** Node-RED holds it live; if a result is ever
  lost you'll still see `state` return `idle`, just not whether it was `done` or `error`.
- **`reflect/state` (retained) is only `idle|measuring`** — it is not a history of results.
- **Empty `plant` is rejected** (UI shows "pick a plant first"; HTTP returns `400`). Only
  `[A-Za-z0-9_-]` (1–40) is accepted, so a stray tag can't pollute the data.
- **The HTTP `202` is best-effort** — it means the command was queued to the `mqtt out` node,
  NOT that the broker was reachable. If Node-RED↔broker is briefly down the command can be
  lost silently; watch `reflect/state`/`result` to confirm the device actually acted.
- Security: bind to localhost/Tailscale as above; if you expose the HTTP endpoint for
  Shortcuts, put a token / Basic Auth in front. Medium-term, lock the broker down
  (`allow_anonymous false` + a password file) and give the ESP32 + Node-RED their own creds.

## Measurement station (weight) — second flow

The `flows.json` also carries a **Measurement station** tab (Phase 3): it subscribes
`monitor-air/+/measure/event_raw`, resolves the NFC tag UID → `plant_id` via
`tag-map.json`, **acks** the ESP (every copy — the device is at-least-once, up to 5),
and republishes an enriched `plant_weight` (once per `(device, event_id)`, 60 s dedup)
for Telegraf. See `../MEASUREMENT-STATION.md` for the full contract.

- **UID map** — `tag-map.json` here is **bind-mounted** read-only into `/data/tag-map.json`
  (docker-compose) and read by a **`file in` node** on every measurement, so edits are
  **live — no rebuild or volume reset** (unlike this flow file). (A `file in` node, not
  `fs` in a function, so it doesn't depend on `functionExternalModules`.) Add a tag with
  `../add-tag.sh <uid> <plant-id>`, then commit `tag-map.json`.
- Unknown UID → `plant_id="unknown"` with the raw `uid` kept (record is never dropped).
- `plant_id` values MUST match the reflect `plant` ids so weight and spectrum join per plant.

## Persistence / repo drift

Flows live in the `nodered-data` volume after import. Edits in the GUI won't flow back to this
repo — re-export (menu → Export) into `flows.json` if you want them versioned.
