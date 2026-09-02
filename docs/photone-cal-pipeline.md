# Plan: Photone 動態校正係數管線(k_model v1)

_裸 BH1750 餘弦誤差隨光場變,單一常數是錯的模型;但稀疏資料也撐不起連續時變曲線 ——
故採「分層、版本化、帶品質狀態」的離散模型。_

## 目標
BH1750 ×2(主 0x23/ref 0x5C,無擴散罩)與 AS7341 spectrum-PPFD 對 Photone 黃金
標準的偏差,以「分層、版本化、帶品質狀態」的校正模型自動維護:
新參考點自動吸收、季節自動適應、硬體/參考儀器變更顯式分代、資料不足時誠實標示
而非硬給數字。server host 計算與套用,raw 永不改寫。

## 核心資料結構

```
k_model[target × source × regime × epoch] = {
  # target ∈ {bh1750_lux_main, bh1750_lux_ref, as7341_ppfd}
  # (position 由 source 決定;仰角為診斷欄位非維度)
  # 命名:k 沿用 record-photone.sh 既有詞彙(k_spec/k_lux);
  #       與既有常數 CAL 的關係:CAL = 從 k_model(as7341_ppfd) 採納的當前值
  estimate,            # 加權中位數(log 空間統計)
  ci,                  # session-level block bootstrap 區間
  n_sessions,          # 獨立量測 session 數(同一輪多筆 = 1)
  n_eff,               # Kish: (Σw)²/Σw²,w = 近因衰減(半衰期 30d)
  coverage, last_ref_age,
  status ∈ {valid, provisional, stale, unvalidated}
}
```

- **模型語意:擺位隨 source 而異。**
  | source | Photone 擺位 | 語意 |
  |---|---|---|
  | daylight | **lux_ref 位置+角度(colocated)** | **純感測器校正**(0x5C 尺度誤差含其安裝角餘弦項),零位置轉移 —— 全管線的錨定模型 |
  | lamp | 燈下植物平均位置(canopy) | 冠層估計器(尺度×位置轉移合併);燈幾何固定,最穩 |
  | mixed | 同 lamp(canopy) | 冠層估計器,診斷/疊加拆解 |
  - **顯式假設(要維護)**:「ref 的擺位代表植物的日光曝露環境」——成立則校準後的
    lux_ref 兼任 daylight 冠層光量;ref 移位 = epoch 事件
  - **系統不變量(2026-08-28 修訂):lux_ref 永遠不吃燈**(sensors.h:14 / FIRMWARE.md /
    遮燈 DLI 帳本整天積分 lux_ref,含燈開時段 —— 生產系統既有依賴)。因此
    **bh1750_lux_ref 的配對不受燈態限制**,且**不加新欄位** —— source 本就編碼擺位
    (position = source 的確定函數,daylight ⇒ ref 位),房間狀態由 row 上的
    lamp_state 如實記載:燈開∧day 推導 mixed 時,`--source daylight` 為**合法精化**
    (= 在 ref 位量日光場,需 --lux),該 row 為 **ref-only 證據**(lux_at 與八通道
    為燈照,寫 -1 sentinel),tag 即 daylight、入 daylight×regime 桶(regime 由
    乾淨的 lux_ref_at 判)。**(source=daylight, lamp_state=1) 就是 ref-only row 的
    完整識別**,消費端一律以此判別。夜間無日光場,`--source daylight` 照舊矛盾中止
  - daylight×main、daylight×as7341 = 次要模型(配對帶盒/光罩×位置轉移);
    **副產品**:Photone@ref ÷ main 結合 ref 錨定校正可拆出盒罩衰減係數
    (= BH1750 氧化/光罩衰減監測一直缺的量化工具,列 diagnostic 產出)
  - 仰角分桶:daylight 各桶仍需(Photone 有餘弦修正、ref 沒有,同角擺放不抵消);
    lamp/mixed(幾何固定)不需
  - ⚠️ canopy SOP 要**物理標記固定量測點**,「目測平均」會漂
- target:**兩個獨立 model family,絕不共用估計**
  - `bh1750_lux`(main/ref 各自):真值 = **Photone lux ÷ 該 sensor lux**。
    誤差機制=餘弦/幾何+位置轉移
  - `as7341_ppfd`:真值 = **Photone PPFD ÷ raw spectrum S**(= CAL 的直接擬合),
    **不經過 lux**。S 沿用 calibrate-ppfd.sh 現定義(counts 光譜積分,不含 gain/tint);
    **配置身分 = (gain, tint_ms)**,兩者 row 上已有 —— estimator 只取與該 epoch
    宣告配置相符的 rows,不做 counts 正規化(避免改 S 定義破壞與既有 CAL 可比性)
  - ⚠️ **禁止循環**:lux 模型不得使用 ppfd/(lux/54);CAL 不得吃 corrected lux
