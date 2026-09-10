"""
Pure timer countdown-ring state.

Computes the LED countdown-ring spec from `em_timers` state — which running
timer (if any) should own the ring, and what fraction/rate spec to send it —
without touching a device, asyncio, or a clock of its own. The caller
supplies `now` (a monotonic reading) the same way `em_timers.TimerRecord.
received_at` is populated by its caller rather than read in `em_timers`
itself: every module in this family stays clock-free so it can be tested
with plain floats.
"""

from __future__ import annotations

import em_timers


# Countdown-ring dead-man TTL: floor and ceiling in seconds. The floor
# matches em_scenes.meter_ttl's own floor (a timer with only a few seconds
# left still gets a sane window); the ceiling caps how long a controller
# that dies and never comes back leaves a stale spec on a device — past an
# hour of remaining time, more padding buys nothing, since the ceiling only
# ever matters for that dead-controller edge case, which is a dark ring
# either way per the design's "no resync on restart" decision.
TTL_FLOOR_S = 30.0
TTL_CEILING_S = 3600.0


def first_running(session: "em_timers.AlarmSession") -> "em_timers.TimerRecord | None":
    """
    The first-started timer still running, or None if none is.

    `session.running` is a plain dict, and `AlarmSession.apply()` only ever
    does `self.running[timer_id] = timer` — it never reorders an existing
    key on a later `updated` event — so as long as `started` is the first
    event ever seen for a given timer_id (true by construction: HA does not
    emit `updated` before `started`), insertion order already IS start
    order. A timer that was cancelled and later replaced by a new one with
    an earlier-alphabetical id, for instance, still surfaces correctly here
    because cancellation removes the old key entirely rather than leaving a
    tombstone to reorder around.
    """
    for timer in session.running.values():
        return timer
    return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def countdown_spec(
    timer: "em_timers.TimerRecord",
    now: float,
    color: tuple[int, int, int],
) -> dict | None:
    """
    Build the wire-ready `led_anim` spec for a running timer's countdown
    ring, or None if there is nothing to interpolate from.

    Returns None when `timer.received_at` is unset (this record predates
    the controller ever observing a clock for it) or `total_seconds <= 0`
    (a malformed payload — dividing by it would raise or produce nonsense).
    """
    if timer.received_at is None or timer.total_seconds <= 0:
        return None

    elapsed = (now - timer.received_at) if timer.is_active else 0.0
    # Clamped defensively, not trusted: it is not confirmed whether HA's
    # `total_seconds` payload field is adjusted when a running timer is
    # extended ("add five minutes to the pasta timer"). If it isn't, a
    # `seconds_left` that has grown past the original `total_seconds` must
    # not push the fraction above 1.0 (or, symmetrically, below 0.0 once a
    # timer has run past its nominal end while a `finished` event is still
    # in flight).
    fraction_now = _clamp(
        (timer.seconds_left - elapsed) / timer.total_seconds, 0.0, 1.0
    )
    running_mps = 1.0 / timer.total_seconds if timer.is_active else 0.0

    # Padded past the timer's own remaining time, the same x2-plus-pad shape
    # as em_scenes.meter_ttl, so a normal `updated`/`finished` HA event
    # always arrives before this dead-man does; see TTL_FLOOR_S/TTL_CEILING_S
    # above for why those two bounds are what they are.
    ttl_sec = int(min(TTL_CEILING_S, max(TTL_FLOOR_S, timer.seconds_left * 2.0 + 20.0)))

    return {
        "pattern": "countdown",
        "colors": [list(color)],
        "fractionNow": fraction_now,
        "runningMps": running_mps,
        "ttlSec": ttl_sec,
    }
