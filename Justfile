# Talker — ESP32 Voice AI Intercom

set shell := ["zsh", "-c"]

# Source ESP-IDF tools before any idf.py command
idf := "source ~/.espressif/tools/activate_idf_v6.0.sh && python $IDF_PATH/tools/idf.py"

# Which firmware project to operate on. Override with `just PROJECT=<name> <recipe>`
# or `PROJECT=<name> just <recipe>`.
PROJECT := env_var_or_default("PROJECT", "talker")
proj_dir := "firmware/projects/" + PROJECT
build_dir := justfile_directory() + "/build/" + PROJECT

# ---- Firmware: per-project (defaults to talker) ----

# Set ESP32 target (run once per project)
setup:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} set-target esp32

# Build firmware
build:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} build

# Flash firmware to ESP32
flash:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} flash

# Open serial monitor
monitor:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} monitor

# Flash + monitor
fm:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} flash monitor

# Clean build artifacts for this project
clean:
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} fullclean

# Provision a client: fetch config from Firestore, write secrets.h, build and flash
provision CLIENT_ID:
    python scripts/provision.py --project {{PROJECT}} {{CLIENT_ID}}
    cd {{proj_dir}} && {{idf}} -B {{build_dir}} build flash monitor

# ---- Server ----

# Run the server locally
server:
    cd server && uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Deploy is handled by GitHub Actions on push to main
deploy:
    gcloud run deploy talker-server --source server/ --region us-central1

# Install server dependencies
install:
    pip install -r server/requirements.txt

# Generate test audio and run live integration tests
test-live:
    cd tests/live && python generate_audio.py && python test_scenarios.py

# ---- Infra ----

tf-init:
    cd infra && terraform init

tf-plan:
    cd infra && terraform plan

tf-apply:
    cd infra && terraform apply
