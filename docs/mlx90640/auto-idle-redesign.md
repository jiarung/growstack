# auto-idle 重新設計

_2026-09-29。取代 `025229d` 的 auto-idle 流程。機制背景見 [phase-1b.md](./phase-1b.md)
〈過熱追查〉,量測程序見 [overheating-procedure.md](./overheating-procedure.md)。_

**量到的和沒量到的分開**,沿用 overheating-procedure 的規則:沒有量測支撐的標 **[未驗]**。

---

## 0. 根因:RGB 鏡頭(OV5640 模組)本身發熱

這一節之前只存在對話裡(2026-09-22 → 09-29),從沒進過檔案。

| 事實 | 證據等級 | 來源 |
|---|---|---|
| 燙的是**鏡頭**,不是穩壓器、不是 S3 | 使用者手摸 | 09-22「攝影機還是很燙」;09-23「我應該有明確的說是鏡頭很燙吧?」 |
| sensor 待機(0x3008 bit6)後鏡頭**不燙** | 使用者手摸 | 09-23「現在看起來 idle 就不燙了」 |
| 所以熱 = OV5640 醒著時的 duty cycle(持續讀出 + **sensor 內部** JPEG 壓縮) | 由上兩列推論 | `025229d` |
| 停下 sensor 的效果在 S3 die 上也看得到:50.5 → 42.5(5 min)→ 37.5 °C(~20 min) | 量測,**die_c 不是鏡頭** | `d67b716`,cam=idle + xclk=10 |
| 只降 xclk 20→10、sensor 醒著:die −2.0 °C | 量測,但該輪程序作廢(cam 被叫醒過) | 09-23 soak |
| 鏡頭本體溫度 | **沒量過**。MLX90640 反向對板(程序 §5)從未執行 | — |

**對設計的意思**:能讓鏡頭涼下來的唯一被證實的手段是「sensor 不在跑」。降 xclk、降解析度
只是降 duty,效果只在 die 上量過、而且小。所以 **auto-idle 不能拆**(使用者 09-29 明確否決
過「拆掉 auto-idle」—— 拆掉等於沒有過熱解法),要修的是它醒不過來的那一半。

---

## 1. 舊設計壞在哪

```
auto-idle tick → 寫 0x3008 bit6(sensor 停止輸出)
                  ESP32 的 DVP/DMA 沒被通知,仍在等 VSYNC
wake          → 清 bit6,delay 1.2 s
                  sensor 恢復,DMA 狀態機留在停格 → driver 反覆遞回同兩個舊 buffer
capture       → fb->timestamp 永不前進 → captureFresh() 3 s timeout → 500 "capture failed"
              → 永久,直到斷電(09-29 實測 11/11 失敗;set_framesize 救不回)
```

真正的缺陷不只是 0x3008,而是三件事疊在一起:

1. **只改了 sensor 狀態,沒改 driver 狀態。** 兩者從此不同步。
2. **喚醒沒有被驗證。** 「醒了」的依據是 `cam_idle=false` 與 `0x3029=0x70`,兩個都是
   sensor 自己的暫存器 —— 而壞掉的是 ESP32 那一側。
3. **失敗沒有出路。** 新鮮度 timeout 只回 nullptr,沒有任何一層會嘗試恢復,也沒有任何
   狀態說「相機已卡死」。結果看起來像偶發的 500,追了一輪 PSRAM 碎片化。

外加驗收漏洞:`025229d` 只驗了溫度,沒驗「idle 之後還拍得出來」。

---

## 2. 不變量(驗收直接對這幾條)

- **I1 冷卻**:沒人用時 sensor **確實停止**(standby),不是只降速。
- **I2 醒得來**:任何喚醒之後,要嘛拿到新鮮幀(`timestamp` 晚於請求),要嘛在有界時間內
  回報**具名**的失敗。沒有「永久」。
