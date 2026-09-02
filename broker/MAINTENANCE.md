# Maintenance — what breaks here, and how to notice

This file is about **keeping the thing running**, not about what it does
(`FLOWS.md`) or how to query it (`QUERYING.md`). It exists because the same four
failure classes keep recurring, and every one of them **looks like nothing is
wrong** from the outside.

It is a framework, not a finished document. Add to it when something bites you.

---

## The five ways this system lies to you

Ranked by how many times each has actually happened.

### 1. A scheduled job that never runs (3×)

Nothing alerts on a cron entry that isn't there. The data just quietly stops
being produced, and every dashboard built on it renders an empty panel that
looks like "no data yet".

| when | job | why |
|---|---|---|
| 2026-07-29 | `backup/influx-backup.sh` | documented cron line was never installed; one 20 KB backup existed, from when the DB was nearly empty |
| 2026-08-22 | `publish-weight-ref.sh` | the entry existed but contained a literal `/home/<user>/` placeholder and a `/data/` log path that does not exist — it had never run once |
| 2026-08-31 | `compute-k-models.sh` | never scheduled at all. The whole k-model pipeline (`k_model` / `k_adopted` / `light_context`) had produced **zero rows**, and both k-* dashboards were empty |

**Cron is not in version control.** The canonical list is here; each script also
states its own line in its header. Diff them against reality:

```bash
crontab -l | grep monitor-air
```

Expected (5 entries, host timezone is **UTC** — see the timing trap below):

```cron
30 19 * * *  backup/influx-backup.sh          # 03:30 Taipei
15 0  * * *  ppfd-cal-daily.sh                # 08:15 Taipei
5  *  * * *  publish-weight-ref.sh            # hourly
10 *  * * *  compute-k-models.sh              # hourly
0  1  * * 1  cal-review-reminder.sh           # Mon 09:00 Taipei
```

Use **account-absolute paths** and a `/tmp` log. `"$HOME"` happens to work
because cron sets HOME, but the placeholder incident is why the convention is
"write it out in full".

Checking a job ran is not the same as checking it *worked* — see below.

### 2. "It works in my shell" (3×)

**Cron does not run your shell.** No pyenv, no anaconda, a minimal PATH.

```bash
which python3            # yours — probably 3.11 via pyenv/anaconda
env -i PATH=/usr/bin:/bin python3 -V     # what cron gets — 3.8.10 here
```

That six-year version gap has broken three things:

- `zoneinfo` does not exist before 3.9 → `solar-noon.py` uses `TZ` + `time.tzset()`.
- `datetime.fromisoformat` accepts only **3 or 6** fractional digits before 3.11.
  InfluxDB returns **9** (nanoseconds), so every `_time` parse raised
  `ValueError: Invalid isoformat string`. `compute-k-models.py` crashed on this
  from its very first scheduled run — after a manual run had "proved" it worked.
  It now normalises the fraction in `_iso()`.
- A one-digit fraction (`.5`) fails too, for the same reason. Pad *and* truncate.

**Test every cron change the way cron will run it:**

```bash
env -i PATH=/usr/bin:/bin HOME="$HOME" bash -c "cd $PWD && ./the-script.sh"
```

### 3. A write that reports success and stores nothing (1×, twice over)

```python
INFLUX = ["docker", "exec", "-e", "INFLUX_TOKEN", "monitor-air-influxdb", "influx"]
#                            ^^^ missing -i
```

`docker exec` **without `-i` does not attach stdin**. `influx write` then reads
EOF immediately, writes nothing, and **exits 0**. Reads were unaffected because
the query path passes Flux as an *argument*, so the pipeline looked healthy:
queries returned data, `compute-k-models.sh` printed "10 bucket(s) written", and
the bucket held nothing across all of 1970–2200.

The same constant sat in `ack-k-hold.sh` — the script that releases the ±10%
adoption brake. `--accept` would have done nothing, silently.

Proof, same token, only `-i` differing:

```
without -i   exit=0   0 points landed
with    -i   exit=0   1 point  landed
```

`mark-weight.sh` avoids this entirely with `docker cp` + `--file`, which is
worth copying for anything large.

**`influx delete` has the mirror-image trap:** a predicate that matches nothing
also exits 0. Always count before and after:

```bash
cnt() { ./influx-q.sh "from(bucket:\"sensors\")|>range(start:$2,stop:$3)
  |>filter(fn:(r)=>r._measurement==\"plant_weight\" and r.plant_id==\"$1\")
  |>group()|>count()|>keep(columns:[\"_value\"])"; }
```

