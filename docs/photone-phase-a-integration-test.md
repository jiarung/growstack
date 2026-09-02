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
- [ ] PASS = 新 rows 同時有 `location`、`source` 欄(source ∈ auto|manual|seed|checkpoint|poll)且 `on` 為 1/0

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

**e2. ref 錨定收錄(燈開白天,Photone 貼 ref 位,`--source daylight`)**
```bash
./record-photone.sh --ppfd <v> --lux <v> --source daylight --dry-run   # 挑燈開的白天時刻(推導=mixed)
```
- [ ] **不視為矛盾**(推導 mixed + `--source daylight` = 合法精化);標頭 `source=daylight  [ref-only]`,
      警告「daylight under a lit lamp (ref anchor) … lux_ref evidence only」
- [ ] line protocol:tag `source=daylight`、`lamp_state=1`、`lux_at=-1`、八通道 -1、
      `lux_ref_at` 為正常值、`source_override=0`(這不是 override)
- [ ] 報表有 `r_ref = Photone/lux_ref`
- [ ] 反例:同時刻不帶 `--lux` → 拒收("needs --lux");夜間燈開 + `--source daylight` →
      矛盾中止(夜間無日光場,= 案例 d)
- [ ] 寫入一筆後看面板:id 12 overlay **不出現**該 Photone 點(daylight∧lamp_state=1 被濾,
      ref 位不屬植株位曲線);id 13 該 row 的 k_spec/k_lux/lux_div54 顯示 -1(sentinel)
      而非負比值,`lamp_state` 欄=1

**f. 真實寫入一筆(唯一會寫的步驟)**
```bash
./record-photone.sh --ppfd <Photone 實測> --lux <Photone 實測>
```
- [ ] Influx `photone` 新 row 有:`light_location` tag、`lamp_state`、`sun_alt_deg`、
      `source_override=0`、`config_override=0`、`gain_x=4`(Stage 3 後)
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

---

# 執行結果 — 2026-08-28（broker host）

| Stage | 結果 | 摘要 |
|---|---|---|
| 0 前提 | PASS* | `secrets.h` 不在本機（dev host 檔案），改以功能佐證：30 min 內 119 筆 ambient spectrum → 旗標確為開啟 |
| 1 telegraf | **PASS** | force-recreate 後 inode 一致（44840094 兩邊相同）；rows 同時帶 `location` + `source` |
| 2 light.py | **PASS** | `selftest OK`；重啟即觸發 checkpoint，Influx 出現 `source=checkpoint`（15:41:32），三種 source 皆現 |
| 3 firmware | 前半 PASS / **後半 FAIL** | `gain=4`、`tint_ms=280.78` 每筆都有 ✓。**reclaim 驗證把 AS7341 打掛了** — 見下 |
| 4 record-photone | **a–e PASS** / f 待你 | device tag 修正後重跑，六案例中的 a–e 全過（見「第二輪」）；f 需要你手邊的 Photone 實測值 |
| 5 solar-noon | **PASS** | 正午 epoch → `--alt-at` = 74.71，與 `solar_noon()` 印的相同；子夜 −54.92、日出前 +4.09、黃昏後 −9.20，跨 0° 符號正確 |
| 6 收尾 | 未完成 | 卡在 3、4 |

## 阻斷問題 1 — AS7341 + AS7263 停止 ACK（reflect 觸發）

**時間軸**
```
15:42:59  ambient 正常（read_ms 613, gain=4），此前連續數十筆皆同
15:43:04  reflect(64x) 對「無遮蔽的午後天空」→ read_ms=1072, saturated=1,
          lit_f590/f630/f680 = 65535（真實飽和，非 I2C 故障值）
15:43 起  ambient 完全停止 — 不是發佈垃圾，是 sensors.cpp:608 的
          setGain 驗證失敗 → 讀數丟棄。此行為正確。
```

**遠端鑑識**（`diag/cmd` ← `as7341`，依 mqtt_client.cpp:57-59 在斷電前取得）
完整輸出：`tasks/as7341-forensic-2026-08-28T1546.txt`

