import asyncio
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("websockets", types.ModuleType("websockets"))
import em_api


def run(awaitable):
    return asyncio.run(awaitable)


class Request(dict):
    def __init__(self, body=None, *, match_info=None, query=None, headers=None,
                 remote=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers = headers or {}
        self.remote = remote
        self.rel_url = SimpleNamespace(query=self.query)
        self.update(match_info=self.match_info, query=self.query,
                    headers=self.headers, user={"role": "admin", "username": "admin"})
        self._body = body

    async def json(self):
        return self._body


def test_wifi_state_expires_pending_and_records_duplicate(monkeypatch):
    em_api._wifi_states.clear()
    now = 1000.0
    monkeypatch.setattr(em_api.time, "time", lambda: now)
    state = em_api.wifi_state("dev")
    state["pending"] = {"ssid": "Home", "started_at": now - em_api._WIFI_PENDING_TTL - 1}
    expired = em_api.wifi_state("dev")
    assert expired["pending"] is None
    assert expired["last_result"]["error"].startswith("no result")

    state, duplicate = em_api.wifi_record_result("dev", True, "Home", "")
    assert not duplicate and state["last_result"]["ok"]
    same, duplicate = em_api.wifi_record_result("dev", True, "Home", "")
    assert duplicate and same is state


def test_api_middleware_and_error_shapes(monkeypatch):
    async def handler(request):
        return "ok"

    monkeypatch.setattr(em_api, "INGRESS_ONLY", True)
    with pytest.raises(em_api.web.HTTPForbidden):
        run(em_api._ingress_only_middleware(Request(remote="192.0.2.1"), handler))
    assert run(em_api._ingress_only_middleware(
        Request(remote=em_api.INGRESS_GATEWAY_IP), handler)) == "ok"
    monkeypatch.setattr(em_api, "INGRESS_ONLY", False)
    assert run(em_api._ingress_only_middleware(Request(), handler)) == "ok"

    async def auth_error(_request):
        raise em_api.auth.AuthError("denied", "nope")

    response = run(em_api._error_middleware(Request(), auth_error))
    assert response.status == 401

    async def ordinary_error(_request):
        raise RuntimeError("boom")

    error_request = Request()
    error_request.method = "GET"
    error_request.path = "/broken"
    response = run(em_api._error_middleware(error_request, ordinary_error))
    assert response.status == 500

    async def http_error(_request):
        raise em_api.web.HTTPBadRequest()

    with pytest.raises(em_api.web.HTTPBadRequest):
        run(em_api._error_middleware(Request(), http_error))


def test_static_handlers_cover_missing_and_bundle_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "STATIC_DIR", tmp_path)
    assert run(em_api._serve_spa(Request())).status == 503
    assert run(em_api._serve_dashboard(Request())).status == 503

    (tmp_path / "index.html").write_text("<head>\nbody")
    (tmp_path / "dashboard.html").write_text("<head>static/dashboard.js")
    (tmp_path / "dashboard.js").write_text("bundle")
    spa = run(em_api._serve_spa(Request(headers={"X-Ingress-Path": "/ha/x/"})))
    dashboard = run(em_api._serve_dashboard(Request()))
    assert '<base href="/ha/x/">' in spa.text
    assert "dashboard.js?v=" in dashboard.text


def test_api_startup_helpers(monkeypatch):
    monkeypatch.setattr(em_api, "_PROCESS_START", em_api.time.time() - 10)
    monkeypatch.setattr(em_api.os, "times", lambda: (1.0, 2.0, 0, 0, 0))
    em_api.sample_cpu()
    monkeypatch.setattr(em_api.auth, "get_bootstrap_token", lambda: "token")
    response = run(em_api._get_setup_state(Request()))
    assert json.loads(response.text) == {"needs_setup": True}
    with pytest.raises(em_api.web.HTTPFound):
        run(em_api._redirect_root(Request()))


def test_request_and_state_helpers_cover_invalid_values(monkeypatch):
    class BadJSON(Request):
        async def json(self):
            raise ValueError("bad")

    with pytest.raises(em_api.web.HTTPBadRequest):
        run(em_api._json_body(BadJSON()))
    with pytest.raises(em_api.web.HTTPBadRequest):
        em_api._require_str({}, "name")
    assert em_api._require_str({"name": "  x "}, "name") == "x"

    assert em_api._stored_volume({"config": "{"}) is None
    assert em_api._stored_volume({"config": json.dumps({"startupVolume": "bad"})}) is None
    assert em_api._stored_volume({"config": json.dumps({"startupVolume": 127})}) == 1.0
    assert em_api._row_sections({}) == []
    assert em_api._row_sections({"config_sections": "{"}) == []
    assert em_api._dropped_keys({"b": 2}, {"a": 1, "b": 2}) == ["a"]


