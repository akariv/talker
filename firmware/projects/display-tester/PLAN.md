# Display board test plan

## Goal
Verify the 2.9" BWR e-ink display is wired correctly and responding before
integrating the client/server fetch logic.

## Wiring (reference)

| Display pin | ESP32 pin |
|-------------|-----------|
| VCC         | 3V3       |
| GND         | GND       |
| SCL         | D18       |
| SDA         | D23       |
| CS          | D5        |
| DC          | TX2       |
| RES         | RX2       |
| BUSY        | D4        |

## Test stages

### 1. Blank white fill
Write all 0xFF to the black plane and all 0xFF to the red plane.
Expected: display clears to white after a full refresh cycle (~15–20 s).
Confirms: SPI communication, reset, and busy-wait are working.

### 2. Black fill
Write all 0x00 to the black plane, all 0xFF to the red plane.
Expected: display goes fully black.
Confirms: black plane data transmission is correct.

### 3. Red fill
Write all 0xFF to the black plane, all 0x00 to the red plane.
Expected: display goes fully red.
Confirms: red plane data transmission is correct.

### 4. Checkerboard
Alternate 0xAA / 0x55 bytes across the black plane, 0xFF to red.
Expected: black and white checkerboard pattern.
Confirms: bit ordering (MSB first) is correct.

### 5. Known bitmap
Write a hardcoded 9,472-byte payload (e.g. a border rectangle in black
and a small filled block in red, pre-computed offline).
Expected: matches the pre-computed image exactly.
Confirms: both planes render correctly in combination and the payload
format matches what the server will send.

## Pass criteria
All five stages render without corruption, with no garbage pixels or
missing regions. BUSY line goes low within 30 s on each refresh.

## Next step
Once passing, replace the hardcoded payload in stage 5 with a WiFi
fetch from the cloud server endpoint.
