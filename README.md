# Talker — ESP32 Intercom Echo System

An ESP32-based intercom that records audio via an INMP441 I2S microphone, streams it to a server over WiFi, then plays it back through the onboard DAC.

## Hardware

- **ESP32** (original, with DAC on GPIO 26)
- **INMP441** I2S MEMS microphone (SCK=33, WS=25, SD=32)
- WiFi network

## Project Structure

```
client/   — ESP-IDF C firmware for ESP32
server/   — Python echo server (runs on host machine)
```

## Quick Start

### Server

```bash
just server
```

Or manually: `python server/server.py` (listens on port 8080).

### Client (ESP32)

Requires [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/) v5.x.

```bash
just build     # compile
just flash     # flash to ESP32
just monitor   # serial output
just fm        # flash + monitor
```

Or manually:
```bash
cd client
idf.py set-target esp32
idf.py build
idf.py -p /dev/tty.usbserial-XXXX flash monitor
```

### Configuration

Edit `client/main/main.c` defines:
- `WIFI_SSID` / `WIFI_PASS` — WiFi credentials
- `SERVER_IP` / `SERVER_PORT` — server address
- `RECORD_SECONDS` — recording duration

## How It Works

1. ESP32 boots, connects to WiFi, waits 3 seconds
2. Records audio via I2S mic, streams chunks to server via HTTP POST
3. Requests playback via HTTP GET
4. Server sends raw 16-bit 16kHz PCM back
5. ESP32 converts to 8-bit, plays through DAC with DMA