- **I3 喚醒以 ESP32 側為準**:「醒了」= 拿到一張新鮮幀。sensor 暫存器(0x3029、cam_idle)不算。
- **I4 設定不因喚醒而變**:xclk、rest size、`/cam/tune`、翻轉,喚醒前後一致。
- **I5 看得見**:冷態進出、喚醒耗時、恢復、失敗都有計數;卡死是**狀態**,不是一串 500。
- **I6 客戶端先聽到答案**:任何請求在客戶端 timeout(`observe.TIMEOUT_S = 10`)之前拿到
  回應 —— 成功、或具名的「稍後再試」、或具名的失敗。不能讓 client 自己逾時而拿不到原因。

---

## 3. 新流程

_2026-09-29 codex review 後從頭重推導(§7 有逐條裁決)。_

### 3.1 狀態與轉移

```
          需要相機(capture / stream / af / cam=active)
   ┌──────────────────────────────────────────────────────┐
   ▼                                                      │
 AWAKE ──(autoidle 秒無活動)──► COLD ──(需要相機)──► WAKING ─┤ 成功
   │                                                      │
   └──(新鮮度 timeout,且 claim 已清乾淨)──► WAKING          │ 兩次失敗
                                                          ▼
                                                       WEDGED
```

### 3.2 誰執行轉移 —— 一個 coordinator,不在 loop、不在呼叫端的堆疊裡

**所有轉移由一個專用 FreeRTOS task(`cam_power`)執行。** 其他人只送「請求」給它。

| 不能放的地方 | 為什麼 |
|---|---|
| `loop()` | loopTask 是唯一排空熱像 UART 的 task;重建要數秒,`thermal::poll()` 餓死就丟 byte(`0d9fa90` 量的就是這個) |
| httpd handler 裡同步做 | 重建(估 3–6 s,**[未驗]**)+ QSXGA 單張 5–8 s(phase-1b 實測)很可能 > 10 s,違反 I6;而且 httpd 單 task,等待期間 `/thermal` 4 Hz 輪詢全部卡住 |
| `captureFresh()` 裡面 | 呼叫端還持有 `inUse`,而 `CameraExclusive` 要等 `inUse == 0` —— 自己等自己 15 s,把可恢復的情況判成 WEDGED |

**鎖階層**(`af::load()` 自己會拿 `CameraExclusive`,mutex 不可重入):

```
cam_power task:  CameraExclusive ──► enterCold() / rebuild() / af::loadLocked() / regReadLocked()
其他所有人:      只能呼叫「不拿鎖的請求函式」或既有的 CamLock 讀寫;不得在轉移中呼叫任何會再拿鎖的函式
```

- `af::load()` 拆成 `af::loadLocked()`(假設鎖已持有)+ 原 `load()` 薄包裝
- `af::status()` 在 WAKING 中不讀硬體,只回 `state=waking`

### 3.3 進入 COLD

```
cam_power:  CameraExclusive(等 inUse==0 && !streaming)
  1. 寫 0x3008 bit6        sensor 停(熱源停)
  2. esp_camera_deinit()   DVP/DMA/cam task/framebuffer 全部拆掉;記錄回傳碼
  3. state = COLD
```

先 1 後 2:deinit 會把 SCCB 一起關。**[未驗]** deinit 本身不碰 sensor 暫存器、sensor 在
deinit 後維持 standby —— deinit 後 SCCB 已關、讀不到暫存器,所以**無法直接驗**;
間接驗 = P3 的冷態鏡頭溫度。

### 3.4 WAKING —— 非同步,客戶端重試

請求端(`cameraCapture()`、stream、af)看到 state ≠ AWAKE:

```
  COLD | WAKING → (COLD 時送 wake 請求給 cam_power)立即回 503 "camera waking — retry"
  WEDGED        → 立即回 503 "camera wedged: <stage>: <err> — power cycle required"

  每個 503 都帶 X-Cam-State: waking | stalled | wedged    ← 客戶端只看這個,字串給人讀
  waking / stalled 另帶 Retry-After = 預估剩餘喚醒時間(由 wake_ms_last 推,首次用保守值)
```

cam_power 執行:

