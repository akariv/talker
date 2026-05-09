// Shared logic for the display-cam sim.
//
// Endpoint: GET /display/frame?w=296&h=128
// Auth headers: X-Client-Name, X-Api-Key
// Body on 200: 9472 bytes = black_plane[4736] ‖ red_plane[4736].
// Each plane is 128×296 (W×H), MSB-first 1bpp, row-major.
// Bit conventions (server display.py):
//   black plane: 1 = white, 0 = black
//   red   plane: 1 = red,   0 = transparent
// Server rotates 90° CW into native, so to recover landscape:
//   native (xN, yN)  ↔  landscape (yN, 127 − xN)

const NATIVE_W = 128;
const NATIVE_H = 296;
const ROW_BYTES = NATIVE_W / 8;          // 16
const PLANE_BYTES = ROW_BYTES * NATIVE_H; // 4736
const FRAME_BYTES = PLANE_BYTES * 2;      // 9472

const STORAGE_KEY = "displayCamSimSettings";

function saveSettings(s) {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}

function getSettings() {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    try { return JSON.parse(raw); }
    catch { return null; }
}

function clearSettings() {
    sessionStorage.removeItem(STORAGE_KEY);
}

// Decode one frame's bytes into RGBA pixels arranged in landscape order
// (296×128). Writes into the provided ImageData.data buffer.
function decodeFrameInto(bytes, imageData) {
    const black = bytes.subarray(0, PLANE_BYTES);
    const red = bytes.subarray(PLANE_BYTES, FRAME_BYTES);
    const out = imageData.data;
    const W = NATIVE_H;  // 296 — landscape width
    const H = NATIVE_W;  // 128 — landscape height

    for (let yL = 0; yL < H; yL++) {
        const xN = (NATIVE_W - 1) - yL;            // 127 − yL
        const byteCol = xN >> 3;
        const mask = 0x80 >> (xN & 7);
        for (let xL = 0; xL < W; xL++) {
            const yN = xL;
            const byteIdx = yN * ROW_BYTES + byteCol;
            const isWhite = (black[byteIdx] & mask) !== 0;
            const isRed = (red[byteIdx] & mask) !== 0;

            const px = (yL * W + xL) * 4;
            if (isRed) {
                out[px]     = 0xcc; // a panel-ish red, not pure 0xff
                out[px + 1] = 0x22;
                out[px + 2] = 0x22;
            } else if (isWhite) {
                out[px]     = 0xff;
                out[px + 1] = 0xff;
                out[px + 2] = 0xff;
            } else {
                out[px]     = 0x00;
                out[px + 1] = 0x00;
                out[px + 2] = 0x00;
            }
            out[px + 3] = 0xff;
        }
    }
}

// Fetch one frame and render it. Returns nothing; updates statusEl.
async function fetchAndRender(canvas, statusEl) {
    const s = getSettings();
    if (!s) {
        setStatus(statusEl, "no settings — go to login", "error");
        return;
    }

    const url = `${s.serverUrl}/display/frame?w=${s.w}&h=${s.h}`;
    setStatus(statusEl, `GET ${url} …`, "");

    const t0 = performance.now();
    let resp;
    try {
        resp = await fetch(url, {
            headers: {
                "X-Client-Name": s.clientName,
                "X-Api-Key": s.apiKey,
            },
        });
    } catch (e) {
        // fetch() throws on network errors and on CORS rejection (which
        // surfaces as a TypeError with no useful detail in the browser).
        setStatus(statusEl,
            `network error: ${e.message} — server down or CORS blocked`,
            "error");
        return;
    }

    const dt = (performance.now() - t0).toFixed(0);

    if (resp.status === 204) {
        setStatus(statusEl, `204 — no change (${dt} ms)`, "ok");
        return;
    }

    if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        setStatus(statusEl,
            `HTTP ${resp.status} (${dt} ms) ${body.slice(0, 120)}`,
            "error");
        return;
    }

    const buf = await resp.arrayBuffer();
    if (buf.byteLength !== FRAME_BYTES) {
        setStatus(statusEl,
            `unexpected length ${buf.byteLength} (want ${FRAME_BYTES})`,
            "error");
        return;
    }

    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(NATIVE_H, NATIVE_W);  // 296×128
    decodeFrameInto(new Uint8Array(buf), img);
    ctx.putImageData(img, 0, 0);

    setStatus(statusEl, `200 (${buf.byteLength} B in ${dt} ms)`, "ok");
}

function setStatus(el, msg, cls) {
    if (!el) return;
    el.textContent = msg;
    el.className = "status" + (cls ? " " + cls : "");
}
