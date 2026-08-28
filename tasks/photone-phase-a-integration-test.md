# Phase A Integrated Test — 流程(broker host 執行)

_Phase A commit `533bf77`。目標:六個 stage 依序過,全過 = Phase A 收工,可開 Phase B。_
_任何一步 FAIL → 停下修復,不往下走(後面的 stage 依賴前面的資料契約)。_
_除 Stage 4f 外全部唯讀或 --dry-run,無資料風險;4f 只寫一筆 photone row(可刪)。_

## Stage 0 — 前提

```bash
# broker host
cd /home/jiarung/monitor-air && git pull
git log --oneline -1        # 預期:533bf77
```
- [ ] `PUBLISH_AMBIENT_SPECTRUM=1` 維持開啟(src/secrets.h:18)。
  ⚠ 該行註解說「校正完 revert 0」—— **與 k-model 管線衝突,收數期永久需要 ambient
  spectrum**,註解該改掉,別真的關。
- [ ] AS7341 供電狀態知悉即可(todo #1 未修不擋:per-target paired 保證 lux 錨定配對存活)。

## Stage 1 — Telegraf(source tag)

```bash
cd /home/jiarung/monitor-air/broker
docker compose up -d --force-recreate telegraf     # 單檔 bind mount,restart 不夠(FLOWS.md)
# inode 一致性:兩個數字必須相同
stat -c %i telegraf/telegraf.conf
docker exec monitor-air-telegraf stat -c %i /etc/telegraf/telegraf.conf
```
驗收(Stage 2 的 checkpoint 會立刻給一筆帶 source 的 row,先做 Stage 2 再回來查也行):
```bash
docker exec -e INFLUX_TOKEN monitor-air-influxdb influx query --org monitor-air '
from(bucket:"sensors") |> range(start:-2h)
  |> filter(fn:(r)=> r._measurement=="light" and r._field=="on")
  |> keep(columns:["_time","_value","location","source"]) |> tail(n:5)'
```
- [ ] PASS = 新 rows 同時有 `location`、`source` 欄(source ∈ auto|manual|seed|checkpoint)且 `on` 為 1/0

## Stage 2 — light.py(checkpoint)

```bash
cd /home/jiarung/monitor-air/broker
docker compose build light && docker compose up -d --force-recreate light
docker exec monitor-air-light python3 light.py --selftest    # 或容器內對應路徑
docker logs -f monitor-air-light | head -30
```
- [ ] `--selftest` 印 `selftest OK`(host 有 aiomqtt/kasa,不用 stub)
- [ ] **重啟即觸發首次 checkpoint**(現在 >03:00 Taipei,`should_checkpoint(last_date=None)` 成立):
      log 出現 `checkpoint: plug=ON|OFF`
- [ ] Influx `light` 出現 `source=checkpoint` 的 row(用 Stage 1 的查詢)
- [ ] 隔天 03:00 後再查一次:每日恰一筆 checkpoint(排除重複觸發)

## Stage 3 — Firmware(gain/tint_ms 量測事實)

```bash
# Mac 上燒錄;serial 燒錄記得 DOUT mode(off-brand flash),OTA 則照現行 env
pio run -e ota -t upload
```
```bash
# broker host:新 rows 帶 config identity
docker exec -e INFLUX_TOKEN monitor-air-influxdb influx query --org monitor-air '
from(bucket:"sensors") |> range(start:-10m)
  |> filter(fn:(r)=> r._measurement=="spectrum" and (r._field=="gain" or r._field=="tint_ms"))
  |> keep(columns:["_time","_field","_value"]) |> tail(n:6)'
```
- [ ] PASS = `gain=4`、`tint_ms=280.78`(每筆 ambient 都有)
- [ ] **reclaim 驗證**:跑一次 reflect(既有 burst 流程)後,下一筆 ambient 的 gain 仍 =4
      (setGain 驗證 + 每讀重設都在管;若出現 64 → FAIL,立刻回報)

## Stage 4 — record-photone.sh 六案例(核心)

全部在 broker host `broker/` 下跑;a–e 用 `--dry-run`(需 token 但不寫入)。

**a. 正常時刻推導**
```bash
./record-photone.sh --ppfd 42 --lux 2100 --dry-run
```
- [ ] 輸出含 `lamp=ON|OFF (last light row …) sun_alt=… → source=…  location=livingroom`
- [ ] source 與你人工核對一致(現在燈態×白天/晚上)
- [ ] 顯示 `telemetry gain=4/tint=280.78ms`(Stage 3 之後才會有;之前顯示 e0-legacy 屬正常)

**b. 歷史時刻 ×2(一開一關)**
從 Influx 挑兩個已知時刻(白天燈關、晚上燈開):
- [ ] `--at <白天燈關時刻> --dry-run` → `source=daylight`
- [ ] `--at <晚上燈開時刻> --dry-run` → `source=lamp`

**c. UNKNOWN(>26h 無 light row / t 前無 row)**
挑 light controller 上線前的時刻(如 `--at 2026-06-01T04:00:00Z`):
- [ ] 無 `--source` → 拒收,錯誤訊息含 "lamp state UNKNOWN … pass an explicit --source"
- [ ] 加 `--source daylight --dry-run` → 收,警告 `source from --source override`,line protocol 有 `source_override=1`

**d. 矛盾中止**
用 b 的燈開時刻:
- [ ] `--at <燈開時刻> --source daylight --dry-run` → 中止,同時顯示 derived 與 --source 兩者

**e. 燈關∧夜間拒收**
挑一個夜裡燈已關的時刻:
- [ ] → `measurement rejected: lamp off at night — no light source to measure`

**e2. ref-pair(燈開白天,Photone 貼 ref 位)**
```bash
./record-photone.sh --ppfd <v> --lux <v> --ref-pair --dry-run    # 挑燈開的白天時刻
```
- [ ] 標頭出現 `[ref-pair]`;警告「ref-pair under lamp-on … ref evidence only」
- [ ] line protocol:`ref_pair=1`、`lux_at=-1`、八通道 -1、`lux_ref_at` 為正常值
- [ ] 報表有 `r_ref = Photone/lux_ref`
- [ ] 反例:夜間燈開 + `--ref-pair` → 拒收("needs daylight");不帶 `--lux` → 拒收
- [ ] 寫入一筆 ref-pair 後看面板:id 12 overlay **不出現**該 Photone 點(ref 位不屬
      植株位曲線);id 13 該 row 的 k_spec/k_lux/lux_div54 顯示 -1(sentinel)而非負比值,
      `ref_pair` 欄=1

**f. 真實寫入一筆(唯一會寫的步驟)**
```bash
./record-photone.sh --ppfd <Photone 實測> --lux <Photone 實測>
```
- [ ] Influx `photone` 新 row 有:`light_location` tag、`lamp_state`、`sun_alt_deg`、
      `source_override=0`、`config_override=0`、`ref_pair`、`gain_x=4`(Stage 3 後)
- [ ] `photone-log.csv` 頭列 = v3 header;舊 header 檔已轉存 `photone-log.pre-<stamp>.csv`
      (含今天稍早寫的 v2 檔;`photone-log.v1.csv` 若存在則原樣未動)
- [ ] 印出的 k_spec/k_lux 數量級合理(k_lux 接近既有經驗值)

## Stage 5 — solar-noon.py --alt-at 抽查

```bash
python3 solar-noon.py                                   # 記下今日正午 alt
python3 solar-noon.py --alt-at $(date -d "12:00 +8 hours ago" +%s 2>/dev/null || date +%s)
```
- [ ] 正午 epoch 餵 `--alt-at` ≈ `solar_noon()` 印的 alt(±0.5°)
- [ ] 子夜 epoch → 負值;清晨/黃昏跨 0° 的符號正確

## Stage 6 — 收尾

- [ ] 六 stage 結果記回本檔(PASS/FAIL + 日期)
- [ ] secrets.h 的 PUBLISH_AMBIENT_SPECTRUM 註解改為「k-model 收數期常開」
- [ ] 全過 → Phase A 收工;收數 SOP 啟動(每週 1–2 筆 daylight 貼 ref 位、晴天中午直射窗刻意收)
- [ ] 硬體線(獨立,不擋 Phase B 開工):0x5C 盤點拍照 → `mark-epoch.sh epoch` 首筆回填;canopy 標記

## 已知豁免(裁定 pass,2026-08-28)
PPFD 空值錯誤訊息不精確、`--tint-ms` override 值被 telemetry 蓋回(旗標仍正確)。
(CSV 輪替覆蓋問題已於 ref-pair 修訂一併修正:輪替改用時間戳檔名,永不覆蓋。)
