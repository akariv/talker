# WROOM-32 + OV7670 + e-ink display: implementation plan

## Overview

The WROOM-32 lacks PSRAM and ML acceleration, so face detection runs on the
cloud server rather than on-device. The ESP32 captures a frame, compresses it
to JPEG, POSTs it to the server, and the server returns a detection result.
If a face is found the server updates the /frame endpoint and signals the
ESP32 to refresh the display.

---

## Hardware

### Board
ESP32 WROOM-32 DevKit V1 — unchanged.

### New components
- OV7670 camera module (no FIFO variant)
- 10 kΩ pull-up resistors × 2 (for SIOC and SIOD)

---

## Wiring

### Display — unchanged

| Display pin | Signal       | ESP32 pin |
|-------------|--------------|-----------|
| VCC         | Power        | 3V3       |
| GND         | Ground       | GND       |
| SCL         | SPI clock    | D18       |
| SDA         | SPI MOSI     | D23       |
| CS          | Chip select  | D5        |
| DC          | Data/command | TX2       |
| RES         | Reset        | RX2       |
| BUSY        | Busy signal  | D4        |

### OV7670 camera

| OV7670 pin | Signal           | ESP32 GPIO | Notes                         |
|------------|------------------|------------|-------------------------------|
| VCC        | Power            | 3V3        |                               |
| GND        | Ground           | GND        |                               |
| SIOC       | I2C clock (SCCB) | GPIO 22    | 10 kΩ pull-up to 3V3         |
| SIOD       | I2C data (SCCB)  | GPIO 21    | 10 kΩ pull-up to 3V3         |
| XCLK       | Clock input      | GPIO 27    | Generated via LEDC peripheral |
| PCLK       | Pixel clock      | GPIO 25    | Input                         |
| VSYNC      | Frame sync       | GPIO 26    | Input                         |
| HREF       | Line sync        | GPIO 19    | Input                         |
| D0         | Pixel data bit 0 | GPIO 34    | Input-only pin — OK here      |
| D1         | Pixel data bit 1 | GPIO 35    | Input-only pin — OK here      |
| D2         | Pixel data bit 2 | GPIO 32    | Input                         |
| D3         | Pixel data bit 3 | GPIO 33    | Input                         |
| D4         | Pixel data bit 4 | GPIO 13    | Input                         |
| D5         | Pixel data bit 5 | GPIO 14    | Input                         |
| D6         | Pixel data bit 6 | GPIO 39    | Input-only pin — OK here      |
| D7         | Pixel data bit 7 | GPIO 36    | Input-only pin — OK here      |
| RESET      | Camera reset     | GPIO 15    | Active LOW                    |
| PWDN       | Power down       | GND        | Tie LOW to keep camera on     |

Note: GPIO 34, 35, 36, 39 are input-only — fine for data/clock inputs.
Avoid using GPIO 12 during boot (strapping pin); it is not used here.

---

## Memory budget

520 KB SRAM total. Approximate allocation:

| Component          | Size      |
|--------------------|-----------|
| WiFi stack         | ~100 KB   |
| Display buffer     | ~10 KB    |
| Camera frame (QQVGA grayscale 160×120) | ~19 KB |
| JPEG output buffer | ~8 KB     |
| Stack + heap overhead | ~50 KB |
| **Free headroom**  | **~333 KB** |

Grayscale (1 byte/pixel) rather than RGB565 (2 bytes/pixel) is used to
halve the frame buffer. Color is unnecessary for face detection.

---

## Firmware architecture

### Phase 1 — Display board test
Run the existing board test plan (display-plan.md) to confirm display wiring
before adding the camera. No changes needed there.

### Phase 2 — Camera bring-up

1. Generate XCLK at 8 MHz via the LEDC peripheral:
```cpp
   ledcSetup(0, 8000000, 1);
   ledcAttachPin(27, 0);
```

2. Configure OV7670 over SCCB (I2C at 100 kHz):
   - Output format: QQVGA (160×120), YUV422 (Y channel = grayscale)
   - Set CLKRC to divide PCLK to ~500 kHz
   - Disable AGC/AWB initially for easier debugging

3. Capture a single frame by bit-banging the parallel bus:
   - Wait for VSYNC rising edge (start of frame)
   - For each line: wait for HREF high, sample D0–D7 on each PCLK edge
   - Store Y byte only (skip U/V bytes in YUV422) into a 19,200-byte buffer

