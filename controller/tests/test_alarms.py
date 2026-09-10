import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import em_alarms
from em_alarms import AlarmSpec


NY = "America/New_York"


def _epoch(y, mo, d, hh, mm, tz, fold=0):
    return dt.datetime(y, mo, d, hh, mm, tzinfo=ZoneInfo(tz), fold=fold).timestamp()


def spec(**kw):
    base = dict(
        id="a1", device_id="dev", hour=7, minute=0,
        recurrence="daily", tz=NY, enabled=True,
    )
    base.update(kw)
    return AlarmSpec(**base)


# ── daily ────────────────────────────────────────────────────────────────

def test_daily_picks_today_when_time_still_ahead():
    after = _epoch(2026, 6, 1, 6, 0, NY)          # 06:00, before 07:00
    assert em_alarms.next_fire(spec(), after) == _epoch(2026, 6, 1, 7, 0, NY)


def test_daily_rolls_to_tomorrow_once_past():
    after = _epoch(2026, 6, 1, 8, 0, NY)          # 08:00, after 07:00
    assert em_alarms.next_fire(spec(), after) == _epoch(2026, 6, 2, 7, 0, NY)


def test_daily_is_strictly_after_so_advance_moves_to_next_day():
    # The scheduler passes the just-handled occurrence's own time to advance.
    handled = _epoch(2026, 6, 1, 7, 0, NY)
    assert em_alarms.next_fire(spec(), handled) == _epoch(2026, 6, 2, 7, 0, NY)


# ── weekly ───────────────────────────────────────────────────────────────

def test_weekly_weekday_mask_skips_the_weekend():
    # 2026-06-05 is a Friday; Sat/Sun are skipped to Monday 2026-06-08.
    after = _epoch(2026, 6, 5, 8, 0, NY)
    s = spec(recurrence="weekly", weekday_mask=em_alarms.WEEKDAYS_MASK)
    assert em_alarms.next_fire(s, after) == _epoch(2026, 6, 8, 7, 0, NY)


def test_weekly_empty_mask_never_fires():
    after = _epoch(2026, 6, 1, 6, 0, NY)
    assert em_alarms.next_fire(spec(recurrence="weekly", weekday_mask=0), after) is None


# ── once ─────────────────────────────────────────────────────────────────

def test_once_future_fires_and_past_returns_none():
    s = spec(recurrence="once", date="2026-06-02")
    before = _epoch(2026, 6, 1, 6, 0, NY)
    after = _epoch(2026, 6, 3, 6, 0, NY)
    assert em_alarms.next_fire(s, before) == _epoch(2026, 6, 2, 7, 0, NY)
    assert em_alarms.next_fire(s, after) is None


# ── enabled flag ───────────────────────────────────────────────────────────

def test_disabled_never_fires():
    after = _epoch(2026, 6, 1, 6, 0, NY)
    assert em_alarms.next_fire(spec(enabled=False), after) is None


# ── DST correctness ────────────────────────────────────────────────────────

def test_dst_spring_forward_gap_resolves_to_first_valid_instant():
    # US DST 2026 begins Sun 2026-03-08: 02:00 -> 03:00, so 02:30 does not
    # exist. Policy: fire at the first valid instant after the gap = 03:00.
    s = spec(hour=2, minute=30)
    after = _epoch(2026, 3, 8, 0, 0, NY)
    got = em_alarms.next_fire(s, after)
    assert got == _epoch(2026, 3, 8, 3, 0, NY)


def test_dst_fall_back_ambiguous_resolves_to_earlier():
    # US DST 2026 ends Sun 2026-11-01: 02:00 -> 01:00, so 01:30 occurs twice.
    # Policy: the earlier (fold=0, still EDT).
    s = spec(hour=1, minute=30)
    after = _epoch(2026, 11, 1, 0, 0, NY)
    got = em_alarms.next_fire(s, after)
    assert got == _epoch(2026, 11, 1, 1, 30, NY, fold=0)


def test_wall_time_preserved_across_a_dst_transition():
    # A 07:00 daily alarm stays 07:00 local across spring-forward, which means
    # its UTC offset changes by an hour (EST -05:00 -> EDT -04:00).
    before_dst = em_alarms.next_fire(spec(), _epoch(2026, 3, 7, 8, 0, NY))
    after_dst = em_alarms.next_fire(spec(), _epoch(2026, 3, 8, 8, 0, NY))
    assert before_dst == _epoch(2026, 3, 8, 7, 0, NY)   # EDT day
    assert after_dst == _epoch(2026, 3, 9, 7, 0, NY)    # still 07:00 local
    # And the UTC wall clocks differ from a naive +24h assumption:
    assert dt.datetime.fromtimestamp(before_dst, ZoneInfo(NY)).hour == 7
    assert dt.datetime.fromtimestamp(after_dst, ZoneInfo(NY)).hour == 7


# ── occurrence key & grace ─────────────────────────────────────────────────

def test_resolve_once_date_picks_today_or_tomorrow():
    # 06:00 local, alarm for 07:00 -> today.
    assert em_alarms.resolve_once_date(7, 0, NY, _epoch(2026, 6, 1, 6, 0, NY)) == "2026-06-01"
    # 08:00 local, alarm for 07:00 -> tomorrow.
    assert em_alarms.resolve_once_date(7, 0, NY, _epoch(2026, 6, 1, 8, 0, NY)) == "2026-06-02"


def test_occurrence_key_is_the_local_fire_date():
    fire = _epoch(2026, 6, 2, 7, 0, NY)
    assert em_alarms.occurrence_key(spec(), fire) == "2026-06-02"


def test_within_grace_window():
    assert em_alarms.within_grace(1000.0, 1000.0, 600.0) is True
    assert em_alarms.within_grace(1000.0, 1599.0, 600.0) is True
    assert em_alarms.within_grace(1000.0, 1601.0, 600.0) is False
    # A future scheduled time is not "late" at all.
    assert em_alarms.within_grace(2000.0, 1000.0, 600.0) is False


# ── validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    dict(hour=24),
    dict(minute=60),
    dict(recurrence="hourly"),
    dict(tz="Not/AZone"),
    dict(recurrence="weekly", weekday_mask=0),
    dict(recurrence="once", date=None),
    dict(recurrence="once", date="2026-13-40"),
])
def test_validate_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        em_alarms.validate(spec(**bad))


def test_validate_accepts_a_good_spec():
    em_alarms.validate(spec())
    em_alarms.validate(spec(recurrence="weekly", weekday_mask=em_alarms.WEEKENDS_MASK))
    em_alarms.validate(spec(recurrence="once", date="2026-06-02"))
