# Plan:Photone 校正值記錄 CLI

## 目標
把校準過的 Photone 讀值當**地面真值**點,存進 InfluxDB、標好光源條件,用來:
1. 驗證/修正光譜 CAL(air.json id 9/11,`CAL=0.0017469`,日光下校的)。
2. 導出**每種光源的修正係數** —— 尤其待解的**燈下**(光譜目前只有 lux/54 的 0.48×,原因未定,見 `PPFD-CALIBRATION.md` / 記憶 ppfd-calibration-status)。
3. Grafana 把真值疊在感測器推算值上,一眼看偏差。

手動、低量、少數點。

## 1. CLI 介面 — `broker/record-photone.sh`
沿用 `add-plant.sh` / `calibrate-ppfd.sh` 的模式(bash wrapper + 內嵌 python3、`.env` grep token、`docker exec influx`)。

```bash
./record-photone.sh --ppfd 42.0 --source lamp [options]
```

| 參數 | 說明 |
|---|---|
| `--ppfd <µmol/m²/s>` | **必填**,Photone PPFD 讀值 |
| `--source <daylight\|lamp\|mixed>` | **必填**,光源條件 —— 修正係數分光源,這是關鍵 tag |
| `--lux <value>` | 選填,Photone 若也給 lux → 交叉驗 BH1750 |
| `--device <name>` | 預設 `livingroom`,配對的感測器位置 |
| `--at <RFC3339\|now\|-5m>` | 預設 now;手動量測允許回填到分鐘 |
| `--note "<text>"` | 選填,如「cactus-03 冠層、燈距 30cm」 |
| `--dry-run` | 只印不寫 |

**執行時**:
1. 驗證輸入(ppfd>0、source 合法、時間可解析)。
2. 寫一個點 → measurement `photone`。
3. 撈同一時刻(`--at` ±2 分鐘)該 device 的感測值並做**窗品質檢查**:
   - 空窗 → 仍寫 photone 點,但 context 欄位標 null、印「no paired telemetry」、`paired=false`。
   - 不穩窗(窗內燈 on/off 切換、lux delta 過大、通道變異過大)→ **警告**,別盲取均值造出假比值。
   - 印**樣本數 n、lux min/max、stdev**;取值用最近樣本或中位數 + 統計,不只盲均值。
4. 印**三方對照表** + 比值:
   - `k_spec = photone / 光譜` ← 該光源的**光譜修正係數**
   - `k_lux  = photone / (lux/54)`
   - ⚠️ `source=mixed` 時 `k` 只是**觀測值(k_observed)**,不是 source-intrinsic 的校正 —— 它隨當下燈/日光比例變。**mixed 標為 diagnostic-only**,不拿來當校正,除非另建燈佔比模型。
5. 結束(只寫點、報係數,**不自動套用**)。

## 2. 中間資料 pipeline
- **儲存**:新 measurement `photone`(同 bucket `sensors`),line protocol 經 `docker exec influx write`,token 從 `.env`。
  - tags:`device`、`source`(低基數,3 值)、`gain`(採集 gain,標記用)
  - fields:`ppfd`、`lux`(選)、`note`(string)
- **為何獨立 measurement**:真值 ≠ 遙測;量極小;好疊圖、好查、永不和感測資料混。
- **修正係數不落地(即時算)** —— CLI 當場印、Grafana 即時 join。
- **修正模型(先簡單)**:每光源一個純量 `k_source = mean(photone/光譜)`。`k_daylight` 應 ≈1(驗證 CAL);`k_lamp` 就是燈下修正。點數夠(每光源 ≥3–5)之前**不做自動改 CAL**(YAGNI)。

### 2b. 原始資料的未來可用性(關鍵)
CAL 和修正模型以後**都會變**。若每筆只存「當下算出來的光譜 PPFD」,一改 CAL 舊比較就作廢。原則:**derive 的可丟(k 即時算),raw 的一定留、且要能重算。**