4. Verify: serve the raw buffer as a PGM image over a `/snapshot` HTTP
   endpoint and view in a browser to confirm the image is correct.

### Phase 3 — JPEG compression

Use the esp32-camera JPEG encoder (ships with Arduino ESP32 core) to
compress the grayscale frame before sending:

```cpp
#include "img_converters.h"

uint8_t* jpg_buf = nullptr;
size_t   jpg_len = 0;
frame2jpg_grayscale(raw_buf, 160, 120, 12, &jpg_buf, &jpg_len);
// quality=12 → ~3–6 KB output, adequate for face detection
```

Free the JPEG buffer after the POST completes.

### Phase 4 — POST frame to server

On each capture cycle:

1. Compress frame to JPEG
2. POST to `/detect` with Content-Type: image/jpeg
3. Parse JSON response to get detection result
4. If action is "refresh": GET /frame and update display
5. Apply a 30-second cooldown between POSTs to avoid hammering the server

```cpp
#define DETECT_COOLDOWN_MS 30000

unsigned long last_post = 0;

void capture_and_detect() {
    if (millis() - last_post < DETECT_COOLDOWN_MS) return;
    capture_frame();
    compress_to_jpeg();
    if (post_frame_to_server() == REFRESH) {
        fetch_and_display_frame();
    }
    last_post = millis();
}
```

---

## Server updates

### New endpoint: POST /detect

Accepts a JPEG image body, runs face detection, updates internal state,
and returns a refresh signal.

```python
import cv2
import numpy as np
from flask import Flask, Response, request, jsonify

face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

detection_state = {"last_seen": None, "count": 0}

@app.route("/detect", methods=["POST"])
def detect():
    jpg = np.frombuffer(request.data, dtype=np.uint8)
    img = cv2.imdecode(jpg, cv2.IMREAD_GRAYSCALE)
    faces = face_cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5)

    if len(faces) > 0:
        detection_state["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
        detection_state["count"] += 1
        invalidate_frame_cache()      # signals /frame to re-render
        return jsonify({"action": "refresh", "faces": len(faces)})

    return jsonify({"action": "no-refresh", "faces": 0})
```

Haar cascades work well at QQVGA resolution and add negligible server load.
Swap for a DNN-based detector later if accuracy needs improvement.

### Updated /frame render

Re-render when `detection_state` changes:

```python
def render_frame():
    # ... existing render logic ...
    draw_bw.text((8, 8),  "Last seen:", fill=255)
    draw_bw.text((8, 28), detection_state["last_seen"] or "nobody", fill=255)
    draw_red.text((8, 52), f"Count: {detection_state['count']}", fill=255)
```

---

## System flow
[OV7670] → bit-bang capture → grayscale QQVGA frame
│
JPEG compress (~4 KB)
│
POST /detect (HTTPS)
│
server runs Haar cascade
/         
face              no face
│                  │
update /frame state    return no-refresh
return "refresh"            │
│              capture next frame
GET /frame                │
│              wait cooldown
update e-ink display

---

## Expected performance

| Metric                  | Estimate         |
|-------------------------|------------------|
| Frame capture (bit-bang)| ~200 ms          |
| JPEG compression        | ~50 ms           |
| POST + server detection | ~300–500 ms      |
| Display refresh         | ~15–20 s         |
| Effective detection rate| ~1–2 fps         |

---

## Open questions

1. **Lighting:** Haar cascades are sensitive to lighting conditions. If
   deployed indoors, consider adding an LED or constraining the environment.

2. **Power:** continuous capture + WiFi draws ~200 mA. If battery-powered,
   sleep between capture cycles (e.g. wake every 2 seconds rather than
   running continuously).

3. **Privacy:** raw JPEG frames leave the device and reach the server. If
   this is a concern, consider running a very lightweight on-device
   pre-filter (motion detection via frame differencing) before deciding
   whether to POST the image.

---

## Suggested build order

1. Display board test with existing plan
2. Camera bring-up: single frame + /snapshot endpoint
3. JPEG compression: verify output quality at quality=12
4. Server: add /detect endpoint, test with curl and a sample JPEG
5. Integration: connect all phases end-to-end
