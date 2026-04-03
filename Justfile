# Talker — ESP32 Voice AI Intercom

set shell := ["zsh", "-c"]

# Source ESP-IDF tools before any idf.py command
idf := "source ~/.espressif/tools/activate_idf_v6.0.sh && python $IDF_PATH/tools/idf.py"

# Run the server locally
server:
    cd server && uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Deploy is handled by GitHub Actions on push to main
# For manual deploy, use workflow_dispatch or:
deploy:
    gcloud run deploy talker-server --source server/ --region us-central1

# Terraform
tf-init:
    cd infra && terraform init

tf-plan:
    cd infra && terraform plan

tf-apply:
    cd infra && terraform apply

# Provision a client: fetch config from Firestore, write secrets.h, build and flash
provision CLIENT_ID:
    python scripts/provision.py {{CLIENT_ID}}
    cd client && {{idf}} build flash monitor

# Build ESP-IDF client firmware
build:
    cd client && {{idf}} build

# Flash firmware to ESP32
flash:
    cd client && {{idf}} flash

# Open serial monitor
monitor:
    cd client && {{idf}} monitor

# Flash + monitor
fm:
    cd client && {{idf}} flash monitor

# Set ESP32 target (run once)
setup:
    cd client && {{idf}} set-target esp32

# Clean build artifacts
clean:
    cd client && {{idf}} fullclean

# Install server dependencies
install:
    pip install -r server/requirements.txt

# Generate test audio and run live integration tests
test-live:
    cd tests/live && python generate_audio.py && python test_scenarios.py