**CLI 只在寫入當下做一次配對**,把 co-timed context 快照進**同一個 photone 點**;下游(Grafana/表格)**讀存好的欄位、不再回頭 time-join**(手動時間戳和 15s 遙測對不齊,dashboard 端 join 會歪)。

每筆快照(±2 分鐘窗):
- **AS7341 原始 8 通道均值**(`f415`…`f680`,全部寫 **float**)→ 未來換 CAL、換 Rᵢ 除數、甚至換公式,都能從原始 counts 重算。最保險的一層,量極小值得。
- **採集設定**:`gain`(tag)**+ 積分時間** `atime`/`astep`(或 `tint_ms`)—— counts 同時依賴 gain **和積分時間**(這正是先前校準踩過的洞),只存 gain 不夠。
- `spec_ppfd_at`、`lux_at`、`lux_ref_at`(當時感測值)、`cal_at`(當下 CAL,provenance)、`n`/窗品質標記(說明這些是窗均值)、`paired`(bool)。
- **append-only CSV 副本** `broker/photone-log.csv`(人類可讀、可匯出、DB 掛了也在)。
- bucket **永久保留**(retention=0,已是)。

型別注意:所有 derived/context 值一律 **float**(raw counts 若保證永遠整數可 int),避免 InfluxDB 同欄 int/float 衝突。

結果:任何未來的 CAL / 公式 / 修正模型,都能拿這批原始點**重新推導**,不必重量。

## 3. Grafana 展示
- **疊在 PPFD 面板(id 9)**:加第二條 query 把 `photone` ppfd 畫成**純點**(無線、大點、醒目色)→ 真值點坐在感測曲線上,偏差一眼看出。依 device 過濾。**區分 paired / unpaired 點**(空窗那些標不同色)。
- **新「CAL check」表格面板**:最新 photone 點 + photone/光譜/lux54/k_spec/k_lux + **配對樣本數/窗品質**,依 source 分組。**比值直接讀存好的 `spec_ppfd_at` 等欄位算,不做 dashboard 端 time-join**(時間戳會歪)。一眼看:日光 k≈1 嗎?k_lamp 多少?
- (之後選配)stat 面板顯示當前 `k_daylight`、`k_lamp`。

## 4. 套用修正(payoff,之後的 phase,現在不做)
- k_lamp 可信後:
  - (a) 最簡:光譜 PPFD 乘光源係數 —— 但實際是日光+燈**混光**,單一純量只是近似。
  - (b) 較好:導燈專屬 CAL,用**燈佔比** `(lux−lux_ref)/lux` 混合加權。較費工,**等點數夠再決定**。
- 先記錄、看 k、再選模型。

## 5. 範圍/取捨(ponytail)
- 薄薄一支「寫入 + 對照印表」。**無新服務、無 schema 遷移、無設定檔**。
- ⚠️ **重複點**:S 公式除數(55…1070)和 `CAL=0.0017469` 目前**內嵌在 air.json**(面板 9/11),此腳本要用同一組 → 會複製一份。`ponytail:` 兩處內嵌可接受,但**加一個明顯的 self-check + 註解指向 air.json 這個 canonical 來源**;真要一致就把 CAL 移到 `.env` 共用(先標記、不做)。
- 自帶 `--dry-run` + 比值數學的一個 assert 自檢。
- **daylight 的 k 要跨天/跨亮度多點才可信**(不能單一天一個點就信),再談自動改 CAL。

## 落地順序
1. `record-photone.sh`(寫入 + 三方對照)← 量完當晚就能用
2. Grafana 疊點 + CAL check 表
3. (資料夠後)決定修正模型 → 套用

## 相關
- `PPFD-CALIBRATION.md`(CAL 由來)、`SENSOR-DRIFT-DESIGN.md`(採集設定缺口)、記憶 `ppfd-calibration-status`(燈下 0.48× 未解、下一步 Photone)。
