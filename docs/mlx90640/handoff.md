可以。你要把這段搬進 GitHub repo 給 coding agent 接手的話，我建議直接用下面這份作為 **project handoff / implementation context**。我把前面已經放棄的 X/Z 複雜機構先降成 future scope，避免 agent 照舊藍圖實作錯方向。

# Plant Thermal Imaging MVP — Implementation Summary

## 1. Current goal

目前要做的是一套植物 **RGB + thermal imaging** 系統，用來建立每株植物的：

* RGB image
* 32×24 thermal matrix
* leaf / plant ROI
* leaf temperature statistics
* environmental context
* timestamp / plant identity

第一版的核心目標是：

> **先證明固定觀測點 + Pan/Tilt 能否穩定取得可分析的植物 RGB / thermal paired data。**

目前**不做 X/Z linear motion**。

機械自由度只有：

```text
             Plant
               ↑

      ┌─────────────────┐
      │   Sensor Head   │
      │                 │
      │ RGB     Thermal │
      │ OV5640  MLX90640│
      └────────┬────────┘
               │
             TILT
           MG996R
               │
              PAN
           MG996R
               │
          fixed frame
```

之後實驗證明固定點 coverage 不足，再考慮加 X rail / Z stage。

---

# 2. Sensor head

Sensor head 同時包含兩個 camera。

### Thermal

選定：

**GY-MCU90640 + MLX90640BAB**

目標 FOV：

**55° × 35°**

解析度：

**32 × 24 = 768 thermal pixels**

目前 MVP 優先使用模組自己的 MCU，而不是 ESP32 直接 I²C 操作裸 MLX90640。

通訊：

```text
GY-MCU90640       ESP32-S3

VCC      → power
GND      → GND
TX       → UART RX
RX       → UART TX
```

模組 UART frame 為 **1544 bytes**，包含 768 個 temperature pixels、Ta 與 checksum；每個溫度值為兩 bytes，數值 /100 為 °C。

模組支援：

```text
0.5 Hz
1 Hz
2 Hz
4 Hz
8 Hz
```

最高 8 Hz 對應 460800 baud。

第一版預計：

```text
4–8 Hz
```

模組也支援 emissivity configuration，之後可針對植物葉片做 calibration。

另外保留 bypass MCU 的可能：未來如果需要完整控制 MLX90640，可以切換到直接 I²C。

---

### RGB

選定方向：

**OV5640 AF**

目標：

```text
5 MP
2592 × 1944 max
DVP interface
~68° FOV
autofocus
```

RGB FOV 比 thermal 稍大：

```text
RGB
┌────────────────────────┐
│                        │
│     Thermal FOV        │
│    ┌──────────────┐    │
│    │              │    │
│    └──────────────┘    │
│                        │
└────────────────────────┘
```

這是刻意設計的。

RGB 可以提供 thermal 外圍 context，之後做 registration 時將 thermal FOV 映射進 RGB image。

**重要機構要求：**

OV5640 與 MLX90640 必須剛性固定在同一 sensor head 上，兩者相對：

```text
translation
rotation
distance
```

不能在每次 Pan/Tilt 後改變。

之後才能 calibration：

```text
thermal pixel
      ↓
RGB coordinate
```

---

# 3. Pan / Tilt

目前機構只做兩個 rotational DOF。

Servo：

**MG996R ×2**

用途：

```text
Servo 1 → PAN
Servo 2 → TILT
```

支架：

**MG995 / MG996R compatible 2-DOF Pan/Tilt bracket**

Servo controller：

**PCA9685**

因此：

```text
ESP32-S3
   │
   │ I²C
   ↓
PCA9685
   │
   ├── MG996R PAN
   │
   └── MG996R TILT
```

不要直接靠 ESP32 GPIO software PWM 控 servo。

---

# 4. Power architecture

主電源目前規劃：

**Mean Well LRS-150-24**

```text
AC 110V
   │
   ↓
LRS-150-24
   │
   24V
   │
   ├───────────────┐
   ↓               ↓
24→6V buck      24→5V buck
5A              ≥3A
   │               │
   ↓               ↓
MG996R ×2       ESP32-S3
                RGB camera
                Thermal
```

Servo rail 與 MCU rail 分開降壓。

但：

```text
GND must be common
```

即：

```text
6V buck GND
5V buck GND
ESP32 GND
PCA9685 GND
GY-MCU90640 GND
```

需要 common reference。

---

# 5. Controller

目前預計：

**ESP32-S3**

但 camera 會大量占用 GPIO，因此正式組裝前需要確認使用的 S3 board 是否：

* 有足夠 GPIO
* 支援 OV5640 DVP
* 有 PSRAM
* camera + UART + I²C + servo controller 能同時使用

偏好的 board configuration：

```text
ESP32-S3
16 MB Flash
8 MB PSRAM
DVP camera support
```

Thermal 使用 UART。

PCA9685 使用 I²C。

OV5640 使用 DVP。

---

# 6. First MVP scan workflow

不要一開始做 continuous video。

採用 **stop → settle → capture**。

```text
target pan/tilt
       ↓
move servo
       ↓
wait for mechanical settling
       ↓
~300–500 ms
       ↓
capture RGB
       ↓
capture thermal frames
       ↓
3–5 frames
       ↓
average / median
       ↓
save observation
```

概念 API：