```
  1. esp_camera_init(config from CamSettings)   記錄回傳碼;NO_MEM 與其他錯分開記
  2. 重套 CamSettings(§3.6)
  3. af::loadLocked()                           失敗 ≠ 喚醒失敗(定焦也能拍),只計數
  4. 取新鮮幀(VGA)驗證                          成功 → AWAKE
     任一步失敗 → deinit,重試一次 → 再失敗 → WEDGED(記下哪一步、錯碼)
```

客戶端:重試放在 `tools/s3cam/board_http.py`(所有工具取板子資料的唯一入口),
`X-Cam-State` 為 waking / stalled 時依 Retry-After 重試,總時限另設(例:30 s)。
不放在 `observe._get` —— viewer 的 `/thermal` 代理、focus/vcm 掃描直接打 `/capture`,只修一條
路徑就會重演 `d74c981` 之前「一條路修好、其他路照舊」的狀況。所以 I6 在「每一個 HTTP 請求」的層級成立,整體等待由 client 決定。

**[未驗]** init 會軟重置 OV5640(0x3008=0x82)從而解除 standby、standby 中 SCCB probe 可靠
—— 都不寫成事實。「喚醒成功」的判準只有一個:**冷態後 init 回 OK 且拿到新鮮幀**。

### 3.5 AWAKE 中途卡住

`captureFresh()` timeout → `cameraCapture()` 先**完整清理**(dropToRest、`inUse--`),回傳
typed 結果 `STALLED`(不再是單純 nullptr)→ handler 回 503 "camera stalled — recovering,
retry" 並送 recover 請求 → cam_power 走 3.4 的重建。
`STALLED`、`NOMEM`(psram)、`WAKING` 是不同的 `X-Cam-State` 值與不同的字串 —— `d74c981`
的教訓;分類靠 header,不靠比對字串。

**誤觸風險**:QSXGA 慢幀 / AE 收斂不會觸發,因為門檻仍是既有的 3 s 新鮮度 timeout,
而正常 QSXGA 幀遠小於此(5–8 s 的單張時間主要是排水,不是單幀)。**[未驗]**:P3 記錄
正常運作下 `STALLED` 次數必須為 0。

### 3.6 CamSettings —— 唯一真實來源,每個狀態下的 setter 契約

欄位:`xclk_mhz`、`rest_size`、`ae_level`、`gainceiling_x`、`brightness`、`hmirror`、`vflip`
(= 現有 `CameraTune` + xclk + rest)。預設值 = 現在的編譯期值(xclk 20、VGA、head_mount.h 翻轉、driver 預設 tune)。

| 狀態 | setter 行為 | 回應 |
|---|---|---|
| AWAKE | 套用到硬體;**成功才**寫入 CamSettings | 成功 `ok`;失敗 `rejected`,CamSettings 不變 |
| COLD / WAKING | 驗證範圍後只寫 CamSettings | `ok (pending — applied on wake)`,**明講** |
| WEDGED | 拒絕 | `rejected (camera wedged)` |

重套順序(init 後):xclk → 翻轉 → tune → rest size。`cameraInit()` 讀 CamSettings,
**不再寫死 20 MHz**。
`/cam/reg` 的原始暫存器寫入**不屬於設定、不會重套**,喚醒即消失 —— 跟斷電一樣,文件與
端點說明寫明。

### 3.7 WEDGED

- 所有相機端點立即回 503 + 具名原因(哪一步、錯碼);auto-idle 停;heartbeat `cam_state=wedged`
- 不自動無限重試
- 退路見 §6 P0 決策關卡(`ESP.restart()` 方案)

### 3.8 AWAKE 時的熱

醒著時維持 rest=VGA。xclk 預設是否改 10 **不在本設計內決定**(只量到 −2 °C die、那輪作廢),
列為 P3 可選 A/B。

---

## 4. 代價與風險

