"""Voice processing pipeline: PCM → Whisper STT → Claude LLM → response queue."""

import io
import os
import wave
import logging

import anthropic
import openai

import db

log = logging.getLogger(__name__)

SAY_TOOL = {
    "name": "say",
    "description": "Speak a message to the user through the intercom speaker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to speak aloud",
            }
        },
        "required": ["text"],
    },
}

DEFAULT_SYSTEM_PROMPT = (
    "You are a voice assistant on an ESP32 intercom device. "
    "Use the 'say' tool to speak your responses to the user. "
    "Keep responses concise and conversational — the user is listening, not reading. "
    "You may call 'say' multiple times to break up longer responses."
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
    wav_bytes = pcm_to_wav_bytes(pcm_data)
    client = openai.OpenAI()
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=("recording.wav", wav_bytes, "audio/wav"),
    )
    text = result.text.strip()
    log.info(f"Transcription: {text!r}")
    return text


def run_llm(client_name: str, user_text: str) -> list[str]:
    """Send user message to Claude with say tool, return list of say texts."""
    # Get client config for custom system prompt
    client_config = db.get_client(client_name)
    system_prompt = (client_config or {}).get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    # Build conversation history
    history = db.get_conversation(client_name)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_text})

    client = anthropic.Anthropic()
    say_texts = []

    # Loop to handle tool_use responses
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=[SAY_TOOL],
            messages=messages,
        )

        # Collect say tool calls and build tool_result responses
        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "say":
                text = block.input.get("text", "")
                if text:
                    say_texts.append(text)
                    log.info(f"say: {text!r}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Message queued for playback.",
                })

        if response.stop_reason == "tool_use" and tool_results:
            # Add assistant response + tool results, continue the loop
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return say_texts


def process_voice(client_name: str, pcm_data: bytes):
    """Full pipeline: transcribe → LLM → enqueue responses."""
    # Transcribe
    text = transcribe(pcm_data)
    if not text:
        log.warning(f"Empty transcription for {client_name}")
        db.enqueue_response(client_name, "I didn't catch that, could you try again?", 0)
        return

    # Save user message
    db.append_message(client_name, "user", text)

    # Run LLM
    say_texts = run_llm(client_name, text)

    if not say_texts:
        say_texts = ["I'm not sure what to say to that."]

    # Enqueue responses and save assistant message
    full_response = " ".join(say_texts)
    db.append_message(client_name, "assistant", full_response)

    for i, t in enumerate(say_texts):
        db.enqueue_response(client_name, t, sequence=i)

    log.info(f"Queued {len(say_texts)} response(s) for {client_name}")
