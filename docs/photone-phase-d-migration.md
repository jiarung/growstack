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

- [x] **B1 ✅ 2026-09-11 — k-migration 五個面板全部可用了。**
  `raw` union 的 schema 不合（`keep()` 漏了）已於 `a78cc7f` 修好；那之後只剩 panel 4
  的 `integral: found out-of-order times` —— **`group()` 之後 Flux 不保證列序**，而
  `integral()` 逐列走並拒絕亂序。兩處 `integral` 前補 `sort(columns: ["_time"])`。
  同一個坑 `daily.json` panel 10 的 `cyc` 區塊早就帶著註解修過了。

  ### 但「面板能跑」不是這一項真正的問題

  修完之後 panel 4 讀 **+33%**，而 mixed 佔 81% 的光、k=3.93，校正側該給約 **+270%**。
  原因在 `cor` 的條件裡：

  ```flux
  if l.src == "unknown" or l.src == "mixed" ...  then 1.0 else r.value
                          ^^^^^^^^^^^^^^^^^^
  ```

  **`(mixed,none)` 自 `7a64a3f` 起就是 lux 目標的合法 cell**（`kmodels.py` UNIVERSE），
  `kconsume` 和生產面板都在校正它 —— **只有這個驗收工具還停在舊矩陣**。
  它不是壞掉，是**在量一個生產端早就離開的宇宙**，而一個會渲染錯誤數字的面板
  比一個會報錯的面板危險。

  panel 1 / 4（`bh1750_lux_main`）已改為納入 `(mixed,none)`；
  **panel 2（`bh1750_lux_ref`，矩陣只有 daylight）與 panel 3（`as7341_ppfd`，mixed
  對它只是 k_observed）刻意保留排除** —— 三者現在各自對上 `kmodels.py` 的 UNIVERSE。

  修正後 panel 4：`09-09 272.8 / 09-10 269.2 / 09-11 271.0`（散佈 1.5%）。

  ### ±2% 閘門已作廢，改讀穩定度

  那個閘門假設的是「切換前的平行期，校正側應等於 raw」。生產端已於 2026-09-08..11
  切走（air.json 即時讀 `k_adopted`），所以 **+270% 是設計上的結果，不是失敗**。
  現在該看的是**這條線穩不穩** —— 跳動代表某個係數動了或 cell 組成變了，兩者都值得知道。
  面板標題與描述已改寫，不再宣稱一個沒人會再用的門檻。

- [ ] **B2 — `k_adopted[bh1750_lux_main/daylight/diffuse] = 0.209968` 是已知錯誤值。**
  來自被撤回的汙染配對（見 `docs/incidents/2026-09.md#0901`）。`model_id` 顯示它是在
  **e0-legacy** 賺到的，carry 進 e1 後於 2026-09-10 到期 —— `stale` / `carry-expired`，
  **值仍是 `0.209968`**（2026-09-11 實測，預言完全應驗）。

  ### 2026-09-11 重新評估：這不是「待修的阻塞」，是「量不到的天花板」

  **自動路徑全部無效**，因為證據收不到：

  ```
  bh1750_lux_main / daylight/diffuse / e1
    estimate    17.5312      n_sessions  1      status  unvalidated
  ```

  `unvalidated` 在採納表裡什麼都不觸發（`kadopt.py:177`「provisional never
  displaces an earned value; unvalidated says nothing」）。epoch 重置也不適用 ——
  B4 的正當性來自一個**已記錄的器材事件**，而 B2 的壞值來自一個**已經修好的程式
  缺陷**（`ref_only` 配對汙染），沒有器材變更可標。照搬 B4 就是 codex 警告的
  「用 epoch 機制洗白一次重置」。

  **n=1 的原因是這個站點的幾何**：`ref_only` 排除了燈開著的 daylight 列，而
  08-11 以來 13 筆 daylight 量測裡**只有 1 筆燈是關的**。這就是
  [`FLOWS.md` 的 gap 8](../broker/FLOWS.md#known-gaps)。

  ### 它值多少（2026-09-11 實測，近 7 天）

  | cell | 佔積分光量 |
  |---|---|
  | mixed/none | 81.4% |
  | lamp/none | 14.4% |
  | unknown/unknown | 1.9% |
  | **daylight/diffuse** | **0.74%** |
  | none/none | 0.2% |

  修好它對校正後 DLI 的影響是 **+3.5%**。代價是**五次停燈 30 分鐘的日光量測**
  （每次約 0.65 mol，只能挑晴天，用面板 3 判斷當天有沒有餘裕）。

  **而唯一的候選替代值是 `17.5312`、n=1、`unvalidated`** —— 那筆 09-01 07:45 的
  晨窗量測，感測器讀 461 lux 而 Photone 讀 8,080。它本身就被標記為可能的離群值。
  **用一個未驗證的孤例換掉一個已知錯誤的值，不是修復。**

  ### 結論

  **降級為已知天花板，不再當阻塞。** 目前的曝險是校正後 DLI 低約 3.5%，而且管線
  自己已經把該桶標成 `stale`；air.json 的 `kOf()` 也會退回 seed 不顯示它。
  `kconsume` 仍會套用（`kconsume.py:84` 只對 `seed` 退回）—— 那是已揭露的行為。

  **要真的關掉它，需要的是量測活動而不是程式改動**：晴天用 `lamp-hold.sh` 停燈
  30 分鐘，在樹冠點量一次 daylight Photone，重複五次。在那之前，這一項不該再被
  當成「下一步要修什麼」。

- [x] **B3 ✅ 2026-09-11 — 通量比改在燈下窗量，與門檻同尺。**
  `cal-review-reminder.sh` 原本在 **06:00–08:00** 晨窗算 `clear/lux`，門檻卻用 6.0 ——
  那是**燈下**量出來的基準（6.3–6.6）。一個尺度對另一個尺度的門檻。

  **修法不是換算係數，因為沒有穩定的係數。** 2026-08-30..09-10 實測燈下÷晨窗：

  ```
          晨窗    燈下窗   比
  08-30   7.51    2.96    0.39      09-04   5.57    5.15    0.92
  09-01   9.01    2.66    0.29      09-07  10.40    3.72    0.36
  09-03   7.65    2.70    0.35      09-10   5.69    3.29    0.58
  ```

  **0.29–0.93，散佈 3.2 倍。** 兩個窗量的是不同的東西：晨窗的日光光譜每天不同，
  燈是固定光源 —— 這正是 `air.json` panel 17 的描述早就寫過的理由
  （「the lamp is a controlled reference source… a daily median isolates the sensor
  and its optical path」），也是 `spectrum-throughput-drift` 告警用 09:00–18:00 的理由。

  `inW` 改為 540–1080，與那兩者同窗，6.0 終於在比同一件事。

  **修正前它會在最嚴重的劣化期一路報 OK**：08-30～09-03 晨窗讀 7.5–9.7（≥6.0 → 通過），
  而同期燈下只有 2.7–3.2。修正後同一批日子會正確地報「光路未達標」。

  實跑（2026-09-11）：09-08/09/10 三天有 `valid` 的 CAL 候選，通量比 3.70 / 3.60 / 3.29
  → **光路未達標**，乾淨日 0 天。這就是誠實的狀態。

  ⚠ **「通量比為什麼停在 3.3–3.7」仍然沒有 root cause** —— 但那是 CAL 採用的前置條件，
  不是本項。本項要修的「兩個尺度」已經解決。

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
