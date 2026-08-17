# 每日 PPFD 校準監測 → Grafana(實作 plan)

> **⚠ 已被 `PPFD-CAL-ROUTINE-PLAN.md` 取代（2026-08-17）。保留此檔僅為記錄當時的推理。**
>
> 實際做出來的東西與本文有兩處重大差異：
>
> 1. **`LUX_MIN` 是 300 不是 3000。** 本文第 12 行擔心的「暗房」比預期嚴重一個數量級 —— 實測開燈前窗只有 230–877 lux，用 3000 的話序列**每天**都是 `valid=0`。本文列的退路「降 lux-min（品質打折）」才是唯一可行的路，不是備案。
> 2. **`window_mode`／`skipped`／`lamp_off_ok` 由編排層加上，`calibrate-ppfd.sh` 未被修改。** 本文 Phase 1 規劃在 `emit()` 裡加欄位；那是錯的分層 —— 擬合引擎不知道用了哪個窗，而跳過的日子它根本不會被呼叫。
>
> 另外本文 Phase 3 建議的 cron 時間 `15 8 * * *` 會在台北 16:15 執行，因為這台主機跑 UTC。實際使用 `15 0 * * *`。

_目標:每天固定時間對「開燈前日光窗」自動跑 PPFD 校準,把**候選 CAL + 品質指標**寫進 InfluxDB → Grafana 顯示,讓校準持續被監測、可見,不用每次手動跑。_

> 這是 **host 端(broker/)** 工作。firmware 不用改。搬到 grafana host 執行。
> 相關:`calibrate-ppfd.sh`(重用其核心)、`PPFD-CALIBRATION.md`、`PHOTONE-CAL-PLAN.md`、`SENSOR-DRIFT-DESIGN.md`、`grafana/provisioning/dashboards/air.json`(PPFD 面板 id 9/11/12,`CAL=0.0017469`)。

---

## ⚠️ 前提(沒滿足前,這 pipeline 會產出空值/垃圾 —— 但可以先建起來擺著)
1. **AS7341 供電要修好**。brownout 期間的 spectrum 是垃圾 → 校準無意義。
2. **暗房限制**:livingroom 開燈前日光實測**整段 < 3000 lux(`LUX_MIN` 預設)→ kept 0**。所以每日日光-lux 擬合**現在天天會 `INSUFFICIENT DATA`**。要 (a) 讓 AS7341 見更多日光(挪窗邊/陽台),或 (b) 降 `--lux-min`(品質打折),否則每日結果多半 `valid=0`。

## 本質定位(重要)
- 日光-lux 法的參考是 **BH1750 lux**,**不是絕對真值** → 這個每日 job 是**「候選 CAL / 漂移監測」,不是權威值**。
- **權威 CAL 應由 Photone 導**(見 `PHOTONE-CAL-PLAN.md`,燈下也能做,繞過暗房 + BH1750)。
- 因此 **本 pipeline 不自動改面板公式**(自動套 = 把爛窗垃圾烤進 CAL,危險)。**套用永遠人工覆核。**

## 決策(推薦預設,host 上可調)
1. **套用策略 = 監測用候選 CAL,不自動套用**面板。✅ 推薦
2. **暗房 = Photone 當權威(2c);日光-lux 這條僅作每日趨勢監測**。可另加 (a) 挪 sensor 見日光。
3. **範圍 = 順便把 ratio canary(AS7341 clear ÷ BH1750 lux)也塞進同一每日 job**(同一批資料,一起上 Grafana)。✅

---

## 架構
```
每日 cron/systemd timer(固定本地時間,窗口收盤後,如 08:15 Asia/Taipei)
  → 算「今天開燈前日光窗」= 本地 00:00→LIGHT_WINDOW_START_MIN(預設 08:00)
  → 跑 calibrate-ppfd.sh --emit(重用核心,輸出機器可讀)
  → 算 候選 CAL(OLS/median) + 品質(R²、drift%、N、lux span、valid) + ratio canary
  → docker exec influx write → measurement `ppfd_cal`(tag: device)
  → Grafana 面板讀 `ppfd_cal` → CAL 趨勢 + 現用 CAL 參考線 + 品質/valid + ratio
  → (人工)趨勢穩定且偏離 → 手動改 air.json CAL(9/11/12)+ redeploy
```

## InfluxDB schema(新 measurement `ppfd_cal`)
- **tag**:`device`
- **fields**(全 float,`valid`/`n_kept` 當 float 存以保型別穩定):
  - `cal_ols`、`cal_med` — 候選 CAL(OLS through origin / median ratio)
  - `r2`、`drift_pct`、`spread_pct` — 擬合品質
  - `n_kept`、`lux_lo`、`lux_hi` — 樣本數與亮度範圍
  - `ratio_clear_lux` — 當窗 clear/lux 中位數(ratio canary,2a)
  - `valid` — 1=通過所有 guardrail 可信;0=資料不足/漂移過大(仍寫,趨勢才看得出「一直不夠光」)
