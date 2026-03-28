# Talker — ESP32 Intercom Echo System

# Run the Python echo server
server:
    python server/server.py

# Build ESP-IDF client firmware
build:
    cd client && idf.py build

# Flash firmware to ESP32
flash:
    cd client && idf.py flash

# Open serial monitor
monitor:
    cd client && idf.py monitor

# Flash + monitor
fm:
    cd client && idf.py flash monitor

# Set ESP32 target (run once)
setup:
    cd client && idf.py set-target esp32

# Clean build artifacts
clean:
    cd client && idf.py fullclean
