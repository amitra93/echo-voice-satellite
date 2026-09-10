import asyncio
import inspect
import time as time_module

import pytest

import em_timer_alarm
import em_timers


def _event(timer_id):
    return {
        "event": "finished", "timer_id": timer_id, "name": timer_id,
        "total_seconds": 1, "seconds_left": 0, "is_active": False,
    }


def test_alarm_runner_plays_fifo_alarms_and_announces_each(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    session.apply(_event("pasta"))
    played = []
    states = []
    player_calls = []

    async def playback(device, pcm, cancel):
        played.append((device, pcm))

    async def interrupt(_device_id):
        player_calls.append("interrupt")
        return None

    async def resume(_device_id):
        player_calls.append("resume")
        return None

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", interrupt)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", resume)

    async def on_state(device_id, firing):
        states.append((device_id, firing))

    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: "device", playback, on_state=on_state, max_ring_s=0,
    )
    runner._sound_cache = b"pcm"

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())

    assert [pcm for _device, pcm in played] == [b"pcm", b"pcm"]
    assert states == [("device", True), ("device", False)]
    assert player_calls == ["interrupt", "resume"]
    assert session.current is None


def test_alarm_runner_stop_cancels_playback_task(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    started = asyncio.Event()
    controls = []

    async def playback(_device, _pcm, _cancel):
        started.set()
        await asyncio.sleep(100)

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    class FakeDevice:
        async def send_control(self, message):
            controls.append(message)
            return None

    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: FakeDevice(), playback, max_ring_s=120,
    )
    runner._sound_cache = b"pcm"

    async def run():
        runner.start("device", session)
        await started.wait()
        assert await runner.stop("device")
        assert not await runner.stop("device")

    asyncio.run(run())
    assert controls == [{"type": "speaker_flush"}]


def test_alarm_runner_stop_cancels_even_when_flush_fails(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    started = asyncio.Event()

    async def playback(_device, _pcm, _cancel):
        started.set()
        await asyncio.sleep(100)

    class GoneDevice:
        async def send_control(self, _message):
            raise ConnectionError("control closed")

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: GoneDevice(), playback, max_ring_s=120,
    )
    runner._sound_cache = b"pcm"

    async def run():
        runner.start("device", session)
        await started.wait()
        assert await runner.stop("device")
        assert not runner.is_running("device")

    asyncio.run(run())


def test_alarm_runner_discards_undeliverable_alarm_without_touching_music(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    calls = []

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", lambda *_: calls.append("interrupt"))
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", lambda *_: calls.append("resume"))
    runner = em_timer_alarm.TimerAlarmRunner(lambda _device_id: None, _unused_playback)

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())
    assert session.current is None
    assert calls == []


def test_alarm_runner_missing_sound_discards_alarm_without_playback(monkeypatch, tmp_path):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    session.apply(_event("pasta"))
    calls = []
    states = []
    snapshots = []
    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", lambda *_: calls.append("interrupt"))
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", lambda *_: calls.append("resume"))
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: object(), _unused_playback,
        on_state=lambda device_id, firing: asyncio.sleep(0, result=states.append((device_id, firing))),
        on_changed=lambda _device_id: asyncio.sleep(0, result=snapshots.append(session.snapshot())),
        sound_file=str(tmp_path / "missing.flac"),
    )

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())
    assert session.current is None
    assert session.queue == []
    assert calls == []
    assert states == []
    assert snapshots == [{"state": "idle", "current": None, "queue": []}]


async def _noop(_device_id):
    return None


async def _unused_playback(*_args):
    raise AssertionError("undeliverable alarm must not start playback")


def test_em_timer_alarm_never_imports_wall_clock_modules():
    """Pins the structural property that makes ring timing DST-safe in the
    first place: em_timer_alarm.py has no `time`/`datetime` import to be
    tempted to read `time.time()`/`datetime.now()` from at all. Its only
    clock is `asyncio.get_running_loop().time()` (CLOCK_MONOTONIC), which a
    system wall-clock adjustment — DST or an NTP correction — never moves.
    See docs/timer-validation.md's automated coverage section.
    """
    source = inspect.getsource(em_timer_alarm)
    assert "import time" not in source
    assert "import datetime" not in source
    assert "from datetime" not in source


@pytest.mark.parametrize("jump_seconds", [3600, -3600])
def test_alarm_ring_duration_is_immune_to_a_wall_clock_dst_jump(monkeypatch, jump_seconds):
    """max_ring_s=0 means the deadline is already met by the time the very
    first burst returns, so — absent any wall-clock influence — the alarm
    dismisses after exactly one burst (the same baseline
    test_alarm_runner_plays_fifo_alarms_and_announces_each already relies
    on). Jumping time.time() by a full DST hour, in either direction, mid-
    ring must not change that: if the safety timeout were ever computed
    against time.time() instead of the loop's monotonic clock, springing
    forward would fire it early and falling back would delay it — either
    would change the burst count observed here.
    """
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    bursts = 0

    async def playback(_device, _pcm, _cancel):
        nonlocal bursts
        bursts += 1
        real_time = time_module.time
        monkeypatch.setattr(time_module, "time", lambda: real_time() + jump_seconds)

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: "device", playback, max_ring_s=0,
    )
    runner._sound_cache = b"pcm"

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())

    assert bursts == 1
    assert session.current is None


def test_unanswered_ring_timeout_notifies_so_consumers_retire(monkeypatch):
    """Pasta, 18:35: a 120s unanswered ring cleared `current` via
    timeout_current() with no timer.alarm push, so HACS presence and the
    card showed "ringing" for an hour against an idle controller — and a
    later dismiss correctly reported false while the card never cleared.
    The timeout must publish like every other session mutation."""
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    changed = []

    async def playback(_device, _pcm, _cancel):
        pass

    async def on_changed(device_id):
        changed.append(device_id)

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: "device", playback,
        on_changed=on_changed, max_ring_s=0,
    )
    runner._sound_cache = b"pcm"

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())

    assert session.current is None
    assert changed == ["device"]


def test_undeliverable_alarm_notifies_instead_of_sticking_ringing(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    changed = []

    async def on_changed(device_id):
        changed.append(device_id)

    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: None, _unused_playback, on_changed=on_changed,
    )

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())

    assert session.current is None
    assert changed == ["device"]


def test_each_queued_alarm_receives_its_own_ring_deadline(monkeypatch):
    session = em_timers.AlarmSession()
    session.apply(_event("pizza"))
    session.apply(_event("pasta"))
    played = []
    currents = []
    clock_values = iter((0.0, 0.5, 1.0, 1.0, 1.5, 2.0))

    async def playback(_device, _pcm, _cancel):
        played.append(session.current.timer_id)

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    monkeypatch.setattr(em_timers, "BURST_GAP_S", 0)
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: "device", playback, max_ring_s=1.0,
        on_current=lambda _device_id: asyncio.sleep(
            0, result=currents.append(session.current.timer_id)
        ),
    )
    runner._sound_cache = b"pcm"
    runner._clock = lambda: next(clock_values)

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())

    assert played == ["pizza", "pizza", "pasta", "pasta"]
    assert currents == ["pizza", "pasta"]
    assert session.current is None