Predicates can only test **tags**, never fields — `uid` is a field, so it cannot
be selected on. Bracket the exact nanosecond range instead.

### 4. One row's normal state kills the whole batch (2×, both today)

`publish-weight-ref.sh` twice treated an ordinary condition as fatal:

- Retired ids (repotted; their tag moved to the successor) have history but no
  tag. That warned per plant and exited 4 — five lines an hour that **no action
  could ever clear**, because the list only grows with each repot.
- A pot with no watering event yet has no saturation reference at all (`sat` is
  null). `float('')` aborted the run, so **one new pot stopped all 18 refs from
  publishing**, for three consecutive hours.

When a batch job meets a row it cannot use, ask whether that row's state is
*normal*. If it is, skip it with one line and carry on. Reserve the hard exit
for states that are genuinely impossible.

---

### 5. A config that renders, but never loads (1×)

Found 2026-09-02: the two k-model alert rules — the ones the entire Phase C
adoption brake relies on for a human to ever be told — had **never been loaded**.
They went into the template on 08-29 (`4d27655`); Grafana was still serving the
six rules rendered on **08-22**. Eleven days of a brake that engages silently.

Two independent halves, and both must happen:

1. **`rules.yaml` is gitignored**, rendered from `rules.yaml.tmpl` by `start.sh`.
   Editing the template changes nothing until that runs. Same root as §1 — the
   live state is not in version control, so the drift produces **no diff**:
   `git status` is clean while the running system is stale.
2. **Grafana reads `provisioning/alerting/` only at startup.** Dashboards have a
   directory watcher; alerting does not. And `start.sh`'s own
   `docker compose up -d` will **not** recreate Grafana when `docker-compose.yml`
   is unchanged — so running `start.sh` alone renders the file and leaves it
   unloaded. The recreate is a separate step, and it is the half that is easy to
   miss because the render step looks like it succeeded.

Check the file against the template, then Grafana against the file. The second
check is the one that catches half 2:

```bash
grep -c '^ *- uid:' grafana/provisioning/alerting/rules.yaml{.tmpl,}   # must match
curl -su "admin:$GF_SECURITY_ADMIN_PASSWORD" \
  localhost:3001/api/v1/provisioning/alert-rules | jq length            # must match too
```

**Loading is still not firing.** Both k-model rules carry `noDataState: OK`,
which is correct here — "no events" and "no held bucket" are the healthy states,
unlike a deadman where NoData means the query broke. But it keeps §5's cousin
alive: a query that breaks by returning *empty* rather than erroring reports
healthy, and `execErrState: Error` only covers hard errors. The way to gain
confidence without waiting for a real event is to run the rule's own Flux with
only its state predicate relaxed, and check the output *shape* — labels present,
`_time` fresh. FLOWS.md gap 5 is what that shape protects against.


## Deployment: what a change actually requires

Editing the file is rarely enough.

| you changed | required | why |
|---|---|---|
| `grafana/provisioning/dashboards/*.json` | nothing (~10 s auto-reload) | verify by fetching `/api/dashboards/uid/...`, not by eye |
| `telegraf/telegraf.conf` | `docker compose up -d --force-recreate telegraf` | single-file bind mount, attached by **inode** |
| `node-red/tag-map.json` | same force-recreate | same inode trap |
| `node-red/flows.json` | `add-plant.sh` does it: rebuild image + `docker volume rm broker_nodered-data` + up | flows live in the volume, not the mount |
| `grafana/provisioning/alerting/rules.yaml.tmpl` | `bash start.sh` **then** `docker compose up -d --force-recreate grafana` | rendered to a gitignored file; alerting provisioning is read at startup only (§5) |
| `control/light.py` | `docker compose build light && docker compose up -d --force-recreate light` | baked into the image (`build: ./control`) |
| any `*.sh` / `*.py` in `broker/` | nothing | run from the host |
| `src/` (firmware) | flash from the dev host | not this machine |

**The inode trap is the one that keeps recurring** — `FLOWS.md` gap 3 has the
worked example. Note that **git counts as a rename-on-save editor**: any
`checkout` / `pull` / `reset` / cherry-pick that touches those two files detaches
the mount, with identical content, so nothing looks wrong. Verify:

```bash
[ "$(stat -c %i telegraf/telegraf.conf)" \
  = "$(docker exec monitor-air-telegraf stat -c %i /etc/telegraf/telegraf.conf)" ] \
  && echo attached || echo DETACHED
```

Grafana runs on **3001** (3000 is an unrelated app on this host).

---

## Routine checks

