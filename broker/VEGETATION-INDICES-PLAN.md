# Plan: Vegetation-index monitoring — NDVI / NDRE / CIred-edge

## Context
The reflectance pipeline (phone → dark/LED/lit → `net_*` per plant, published to
the `spectrum` topic, tagged `plant`/`mode=reflect`) gives raw per-band counts but
**no health metric**. The goal: monitor plant health/stress over time with the
standard vegetation indices, using the **existing hardware** — no new sensors.

**Hardware fit (verified):** the **AS7263** 6-band NIR sensor covers exactly the
bands these indices need — `n680` (red), `n730` (red-edge), `n760/n810/n860`
(NIR). A real reflectance read (test-01) gave net_n680=382, n730=516, n810=4520 —
the LED provides usable NIR illumination and the AS7263 reads real signal.

**Key design choice — AS7263-only, ratio-based:** all three indices can be
computed from the **single AS7263** (n680, n730, n810). Because they're band
**ratios under the same LED on one sensor**, they're robust to the two problems
found below (cross-sensor aim, missing white-NIR). Perfect for **relative
monitoring** (each plant vs its own baseline over time) — which is what detects
health *changes*.

> **Status 2026-08-06 — blocked on hardware, not on this plan.** The AS7263 does
> not currently aim at a leaf, so a re-scan would produce the same empty NIR the
> constraints below describe. The optics/mount are being redesigned; nothing here
> is actionable until that lands, and the first reflectance data taken afterwards
> is the real baseline.
>
> Two things settled since this was written, both reinforcing the AS7263-only
> choice: the AS7341 has **no band between 680 nm and its broad NIR channel**, so
> NDRE and CIre are not merely inconvenient on it but impossible; and the 47
> plant-tagged reflectance readings from 2026-07-03 are unusable regardless —
> they predate the AS7263 wiring (no `n730`) *and* a lens clean around 07-22 (⚠ magnitude disputed — `PPFD-CALIBRATION.md` records ~4×;
> see [`docs/incidents/2026-07.md`](../docs/incidents/2026-07.md#0722)) that
> moved the optical path by ~2.2×, so they are not comparable with anything taken
> later.

## Constraints found (from InfluxDB, 2026-07-22)
1. **The 12 real cacti have NO NIR data.** Only `cactus-01`, `test-01`, `white-ref`
   ever got `net_n810`; the 2026-07-03 full scan predates the AS7263 wiring
   (commit 6368000, same day). → **A fresh full re-scan with NIR verified is a
   prerequisite** for any index on the real plants.
2. **white-ref NIR = 0** (net_n680/n730/n810 all 0, though nir_valid=1) while its
   visible works (net_f555=211). Likely the white card sat in the AS7341's field
   of view but not the AS7263's (separate sensors, separate aim/LED throw). → NIR
   **white-normalization is not usable yet**; the ratio indices sidestep it.
3. **NIR reads flap** (nir_valid toggles 0/1 across samples). → gate on
   `nir_valid == 1` and average the burst samples.

## The indices (AS7263 bands)
| index | formula | bands | detects |
|---|---|---|---|
| **NDVI** | (n810 − n680)/(n810 + n680) | NIR, red | greenness / biomass |
| **NDRE** | (n810 − n730)/(n810 + n730) | NIR, red-edge | chlorophyll / early stress |
| **CIre** | (n810 / n730) − 1 | NIR, red-edge | chlorophyll content (most sensitive) |

Cacti are not thin leaves, so **absolute** NDVI semantics don't transfer — track
**each plant relative to its own baseline**; a downward NDRE/CIre trend flags
stress earlier than visible change.

⚠️ **Baselines are only valid within a fixed capture setup.** Raw-net ratios are
robust to a constant offset but NOT to a **multiplicative spectral bias** — a
change in LED drive, AS7263 gain/integration time, distance, or sensor position
shifts the indices for real (codex). So: **reset a plant's baseline whenever the
capture setup changes** (same lesson as the AS7341 gain/tint that bit the PPFD
calibration). Record the setup with each scan.

## Approach — compute in Flux, no new storage/ingest
Follow the project pattern (PPFD/DLI/k_spec are all Flux-computed in the panel from
stored raw). The `net_n*` are already in InfluxDB via the existing Telegraf spectrum
input — **no telegraf change, no firmware change, no new measurement**. Indices are
derived in the Grafana panel queries from stored `net_n680/n730/n810`, gated on
`nir_valid == 1`. Raw stays the source of truth → any formula/white-ref refinement
later just re-derives.

## Changes

1. **`broker/grafana/provisioning/dashboards/air.json`** — two new panels:
   - **id 15 "Vegetation indices — latest per plant" (table)**: for each `plant`,
     the latest reflect read (nir_valid=1), pivot n680/n730/n810 → columns NDVI,
     NDRE, CIre. One row per plant, sortable. Placement y=78, full width.
   - **id 16 "Vegetation index over time (per plant)" (timeseries)**: NDVI/NDRE/CIre
     history per plant — the baseline-tracking view. y=86.
   Flux sketch (table). Compute the index **per valid sample**, then take the
   **median per plant over the latest burst** (not a single `last()`) so one
   flapped/noisy sample can't set a plant's row (resolves the "average the burst"
   note — design & prereq agree):
   ```flux
   from(bucket:"sensors") |> range(start:-90d)
     |> filter(fn:(r)=> r._measurement=="spectrum" and r.mode=="reflect"
          and (r._field=="net_n680" or r._field=="net_n730" or r._field=="net_n810" or r._field=="nir_valid"))
     |> pivot(rowKey:["_time","plant"], columnKey:["_field"], valueColumn:"_value")
     // gate: valid NIR + all three bands strictly positive (dark-subtracted counts can go negative)
     |> filter(fn:(r)=> r.nir_valid==1.0 and r.net_n680>0.0 and r.net_n730>0.0 and r.net_n810>0.0)
     |> map(fn:(r)=>({ r with
          NDVI:(r.net_n810 - r.net_n680)/(r.net_n810 + r.net_n680),
          NDRE:(r.net_n810 - r.net_n730)/(r.net_n810 + r.net_n730),
          CIre:(r.net_n810 / r.net_n730) - 1.0 }))
     // sanity-bound: NDVI/NDRE ∈ [-1,1] by construction; drop anything outside (bad row)
     |> filter(fn:(r)=> r.NDVI >= -1.0 and r.NDVI <= 1.0 and r.NDRE >= -1.0 and r.NDRE <= 1.0)
     |> group(columns:["plant"]) |> median(column:"NDVI")   // (repeat per index, or aggregate the latest burst)
   ```
   Verified data facts (2026-07-22): the three `net_n*` fields share one exact
   `_time` (single multi-field point → pivot is sound); `nir_valid` is numeric
   (stringifies "0"/"1" → `==1.0` works). Both were codex concerns, checked OK.

2. **`broker/README.md`** *(doc)* — a short "Vegetation indices" note: the three
   indices, AS7263-only rationale, relative-baseline caveat, how to (re)measure.

## Prerequisites (physical — user, before the panels show real data)
1. **Fresh full re-scan** of the 12 cacti via the phone /ui → Measure, confirming
   `nir_valid=1` per plant (re-measure if it flapped to 0). This is the same
   per-plant reflect flow already built — just needs NIR to land this time.
2. *(Optional, for absolute reflectance later)* fix the **white-ref NIR**: aim the
   white card into the **AS7263's** field of view and re-measure so net_n* > 0.
   Not needed for the relative ratio indices; needed only if we later want
   white-normalized absolute reflectance.

## Future (deferred — after baselines exist)
- **Per-plant baseline + drift alert**: once each plant has N good reads, alert on a
  sustained NDRE/CIre drop vs its own baseline (relative, no absolute threshold).
  Same spirit as `SENSOR-DRIFT-DESIGN.md`. Needs a regular re-measure cadence.
- **White-ref normalization** (visible + fixed NIR) → absolute reflectance, if a
  cross-plant absolute comparison is ever needed.

## Verification
1. **Before panels**: confirm the Flux math on the one good NIR read (test-01,
   05:47:06Z 2026-07-16): net_n680=382, n730=516, n810=4520 →
   NDVI=(4520−382)/(4520+382)=**0.844**, NDRE=(4520−516)/(4520+516)=**0.795**,
   CIre=4520/516−1=**7.76**. Query the panel Flux over that window → same numbers.
2. Each air.json edit: `python3 -c json.load` + `docker compose restart grafana` +
   "finished to provision dashboards" no error.
3. After a fresh cacti scan: table shows a row per re-measured plant with sane
   indices (healthy vegetation NDVI ~0.7–0.9); the time panel starts a baseline.
4. codex review the panel Flux before commit (standing rule).

## Notes / ponytail
- **No code, no new service, no schema change** — two Grafana panels reading data
  that already flows. Indices are 3 lines of Flux each.
- AS7263-only keeps it robust: skips the cross-sensor (AS7341↔AS7263) aim mismatch
  and the unusable white-NIR. `# ponytail: raw-net ratios, not white-normalized —
  fine for per-plant trend; white-ref refinement only if absolute is ever needed.`
- Gate every index on `nir_valid==1` and positive denominators, or one bad read
  poisons a plant's row.
