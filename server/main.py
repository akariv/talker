"""FastAPI voice AI server for ESP32 intercom."""

import logging

from fastapi import FastAPI, Request, Response, BackgroundTasks

import auth
import db
import pipeline
import tts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Talker Voice Server")


@app.post("/voice")
async def voice(request: Request, background_tasks: BackgroundTasks):
    """Receive a voice recording (raw 16-bit 16kHz mono PCM) and process it."""
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


@app.on_event("startup")
async def startup():
    """Seed test clients for local development."""
    if not db.USE_FIRESTORE and not db.get_client("kitchen"):
        db.add_client("kitchen", "testkey", location="Kitchen")
        db.add_client("alice-room", "testkey", location="Alice's room", owner="Alice")
        db.add_client("bob-room", "testkey", location="Bob's room", owner="Bob")
        log.info("Seeded test clients: kitchen, alice-room, bob-room")
