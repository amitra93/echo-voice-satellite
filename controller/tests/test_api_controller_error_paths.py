import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("websockets", types.ModuleType("websockets"))
import em_api


def run(awaitable):
    return asyncio.run(awaitable)


def request(body=None, *, match_info=None, query=None):
    class Request(dict):
        async def json(self):
            return body

    result = Request()
    result.match_info = match_info or {}
    result.query = query or {}
    result.headers = {}
    result.remote = None
    result.update(match_info=result.match_info, query=result.query, headers=result.headers,
                  user={"role": "admin"})
    return result


class TransferWS:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        try:
            return next(self.replies)
        except StopIteration:
            raise asyncio.TimeoutError()


def transfer_live():
    return SimpleNamespace(device_id="dev")


def test_file_transfer_reports_shell_and_probe_failures(monkeypatch):
    live = transfer_live()
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *args: asyncio.sleep(0))

    async def no_shell(_live):
        raise RuntimeError("offline")

    monkeypatch.setattr(em_api, "_get_device_shell_ws", no_shell)
    result = run(em_api._stream_file_to_device(live, b"x", "/tmp/x"))
    assert (result.stage, result.ok) == ("shell", False)

    for replies, expected in [
        (["DECODER:none MD5:none __DETECT_DONE__"], "decoder"),
        (["DECODER:busybox MD5:none __DETECT_DONE__"], "md5tool"),
    ]:
        ws = TransferWS(replies)
        monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda _live, ws=ws: asyncio.sleep(0, result=ws))
        result = run(em_api._stream_file_to_device(
            live, b"x", "/tmp/x", require_verify=expected == "md5tool"))
        assert result.stage == expected


def test_file_transfer_handles_unverified_success_and_md5_results(monkeypatch):
    live = transfer_live()
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *args: asyncio.sleep(0))

    cases = [
        (["DECODER:python3 MD5:none __DETECT_DONE__", "TRANSFER_OK"], True, "unverified"),
        (["DECODER:busybox MD5:busybox __DETECT_DONE__", "TRANSFER_OK", "VERIFY_OK"], True, "ok"),
        (["DECODER:python MD5:plain __DETECT_DONE__", "TRANSFER_OK", "VERIFY_BAD: deadbeef"], False, "corrupt"),
    ]
    for replies, ok, stage in cases:
        ws = TransferWS(replies)
        monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda _live, ws=ws: asyncio.sleep(0, result=ws))
        result = run(em_api._stream_file_to_device(live, b"payload", "/tmp/file", mode="600"))
        assert bool(result) is ok
        assert result.stage == stage
        assert any("chmod 600" in message for message in ws.sent)


def test_api_turn_audio_media_and_config_error_branches(monkeypatch):
    assert run(em_api._get_device_turns.__wrapped__(request(
        match_info={"id": "dev"}, query={"limit": "bad"}))).status == 400

    monkeypatch.setattr(em_api.db, "get_device", lambda *_: None)
    assert run(em_api._get_turn_audio.__wrapped__(request(
        match_info={"id": "dev", "turn": "1", "kind": "bad"}))).status == 400
    assert run(em_api._get_turn_audio.__wrapped__(request(
        match_info={"id": "dev", "turn": "bad", "kind": "stt"}))).status == 400

    live = SimpleNamespace(
        sent=[],
        muted=False,
        capabilities=[],
        async_send_control=None,
    )

    async def send_control(message):
        live.sent.append(message)

    live.send_control = send_control
    old_devices = em_api._devices
    em_api._devices = {"dev": live}
    try:
        assert run(em_api._post_media_command.__wrapped__(request(
            {"command": "unknown"}, match_info={"id": "dev"}))).status == 400
        monkeypatch.setattr(em_api.em_player, "pause", lambda *_: (_ for _ in ()).throw(RuntimeError("busy")))
        assert run(em_api._post_media_command.__wrapped__(request(
            {"command": "pause"}, match_info={"id": "dev"}))).status == 409
        monkeypatch.setattr(em_api.em_player, "play", lambda *_: asyncio.sleep(0))
        assert run(em_api._post_media_command.__wrapped__(request(
            {"media_url": "https://example.test/a"}, match_info={"id": "dev"}))).status == 200
        assert run(em_api._post_media_command.__wrapped__(request(
            {"command": "mute_toggle"}, match_info={"id": "dev"}))).status == 200
        assert live.sent[-1] == {"type": "mute_toggle"}
    finally:
        em_api._devices = old_devices

    live = SimpleNamespace(
        device_id="dev", capabilities=["led_anim"], led_anim_capable=True,
        sent=[], oww_trigger_capable=False, oww_model_ready=True,
    )
    live.send_control = send_control
    monkeypatch.setattr(em_api, "_hold_back_oww_model", lambda _live, config: (config, None))
    monkeypatch.setattr(em_api.em_scenes, "resolve", lambda _config: {"listening_anim": {"pattern": "solid"}})
    monkeypatch.setattr(em_api.ha_sidechannels, "wake_model", lambda *_: None)
    config = {
        "owwThreshold": 0.4, "owwModel": "hey", "owwSpeexNs": True, "nsAsr": True,
        "saveUtterances": True, "saveWakeCaptures": True, "wakeCaptureSec": 2,
        "wakeNearMissFloor": 0.1, "bargeInEnabled": True, "bargeInThreshold": 0.1,
        "buttonSingleTapEvent": True, "buttonMultiTapMs": 300, "wakeArbitrationMs": 500,
        "owwOnDevice": "on", "eqBands": [], "eqLoudness": True, "ttsGainDb": 4,
        "limiterEnabled": True, "limiterThreshold": -1, "limiterRelease": 100,
        "bassGuardEnabled": True, "bassGuardDb": 2, "bleProxyEnabled": True,
    }
    run(em_api._apply_live_config("dev", live, config))
    assert live.oww_on_device == em_api.em_shadow.MODE_SHADOW
    assert live.limiter_enabled and live.bass_guard_enabled and live.ble_proxy_enabled


