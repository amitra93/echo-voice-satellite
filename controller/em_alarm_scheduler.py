"""Wall-clock alarm scheduler — the impure counterpart to pure em_alarms.

Ticks (rather than sleeping to the next fire) so an NTP step, a host
suspend/resume, or a DST jump is caught within one interval; a single long
sleep would miss all three. Minute-granular alarms make a short tick cheap.
An add/delete wakes it immediately via notify() so a soon alarm is not
delayed up to a whole tick.

Every dependency is injected so reconcile_once() is testable with a fake
clock and plain callbacks — no sqlite, no asyncio sleeping — the same
discipline as em_timer_alarm's TimerAlarmRunner.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import em_alarms


log = logging.getLogger(__name__)

# Loaded state per alarm: (spec, next_fire_utc, last_fired_occurrence).
LoadedAlarm = tuple[em_alarms.AlarmSpec, "float | None", "str | None"]

LoadCallback = Callable[[], list[LoadedAlarm]]
PersistCallback = Callable[[str, "float | None", "str | None"], None]
DisableCallback = Callable[[str], None]
# fire(device_id, timer_id, label) -> outcome string ("ringing"/"muted"/…).
FireCallback = Callable[[str, str, "str | None"], Awaitable[str]]

DEFAULT_TICK_S = 20.0


class AlarmScheduler:
    def __init__(
        self,
        *,
        load: LoadCallback,
        persist: PersistCallback,
        disable: DisableCallback,
        fire: FireCallback,
        now: Callable[[], float] = time.time,
        grace_s: float = em_alarms.ALARM_LATE_GRACE_S,
        tick_s: float = DEFAULT_TICK_S,
    ) -> None:
        self._load = load
        self._persist = persist
        self._disable = disable
        self._fire = fire
        self._now = now
        self._grace = grace_s
        self._tick_s = tick_s
        self._wakeup = asyncio.Event()
        self._stop = False

    async def reconcile_once(self) -> None:
        """One full pass: fire everything due (within grace), advance the rest.

        Robust to a long outage: a backlogged occurrence past the grace
        window is skipped and scheduling jumps straight to the next future
        occurrence (floor = now), so we never loop through every missed day.
        `last_fired_occurrence` stops a fired occurrence re-firing on the next
        tick or a later restart.
        """
        now = self._now()
        for spec, next_fire_utc, last_fired in self._load():
            try:
                await self._reconcile_alarm(spec, next_fire_utc, last_fired, now)
            except Exception:  # noqa: BLE001 — one bad alarm must not stall the rest
                log.exception("[alarm %s] reconcile failed", spec.id)

    async def _reconcile_alarm(
        self,
        spec: em_alarms.AlarmSpec,
        next_fire_utc: "float | None",
        last_fired: "str | None",
        now: float,
    ) -> None:
        if next_fire_utc is None:
            # Never scheduled (freshly created without a cached time): compute
            # and store it, but do not treat this pass as a fire.
            self._persist(spec.id, em_alarms.next_fire(spec, now), last_fired)
            return
        if now < next_fire_utc:
            return

        occurrence = em_alarms.occurrence_key(spec, next_fire_utc)
        late = em_alarms.within_grace(next_fire_utc, now, self._grace)
        if last_fired != occurrence and late:
            timer_id = f"alarm:{spec.id}:{occurrence}"
            outcome = await self._fire(spec.device_id, timer_id, spec.label)
            log.info("[alarm %s] fired %s -> %s", spec.id, occurrence, outcome)
        elif last_fired != occurrence:
            log.info("[alarm %s] skipped stale occurrence %s", spec.id, occurrence)

        # Advance. From the occurrence itself when handled on time (next
        # calendar occurrence); from now when we skipped a backlog (jump to
        # the next future occurrence in one step).
        floor = next_fire_utc if late else now
        nxt = em_alarms.next_fire(spec, floor)
        if nxt is None:
            # Spent one-off: disable and clear its schedule.
            self._disable(spec.id)
            self._persist(spec.id, None, occurrence)
        else:
            self._persist(spec.id, nxt, occurrence)

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.reconcile_once()
            except Exception:  # noqa: BLE001
                log.exception("[alarm] scheduler pass failed")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._tick_s)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()

    def notify(self) -> None:
        """Wake the loop now — call after any alarm add/delete."""
        self._wakeup.set()

    def stop(self) -> None:
        self._stop = True
        self._wakeup.set()
