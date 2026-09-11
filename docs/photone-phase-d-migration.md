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

- [ ] **B3 — 通量比的基準與量測窗不同尺。**
  `cal-review-reminder.sh` 在 pre-lamp 窗算 `clear/lux`，門檻卻用 6.0（註解引用燈下量的 6.3–6.6 基準）。
  實測同日兩窗換算係數 **3.0–3.9×**，所以它報 7.51「OK」時，燈下尺度只有 2.50 ——
  低於告警帶下緣。**它在整段最嚴重的光學劣化期間一路發 OK。**
  健康期的 pre-lamp 值實測是 1.1–3.3，門檻 6.0 比健康值本身還高 2–5 倍，
  也就是它不可能以正確的理由通過。現行硬體的健康 pre-lamp 基準**還沒有樣本**。

- [x] **B4 ✅ 2026-09-11 — `as7341_ppfd` 三個桶全部 `stale`/`carry-expired`，`lamp/none` 停在被汙染的 `0.228541`。**
  來源是 2026-08-24 20:00 那次量測：`photone=220` 但 `spec_ppfd_at=1.68`（S≈962），
  AS7341 當時正在崩潰途中 —— 19:55 的 `clear` 只有 416（同日同時段中位數 10,917），
  20:15 全通道進入 65535 的 I2C 失敗哨兵。**現有的准入規則沒有一條擋得住它**：
  器材沒換、燈的狀態正確、設定相符、cell 合法、counts 為正且低於 `SAT_COUNT`。

  它不會自己好。`carry` 過期只改狀態不改值（B2 同理，2026-09-11 已驗證）。而若
  放著等新證據進來，`kadopt.py:153-170` 的 ±10% 煞車一定會跳（0.00675 vs 0.228541
  差 34 倍），桶會變 `held` 並等 `ack-k-hold.sh`。

  ### 處置：補標 2026-09-04 重新擺位這個 epoch

  **這個 epoch 本來就該標，與汙染值無關** —— 它是
  [`2026-09.md#0904`](incidents/2026-09.md#0904) 已經記錄在案的事件，而且用
  BH1750 當對照組：`clear` 在 11:20 恢復 **43 倍**（515 → 22,385），而 `lux` 在
  同一段時間只在 3,114–4,387 之間（擺位動作當下那一筆是 3,114，前後都在 4,100–4,400）。
  **光沒有變，變的只有 AS7341 看到的** —— 那是 counts→PPFD 映射改變的定義。
  漏標它本身就是缺失；重置汙染值是順帶的結果，不是理由。

  燈下（固定參考光源）`clear` 逐日中位數獨立佐證（台北日期）：

  ```
  9-2  11,412    9-3  404（仍在劣化 regime，事故紀錄當日通量比 2.97）
  9-4  22,473 ← 階躍    9-5  22,532    9-6  22,878
  ```

  **邊界 `2026-09-04T03:20:00Z`（= 09-04 11:20 台北，事故紀錄裡「放回去的那一刻」）**

  | 量測 | source | k | 落在 |
  |---|---|---|---|
  | 09-01 07:45 | daylight | 0.0108 | e1（舊） |
  | 09-07 19:15 | lamp | 0.00435 | **e2** |
  | 09-09 20:00 | lamp | 0.00675 | **e2** |
  | 09-10 19:00/19:30/20:00 | lamp | 0.0071 | **e2**（間隔 30 分，仍串成 1 session） |

  燈下證據 **n=3 全數保留**；08-24/25 的故障期與 09-03 的劣化期都留在舊側。
  唯一的代價是那筆孤例日光（n=1）留在 e1 —— 但它在儀器狀態改變之前，本來就不該混算。

  **三個桶都不會 carry。** `materialize()` 只在前身是 `adopted`/`held` 時繼承
  （`kadopt.py:76`），而 `lamp/none` 與 `daylight/diffuse` 是 `stale`、
  `daylight/direct` 是 `seed` —— docstring 明寫「seed/stale predecessors have
  nothing earned to carry」。三個桶在 e2 全部回到 seed `0.0017469`。

  **然後會自動發生**：`lamp/none` 的 model status 已是 `provisional`，而
  `kadopt.py:144-149` 的 seed 分支在 `provisional` 或 `valid` 就 bootstrap，
  **不過 ±10% 煞車**。所以下一次 cron（每小時 :10）會直接採納約 0.0069，
  `reason="bootstrap"`，不進 `held`、不需要 `ack-k-hold.sh`。
  air.json 面板 7/9 會在一小時內從 PPFD ~100 跳到 ~370，與 Photone 實測的 320 一致。

  ### 步驟

  `--config` 與 `--photone` 是必填（`mark-epoch.sh:123,129`），沿用 e1 的值：

  ```bash
  cd ~/monitor-air/broker
  ./mark-epoch.sh epoch --target as7341_ppfd --device livingroom \
    --start 2026-09-04T03:20:00Z \
    --config '{"gain": 4.0, "tint_ms": 280.78}' \
    --photone '{"phone": "iPhone 15 Pro", "app_ver": "3.2.1"}' \
    --reason "sensor repositioned 2026-09-04 11:20 after the desiccant knock: clear recovered 43x while lux held flat at 4,100-4,400 (docs/incidents/2026-09.md#0904)"
  ./mark-epoch.sh list
  # 同一輪：告警只看當前 epoch（見下節），改完必須重啟 Grafana 才會載入
  python3 kmodels.py --selftest
  ./compute-k-models.sh --fixture fixtures
  env -i PATH=/usr/bin:/bin HOME="$HOME" bash -c "cd $PWD && ./compute-k-models.sh"
  git add epochs.json && git commit        # append-only 註冊表，必須進版控
  ```

  ### 必須同時處理的副作用

  **`k-adoption-stuck` 告警會繼續對 e1 的三列叫。** 它從 1970 掃描**所有** epoch，
  對任何 `held`/`stale` 超過 24h 的列告警（`rules.yaml.tmpl:699,705`），而建立 e2
  不會改動 e1，採納流程也只處理當前 epoch 的 universe（`compute-k-models.py:133`）。
  **被取代的 epoch 停在 stale 是預期狀態，不是待辦** —— 告警應該只看每個 target
  的當前 epoch。不修的話這一步會換來三個永久誤報。

  **修法（必須與標記 epoch 同一輪做，不能只列在驗收）**：在 pivot 之後、
  `adoption_state` 過濾之前，對每個 `(target, source, regime)` 只留 `_time` 最新的
  那一列。被取代的 epoch 在 `compute-k-models.py:133` 之後就不再被寫入，而當前
  epoch 的桶至少在 materialize 當下寫過一次，所以「最新的寫入」就是「當前 epoch」——
  不需要把 `epochs.json` 搬進 InfluxDB。

  ```flux
  // 在既有的 pivot 之後加：
  |> group(columns: ["target", "source", "regime"])
  |> top(n: 1, columns: ["_time"])
  |> group()
  // 然後才是既有的 filter(adoption_state == "held" or ... == "stale")
  ```

  改的是 `rules.yaml.tmpl`，而告警規則**只在 Grafana 啟動時讀取**（面板有 watcher，
  規則沒有）—— 改完要重啟容器，否則看起來生效其實沒有。這是本 repo 的既有陷阱。

  ### 驗收

  1. `epochs.json` 多一列 `as7341_ppfd-e2`
  2. `k_model` 出現 e2 的桶，`lamp/none` n=3、estimate ≈ 0.0069
  3. `k_adopted[.../e2]` → `adopted` / `bootstrap` / 約 0.0069，且 `model_id` 指向 e2
  4. panel 9 從 ~100 變 ~370，對得上 Photone 的 320
  5. lux 兩個 target 完全不受影響（epoch 是 per-target）
  6. `k-adoption-stuck` 沒有因為 e1 產生新的誤報
  7. 告警規則**真的載入了** —— 用 `MAINTENANCE.md:155-157` 那組核對，三個數字必須一致：

     ```bash
     grep -c '^ *- uid:' grafana/provisioning/alerting/rules.yaml{.tmpl,}
     curl -s -u ... localhost:3001/api/v1/provisioning/alert-rules | jq length
     ```

     2026-09-02 就是靠這組發現兩條 k-model 規則從來沒被載入過，靜默了 11 天
     （`MAINTENANCE.md:133`）。

  ### 執行結果（2026-09-11）

  `as7341_ppfd-e2` 已登記，**第一次 cron 就 bootstrap 採納**，與推演完全一致：

  ```
  k_adopted[as7341_ppfd/lamp/none/e2]
    value 0.00674918   state adopted   reason bootstrap
    model_id  as7341_ppfd/lamp/none/as7341_ppfd-e2@2026-09-11T05:54:14Z
  daylight/diffuse、daylight/direct  → seed 0.0017469
  panel 9  ~100 → 327.5      （Photone 實測 320）
  ```

  告警從 5 列降到 3 列，消失的正是 as7341 的兩列；剩下 3 列是 lux 的真實 stale。
  三數核對 8/8/8（`rules.yaml.tmpl` / `rules.yaml` / API），Grafana 另外 force-recreate
  過 —— `start.sh` 只渲染不重建。commit `d1aede5`。

  ### 殘留風險

  - **本項會重置 k-migration 的 7 天平行期時鐘**，標 epoch 本來就是這種事件。
  - **e1 的桶不會消失，`0.228541` 仍躺在那裡。** 而 `kconsume.py:84-85` 只對 `seed`
    退回並標記，**其餘狀態一律套用 `row["value"]`，包含 `stale`** —— 所以**邊界之前
    的歷史 cell 在正規消費端仍會乘到那個汙染值**。air.json 的 `kOf()` 有額外把關，
    但那是顯示政策，不是 `kconsume` 的行為。這一項 B4 不解決。
  - `daylight/diffuse` 在 e2 從 n=0 開始，短期內沒有日光係數。

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
