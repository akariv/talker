# WROOM-32 client implementation plan
# Display polling + button-triggered camera

## Overview

Two independent subsystems on one WROOM-32, coordinated via deep sleep
wakeup causes:

- **Display:** wakes on a timer every POLL_INTERVAL seconds, fetches a
  bitmap from the server, updates the e-ink display, sleeps again.
- **Camera:** wakes on button press (GPIO 2), captures
  BURST_SIZE consecutive frames, POSTs each to the server as JPEG, sleeps.

Server logic is TBD — endpoints are treated as black boxes for now.

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

### OV7670 camera — unchanged from camera-display-plan.md

### Button

| Component   | ESP32 pin |
|-------------|-----------|
| Button      | GPIO 2    |

GPIO 2 is active LOW (pulled up internally). Press = LOW.

---

## Configurable parameters

```cpp
#define POLL_INTERVAL_S  30       // display refresh interval
#define BURST_SIZE        5       // frames captured per button press
#define JPEG_QUALITY     12       // 1–63, lower = smaller file
#define SERVER_URL       "https://yourserver.com"
```

---

## Firmware architecture

### Boot and wakeup routing

Every wake cycle starts here:

```cpp
void setup() {
    auto cause = esp_sleep_get_wakeup_cause();

    switch (cause) {
        case ESP_SLEEP_WAKEUP_TIMER:
            run_display_poll();
            break;
        case ESP_SLEEP_WAKEUP_EXT0:
            run_camera_burst();
            break;
        default:
            // First boot: initialise both subsystems, then fall through
            run_display_poll();
            break;
    }

    enter_deep_sleep();
}

void enter_deep_sleep() {
    esp_sleep_enable_timer_wakeup(POLL_INTERVAL_S * 1000000ULL);
    esp_sleep_enable_ext0_wakeup(GPIO_NUM_2, 0);  // wake on LOW
    rtc_gpio_pullup_en(GPIO_NUM_2);
    rtc_gpio_pulldown_dis(GPIO_NUM_2);
    esp_deep_sleep_start();
}
```

### Display subsystem: run_display_poll()
connect WiFi
→ GET /frame  (expect 9,472 bytes)
→ validate payload size
→ write black plane via SPI (DTM1)
→ write red plane via SPI (DTM2)
→ trigger display refresh
→ wait BUSY low
→ disconnect WiFi

No changes to the display logic from the original plan. Reuse fetch_frame()
and epd_display() verbatim.

### Camera subsystem: run_camera_burst()
init OV7670 (SCCB config + XCLK)
connect WiFi
repeat BURST_SIZE times:
capture one QQVGA grayscale frame (160×120, ~19 KB)
compress to JPEG (~3–6 KB at quality=12)
POST /frames  (Content-Type: image/jpeg)
free JPEG buffer
disconnect WiFi
deinit OV7670

Frame index is passed as a query parameter so the server can correlate
frames from the same burst:
POST /frames?burst_id=<millis>&index=0
POST /frames?burst_id=<millis>&index=1
...
POST /frames?burst_id=<millis>&index=4

`burst_id` is `millis()` at the start of the burst — not guaranteed unique
across power cycles, but sufficient for grouping within a session. A
monotonic counter stored in RTC memory would be more robust if needed.

### RTC memory for burst counter

```cpp
RTC_DATA_ATTR uint32_t burst_counter = 0;  // survives deep sleep
```

Increment at the start of each camera wake. Use as burst_id instead of
millis() for true uniqueness across deep sleep cycles.

---

## Memory budget

| Component                        | Size    |
|----------------------------------|---------|
| WiFi stack                       | ~100 KB |
| Display buffer                   | ~10 KB  |
| Camera frame (QQVGA grayscale)   | ~19 KB  |
| JPEG output buffer               | ~6 KB   |
| Stack + heap overhead            | ~50 KB  |
| **Free headroom**                | ~335 KB |

Display and camera buffers are never needed simultaneously (different wakeup
paths), so no conflicts.

---

## Build order

1. **Display only:** board test (display-plan.md), then polling loop with
   deep sleep. Verify timer wakeup and display refresh work reliably.

2. **Button wakeup:** confirm GPIO 0 ext0 wakeup triggers the correct path.
   Use a Serial.println to log wakeup cause before adding camera logic.

3. **Camera bring-up:** single frame capture + /snapshot HTTP endpoint.
   Verify image quality at JPEG quality=12.

4. **Burst capture:** loop BURST_SIZE times, log each POST response code.
   Use a stub server (e.g. httpbin.org/post) to confirm payloads arrive.

5. **Integration:** both subsystems running, switching correctly on wakeup
   cause.

---

## Open questions (server-side, TBD)

- What does the server render on /frame in response to received camera
  frames?
- Should the server acknowledge each frame POST, and should the ESP32 retry
  on failure?
- Is burst_id needed server-side, or is frame ordering irrelevant?