Nothing here is automated yet. Roughly in order of how cheaply it catches a real
problem.

**Did the scheduled jobs run — and succeed?** A fresh log mtime only proves it
started.

```bash
for l in /tmp/k-models.log /tmp/ppfd-cal-daily.log /tmp/publish-weight-ref.log \
         /tmp/cal-review-reminder.log broker/backup/backup.log; do
  printf '%-34s %s\n' "$l" "$(date -r "$l" '+%m-%d %H:%M')"; tail -2 "$l"; done
```

**Are the alert rules that are in git actually loaded?** Three counts that must
all agree — template, rendered file, and what Grafana is really running. The
third is the only one that reflects reality (§5):

```bash
grep -c '^ *- uid:' grafana/provisioning/alerting/rules.yaml{.tmpl,}
curl -su "admin:$GF_SECURITY_ADMIN_PASSWORD" \
  localhost:3001/api/v1/provisioning/alert-rules | jq length
```

A mismatch produces no diff and no alert — by construction, the thing that would
have told you is the thing that is missing.

**Is every sensor still answering?** The health topic re-probes live, it is not
a boot snapshot:

```bash
docker exec monitor-air-mqtt mosquitto_sub -t 'monitor-air/+/health' -C 1 -W 20
```

`hx711: 0` is **normal** — it only samples while a pot is on the scale, so it
reads 0 between weighings. `as7341: 0` means the chip has stopped ACKing on I²C;
before power-cycling anything, take the forensic (below).

**Is the light controller's idea of the plug still true?** It re-reads every
10 minutes and republishes with `source=poll` on drift, so a switch made in the
Tapo app self-corrects. A `poll:` line in the log means it caught one.

---

## Procedures

### Repotting a pot

A repot ends one id and starts its successor: mass changes discontinuously, so
mixing them makes the depletion metric meaningless. Convention is a `b` suffix
(`cactus-03` → `cactus-03b`) and **retired ids are never reused**.

1. **Move the post-repot readings** to the new id. Tags cannot be updated in
   place — write the point again under the new `plant_id` at its **original
   nanosecond timestamp**, then delete the old one. Build the timestamp with
   integer arithmetic (`epoch_s * 10**9 + int(frac.ljust(9,'0'))`); parsing
   RFC3339 fractions as floats loses the nanosecond digits.
2. **Point the tag at the successor** in `node-red/tag-map.json`.
3. **Add the new id** to the dropdown (`./add-plant.sh cactus-03b`), which also
   does the Node-RED rebuild. Leave the old id in the list.
4. **Refresh the references**: `./publish-weight-ref.sh`.

The new pot gets no OLED reference and does not appear in the depletion table
until it has a watering event and a span over 5 g. That is expected.

### "Retired" is derived, not declared

There is no retired flag, and staleness cannot substitute for one — a pot
replaced yesterday has a *recent* last reading, indistinguishable from a pot
that simply has not been watered.

The panels use **tag ownership** instead: one NFC tag is on exactly one pot, so
whichever `plant_id` owns that uid's most recent reading is the pot that still
exists. Repotting hands the tag to the successor, and the old id stops owning
any uid. That is the observable definition, and the data is already in the
database — see the `live` block in `daily.json` panels 10 and 14. It agreed
exactly with `tag-map.json` (26 of 26) the day it was written.

### Capturing an AS7341 failure

The failure leaves WiFi alive, so **take the evidence before cutting power**:

```bash
docker exec monitor-air-mqtt mosquitto_sub -t 'monitor-air/staging-01/diag/out' -W 40 &
docker exec monitor-air-mqtt mosquitto_pub -q 1 \
  -t 'monitor-air/staging-01/diag/cmd' -m 'as7341'
```

Every line names the exact step. The legend in the output distinguishes
bus/silicon (NACK) from a chip not holding state (readback mismatch) from AGC
corruption. A 2026-08-31 capture — 561 failures, 0 clean transactions, all
address NACK, with AS7263 down at the same time and every other I²C device fine
— is in `tasks/as7341-forensic-2026-08-28T1546.txt`.

⚠️ Running a reflect measurement (64× gain) at an **unshaded** bright sky is what
triggered that failure. Shade the sensor before exercising the reflect path.

---

## Related

- `FLOWS.md` — every data path and whether it is actually running; gap 3 is the bind-mount trap
- `QUERYING.md` — four query traps, each of which has already produced a wrong conclusion
- `README.md` — setup, the plant registry, the light controller
- `.claude/skills/verify/SKILL.md` — screenshotting Grafana panels, which is the only way to check overrides, threshold colours and column order
