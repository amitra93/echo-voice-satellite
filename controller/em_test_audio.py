"""Audio normalization used by the controller's Phase 1b E2E test."""

from __future__ import annotations

import asyncio
import io
import wave

TEST_AUDIO_MAX_INPUT = 50 * 1024 * 1024
TEST_AUDIO_MAX_SECONDS = 120


async def decode_test_audio(data: bytes) -> bytes:
    """Normalize an uploaded file to bounded 16 kHz mono S16 PCM."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        pcm, err = await asyncio.wait_for(proc.communicate(input=data), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("audio conversion timed out")
    if proc.returncode != 0:
        raise ValueError(f"audio conversion failed: {err.decode(errors='replace')[:200]}")
    max_bytes = TEST_AUDIO_MAX_SECONDS * 16_000 * 2
    if not pcm:
        raise ValueError("audio contains no samples")
    if len(pcm) > max_bytes:
        raise ValueError(f"audio exceeds {TEST_AUDIO_MAX_SECONDS} seconds")
    return pcm


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap normalized PCM in a canonical 16 kHz mono WAV for the device."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(pcm)
    return out.getvalue()
