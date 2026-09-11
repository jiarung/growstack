# Phase D — 消費端遷移 runbook

_程式面(kconsume.py + fixture + k-migration dashboard)完成後,遷移本身是**運維動作**:
每項 7 天平行期 → 日積分差異 ≤2%(或可解釋)→ 翻生產面板 query → 打 annotation。
回退 = git revert 該 query;raw 從未被改寫,零資料風險。_

## 前置(全部滿足才開始計時)

- [ ] Phase A integrated test 全過(docs/photone-phase-a-integration-test.md)
- [ ] compute-k-models.sh 掛上 host cron(**鎖內建,cron 行不包 flock**),連續數輪綠
- [ ] k-models dashboard 顯示各桶;首批 bootstrap 採納值人工過目(Phase C live 驗收)
- [ ] k-migration dashboard 兩條線開始並行(未校正桶 corrected==raw 是預期)

## 逐項(依序,不並行)

| # | 消費者 | 平行儀表 | 翻法 | 閘 |
|---|---|---|---|---|
| 1 | air.json Spectrum PPFD(panel 9 自 2026-09-11 讀 k_adopted;panel 12 仍硬編碼,對照用)<br>⚠ panel 9 **刻意只用 `lamp/none` 這一格**,不做 canonical join —— 與 light.py 同一個取捨:面板的 CAL 是單一純量,做逐 cell join 會變成這個 join 的第三份實作。代價是 `daylight/*` 日後被採納時 panel 9 不會跟著動;要那個,得先解掉 B1 讓平行期跑得起來。 | k-migration「item 1」 | 面板 map 的 `CAL *` 改為 canonical join 的 `k *`(照 k-migration query) | 7d ≤2% + as7341 桶 ≥provisional |
| 2 | air.json DLI-lux / daily.json 總 lux(lux/54) | 「item 2」+ delta 表 | `(lux×k_main)/54` | 7d ≤2% + lux_main daylight 桶 valid |
| 3 | daily.json 遮燈 DLI(lux_ref/54) | 「item 3」 | `(lux_ref×k_ref)/54` | 7d ≤2% + lux_ref 桶 valid |
| 4 | ratio canary 基準 | — | 分母改校正 lux 後**重定基準區間**(舊區間作廢) | item 2 翻完後 |
| 5 | ppfd-cal-daily.sh / cal-review-reminder.sh | — | 改讀 as7341_ppfd 桶或退役(職能被管線吸收) | item 1 翻完後 |
| 6 | **light.py 控制 DLI(raw integral)** | — | **最後,需使用者單獨核可**;uncorrected/seed 期間一律維持 raw —— 燈控絕不因校正狀態跳變 | 使用者點頭 |

每次翻完:Grafana annotation(切換日)+ 本檔打勾 + 觀察 48h 告警不誤發。

## 契約備忘

- canonical join 順序不可換:truncate 5m → station-map → light_context → k_adopted → **在原始 _time 乘,之後才 aggregate**(參考實作 broker/kconsume.py,fixture 凍結於 fixtures/expected/corrected.json)
- fallback = 乘 per-target seed(lux 1.0 / PPFD 0.0017469)+ 旗標 unknown|mixed|out-of-matrix|seed —— 乘 seed 在數值上就是未校正,永不跳變
- 套用語意 = epoch-current:採納更新會使歷史查詢微動(±10% 限幅),每次採納打 annotation;任何過去結果可由 raw + k_adopted 全史重建
- k-migration 的 Flux 與 kconsume.py 必須同語意;部署後首週抽 3 個時點人工對算兩者

## 阻塞中（2026-09-07 盤點）

翻任何一項生產面板之前，這三件必須先解決。**前兩件是硬阻塞**：沒有平行面板就沒有驗收依據，
而汙染的採納值會被直接套進去。

