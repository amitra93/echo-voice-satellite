import asyncio

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

    async def playback(_device, _pcm, _cancel):
        started.set()
        await asyncio.sleep(100)

    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", _noop)
    class FakeDevice:
        async def send_control(self, _message):
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
    calls = []
    monkeypatch.setattr(em_timer_alarm.em_player, "interrupt", lambda *_: calls.append("interrupt"))
    monkeypatch.setattr(em_timer_alarm.em_player, "resume_interrupted", lambda *_: calls.append("resume"))
    runner = em_timer_alarm.TimerAlarmRunner(
        lambda _device_id: object(), _unused_playback, sound_file=str(tmp_path / "missing.flac"),
    )

    async def run():
        runner.start("device", session)
        await asyncio.wait_for(runner._tasks["device"], timeout=1)

    asyncio.run(run())
    assert session.current is None
    assert calls == []


async def _noop(_device_id):
    return None


async def _unused_playback(*_args):
    raise AssertionError("undeliverable alarm must not start playback")
