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


def test_finished_timers_are_current_then_fifo():
    session = em_timers.AlarmSession()
    assert session.apply(event("started", "pizza")).accepted
    assert session.apply(event("finished", "pizza", seconds_left=0)).alarm_changed
    assert session.apply(event("finished", "pasta", seconds_left=0)).alarm_changed
    assert session.apply(event("finished", "tea", seconds_left=0)).alarm_changed

    assert session.current.timer_id == "pizza"
    assert [timer.timer_id for timer in session.queue] == ["pasta", "tea"]
    assert session.dismiss_current().timer_id == "pizza"
    assert session.current.timer_id == "pasta"
    assert session.dismiss_current().timer_id == "pasta"
    assert session.current.timer_id == "tea"


def test_identical_event_is_idempotent_but_changed_update_is_not():
    session = em_timers.AlarmSession()
    started = event("started", "pizza")
    assert session.apply(started).accepted
    assert session.apply(started).duplicate

    update = event("updated", "pizza", seconds_left=30)
    assert session.apply(update).accepted
    assert session.apply(update).duplicate
    assert session.running["pizza"].seconds_left == 30


def test_cancelled_timer_is_removed_from_running_and_alarm_queue():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    session.apply(event("finished", "pizza", seconds_left=0))
    session.apply(event("finished", "pasta", seconds_left=0))
    transition = session.apply(event("cancelled", "pasta", seconds_left=0, is_active=False))

    assert transition.alarm_changed
    assert session.current.timer_id == "pizza"
    assert session.queue == []


def test_dismiss_all_and_disconnect_do_not_delete_running_timers():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    session.apply(event("finished", "tea", seconds_left=0))
    session.apply(event("finished", "pasta", seconds_left=0))

    assert [timer.timer_id for timer in session.dismiss_all()] == ["tea", "pasta"]
    assert "pizza" in session.running
    session.apply(event("finished", "coffee", seconds_left=0))
    assert [timer.timer_id for timer in session.disconnect()] == ["coffee"]
    assert "pizza" in session.running
    assert session.snapshot()["state"] == "idle"


def test_timeout_and_delivery_failure_advance_to_the_next_alarm():
    session = em_timers.AlarmSession()
    session.apply(event("finished", "pizza", seconds_left=0))
    session.apply(event("finished", "pasta", seconds_left=0))

    assert session.timeout_current().timer_id == "pizza"
    assert session.current.timer_id == "pasta"
    assert session.delivery_failed().timer_id == "pasta"
    assert session.current is None


def test_late_finished_event_cannot_resurrect_a_cancelled_timer():
    session = em_timers.AlarmSession()
    session.apply(event("started", "pizza"))
    session.apply(event("cancelled", "pizza", seconds_left=0, is_active=False))

    transition = session.apply(event("finished", "pizza", seconds_left=0, is_active=False))

    assert transition.duplicate
    assert session.current is None
    assert session.queue == []


def test_alarm_speech_threshold_has_absolute_and_noise_relative_floor():
    assert not em_timers.alarm_should_capture(0.014, 0.001)
    assert em_timers.alarm_should_capture(0.015, 0.001)
    assert not em_timers.alarm_should_capture(0.024, 0.01)
    assert em_timers.alarm_should_capture(0.025, 0.01)


def test_alarm_waits_for_chime_tail_before_listening():
    assert em_timers.ALARM_LISTEN_SETTLE_S > 0
    assert em_timers.ALARM_LISTEN_SETTLE_S < em_timers.BURST_GAP_S