- source_class:由**量測時刻的燈態 × 太陽仰角**推導,不由時鐘也不由「現在」:
  - **燈態以量測 timestamp 回查 Influx**(`light.on` state transition 回推,
    同 lamp-hold.sh check 的既有作法);**light location 從 broker/.env 的
    LIGHT_LOCATION 解析**(device ≠ location,兩者本就分離),解析結果寫入 row
    (`light_location`)—— location 解析:**以 device + 量測 timestamp 查
    station-map**(歷史 `--at` 因此查到「當時」的 location,不是現在的);
    `.env` LIGHT_LOCATION 僅在 station-map 尚未建立時作 bootstrap ——
    不可用 retained「目前」state,否則 `--at` 歷史量測會被錯標
  - **燈態狀態契約**(Influx `light` 不是乾淨 transition log,規則如下):
    (a) Phase A 改 Telegraf:light topic 保留 `source` 為 tag;
    (b) **seed 是可信狀態觀測**(controller 重啟時實際輪詢插座的結果 ——
    排除它反而會信任重啟前的過時 transition,斷電改變狀態時必錯);
    (c) state(t) = 全部 rows(含 seed)最後一筆 `_time <= t` 的值(右連續);
    (d) **staleness 界限**:該 row 距 t 超過 **26h**(排程保證每日至少一次切換,
    超過 = 資料斷流)→ UNKNOWN;t 前無任何 row → UNKNOWN;
    (e) fixture 必含:重啟改變狀態(pre-restart ON → seed OFF → 量測應判 OFF)、
    斷流 >26h → UNKNOWN
  - 推導表(day = 仰角 > 0°):燈關∧day → `daylight`;燈開∧非 day → `lamp`;
    燈開∧day → `mixed`;燈關∧非 day → **拒收**(無光源;曙暮光弱讀值捨棄);
    燈態 UNKNOWN → 拒收,除非顯式 `--source` 覆寫(記 override 旗標)
  - `--source` 與推導不符:中止並顯示兩者,強制人工確認 —— **唯一例外**:推導 mixed
  時 `--source daylight` = ref 錨定收錄(見上,合法精化非矛盾)。因 canopy 語意 + 燈幾何固定,
  mixed 可嘗試「mixed − lamp 桶估計 = 日光分量」的疊加拆解(diagnostic → 
  若殘差穩定可貢獻 daylight 桶;Phase B 先只做診斷,拆解另評)
- regime:**由配對窗內 lux_ref 均值客觀判定,零人工標註**(v1 常數,改值 = 
  versioned migration 事件):
  `direct` = lux_ref_at ≥ 20000;`diffuse` = lux_ref_at ≤ 10000;
  之間 = `ambiguous`(guard band,不入桶;過渡時刻另有既有 lux-CV gate 擋)
  - 站點事實:遮蔭北陽台平時漫射,但**晴天中午 ~1-2hr 直射窗**,晴天可佔當日
    DLI 過半 —— 兩 regime 的裸 sensor 餘弦誤差不同,都必須覆蓋
  - 可回溯:歷史資料 compute 時同樣分類(人工標籤做不到)
  - 主分層 = source × regime;daylight 拆 diffuse/direct;**lamp/mixed 的 canonical
    regime = `none`**(所有儲存 tag、MQTT topic 段、light_context、join 一律用
    字面值 none,不得用空字串/缺 tag)
  - 仰角為診斷欄位(照記,不當主分桶);diffuse 桶 k 預期近常數,
    direct 桶自成一格,細分仰角等證據
  - SOP:**晴天中午直射窗要刻意收配對**(DLI 權重最大,不可只靠外插)
  - 仍需補「任意時刻仰角」計算(診斷欄位用;solar-noon.py 只給正午值)
- epoch:不可變 registry(見下)

## 係數決定程序(estimator spec —— 從 photone rows 到一個係數的完整計算)

**Canonical input = Influx `photone` measurement**(CSV 僅稽核,無八通道欄位,
永不作 estimator 輸入)。Flux:全時間範圍、`paired==1`、pivot 成 row
{ts, ppfd, lux(Photone lux;**既有 field 名**,未填 --lux 時 field 缺席,無 -1), lux_at, lux_ref_at, f415..f680 八通道, **gain_x**(legacy 字串 tag `gain` 不參與新版 epoch 比對), tint_ms, device}
+ 推導欄位 source/regime/epoch。**Sentinel 規則:任何參與比值的欄位必須 > 0**
(unpaired/缺值寫 -1,paired=1 時 lux_ref_at 仍可能為 -1 —— 一律排除該 row 對
該 target 的貢獻)。

**Step 1 — row 級整備**(compute-k-models.sh 每輪對全量重算,冪等):
- epoch 標定:ts 對 `epochs.json` 區間 → epoch_id;registry 建立前的資料
  一律歸 `e0-legacy`(可用,帶 pre-epoch 旗標)
- regime 標定:**僅 source=daylight** 適用 lux_ref_at 閾值 →
  direct|diffuse|ambiguous(ambiguous 丟棄);lamp/mixed rows 一律 regime=none
  (不套 guard band)。**ref-only rows(source=daylight ∧ lamp_state=1)無需例外
  邏輯**:tag 已是 daylight,regime 照常由 lux_ref_at 判(lux_ref 不吃燈,該欄位
  在燈開時仍是純日光);其 lux_at/八通道為 sentinel,經 >0 規則自然不進
  main/as7341 的 daylight 桶
- 品質過濾:paired=1 且該 target 所需欄位齊全(**無填補**:缺 lux 的 row
  不進 lux 類 target,只能進 as7341_ppfd)
