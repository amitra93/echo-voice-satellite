"""A short, sequence-addressed rewind buffer for wake-triggered turns."""

from collections import deque


FRAME_COUNT = 40  # 3.2 seconds at the 80ms PCM cadence


class FrameRing:
    """Retain the latest contiguous data-plane PCM frames for one device."""

    def __init__(self, maxlen: int = FRAME_COUNT):
        self._frames = deque(maxlen=maxlen)

    def append(self, sequence: int, payload: bytes) -> None:
        self._frames.append((sequence, payload))

    def clear(self) -> None:
        self._frames.clear()

    def after(self, activation_sequence: int) -> list[bytes]:
        """Return frames strictly after a retained activation sequence.

        A stream restart clears this ring, and it is far shorter than the
        16-bit sequence rollover period, so an exact lookup avoids guessing
        across either discontinuity.
        """
        for index, (sequence, _payload) in enumerate(self._frames):
            if sequence == activation_sequence:
                return [payload for _sequence, payload in list(self._frames)[index + 1:]]
        return []
