import asyncio
import json
import sys
import types
import math

import pytest


def _install_import_stubs():
    openwakeword = types.ModuleType("openwakeword")
    model = types.ModuleType("openwakeword.model")
    model.Model = type("FakeOWWModel", (), {})
    openwakeword.model = model
    sys.modules.setdefault("openwakeword", openwakeword)
    sys.modules.setdefault("openwakeword.model", model)

    zeroconf = types.ModuleType("zeroconf")
    zeroconf.ServiceInfo = type("ServiceInfo", (), {})
    zasync = types.ModuleType("zeroconf.asyncio")
    zasync.AsyncZeroconf = type("AsyncZeroconf", (), {})
    zeroconf.asyncio = zasync
    sys.modules.setdefault("zeroconf", zeroconf)
    sys.modules.setdefault("zeroconf.asyncio", zasync)

    websockets = types.ModuleType("websockets")
    websockets_async = types.ModuleType("websockets.asyncio")
    websockets_server = types.ModuleType("websockets.asyncio.server")
    websockets_server.ServerConnection = type("ServerConnection", (), {})
    websockets_async.server = websockets_server
    websockets.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
    websockets.asyncio = websockets_async
    sys.modules.setdefault("websockets", websockets)
    sys.modules.setdefault("websockets.asyncio", websockets_async)
    sys.modules.setdefault("websockets.asyncio.server", websockets_server)


_install_import_stubs()
import em_controller

# The controller has captured the classes it needs during import. Do not leave
# the synthetic package in sys.modules: later tests correctly treat the
# optional openwakeword dependency as absent.
sys.modules.pop("openwakeword.model", None)
sys.modules.pop("openwakeword", None)


class FakeWS:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def new_device(capabilities=None):
    return em_controller.Device("dev", "192.0.2.1", capabilities or [], FakeWS())


def test_capabilities_and_rtt_aggregation():
    device = new_device(["led_anim", "audio_mix", "button_hold", "oww_shadow", "oww_trigger"])
    assert device.led_anim_capable
    assert device.audio_mix_capable
    assert device.button_hold_capable
    assert device.oww_shadow_capable
    assert device.oww_trigger_capable
    empty = new_device()
    assert not empty.led_anim_capable and not empty.oww_trigger_capable

    assert device.drain_rtt() == {}
    device.record_rtt(50, False)
    device.record_rtt(250, True)
    device.record_rtt(300, False)
    result = device.drain_rtt()
    assert result == {
        "rttSumMs": 600,
        "rttSamples": 3,
        "rttMinMs": 50,
        "rttMaxMs": 300,
        "rttExcursions": 2,
        "rttExcursionsIdle": 1,
        "rttSamplesIdle": 2,
    }
    assert device.drain_rtt() == {}


def test_control_messages_and_listening_field():
    async def run():
        device = new_device()
        await device.set_leds([{"id": 1}], listening=True)
        await device.send_led_anim({"pattern": "off"})
        await device.ping()
        await device.mic_start()
        await device.mic_start_turn()
        await device.mic_stop()
        await device.beam_lock()
        await device.beam_unlock()
        await device.push_config(owwThreshold=0.4)
        messages = [json.loads(value) for value in device.control_ws.messages]
        assert messages[0] == {"type": "leds", "leds": [{"id": 1}], "listening": True}
        assert messages[-1] == {"type": "config", "owwThreshold": 0.4}
        await device.set_leds([], listening=None)
        assert "listening" not in json.loads(device.control_ws.messages[-1])

    asyncio.run(run())


def test_send_control_and_data_swallow_disconnects_and_spend_one_budget():
    class Broken:
        async def send(self, _message):
            raise RuntimeError("gone")

    async def run():
        device = new_device()
        device.control_ws = Broken()
        await device.send_control({"type": "x"})
        device.begin_data_stream()
        sleeps = []

        async def sleep(step):
            sleeps.append(step)

        original_sleep = em_controller.asyncio.sleep
        em_controller.asyncio.sleep = sleep
        try:
            await device.send_data(b"one")
            remaining = device._data_grace_left
            await device.send_data(b"two")
        finally:
            em_controller.asyncio.sleep = original_sleep
        assert sleeps
        assert math.isclose(remaining, 0, abs_tol=1e-9)
        assert math.isclose(device._data_grace_left, 0, abs_tol=1e-9)

    asyncio.run(run())


def test_send_data_sends_when_connected_and_is_busy(monkeypatch):
    async def run():
        device = new_device()
        data = FakeWS()
        device.data_ws = data
        await device.send_data(b"pcm")
        assert data.messages == [b"pcm"]
        monkeypatch.setattr(em_controller.em_player, "is_playing", lambda device_id: True)
        assert device.is_busy()
        device.speaking = True
        assert device.is_busy()

    asyncio.run(run())


