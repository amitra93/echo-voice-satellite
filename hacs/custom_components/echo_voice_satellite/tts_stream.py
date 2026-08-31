"""Incremental transcode of HA's TTS ResultStream to the wire PCM format.

Model 3 (docs/design/full-duplex-plan.md "TTS conversion"): HA's `assist_satellite`
support gives a custom integration a streaming `ResultStream`
(`tts.async_get_stream`), not just a `tts_proxy` URL. Providers commonly
return MP3/OGG/WAV at whatever rate the engine produces, so a decode step is
unavoidable — this module runs it as the bytes arrive, via one long-lived
ffmpeg process, and never buffers the whole response. The controller does a
cheap linear 24kHz->48kHz upsample and applies EQ; this module's only job is
producing correct 24 kHz mono S16LE PCM as fast as HA's engine emits it.
"""

from __future__ import annotations

import asyncio

from .audio_frame import TTS_FRAME_BYTES


class TTSIncompatible(ValueError):
    def __init__(self, observed: str):
        super().__init__(observed)
        self.observed = observed


async def stream_result_to_audio(
    result_stream, audio_channel, *, max_bytes: int = 43_200_000
) -> int:
    """Transcode a real HA ResultStream to 24 kHz mono S16LE and forward it.

    Streams `send_tts` calls to `audio_channel` as PCM becomes available and
    finishes with `send_tts_eos`. Returns the number of PCM bytes sent.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    async def feed() -> None:
        try:
            async for chunk in result_stream.async_stream_result():
                process.stdin.write(chunk)
                await process.stdin.drain()
        finally:
            if not process.stdin.is_closing():
                process.stdin.close()

    feeder = asyncio.create_task(feed())
    sent = 0
    pending = bytearray()
    try:
        while True:
            chunk = await process.stdout.read(16_384)
            if not chunk:
                break
            pending.extend(chunk)
            usable = len(pending) - (len(pending) % 2)
            while usable:
                pcm = bytes(pending[:min(TTS_FRAME_BYTES, usable)])
                del pending[:len(pcm)]
                usable = len(pending) - (len(pending) % 2)
                sent += len(pcm)
                if sent > max_bytes:
                    raise TTSIncompatible("tts_turn_bytes limit exceeded")
                await audio_channel.send_tts(pcm)
        await feeder
        returncode = await process.wait()
        if returncode != 0:
            error = await process.stderr.read()
            raise TTSIncompatible(
                f"ffmpeg conversion failed: {error.decode(errors='replace')[:160]}"
            )
        if pending:
            raise TTSIncompatible("odd PCM payload")
        await audio_channel.send_tts_eos()
        return sent
    finally:
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