- 例(line protocol):
  ```
  ppfd_cal,device=livingroom cal_ols=0.00182,cal_med=0.00190,r2=0.984,drift_pct=12.0,spread_pct=9,n_kept=45,lux_lo=3200,lux_hi=8100,ratio_clear_lux=6.4,valid=1
  ```

---

## 分階段(每階段有驗收;host 上做,建議一樣 STOP-before-commit)

### Phase 1 — `calibrate-ppfd.sh` 加機器可讀輸出
- 加 `--emit` 旗標:除了現有互動輸出,末尾多印**一行 InfluxDB line protocol**(或 `--json`)。
- **不足資料/漂移過大時也要 emit**,帶 `valid=0` + 現有的部分指標(`n_kept` 等),不要 silent exit。
- 純加,不動現有行為。
- **驗收**:`./calibrate-ppfd.sh --device livingroom --start <過去日光窗> --emit` → 拿到乾淨一行 line protocol;不足時 `valid=0`。

### Phase 2 — 每日 wrapper + 寫入 InfluxDB
- 新 `broker/ppfd-cal-daily.sh`:
  - 算「今天開燈前窗」:本地當天 `00:00` → `LIGHT_WINDOW_START_MIN`(從 `.env` 讀,預設 480=08:00),轉 UTC 當 flux range。
  - 跑 Phase 1 `--emit` → 拿 line protocol。
  - `docker exec -e INFLUX_TOKEN monitor-air-influxdb influx write --org monitor-air --bucket sensors '<line>'`(容器/org 名沿用 `monitor-air*`,**不受 repo 改名影響**)。
  - token 從 `.env` grep(同 `calibrate-ppfd.sh` 模式)。
- **驗收**:手動跑一次 → InfluxDB `ppfd_cal` 有 1 點(`influx query` 撈得到)。

### Phase 3 — 排程
- host 上 cron 或 systemd timer,**固定本地時間**(窗口收盤後,如 `15 8 * * *` Asia/Taipei)。
- 確認 host 時區 = Asia/Taipei(或在腳本內固定用 TZ 算窗口)。
- **驗收**:timer 觸發、隔天自動多一點;log 有紀錄。

### Phase 4 — Grafana 面板(air.json)
- 新面板:
  - `cal_ols` 時序(只畫 `valid==1`)+ **現用 CAL 參考線 `0.0017469`**(threshold/constant)做對照。
  - 品質 stat/table:最新 `r2`/`drift_pct`/`n_kept`/`valid`。
  - (2a)`ratio_clear_lux` 時序 + 健康帶。
- **驗收**:面板顯示序列;`valid=0` 的日子看得出斷點/標記。

### Phase 5 — 覆核/套用 SOP(文件,保持人工)
- 寫進 `PPFD-CALIBRATION.md`:**候選 CAL 連續 N 天穩定、且與現用 CAL 偏離 > X%、品質良好(r2 高、drift 低、valid)→ 才手動更新 air.json 面板 9/11/12 的 CAL,redeploy dashboard。**
- **絕不自動改公式。** 權威錨仍以 Photone(`PHOTONE-CAL-PLAN.md`)為準,本 job 負責「盯漂移 + 提候選值」。

---

## 依賴
host(docker / InfluxDB `monitor-air-influxdb` / Grafana)、`.env` 的 `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`、`calibrate-ppfd.sh`、`LIGHT_WINDOW_START_MIN`。**外加前提:AS7341 供電修好 + 日光 > lux-min(或降 lux-min)。**

## 風險
- **HIGH 暗房** → 每日多半 `valid=0`,直到解決日光曝光 / 降 lux-min / 改以 Photone 為主。
- **HIGH 供電未修** → 資料垃圾,先修。
- **MEDIUM** 日光-lux 僅監測值(參考 BH1750),非權威 → **不自動套用**。
- **MEDIUM** CAL 綁 ambient gain 4×:若改 gain,`ppfd_cal` 與面板都要一起重算(在腳本註明 gain 假設)。
- **LOW** timezone(台灣無 DST,固定 +8)、cron 需 docker 權限(腳本已 docker exec)。

## 複雜度:Medium(host 腳本 + cron + 一個 Grafana 面板,重用現有 `calibrate-ppfd.sh`)

---
_建立 2026-08-16。搬到 grafana host 後,建議照 Phase 1→5 逐步做、每階段驗收。實作前先確認 AS7341 供電已修、且能取得 > lux-min 的日光窗(否則先當「pipeline 建置、待資料」擺著)。_
