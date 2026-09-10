import datetime as dt
from zoneinfo import ZoneInfo

import asyncio

import em_alarms
from em_alarms import AlarmSpec
from em_alarm_scheduler import AlarmScheduler


NY = "America/New_York"


def _epoch(y, mo, d, hh, mm, tz=NY):
    return dt.datetime(y, mo, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def spec(**kw):
    base = dict(id="a1", device_id="dev", hour=7, minute=0,
                recurrence="daily", tz=NY, enabled=True)
    base.update(kw)
    return AlarmSpec(**base)


class Harness:
    def __init__(self, loaded, now):
        self.loaded = loaded
        self.now = now
        self.persisted = []      # (id, next_fire_utc, last_fired)
        self.disabled = []
        self.fired = []          # (device_id, timer_id, label)
        self.fire_outcome = "ringing"
        self.sched = AlarmScheduler(
            load=lambda: self.loaded,
            persist=lambda i, n, l: self.persisted.append((i, n, l)),
            disable=lambda i: self.disabled.append(i),
            fire=self._fire,
            now=lambda: self.now,
        )

    async def _fire(self, device_id, timer_id, label):
        self.fired.append((device_id, timer_id, label))
        return self.fire_outcome


def test_due_within_grace_fires_and_advances_to_next_day():
    nf = _epoch(2026, 6, 1, 7, 0)
    h = Harness([(spec(), nf, None)], now=nf + 5)   # 5s late
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == [("dev", "alarm:a1:2026-06-01", None)]
    assert h.persisted == [("a1", _epoch(2026, 6, 2, 7, 0), "2026-06-01")]
    assert h.disabled == []


def test_not_due_does_nothing():
    nf = _epoch(2026, 6, 1, 7, 0)
    h = Harness([(spec(), nf, None)], now=nf - 60)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == [] and h.persisted == []


def test_already_fired_occurrence_never_double_fires():
    nf = _epoch(2026, 6, 1, 7, 0)
    h = Harness([(spec(), nf, "2026-06-01")], now=nf + 5)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == []
    # Still advances defensively so a stuck next_fire can't wedge.
    assert h.persisted == [("a1", _epoch(2026, 6, 2, 7, 0), "2026-06-01")]


def test_stale_past_grace_is_skipped_and_jumps_to_future():
    nf = _epoch(2026, 6, 1, 7, 0)
    # Controller was down for hours; now it is 2026-06-03 09:00.
    now = _epoch(2026, 6, 3, 9, 0)
    h = Harness([(spec(), nf, None)], now=now)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == []
    # floor = now, so it jumps straight to the next future occurrence.
    assert h.persisted == [("a1", _epoch(2026, 6, 4, 7, 0), "2026-06-01")]


def test_one_off_fires_then_disables():
    nf = _epoch(2026, 6, 2, 7, 0)
    s = spec(recurrence="once", date="2026-06-02")
    h = Harness([(s, nf, None)], now=nf + 5)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == [("dev", "alarm:a1:2026-06-02", None)]
    assert h.disabled == ["a1"]
    assert h.persisted == [("a1", None, "2026-06-02")]


def test_one_off_past_grace_disables_without_firing():
    nf = _epoch(2026, 6, 2, 7, 0)
    s = spec(recurrence="once", date="2026-06-02")
    h = Harness([(s, nf, None)], now=nf + 3600)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == []
    assert h.disabled == ["a1"] and h.persisted == [("a1", None, "2026-06-02")]


def test_uncached_next_fire_is_computed_not_fired():
    now = _epoch(2026, 6, 1, 6, 0)
    h = Harness([(spec(), None, None)], now=now)
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == []
    assert h.persisted == [("a1", _epoch(2026, 6, 1, 7, 0), None)]


def test_muted_or_offline_outcome_still_advances():
    nf = _epoch(2026, 6, 1, 7, 0)
    h = Harness([(spec(), nf, None)], now=nf + 5)
    h.fire_outcome = "muted"
    asyncio.run(h.sched.reconcile_once())
    assert h.fired == [("dev", "alarm:a1:2026-06-01", None)]
    assert h.persisted == [("a1", _epoch(2026, 6, 2, 7, 0), "2026-06-01")]


def test_one_bad_alarm_does_not_stall_the_rest():
    nf = _epoch(2026, 6, 1, 7, 0)
    bad = spec(id="bad", tz="Not/AZone")   # next_fire will raise on this tz
    good = spec(id="good")
    h = Harness([(bad, nf, None), (good, nf, None)], now=nf + 5)
    asyncio.run(h.sched.reconcile_once())
    assert ("dev", "alarm:good:2026-06-01", None) in h.fired