- **飽和拒收**:任一八通道 ≥ SAT_COUNT(與 calibrate-ppfd.sh 同一常數)→
  該 row 對 `as7341_ppfd` 拒收(lux 類 target 不受影響);錄製端雖已丟棄
  飽和樣本,estimator 仍防禦性檢查(涵蓋歷史與旁路寫入)

**Step 2 — row 級比值**(每 row 對每個適用 target 產一個 kᵢ):
| target | kᵢ 公式 | 適用 rows | 語意 |
|---|---|---|---|
| `bh1750_lux_ref` | **lux ÷ lux_ref_at** | source=daylight(**任意 lamp_state** —— lamp_state=1 即 ref-only row)且 lux 存在;桶一律 daylight×regime | 錨定:純感測器校正(lux_ref 不吃燈,燈態無關) |
| `bh1750_lux_main` | lux ÷ lux_at | daylight rows(次要:含盒×位置轉移)/ lamp·mixed rows(冠層估計) | 兩種語意**分桶存放,永不混合** |
| `as7341_ppfd` | **ppfd ÷ S**(S 僅由八通道 counts 依 calibrate-ppfd.sh 的 `S_of(counts)` 計算;**gain_x/tint_ms 只作 epoch 配置身分/篩選,不參與 S 或 kᵢ 數值**) | ppfd 必有,全 source | = CAL 候選;daylight 含位置轉移,lamp=冠層 |
- 手動量測四個量的對映:Photone lux → field `lux`(位置由 source 隱含,daylight 即 ref 位);Photone ppfd → field `ppfd`(同)

**Step 3 — session 聚合**:在同一 target×source×regime×epoch 內,rows 依 ts 排序後
**相鄰間隔 ≤30min 即連鎖(chaining)**成一個 session(A-B 29min、B-C 29min → ABC 同
session,即使 A-C 58min);session 比值 **z_j = median(ln kᵢ)(log 空間;
偶數筆取兩中位的算術平均)、k_j = exp(z_j)**;session ts =
rows 依 ts 排序的中位(偶數筆取較早者)。
**session_id = `<target>/<source>/<regime>/<epoch>/<首 row ts 的 RFC3339 UTC 秒級>`**
(確定性,重算不變)。**之後所有統計以 session 為單位**(同一輪手動量測
不得偽裝成多個獨立證據)。

**Step 4 — 桶級估計**(對每個桶 [target × source × regime × epoch]):
1. z_j = ln k_j
2. 權重 w_j = 0.5^(age_days/30)(近因半衰期 30 天;epoch 內才有 age)
3. **estimate = exp(z 的加權中位數)** —— 加權中位數 = z 升冪排序後,
   正規化累積權重首次 ≥ 0.5 的那筆(同分取索引小者)
4. n_eff = (Σw)² / Σw²(Kish)
5. CI:session 級 bootstrap —— 以正規化權重為抽樣機率、**有放回抽 n_sessions 筆**、
   每個 replicate 取(未加權)中位數(權重已用於抽樣不重複計),B=1000,**seed=42**
   (可重現,PRNG = Python `random.Random(42)`);CI = **雙尾 95%**
   (2.5/97.5 百分位),取法 **nearest-rank(1-based,rank=ceil(p×B),無插值)**。
   n_sessions < 3 → 不計 CI,status 直接 unvalidated
6. status 判定(見晉升門檻)
7. **evidence_rev = sha256(canonical serialization)前 12 hex**:
   - row 級:每個 session 先算 **evidence_digest** = sha256(該 session 全部
     「實際採用 rows」各一行 `row_ts|kᵢ(9位,round-half-even)|旗標集
     (config_override/source_override/排除判定)`,依 row_ts 升冪、\n 連接)
   - session 級 serialization = 每 session 一行
     `session_id|evidence_digest|k_j(6位)|session_ts`(session_id 升冪、\n 連接)
   —— **只含證據內容**:時間流逝(權重衰減)、estimate/CI/status 變動
   **皆不改變**版號;row 增刪、任何 row 值/旗標修正**必變**(即使中位數與
   session_id 不變)。與含 computed_at 的 model_id 分工。
   fixture:(a) 同 session 值修正 → 必變;(b) session 內補一筆但中位數不變
   → **必變**;(c) 無新 session、僅權重老化使 estimate/status 變 → **不變**
8. 全部欄位連同 n_sessions/coverage/last_ref_age/evidence_rev
   寫入 `k_model` measurement + retained MQTT

**Step 5 — 採納(全自動,異常剎車;人工只處理告警)**:
- 每桶維護黏性的**「當前採納值」**;消費端**只讀 k_adopted**。
  **初值原則 = 系統現狀(per target)**:bh1750_* → 1.0(乘法 multiplier,無因次);
  as7341_ppfd → **現行 CAL = 0.0017469**(絕對值,µmol·m⁻²·s⁻¹/count)——
  「1.0 對 AS7341 是數百倍錯誤」故初值絕非 1.0;±10% 剎車帶為相對變化,兩者通用
- **bucket universe(per-target 合法矩陣)**,每 epoch 建立時物化 seed rows:
  | target | 合法桶 |
  |---|---|
  | bh1750_lux_ref | daylight×diffuse、daylight×direct(**僅此二桶**——ref 的校正語意只存在於日光) |
  | bh1750_lux_main | daylight×diffuse、daylight×direct、lamp×none |
  | as7341_ppfd | daylight×diffuse、daylight×direct、lamp×none |
  universe 內的桶 k_adopted 永遠有 row;mixed 只算 k_model(診斷),
  **不建 k_adopted**;清除僅在桶退出 universe