def test_merge_device_handles_offline_and_live_state(monkeypatch):
    row = {
        "device_id": "dev", "label": "Room", "approved": 1, "ip": "192.0.2.2",
        "firmware_ver": "v1", "firmware_previous": None, "first_seen": 1,
        "last_seen": 2, "config": json.dumps({"startupVolume": 80}),
        "config_sections": "[\"wakeword\"]", "token": "secret",
    }
    old = em_api._devices
    em_api._devices = {}
    try:
        offline = em_api._merge_device(row)
        assert not offline["connected"] and offline["volume"] is not None
        live = SimpleNamespace(
            speaking=True, muted=True, listening=True, thinking=False, stats={"ambientLux": 0},
            rtt_last_ms=12, volume=0.5, ble_proxy_enabled=True, oww_near_misses=3,
            capabilities=["ambient_light"], secure=True,
            oww_shadow_capable=True, oww_trigger_capable=True, audio_mix_capable=True,
            button_hold_capable=True,
        )
        em_api._devices = {"dev": live}
        merged = em_api._merge_device(row)
        assert merged["connected"] and merged["ambient_light_lux"] == 0
        assert merged["capabilities"] == ["ambient_light"]
    finally:
        em_api._devices = old


def test_patch_user_validates_and_changes_roles(monkeypatch):
    row = {"id": 2, "username": "sam", "role": "readonly", "ha_user_id": None}
    monkeypatch.setattr(em_api.db, "get_user_by_id", lambda user_id: row if user_id == 2 else None)
    monkeypatch.setattr(em_api.db, "set_user_role", lambda *_: None)
    monkeypatch.setattr(em_api.db, "admin_count", lambda: 2)
    with pytest.raises(em_api.web.HTTPBadRequest):
        run(em_api._patch_user.__wrapped__(Request({}, match_info={"id": "x"})))
    bad = run(em_api._patch_user.__wrapped__(Request({"role": "admin"}, match_info={"id": "x"})))
    assert bad.status == 400
    missing = run(em_api._patch_user.__wrapped__(Request({"role": "admin"}, match_info={"id": "3"})))
    assert missing.status == 404
    unchanged = run(em_api._patch_user.__wrapped__(Request({"role": "readonly"}, match_info={"id": "2"})))
    assert json.loads(unchanged.text)["changed"] is False
    changed = run(em_api._patch_user.__wrapped__(Request({"role": "admin"}, match_info={"id": "2"})))
    assert json.loads(changed.text)["changed"] is True

    admin = {"id": 1, "username": "admin", "role": "admin", "ha_user_id": None}
    monkeypatch.setattr(em_api.db, "get_user_by_id", lambda *_: admin)
    monkeypatch.setattr(em_api.db, "admin_count", lambda: 1)
    last = run(em_api._patch_user.__wrapped__(Request({"role": "readonly"}, match_info={"id": "1"})))
    assert last.status == 409


def test_ingress_and_auth_handlers_cover_fallbacks(monkeypatch):
    monkeypatch.setattr(em_api.em_ingressauth, "decide", lambda **_: None)
    assert run(em_api._post_ingress_login(Request())).status == 401
    monkeypatch.setattr(em_api.em_ingressauth, "decide", lambda **_: {"user_id": "ha"})
    monkeypatch.setattr(em_api.auth, "login_via_ingress", lambda _: asyncio.sleep(0, result=("tok", "admin")))
    assert json.loads(run(em_api._post_ingress_login(Request())).text)["via"] == "ingress"

    monkeypatch.setattr(em_api.auth, "login", lambda *_: asyncio.sleep(0, result=("tok", "admin")))
    assert json.loads(run(em_api._post_login(Request({"username": "u", "password": "p"}))).text)["token"] == "tok"
    monkeypatch.setattr(em_api.auth, "resolve_session", lambda *_: asyncio.sleep(0, result=None))
    assert run(em_api._post_logout(Request())).status == 200
    monkeypatch.setattr(em_api.auth, "resolve_session", lambda *_: asyncio.sleep(0, result={"token": "t"}))
    monkeypatch.setattr(em_api.auth, "logout", lambda *_: asyncio.sleep(0))
    assert run(em_api._post_logout(Request())).status == 200


