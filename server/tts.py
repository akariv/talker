"""Text-to-speech: text → 16-bit 16kHz mono PCM."""

import logging
import numpy as np
from scipy.signal import resample_poly
import openai

log = logging.getLogger(__name__)


def text_to_pcm(text: str) -> bytes:
    """Convert text to 16-bit 16kHz mono PCM via OpenAI TTS."""
    client = openai.OpenAI()

    # OpenAI TTS returns 24kHz 16-bit mono PCM with response_format="pcm"
    response = client.audio.speech.create(
        model="tts-1",
        voice="shimmer",
        input=text,
        response_format="pcm",
    )
    pcm_24k = response.content
    log.info(f"TTS returned {len(pcm_24k)} bytes (24kHz)")

    # Resample 24kHz → 16kHz (factor 2/3)
    samples_24k = np.frombuffer(pcm_24k, dtype=np.int16)
    samples_16k = resample_poly(samples_24k, up=2, down=3).astype(np.int16)

    pcm_16k = samples_16k.tobytes()
    log.info(f"Resampled to {len(pcm_16k)} bytes (16kHz)")
    return pcm_16k