- **k_adopted schema**:Influx tags = **{target, source, regime, epoch}**
  (epoch 必為 tag,epoch-current join 依賴它),fields =
  {value, unit, adoption_state, model_id, prev_value(=被取代的採納值), reason,
  **pending_value, pending_model_id, pending_evidence_rev,
  hold_ack_state(none|awaiting|acked_accept|acked_reject),
  state_since(RFC3339 string,僅 adoption_state 轉變時更新 —— stuck 告警的
  時鐘,held 內 rev churn 不得重置;2026-08-28 Phase C review 增補)**},
  ts = adopted_at;MQTT 與 k_model 共 topic
  `monitor-air/ref/k/<target>/<source>/<regime>`,payload = {model:{...},
  adopted:{...}} 兩節、各含自己的 epoch;**epoch rollover = 覆寫 payload 為新
  epoch 內容(topic 不含 epoch,不清除)**;發空 retained **僅在**桶退出
  universe;**k_adopted 僅在 value/state/pending/ack 任一變更時寫 Influx**
  (point ts = 該變更時刻,adopted_at 語意因此成立;未變更輪不寫,
  保護採納時間與 as-of 重建);retained MQTT 則每輪全量重發
  (Influx 先寫、MQTT 後發,失敗下輪重發即冪等)
- **採納狀態機** adoption_state ∈ {seed(初值,非正經值), adopted, held, stale}:
  「已有正經採納值」= state=adopted;hold 觸發時 state=held、值凍結在 prev;
  **epoch 切換的沿用轉移**:新 epoch 建立時,若前代桶(同 target×source×regime,
  prev epoch)state=adopted → 自動寫入新 epoch 桶 {value=前代值,
  state=adopted, reason=carry, 期限=start+30d};30d 內未有本 epoch 估計晉升 →
  state=stale(carry-expired),值仍黏住(不無聲回 seed)但持續告警;
  **解除 = `ack-k-hold.sh <target>/<source>/<regime> --evidence-rev <rev>
  --accept|--reject`**(epoch 預設 current,舊代需顯式 `--epoch <id>`;
  **CAS 權威來源 = k_adopted 最新 row 的 pending_evidence_rev**,不符 →
  拒絕並印最新 pending 內容要求重審(人工剎車的確認對象不得漂移);
  accept=value←pending_value、state 回 adopted、pending 清空;
  reject=pending 清空、hold_ack_state=acked_reject(該 rev 不再告警)),
  寫 audit row。**hold 轉移**:觸發 → 寫入 pending_{value,model_id,
  evidence_rev}、hold_ack_state=awaiting;同 rev 重跑 → 不變不重發;
  新 rev 到達且仍跳帶 → pending_* 更新(新 rev = 新告警);
  **併發**:ack-k-hold.sh 與 compute 共用同一把排他鎖
  (`flock /tmp/k-models.lock`),取鎖後重讀 latest pending rev → 比對 →
  寫入,才構成 CAS(Influx 讀後寫本身不是);
  **回復轉移(補全)**:held 期間新一輪估計回到帶內(valid 且 |Δ|≤10%)或
  降級(非 valid)→ pending 清空、hold_ack_state=none、state 回 adopted
  (現值不變),k_event 寫 event_type=resolved(觀測用,不發 Telegram);
  stale 桶重新達 valid → 走正常自動採納表(帶內無聲採納/跳帶 hold),
  adoption_state 於採納時回 adopted;held 期間 epoch 切換 → pending 清空,
  新 epoch carry 規則接手;
  fixture(Phase C):hold 後同 rev 重跑、舊 rev ack 失敗、新 rev ack 成功、
  reject 後同 rev 靜默、**held 回帶自動解除、stale 恢復採納** —— 全鏈路;告警去重 key =
  **(桶, event_type, evidence_rev)**(model_id 含 computed_at 每輪皆新,不可作去重
  身分):hold 存續期同 evidence_rev 只首發;ack 後同 evidence_rev 永不再發;
  新 evidence_rev(輸入變了)才可能再發
- **告警落地**:compute 寫 `k_event`:tags {target, source, regime, **epoch**,
  event_type(**hold|stale|resolved**), device(=站名,配合既有 Telegram
  grouping alertname+device)},fields {model_id, estimate, adopted_value, message},
  ts=事件時刻;fields 另含 evidence_rev;**寫入前查同 (桶, event_type, evidence_rev)
  已存在則跳過**(去重)。
  Grafana rule 進 rules.yaml.tmpl(uid=k-alerts):count(k_event **where
  event_type == hold or stale**, 近 1h)> 0(resolved 只入庫供觀測,明確
  被過濾 —— rule 驗收含「寫入 resolved 不觸發告警」),
  維度做成 labels(照該檔既有 series-collapse 慣例);近 1h 無 rows 自動解除;
  NoData = OK(k_event 本來就稀疏)。
  **修訂(2026-08-28,Phase C review):加第二條狀態驅動規則 uid=k-adoption-stuck**
  —— k_event 去重(同 rev 只首發)+ 1h 窗意味著漏接一則 Telegram 就會讓 held
  桶永遠靜默等待,carry-expired 的「持續告警」也只會是一響。stuck 規則讀
  k_adopted 最新 state:held|stale 持續 >24h → 持續 firing 直到狀態離開
  (ack/回復/重新晉升)。24h 起跳,不與事件首發重複;「持續告警」契約由此承擔
