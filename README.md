# Talker — ESP32 Voice AI Intercom

An ESP32-based voice intercom that records speech, sends it to an AI-powered server for processing (Whisper STT + Claude LLM), and plays back spoken responses via TTS.

## How It Works

1. ESP32 boots, connects to WiFi, waits 3 seconds
2. Records audio via I2S INMP441 mic (10 seconds)
3. Streams chunks to server via HTTP POST (`/upload`)
4. Server transcribes with Whisper, sends to Claude with a "say" tool
5. Claude's `say()` calls are queued as responses
6. ESP32 polls `/poll` — server converts text to speech (OpenAI TTS) and returns audio
7. ESP32 plays audio through DAC, then loops back to step 2

## Hardware

- **ESP32** (original, with DAC on GPIO 26)
- **INMP441** I2S MEMS microphone (SCK=33, WS=25, SD=32)
- WiFi network

## Project Structure

```
client/   — ESP-IDF C firmware for ESP32
server/   — FastAPI voice AI server (Python)
```

## Quick Start

### Server

```bash
just install   # install Python dependencies
just server    # run locally on port 8080
```

Requires `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` environment variables.

A test client (`name=test`, `api_key=testkey`) is auto-created in local dev mode.

### Client (ESP32)

Requires [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/) v5.x.

1. Edit `client/main/secrets.h` with your WiFi credentials, server URL, and client API key
2. Build and flash:

```bash
just build
just fm        # flash + monitor
```

### Deploy to Cloud Run

```bash
just deploy
```

Secrets should be configured in GCP Secret Manager:
- `anthropic-key` — Anthropic API key
- `openai-key` — OpenAI API key

### Configuration

Edit `client/main/secrets.h`:
- `WIFI_SSID` / `WIFI_PASS` — WiFi credentials
- `SERVER_URL` — server address (`http://...` for local, `https://...` for Cloud Run)
- `CLIENT_NAME` / `CLIENT_API_KEY` — client identity

Edit `client/main/main.c` defines:
- `RECORD_SECONDS` — recording duration (default: 10)
- `DAC_GAIN` — amplification for 8-bit DAC output (default: 15)
- `POLL_MAX_RETRIES` — how long to wait for AI response (default: 30s)