def test_device_config_handler_scoping_and_replace_guards(monkeypatch):
    row = {"device_id": "dev", "approved": 1}
    stored = {"owwThreshold": 0.5, "startupVolume": 80}
    calls = []
    monkeypatch.setattr(em_api.db, "get_device", lambda *_: row)
    monkeypatch.setattr(em_api.db, "get_device_config_sections", lambda *_: ["wakeword"])
    monkeypatch.setattr(em_api.db, "get_device_config", lambda *_: dict(stored))
    monkeypatch.setattr(em_api.db, "set_device_config_sections", lambda *args: calls.append(("sections", args)))
    monkeypatch.setattr(em_api.db, "set_device_config", lambda *args: calls.append(("config", args)))
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda *_: {"owwThreshold": 0.4})
    monkeypatch.setattr(em_api, "_push_event", lambda *_: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_apply_live_config", lambda *args: asyncio.sleep(0))

    monkeypatch.setattr(em_api.db, "get_device", lambda *_: None)
    assert run(em_api._post_device_config.__wrapped__(request(
        {}, match_info={"id": "missing"}))).status == 404
    monkeypatch.setattr(em_api.db, "get_device", lambda *_: row)
    assert run(em_api._post_device_config.__wrapped__(request(
        {"config_sections": "wakeword"}, match_info={"id": "dev"}))).status == 400
    assert run(em_api._post_device_config.__wrapped__(request(
        {"config_sections": ["unknown"]}, match_info={"id": "dev"}))).status == 400
    assert run(em_api._post_device_config.__wrapped__(request(
        {"owwThreshold": 0.4}, match_info={"id": "dev"}))).status == 409

    old_devices = em_api._devices
    em_api._devices = {}
    try:
        response = run(em_api._post_device_config.__wrapped__(request(
            {"use_global_config": True, "startupVolume": 80},
            match_info={"id": "dev"})))
        assert response.status == 200
        assert json.loads(response.text)["pushed"] is False
        response = run(em_api._post_device_config.__wrapped__(request(
            {"config_sections": [], "replace": True, "owwThreshold": 0.4},
            match_info={"id": "dev"})))
        assert response.status == 200
    finally:
        em_api._devices = old_devices


def test_api_successful_turn_audio_activity_and_media_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api.db, "get_device", lambda *_: {"label": "Living Room"})
    monkeypatch.setattr(em_api.db, "get_turns", lambda *_: [])
    monkeypatch.setattr(em_api.em_recordings, "filename", lambda *_: "turn.wav")
    path = tmp_path / "turn.wav"
    path.write_bytes(b"RIFF")
    monkeypatch.setattr(em_api.em_recordings, "resolve", lambda *_: path)
    response = run(em_api._get_turn_audio.__wrapped__(request(
        match_info={"id": "dev", "turn": "7", "kind": "stt"})))
    assert response.headers["Content-Type"] == "audio/wav"

    response = run(em_api._get_device_turns.__wrapped__(request(
        match_info={"id": "dev"}, query={"limit": "2", "since": "1.5"})))
    assert json.loads(response.text) == []
    assert run(em_api._get_device_activity.__wrapped__(request(
        match_info={"id": "dev"}, query={"days": "bad"}))).status == 400

    live = SimpleNamespace(sent=[], volume=0.0)

    async def send_control(message):
        live.sent.append(message)

    live.send_control = send_control
    old_devices = em_api._devices
    em_api._devices = {"dev": live}
    try:
        monkeypatch.setattr(em_api.em_player, "resume", lambda *_: asyncio.sleep(0))
        assert run(em_api._post_media_command.__wrapped__(request(
            {"command": "resume"}, match_info={"id": "dev"}))).status == 200
        monkeypatch.setattr(em_api.em_player, "stop", lambda *_: asyncio.sleep(0))
        assert run(em_api._post_media_command.__wrapped__(request(
            {"command": "stop"}, match_info={"id": "dev"}))).status == 200
        monkeypatch.setattr(em_api.ha_sidechannels, "volume", lambda *_: None)
        assert run(em_api._post_media_command.__wrapped__(request(
            {"volume": 2}, match_info={"id": "dev"}))).status == 200
    finally:
        em_api._devices = old_devices