```text
scan_target(target)
    ↓
move_to(pan, tilt)
    ↓
wait_until_stable()
    ↓
rgb = capture_rgb()
    ↓
thermal_frames = capture_thermal(n=5)
    ↓
thermal = aggregate(thermal_frames)
    ↓
save_observation(...)
```

---

# 7. Suggested data model

第一版 observation 建議至少：

```json
{
  "timestamp": "...",

  "plant_id": "...",

  "pose": {
    "pan_deg": 0,
    "tilt_deg": -20
  },

  "rgb": {
    "file": "...",
    "width": 2592,
    "height": 1944
  },

  "thermal": {
    "width": 32,
    "height": 24,
    "ta_c": 26.4,
    "emissivity": 0.98,
    "matrix": []
  },

  "environment": {
    "temperature_c": null,
    "humidity_pct": null
  }
}
```

environment 可以接既有 monitor-air sensor pipeline，不需要 MVP 第一階段重新發明一套。

---

# 8. Software modules

Repo 不要把所有東西塞進一個 ESP32 main。

建議拆：

```text
thermal/
    gymcu90640.*
    thermal_frame.*

camera/
    ov5640.*

motion/
    pan_tilt.*
    pca9685.*

scan/
    scan_controller.*
    scan_target.*

storage/
    observation.*

calibration/
    camera_registration.*
```

其中最先做的是：

### `thermal/gymcu90640`

負責：

```text
UART receive
frame sync (0x5A 0x5A)
1544-byte frame parsing
768 temperature decode
Ta decode
checksum validation
timeout
invalid-frame recovery
```

輸出不要暴露 raw UART。

例如：

```cpp
struct ThermalFrame {
    float pixels[24][32];
    float ambient_temperature;
    uint64_t timestamp_ms;
};
```

---

# 9. RGB ↔ Thermal registration

這會是 MVP 很重要的第二階段。

兩個 camera 不在完全相同的 optical center，所以不能單純：

```text
thermal x * scale = rgb x
```

需要 calibration。

最終希望得到：

```text
Thermal coordinate
(x_t, y_t)
       ↓
registration
       ↓
RGB coordinate
(x_rgb, y_rgb)
```

之後：

```text
RGB
 ↓
plant / leaf segmentation
 ↓
RGB ROI
 ↓
project to thermal
 ↓
thermal ROI
 ↓
leaf temperature
```

第一版甚至不用 ML segmentation。

可以先：

**人工標 ROI。**

先回答一個更重要的問題：

> thermal resolution + distance + FOV 到底能不能有效區分我們的植物/葉片？

---

# 10. MVP metrics

這個專案第一階段不要用「camera 能不能拍照」作 success criteria。

我們真正需要測：

### Repeatability

同一盆植物：

```text
same pose
same distance
10 captures
```

比較：

```text
mean leaf temperature
median
P10/P90
thermal ROI position
```

### Registration stability

Pan/Tilt 多次移動回相同角度：

```text
A → B → C → A
```

RGB ↔ thermal registration 是否仍維持。

### Spatial resolution

不同距離：

```text
0.5 m
0.75 m
1.0 m
1.25 m
1.5 m
```

觀察一盆植物到底佔：

```text
thermal pixels
```

多少。

這會直接決定下一版需不需要 X axis。

---

# 11. Explicitly deferred

以下全部是 **future scope**：

```text
X linear rail
Z stage
MGN12
GT2 belt
GT2 pulley
belt tensioner
42BYG stepper
TMC2209
limit switches
drag chain
T8 lead screw
XY gantry
口字型 rail
3-row automatic scanning
```

這些料之前已經研究過，但現在不要讓 coding agent 把它們當 current architecture。

決策條件是：

```text
Fixed Pan/Tilt MVP
        ↓
實際 thermal dataset
        ↓
coverage / resolution 不足？
       / \
     no   yes
     ↓     ↓
   keep   判斷問題
          │
     ┌────┴─────┐
     ↓          ↓
 horizontal   height
 coverage     coverage
     ↓          ↓
   X-axis     Z-stage
```

---

# 12. Current hardware status / BOM direction

**MVP 需要：**

```text
ESP32-S3                 ×1
GY-MCU90640 BAB          ×1
OV5640 AF ~68°           ×1

MG996R                    ×2
Pan/Tilt bracket          ×1
PCA9685                   ×1

LRS-150-24                ×1
24V → 6V 5A buck         ×1
24V → 5V ≥3A buck        ×1

2040 extrusion
sensor mounting plate
wiring/connectors
```

之前購物車中的：

```text
42BYG48
TMC2209
MGN12
GT2 belt
GT2 pulley
GT2 tensioner
limit switch
drag chain
stepper bracket
```

**暫時全部不屬於 MVP。**

---

## 一句話 project definition

可以直接放進 repo 的 `README` / `AGENTS.md`：

> Build a fixed-position pan/tilt plant imaging platform that captures spatially registered RGB and 32×24 thermal observations. The first milestone is to quantify thermal spatial resolution, RGB–thermal registration stability, and measurement repeatability across viewing angles and distances before introducing any linear X/Z motion.

我會建議你現在在 repo **先實作 `GY-MCU90640 parser → ThermalFrame → test fixture`**，這是目前最獨立、最好測，而且之後機構怎麼改都不會浪費的第一塊。

