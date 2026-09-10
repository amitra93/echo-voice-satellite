"""Pure wall-clock alarm scheduling logic.

The controller owns durable alarm records — unlike timers, which Home
Assistant owns — because HA has no alarm concept to defer to (see
docs/design/alarms-design.md). This module is the clock-free decision core,
tested with plain floats and fixed timezones; the impure asyncio scheduler
lives in em_alarm_scheduler.py, the same split as em_timers vs
em_timer_alarm.

Wall clock, deliberately and only here: an alarm's "07:00" is a wall-clock
fact, not a duration, so — against the codebase's monotonic-by-default rule,
which exists for the device's bogus pre-NTP clock — scheduling reads real
time. Every function still takes ``now_utc``/``after_utc`` as a float epoch
supplied by the caller rather than reading a clock of its own, keeping the
module testable the same way the rest of this family is.

DST is handled by construction: a next-fire time is always re-materialised in
the alarm's own ``ZoneInfo`` from its local wall time, so a 07:00 alarm stays
07:00 local across a transition with no correction step. On the transition
day, a spring-forward gap (a local time that does not exist) resolves to the
first valid instant after the gap, and a fall-back ambiguity (a local time
that occurs twice) resolves to the earlier of the two (``fold=0``).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo


RECURRENCE = frozenset({"once", "daily", "weekly"})

# How late a missed occurrence may still fire on controller startup or device
# reconnect. Past this, the occurrence is skipped and (for recurring alarms)
# scheduling jumps to the next future occurrence. See the scheduler.
ALARM_LATE_GRACE_S = 600.0

# Weekday bitmask: bit 0 = Monday … bit 6 = Sunday, matching
# datetime.date.weekday(). The three common presets are named for the API and
# card to reuse rather than re-deriving the masks.
WEEKDAYS_MASK = 0b0011111  # Mon–Fri
WEEKENDS_MASK = 0b1100000  # Sat–Sun
ALL_DAYS_MASK = 0b1111111


@dataclass(frozen=True)
class AlarmSpec:
    """One durable alarm record, as pure data.

    ``next_fire_utc`` is not held here — it is derived by :func:`next_fire`
    and cached in the DB row by the scheduler, the same way em_timer_ring
    derives a countdown from an em_timers record rather than storing it.
    """

    id: str
    device_id: str
    hour: int
    minute: int
    recurrence: str            # one of RECURRENCE
    tz: str                    # IANA timezone name
    enabled: bool = True
    weekday_mask: int = 0      # weekly only; ignored otherwise
    date: str | None = None    # once only; ISO local date "YYYY-MM-DD"
    label: str | None = None


def validate(spec: AlarmSpec) -> None:
    """Raise ValueError if the spec could never schedule sensibly.

    Called at the API boundary so a malformed alarm is rejected on write
    rather than silently never firing. Kept here, beside the semantics it
    guards, so the scheduler and every test share one definition of valid.
    """
    if not (0 <= spec.hour <= 23) or not (0 <= spec.minute <= 59):
        raise ValueError("time out of range")
    if spec.recurrence not in RECURRENCE:
        raise ValueError(f"unknown recurrence {spec.recurrence!r}")
    try:
        ZoneInfo(spec.tz)
    except Exception as e:  # noqa: BLE001 — any ZoneInfo failure is a bad tz
        raise ValueError(f"unknown timezone {spec.tz!r}") from e
    if spec.recurrence == "weekly" and not (spec.weekday_mask & ALL_DAYS_MASK):
        raise ValueError("weekly alarm has no days selected")
    if spec.recurrence == "once":
        if not spec.date:
            raise ValueError("one-off alarm needs a date")
        _dt.date.fromisoformat(spec.date)  # raises ValueError on a bad date


def _resolve_local(
    year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo
) -> float:
    """UTC epoch for a local wall time, applying the DST edge policy.

    Normal and fall-back-ambiguous times resolve immediately via ``fold=0``
    (the earlier instant, per policy). A spring-forward gap — where the
    round-trip through UTC does not reproduce the requested wall time — is
    stepped forward a minute at a time to the first valid instant after the
    gap. The scan is bounded well past any real transition (max seen is one
    hour) and falls back to a plain localisation rather than looping.
    """
    naive = _dt.datetime(year, month, day, hour, minute)
    for _ in range(0, 24 * 60):
        aware = naive.replace(tzinfo=tz, fold=0)
        roundtrip = aware.astimezone(_dt.timezone.utc).astimezone(tz)
        if roundtrip.replace(tzinfo=None) == naive:
            return aware.timestamp()
        naive += _dt.timedelta(minutes=1)
    return _dt.datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def next_fire(spec: AlarmSpec, after_utc: float) -> float | None:
    """The earliest UTC epoch this alarm fires strictly after ``after_utc``.

    ``None`` when the alarm is disabled or spent (a one-off whose single
    instant is not after ``after_utc``, or a weekly alarm with no days).

    Strictly-after is deliberate: the scheduler passes the just-handled
    occurrence's own time to advance a recurring alarm to the next calendar
    occurrence, and passes ``now`` to skip a long backlog straight to the
    next future occurrence.
    """
    if not spec.enabled:
        return None
    tz = ZoneInfo(spec.tz)

    if spec.recurrence == "once":
        if not spec.date:
            return None
        d = _dt.date.fromisoformat(spec.date)
        fire = _resolve_local(d.year, d.month, d.day, spec.hour, spec.minute, tz)
        return fire if fire > after_utc else None

    start = _dt.datetime.fromtimestamp(after_utc, tz).date()
    # 15 days spans a full week plus slack, so a weekly mask with any day set
    # is always hit; daily needs only today/tomorrow but the extra iterations
    # are free.
    for offset in range(0, 15):
        day = start + _dt.timedelta(days=offset)
        if spec.recurrence == "weekly" and not (spec.weekday_mask >> day.weekday()) & 1:
            continue
        fire = _resolve_local(day.year, day.month, day.day, spec.hour, spec.minute, tz)
        if fire > after_utc:
            return fire
    return None


def resolve_once_date(
    hour: int, minute: int, tz: str, now_utc: float
) -> str | None:
    """Local ISO date of the next occurrence of hour:minute in ``tz``.

    Lets a bare "set an alarm for 7am" become a one-off for the next 7am
    (today if still ahead, else tomorrow) without the voice layer needing a
    clock: the caller resolves the date here, then stores a ``once`` alarm.
    """
    probe = AlarmSpec(
        id="_probe", device_id="_", hour=hour, minute=minute,
        recurrence="daily", tz=tz,
    )
    fire = next_fire(probe, now_utc)
    if fire is None:
        return None
    return _dt.datetime.fromtimestamp(fire, ZoneInfo(tz)).date().isoformat()


def occurrence_key(spec: AlarmSpec, scheduled_utc: float) -> str:
    """Stable id for one firing: the local calendar date it fired for.

    Feeds the per-occurrence synthetic timer id (``alarm:<id>:<date>``) that
    keeps a recurring alarm from being deduped against yesterday's ring by
    em_timers.AlarmSession, and the ``last_fired_occurrence`` guard that stops
    a fired occurrence re-firing on the next tick or a later restart.
    """
    local = _dt.datetime.fromtimestamp(scheduled_utc, ZoneInfo(spec.tz))
    return local.date().isoformat()


def within_grace(
    scheduled_utc: float, now_utc: float, grace_s: float = ALARM_LATE_GRACE_S
) -> bool:
    """Whether a due occurrence is recent enough to still fire late."""
    return 0.0 <= (now_utc - scheduled_utc) <= grace_s
