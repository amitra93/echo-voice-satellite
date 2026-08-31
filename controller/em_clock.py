"""Controller/device monotonic clock synchronization.

The device clock is an arbitrary monotonic origin, not wall time. A probe uses
the four NTP-style timestamps below, all expressed in microseconds in their
sender's monotonic domain:

    t1 controller sends, t2 device receives, t3 device sends, t4 controller receives

The estimator keeps only low-RTT samples because TCP retransmission delay is
not clock offset. A linear fit of offset against controller time also captures
the small clock-rate difference needed for scheduled playback.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import mean
import time


def monotonic_us() -> int:
    """Return the controller's monotonic clock in microseconds."""
    return time.monotonic_ns() // 1_000


@dataclass(frozen=True)
class ClockSample:
    controller_us: int
    offset_us: float
    rtt_us: int


class ClockSync:
    """Bounded, RTT-filtered offset/drift estimator."""

    def __init__(
        self,
        *,
        max_rtt_us: int = 200_000,
        max_samples: int = 32,
        min_samples: int = 4,
        min_span_us: int = 500_000,
    ) -> None:
        if (
            max_rtt_us <= 0
            or max_samples < 2
            or min_samples < 2
            or min_samples > max_samples
            or min_span_us < 0
        ):
            raise ValueError("invalid clock synchronizer limits")
        self.max_rtt_us = max_rtt_us
        self.samples: deque[ClockSample] = deque(maxlen=max_samples)
        self.min_samples = min_samples
        self.min_span_us = min_span_us
        self.rejected = 0
        self._offset_us = 0.0
        self._drift = 0.0
        self._reference_us: int | None = None

    def reset(self) -> None:
        self.samples.clear()
        self.rejected = 0
        self._offset_us = 0.0
        self._drift = 0.0
        self._reference_us = None

    def update(self, t1: int, t2: int, t3: int, t4: int) -> bool:
        """Accept one probe and return whether it passed validation/filtering."""
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (t1, t2, t3, t4)):
            self.rejected += 1
            return False
        if any(value < 0 for value in (t1, t2, t3, t4)):
            self.rejected += 1
            return False
        # t1/t4 share the controller clock and t2/t3 share the device clock;
        # their absolute numeric origins are unrelated, so never compare
        # timestamps across those domains directly.
        if not (t1 < t4 and t2 <= t3):
            self.rejected += 1
            return False
        rtt_us = (t4 - t1) - (t3 - t2)
        if rtt_us <= 0 or rtt_us > self.max_rtt_us:
            self.rejected += 1
            return False
        # The midpoint formula cancels symmetric network delay and yields
        # device_clock - controller_clock at the probe midpoint.
        midpoint = (t1 + t4) // 2
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        self.samples.append(ClockSample(midpoint, offset, rtt_us))
        self._fit()
        return True

    def _fit(self) -> None:
        if not self.samples:
            return
        reference = self.samples[0].controller_us
        xs = [sample.controller_us - reference for sample in self.samples]
        ys = [sample.offset_us for sample in self.samples]
        x_mean = mean(xs)
        y_mean = mean(ys)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        if denominator:
            self._drift = sum(
                (x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)
            ) / denominator
        else:
            self._drift = 0.0
        self._reference_us = reference
        self._offset_us = y_mean - self._drift * x_mean

    @property
    def synchronized(self) -> bool:
        if len(self.samples) < self.min_samples or self._reference_us is None:
            return False
        return self.samples[-1].controller_us - self.samples[0].controller_us >= self.min_span_us

    @property
    def offset_us(self) -> float | None:
        return None if self._reference_us is None else self._offset_at(self.samples[-1].controller_us)

    @property
    def drift_ppm(self) -> float:
        return self._drift * 1_000_000.0

    @property
    def rtt_us(self) -> int | None:
        return self.samples[-1].rtt_us if self.samples else None

    def _offset_at(self, controller_us: int) -> float:
        if self._reference_us is None:
            return 0.0
        return self._offset_us + self._drift * (controller_us - self._reference_us)

    def controller_to_device(self, controller_us: int) -> int:
        """Convert a controller monotonic timestamp into device time."""
        if self._reference_us is None:
            raise RuntimeError("clock has not received a valid sample")
        return round(controller_us + self._offset_at(controller_us))

    def device_to_controller(self, device_us: int) -> int:
        """Convert a device timestamp back into controller time."""
        if self._reference_us is None:
            raise RuntimeError("clock has not received a valid sample")
        # Drift is tiny; one fixed-point iteration is sufficient at microsecond
        # precision and avoids solving a separate floating-point equation.
        controller = device_us - self._offset_us
        controller -= self._drift * (controller - self._reference_us)
        return round(controller)