```
=== done: 561 failure(s), 0 clean transaction(s) ===
```
**每一筆交易都是 `err=2`（位址 NACK），沒有任何一次成功。** 依 diag 自己的判讀規則，
NACK/short-read → **bus/silicon**，不是「晶片沒守住狀態」，也不是 gain/AGC 損毀。

`health`: `{"bme":1,"lux":1,"lux_ref":1,"as7341":0,"as7263":0,"hx711":1,"pn532":1}`
→ **兩顆光譜晶片一起失效，其餘 I2C 裝置全部正常。** 這比之前「as7341 單獨掛」
的描述更窄：指向兩者共用的東西（供電軌或匯流排分支），而非 AS7341 本身。

**復原**：需要斷電（證據已取畢）。目前燈是 ON —— 依先前觀察，
「開燈狀態下重新上電」是唯一一致有效的條件。

**文件要補的警告**：Stage 3 的 reclaim 驗證**必須在感測器前放遮蔽物**再跑。
對無遮蔽強光打 64x 會硬飽和，本次即由此觸發失效。

## 阻斷問題 2 — device tag 在 15:36:30 從 `livingroom` 變成 `staging-01`

```
15:36:30  air.lux / spectrum 的 device tag 同時翻轉（韌體 OTA 重燒，
          health 的 reset="software (esp_restart)" 相符）
15:41:08  telegraf force-recreate —— 晚了 4.5 分鐘，不是原因
```
telegraf 新舊設定都是 `tags = "_/device/_"`，且舊設定裡沒有 `livingroom` 字串，
所以是 MQTT topic 的第 2 段變了 = 韌體的 `MQTT_DEVICE_ID` 變了。

**影響範圍**（全部綁 `device == "livingroom"`）
- air.json 面板 7, 12, 17, 18
- daily.json 面板 1, 3, 12, 13
- 告警規則 2 條
- 腳本 7 個：calibrate-ppfd / cal-review-reminder / lamp-hold / light-ctl /
  mark-epoch / ppfd-cal-daily / record-photone
- `.env`: `LIGHT_LOCATION=livingroom`、`LIGHT_SENSOR_DEVICE=livingroom`

Node-RED flows.json 的自述把 `staging-01` 稱為「要改掉的預設值」，
**研判是誤用 staging 的 secrets.h 燒錄**。修法在 dev host：把 `MQTT_DEVICE_ID`
改回 `livingroom` 重燒。不建議反向把上述 8 面板 + 2 告警 + 7 腳本改成 staging-01。


---

# 第二輪 — 2026-08-28 16:1x（斷電復原 + 韌體重燒之後）

兩個阻斷問題都解掉了：
- AS7341 斷電後恢復（`as7341:1.0`、`read_ms 613`、`gain=4`、15 s 一筆）
- device tag 於 16:13:07 回到 `livingroom` —— 8 面板 / 2 告警 / 7 腳本恢復
- `as7263:0.0` 仍在，但斷電前那筆也是 0，**不是今天造成的**；`i2c_n` 由 4→5 的增量剛好只有 AS7341
- `hx711` 沒有持續心跳屬正常（`hx711Poll` 只在 `is_ready()` 時取樣），第一輪的 0.0 判讀有誤，撤回

## Stage 4 — record-photone.sh 六案例