- 自動採納規則(compute-k-models.sh 每輪執行):
  | 情境 | 動作 |
  |---|---|
  | **adoption_state = seed** 且 status ∈ {provisional, valid} | 採納(bootstrap;判 state 不判數值 —— as7341 的 seed 是 0.0017469 非 1.0) |
  | 已有採納值,新 **valid** 估計與現值差 ≤ ±10% | 無聲採納更新 |
  | 已有採納值,新估計差 > ±10% | **hold + 告警**(人工判:漏登 epoch?) |
  | provisional 估計 vs 已有的正經採納值 | 永不頂替 |
  | stale | 保持現值 + 告警提醒補量(**絕不**自動退回 1.0) |
- ±10% 帶與 90d stale 為 v1 常數,改值 = versioned migration

## 統計規則與 status 狀態表
- session 為獨立單位;比例係數在 **log 空間**計算;CV 用 log-variance 換算
- **status 決策表(由上而下,首個命中即定;互斥)**:
  | 條件 | status |
  |---|---|
  | 桶無任何 session 或 n_sessions = 1 | unvalidated |
  | epoch = e0-legacy(硬體設定未知) | 上限 provisional,永不 valid |
  | 2 ≤ n_sessions < 5(或 n_eff < 3 / CI 不可算) | provisional |
  | CI 相對寬度 > 20% | provisional |
  | 達門檻但 last_ref_age > 90d | stale(曾 valid 也降) |
  | 其餘 | valid |
  (epoch carry **不影響 k_model status** —— 新 epoch 無 session 就是
  unvalidated,誠實反映證據;沿用由採納層的 adoption_state=adopted +
  reason=carry 表達,兩層語意分離)
- **無 fallback**:空桶/未達標就是無校正,絕不把 global 中位數偽裝成該桶的值
- 精確定義:CI 相對寬度 = (ci_hi − ci_lo) ÷ estimate;last_ref_age =
  執行時刻 − 該桶最新合格 session 的中位時間(days);coverage = 執行時刻往回
  90d 內有合格 session 的 ISO 週(UTC)數 ÷ 13(觀測用,不進 status 判定)

## Epoch registry
- `broker/epochs.json`(append-only,僅由 `mark-epoch.sh` 寫入;script 驗證
  不重疊、不修改既有條目)。**區間 = 半開 [start, 下一筆同 target 的 start)**,
  start 為 RFC3339 UTC。範例:
  ```json
  {"epoch_id":"lux_ref-e1","target":"bh1750_lux_ref",
   "start":"2026-08-28T00:00:00Z","reason":"registry 建立,現況回填",
   "device":"livingroom",
   "config":{"address":"0x5C","position":"<盤點後據實填>","optics":"<盤點後據實填>"},
   "photone":{"phone":"<型號>","app_ver":"<版本>"},"prev":"e0-legacy"}
  ```
- config schema 依 target:bh1750_* = {address, position, optics};
  as7341_ppfd = {gain, tint_ms};photone identity 屬所有 target
- **epochs.json 頂層 schema = `{"epochs":[...], "station_map":[...]}`**
  (mark-epoch.sh 與 fixture reader 皆以此為準)
- **station-map(versioned,epochs.json 同檔、mark-epoch.sh 維護與驗證)**:
  ```json
  "station_map": [{"device":"livingroom","light_location":"livingroom",
                   "valid_from":"1970-01-01T00:00:00Z"}]
  ```
  epoch/station-map 的 **start/valid_from 必須為 UTC 5min 格起點**
  (mark-epoch.sh 拒絕未對齊值 —— 否則 light_context 的 5min 格無法無歧義
  歸屬 epoch;人工標記事件對齊到 5min 零成本);格中 epoch 案例入 fixture(拒絕)。
  append-only;同 device 多筆依 valid_from 嚴格遞增、半開區間
  [valid_from, 下一筆 valid_from);相同/倒退 valid_from、修改既有條目 →
  mark-epoch.sh 拒絕。舊 photone rows(無 light_location 欄)以 row ts 查表
  回填;consumer join light_context 前先以自身 device 查表解析 location;
  mapping 邊界案例入 fixture
- **device 綁定**:每 target 的 epoch 宣告唯一 device;estimator 只取
  row.device == 宣告值(staging/他站 row 一律排除,不混算)
- per-target 各自分代;registry 建立前一律 `e0-legacy`(status 上限 provisional)
- 分代維度:sensor 更換/移位/傾角、擴散罩、盒/光罩、AS7341 gain 或積分、
  **Photone 手機/app 版本**(參考儀器也是儀器)
- 計算永不跨 epoch 取數
- **新 epoch 起始:k_model status = unvalidated**(無本 epoch 證據);
  值的連續性由採納層表達:carry row 為 adoption_state=adopted、reason=carry、
  限 30d(見採納狀態機)—— 絕不無聲切回 seed

