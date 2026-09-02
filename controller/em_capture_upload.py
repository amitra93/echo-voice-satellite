"""Pure parser for device wake-training capture uploads."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field

CAPTURE_BEGIN = 0x10
CAPTURE_PCM = 0x11
CAPTURE_END = 0x12
PROTOCOL_VERSION = 1
FRAME_BYTES = 2560
MAX_FRAMES = 62
MAX_METADATA_BYTES = 4096
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


class CaptureProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class CompletedCapture:
    metadata: dict
    pcm: bytes


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureProtocolError(f"invalid {name}")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise CaptureProtocolError(f"invalid {name}")
    return number


def validate_metadata(metadata: dict) -> dict:
    if not isinstance(metadata, dict):
        raise CaptureProtocolError("metadata must be an object")
    capture_id = metadata.get("captureId")
    model = metadata.get("model")
    checksum = metadata.get("classifierMd5")
    kind = metadata.get("kind")
    if not isinstance(capture_id, str) or not _ID_RE.fullmatch(capture_id):
        raise CaptureProtocolError("invalid capture id")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", model):
        raise CaptureProtocolError("invalid model")
    if not isinstance(checksum, str) or not _MD5_RE.fullmatch(checksum):
        raise CaptureProtocolError("invalid classifier checksum")
    if kind not in {"act", "miss"}:
        raise CaptureProtocolError("invalid kind")
    score = _number(metadata.get("score"), "score")
    threshold = _number(metadata.get("threshold"), "threshold")
    floor = _number(metadata.get("nearMissFloor"), "near-miss floor")
    if kind == "act" and score < threshold:
        raise CaptureProtocolError("activation below threshold")
    if kind == "miss" and not floor < score < threshold:
        raise CaptureProtocolError("near miss outside thresholds")
    if metadata.get("sampleRate") != 16000 or metadata.get("sampleWidth") != 2:
        raise CaptureProtocolError("invalid audio format")
    if metadata.get("channels") != 1 or metadata.get("frameBytes") != FRAME_BYTES:
        raise CaptureProtocolError("invalid audio framing")
    sequence = metadata.get("activationSeq")
    requested = metadata.get("requestedPrerollMs")
    actual = metadata.get("actualPrerollMs")
    if not isinstance(sequence, int) or not 0 <= sequence <= 0xFFFF:
        raise CaptureProtocolError("invalid activation sequence")
    if not isinstance(requested, int) or not 80 <= requested <= 5000:
        raise CaptureProtocolError("invalid requested preroll")
    if not isinstance(actual, int) or not 80 <= actual <= MAX_FRAMES * 80:
        raise CaptureProtocolError("invalid actual preroll")
    complete = metadata.get("complete")
    if not isinstance(complete, bool):
        raise CaptureProtocolError("invalid completeness")
    if actual > requested or complete != (actual == requested):
        raise CaptureProtocolError("inconsistent completeness")
    if not isinstance(metadata.get("bargeThresholdActive"), bool):
        raise CaptureProtocolError("invalid barge flag")
    validated = dict(metadata)
    validated.update(score=score, threshold=threshold, nearMissFloor=floor)
    return validated


@dataclass
class Receiver:
    metadata: dict | None = None
    chunks: list[bytes] = field(default_factory=list)
    next_index: int = 0

    def reset(self) -> None:
        self.metadata = None
        self.chunks.clear()
        self.next_index = 0

    def feed(self, raw: bytes) -> CompletedCapture | None:
        try:
            if not raw:
                raise CaptureProtocolError("empty frame")
            if raw[0] == CAPTURE_BEGIN:
                return self._begin(raw)
            if raw[0] == CAPTURE_PCM:
                return self._pcm(raw)
            if raw[0] == CAPTURE_END:
                return self._end(raw)
            raise CaptureProtocolError("unknown capture frame")
        except Exception:
            self.reset()
            raise

    def _begin(self, raw: bytes) -> None:
        if self.metadata is not None or len(raw) < 4 or raw[1] != PROTOCOL_VERSION:
            raise CaptureProtocolError("invalid begin")
        length = struct.unpack(">H", raw[2:4])[0]
        if not 0 < length <= MAX_METADATA_BYTES or len(raw) != 4 + length:
            raise CaptureProtocolError("invalid metadata length")
        try:
            metadata = json.loads(raw[4:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureProtocolError("invalid metadata JSON") from exc
        self.metadata = validate_metadata(metadata)
        return None

    def _pcm(self, raw: bytes) -> None:
        if self.metadata is None or len(raw) != 3 + FRAME_BYTES:
            raise CaptureProtocolError("invalid PCM frame")
        index = struct.unpack(">H", raw[1:3])[0]
        if index != self.next_index or index >= MAX_FRAMES:
            raise CaptureProtocolError("invalid PCM sequence")
        self.chunks.append(bytes(raw[3:]))
        self.next_index += 1
        return None

    def _end(self, raw: bytes) -> CompletedCapture:
        if self.metadata is None or len(raw) != 23 or not self.chunks:
            raise CaptureProtocolError("invalid end frame")
        chunks, byte_count = struct.unpack(">HI", raw[1:7])
        pcm = b"".join(self.chunks)
        if chunks != len(self.chunks) or byte_count != len(pcm):
            raise CaptureProtocolError("capture totals mismatch")
        if self.metadata["actualPrerollMs"] != chunks * 80:
            raise CaptureProtocolError("capture duration mismatch")
        if hashlib.md5(pcm).digest() != raw[7:23]:
            raise CaptureProtocolError("capture digest mismatch")
        completed = CompletedCapture(dict(self.metadata), pcm)
        self.reset()
        return completed