| 案例 | 結果 | 實際輸出 |
|---|---|---|
| a 正常時刻 | **PASS** | `lamp=ON … sun_alt=26.2° → source=mixed location=livingroom`；`gain=4/tint=280.78ms`；`paired=yes`（7+7 樣本）；`gain_x=4.0` |
| b1 白天燈關 | **PASS** | `--at 2026-08-27T22:30:00Z` → `lamp=OFF sun_alt=11.8° → source=daylight`，`lamp_state=0.0` |
| b2 夜間燈開 | **PASS** | `--at 2026-08-26T11:00:00Z` → `lamp=ON sun_alt=-9.7° → source=lamp`，`lamp_state=1.0` |
| c1 UNKNOWN 無 override | **PASS** | `lamp state UNKNOWN at … (no light row within 26h …) — pass an explicit --source to override (it will be flagged source_override=1)` |
| c2 UNKNOWN + override | **PASS** | 收下，`[override]` + `⚠ source from --source override`；line protocol `source_override=1.0`、`lamp_state=-1.0`、`paired=0.0` |
| d 矛盾中止 | **PASS** | `--source 'daylight' contradicts the derived source 'lamp' (lamp_state=1 @ …, sun_alt=-9.7) — if the derivation is wrong, fix the light data, don't overrule it` |
| e 燈關∧夜間 | **PASS** | `measurement rejected: lamp off at night — no light source to measure (lamp_state=0, sun_alt=-22.7)` |
| f 真實寫入 | 待執行 | 需要你手邊的 Photone 實測 PPFD/lux |

`lux_ref` 是很好的旁證：b1 早晨日光 183.5、b2 夜間燈下 25.0 —— 遮燈感測器行為完全正確。

## 仍待執行（都需要人在現場）

1. **Stage 3 後半 — reclaim 驗證**：必須先在感測器前放遮蔽物。第一輪就是對無遮蔽的午後天空跑 64x 把晶片打掛的，不重複。
2. **Stage 4f — 真實寫入一筆**：`./record-photone.sh --ppfd <實測> --lux <實測>`
3. **Stage 6 — `secrets.h` 的 `PUBLISH_AMBIENT_SPECTRUM` 註解**：在 dev host。

## 第三輪 — rebase 到 `49c694d`（`--ref-pair` 移除）之後重跑

前一輪的 Stage 4 跑在 `064f7ff` 上，`49c694d` 又把 `--ref-pair` 拿掉、改由
`(source=daylight, lamp_state=1)` 表達 ref 錨定。配對與 `reconcile()` 都被改寫過，
所以整組重跑，否則這份記錄是在替已經不存在的旗標背書。

**a–e 全部仍然 PASS**（`--ref-pair` 已如預期變成 `unknown arg`，`ref_pair` 欄位消失）。
案例 a 因為此時太陽已落（`sun_alt=-5.1°`）推導為 `lamp` 而非 `mixed` —— 正確；
連同 b1/b2 與 e2，三種推導在本輪都各自被觀察到一次。

### e2 — ref 錨定收錄（新寫法）

`./record-photone.sh --at 2026-08-28T08:17:00Z --ppfd 42 --lux 2100 --source daylight --dry-run`
（該時刻燈開、`sun_alt=26.3°`，推導 = mixed）

| 驗收項 | 結果 |
|---|---|
| 不視為矛盾，標頭 `source=daylight [ref-only]` | **PASS** |
| 警告文字 | **PASS** — `daylight under a lit lamp (ref anchor): main lux + spectrum are lamp-lit — stored as sentinels; this row carries lux_ref evidence only` |
| tag `source=daylight`、`lamp_state=1.0` | **PASS** |
| `lux_at=-1.0`、八通道全 `-1.0`、`lux_ref_at=764.525`（正常值） | **PASS** |
| `source_override=0.0`（這不是 override） | **PASS** |
| 報表有 `r_ref = Photone/lux_ref` | **PASS** — 2.747 |
| 三串流各自把關 | **PASS** — `paired = yes (spectrum refused, lux refused, lux_ref ok)` |
| 反例：同時刻不帶 `--lux` → 拒收 | **PASS** — `--source daylight under a lit lamp is the ref-anchor recording (Photone at the lux_ref spot) — it needs --lux` |
| 反例：夜間燈開 + `--source daylight` → 矛盾中止（= 案例 d） | **PASS** |

面板那兩項（id 12 overlay 不出現該點、id 13 顯示 sentinel）要等 **f 真實寫入**之後才驗得到。

### 輸入守門（`064f7ff` 引入，`49c694d` 保留）

`--ppfd nan` 與 `--ppfd 0` 皆回 `--ppfd must be a finite number > 0`。
