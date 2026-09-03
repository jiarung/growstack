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

## 過熱追查(2026-09-03,進行中)

**能量到的溫度只有兩個,鏡頭不在其中。** esp32-camera 的 `sensor.h` 沒有任何
temperature op —— OV5640 的溫度從驅動層就取不到,不是我們沒接線。所以:

| 來源 | 是什麼 | 怎麼看 |
|---|---|---|
| `die_c` | ESP32-S3 晶片自己的內部感測器 | `GET /health` |
| `thermal_ta_c` | GY-MCU90640 模組自己的環境溫度 | `GET /health`、`/thermal` |
| 板子表面 | **拿熱像儀對著板子拍** | `thermal_view.py --watch` |

`/health` 是為了「沒人接 console 的那幾小時」而存在:`die_max_c` +
`die_max_at_s` 記錄開機以來的峰值與發生時刻,事後一個 GET 就問得到。
`thermal_ta_c` 走獨立的狀態鏡像而非 `take()` —— frame slot 是 consume-once,
讓狀態端點去讀它會把 frame 從 `/thermal` 手上偷走。

**推理起點:die 已經被證實沒事。** 上面 19 分鐘 /stream 實測收斂在 ~54°C 不失控。
若真有過熱,它就在 die sensor 看不到的地方 —— OV5640 本體、鏡頭座,或 5V→3.3V
的 AMS1117(這類板子最常見的熱點,壓降 1.7V 全變熱)。

**所以最值得做的一步不需要寫任何程式**:把熱像儀對著板子。

```sh
tools/s3cam/thermal_view.py http://<ip>/thermal --watch
```

熱點是哪個「零件」是一個座標問題不是溫度問題,所以工具現在會印
`hot @ r<row> c<col>`。開 /stream 讓它烤,看熱點落在 OV5640、S3 還是穩壓器 ——
這一步直接決定要加散熱片、降 duty cycle,還是換供電方式。

**另一條要一起排除的解釋**:旅館熱點 RSSI 差也會讓串流卡頓、畫面壞掉,症狀跟
過熱很像。`/health` 因此一起吐 `rssi`,別把兩件事混為一談。

### 降溫手段:問題不在晶片,在 duty cycle

「OV5640 本來就會發熱」只對一半。這顆在規格上不算異常耗電 —— **異常的是我們
怎麼跑它**:`fb_count=2` + `CAMERA_GRAB_LATEST`,驅動的 cam task 從 init 之後
就**永不停止**地全解析度讀出、填 framebuffer。而 `PIXFORMAT_JPEG` 配 OV5640 是
**sensor 自己做 JPEG 壓縮**,所以待機時這顆感測器一直在做「全解析度讀出 + 內部
壓縮」,為了沒有人會讀的畫面,24 小時不停。

一個「每幾分鐘拍一張」的 stop-settle-capture 流程,把感測器跑成連續錄影機。
這是設定問題,不是晶片特性 —— 也就是說軟體真的有得救。

`GET /power` 把四個旋鈕變成**執行期可切換**,而不是編譯期猜一個值燒進去。
理由:哪個旋鈕真的有效是經驗問題,燒死一組「省電設定」等於永遠不會知道答案。
一次燒錄,之後用 `/health` 的 `die_max_c` 和熱像儀做 A/B。

| 旋鈕 | 作用 | 代價 |
|---|---|---|
| `?cam=idle` | OV5640 軟體待機(reg 0x3008 bit6)—— **最大的一個** | 喚醒要 settle |
| `?xclk=10` | 主時脈砍半;**同時冷卻兩顆晶片**(DVP/DMA 速率跟著降) | 幀率砍半,對本流程無感 |
| `?cpu=160` | SoC 最大單一槓桿 | 80MHz 可能餓死全解析度 JPEG |
| `?tx=11` | 串流時射頻幾乎全時發射 | 桌面距離無感;看 `rssi` |

**任何一個旋鈕改動都會自動重設 peak**。峰值描述的是產生它的那組設定,沿用舊
峰值會讓每個新設定看起來都毫無改善 —— 這正是這個端點要避免的失敗。

**待機是手動,喚醒是自動。** 待機自動化會拿熱量換安靜的爛資料(AE/AWB 重收斂前
的幾幀曝光是壞的);但喚醒若也要手動,`/capture` 會在忘記時 timeout 成 500,
看起來像感測器壞了而不是像一個模式。所以 `/capture` 和 `/stream` 會自動喚醒並
誠實地付 1.2s settle。

**還沒驗**:以上都是機制,不是結論。`?cam=idle` 之後那顆到底降幾度,要熱像儀
對著板子才知道 —— 這仍然是唯一能量到 OV5640 本體的手段。

## AF firmware blob:已經在本機找到(2026-09-03,存參考,等鏡頭)

上面 step 9 說「AF 需要下載 firmware blob 到 sensor,esp32-camera 庫支援度看變體」。
那顆 blob 就在自己硬碟上,是查溫度暫存器時順手撞到的:

    ~/rt-thread/bsp/k210/driver/camera/ov5640af.h     4332 bytes,載入位址 0x8000
    ~/rt-thread/bsp/k210/driver/camera/drv_ov5640.c   OV5640_Focus_Init() 完整流程

流程本身很短:`0x3000=0x20`(reset MCU)→ 從 `0x8000` 起逐位元組寫完 4332 bytes
→ 清 `0x3022..0x3028`、`0x3029=0x7F` → `0x3000=0x00`(放開 MCU)→ 輪詢 `0x3029`
直到讀回 `0x70`。

**順帶結掉一個舊問題:`0x3029` 是硬體暫存器,不是 driver 抽象** —— 它是 AF
firmware 的狀態暫存器,`0x70` = 韌體就緒。之前問「0x3029 是指 driver 還是硬體層」
的答案在這裡。

還是等 AF 版鏡頭到手才做:**現有這顆是定焦標準版,沒有 VCM,blob 上傳成功也不會動**。
先記下來免得再找一次。

## 明確不做(本 phase)

thermal/servo/任何 broker 整合(env 欄位 null 佔位即可)、正式儲存路徑(Phase 0)、
影像品質調參(對焦能用就好)、OTA(先 USB 燒)。

## 風險

- 雜牌 S3-CAM 的 PSRAM 可能是 2MB 或假料 → step 2 實測,5MP framebuffer 放不下就降解析度,記錄
- OV5640 AF 版的 AF 需要下載 firmware blob 到 sensor,esp32-camera 庫支援度看變體 → step 5 有 fallback
- 手機熱點的 client 隔離(AP isolation)可能擋筆電↔板互連 → 現場先驗 ping,不通改筆電開熱點