def test_speaking_state_is_assigned_even_when_dashboard_push_fails(monkeypatch):
    pushes = []

    async def push(device_id):
        pushes.append(device_id)
        raise asyncio.CancelledError()

    monkeypatch.setattr(em_controller, "_push_device_state", push)

    async def run():
        device = new_device()
        await device._set_speaking(True)
        assert device.speaking is True
        await device._set_speaking(True)
        device.thinking = True
        await device._set_speaking(False)
        assert device.speaking is False
        assert pushes == [device, device]

    asyncio.run(run())


def test_dashboard_state_and_led_helpers(monkeypatch):
    async def run():
        device = new_device(["led_anim"])
        events = []
        monkeypatch.setattr(em_controller.api, "_push_event", lambda event: asyncio.sleep(0, result=events.append(event)))
        await em_controller._push_device_state(device)
        assert events[0]["state"]["connected"] is True
        assert len(em_controller._make_leds(1, 2, 3)) == em_controller.NUM_LEDS

        animations = []
        device.send_led_anim = lambda value: asyncio.sleep(0, result=animations.append(value))
        await em_controller.leds_off(device)
        await em_controller.leds_listening(device)
        device.last_turn_outcome = "no_speech"
        device.led_scene["nospeech_anim"] = {"pattern": "pulse"}
        await em_controller._leds_turn_end(device)
        assert animations[0] == {"pattern": "off"}
        assert animations[1] == device.led_scene["listening_anim"]
        assert animations[2] == {"pattern": "pulse"}
        assert device.last_turn_outcome is None

        legacy = new_device()
        frames = []
        legacy.set_leds = lambda *value, **kwargs: asyncio.sleep(0, result=frames.append((value, kwargs)))
        await em_controller.leds_off(legacy)
        await em_controller.leds_listening(legacy)
        assert len(frames) == 2
        assert frames[1][1] == {"listening": True}

    asyncio.run(run())


def test_legacy_spinner_stops_and_cleans_up():
    async def run():
        device = new_device()
        calls = []
        device.set_leds = lambda *value, **kwargs: asyncio.sleep(0, result=calls.append(value))
        stop = asyncio.Event()
        stop.set()
        await em_controller.leds_spin_green(device, stop)
        assert len(calls) == 1

    asyncio.run(run())


def test_stream_speaker_periods_padding_and_eos():
    async def run():
        device = new_device()
        frames = []
        device.send_data = lambda frame: asyncio.sleep(0, result=frames.append(frame))
        device._set_speaking = lambda value: asyncio.sleep(0)
        pcm = b"\x01" * (em_controller.SPEAKER_BYTES + 3)
        await device.stream_speaker(pcm)
        assert frames[0][0] == em_controller.SPEAKER_FRAME_TYPE
        assert len(frames[0]) == em_controller.SPEAKER_BYTES + 1
        assert len(frames[1]) == em_controller.SPEAKER_BYTES + 1
        assert frames[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])

    asyncio.run(run())


def test_stream_speaker_chunks_preserves_partial_data_and_reports_metrics():
    class EQ:
        def __init__(self):
            self.calls = []

        def process(self, pcm):
            self.calls.append(pcm)
            return pcm

    async def chunks():
        yield b"a" * 100
        yield b"b" * (em_controller.SPEAKER_BYTES - 100 + 5)

    async def run():
        device = new_device()
        frames = []
        device.send_data = lambda frame: asyncio.sleep(0, result=frames.append(frame))
        device._set_speaking = lambda value: asyncio.sleep(0)
        eq = EQ()
        total, eq_ms, first, send_ms = await device.stream_speaker_chunks(chunks(), eq)
        assert total == em_controller.SPEAKER_BYTES + 5
        assert eq.calls == [b"a" * 100, b"b" * (em_controller.SPEAKER_BYTES - 95)]
        assert first is not None and eq_ms >= 0 and send_ms >= 0
        assert len(frames) == 3
        assert frames[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])

    asyncio.run(run())


def test_handle_control_rejects_non_register_and_holds_unknown_device_pending(monkeypatch):
    class WS:
        remote_address = ("192.0.2.1", 1234)

        def __init__(self, message):
            self.message = message
            self.sent = []
            self.closed = False

        async def recv(self):
            return self.message

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self):
            self.closed = True

    async def run():
        bad = WS(json.dumps({"type": "ping"}))
        await em_controller.handle_control(bad)
        assert bad.closed

        pending = WS(json.dumps({"type": "register", "device_id": "new", "ip": "192.0.2.2"}))
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "register_new_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "notify_device_pending", lambda *args: asyncio.sleep(0))
        await em_controller.handle_control(pending)
        assert pending.sent == [{"type": "pending"}]
        assert pending.closed

    asyncio.run(run())
