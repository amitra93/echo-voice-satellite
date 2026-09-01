"""Generation-checked device stop-arm state.

The device flushes voice audio before it sends ``stop_detected``. This module
only decides whether that best-effort report still belongs to the active turn;
it must never turn a late packet into a cancellation of a newer response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Phase = Literal["thinking", "playback", "timer"]


@dataclass(frozen=True)
class Decision:
    action: Literal["armed", "disarmed", "accept", "ignore"]
    reason: str = ""


@dataclass(frozen=True)
class Arm:
    turn_id: int
    generation: int
    phase: Phase
    expires_mono: float


class StopState:
    """One active arm per device, with monotonic generations."""

    def __init__(self):
        self.arm_state: Arm | None = None
        self._last_generation = 0
        self._accepted_generation: int | None = None

    def arm(self, turn_id: int, generation: int, phase: Phase,
            expires_mono: float) -> Decision:
        if not isinstance(turn_id, int) or turn_id <= 0:
            return Decision("ignore", "invalid turn")
        if phase not in ("thinking", "playback", "timer"):
            return Decision("ignore", "invalid phase")
        if not isinstance(generation, int) or generation <= self._last_generation:
            return Decision("ignore", "stale generation")
        self.arm_state = Arm(turn_id, generation, phase, float(expires_mono))
        self._last_generation = generation
        self._accepted_generation = None
        return Decision("armed")

    def disarm(self, generation: int | None = None) -> Decision:
        arm = self.arm_state
        if arm is None:
            return Decision("ignore", "not armed")
        if generation is not None and generation != arm.generation:
            return Decision("ignore", "stale generation")
        self.arm_state = None
        return Decision("disarmed")

    def detected(self, turn_id, generation, phase, now_mono: float) -> Decision:
        arm = self.arm_state
        if arm is None:
            if generation == self._accepted_generation:
                return Decision("ignore", "duplicate detection")
            return Decision("ignore", "not armed")
        if generation != arm.generation:
            return Decision("ignore", "stale generation")
        if now_mono >= arm.expires_mono:
            self.arm_state = None
            return Decision("ignore", "arm expired")
        if turn_id != arm.turn_id:
            return Decision("ignore", "wrong turn")
        if phase != arm.phase:
            return Decision("ignore", "wrong phase")
        self.arm_state = None
        self._accepted_generation = arm.generation
        return Decision("accept")

    def expire(self, now_mono: float) -> Decision:
        arm = self.arm_state
        if arm is None or now_mono < arm.expires_mono:
            return Decision("ignore", "not expired")
        self.arm_state = None
        return Decision("disarmed", "arm expired")
