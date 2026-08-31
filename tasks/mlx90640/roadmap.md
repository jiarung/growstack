# MLX90640 植物熱像 MVP — Roadmap

_細節 spec 在 [handoff.md](./handoff.md)(handoff 文件,當參考不當藍圖照抄)。
本檔只管:階段、產出、驗收閘、決策點。每個 phase 開工時才寫該 phase 的細節計畫
(photone 管線的教訓:一份文件塞全部 = 過長且會被照舊實作錯方向)。_

**一句話目標**:固定點 pan/tilt 拍出可分析的 RGB+thermal 配對資料,
量化三件事(熱像素解析度、registration 穩定性、量測重複性)之後,才談 X/Z。

## 原則

- 每 phase 一個產出物 + 可判定驗收;過了才開下一個
- 純軟體 phase 遠端可做;硬體 phase 標 🔧(需人在場)
- 新板 = 新裝置身分(獨立 MQTT_DEVICE_ID,staging 隔離老規矩)
- loop Codex review 照 photone 管線慣例;commit 前停

## Phase 0 — 三個決策(你裁,不擋 Phase 1/1B)

- [ ] repo 歸屬:growstack 新 platformio env vs 獨立 repo(1B 先在 growstack 開 env 原型,搬走不虧)
- [ ] observation 儲存/傳輸:RGB 走 SD 本地 + metadata MQTT?或 HTTP 上 broker?
      (決定 board 要不要 SD、broker 端要不要 receiver;1B 的實測餵這個決策)
- [ ] board 選型:手上的 ESP32-S3-CAM 為候選 —— **由 1B 的 GPIO/PSRAM 實測盤點裁定**,
      不夠(thermal UART + I²C 排不進去)才另尋

## Phase 1B — ESP32-S3-CAM + OV5640 flow/infra(今晚 🔧,與 Phase 1 並行)

產出:RGB 側完整 capture flow(live 串流 + still)+ 韌體骨架,和三份實測情報
- platformio 新 env(cam board)+ 模組骨架(camera/、storage/ 照 handoff.md §8 拆)
- OV5640 DVP bring-up:init、PSRAM framebuffer、JPEG capture、autofocus 動不動
- **capture flow 兩條都立起來**:
  - live view:MJPEG HTTP 串流(取景/對焦/日後 pan/tilt 對位的操作工具)
  - still capture:拍一張 → 落地(板上有 SD 走 SD,沒有先 HTTP 給筆電)
    + proto-observation JSON(pose/thermal 欄位 null,schema 照 §7)
  - MVP 的「資料」仍是 still(handoff §6 stop-settle-capture);串流是工具不是資料
- **盤點三件事回填 Phase 0**:GPIO 預算表(DVP 佔用後剩什麼腳,UART+I²C 排不排得進)、
  PSRAM 實際大小、SD 有無
- 驗收:連拍 10 張不重啟不掉幀;一張 observation(RGB+JSON)可回讀;盤點表寫進本檔

## Phase 1 — thermal parser ✅(2026-08-31 完成,2 輪 Codex loop 收斂)

產出:`src/s3cam/thermal/gymcu_parser.{h,cpp}`(純 C++,同一份碼進韌體與 host 測試)
+ `test/thermal/`(host runner + 合成 fixture,expected 由公式獨立推導)
- 八案例+timeout 契約全過:clean×3、雜訊前綴(孤 0x5A)、checksum 壞恢復、700B 截斷恢復、
  **dense_sync**(敵意 1538×0x5A 壞幀,迭代 resync 防 stack overflow 的回歸)、bad_header、
  負溫 int16、tail_sync(兩段式恢復);discardPartial() 承擔 roadmap 的 timeout 契約
- chunking 不變性 + latest-wins 不變量(len(frames)+overwritten==frames_ok)
- frame layout 常數(TYPE/SUBTYPE)標 VERIFY-ON-HARDWARE:模組到手後 Phase 2 對實流
- timeout 歸 Phase 2 的 UART 讀取層(parser 是純 byte 機器)

## Phase 2 — 硬體 bring-up 🔧

產出:board 上電,GY-MCU90640 真資料流過 Phase 1 解析器
- 供電:LRS-150-24 → 雙 buck,共地;**servo rail 餘裕要驗**(2×MG996R stall=5A 頂天)
- 驗收:連續 10 分鐘 4Hz 無壞 frame;室溫物體讀值 sanity(手掌 vs 桌面)

## Phase 3 — pan/tilt + scan workflow 🔧

產出:stop→settle→capture 的 scan controller(PCA9685 + 2×MG996R)
- **內建兩條對策:單向逼近**(每目標同方向進場,吃掉 backlash)、
  **兩軸永不同時驅動**(sequential move,兼顧供電餘裕)
- 驗收:A→B→C→A ×10,thermal ROI 位移統計(這就是 MVP metric #2 的雛形)

## Phase 4 — observation 整合 🔧(RGB 側已由 1B 拉前)

產出:scan 一次 = 完整 observation(RGB + thermal + pose + env),儲存依 Phase 0 定案
- environment 欄位接既有 monitor-air pipeline,不重造
- 驗收:完整 observation 落地可回讀;1B 的 proto flow 升級為正式路徑

## Phase 5 — registration + MVP metrics 🔧

產出:三個量測報告 → X/Z 去留決策
- repeatability(同 pose ×10:leaf temp mean/median/P10-P90、ROI 位移)
- registration 穩定性(pan/tilt 往返;**逐距離標定** —— 視差隨距離變,單一 homography 不成立)
- 空間解析度(0.5–1.5m 五檔:一盆植物佔幾個熱像素)
- 驗收:數據回答「固定點夠不夠」;不足才依 handoff.md §11 決策樹談 X/Z

## 明確不做(MVP)

X/Z 機構全家桶(handoff.md §11 清單)、continuous video、ML segmentation
(先人工 ROI)、bypass 模組 MCU 直驅 I²C(保留退路,不先做)。

## 風險備忘

- MG996R 重複性 ±1–2° vs 1.7°/熱像素 —— Phase 3 驗收就是在量這個,不夠就換數位舵機再談機構
- 供電 marginal 是本系統慣犯(AS7341 前科)—— sequential move + 餘裕實測,不賭
- registration 別做一組全域標定就宣告完成(距離敏感)
