"""FastAPI voice AI server for ESP32 intercom."""

import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from PIL import Image

import auth
import db
import display
import pipeline
import tts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Talker Voice Server")

# Per-client upload buffers for chunked recording
upload_buffers: dict[str, bytearray] = defaultdict(bytearray)


@app.post("/ping")
async def ping(request: Request):
    """Health check / TLS warmup endpoint."""
    await auth.authenticate(request)
    return Response(status_code=200)


@app.post("/upload")
async def upload(request: Request):
    """Receive an audio chunk and append to the client's buffer."""
    client_name = await auth.authenticate(request)
    chunk = await request.body()
    upload_buffers[client_name].extend(chunk)
    total = len(upload_buffers[client_name])
    secs = total / (16000 * 2)
    log.info(f"[{client_name}] Chunk: {len(chunk)} bytes | Total: {total} ({secs:.1f}s)")
    return Response(status_code=200)


@app.post("/done")
async def done(request: Request, background_tasks: BackgroundTasks):
    """Signal recording is complete. Triggers the voice processing pipeline."""
    client_name = await auth.authenticate(request)
    pcm_data = bytes(upload_buffers.pop(client_name, bytearray()))
    secs = len(pcm_data) / (16000 * 2)
    log.info(f"[{client_name}] Recording complete: {len(pcm_data)} bytes ({secs:.1f}s)")

    if len(pcm_data) < 3200:
        log.warning(f"[{client_name}] Recording too short, ignoring")
        return Response(status_code=200)

    background_tasks.add_task(pipeline.process_voice, client_name, pcm_data)
    return Response(status_code=202)


@app.post("/voice")
async def voice(request: Request, background_tasks: BackgroundTasks):
    """Single-POST alternative: send full recording in one request."""
    client_name = await auth.authenticate(request)
    pcm_data = await request.body()
    secs = len(pcm_data) / (16000 * 2)
    log.info(f"[{client_name}] Voice message: {len(pcm_data)} bytes ({secs:.1f}s)")

    if len(pcm_data) < 3200:
        log.warning(f"[{client_name}] Recording too short, ignoring")
        return Response(status_code=200)

    background_tasks.add_task(pipeline.process_voice, client_name, pcm_data)
    return Response(status_code=202)


@app.get("/poll")
async def poll(request: Request):
    """Poll for pending voice responses. Returns audio or 204."""
    client_name = await auth.authenticate(request)

    entry = db.poll_next_response(client_name)
    if entry is None:
        return Response(status_code=204)

    text = entry["text"]
    log.info(f"[{client_name}] Generating TTS for: {text!r}")

    try:
        pcm_data = tts.text_to_pcm(text)
        db.mark_delivered(entry)
        log.info(f"[{client_name}] Delivering {len(pcm_data)} bytes of audio")
        return Response(
            content=pcm_data,
            media_type="application/octet-stream",
            headers={"Content-Length": str(len(pcm_data))},
        )
    except Exception as e:
        log.error(f"[{client_name}] TTS failed: {e}")
        db.mark_failed(entry)
        return Response(status_code=503)


# Per-(client,w,h) minute key of the last frame served, for 204 short-circuit.
_last_frame_minute: dict[tuple[str, int, int], str] = {}

# Latest camera capture per client: (grayscale_bytes, width, height).
_last_photo: dict[str, tuple[bytes, int, int]] = {}

# Where to drop PNG previews of uploaded photos. Overridable for dev.
_PHOTO_DIR = Path(__file__).parent.parent#os.environ.get("DISPLAY_CAM_PHOTO_DIR",
                                #   "/tmp/display-cam-photos"))


@app.get("/display/frame")
async def display_frame(request: Request, w: int, h: int):
    """Serve a tri-color e-ink frame for the given logical (landscape) size.

    Returns 204 when the cached minute-key is unchanged, 400 on an
    unsupported (w, h), otherwise 200 with `black_plane ‖ red_plane`
    already in panel-native scan order (9 472 bytes for 128×296 panels).
    """
    client_name = await auth.authenticate(request)

    if (w, h) != (display.LANDSCAPE_W, display.LANDSCAPE_H):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported size {w}x{h}; "
                   f"expected {display.LANDSCAPE_W}x{display.LANDSCAPE_H}",
        )

    now = datetime.now(ZoneInfo("Europe/Berlin"))
    minute_key = now.strftime("%Y%m%dT%H%M")
    cache_key = (client_name, w, h)
    if _last_frame_minute.get(cache_key) == minute_key:
        return Response(status_code=204)

    data = display.render_current_time_frame(
        w, h, now, photo=_last_photo.get(client_name)
    )
    _last_frame_minute[cache_key] = minute_key
    log.info(f"[{client_name}] display/frame: {len(data)} bytes @ {minute_key}")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Length": str(len(data))},
    )


@app.post("/display/photo")
async def display_photo(request: Request):
    """Stash the uploaded grayscale frame and drop a PNG preview to disk.

    Body is raw grayscale bytes; dimensions come from `X-Image-Width` /
    `X-Image-Height` headers. The latest photo becomes the left panel of
    subsequent `/display/frame` renders.
    """
    client_name = await auth.authenticate(request)
    body = await request.body()

    try:
        w = int(request.headers.get("X-Image-Width", "0"))
        h = int(request.headers.get("X-Image-Height", "0"))
    except ValueError:
        w = h = 0

    if w <= 0 or h <= 0 or len(body) != w * h:
        log.warning(f"[{client_name}] display/photo: "
                    f"ignored (len={len(body)} hdr={w}x{h})")
        return Response(status_code=400)

    _last_photo[client_name] = (bytes(body), w, h)

    # Save a PNG preview for local inspection.
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = _PHOTO_DIR# / client_name
        out_dir.mkdir(parents=True, exist_ok=True)
        png_path = out_dir / "out.png"#f"{ts}.png"
        Image.frombytes("L", (w, h), bytes(body)).save(png_path)
        log.info(f"[{client_name}] display/photo: {len(body)} bytes "
                 f"({w}x{h}) -> {png_path}")
    except Exception as e:
        log.warning(f"[{client_name}] display/photo: PNG save failed: {e}")

    return Response(status_code=204)


@app.on_event("startup")
async def startup():
    """Seed test clients for local development."""
    if not db.USE_FIRESTORE and not db.get_client("kitchen"):
        db.add_client("kitchen", "testkey", location="Kitchen")
        db.add_client("alice-room", "testkey", location="Alice's room", owner="Alice")
        db.add_client("bob-room", "testkey", location="Bob's room", owner="Bob")
        log.info("Seeded test clients: kitchen, alice-room, bob-room")