- [ ] **B1 — k-migration 平行面板五個裡四個是壞的，而且從來沒有運作過。**
  `raw` 那個 union 的真實分支帶著 `range()` 留下的 `_start/_stop/_measurement/_field/device`，
  dummy 列只有三欄，schema 不合 → `record is missing label _value`。
  （`k` 和 `ctx` 兩個 union 都有 `keep()` 對齊，唯獨 `raw` 漏了。）
  補上 `keep()` 後 schema 錯誤消失，露出下一層：
  **`internal error: panic: arrow/array: index out of range`**，panel 1/2/3/4 同一個錯。
  引擎層 panic，不是語法問題。**這是整個 migration matrix 的驗收工具，它壞著就沒有 7 天平行期可言。**

- [ ] **B2 — `k_adopted[bh1750_lux_main/daylight/diffuse] = 0.209968` 是已知錯誤值。**
  來自被撤回的汙染配對（見 `docs/incidents/2026-09.md#0901`）。它是 `carry`，
  而 `provisional` 不會置換已採納值，所以不會自己好。
  **2026-09-10 carry 寬限到期**會變 `stale` 並開始告警 —— 但**值不會變**，只是多一個警報。
  只佔 1.3% 的積分光量，但翻 item 2 就會把它套進 daylight cell。
  **2026-09-11 實測：預言完全應驗** —— `adoption_state=stale`、`reason=carry-expired`、
  `value` 仍是 `0.209968`。所以這一項沒有自己好，也不會自己好。
  `as7341_ppfd` 三個桶同樣全是 `stale`/`carry-expired`（lamp/none 停在被 08-24
  感測器故障汙染的 `0.228541`）—— 面板不受影響，因為 2026-09-11 的 `adoption_state`
  閘門會擋下未採納的值，但**過期的 carry 本身仍然沒有清除機制**。

- [ ] **B3 — 通量比的基準與量測窗不同尺。**
  `cal-review-reminder.sh` 在 pre-lamp 窗算 `clear/lux`，門檻卻用 6.0（註解引用燈下量的 6.3–6.6 基準）。
  實測同日兩窗換算係數 **3.0–3.9×**，所以它報 7.51「OK」時，燈下尺度只有 2.50 ——
  低於告警帶下緣。**它在整段最嚴重的光學劣化期間一路發 OK。**
  健康期的 pre-lamp 值實測是 1.1–3.3，門檻 6.0 比健康值本身還高 2–5 倍，
  也就是它不可能以正確的理由通過。現行硬體的健康 pre-lamp 基準**還沒有樣本**。

## 已完成的前置

- [x] **2026-09-07 — `(mixed, none)` 進入 lux 目標的合法矩陣，消費端短路解除。**
  mixed cell 佔 84% 的積分光量，先前被 `kconsume.py` 無條件短路擋在矩陣檢查之前，
  所以 `bh1750_lux_main` 的 k 對絕大多數的光都送不到消費端。
  現在 `k_adopted[bh1750_lux_main/mixed/none] = 3.93031`（bootstrap）。
  as7341 維持排除 —— 它的 k 是光譜換算，mixed 下只是 `k_observed`。
  mixed 仍不發 retained MQTT（避免跨 epoch 的 stale payload）。

## 已知天花板（不是待辦，是限制）

修正後**仍有約 1.85× 殘差，且不會因為多收資料而改善**。
`corr(k, 亮度) = +0.949` —— 感測器是壓縮響應而非刻度錯誤，單一乘子在數學上修不了那個形狀。
兩個 lux 桶的 CI 相對寬度都是 0.53（門檻 0.20），所以它們**永遠不會 `valid`、永遠不會自動採納**，
只能由 bootstrap 帶進來。要真正校準得換模型形狀，那是 blueprint 層級的事
（`docs/photone-cal-pipeline.md` 的「明確不做(v1)」第一條）。

## 記錄

| 日期 | 項 | 動作 | 結果 |
|---|---|---|---|
| 2026-09-07 | 前置 | `(mixed,none)` 入矩陣 + 解除消費端短路 | `k_adopted` mixed = 3.930；selftest ×3、fixture 5/5、live 13 buckets 全過 |
