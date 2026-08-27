"""A sequence-addressed PCM preroll buffer for wake-triggered turns."""

from collections import deque
from dataclasses import dataclass


SEQUENCE_MODULUS = 1 << 16
FRAME_MS = 80
PREROLL_MS = 2_000
PREROLL_FRAMES = PREROLL_MS // FRAME_MS
FRAME_COUNT = 100  # 8 seconds at the 80ms PCM cadence


@dataclass(frozen=True)
class Preroll:
    frames: tuple[bytes, ...]
    complete: bool
    start_sequence: int | None
    reason: str | None = None


class FrameRing:
    """Retain the latest contiguous data-plane PCM frames for one device."""

    def __init__(self, maxlen: int = FRAME_COUNT):
        self._frames = deque(maxlen=maxlen)
        self._last_sequence: int | None = None

    def append(self, sequence: int, payload: bytes) -> None:
        sequence &= SEQUENCE_MODULUS - 1
        if (self._last_sequence is not None
                and sequence != (self._last_sequence + 1) % SEQUENCE_MODULUS):
            # The device resets its data-frame sequence when a mic stream is
            # restarted. Never retain frames from both sequence epochs.
            self.clear()
        self._frames.append((sequence, payload))
        self._last_sequence = sequence

    def clear(self) -> None:
        self._frames.clear()
        self._last_sequence = None

    def from_activation(
        self,
        activation_sequence: int,
        preroll_frames: int = PREROLL_FRAMES,
    ) -> Preroll:
        """Return preroll through the latest frame after an activation.

        The requested start is inclusive and is exactly ``preroll_frames``
        frames before the activation frame. If the start is no longer
        retained, return the safe suffix after the activation rather than
        guessing across a missing frame or stream restart.
        """
        activation_sequence &= SEQUENCE_MODULUS - 1
        frames = list(self._frames)
        activation_index = next(
            (i for i, (sequence, _payload) in enumerate(frames)
             if sequence == activation_sequence),
            None,
        )
        if activation_index is None:
            return Preroll((), False, None, "activation sequence is outside the PCM ring")

        start_sequence = (activation_sequence - preroll_frames) % SEQUENCE_MODULUS
        start_index = next(
            (i for i, (sequence, _payload) in enumerate(frames)
             if sequence == start_sequence),
            None,
        )
        if start_index is None or start_index > activation_index:
            suffix = tuple(payload for _sequence, payload in frames[activation_index + 1:])
            return Preroll(
                suffix,
                False,
                start_sequence,
                "requested preroll start is outside the PCM ring",
            )

        selected = frames[start_index:]
        expected = start_sequence
        for sequence, _payload in selected:
            if sequence != expected:
                suffix = tuple(payload for _sequence, payload in frames[activation_index + 1:])
                return Preroll(
                    suffix,
                    False,
                    start_sequence,
                    "PCM ring contains a sequence gap",
                )
            expected = (expected + 1) % SEQUENCE_MODULUS
        return Preroll(
            tuple(payload for _sequence, payload in selected),
            True,
            start_sequence,
        )

    def from_latest(self, preroll_frames: int = PREROLL_FRAMES) -> Preroll:
        """Return the latest preroll window for a controller-side wake."""
        frames = list(self._frames)
        if not frames:
            return Preroll((), False, None, "PCM ring is empty")
        selected = frames[-preroll_frames:]
        complete = len(selected) == preroll_frames
        return Preroll(
            tuple(payload for _sequence, payload in selected),
            complete,
            selected[0][0],
            None if complete else "PCM ring has fewer than the requested frames",
        )

    def after(self, activation_sequence: int) -> list[bytes]:
        """Return frames strictly after a retained activation sequence.

        A stream restart clears this ring, and it is far shorter than the
        16-bit sequence rollover period, so an exact lookup avoids guessing
        across either discontinuity.
        """
        activation_sequence &= SEQUENCE_MODULUS - 1
        frames = list(self._frames)
        for index, (sequence, _payload) in enumerate(frames):
            if sequence == activation_sequence:
                return [payload for _sequence, payload in frames[index + 1:]]
        return []
