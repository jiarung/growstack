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
| 1 | air.json Spectrum PPFD(panel 9/11/12 硬編碼 CAL) | k-migration「item 1」 | 面板 map 的 `CAL *` 改為 canonical join 的 `k *`(照 k-migration query) | 7d ≤2% + as7341 桶 ≥provisional |
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

## 記錄

| 日期 | 項 | 動作 | 結果 |
|---|---|---|---|
| | | | |