## 套用層
- **v1 不物化 corrected 序列**。k_model 發布 schema(桶主鍵完整入 key,防覆寫):
  - Influx `k_model`:**tags = {target, source, regime, epoch}**,fields =
    {estimate, ci_lo, ci_hi, n_sessions, n_eff, coverage, last_ref_age_d,
    status(string), model_id, evidence_rev},point ts = computed_at;MQTT model 節同列 evidence_rev。(模型有效期即「該桶最新
    一筆」,由查詢 last() 取得);model_id = "<target>/<source>/<regime>/<epoch>@<computed_at>"
  - retained MQTT:**`monitor-air/ref/k/<target>/<source>/<regime>`**(每桶一 topic,
    epoch 在 payload),payload = 上述 fields 的 JSON;桶消失時發空 retained 清除
    (publish-weight-ref.sh 的既有生命週期模式)
  - 面板從受控派生查詢 join 各桶 last();觀測用,正確性不依賴 MQTT
- 面板/告警遷移列 migration matrix(每個面板標 raw/corrected/legacy),切換前
  平行比對 + 日積分差異驗收;確認語意後才討論物化 `*_corr`
- DLI/燈控**先不動**,等 lux 模型 valid 且平行比對通過

## 下游級聯(順序即依賴)
1. `bh1750_lux` 模型 valid(daylight 桶)→ 面板逐一遷移
2. CAL:`as7341_ppfd` 模型本身就是 CAL 的重新擬合(獨立鏈,隨時可做,
   calibrate-ppfd.sh 改為向 k_model 輸出而非只印值)
3. ratio canary 基準重定(分母用 corrected lux 的派生查詢)
4. 燈下 k_spec:= `as7341_ppfd` 的 lamp 桶,同框架內自然長出,不另起爐灶

## 運維監控
- 管線 heartbeat、最後成功時間、每桶 coverage/fallback 比率、模型版本變動
- 參考資料斷供:last_ref_age 超過(90d?)→ 該桶 valid→stale,發告警
- 保留一條 **common-mode 檢查**:ratio-flat 看不見均勻光學衰減
  (daily.json 已知限制)→ 固定燈輸出的絕對幅度監測 + 定期 Photone colocated check

## 實作階段
每 phase 一個產出物;A 完成後**收數即刻開始且永續**(持續活動,不設 phase):
每週 1–2 筆 daylight(SOP:貼 ref 位置角度)、晴天中午直射窗刻意收、
lamp 隨緣、mixed 照量。歷史資料已存在,B 起即有桶可算。

### Phase A — 記錄端(產出:帶完整 metadata 的 photone rows)
- [ ] record-photone.sh:
  - 每次自動查燈態(量測 timestamp 回查 Influx light.on transition),寫 `lamp_state`
  - 計算並記錄量測時刻太陽仰角(診斷欄位)
  - source 自動推導(見 source resolution contract);`--source` 改為可選覆寫
  - 使用者介面:`--ppfd`、`--lux`(+ 既有 --at/--note/--dry-run;燈開白天
    `--source daylight` = ref 錨定收錄)
- [ ] `photone` v2 新增欄位(舊 rows 缺欄 → 一律按 e0-legacy 規則處理):
  | 欄位 | 型別 | 說明 |
  |---|---|---|
  | `light_location` | tag | **station-map(device, 量測 timestamp)** 解析;registry 未建立時才用 .env bootstrap(寫入端移位邊界案例入 fixture) |
  | `lamp_state` | field float | 1=開 0=關 -1=UNKNOWN |
  | `sun_alt_deg` | field float | 量測時刻太陽仰角 |
  | `source_override` | field float | 1=人工覆寫 source 推導(estimator 納入但帶旗標,hold 審查可見) |
  | `gain_x` | field float | 配對窗 telemetry 眾數(倍數,如 4.0);與舊 tag `gain`("4x")並存,舊 rows 無此欄=e0-legacy |
  | `config_override` | field float | 1=gain/tint 來自 CLI 覆寫 —— **該 row 一律不進 as7341_ppfd** |
  (ref-only row **不另設欄位** —— (source=daylight, lamp_state=1) 即完整識別)
  - epoch config 比對:gain_x 精確枚舉相等;tint_ms 精確相等(兩端同源計算,無容差)
- [ ] 任意時刻太陽仰角計算(擴充 solar-noon.py 或內嵌公式)
- [ ] Telegraf light topic 保留 `source` tag(燈態狀態契約前置);
  **部署生效驗收**(FLOWS.md 記載單檔 bind mount 會 inode 脫離,restart
  不保證載入新檔):改檔後 force-recreate 而非 restart;驗收 = 觸發一筆
  seed/checkpoint 後查 Influx `light`,必須同時有 location、source tag 與
  on field;並比對 container 內外 conf inode 一致
- [ ] **firmware:spectrum payload 加 `gain`、`tint_ms` 欄位**(配置身分必須是
  量測事實,CLI 宣告不算):
  - payload:`"gain":4.0`(float,倍數)、`"tint_ms":280.8`(float,毫秒),
    取韌體當下實際設定(AMBIENT_GAIN、ATIME/ASTEP 換算);Telegraf JSON float
    自動入 `spectrum` measurement,無需改 conf
  - record-photone.sh:gain/tint 改取**配對窗內 telemetry 的眾數**;
    `--gain/--tint-ms` 降級為 override(使用即警告 + 寫 override 旗標);
    窗內出現兩種配置(跨窗變更)→ 拒配對;fixture 含此案例
  - 歷史 rows 無 telemetry gain → e0-legacy 規則(上限 provisional,本就相容)
