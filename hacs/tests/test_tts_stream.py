"""tts_stream.py against a faked ffmpeg subprocess — no real ffmpeg needed.

Mirrors controller/tests/test_phase1_turn_engine.py's
`test_test_audio_decoder_*` pattern: monkeypatch
`asyncio.create_subprocess_exec` rather than depending on a real binary
being on PATH.
"""

import asyncio

import pytest

from custom_components.echo_voice_satellite import tts_stream
from custom_components.echo_voice_satellite.audio_frame import TTS_FRAME_BYTES


class _FakeStdin:
    def __init__(self):
        self.written = bytearray()
        self._closing = False

    def write(self, chunk):
        self.written.extend(chunk)

    async def drain(self):
        pass

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True


class _FakeProcess:
    def __init__(self, output: bytes, returncode: int = 0, err: bytes = b""):
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(err)
        self.stderr.feed_eof()
        self._returncode = returncode
        self.returncode = None

    async def wait(self):
        self.returncode = self._returncode
        return self._returncode

    def kill(self):
        self.returncode = -9


class _FakeResultStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def async_stream_result(self):
        for chunk in self._chunks:
            yield chunk


class _FakeChannel:
    def __init__(self):
        self.sent = []
        self.eos_sent = False

    async def send_tts(self, payload):
        self.sent.append(payload)

    async def send_tts_eos(self):
        self.eos_sent = True


def _patch(monkeypatch, **process_kwargs):
    # The fake process (and its asyncio.StreamReader) must be built INSIDE
    # the running loop asyncio.run() provides, not before — StreamReader()
    # requires a current event loop at construction time.
    async def fake_exec(*args, **kwargs):
        assert "-ar" in args and args[args.index("-ar") + 1] == "24000"
        assert "-ac" in args and args[args.index("-ac") + 1] == "1"
        return _FakeProcess(**process_kwargs)

    monkeypatch.setattr(tts_stream.asyncio, "create_subprocess_exec", fake_exec)


def test_streams_pcm_in_frame_sized_chunks_and_signals_eos(monkeypatch):
    pcm = b"\x01\x02" * (TTS_FRAME_BYTES // 2 + 100)  # spans two frames
    _patch(monkeypatch, output=pcm)
    channel = _FakeChannel()

    sent = asyncio.run(
        tts_stream.stream_result_to_audio(_FakeResultStream([b"anything"]), channel)
    )

    assert sent == len(pcm)
    assert b"".join(channel.sent) == pcm
    assert all(len(chunk) <= TTS_FRAME_BYTES for chunk in channel.sent)
    assert channel.eos_sent is True


def test_nonzero_ffmpeg_exit_raises_tts_incompatible(monkeypatch):
    _patch(monkeypatch, output=b"", returncode=1, err=b"bad input")
    channel = _FakeChannel()

    with pytest.raises(tts_stream.TTSIncompatible, match="ffmpeg conversion failed"):
        asyncio.run(
            tts_stream.stream_result_to_audio(_FakeResultStream([b"x"]), channel)
        )
    assert channel.eos_sent is False


def test_turn_byte_limit_is_enforced(monkeypatch):
    pcm = b"\x00\x00" * 10
    _patch(monkeypatch, output=pcm)
    channel = _FakeChannel()

    with pytest.raises(tts_stream.TTSIncompatible, match="limit exceeded"):
        asyncio.run(
            tts_stream.stream_result_to_audio(
                _FakeResultStream([b"x"]), channel, max_bytes=5
            )
        )


def test_odd_trailing_byte_is_rejected_as_incompatible(monkeypatch):
    _patch(monkeypatch, output=b"\x00\x00\x00")  # one full sample + 1 stray byte
    channel = _FakeChannel()

    with pytest.raises(tts_stream.TTSIncompatible, match="odd PCM payload"):
        asyncio.run(
            tts_stream.stream_result_to_audio(_FakeResultStream([b"x"]), channel)
        )


def test_output_exactly_one_frame_never_exceeds_the_frame_limit(monkeypatch):
    # ffmpeg's stdout is read in 16384-byte gulps (below the 24000-byte
    # frame cap), so a payload exactly one frame long can still arrive as
    # two smaller sends — the contract is "at most TTS_FRAME_BYTES per
    # send_tts call and nothing is dropped or reordered", not "coalesced
    # to exactly one call".
    pcm = b"\x02\x01" * (TTS_FRAME_BYTES // 2)
    assert len(pcm) == TTS_FRAME_BYTES
    _patch(monkeypatch, output=pcm)
    channel = _FakeChannel()

    sent = asyncio.run(
        tts_stream.stream_result_to_audio(_FakeResultStream([b"x"]), channel)
    )

    assert sent == TTS_FRAME_BYTES
    assert b"".join(channel.sent) == pcm
    assert all(len(chunk) <= TTS_FRAME_BYTES for chunk in channel.sent)
    assert channel.eos_sent is True


def test_empty_output_still_sends_eos_with_nothing_played(monkeypatch):
    _patch(monkeypatch, output=b"")
    channel = _FakeChannel()

    sent = asyncio.run(
        tts_stream.stream_result_to_audio(_FakeResultStream([b"x"]), channel)
    )

    assert sent == 0
    assert channel.sent == []
    assert channel.eos_sent is True


def test_feeder_writes_every_chunk_from_the_result_stream(monkeypatch):
    _patch(monkeypatch, output=b"\x00\x00")
    channel = _FakeChannel()
    written = []

    real_exec = tts_stream.asyncio.create_subprocess_exec

    async def spying_exec(*args, **kwargs):
        process = await real_exec(*args, **kwargs)
        original_write = process.stdin.write

        def spy(chunk):
            written.append(chunk)
            original_write(chunk)

        process.stdin.write = spy
        return process

    monkeypatch.setattr(tts_stream.asyncio, "create_subprocess_exec", spying_exec)

    asyncio.run(
        tts_stream.stream_result_to_audio(
            _FakeResultStream([b"chunk-one", b"chunk-two"]), channel
        )
    )

    assert written == [b"chunk-one", b"chunk-two"]
