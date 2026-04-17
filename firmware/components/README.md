# Shared ESP-IDF components

Reusable firmware components consumed by projects under `../projects/`.

Each project's top-level `CMakeLists.txt` points here via:

```cmake
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
```

A component lives in its own subdirectory with a `CMakeLists.txt` that calls
`idf_component_register(...)` and an `include/` header directory.

Planned extractions (see `plans/i-want-to-make-delightful-manatee.md`, Phase B):

- `wifi_sta/` — WiFi STA init + event group
- `mic_i2s/` — I2S mic capture with double-buffered DMA
- `speaker_i2s/` — I2S playback (MAX98357A)
- `talker_http/` — HTTP client with `X-Client-Name` / `X-Api-Key` headers + embedded root CA
- `board_hal/` — LED and button helpers, pin map via Kconfig

Components take WiFi/server/API-key values as function arguments. Do not
`#include "secrets.h"` from a component — that header lives in each project's
`main/` and is project-local.
