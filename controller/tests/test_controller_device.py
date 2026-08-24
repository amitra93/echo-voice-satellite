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


def test_button_handler_routes_hold_tap_mute_and_cancel(monkeypatch):
    events = []
    monkeypatch.setattr(em_controller.ha_sidechannels, "button_event", lambda *args: events.append(args))
    monkeypatch.setattr(em_controller.turn_engine, "cancel_voice_turn", lambda device_id: events.append(("cancel", device_id)))

    async def run():
        device = new_device(["button_hold"])
        device.button_single_tap_event = True
        device.button_multi_tap_ms = 0
        await em_controller.handle_button_event(device, {"clickType": 138, "down": True})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 900})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100, "muted": True})
        assert events[:2] == [("dev", "long", 900), ("dev", "single")]

        device.voice_lock = asyncio.Lock()
        await device.voice_lock.acquire()
        device.button_single_tap_event = False
        device.send_control = lambda message: asyncio.sleep(0, result=events.append(message))
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100})
        assert device.cancel_event.is_set()
        assert {"type": "speaker_flush"} in events
        device.voice_lock.release()

    asyncio.run(run())


def test_control_handler_rejects_bad_first_message_and_holds_unknown_device(monkeypatch):
    class WS:
        remote_address = ("192.0.2.9", 8767)

        def __init__(self, messages):
            self.incoming = iter(messages)
            self.sent = []
            self.closed = False

        async def recv(self):
            return next(self.incoming)

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def run():
        bad = WS([json.dumps({"type": "not_register"})])
        await em_controller.handle_control(bad)
        assert bad.closed and bad.sent == []

        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "register_new_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "notify_device_pending", lambda *args: asyncio.sleep(0))
        pending = WS([json.dumps({"type": "register", "device_id": "new", "ip": "192.0.2.9"})])
        await em_controller.handle_control(pending)
        assert pending.sent == [{"type": "pending"}]
        assert pending.closed

    asyncio.run(run())


