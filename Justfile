# Talker — ESP32 Voice AI Intercom

# Run the server locally
server:
    cd server && uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Deploy to Cloud Run
deploy:
    gcloud run deploy talker-server --source server/ --region us-central1 --min-instances 1

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

# Install server dependencies
install:
    pip install -r server/requirements.txt
