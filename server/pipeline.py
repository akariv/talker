"""Voice processing pipeline: PCM → Whisper STT → Claude LLM → response queue."""

import io
import os
import wave
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import openai

import db

log = logging.getLogger(__name__)

SAY_TOOL = {
    "name": "say",
    "description": (
        "Send a spoken message to a specific intercom client. "
        "You can send messages to the client that sent the original message, "
        "or to any other client in the system."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "client_id": {
                "type": "string",
                "description": "The ID of the client to send the message to",
            },
            "text": {
                "type": "string",
                "description": "The text to speak aloud on that client's speaker",
            },
        },
        "required": ["client_id", "text"],
    },
}

# Shared conversation key — all clients share one conversation so the LLM
# has full context of all interactions across the house.
GLOBAL_CONVERSATION = "global"


def build_system_prompt(sender_name: str) -> str:
    """Build a system prompt that includes sender info and all available clients."""
    clients = db.get_all_clients()
    sender = db.get_client(sender_name) or {}

    client_lines = []
    for c in clients:
        parts = [f"id={c['name']}"]
        if c.get("location"):
            parts.append(f"location={c['location']}")
        if c.get("owner"):
            parts.append(f"owner={c['owner']}")
        client_lines.append(", ".join(parts))

    clients_list = "\n".join(f"  - {line}" for line in client_lines)

    sender_desc = sender_name
    if sender.get("location"):
        sender_desc += f" (in {sender['location']})"
    if sender.get("owner"):
        sender_desc += f", belongs to {sender['owner']}"

    custom_prompt = sender.get("system_prompt", "")
    now = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%A, %B %d %Y, %H:%M")

    return (
        "You are a smart home voice assistant connected to an intercom system. "
        "Multiple intercom clients are installed in different rooms. "
        "Use the 'say' tool to send spoken messages to specific clients.\n\n"
        f"Current date and time: {now}\n\n"
        f"The current message was sent from: {sender_desc}\n\n"
        f"Available intercom clients:\n{clients_list}\n\n"
        "Guidelines:\n"
        "- Keep responses concise and conversational — users are listening, not reading.\n"
        "- You can send messages to multiple clients in one response.\n"
        "- When someone asks you to relay a message, send it to the appropriate client(s).\n"
        "- When answering a question, respond to the client that asked.\n"
        "- You may call 'say' multiple times to send to different clients.\n"
        + (f"\nAdditional instructions: {custom_prompt}" if custom_prompt else "")
    )


def pcm_to_wav_bytes(pcm_data: bytes, rate: int = 16000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def transcribe(pcm_data: bytes) -> str:
    """Transcribe raw PCM audio using OpenAI Whisper."""
    import struct
    num_samples = len(pcm_data) // 2
    if num_samples > 0:
        samples = struct.unpack(f"<{num_samples}h", pcm_data)
        peak = max(abs(s) for s in samples)
        rms = int((sum(s*s for s in samples) / num_samples) ** 0.5)
        log.info(f"Audio stats: {len(pcm_data)} bytes, {num_samples/16000:.1f}s, peak={peak}/32767, RMS={rms}")
    else:
        log.warning("Empty audio data")

    wav_bytes = pcm_to_wav_bytes(pcm_data)

    # Save debug WAV
    try:
        with open("/tmp/talker_last_recording.wav", "wb") as f:
            f.write(wav_bytes)
        log.info("Saved debug recording to /tmp/talker_last_recording.wav")
    except Exception:
        pass

    client = openai.OpenAI()
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=("recording.wav", wav_bytes, "audio/wav"),
    )
    text = result.text.strip()
    log.info(f"Transcription: {text!r}")
    return text


def run_llm(sender_name: str, user_text: str) -> list[tuple[str, str]]:
    """Send user message to Claude with say tool.

    Returns list of (client_id, text) tuples.
    """
    system_prompt = build_system_prompt(sender_name)

    # Build conversation history (shared across all clients)
    history = db.get_conversation(GLOBAL_CONVERSATION)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": f"[from {sender_name}] {user_text}"})

    client = anthropic.Anthropic()
    say_messages: list[tuple[str, str]] = []

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=[SAY_TOOL],
            messages=messages,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "say":
                target = block.input.get("client_id", sender_name)
                text = block.input.get("text", "")
                if text:
                    say_messages.append((target, text))
                    log.info(f"say -> {target}: {text!r}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Message queued for {target}.",
                })

        if response.stop_reason == "tool_use" and tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return say_messages


def process_voice(sender_name: str, pcm_data: bytes):
    """Full pipeline: transcribe → LLM → enqueue responses to target clients."""
    text = transcribe(pcm_data)
    if not text:
        log.warning(f"Empty transcription from {sender_name}")
        db.enqueue_response(sender_name, "I didn't catch that, could you try again?", 0)
        return

    # Save user message to shared conversation
    db.append_message(GLOBAL_CONVERSATION, "user", f"[from {sender_name}] {text}")

    # Run LLM
    say_messages = run_llm(sender_name, text)

    if not say_messages:
        say_messages = [(sender_name, "I'm not sure what to say to that.")]

    # Save assistant response to shared conversation
    summary = "; ".join(f"[to {cid}] {t}" for cid, t in say_messages)
    db.append_message(GLOBAL_CONVERSATION, "assistant", summary)

    # Enqueue responses to their target clients
    # Group by client_id to maintain per-client sequence ordering
    per_client: dict[str, list[str]] = {}
    for client_id, msg_text in say_messages:
        per_client.setdefault(client_id, []).append(msg_text)

    for client_id, texts in per_client.items():
        for i, t in enumerate(texts):
            db.enqueue_response(client_id, t, sequence=i)
        log.info(f"Queued {len(texts)} message(s) for {client_id}")