def test_controller_helpers_and_link_auth(monkeypatch, caplog):
    import test_controller_device as controller_tests
    ctl = controller_tests.em_controller
    device = controller_tests.new_device()
    assert ctl.get_device("missing") is None
    ctl._devices[device.device_id] = device
    assert ctl.get_device("dev") is device
    assert ctl._make_leds(1, 2, 3)[-1]["id"] == ctl.NUM_LEDS - 1
    ctl._devices.clear()

    class Task:
        def __init__(self, exc=None, cancelled=False):
            self.exc, self.was_cancelled = exc, cancelled
        def cancelled(self): return self.was_cancelled
        def exception(self): return self.exc
        def get_name(self): return "test-task"

    with caplog.at_level(logging.ERROR):
        ctl._log_task_exception(Task(RuntimeError("boom")))
    assert "Unhandled exception" in caplog.text
    ctl._log_task_exception(Task(cancelled=True))
    ctl._log_task_exception(Task())

    class WS:
        request = SimpleNamespace(headers={})

    monkeypatch.setattr(ctl.db, "get_device_token", lambda *_: "secret")
    monkeypatch.setattr(ctl, "REQUIRE_DEVICE_TLS", False)
    assert run(ctl._link_auth_ok(WS(), "dev", False, "control"))
    WS.request.headers["X-EM-Token"] = "wrong"
    assert not run(ctl._link_auth_ok(WS(), "dev", False, "control"))
    WS.request.headers["X-EM-Token"] = "secret"
    assert run(ctl._link_auth_ok(WS(), "dev", True, "control"))


def test_controller_led_outcome_and_monitor_reconnect(monkeypatch):
    import test_controller_device as controller_tests
    ctl = controller_tests.em_controller
    device = controller_tests.new_device(["led_anim"])
    sent = []
    device.send_led_anim = lambda value: asyncio.sleep(0, result=sent.append(value))
    device.last_turn_outcome = "ok"
    run(ctl._leds_turn_end(device))
    assert sent == [{"pattern": "off"}]
    device.last_turn_outcome = "no_speech"
    device.led_scene["nospeech_anim"] = {"pattern": "pulse"}
    run(ctl._leds_turn_end(device))
    assert sent[-1] == {"pattern": "pulse"}
    device.barge_detected = True
    device.last_turn_outcome = "no_speech"
    run(ctl._leds_turn_end(device))
    assert sent[-1] == {"pattern": "off"}

    async def reconnect():
        ctl._devices["dev"] = device
        old_api_devices = em_api._devices
        em_api._devices = {"dev": device}
        monkeypatch.setattr(ctl.db, "get_device", lambda *_: {"firmware_ver": "v2"})
        original_sleep = ctl.asyncio.sleep
        monkeypatch.setattr(ctl.asyncio, "sleep", lambda *_: original_sleep(0))
        assert await em_api._monitor_reconnect("dev", "v2", previous_version="v1", timeout=1)
        ctl._devices.clear()
        em_api._devices = old_api_devices

    run(reconnect())


def test_controller_spinner_animation_paths(monkeypatch):
    import test_controller_device as controller_tests
    ctl = controller_tests.em_controller

    async def exercise_local_animation():
        device = controller_tests.new_device(["led_anim"])
        calls = []
        device.send_led_anim = lambda value: asyncio.sleep(0, result=calls.append(value))
        stop = asyncio.Event()
        stop.set()
        await ctl.leds_spin_green(device, stop)
        assert calls == [device.led_scene["spin_anim"], {"pattern": "off"}]

    run(exercise_local_animation())

    async def exercise_legacy_animation():
        device = controller_tests.new_device()
        calls = []
        async def set_leds(frame):
            calls.append(frame)
        device.set_leds = set_leds
        original_sleep = ctl.asyncio.sleep
        attempts = 0

        async def stop_after_frame(_delay):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.CancelledError()
            await original_sleep(0)

        monkeypatch.setattr(ctl.asyncio, "sleep", stop_after_frame)
        await ctl.leds_spin_green(device, asyncio.Event())
        assert len(calls) == 2

    run(exercise_legacy_animation())


def test_controller_spinner_handles_cancellation(monkeypatch):
    import test_controller_device as controller_tests
    ctl = controller_tests.em_controller
    device = controller_tests.new_device(["led_anim"])
    calls = []

    async def cancelled(value):
        calls.append(value)
        if len(calls) == 1:
            raise asyncio.CancelledError()

    device.send_led_anim = cancelled
    run(ctl.leds_spin_green(device, asyncio.Event()))
    assert calls == [device.led_scene["spin_anim"], {"pattern": "off"}]


def test_api_proc_helpers_parse_expected_lines(tmp_path, monkeypatch):
    import builtins

    path = tmp_path / "line"
    path.write_text(" value \nignored\n")
    assert em_api._read_first_line(str(path)) == "value"

    class ProcFile:
        def __enter__(self):
            return iter(("MemTotal: 1024 kB\n", "Bad value\n", "MemFree: 512 kB\n"))
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(builtins, "open", lambda path: ProcFile())
    assert em_api._proc_meminfo() == {"MemTotal": 1.0, "MemFree": 0.5}