| 代價/風險 | 大小 | 對策 |
|---|---|---|
| **本 SDK 的 header 明寫 init「只能呼叫一次、無法 deinit」**(雖然宣告了 `esp_camera_deinit`) | **決定性** | 同 boot 重複 init 是**未受保證的行為**,P0 是這顆 `.a` 的資格測試,不是走形式 |
| 冷態後第一次拍照變成「503 → 重試」 | 確定 | client 端重試是設計的一部分(§3.4);viewer 顯示 "waking" 而非錯誤 |
| 喚醒 3–6 s | **[未驗]** | P3 量 `wake_ms`,並量**端到端** cold `/observation` 總時間 |
| 每次喚醒重配 2×QSXGA framebuffer → 碎片化 | **[未驗]** | 冷態時 buffer 已還回;P3 用 warm-up 後的**下限**判定(見 P3),不是「完全不下降」 |
| AF 上傳偶發失敗 | 開機時從未失敗 | 不算喚醒失敗;計數 |
| 新增 `cam_power` task,可能搶到 loopTask(熱像 UART 排空)的 CPU | **[未驗]** | 放在 httpd 那一核、priority 不高於 loopTask(1);P3 以 `poll_gap_max_ms` / `rx_errors` 驗 |

---

## 5. 明確不做(§3 沒提到的)

- 不用 `cam_stop`/`cam_start` 等 driver 私有符號
- 不改 PWDN:兩張板子 `CAM_PIN_PWDN = -1`

---

## 6. 分階段(每階段有驗收,不跨階段)

### P0 — 資格測試:這顆 `.a` 能不能同 boot 重複 deinit→init

最小韌體:診斷端點 `/cam/recover?n=<1..25>`,在 `CameraExclusive` 內:
standby → deinit → init → 重套翻轉 → 取 VGA 新鮮幀,重複 n 次。**不**重載 AF。
每輪回報:`deinit_err`、`init_err`、`init_ms`、`fresh`、`pid`、`psram_largest`、
`fw_state`(用 **locked raw read**,不是 `af::status()` —— 會重入鎖)。

```sh
for i in 1 2 3 4; do curl "http://<ip>/cam/recover?n=25"; done   # 共 100 輪
curl "http://<ip>/observation" -o /dev/null -w "%{http_code}\n"   # 之後 QSXGA 照常能拍
curl "http://<ip>/power?cam=idle"; sleep 3
curl "http://<ip>/cam/recover?n=1"                                 # 已卡死的板子救得回來嗎
```

決策關卡:
- [ ] **100/100** 輪 `init_err=0` 且 `fresh` → 走 §3(同 boot 重建)
- [ ] 卡死板子被救回 → §3.5 成立
- [ ] 失敗(任何一輪、或資源漸漸耗盡)→ **不走 §3.4 的同 boot 重建**。候選退路,各自要再驗:
      - **(R1) 冷態 = standby + deinit;喚醒 = `ESP.restart()`**。每次 boot 的首次 init
        是唯一被保證的路徑(`main.cpp` 已用 restart 做 WiFi 復原,舵機 PWM 不受影響)。
        代價:runtime 設定要存 NVS、held observation 消失、喚醒 ≈ 開機時間(> 10 s,只能靠 503 重試)
      - **(R2) 不 standby,只降 duty**(xclk + VGA)。不卡死,但鏡頭沒有真正涼下來
      - 兩者都是使用者的取捨,**P0 失敗時停下來問**

### P1 — host:狀態機 + 測試(不需要板子)

`cam_power.h` 純函式(狀態 × 事件 → 新狀態 + 動作 + 回應字串),host 編譯測試
(沿用 `test/thermal/host_runner.cpp` 模式)。

- [ ] AWAKE 超時 → COLD;COLD 被請求 → WAKING,請求端立即得到 503 waking
- [ ] 喚醒失敗一次 → 重試;兩次 → WEDGED(帶步驟與錯碼);WEDGED 不再轉出
- [ ] STALLED → 請求端 503 stalled;recover 請求**只在 claim 清空後**才被接受
- [ ] 串流中 / fb 外借中 / autoidle=0 → 不進 COLD
- [ ] AF 失敗 → 仍 AWAKE,計數 +1
- [ ] 每個狀態下的 setter 回應符合 §3.6 表格
- [ ] STALLED / NOMEM / WAKING / WEDGED 的 `X-Cam-State` 值與字串兩兩不同

### P2 — 韌體 + 客戶端

