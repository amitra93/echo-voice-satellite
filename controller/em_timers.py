"""Pure timer lifecycle and alarm-queue state.

Home Assistant remains the timer authority. This module only decides which
finished notifications are pending for one originating Echo; playback and
device availability belong to later phases.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


LIFECYCLE_EVENTS = frozenset({"started", "updated", "cancelled", "finished"})
ALARM_SOUND_FILE = "/app/sounds/timer_finished.flac"
MAX_RING_S = 120.0
BURST_GAP_S = 0.75
ALARM_LISTEN_SETTLE_S = 0.40
ALARM_SPEECH_RMS_MIN = 0.015


def alarm_should_capture(rms: float, noise_floor: float) -> bool:
    """Recognize a likely spoken dismissal over the alarm bed."""
    return rms >= max(ALARM_SPEECH_RMS_MIN, noise_floor * 2.5)


@dataclass(frozen=True)
class TimerRecord:
    timer_id: str
    name: str | None
    total_seconds: int
    seconds_left: int
    is_active: bool
    ha_device_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TimerRecord":
        return cls(
            timer_id=payload["timer_id"],
            name=payload.get("name"),
            total_seconds=int(payload.get("total_seconds", 0)),
            seconds_left=int(payload.get("seconds_left", 0)),
            is_active=bool(payload.get("is_active", False)),
            ha_device_id=payload.get("ha_device_id"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "timer_id": self.timer_id,
            "name": self.name,
            "total_seconds": self.total_seconds,
            "seconds_left": self.seconds_left,
            "is_active": self.is_active,
            "ha_device_id": self.ha_device_id,
        }


@dataclass(frozen=True)
class TimerTransition:
    accepted: bool
    duplicate: bool = False
    alarm_changed: bool = False
    discarded: bool = False


class AlarmSession:
    """State for one Echo's running timers and finished alarm queue."""

    def __init__(self) -> None:
        self.running: dict[str, TimerRecord] = {}
        self.current: TimerRecord | None = None
        self.queue: list[TimerRecord] = []
        self._last_payload: dict[str, str] = {}
        self._cancelled: set[str] = set()

    def apply(self, event: dict[str, Any]) -> TimerTransition:
        event_name = event.get("event")
        timer_id = event.get("timer_id")
        if event_name not in LIFECYCLE_EVENTS or not isinstance(timer_id, str) or not timer_id:
            return TimerTransition(False)

        timer = TimerRecord.from_payload(event)
        fingerprint = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        if self._last_payload.get(timer_id) == fingerprint:
            return TimerTransition(True, duplicate=True)
        self._last_payload[timer_id] = fingerprint

        if timer_id in self._cancelled and event_name != "cancelled":
            return TimerTransition(True, duplicate=True)

        if event_name in ("started", "updated"):
            self.running[timer_id] = timer
            return TimerTransition(True, alarm_changed=self._remove_finished(timer_id))

        if event_name == "cancelled":
            self._cancelled.add(timer_id)
            self.running.pop(timer_id, None)
            return TimerTransition(True, alarm_changed=self._remove_finished(timer_id))

        self.running.pop(timer_id, None)
        if self.current and self.current.timer_id == timer_id:
            return TimerTransition(True, alarm_changed=False)
        if any(item.timer_id == timer_id for item in self.queue):
            return TimerTransition(True, duplicate=True)
        if self.current is None:
            self.current = timer
        else:
            self.queue.append(timer)
        return TimerTransition(True, alarm_changed=True)

    def dismiss_current(self) -> TimerRecord | None:
        if self.current is None:
            return None
        dismissed = self.current
        self.current = self.queue.pop(0) if self.queue else None
        return dismissed

    def dismiss_all(self) -> list[TimerRecord]:
        dismissed = ([self.current] if self.current else []) + list(self.queue)
        self.current = None
        self.queue.clear()
        return dismissed

    def timeout_current(self) -> TimerRecord | None:
        """Advance after the unanswered-alarm safety timeout."""
        return self.dismiss_current()

    def delivery_failed(self) -> TimerRecord | None:
        """Advance when delivering the current alarm is impossible."""
        return self.dismiss_current()

    def disconnect(self) -> list[TimerRecord]:
        """Discard undeliverable alarms while retaining HA running state."""
        return self.dismiss_all()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": "ringing" if self.current else ("queued" if self.queue else "idle"),
            "current": self.current.as_dict() if self.current else None,
            "queue": [timer.as_dict() for timer in self.queue],
        }

    def _remove_finished(self, timer_id: str) -> bool:
        changed = False
        if self.current and self.current.timer_id == timer_id:
            self.current = self.queue.pop(0) if self.queue else None
            changed = True
        before = len(self.queue)
        self.queue = [timer for timer in self.queue if timer.timer_id != timer_id]
        return changed or len(self.queue) != before
