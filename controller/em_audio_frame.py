"""Bidirectional HA audio WebSocket framing."""

from __future__ import annotations

import struct
from dataclasses import dataclass

MIC_PCM = 0x01
MIC_EOS = 0x02
TTS_PCM = 0x03
TTS_EOS = 0x04
MIC_FRAME_BYTES = 2_560       # 80 ms at 16 kHz, mono, S16LE
TTS_FRAME_BYTES = 24_000      # 500 ms at 24 kHz, mono, S16LE
_HEADER = struct.Struct(">BBI")


class AudioProtocolError(ValueError):
    """Raised when an audio frame violates the turn audio contract."""


@dataclass(frozen=True)
class AudioFrame:
    frame_type: int
    sequence: int
    payload: bytes


def encode_frame(frame_type: int, sequence: int, payload: bytes = b"") -> bytes:
    if frame_type not in (MIC_PCM, MIC_EOS, TTS_PCM, TTS_EOS):
        raise AudioProtocolError("unknown audio frame type")
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise AudioProtocolError("audio sequence out of range")
    if frame_type in (MIC_EOS, TTS_EOS) and payload:
        raise AudioProtocolError("EOS payload must be empty")
    if frame_type == MIC_PCM and len(payload) != MIC_FRAME_BYTES:
        raise AudioProtocolError("MIC_PCM payload must be 2560 bytes")
    if frame_type == TTS_PCM and (
        not payload or len(payload) > TTS_FRAME_BYTES or len(payload) % 2
    ):
        raise AudioProtocolError("TTS_PCM payload must be even and at most 24000 bytes")
    return _HEADER.pack(frame_type, 0, sequence) + payload


def decode_frame(raw: bytes) -> AudioFrame:
    if len(raw) < _HEADER.size:
        raise AudioProtocolError("audio frame header is truncated")
    frame_type, flags, sequence = _HEADER.unpack(raw[:_HEADER.size])
    if flags:
        raise AudioProtocolError("audio flags must be zero")
    payload = raw[_HEADER.size:]
    encode_frame(frame_type, sequence, payload)
    return AudioFrame(frame_type, sequence, payload)


class Sequence:
    """Check one direction's monotonically increasing frame sequence."""

    def __init__(self) -> None:
        self.expected = 0
        self.eos = False

    def accept(self, frame: AudioFrame) -> None:
        if self.eos:
            raise AudioProtocolError("audio frame after EOS")
        if frame.sequence != self.expected:
            raise AudioProtocolError("audio sequence error")
        self.expected += 1
        if frame.frame_type in (MIC_EOS, TTS_EOS):
            self.eos = True