- `cam_power` task、請求佇列、`CamSettings`、`af::loadLocked()`、`cameraCapture()` typed 結果
- `/power` 與 heartbeat:`cam_state`、`cold_entries`、`wakes_ok`、`wake_ms_last/max`、
  `rebuilds`、`stalls`、`af_reload_fail`、`wedged_reason`
- `board_http`:`X-Cam-State` waking/stalled 依 Retry-After 重試,總時限 30 s;viewer 顯示 "waking"
- FakeBoard:waking(前 k 次 503 再成功)、wedged、stalled 三種模式 + 測試
- `tools/s3cam/health_soak.py` 加 `--observe` 每輪動作(沿用它的 FIELDS、`--` 斷線列、CSV),
  不另寫第三支 soak 腳本

驗收:
- [ ] `pio run -e s3cam` 與 XIAO env 都編得過;host 測試與 `test/s3cam` 全過
- [ ] codex review 至少兩輪

**P2 結束停下,不 commit、不燒錄,交給使用者。**

### P3 — 上機驗收(使用者燒錄、跑指令)

```sh
curl "http://<ip>/power?autoidle=3"
tools/s3cam/health_soak.py http://<ip> --observe --every 6 --count 30   # 旗標名稱 P2 定
```

- [ ] **30/30 輪 idle → `/observation` 成功且新鮮**(I2;上次漏掉的那條)
- [ ] 每一個 HTTP 請求都在 10 s 內得到回應(I6);記錄端到端總時間(含重試)
- [ ] `wake_ms_max` 記錄;autoidle 預設值依此重新決定
- [ ] 碎片化:第 3 輪之後 `psram_largest` 的**最小值** ≥ 2×fb 配置量 + 已觀測最大 JPEG,
      且 30 輪內沒有單調下降趨勢;`/observation` 從未回 psram exhausted
- [ ] 每次喚醒後 `fw_state = 0x70`
- [ ] COLD 時設 `xclk=10` 回 `pending`,喚醒後 `/power` 回 10(I4)
- [ ] 正常運作下 `stalls = 0`(§3.5 誤觸)
- [ ] `poll_gap_max_ms` 與 `rx_errors` 不比改版前差(cam_power task 沒餓死 UART)
- [ ] 冷態放 20 分鐘:**鏡頭摸起來不燙**(原始判準)+ die 收斂 ≤ 38 °C(對照 `d67b716` 37.5)
- [ ] 可選:MLX90640 反向對板(程序 §5),第一次給鏡頭一個真正的溫度數字

### P4 — 文件與 commit

更新 phase-1b.md、camera.h 註解、tasks/todo.md;commit。

---

## 7. codex review 裁決(2026-09-29,gpt-5.6-terra)

| # | finding | 裁決 | 落在哪 |
|---|---|---|---|
| H1 | header 明寫 init 只能一次;「公開語意明確」是錯的 | **對**(header:187 原文確認) | §4 首列;P0 改為資格測試、100 輪、記錯碼 |
| H2 | `af::load()` 自己拿 `CameraExclusive`,重入非遞迴鎖 | **對**(af.cpp:93) | §3.2 鎖階層、`af::loadLocked()` |
| H3 | 在 `captureFresh()` 裡恢復會等自己的 `inUse` | **對**(camera.cpp:335/579) | §3.5 typed STALLED,清理後才恢復 |
| H4 | 冷拍 = 喚醒 + QSXGA 5–8 s > 10 s | **對**(phase-1b:62) | §3.4 改非同步 + 503 重試;新增 I6 |
| M5 | CamSettings 缺欄位與各狀態 setter 契約 | **對** | §3.6 |
| M6 | P0 用 `af::status()` 會重入;PSRAM 判準太粗 | **對** | P0 locked raw read;P3 下限判準 |
| M7 | init 軟重置、deinit 後維持 standby 都是未驗前提 | **對** | §3.3/3.4 標 [未驗] |
| M8 | 漏了 `ESP.restart()` 退路 | **對**,且因 H1 升級為主要退路 | P0 決策關卡 R1 |
