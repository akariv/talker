"""Generate test WAV files from text using OpenAI TTS, in 16-bit 16kHz mono PCM.

Usage: python generate_audio.py
Outputs .raw files (raw PCM) that can be POSTed to the server.
"""

import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

import numpy as np
from scipy.signal import resample_poly
from openai import OpenAI

MESSAGES = {
    "dinner_ready.raw": "Dinner is ready!",
    "capital_of_france.raw": "What is the capital of France?",
    "ask_alice_movies.raw": "Ask Alice if she wants to go to the movies.",
}


def text_to_pcm_16k(text: str) -> bytes:
    """Convert text to 16-bit 16kHz mono PCM via OpenAI TTS."""
    client = OpenAI()
    response = client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text,
        response_format="pcm",  # 24kHz 16-bit mono
    )
    pcm_24k = response.content
    samples_24k = np.frombuffer(pcm_24k, dtype=np.int16)
    samples_16k = resample_poly(samples_24k, up=2, down=3).astype(np.int16)
    return samples_16k.tobytes()


def main():
    os.makedirs("audio", exist_ok=True)
    for filename, text in MESSAGES.items():
        path = os.path.join("audio", filename)
        if os.path.exists(path):
            print(f"  Skipping {filename} (already exists)")
            continue
        print(f"  Generating {filename}: {text!r}")
        pcm = text_to_pcm_16k(text)
        with open(path, "wb") as f:
            f.write(pcm)
        duration = len(pcm) / (16000 * 2)
        print(f"    -> {len(pcm)} bytes ({duration:.1f}s)")

    print("Done. Audio files in tests/live/audio/")


if __name__ == "__main__":
    main()
