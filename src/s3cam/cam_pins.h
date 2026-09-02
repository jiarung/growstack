#pragma once

// Camera pin map — Goouuu ESP32-S3-CAM (phase-1b.md D3).
//
// PRIMARY: the ESP32S3_EYE / Freenove ESP32-S3-WROOM-CAM map, which the Goouuu
// board (and most S3-CAM clones) copies. If esp_camera_init fails with 0x105 /
// 0x103 or the probe finds no sensor, try the alternates below IN ORDER and
// record the winner in docs/mlx90640/phase-1b.md.
#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK   15
#define CAM_PIN_SIOD    4   // SCCB SDA
#define CAM_PIN_SIOC    5   // SCCB SCL
#define CAM_PIN_D7     16
#define CAM_PIN_D6     17
#define CAM_PIN_D5     18
#define CAM_PIN_D4     12
#define CAM_PIN_D3     10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0     11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK   13

// Non-camera peripherals live here too, so pin allocation has ONE authority
// and a collision is a compile error rather than a field mystery. Free and
// safe on this N16R8 board: 1, 14, 21, 38-42, 47 (33-37 are octal PSRAM,
// 19/20 native USB, 43/44 UART0, 26-32 flash — all unusable).
#define RANGE_PIN_SDA  41
#define RANGE_PIN_SCL  42

// Switching to an ALTERNATE map below would put camera data lines on 41/42;
// the rangefinder must move first. Caught here, at compile time.
#if (RANGE_PIN_SDA == CAM_PIN_XCLK) || (RANGE_PIN_SDA == CAM_PIN_SIOD) || \
    (RANGE_PIN_SDA == CAM_PIN_SIOC) || (RANGE_PIN_SDA == CAM_PIN_D7)   || \
    (RANGE_PIN_SDA == CAM_PIN_D6)   || (RANGE_PIN_SDA == CAM_PIN_D5)   || \
    (RANGE_PIN_SDA == CAM_PIN_D4)   || (RANGE_PIN_SDA == CAM_PIN_D3)   || \
    (RANGE_PIN_SDA == CAM_PIN_D2)   || (RANGE_PIN_SDA == CAM_PIN_D1)   || \
    (RANGE_PIN_SDA == CAM_PIN_D0)   || (RANGE_PIN_SDA == CAM_PIN_VSYNC)|| \
    (RANGE_PIN_SDA == CAM_PIN_HREF) || (RANGE_PIN_SDA == CAM_PIN_PCLK)
#error "RANGE_PIN_SDA collides with a camera pin — pick another (see the free list)"
#endif
#if (RANGE_PIN_SCL == CAM_PIN_XCLK) || (RANGE_PIN_SCL == CAM_PIN_SIOD) || \
    (RANGE_PIN_SCL == CAM_PIN_SIOC) || (RANGE_PIN_SCL == CAM_PIN_D7)   || \
    (RANGE_PIN_SCL == CAM_PIN_D6)   || (RANGE_PIN_SCL == CAM_PIN_D5)   || \
    (RANGE_PIN_SCL == CAM_PIN_D4)   || (RANGE_PIN_SCL == CAM_PIN_D3)   || \
    (RANGE_PIN_SCL == CAM_PIN_D2)   || (RANGE_PIN_SCL == CAM_PIN_D1)   || \
    (RANGE_PIN_SCL == CAM_PIN_D0)   || (RANGE_PIN_SCL == CAM_PIN_VSYNC)|| \
    (RANGE_PIN_SCL == CAM_PIN_HREF) || (RANGE_PIN_SCL == CAM_PIN_PCLK)
#error "RANGE_PIN_SCL collides with a camera pin — pick another (see the free list)"
#endif

// ALTERNATE 1 — XIAO ESP32S3 Sense style, seen on some small S3 cam boards:
//   XCLK 10, SIOD 40, SIOC 39, D7 48, D6 11, D5 12, D4 14, D3 16, D2 18,
//   D1 17, D0 15, VSYNC 38, HREF 47, PCLK 13
// ALTERNATE 2 — ESP32-S3-CAM (aithinker-style relayout, rare):
//   XCLK 40, SIOD 17, SIOC 18, D7 39, D6 41, D5 42, D4 12, D3 3, D2 14,
//   D1 47, D0 13, VSYNC 21, HREF 38, PCLK 11
