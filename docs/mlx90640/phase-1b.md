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
| `?rest=vga` | **待機解析度 —— 真正最大的一個**;待機約 1/17 的像素,見下方說明 | `/capture` 前後各多一次 framesize 切換(500ms settle) |
| `?cam=idle` | OV5640 軟體待機(reg 0x3008 bit6) | 喚醒要 settle |
| `?xclk=10` | 主時脈砍半;**同時冷卻兩顆晶片**(DVP/DMA 速率跟著降) | 幀率砍半,對本流程無感 |
| `?cpu=160` | SoC 最大單一槓桿 | 80MHz 可能餓死全解析度 JPEG |
| `?tx=11` | 串流時射頻幾乎全時發射 | 桌面距離無感;看 `rssi` |

`?rest=` 值得單獨說明,因為它同時解釋了一個看起來矛盾的觀測:**待機(70°C)比
串流(54°C)還燙**。原因是 `cameraInit` 用 `STILL_SIZE` 開 framebuffer 之後就沒再降
下來,而驅動的 cam task 從 init 起永不停止地讀出、`PIXFORMAT_JPEG` 在 OV5640 上又是
**sensor 自己壓縮** —— 所以「待機」其實是連續 5MP 讀出加 5MP 壓縮,為了沒有人會讀
的畫面;串流反而涼,因為 `cameraSetStreaming` 早就把它降到 VGA 了。

現在 init 配置 framebuffer 用 `STILL_SIZE`(配置尺寸必須是最大的,否則 QSXGA 靜態
拍不下),配置完立刻降到 `restSize`;`/capture` 為單張升上去、`cameraRelease` 再降
回來 —— 降回來的時機必須在 buffer 還給驅動**之後**,因為 `set_framesize` 會停掉並
重啟擷取引擎、可能重配 PSRAM framebuffer,在呼叫端還握著 fb 時做會讓那個指標懸空。

**任何一個旋鈕改動都會自動重設 peak**。峰值描述的是產生它的那組設定,沿用舊
峰值會讓每個新設定看起來都毫無改善 —— 這正是這個端點要避免的失敗。

**待機是手動,喚醒是自動。** 待機自動化會拿熱量換安靜的爛資料(AE/AWB 重收斂前
的幾幀曝光是壞的);但喚醒若也要手動,`/capture` 會在忘記時 timeout 成 500,
看起來像感測器壞了而不是像一個模式。所以 `/capture` 和 `/stream` 會自動喚醒並
誠實地付 1.2s settle。

**還沒驗**:以上都是機制,不是結論。`?cam=idle` 之後那顆到底降幾度,要熱像儀
對著板子才知道 —— 這仍然是唯一能量到 OV5640 本體的手段。

### 先看見,再動手:`/cam/reg`

外部資料指出兩件我們的模型沒算到的事:(a) OV5640 上電預設開啟**內建 DVDD
regulator**,會和板上外部 LDO 同時供電、差額變成熱 —— **這部分軟體待機關不掉**,
因為它在電源層不在功能層;(b) OV5640 額定 60°C 以下,超過 60–70°C 出現紫屏、
雜訊、凍結 —— 和我們看到的畫面劣化吻合,也代表量到的 70°C 對 S3 沒事、**對鏡頭
是超規**。

但那份資料**沒有給暫存器編號**,而我們手上那份 698 行廠商表裡 `regulator/LDO/
DVDD` 一個字都沒有。猜一個位址去寫的代價是鏡頭斷電,而 `CAM_PIN_PWDN` 和
`CAM_PIN_RESET` **都是 -1**(沒接線)。

所以先做儀器:`/cam/reg` dump 0x3000–0x3040,hex + binary + 已知註解。
暫存器寫入是揮發性的,拔電即復原 —— 這正是能安全實驗的理由。

查廠商表時另外撞到兩條線索,都是「讀出來就知道」而不必猜的:

    {0x300e, 0x58}, // MIPI power down, DVP enable   ← 我們走 DVP,MIPI 應該關掉
    {0x3006, 0xc3}, // disable clock of JPEG2x, JPEG ← 內部時脈可分區關閉
    {0x3008, 0x42}, // software power down, bit[6]   ← 證實了我們的待機寫法

0x3008 的 mask 因此從 `0xFF` 改成 `0x40`:廠商表寫整個位元組是因為它擁有整份
設定,我們沒有,所以只動 bit6,保留 esp32-camera 設好的其他位元。

**要用它回答的三個問題**:MIPI 關了沒(0x300e)、哪些時脈開著(0x3004/0x3006)、
待機前後哪些位元真的變了(0x3008 前後各 dump 一次)。

### 0x3006 逐 bit 實測結論(2026-09-03)

上面三個問題只答了「哪些時脈開著」的一半,但答得很乾淨。做法:一次只用 mask 動
**一個** bit(`?a=0x3006&m=0x10&v=0x00`),每輪都從 `0xFF` 重新起跳,`/capture` 當
判定 —— 判定看的是「回得出一張結尾有 `FFD9` 的完整 JPEG」,不是 HTTP 200,因為
少一塊時脈可能是畫面壞掉而不是硬掛。

| bit | mask | `/capture` | 判定 |
|---|---|---|---|
| 5 | 0x20 | 掛 | **我們的 JPEG 路徑要用** |
| 4 | 0x10 | OK | 可關 |
| 3 | 0x08 | 回 14 bytes 純文字 `capture failed` | **我們的 JPEG 路徑要用** |
| 2 | 0x04 | OK 188KB / 2560×1920 | 可關 |

**可關的最大集合 = `0x3006 = 0xEB`**(bit4 + bit2 關掉)。連拍三張 187819 /
184497 / 182646 bytes,全部 2560×1920、`FFD9` 完整,縮圖與 baseline 目視同一場景、
同樣的雜訊程度。

**廠商的 `0xc3` 不能照抄** —— 它關的 bit5..bit2 裡有兩個是我們要的,直接寫下去
`/capture` 就死。`reg_diff.py` docstring 講的「它關 JPEG clock 是因為它不用 JPEG,
我們用」到此有實測數字,這是那條原則最好的例子:差異是問題,不是待辦。

**這一格省下幾度沒有量到。** 掃描期間 `die_c 37.6 / die_max_c 41.6`,但那個 peak
橫跨了整段實驗(含 capture 掛掉的狀態),對 `0xEB` 不成立;而且 `/cam/reg` 的寫入
**不會**觸發 `resetPeak`(只有 `/power` 會)—— 要量之前得先 `?cpu=<現值>` 打一發
清 peak。關兩個時脈域的差值也很可能整個淹在雜訊裡。

所以 `0xEB` **不寫進 `cameraInit()`**:register 寫入揮發、重開機回 `0xFF`,在量到
溫差之前把它固化,等於用一個沒有證據的值換掉一個已知能動的預設。等散熱連同機構
一起決定時,這個值是備選手段之一。

**順帶記一個與過熱無關的觀察**:兩張圖都明顯偏紫。資料說 60–70°C 超規會紫屏,但
拍的當下 `die 37.6°C`、`ta 33.6°C`,離超規很遠 —— 所以此刻這個紫是**別的**原因
(AWB / IR-cut / 鏡頭)。先與過熱脫鉤記著,否則之後烤到高溫再看到紫,會誤以為
驗證了因果。

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
