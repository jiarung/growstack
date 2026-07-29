# Plant Measurement Station — server-side spec (Phase 3)

The ESP32-S3 probe (`staging-01`) is an NFC-triggered weight **measurement station** (firmware Phase 2,
committed). This is the **server-side** work: Node-RED resolves the tag → plant identity, acks the device,
and hands a clean record to InfluxDB via Telegraf. **The ESP stays dumb — it only knows the tag UID.**

> **Status: implemented.** Node-RED flow → `node-red/flows.json` (tab "Measurement station");
> UID map → `node-red/tag-map.json` (bind-mounted, register a tag with `./add-tag.sh <plant-id>`
> + a tap, live — no rebuild); Telegraf consumer + Grafana weight panel added. Import the flow once
> (`node-red/README.md`), then `docker compose up -d --build`.

## Flow
```
ESP  ──measure/event_raw──▶  Node-RED  ──measure/ack──▶  ESP   (stops the ESP's retries → OLED "sent")
                                  │  UID → plant_id (git-tracked map)
                                  ▼
                            plant_weight  ──▶  Telegraf  ──▶  InfluxDB  ──▶  Grafana
```
Two topics on purpose: Telegraf subscribes ONLY to `plant_weight`, never `event_raw` → no double-write.

## MQTT contract (fixed by the firmware — do NOT change these)
- **ESP → `monitor-air/<dev>/measure/event_raw`** (QoS0, non-retained):
  ```json
  {"event_id": 1, "uid": "00A8635C", "weight_g": 334.8}
  ```
  - `event_id` is a **per-session counter** (RAM-only; resets to 0 on ESP reboot). **NOT globally unique** —
    dedup by **(device, event_id)** within a short window only.
  - **At-least-once**: the ESP resends the SAME event (same event_id) every **3 s, up to 4 retries**
    (5 copies total) UNTIL it receives a matching ack. So Node-RED WILL see duplicates.
- **Node-RED → `monitor-air/<dev>/measure/ack`** (the ESP subscribes):
  ```json
  {"event_id": 1}
  ```
  - MUST echo the SAME `event_id`. This is what **stops the ESP's retries** and flips its OLED to `sent OK`.
  - **Ack every copy** you receive (idempotent) so the device stops ASAP; but **write to InfluxDB only once**
    per (device, event_id).
  - (Optional, future) the ack MAY also include the resolved plant name — the firmware ignores unknown
    fields today, so `{"event_id":1,"plant_id":"cactus-01"}` is safe and lets a later firmware show the name.

## Node-RED responsibilities
Subscribe `monitor-air/+/measure/event_raw`. Per message:
1. Parse `event_id`, `uid`, `weight_g`; get `<dev>` from the topic.
2. **Dedup**: keep a recent-seen set of `(dev, event_id)` with a ~60 s TTL (flow context is fine for this —
   it's ephemeral by nature, unlike the tag map). If already seen → **still send the ack** (step 4) but
   **skip** steps 3+5 (no re-lookup, no re-write).
3. **UID → plant_id**: look up `uid` in the tag map (below). Unknown UID → use `plant_id = "unknown"` and
   keep the raw `uid` as a field so the record isn't lost + the missing registration is visible (don't drop it).
4. **Ack**: publish `{"event_id": <id>}` to `monitor-air/<dev>/measure/ack` (QoS0). Do this for first receipts
   AND retries.
5. **Republish enriched** (first receipt only): publish to `monitor-air/<dev>/plant_weight`:
   ```json
   {"plant_id": "cactus-01", "uid": "00A8635C", "weight_g": 334.8}
   ```

## UID → plant_id map (version-controlled — NOT flow/global context)
Keep it in git (survives rebuilds, reviewable). ⚠️ The Node-RED container mounts only the named volume
`/data`; a repo file under `broker/node-red/` is NOT visible inside unless you mount it. Do ONE of:
- **Bind-mount (recommended, live-editable):** in docker-compose.yml, node-red service →
  `volumes: - ./node-red/tag-map.json:/data/tag-map.json:ro`. Node-RED reads `/data/tag-map.json`.
- **Seed via the Dockerfile** (like flows.json): `COPY tag-map.json /data/tag-map.json` — but a named volume
  is only seeded when EMPTY, so map edits then need `docker volume rm broker_nodered-data` (same caveat as
  flows.json). The bind-mount avoids this.

Format (`tag-map.json`):
```json
{
  "00A8635C": "cactus-01",
  "04A1B2C3": "cactus-02"
}
```
Node-RED reads it (file-in node) on every measurement, so a new tag is live on the next weigh — no
redeploy. Register one with `./add-tag.sh <plant-id>`, which waits for a tap and reads the uid off
`measure/event_raw` (uid-as-argument still works). **`plant_id` values MUST match the existing reflect `plant` ids** (the Node-RED
dropdown list) so weight and spectrum join on one plant. Missing file / bad JSON / unknown UID → don't
crash: `plant_id="unknown"`, keep `uid`, still ack.

## Telegraf — add a `plant_weight` consumer
Mirror the existing telemetry consumer; only `plant_weight` is ingested (never `event_raw`).
**Repo-ready** (Telegraf and Mosquitto are separate containers, so `servers` is required, like the
existing consumers):
```toml
[[inputs.mqtt_consumer]]
  servers = ["tcp://mosquitto:1883"]   # REQUIRED — same broker as the other consumers
  topics = ["monitor-air/+/plant_weight"]
  data_format = "json"
  name_override = "plant_weight"
  tag_keys = ["plant_id"]              # (+ "measure_type" in Phase 4)
  json_string_fields = ["uid"]         # KEEP uid as a field so an "unknown" plant_id is still traceable
  [[inputs.mqtt_consumer.topic_parsing]]
    topic = "monitor-air/+/plant_weight"
    tags = "_/device/_"
```
→ InfluxDB: measurement `plant_weight`, tags `device` + `plant_id`, fields `weight_g` (float) + `uid`
(string), server timestamp. **`plant_id` MUST use the exact same value domain as the reflect `plant`
tag** (spectrum consumer already tags `plant`) so weight and spectrum join on one plant.

## Grafana
- v1: `weight_g` over time per `plant_id` (time series or a latest-per-plant table).
- Phase 4 (dry/wet): a `measure_type` tag (dry|wet) → **water uptake = wet − dry** per plant + a trend.

## Notes / gotchas
- **Dedup is mandatory** — the ESP is at-least-once (up to 5 copies). Without it you write ~5 points/measurement.
- **Dedup cache is ephemeral** — flow-context dedup is lost on a Node-RED restart; a restart DURING the ESP's
  ~12 s retry window can let one duplicate through. Acceptable (rare); persist the dedup set only if it bites.
- **Ack fast** — the device publishes once, retries every 3 s ×4 (a ~12 s retry window), and declares `UNSENT`
  ~15 s in. Ack on first receipt so the operator sees `sent OK` promptly.
- **`event_id` is session-scoped** — after an ESP reboot the counter restarts at 1, so `(dev, event_id)` can
  repeat across reboots. The 60 s dedup window makes that a non-issue in practice (measurements are seconds
  apart, reboots minutes+). Don't use `event_id` as a long-term primary key.
- **Unknown UID** — store `plant_id="unknown"` + the raw `uid` (both) so the missing registration is visible
  and back-fillable; never drop the record.
- Timestamp = server (Telegraf write) time; the event carries none (publish is near-instant after weighing).
