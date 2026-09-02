# Phase 1B — ESP32-S3-CAM + OV5640 flow/infra(細節計畫)

_Roadmap 條目的展開。目標:今晚插板可燒,RGB 側 capture flow(live + still)立起來,
並產出三份實測情報(GPIO 預算/PSRAM/SD)回填 Phase 0。_

## 決策區(開工前要定)

### D1. 影像落地/讀取形式 ⬅ 你點名的那個

| 選項 | 形式 | 今晚成本 | 備註 |
|---|---|---|---|
| **A. HTTP pull(建議)** | 板上開三個端點:`GET /stream`(MJPEG)、`GET /capture`(拍一張回 JPEG)、`GET /observation`(拍一張回 JSON,含 base64 縮圖?否 —— JSON 引用 capture id) | 零 receiver 基建,筆電 curl/瀏覽器直接用 | 資料「存在筆電拉下來的那份」;正式儲存路徑(SD/broker receiver)留 Phase 0,不被今晚綁死 |
| B. HTTP push | 板主動 POST 到筆電上的 receiver script | 要先寫 receiver + 板要知道筆電 IP | 比較像正式架構,但今晚多一層可壞的東西 |
| C. SD 本地 | 寫卡,事後拔卡讀 | 依板上有無 SD 槽 + 手上有無卡 | 盤點完才知道可不可行 |

**建議 A**:pull 模型今晚零依賴;`/capture` 與 `/observation` 用同一 capture id 配對
(`cap-<UTC 時戳>`),JSON schema 照 handoff §7(pose/thermal/environment 全 null)。
curl 下來的檔名 = capture id → 讀取形式即檔名即 id,之後換 push/SD 只換傳輸不換 schema。

### D2. WiFi 供裝
沿用站上韌體慣例:secrets.h + 開機 serial 印 IP。你在外面 → 手機熱點 SSID 進 secrets。
(不做 mDNS/供裝 UI,MVP 不值得。)

### D3. 板子變體辨識(gating!)
「ESP32-S3-CAM」不是單一 pin map —— Freenove / Goouuu / 雜牌腳位各異,燒錯 map 卡 init。
**開工第一步是你提供:板上絲印/型號照片,或 serial 印出的 board id。**沒這個我只能給
最常見變體的 map + 備選表,現場對。

## 決策記錄(2026-08-30)

- **D1 = A(HTTP pull)定案**:/stream、/capture、/observation;capture id = `cap-<UTC時戳>`
- **D3 解:Goouuu ESP32-S3-CAM**,ESP32-S3-N16R8(16MB flash/8MB PSRAM octal)、
  PCB 天線、雙 Type-C(TTL+OTG)、**無 SD 槽(照片目視,step 7 現場確認)**
  → 主 pin map = ESP32S3_EYE/Freenove 系(XCLK15、SIOD4/SIOC5、D0-D7=11/9/8/10/12/18/17/16、
  VSYNC6/HREF7/PCLK13),cam_pins.h 附備選表
- SD 大概率無 → D1 選 A 更加正確;PSRAM 8MB 標稱,step 2 實測為準
- **骨架已建(env `s3cam` + src/s3cam/)並過第一輪 Codex review**:capture 新鮮度改
  依 driver frame timestamp 排水(單張丟棄不構成保證)、held observation 原子置換
  (配置失敗保留舊對,絕不舊圖新 id)、capture id 帶單調序號(同秒不撞)、
  timestamp 未同步時誠實 null(time_source/uptime_ms 補上)、QSXGA 實為 2560×1920
  (lib 定義,JSON 以 fb 實值為準)、/stream 佔住單一 httpd task —— capture 前先關串流

## 施工順序(每步一個可判定驗證)

| # | 步驟 | 驗證 |
|---|---|---|
| 0 | 板子變體辨識(D3)→ 選 camera pin map | 型號寫進本檔 |
| 1 | growstack 新 platformio env `s3cam` + 模組骨架 camera/ storage/(照 handoff §8) | 空 build 過 |
| 2 | OV5640 init + PSRAM framebuffer + 拍一張 JPEG 到記憶體 | serial 印出 frame size;PSRAM 實測值記錄 |
| 3 | `GET /capture`:HTTP 回 JPEG | 筆電 curl 存檔開得起來 |
| 4 | `GET /stream`:MJPEG live view | 瀏覽器看得到動態畫面,拿來對焦 |
| 5 | AF 探測:OV5640 AF 韌體下載流程動不動 | 動 → 記錄;不動 → 定焦 fallback,記錄 |
| 6 | `GET /observation`:capture id + JSON(§7 schema,null 佔位) | curl 兩個檔案 id 配對,回讀 JSON 過 schema 目檢 |
| 7 | 盤點表:GPIO 剩餘腳(UART×1 + I²C×1 排得進?)、PSRAM、SD 有無 | 表寫回 roadmap Phase 0 |
| 8 | 驗收:連拍 10 張(/capture ×10)不重啟不掉幀 | 10 檔齊全 |

## 驗收結果(2026-08-31 00:29,旅館熱點實測)

- ✅ PSRAM 實測 8,386,279 bytes(真 8MB);sensor=OV5640,主 pin map 一次命中
- ✅ 溫度:/stream 連烤 19min,die 45→55°C 收斂 ~54°C 無失控;熱假說降級為
  「串流模式餘裕有限」,stop-settle 實戰低 duty cycle 無虞(心跳帶 die=)
- ✅ 連拍 10 張:id 全異遞增(0013–0022)、~150KB 穩定、無重啟、psram 無洩漏;
  單張 5–8s(新鮮度排水 + QSXGA 幀時間)—— scan 節奏可接受,記錄
- ✅ observation 配對:JSON id == /last.jpg X-Capture-Id、time_source=ntp、2560×1920
- ⚠ 異常待重現:第二次 /observation 曾回空響應(id 未推進)—— 觀察中
- ❌ **對焦:0.3–2m 全距離肉眼皆糊** → 先查鏡頭出廠保護膜;撕膜後仍糊 =
  VCM 停在不可用位,**AF firmware blob 上傳升級為必做**(新增 step 9)
- GPIO 盤點:相機佔 4–18 內 14 腳,剩 1/2/3/14/19-21/35-42/47-48;
  thermal UART + PCA9685 I²C 綽綽有餘。SD:無(目視確認)
- 過程備忘:燒錄用 OTG 口(usbmodem)、log 在 TTL 口(CDC_ON_BOOT=0);
  GPIO2+GPIO48 開機壓 LED;AE -2 + gainceiling 8X;綠偏=AWB 暗場暫態非缺陷

## 明確不做(本 phase)

thermal/servo/任何 broker 整合(env 欄位 null 佔位即可)、正式儲存路徑(Phase 0)、
影像品質調參(對焦能用就好)、OTA(先 USB 燒)。

## 風險

- 雜牌 S3-CAM 的 PSRAM 可能是 2MB 或假料 → step 2 實測,5MP framebuffer 放不下就降解析度,記錄
- OV5640 AF 版的 AF 需要下載 firmware blob 到 sensor,esp32-camera 庫支援度看變體 → step 5 有 fallback
- 手機熱點的 client 隔離(AP isolation)可能擋筆電↔板互連 → 現場先驗 ping,不通改筆電開熱點