- [ ] **controller 每日 state checkpoint**(讓 26h staleness 契約成立;現況
  同態不發布,燈連續多日同態會令歷史回查誤判 UNKNOWN):
  - light.py 每日 **03:00 Taipei** 讀插座**實際狀態**並發布
    state(source:"checkpoint",retained 同現行);讀取失敗重試 3 次,
    仍失敗僅 log error(當日缺 checkpoint = 可能踩 26h UNKNOWN,可接受)
  - fixture:「連續 >26h 同態但有 checkpoint → 狀態可信」案例
  - **controller 層自測** `light.py --selftest`:固定 Taipei 時鐘 + 模擬同態
    插座,斷言發出一筆 retained state 含 source:"checkpoint" 與正確 on 值;
    模擬讀取三連敗 → 斷言不發布且主迴圈不中斷
- [ ] `broker/epochs.json` + `mark-epoch.sh`;首筆 epoch 回填現況
- [ ] canopy 量測點物理標記(硬體動作,使用者)
- [ ] **0x5C 首 epoch 前實體盤點**(硬體動作,使用者):實際 optics、位置、
  朝向、遮罩(sensors.h 記載它 shielded from grow lamp —— 遮罩幾何影響
  「純感測器校正」的可解釋性,必須據實入 epoch config,不可預填 bare);
  拍照存 docs/media/,epoch config 引用檔名
- 驗證(可判定,全部給命令與預期):
  - `--dry-run` 正常時刻:輸出含 lamp_state/仰角/推導 source,與人工核對一致
  - `--at <歷史時刻>` ×2(一筆燈開、一筆燈關的已知時刻):source 推導正確
  - 燈態 UNKNOWN 案例:無 --source → 拒收;有 --source → 收,帶 override 旗標
  - `--source` 與推導矛盾 → 中止且顯示兩者
  - `mark-epoch.sh` 重疊區間 → 拒絕;合法新增 → append 且既有條目不變

### Phase B — 計算端(產出:k_model 序列,Influx + retained MQTT)
- [ ] compute-k-models.sh:estimator spec Step 1–4 + status 決策表 → 先寫 Influx、
  後發 MQTT(冪等,失敗下輪自癒);排程 = **host crontab**(照 influx-backup.sh
  慣例;**鎖內建於 wrapper(flock -w 300),cron 行不得再包 flock —— 同鎖檔巢狀
  = 死鎖**):`10 * * * * /home/jiarung/monitor-air/broker/compute-k-models.sh >> /tmp/k-models.log 2>&1`
  (**具體帳號絕對路徑 + /tmp log** —— repo 已記錄過佔位路徑與不存在的 /data/
  曾令 cron 從未跑過;README.md:429 事故),token 從 broker/.env 讀;
  驗收:`crontab -l | grep k-models` + 整點後 `tail /tmp/k-models.log`
- [ ] fixture = **多 measurement 離線重放目錄** `broker/fixtures/`(版本控制):
  {photone.lp, light.lp(含 seed/重啟/26h 斷流案例), air.lp(lux_ref 供 5min
  聚合), epochs.json, station-map, computed_at.txt(固定執行時刻)};
  `compute-k-models.sh --fixture <dir>` **完全離線**(讀 line-protocol 檔,
  不碰 Influx/MQTT),輸出 canonical artifacts 依 phase 分:**Phase B 驗收比
  {k_model, light_context}.json;Phase C 擴充比 {k_adopted, k_event}.json**
  (B 階段採納引擎未啟用,後兩檔不產出),diff `fixtures/expected/` = pass/fail。案例涵蓋:正常
  daylight session、guard-band、sentinel、30min 連鎖、epoch 邊界、空桶、
  transition 恰在量測時刻、seed 改變狀態、device 不符、gain/tint 不符
  (排除)、**同配置下 S 不因 gain/tint 值而變**、
  通道飽和、歷史 `lux` 欄位相容、**gain_x=4.0 與 legacy gain="4x" 並存
  (epoch 比對走新欄位)**、**config_override=1 且配置相符仍不得產生
  as7341_ppfd session**、**缺 gain_x 的 e0-legacy 上限行為**、
  **ref-only row(daylight ∧ lamp_state=1)只產 bh1750_lux_ref session
  (daylight×regime 桶),其他 target 零 session**
- 驗證:fixture diff 全過;歷史真實資料跑一輪,人工抽查 3 個桶的 session 分組合理
- **本 phase 不含採納**:k_model 只算只發,誰都還不消費

### Phase C — 採納端(產出:k_adopted 黏性序列 + 告警)
- [ ] 採納引擎(併入 compute-k-models.sh 每輪尾段):依 Step 5 採納表寫 `k_adopted`;
  hold/stale 事件發告警(既有 Telegram 通道)
- [ ] Grafana `k-models.json`(uid=k-models):每桶 estimate+CI 時序、status 表、
  coverage、**k_adopted vs k_model 對照**(hold 中的桶一眼可見)
- 驗證:fixture 擴充採納情境(bootstrap、±10% 內無聲更新、跳帶 hold、provisional
  不頂正經值、stale 保值)逐案 diff;真實資料上首批 bootstrap 採納值人工過目一次