def test_control_handler_processes_device_state_messages(monkeypatch):
    class WS(FakeWS):
        remote_address = ("192.0.2.10", 8767)

        def __init__(self, first, messages):
            self.sent = []
            self.first = first
            self.messages = iter(messages)
            self.closed = False

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            return self.first

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            self.closed = True

    async def never_wake(_device):
        await asyncio.Event().wait()

    async def no_op(*args, **kwargs):
        return None

    row = {"label": "Kitchen", "approved": 1, "firmware_ver": "v1"}
    config = {"owwOnDevice": "off", "startupVolume": 80,
              "owwModel": "hey_jarvis_v0.1", "bleProxyEnabled": True}
    messages = [
        json.dumps({"type": "ambient_light", "lux": 12}),
        json.dumps({"type": "mute_state", "muted": True}),
        json.dumps({"type": "volume_state", "level": 90}),
        json.dumps({"type": "stats", "cpuPct": 5, "ambientLux": 12,
                    "owwShadow": {"frames": 3, "drops": 1, "crossings": 1,
                                   "maxScore": 0.8, "threshold": 0.3}}),
        json.dumps({"type": "wifi_result", "ok": True, "ssid": "Home"}),
        json.dumps({"type": "playback_stats", "periods": 4, "underruns": 1,
                    "stats": {"min_depth": 2}}),
        json.dumps({"type": "oww_shadow_cross", "score": 0.7, "ageMs": 20}),
        json.dumps({"type": "oww_wake", "score": 0.8, "threshold": 0.3, "ageMs": 10}),
        json.dumps({"type": "ble_adverts", "adverts": [{"address": "x"}]}),
        json.dumps({"type": "wifi_scan_result", "networks": []}),
        json.dumps({"type": "log", "level": "info", "message": "hello"}),
        json.dumps({"type": "pong"}),
        json.dumps({"type": "unknown"}),
    ]

    async def run():
        old_devices = em_controller._devices
        em_controller._devices = {}
        em_controller.websockets.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
        ws = WS(json.dumps({"type": "register", "device_id": "dev",
                            "ip": "192.0.2.10", "capabilities": ["oww_shadow"]}), messages)
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: row)
        monkeypatch.setattr(em_controller.db, "get_turns", lambda *args: [])
        monkeypatch.setattr(em_controller.db, "get_effective_device_config", lambda *args: config)
        monkeypatch.setattr(em_controller.db, "get_device_config", lambda *args: {})
        monkeypatch.setattr(em_controller.db, "set_device_config", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "record_device_stats", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "touch_device_seen", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "bump_wake_counters", lambda *args, **kwargs: None)
        monkeypatch.setattr(em_controller.db, "upsert_device_seen", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "set_turn_playback", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "_push_event", no_op)
        monkeypatch.setattr(em_controller.api, "_push_log_event", no_op)
        monkeypatch.setattr(em_controller.api, "wifi_record_result", lambda *args: ({"pending": None}, False))
        monkeypatch.setattr(em_controller.api, "notify_device_connected", no_op)
        monkeypatch.setattr(em_controller.api, "notify_device_disconnected", no_op)
        monkeypatch.setattr(em_controller.em_sendspin, "unregister_device", no_op)
        monkeypatch.setattr(em_controller.em_player, "device_gone", lambda *args: None)
        monkeypatch.setattr(em_controller, "leds_off", no_op)
        monkeypatch.setattr(em_controller, "wake_word_listener", never_wake)
        monkeypatch.setattr(em_controller.ha_sidechannels, "ambient_light", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "mute_state", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "volume", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "capabilities", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "wake_model", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "ble_adverts", lambda *args: None)
        try:
            await em_controller.handle_control(ws)
            assert ws.closed is False
            assert any(json.loads(value)["type"] == "ack" for value in ws.sent)
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_data_handler_routes_valid_audio_and_vad_sentinel(monkeypatch):
    class DataWS:
        remote_address = ("192.0.2.11", 8767)

        def __init__(self, frames):
            self.frames = iter(frames)
            self.closed = False

        async def recv(self):
            return json.dumps({"type": "identify", "device_id": "dev"})

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration

    async def run():
        device = new_device()
        old_devices = em_controller._devices
        em_controller._devices = {"dev": device}
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        ws = DataWS([
            "not binary",
            b"\x01",
            b"\x02bad",
            bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"audio",
            bytes([em_controller.MIC_FRAME_TYPE, 0, 0, em_controller.VAD_END_TYPE]),
        ])
        try:
            await em_controller.handle_data(ws)
            assert device.mic_queue.get_nowait() == b"audio"
            assert device.mic_queue.get_nowait() == em_controller.turn_engine.VAD_SENTINEL_END
            assert device.data_ws is None
            assert not device.data_ready.is_set()
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_router_and_shell_handler_reject_invalid_sessions(monkeypatch):
    class WS:
        remote_address = ("192.0.2.12", 8767)

        def __init__(self, path="/"):
            self.path = path
            self.request = types.SimpleNamespace(path=path)
            self.closed = False

        async def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    async def run():
        missing = WS("/shell/")
        await em_controller.handle_shell(missing, "/shell/")
        assert missing.closed

        denied = WS("/shell/dev")
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=False))
        await em_controller.handle_shell(denied, "/shell/dev?pty=1")
        assert denied.closed

        no_pending = WS("/shell/dev")
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        em_controller._shell_pending.pop("dev", None)
        await em_controller.handle_shell(no_pending, "/shell/dev")
        assert no_pending.closed

        unknown = WS("/other")
        await em_controller._route(unknown, False)
        assert unknown.closed

    asyncio.run(run())


def test_shell_handler_bridges_programmatic_and_dashboard_sessions(monkeypatch):
    import aiohttp

    class DeviceWS:
        def __init__(self, messages=()):
            self.messages = iter(messages)
            self.sent = []
            self.closed = False

        async def send(self, value):
            self.sent.append(value)

        async def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

    class DashboardWS:
        def __init__(self, messages=()):
            self.messages = iter(messages)
            self.text = []
            self.binary = []

        async def send_str(self, value):
            self.text.append(value)

        async def send_bytes(self, value):
            self.binary.append(value)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

    async def run():
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        pending = asyncio.get_running_loop().create_future()
        em_controller._shell_pending["dev"] = pending
        programmatic = DeviceWS()
        await em_controller.handle_shell(programmatic, "/shell/dev?pty=1")
        assert pending.result() is programmatic
        assert not programmatic.closed
        em_controller._shell_pending.pop("dev", None)

        pending = asyncio.get_running_loop().create_future()
        dashboard = DashboardWS([
            types.SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"stdin"),
            types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="text"),
            types.SimpleNamespace(type=aiohttp.WSMsgType.CLOSE, data=b""),
        ])
        device = DeviceWS([b"stdout", "status"])
        em_controller._shell_pending["dev"] = pending
        em_controller._shell_dashboard["dev"] = dashboard
        await em_controller.handle_shell(device, "/shell/dev?pty=1")
        assert json.loads(dashboard.text[0]) == {"type": "shell_meta", "pty": True}
        assert dashboard.binary == [b"stdout"]
        assert "status" in dashboard.text
        assert device.sent == [b"stdin", b"text"]
        em_controller._shell_pending.pop("dev", None)
        em_controller._shell_dashboard.pop("dev", None)

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
