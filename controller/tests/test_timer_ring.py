import em_timer_ring
import em_timers


def event(name, timer_id, **values):
    return {
        "event": name,
        "timer_id": timer_id,
        "name": values.get("name", timer_id),
        "total_seconds": values.get("total_seconds", 60),
        "seconds_left": values.get("seconds_left", 60),
        "is_active": values.get("is_active", name != "cancelled"),
    }


def record(timer_id, total_seconds=60, seconds_left=60, is_active=True,
           received_at=100.0):
    return em_timers.TimerRecord(
        timer_id=timer_id,
        name=timer_id,
        total_seconds=total_seconds,
        seconds_left=seconds_left,
        is_active=is_active,
        received_at=received_at,
    )


# ─── first_running ───────────────────────────────────────────────────────

def test_first_running_on_empty_session_is_none():
    session = em_timers.AlarmSession()
    assert em_timer_ring.first_running(session) is None


def test_first_running_single_timer():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    timer = em_timer_ring.first_running(session)
    assert timer is not None
    assert timer.timer_id == "pizza"


def test_first_running_returns_earliest_started():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    session.apply(event("started", "pasta"))
    session.apply(event("updated", "pizza", seconds_left=30))
    # An `updated` on an already-running timer must not reorder it —
    # apply() only ever does self.running[timer_id] = timer.
    timer = em_timer_ring.first_running(session)
    assert timer.timer_id == "pizza"
    assert timer.seconds_left == 30


def test_first_running_after_first_timer_cancelled():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    session.apply(event("started", "pasta"))
    session.apply(event("cancelled", "pizza", seconds_left=0, is_active=False))

    timer = em_timer_ring.first_running(session)
    assert timer.timer_id == "pasta"


# ─── countdown_spec ──────────────────────────────────────────────────────

def test_countdown_spec_normal_running_case():
    timer = record("pizza", total_seconds=100, seconds_left=80, received_at=0.0)
    spec = em_timer_ring.countdown_spec(timer, now=20.0, color=(0, 200, 0))

    assert spec["pattern"] == "countdown"
    assert spec["colors"] == [[0, 200, 0]]
    # elapsed=20, seconds_left=80 -> (80-20)/100 = 0.6
    assert spec["fractionNow"] == 0.6
    assert spec["runningMps"] == 1.0 / 100
    assert spec["ttlSec"] > 0


def test_countdown_spec_paused_has_no_time_based_decay():
    timer = record("pizza", total_seconds=100, seconds_left=40,
                    is_active=False, received_at=0.0)
    spec = em_timer_ring.countdown_spec(timer, now=999.0, color=(1, 2, 3))

    assert spec["runningMps"] == 0.0
    # Paused: elapsed is forced to 0 regardless of how much wall time has
    # passed since received_at, so fractionNow reads straight off
    # seconds_left/total_seconds.
    assert spec["fractionNow"] == 0.4


def test_countdown_spec_none_when_received_at_missing():
    timer = record("pizza", received_at=None)
    assert em_timer_ring.countdown_spec(timer, now=10.0, color=(0, 0, 0)) is None


def test_countdown_spec_none_when_total_seconds_not_positive():
    timer = record("pizza", total_seconds=0, received_at=0.0)
    assert em_timer_ring.countdown_spec(timer, now=10.0, color=(0, 0, 0)) is None

    timer_negative = record("pizza", total_seconds=-5, received_at=0.0)
    assert em_timer_ring.countdown_spec(timer_negative, now=10.0, color=(0, 0, 0)) is None


def test_countdown_spec_clamps_seconds_left_above_total_to_one():
    # A timer extension may not adjust total_seconds, so seconds_left can
    # exceed it — the fraction must clamp to 1.0, not run above it.
    timer = record("pizza", total_seconds=60, seconds_left=300, received_at=0.0)
    spec = em_timer_ring.countdown_spec(timer, now=0.0, color=(0, 0, 0))
    assert spec["fractionNow"] == 1.0


def test_countdown_spec_clamps_negative_fraction_to_zero():
    # More elapsed time than seconds_left accounts for (e.g. a finished
    # event still in flight) must not run the fraction negative.
    timer = record("pizza", total_seconds=60, seconds_left=10, received_at=0.0)
    spec = em_timer_ring.countdown_spec(timer, now=100.0, color=(0, 0, 0))
    assert spec["fractionNow"] == 0.0