- 完成即進入無人值守:此後人工只處理 hold/stale 告警

### Phase D — 消費端遷移(產出:下游逐一改讀 k_adopted)
- **逐點選桶機制**:compute-k-models.sh 附帶物化 `light_context` 序列,schema:
  measurement `light_context`,tag {location},fields {source(string),
  regime(string), epoch_lux_main, epoch_lux_ref, epoch_as7341(各 target 的
  epoch_id string,由 registry 區間查得)};**row ts = UTC 對齊 5min 格起點,語意覆蓋 [ts, ts+5min)**;regime 判定
  輸入 = 該格內 telemetry lux_ref 樣本 **mean**(15s 週期正常 ~20 樣本,
  **0 樣本 → UNKNOWN**,不 forward-fill);格內含燈態變化點、**或格內跨越太陽仰角 0°
  交點(日出/日落)**→ 整格 UNKNOWN(否則同格前後半分屬 lamp/mixed 或
  拒收/daylight 會被錯套;fixture 含日出、日落各一例);
  mean 落 guard band → daylight 格 UNKNOWN;資料落後 >15min 該格留空(=UNKNOWN)。可全量重算,冪等
- **canonical consumer join(順序固定,不可任選)**:每筆 raw sample
  (1) `context_time = date.truncate(_time, 5m)`(UTC);
  (2) location = station-map(device, `_time`);
  (3) 以 {location, context_time} join light_context 取 (source, regime,
  epoch_<target>);(4) 以四 tags join k_adopted 取值;
  (5) **在原始 `_time` 上乘係數,之後才做任何 aggregate/DLI 積分**
  (join 先於聚合 —— 順序顛倒會改變 DLI 與轉燈格數值);
  缺 context → 既有 uncorrected fallback。fixture:非整 5min 的 telemetry、
  格內燈態變化、DLI 積分對照;**context=UNKNOWN、context.source=mixed、或 (target, context) 不在合法矩陣
  (如 ref×lamp)→ 一律乘 per-target seed 常數(bh1750_*=1.0、as7341_ppfd=
  現行 CAL 0.0017469;寫死於 consumer 查詢的 fallback 分支)並帶 uncorrected
  旗標(值區分 unknown|mixed|out-of-matrix)**;合法矩陣內的桶必有 k_adopted
  row,且 **row 的 adoption_state=seed 時同樣帶旗標(值=seed)** —— 乘 seed
  在數值上就是未校正,旗標讓觀測端分得出「校正中」與「還沒校到」。
  v1 對混光誠實不校,疊加拆解留 v2。這物化的是「桶標籤」非 corrected 值,與 v1 原則相容
- **套用語意 = epoch-current**:同一 epoch 內全部資料點用該桶**當前**採納值
  (k 是感測器屬性,epoch 內恆定;歷史校正隨估計改善而改善)。後果與補償:
  採納更新會使歷史查詢結果微動(±10% 剎車限幅)—— 每次採納打 Grafana
  annotation;任何過去的查詢結果可由 raw + k_adopted 全史精確重建
- 選桶表(per consumer):lux 面板/main → bh1750_lux_main×context;
  遮燈 DLI/ref → bh1750_lux_ref×context;PPFD/CAL → as7341_ppfd×context;
  canary 分母 → 同 main
- **migration matrix(逐檔,依序遷移;每項:平行期 7d、日積分差異 ≤2% 或
  可解釋、切換日打 annotation、回退 = revert query,raw 未動故零資料風險)**:
  | # | 消費者 | 現行 | 目標 |
  |---|---|---|---|
  | 1 | air.json Spectrum PPFD(硬編碼 CAL) | CAL×S | as7341_ppfd 桶值 ×S(canonical join) |
  | 2 | air.json DLI-lux / daily.json 總 lux | lux/54 | (lux×k_main)/54 |
  | 3 | daily.json 遮燈 DLI(lux_ref/54) | lux_ref/54 | (lux_ref×k_ref)/54 |
  | 4 | canary(clear÷lux)基準 | 舊區間已作廢 | 分母改校正 lux 後重定 |
  | 5 | ppfd-cal-daily.sh / cal-review-reminder.sh | 自算 CAL | 改讀 as7341_ppfd 桶(或退役,職能被管線吸收) |
  | 6 | **light.py 控制 DLI(直接 integral raw air.lux)** | raw | **最後遷移且需使用者單獨核可**;uncorrected/seed 期間一律維持 raw 行為 —— 燈控絕不因校正狀態切換而跳變 |
- 驗證:light_context 對已知燈程/日照日抽查正確;每項切換前後日積分差異可解釋;
  告警不誤發

## 明確不做(v1)
- 連續時變曲線(spline/GAM)—— v2,且僅在仰角效應有統計證據後
- firmware NVS luxScale(時變係數進 device 會讓 raw 語意隨時間變)
- corrected 序列物化 —— 等面板語意確認
- lamp 桶的主動收數計畫 —— 隨緣,燈譜穩定不急
- `--position` 欄位 —— position 是 source 的確定函數(SOP 對映即 schema);
  偶發的貼 main 診斷配對以 `--note` 標
- `--sky` / 任何人工天氣標籤 —— regime 由 lux_ref 量測值客觀判定(見 regime 節)
