# Firmware

ESP-IDF firmware for all client projects that talk to the shared server in
`../server/`. Each project is a full, independently-buildable ESP-IDF project.

## Layout

```
firmware/
  projects/
    talker/        — full voice intercom (WiFi → mic → server → TTS playback)
    board-test/    — hardware diagnostic loop (LEDs, speaker, mic, WiFi, button)
  components/      — shared ESP-IDF components (see components/README.md)
```

Build artifacts for each project land in `../build/<project>/` at repo root;
the tree under `firmware/` stays clean.

## Building

From the repo root:

```bash
just setup            # one-time, for the default project (talker)
just build            # builds talker
just fm               # flash + monitor talker

just PROJECT=board-test setup
just PROJECT=board-test build
just PROJECT=board-test fm
```

Every firmware recipe in the `Justfile` accepts `PROJECT=<name>` to select
which project under `projects/` to operate on. Default is `talker`.

## Providing credentials (`secrets.h`)

Each project's `main/main.c` includes `secrets.h` (gitignored). Two ways to
create it:

**Option A — provision from Firestore (recommended for talker):**
```bash
just provision kitchen                         # provisions talker (default)
just PROJECT=board-test provision test-device  # provisions board-test
```
`scripts/provision.py` fetches WiFi + server + API-key config from Firestore
and writes `firmware/projects/<project>/main/secrets.h`.

**Option B — copy the example template (simplest for board-test or one-off builds):**
```bash
cp firmware/projects/board-test/main/secrets.h.example \
   firmware/projects/board-test/main/secrets.h
# edit to fill in real WiFi credentials
```
Every project ships a `secrets.h.example` checked into git that documents the
exact macros that project's firmware expects.

## Adding a new project

1. Copy an existing project as a starting point:
   ```bash
   cp -r firmware/projects/talker firmware/projects/doorbell
   ```
2. Edit `firmware/projects/doorbell/CMakeLists.txt` — change `project(talker)`
   to `project(doorbell)`.
3. Replace `main/main.c` with your firmware code. Shared components under
   `../components/` are available automatically via `EXTRA_COMPONENT_DIRS`.
4. Adjust `sdkconfig.defaults` and `partitions.csv` if the hardware or flash
   layout differs.
5. Build: `just PROJECT=doorbell setup build fm`.

## Shared components

See `components/README.md`. Components take config values (WiFi creds, server
URL, API key) as function arguments rather than `#include "secrets.h"` — this
keeps them project-agnostic.