def test_controller_connection_and_playback_error_paths(monkeypatch):
    import test_controller_device as controller_tests
    controller = controller_tests.em_controller
    device = controller_tests.new_device()

    async def run_controller():
        device.data_ws = None
        device.begin_data_stream()
        waits = []

        async def sleep(step):
            waits.append(step)
            device.data_ws = controller_tests.FakeWS()

        monkeypatch.setattr(controller.asyncio, "sleep", sleep)
        await device.send_data(b"reconnect")
        assert waits == [0.1]

        class Broken:
            async def send(self, _data):
                raise RuntimeError("gone")

        device.data_ws = Broken()
        await device.send_data(b"error")
        device.thinking = True
        await device._set_speaking(True)
        assert not device.thinking
        await device._set_speaking(True)  # idempotent edge

    run(run_controller())


def test_controller_approved_registration_initialises_and_cleans_up(monkeypatch):
    import test_controller_device as controller_tests
    controller = controller_tests.em_controller

    class WS:
        remote_address = ("192.0.2.20", 8767)

        def __init__(self):
            self.sent = []
            self.closed = False

        async def recv(self):
            return json.dumps({
                "type": "register", "device_id": "new", "ip": "192.0.2.20",
                "version": "v1", "capabilities": ["led_anim"],
                "ambient_light_status": {"reason": "no_chip"},
            })

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def noop(*_args, **_kwargs):
        return None

    async def reconcile(*_args):
        return None

    async def fake_wake(_device):
        await asyncio.sleep(3600)

    ws = WS()
    row = {"label": "Kitchen", "approved": 1}
    config = {
        "owwThreshold": 0.4, "owwModel": "hey", "startupVolume": 90,
        "eqBands": [0.0] * 8, "owwOnDevice": "off", "ledScene": "lcd",
    }
    old_devices = controller._devices
    controller._devices = {}
    try:
        monkeypatch.setattr(controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(controller.db, "get_config", lambda *_: "strict")
        monkeypatch.setattr(controller.db, "get_device", lambda *_: row)
        monkeypatch.setattr(controller.db, "get_effective_device_config", lambda *_: config)
        monkeypatch.setattr(controller.db, "get_turns", lambda *_: [])
        monkeypatch.setattr(controller.db, "upsert_device_seen", lambda *_: None)
        monkeypatch.setattr(controller.db, "log_device", lambda *_: None)
        monkeypatch.setattr(controller.db, "touch_device_seen", lambda *_: None)
        monkeypatch.setattr(controller.api, "reconcile_oww_assets", reconcile)
        monkeypatch.setattr(controller.api, "notify_device_connected", noop)
        monkeypatch.setattr(controller.api, "notify_device_disconnected", noop)
        monkeypatch.setattr(controller.api, "_push_event", noop)
        monkeypatch.setattr(controller, "_push_device_state", noop)
        monkeypatch.setattr(controller, "leds_off", noop)
        monkeypatch.setattr(controller, "wake_word_listener", fake_wake)
        monkeypatch.setattr(controller.ha_sidechannels, "capabilities", lambda *_: None)
        monkeypatch.setattr(controller.ha_sidechannels, "wake_model", lambda *_: None)
        monkeypatch.setattr(controller.em_scenes, "resolve", lambda *_: {"listening": []})
        run(controller.handle_control(ws))
        assert {message["type"] for message in ws.sent} >= {"ack", "config"}
        assert ws.closed is False
    finally:
        controller._devices = old_devices
