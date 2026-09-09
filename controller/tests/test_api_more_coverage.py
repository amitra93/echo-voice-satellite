"""
Broad handler-level coverage for em_api.py's largest untested surface.

em_api.py is the single biggest coverage gap in the controller test suite
(53.4% at baseline) because most of its ~100 aiohttp route handlers had no
test calling them directly. This file follows the two patterns already
established elsewhere in the suite:

  - `tests/test_api_controller_branches.py`'s style: construct a lightweight
    fake `Request`-like object (dict-based, with `.match_info`/`.query`/
    `.headers`/`.json()`) and call a handler function directly — cheapest,
    used for the vast majority of tests here.
  - `tests/test_training_captures_api.py`'s style: boot a real aiohttp
    `TestServer`/`TestClient` against a small `web.Application` wired with
    just the routes under test, for handlers that need real multipart/
    websocket/streaming behaviour aiohttp itself provides.

`websockets` is stubbed before import (em_api imports it at module level but
the handlers under test do not need it), matching every other em_api test
file.
"""

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

if "websockets" not in sys.modules:
    _ws = types.ModuleType("websockets")
    _ws.WebSocketException = Exception
    sys.modules["websockets"] = _ws
    _wsa = types.ModuleType("websockets.asyncio")
    sys.modules["websockets.asyncio"] = _wsa
    _wss = types.ModuleType("websockets.asyncio.server")
    _wss.ServerConnection = object
    sys.modules["websockets.asyncio.server"] = _wss

from aiohttp import web  # noqa: E402

import em_api  # noqa: E402
import em_recordings  # noqa: E402


def run(awaitable):
    return asyncio.run(awaitable)


class Request(dict):
    """Same fake Request shape as test_api_controller_branches.py: enough
    of aiohttp's web.Request surface for handlers that read match_info,
    query, headers and a JSON body, without booting a real server."""

    def __init__(self, body=None, *, match_info=None, query=None, headers=None,
                remote=None, user=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers = headers or {}
        self.remote = remote
        self.rel_url = SimpleNamespace(query=self.query)
        self.update(match_info=self.match_info, query=self.query,
                    headers=self.headers,
                    user=user or {"role": "admin", "username": "admin", "id": 1})
        self._body = body

    async def json(self):
        return self._body


def _as_admin():
    return {"role": "admin", "username": "admin", "id": 1}


def _as_readonly():
    return {"role": "readonly", "username": "ro", "id": 2}


# ─── create_app / init / create_runner ─────────────────────────────────────

def test_create_app_registers_every_route_without_touching_the_network():
    """
    create_app() just builds an aiohttp Application and registers routes —
    no I/O, so it is fully exercised by simply calling it. This alone is
    ~130 lines of route registration that no other test ever executed.
    """
    em_api.init({}, {}, {})
    app = run(em_api.create_app())
    assert isinstance(app, web.Application)
    paths = {route.resource.canonical for route in app.router.routes()
            if route.resource is not None}
    # A representative sample spanning most of the sections create_app wires
    # up — devices, training captures, releases, system, provisioning,
    # oww models, and the two websocket endpoints.
    for expected in (
        "/", "/api/setup", "/api/auth/login", "/api/devices",
        "/api/devices/{id}/config", "/api/training_captures",
        "/api/releases/latest", "/api/releases/controller",
        "/api/global/config", "/api/support/bundle", "/api/system/status",
        "/api/oww_models", "/api/devices/{id}/shell",
        "/api/provision/start_script", "/api/events",
    ):
        assert expected in paths, f"{expected} missing from create_app()'s routes"


def test_create_runner_wires_init_and_create_app_together():
    devices = {}
    runner = run(em_api.create_runner(devices, {}, {}))
    assert isinstance(runner, web.AppRunner)
    assert em_api._devices is devices


# ─── Middleware and static/setup handlers ──────────────────────────────────

def test_ingress_only_middleware_and_error_middleware_shapes(monkeypatch):
    async def handler(_request):
        return web.Response(text="ok")

    monkeypatch.setattr(em_api, "INGRESS_ONLY", True)
    with pytest.raises(web.HTTPForbidden):
        run(em_api._ingress_only_middleware(Request(remote="203.0.113.5"), handler))
    ok = run(em_api._ingress_only_middleware(
        Request(remote=em_api.INGRESS_GATEWAY_IP), handler))
    assert ok.text == "ok"
    monkeypatch.setattr(em_api, "INGRESS_ONLY", False)
    ok2 = run(em_api._ingress_only_middleware(Request(remote="203.0.113.5"), handler))
    assert ok2.text == "ok"


def test_serve_spa_and_dashboard_503_when_static_dir_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "STATIC_DIR", tmp_path / "does-not-exist")
    assert run(em_api._serve_spa(Request())).status == 503
    assert run(em_api._serve_dashboard(Request())).status == 503


def test_post_setup_creates_the_first_admin_account_and_logs_in(monkeypatch):
    create_calls = []

    async def fake_create_first_admin(token, username, password):
        create_calls.append((token, username, password))

    async def fake_login(username, password):
        return "session-token", "admin"

    monkeypatch.setattr(em_api.auth, "create_first_admin", fake_create_first_admin)
    monkeypatch.setattr(em_api.auth, "login", fake_login)
    response = run(em_api._post_setup(Request(
        {"token": "boot", "username": "admin", "password": "hunter2hunter2"})))
    assert response.status == 201
    assert json.loads(response.text) == {"token": "session-token", "role": "admin"}
    assert create_calls == [("boot", "admin", "hunter2hunter2")]


def test_post_setup_propagates_auth_errors_as_http_errors(monkeypatch):
    async def fake_create_first_admin(*_a):
        raise em_api.auth.AuthError("setup_complete", "A user already exists", 403)

    monkeypatch.setattr(em_api.auth, "create_first_admin", fake_create_first_admin)
    with pytest.raises(em_api.auth.AuthError):
        run(em_api._post_setup(Request(
            {"token": "x", "username": "y", "password": "hunter2hunter2"})))


# ─── Users (both decorated with @auth.require_admin — called via
#     .__wrapped__ to bypass session RESOLUTION the way
#     test_api_controller_branches.py already does, since resolve_session
#     reads real request state this fake Request does not provide) ────────

def test_get_users_lists_accounts_and_hides_password_hash(monkeypatch):
    rows = [
        {"id": 1, "username": "admin", "role": "admin", "ha_user_id": None,
         "created_at": "2026-01-01", "password_hash": "secret-hash"},
        {"id": 2, "username": "ha-alice", "role": "readonly", "ha_user_id": "ha-1",
         "created_at": "2026-01-02", "password_hash": "sentinel"},
    ]
    monkeypatch.setattr(em_api.db, "get_all_users", lambda: rows)
    response = run(em_api._get_users.__wrapped__(Request(user=_as_admin())))
    assert response.status == 200
    body = json.loads(response.text)
    assert body[0]["username"] == "admin"
    assert body[1]["ha_linked"] is True
    assert all("password_hash" not in u for u in body)


def test_patch_user_role_changes_are_gated_and_the_last_admin_is_protected(monkeypatch):
    admin_row = {"id": 1, "username": "admin", "role": "admin", "ha_user_id": None}
    other_row = {"id": 2, "username": "sam", "role": "readonly", "ha_user_id": None}

    def fake_get_user_by_id(uid):
        return {1: admin_row, 2: other_row}.get(uid)

    monkeypatch.setattr(em_api.db, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(em_api.db, "set_user_role", lambda *a: None)

    bad_role = run(em_api._patch_user.__wrapped__(Request(
        {"role": "superuser"}, match_info={"id": "2"}, user=_as_admin())))
    assert bad_role.status == 400

    bad_id = run(em_api._patch_user.__wrapped__(Request(
        {"role": "admin"}, match_info={"id": "not-a-number"}, user=_as_admin())))
    assert bad_id.status == 400

    missing = run(em_api._patch_user.__wrapped__(Request(
        {"role": "admin"}, match_info={"id": "99"}, user=_as_admin())))
    assert missing.status == 404

    unchanged = run(em_api._patch_user.__wrapped__(Request(
        {"role": "readonly"}, match_info={"id": "2"}, user=_as_admin())))
    assert json.loads(unchanged.text)["changed"] is False

    monkeypatch.setattr(em_api.db, "admin_count", lambda: 1)
    last_admin = run(em_api._patch_user.__wrapped__(Request(
        {"role": "readonly"}, match_info={"id": "1"}, user=_as_admin())))
    assert last_admin.status == 409

    monkeypatch.setattr(em_api.db, "admin_count", lambda: 2)
    promoted = run(em_api._patch_user.__wrapped__(Request(
        {"role": "admin"}, match_info={"id": "2"}, user=_as_admin())))
    assert json.loads(promoted.text) == {"id": 2, "role": "admin", "changed": True}


# ─── Auth (login/logout/me/ingress) ────────────────────────────────────────

def test_post_ingress_login_401s_when_ingress_decide_finds_no_identity(monkeypatch):
    monkeypatch.setattr(em_api.em_ingressauth, "decide", lambda **_kw: None)
    response = run(em_api._post_ingress_login(Request(remote="203.0.113.1")))
    assert response.status == 401


def test_post_ingress_login_logs_in_the_forwarded_ha_user(monkeypatch):
    monkeypatch.setattr(em_api.em_ingressauth, "decide",
                        lambda **_kw: {"user_id": "ha-1", "username": "alice"})

    async def fake_login_via_ingress(identity):
        assert identity["user_id"] == "ha-1"
        return "tok-ingress", "readonly"

    monkeypatch.setattr(em_api.auth, "login_via_ingress", fake_login_via_ingress)
    response = run(em_api._post_ingress_login(Request(
        remote=em_api.INGRESS_GATEWAY_IP,
        headers={"X-Remote-User-Id": "ha-1", "X-Remote-User-Name": "alice"})))
    assert json.loads(response.text) == {"token": "tok-ingress", "role": "readonly", "via": "ingress"}


def test_post_login_and_logout_round_trip(monkeypatch):
    async def fake_login(username, password):
        assert (username, password) == ("admin", "hunter2hunter2")
        return "tok-1", "admin"

    monkeypatch.setattr(em_api.auth, "login", fake_login)
    logged_in = run(em_api._post_login(Request({"username": "admin", "password": "hunter2hunter2"})))
    assert json.loads(logged_in.text) == {"token": "tok-1", "role": "admin"}

    logout_calls = []

    async def fake_resolve_session(_request):
        return {"token": "tok-1"}

    async def fake_logout(token):
        logout_calls.append(token)

    monkeypatch.setattr(em_api.auth, "resolve_session", fake_resolve_session)
    monkeypatch.setattr(em_api.auth, "logout", fake_logout)
    response = run(em_api._post_logout(Request()))
    assert response.status == 200
    assert logout_calls == ["tok-1"]


def test_post_logout_is_a_no_op_with_no_session(monkeypatch):
    async def fake_resolve_session(_request):
        return None

    monkeypatch.setattr(em_api.auth, "resolve_session", fake_resolve_session)
    response = run(em_api._post_logout(Request()))
    assert response.status == 200


def test_get_me_reports_the_current_session(monkeypatch):
    response = run(em_api._get_me.__wrapped__(Request(
        user={"id": 1, "username": "admin", "role": "admin"})))
    assert json.loads(response.text) == {"id": 1, "username": "admin", "role": "admin"}


# ─── Devices ────────────────────────────────────────────────────────────────

def _device_row(**overrides):
    row = {
        "device_id": "dev1", "label": "Kitchen", "approved": 1, "ip": "192.0.2.1",
        "firmware_ver": "v1", "firmware_previous": None, "first_seen": 1,
        "last_seen": 2, "config": "{}", "config_sections": "[]", "token": "tok",
    }
    row.update(overrides)
    return row


def test_get_devices_and_get_pending_merge_live_state(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_all_devices", lambda: [_device_row()])
    monkeypatch.setattr(em_api.db, "get_pending_devices",
                        lambda: [_device_row(device_id="dev2", approved=0)])
    all_devices = run(em_api._get_devices.__wrapped__(Request(user=_as_readonly())))
    pending = run(em_api._get_pending.__wrapped__(Request(user=_as_readonly())))
    assert json.loads(all_devices.text)[0]["device_id"] == "dev1"
    assert json.loads(pending.text)[0]["device_id"] == "dev2"


def test_get_device_404s_for_an_unknown_id(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_device", lambda _id: None)
    response = run(em_api._get_device.__wrapped__(
        Request(match_info={"id": "ghost"}, user=_as_readonly())))
    assert response.status == 404


def test_get_device_returns_the_merged_row(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_device", lambda _id: _device_row())
    response = run(em_api._get_device.__wrapped__(
        Request(match_info={"id": "dev1"}, user=_as_readonly())))
    assert json.loads(response.text)["device_id"] == "dev1"


def test_get_device_turns_redacts_for_readonly_and_validates_query_params(monkeypatch):
    monkeypatch.setattr(em_api.db, "reconcile_device_audio", lambda _id: None)
    monkeypatch.setattr(em_api.db, "get_turns", lambda *a: [
        {"id": 1, "stt_text": "secret", "tts_text": "reply", "outcome": "ok"},
    ])
    admin_response = run(em_api._get_device_turns.__wrapped__(
        Request(match_info={"id": "dev1"}, user=_as_admin())))
    assert json.loads(admin_response.text)[0]["stt_text"] == "secret"

    readonly_response = run(em_api._get_device_turns.__wrapped__(
        Request(match_info={"id": "dev1"}, user=_as_readonly())))
    assert "stt_text" not in json.loads(readonly_response.text)[0]

    bad_query = run(em_api._get_device_turns.__wrapped__(
        Request(match_info={"id": "dev1"}, query={"limit": "not-a-number"},
                user=_as_admin())))
    assert bad_query.status == 400


def test_post_recordings_prune_reports_the_result(monkeypatch):
    monkeypatch.setattr(em_api.db, "prune_audio", lambda keep: {"deleted": 3, "kept": 20})
    response = run(em_api._post_recordings_prune.__wrapped__(Request(user=_as_admin())))
    assert json.loads(response.text) == {"deleted": 3, "kept": 20}


def test_get_turn_audio_validates_kind_turn_id_and_device_existence(monkeypatch):
    bad_kind = run(em_api._get_turn_audio.__wrapped__(Request(
        match_info={"id": "dev1", "turn": "1", "kind": "video"}, user=_as_admin())))
    assert bad_kind.status == 400

    bad_turn = run(em_api._get_turn_audio.__wrapped__(Request(
        match_info={"id": "dev1", "turn": "not-a-number"}, user=_as_admin())))
    assert bad_turn.status == 400

    monkeypatch.setattr(em_api.db, "get_device", lambda _id: None)
    unknown_device = run(em_api._get_turn_audio.__wrapped__(Request(
        match_info={"id": "ghost", "turn": "1"}, user=_as_admin())))
    assert unknown_device.status == 404

    monkeypatch.setattr(em_api.db, "get_device", lambda _id: _device_row())
    monkeypatch.setattr(em_api.em_recordings, "filename", lambda *a: None)
    no_recording = run(em_api._get_turn_audio.__wrapped__(Request(
        match_info={"id": "dev1", "turn": "1"}, user=_as_admin())))
    assert no_recording.status == 404


# ─── _run_update — the A/B slot OTA background task ────────────────────────
#
# _run_update is a long straight-line background task with many early-return
# failure exits. Rather than one giant test, each scenario below sets up
# just enough mocking to reach a DIFFERENT exit and asserts the specific
# outcome (an _update_failed reason, or a specific pushed event) that exit
# is responsible for — mirroring how the function itself is structured.

def _record_events(events):
    """An async `_push_event` stand-in that records instead of broadcasting.
    A plain async function rather than the `lambda: asyncio.sleep(0, ...)`
    trick used elsewhere in this file — that trick calls through the module
    -level `asyncio.sleep`, which some of these tests ALSO monkeypatch, and
    the patched version does not accept `result=`."""
    async def _push(event):
        events.append(event)
    return _push


def _run_update_common_mocks(monkeypatch, *, device_row=None, shell_responses=None,
                            transfer_ok=True, reconnect_ok=True):
    """Shared plumbing every _run_update scenario needs: a live device, a
    fetchable binary, a working shell, and no real sleeps."""
    device_row = device_row if device_row is not None else {"firmware_ver": "v1"}
    em_api._devices["dev"] = object()
    monkeypatch.setattr(em_api.db, "get_device", lambda _id: device_row)
    monkeypatch.setattr(em_api.db, "set_firmware_previous", lambda *a: None)
    monkeypatch.setattr(em_api, "_push_log_event", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_push_event", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_sync_start_script", lambda *a: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_sync_debloat", lambda *a: asyncio.sleep(0))
    # em_api.asyncio IS the same module object as the `asyncio` imported at
    # the top of this file — patching its `.sleep` and then calling
    # `asyncio.sleep(0)` from the replacement recurses into itself. Capture
    # the real one first and drive the fake off that instead.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(em_api.asyncio, "sleep", lambda *_a: _real_sleep(0))

    responses = iter(shell_responses or ["SLOT:server_a", "FREE 500 90% /data"])

    async def fake_shell_run(*_a, **_kw):
        try:
            return next(responses)
        except StopIteration:
            return ""

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)

    async def fake_stream(*_a, **_kw):
        return transfer_ok

    monkeypatch.setattr(em_api, "_stream_binary_to_slot", fake_stream)

    async def fake_monitor(*_a, **_kw):
        return reconnect_ok

    monkeypatch.setattr(em_api, "_monitor_reconnect", fake_monitor)


def test_run_update_uses_the_uploaded_binary_override_and_confirms(monkeypatch):
    _run_update_common_mocks(monkeypatch)
    events = []
    monkeypatch.setattr(em_api, "_push_event", _record_events(events))
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"firmware-bytes"))
    assert em_api._update_errors.get("dev") is None
    assert any(e["type"] == "device_updated" for e in events)
    assert "dev" not in em_api._updates_in_progress


def test_run_update_fails_when_the_release_binary_cannot_be_fetched(monkeypatch):
    _run_update_common_mocks(monkeypatch)

    async def fake_fetch(*_a, **_kw):
        return None

    monkeypatch.setattr(em_api, "_fetch_binary", fake_fetch)
    run(em_api._run_update("dev", {"version": "v2", "url": "https://example/x"}))
    assert "Failed to fetch binary" in em_api._update_errors["dev"]


def test_run_update_fails_when_the_device_disconnects_before_starting(monkeypatch):
    _run_update_common_mocks(monkeypatch)
    em_api._devices.pop("dev", None)  # disconnected
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "disconnected" in em_api._update_errors["dev"]


def test_run_update_fails_on_ab_migration_failure(monkeypatch):
    _run_update_common_mocks(monkeypatch, shell_responses=["MIGRATE_FAILED"])
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "migration failed" in em_api._update_errors["dev"]


def test_run_update_fails_when_active_slot_is_undetectable(monkeypatch):
    _run_update_common_mocks(monkeypatch, shell_responses=["garbage output, no SLOT: token"])
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "determine active slot" in em_api._update_errors["dev"]


def test_run_update_fails_when_free_space_is_too_low(monkeypatch):
    _run_update_common_mocks(
        monkeypatch,
        shell_responses=["SLOT:server_a", "FREE 1 99% /data"],  # 1MB free
    )
    run(em_api._run_update("dev", {"version": "v2"},
                           binary_override=b"x" * (20 * 1024 * 1024)))
    assert "Not enough space" in em_api._update_errors["dev"]


def test_run_update_proceeds_when_free_space_is_unparseable(monkeypatch):
    _run_update_common_mocks(
        monkeypatch,
        shell_responses=["SLOT:server_a", "FREE not-a-df-line-at-all"],
    )
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    # An unreadable df is "carry on", never a failure.
    assert em_api._update_errors.get("dev") is None


def test_run_update_fails_when_the_binary_transfer_is_rejected(monkeypatch):
    _run_update_common_mocks(monkeypatch, transfer_ok=False)
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "transfer to" in em_api._update_errors["dev"]


def test_run_update_reports_auto_rollback_when_the_device_returns_on_the_old_version(monkeypatch):
    _run_update_common_mocks(
        monkeypatch, device_row={"firmware_ver": "v1"}, reconnect_ok=False)
    events = []
    monkeypatch.setattr(em_api, "_push_event", _record_events(events))
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "auto-rolled back" in em_api._update_errors["dev"]
    assert any(e["type"] == "device_auto_rolled_back" for e in events)


def test_run_update_reports_a_plain_timeout_when_the_device_never_returns(monkeypatch):
    """
    The device comes back on neither the new NOR the old version — not a
    rollback, just a device that never confirmed. db.get_device is read
    TWICE (once up front to capture current_ver, once after the failed
    reconnect to see what actually came back), so this needs a value that
    changes between calls — a real device would not do this, but it is the
    only way to reach the branch where `running` is neither `expected` nor
    `current_ver`.
    """
    _run_update_common_mocks(monkeypatch, reconnect_ok=False)
    calls = {"n": 0}

    def flaky_get_device(_id):
        calls["n"] += 1
        return {"firmware_ver": "v1" if calls["n"] == 1 else "v-mystery"}

    monkeypatch.setattr(em_api.db, "get_device", flaky_get_device)
    events = []
    monkeypatch.setattr(em_api, "_push_event", _record_events(events))
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "timed out" in em_api._update_errors["dev"]
    assert any(e["type"] == "device_update_failed" for e in events)


def test_run_update_catches_and_reports_an_unexpected_exception(monkeypatch):
    _run_update_common_mocks(monkeypatch)

    async def boom(*_a, **_kw):
        raise RuntimeError("shell plane exploded")

    monkeypatch.setattr(em_api, "_shell_run", boom)
    run(em_api._run_update("dev", {"version": "v2"}, binary_override=b"x"))
    assert "OTA exception" in em_api._update_errors["dev"]
    assert "dev" not in em_api._updates_in_progress


# ─── _stream_file_to_device timeout branches ───────────────────────────────
#
# tests/test_api_controller_error_paths.py already covers the shell-open
# failure, decoder-missing (with output), md5tool-missing, unverified
# success, verified success and corrupt-md5 outcomes. What is left is the
# three "nothing came back in time" branches, each guarded by a REAL
# `time.monotonic()`-based deadline (15s / 120s / 120s) that must not
# actually be waited out in a test.
#
# Counting/jumping em_api.time.monotonic() by call number turned out to be
# fragile: asyncio's own `wait_for`/event-loop internals read the very same
# `time.monotonic` (em_api.time IS the shared `time` module) an
# implementation-dependent number of times per call, so the "which call
# number is the one that matters" arithmetic silently drifted and either
# starved a phase that was supposed to succeed or spun for the real 120s
# instead of failing fast — both observed while developing this test.
#
# The robust fix ties the clock to the SEMANTIC event under test instead of
# a call count: a shared FakeClock starts at the real time and only jumps
# forward when the device genuinely stops answering (TransferWS's replies
# run out), which is exactly the moment any of the three polling loops
# below should give up. Every call before that reads a normal, real-ish
# time, so a phase that is SUPPOSED to succeed does so exactly as it would
# against a real clock.

class FakeClock:
    def __init__(self):
        self._real = em_api.time.monotonic
        self._offset = 0.0

    def time(self) -> float:
        return self._real() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds


class TransferWS:
    def __init__(self, replies, clock=None):
        self.replies = iter(replies)
        self.sent = []
        self._clock = clock

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        try:
            return next(self.replies)
        except StopIteration:
            if self._clock is not None:
                # The device has genuinely stopped answering — jump the
                # shared clock far past any of this function's deadlines,
                # so whichever poll loop is currently running gives up on
                # its very next check instead of spinning for real minutes.
                self._clock.advance(10_000.0)
            raise asyncio.TimeoutError()


def test_stream_file_to_device_detect_phase_times_out_with_no_output(monkeypatch):
    """DETECT_MARKER never appears — the shell plane is producing no output
    at all (a link problem, not a missing tool), so this must be reported
    distinctly from "decoder" (#121 was exactly this misreported)."""
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *a: asyncio.sleep(0))
    clock = FakeClock()
    monkeypatch.setattr(em_api.time, "monotonic", clock.time)
    ws = TransferWS([], clock=clock)  # every recv() times out immediately
    monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda _live: asyncio.sleep(0, result=ws))
    live = SimpleNamespace(device_id="dev")
    result = run(em_api._stream_file_to_device(live, b"x", "/tmp/x"))
    assert result.stage == "shell"
    assert "no output" in result.detail


def test_stream_file_to_device_transfer_confirmation_times_out(monkeypatch):
    """Detection succeeds (a real busybox decoder+md5 reply lands on the
    very first recv), but TRANSFER_OK never arrives."""
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *a: asyncio.sleep(0))
    clock = FakeClock()
    monkeypatch.setattr(em_api.time, "monotonic", clock.time)
    ws = TransferWS(["DECODER:busybox MD5:busybox __DETECT_DONE__"], clock=clock)
    monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda _live: asyncio.sleep(0, result=ws))
    live = SimpleNamespace(device_id="dev")
    result = run(em_api._stream_file_to_device(live, b"payload", "/tmp/file"))
    assert result.stage == "send"


def test_stream_file_to_device_md5_verification_times_out(monkeypatch):
    """The transfer itself confirms (TRANSFER_OK), but the md5 round trip
    that would promote {dest}.part -> {dest} never answers — {dest} is left
    untouched rather than promoted unverified."""
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *a: asyncio.sleep(0))
    clock = FakeClock()
    monkeypatch.setattr(em_api.time, "monotonic", clock.time)
    ws = TransferWS([
        "DECODER:busybox MD5:busybox __DETECT_DONE__",
        "TRANSFER_OK",
    ], clock=clock)
    monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda _live: asyncio.sleep(0, result=ws))
    live = SimpleNamespace(device_id="dev")
    result = run(em_api._stream_file_to_device(live, b"payload", "/tmp/file"))
    assert result.stage == "verify"


# ─── _sync_start_script / _debloat_packages / _sync_debloat ───────────────

def _no_real_sleep(monkeypatch):
    """asyncio.sleep(1.0) calls pace real shell sessions closing cleanly on
    hardware; none of that is needed against these mocks."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(em_api.asyncio, "sleep", lambda *_a: real_sleep(0))


def test_debloat_packages_strips_comments_and_reports_unreadable_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    assert em_api._debloat_packages() == []  # file does not exist -> OSError

    (tmp_path / "debloat_packages.txt").write_text(
        "# comment\ncom.amazon.one\n\ncom.amazon.two\n  # indented comment\n")
    assert em_api._debloat_packages() == ["com.amazon.one", "com.amazon.two"]


def test_sync_start_script_skips_when_payload_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)  # no start_server.sh there
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_start_script(live, "dev"))  # must not raise


def test_sync_start_script_is_a_no_op_when_already_in_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    script = b"#!/system/bin/sh\necho hi\n"
    (tmp_path / "start_server.sh").write_bytes(script)
    want = em_api.hashlib.md5(script).hexdigest()

    async def fake_shell_run(*_a, **_kw):
        return f"{want}  /data/local/bin/start_server.sh\n"

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)

    def unexpected(*_a, **_kw):
        raise AssertionError("an in-sync script must not attempt a transfer")

    monkeypatch.setattr(em_api, "_stream_file_to_device", unexpected)
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_start_script(live, "dev"))


@pytest.mark.parametrize("verify_reply,expect_synced", [
    ("SCRIPT_SYNCED", True),
    ("SCRIPT_MD5_MISMATCH:deadbeef", False),
])
def test_sync_start_script_syncs_and_reports_the_final_verify_outcome(
        monkeypatch, tmp_path, verify_reply, expect_synced):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    _no_real_sleep(monkeypatch)
    (tmp_path / "start_server.sh").write_bytes(b"#!/system/bin/sh\necho new\n")

    shell_calls = []

    async def fake_shell_run(_live, cmd, **_kw):
        shell_calls.append(cmd)
        # The verify command's own md5sum target is "start_server.sh.new",
        # which contains "start_server.sh" as a substring — so this must
        # match on the INITIAL check's exact shape (a bare `busybox md5sum`,
        # no `NEW=$(...)` wrapper) or it also swallows the verify call below
        # and this test can never observe SCRIPT_SYNCED/MD5_MISMATCH at all.
        if cmd.startswith("busybox md5sum") and "start_server.sh" in cmd:
            return "0000  /data/local/bin/start_server.sh\n"  # out of date
        return verify_reply

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)

    async def fake_stream(*_a, **_kw):
        return True

    monkeypatch.setattr(em_api, "_stream_file_to_device", fake_stream)
    events = []

    async def fake_push_log_event(*a):
        events.append(a)

    monkeypatch.setattr(em_api, "_push_log_event", fake_push_log_event)
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_start_script(live, "dev"))
    levels = [e[1] for e in events]
    # The FIRST push is always "info" ("out of date — syncing"), win or lose
    # — it fires before the transfer is even attempted. Only the LAST push
    # (the verify-outcome report) actually distinguishes SCRIPT_SYNCED from
    # SCRIPT_MD5_MISMATCH, so that's the one this test must check.
    assert (levels[-1] == "info") is expect_synced
    assert ("warn" in levels) is not expect_synced


def test_sync_start_script_reports_a_failed_transfer_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    _no_real_sleep(monkeypatch)
    (tmp_path / "start_server.sh").write_bytes(b"#!/system/bin/sh\necho new\n")

    async def fake_shell_run(*_a, **_kw):
        return "0000  out of date\n"

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)

    async def fake_stream(*_a, **_kw):
        return em_api._transfer_failed("shell")

    monkeypatch.setattr(em_api, "_stream_file_to_device", fake_stream)
    events = []

    async def fake_push_log_event(*a):
        events.append(a)

    monkeypatch.setattr(em_api, "_push_log_event", fake_push_log_event)
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_start_script(live, "dev"))
    assert any(e[1] == "warn" and "sync failed" in e[3] for e in events)


def test_sync_debloat_skips_script_half_when_payload_is_unreadable_and_no_packages(
        monkeypatch, tmp_path):
    """Both halves degrade independently: an unreadable script payload
    skips half 1 entirely (logged, not raised) and an empty package list
    ends half 2 with nothing to do."""
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)  # no files present
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_debloat(live, "dev"))  # must not raise


def test_sync_debloat_syncs_the_boot_script_and_reports_the_pm_hide_result(
        monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    _no_real_sleep(monkeypatch)
    (tmp_path / "echomuse-debloat.sh").write_bytes(b"#!/system/bin/sh\n# debloat\n")
    (tmp_path / "debloat_packages.txt").write_text(
        "com.amazon.whad\ncom.amazon.tahoe\n")

    async def fake_shell_run(_live, cmd, **_kw):
        # Order matters: the verify command's md5sum target is
        # DEBLOAT_SCRIPT_PATH + ".new", so it also contains DEBLOAT_SCRIPT_PATH
        # as a substring. The more specific `NEW=$(...)` wrapper shape must be
        # checked first or the initial-check branch swallows the verify call
        # too and DEBLOAT_SYNCED is never observed (same trap as the
        # start_server.sh test above).
        if cmd.startswith("NEW=$(busybox md5sum"):
            return "DEBLOAT_SYNCED\n"
        if "md5sum" in cmd and em_api.DEBLOAT_SCRIPT_PATH in cmd:
            return "0000  out of date\n"
        # The pm-hide reconciliation command — one visible package hidden,
        # one still stubbornly visible (PERSISTENT, e.g. com.amazon.whad).
        return "HIDDEN_APPLIED:1\nSTILL_VISIBLE: com.amazon.whad\n"

    monkeypatch.setattr(em_api, "_shell_run", fake_shell_run)

    async def fake_stream(*_a, **_kw):
        return True

    monkeypatch.setattr(em_api, "_stream_file_to_device", fake_stream)
    events = []

    async def fake_push_log_event(*a):
        events.append(a)

    monkeypatch.setattr(em_api, "_push_log_event", fake_push_log_event)
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_debloat(live, "dev"))
    messages = [e[3] for e in events]
    assert any("debloat script synced" in m for m in messages)
    assert any("hid 1 newly-listed" in m for m in messages)
    assert any("could not be" in m and "com.amazon.whad" in m for m in messages)


def test_sync_debloat_reports_hide_list_transfer_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(em_api, "PAYLOADS_DIR", tmp_path)
    _no_real_sleep(monkeypatch)
    # No echomuse-debloat.sh -> half 1 is skipped entirely (already covered);
    # this isolates half 2's own failure branch.
    (tmp_path / "debloat_packages.txt").write_text("com.amazon.whad\n")

    async def fake_stream(*_a, **_kw):
        return em_api._transfer_failed("shell")

    monkeypatch.setattr(em_api, "_stream_file_to_device", fake_stream)
    events = []

    async def fake_push_log_event(*a):
        events.append(a)

    monkeypatch.setattr(em_api, "_push_log_event", fake_push_log_event)
    live = SimpleNamespace(device_id="dev")
    run(em_api._sync_debloat(live, "dev"))
    assert any(e[1] == "warn" and "hide-list sync failed" in e[3] for e in events)


# ─── _oww_wanted_models / _hold_back_oww_model ─────────────────────────────
# Pure decision logic (issue #191's install-before-switch guard) — no I/O,
# just db.get_effective_device_config and the live Device's own attributes.

def test_oww_wanted_models_orders_wake_before_stop_and_drops_blanks(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_effective_device_config",
                        lambda _id: {"owwModel": "alexa", "stopModel": "stop_v1"})
    assert em_api._oww_wanted_models("dev") == ["alexa", "stop_v1"]

    # A device with only a stop model configured (no wake word chosen yet).
    monkeypatch.setattr(em_api.db, "get_effective_device_config",
                        lambda _id: {"owwModel": "", "stopModel": "stop_v1"})
    assert em_api._oww_wanted_models("dev") == ["stop_v1"]

    # Same model used for both is deduplicated to one entry, not sent twice.
    monkeypatch.setattr(em_api.db, "get_effective_device_config",
                        lambda _id: {"owwModel": "shared", "stopModel": "shared"})
    assert em_api._oww_wanted_models("dev") == ["shared"]

    # No config row at all (get_effective_device_config returning None) must
    # not raise — a device that has never been configured wants nothing.
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda _id: None)
    assert em_api._oww_wanted_models("dev") == []


def test_hold_back_oww_model_only_holds_capable_devices_with_a_real_change():
    live = SimpleNamespace(oww_model="old_model", capabilities=["wake_request_v1"])

    # No change to owwModel at all — nothing to hold back.
    sent, pending = em_api._hold_back_oww_model(live, {"owwModel": "old_model"})
    assert sent == {"owwModel": "old_model"}
    assert pending is None

    # A genuine switch, and the device can request wakes — held back to the
    # CURRENT model while the classifier installs; caller learns what the
    # eventual target is.
    sent, pending = em_api._hold_back_oww_model(live, {"owwModel": "new_model", "x": 1})
    assert sent == {"owwModel": "old_model", "x": 1}
    assert pending == "new_model"

    # Same switch, but firmware without wake_request_v1 cannot safely accept
    # a held-back config at all — goes straight through, unheld.
    old_fw = SimpleNamespace(oww_model="old_model", capabilities=[])
    sent, pending = em_api._hold_back_oww_model(old_fw, {"owwModel": "new_model"})
    assert sent == {"owwModel": "new_model"}
    assert pending is None

    # No capabilities attribute at all (a bare stand-in) must not raise.
    bare = SimpleNamespace(oww_model="old_model")
    sent, pending = em_api._hold_back_oww_model(bare, {"owwModel": "new_model"})
    assert pending is None


# ─── _get_provision_oww_manifest / _get_provision_oww_asset ────────────────

def test_get_provision_oww_manifest_lists_the_fleet_wake_and_stop_assets(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_global_device_config",
                        lambda: {"owwModel": "alexa", "stopModel": "stop_v1"})
    fake_asset = SimpleNamespace(name="alexa.onnx", size=123, md5="abc123")
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets",
                        lambda models: ([fake_asset], []))
    resp = run(em_api._get_provision_oww_manifest(Request()))
    body = json.loads(resp.text)
    assert body["assets"] == [{"name": "alexa.onnx", "size": 123, "md5": "abc123"}]
    assert body["problems"] == []


def test_get_provision_oww_asset_serves_bytes_for_a_listed_name(monkeypatch, tmp_path):
    payload = tmp_path / "alexa.onnx"
    payload.write_bytes(b"fake-model-bytes")
    monkeypatch.setattr(em_api.db, "get_global_device_config",
                        lambda: {"owwModel": "alexa"})
    fake_asset = SimpleNamespace(name="alexa.onnx", source=payload, md5="deadbeef")
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets",
                        lambda models: ([fake_asset], []))
    resp = run(em_api._get_provision_oww_asset(Request(match_info={"name": "alexa.onnx"})))
    assert resp.body == b"fake-model-bytes"
    assert resp.headers["X-Asset-MD5"] == "deadbeef"


def test_get_provision_oww_asset_404s_for_a_name_the_manifest_never_listed(monkeypatch):
    # Resolved against the manifest, never the raw request path — a name
    # nobody currently wants must not be servable even if the file exists
    # on disk somewhere, or this becomes a path-traversal-adjacent read.
    monkeypatch.setattr(em_api.db, "get_global_device_config",
                        lambda: {"owwModel": "alexa"})
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets",
                        lambda models: ([], []))
    resp = run(em_api._get_provision_oww_asset(
        Request(match_info={"name": "../../etc/passwd"})))
    assert resp.status == 404


def test_get_provision_oww_asset_500s_when_the_file_cannot_be_read(monkeypatch, tmp_path):
    missing = tmp_path / "gone.onnx"  # never written
    monkeypatch.setattr(em_api.db, "get_global_device_config",
                        lambda: {"owwModel": "alexa"})
    fake_asset = SimpleNamespace(name="gone.onnx", source=missing, md5="x")
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets",
                        lambda models: ([fake_asset], []))
    resp = run(em_api._get_provision_oww_asset(Request(match_info={"name": "gone.onnx"})))
    assert resp.status == 500


# ─── _controller_stats / _proc_meminfo / _read_first_line ──────────────────
# Deliberately run against the REAL host: every field degrades to "absent"
# on failure (see the function's own docstring), so calling it unmocked on
# this Linux CI host exercises almost the whole function using genuine
# /proc data rather than fabricated fixtures — the same reasoning
# test_support.py already uses for CpuHistory.

def test_controller_stats_reports_the_real_host_without_raising(monkeypatch, tmp_path):
    # Give it a real, empty "database" and recordings dir so the storage
    # branches run their real stat()/disk_usage() calls too, rather than
    # hitting whatever DB_PATH happens to be set to in this process.
    db_file = tmp_path / "echomuse.db"
    db_file.write_bytes(b"x" * (2 * 1024 * 1024))  # 2MB: rounds to a visible non-zero MB
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setattr(em_recordings, "recordings_dir", lambda: tmp_path)
    stats = em_api._controller_stats()
    # These come straight from platform/os and cannot be absent on any host.
    assert stats["python"]
    assert stats["platform"]
    assert isinstance(stats["cpu_count"], int)
    assert isinstance(stats["container"], bool)
    # /proc/meminfo exists on every Linux host this test runs on.
    assert "mem_total_mb" in stats
    assert stats["db_mb"] > 0


def test_proc_meminfo_returns_empty_dict_rather_than_raising_without_procfs(monkeypatch):
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/meminfo":
            raise OSError("no procfs on this platform")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(em_api, "open", fake_open, raising=False)
    assert em_api._proc_meminfo() == {}


def test_read_first_line_strips_trailing_whitespace(tmp_path):
    f = tmp_path / "one_line"
    f.write_text("max\n")
    assert em_api._read_first_line(str(f)) == "max"


# ─── _update_check_interval / _get_cached_release ──────────────────────────

def test_update_check_interval_falls_back_to_default_on_garbage_config(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_config", lambda *_a, **_kw: "not-a-number")
    assert em_api._update_check_interval() == 3600


def test_get_cached_release_serves_the_hot_in_memory_cache_without_any_db_call(monkeypatch):
    monkeypatch.setattr(em_api, "_release_cache", {"version": "v9.9.9"})
    monkeypatch.setattr(em_api, "_release_cache_ts", em_api.time.monotonic())

    def boom(*_a, **_kw):
        raise AssertionError("a hot cache hit must not touch the DB at all")

    monkeypatch.setattr(em_api.db, "get_config", boom)
    assert run(em_api._get_cached_release()) == {"version": "v9.9.9"}


def test_get_cached_release_returns_none_when_disabled_and_nothing_cached(monkeypatch):
    monkeypatch.setattr(em_api, "_release_cache", None)
    monkeypatch.setattr(em_api, "_release_cache_ts", 0.0)
    # No DB-cached version/url, and polling disabled (<=0) — must not reach
    # out to GitHub at all; an explicit "Check now" call bypasses this
    # function entirely and calls _fetch_latest_release directly.
    monkeypatch.setattr(em_api.db, "get_config",
                        lambda key, default=None: "0" if key == "update_check_interval" else default)
    assert run(em_api._get_cached_release()) is None


# ─── _fetch_latest_release / _fetch_controller_release ─────────────────────
# Both talk to the GitHub API through `aiohttp.ClientSession`. A minimal fake
# session/response pair (routed by URL, since _fetch_controller_release makes
# TWO sequential GETs on the same session) lets these run with no network.

class _FakeGHResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeGHSession:
    """`get(url)` is routed through a caller-supplied function so a single
    session can answer the tags-list call and the tag-object call
    _fetch_controller_release makes on it differently."""

    def __init__(self, responder):
        self._responder = responder

    def get(self, url, **_kw):
        return self._responder(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _install_fake_github(monkeypatch, responder):
    monkeypatch.setattr(em_api.aiohttp, "ClientSession",
                        lambda *a, **kw: _FakeGHSession(responder))


def test_fetch_latest_release_persists_the_newest_v_tag_with_a_server_asset(monkeypatch):
    releases = [
        {"draft": True, "tag_name": "v9.9.9"},               # skipped: draft
        {"tag_name": "controller-v3.0.0", "assets": []},     # skipped: not a v* tag
        {"tag_name": "v2.11.0", "assets": [{"name": "checksums.txt"}]},  # no server asset
        {"tag_name": "v2.10.0", "assets": [
            {"name": "server", "browser_download_url": "https://dl/server"}],
         "body": "release notes here", "html_url": "https://gh/releases/v2.10.0",
         "published_at": "2026-01-01T00:00:00Z"},
    ]
    _install_fake_github(monkeypatch, lambda url: _FakeGHResponse(200, releases))
    written = {}
    monkeypatch.setattr(em_api.db, "set_config",
                        lambda k, v: written.__setitem__(k, v))
    monkeypatch.setattr(em_api.db, "get_config",
                        lambda k, default=None: "old-repo/name" if k == "github_repo" else
                        ("v1.0.0" if k == "latest_version" else default))
    events = []
    monkeypatch.setattr(em_api, "_push_event", lambda e: events.append(e) or _await_none())

    result = run(em_api._fetch_latest_release())
    assert result["version"] == "v2.10.0"
    assert result["url"] == "https://dl/server"
    assert written["latest_version"] == "v2.10.0"
    assert written["latest_binary_url"] == "https://dl/server"
    # The version changed from the previously-cached one, so the dashboard
    # gets told without waiting for a reload.
    assert events and events[0]["type"] == "release_update"


def test_fetch_latest_release_returns_none_without_a_qualifying_release(monkeypatch):
    # Nothing here is a non-draft v*-tagged release with a `server` asset.
    _install_fake_github(monkeypatch, lambda url: _FakeGHResponse(
        200, [{"tag_name": "controller-v1.0.0", "assets": []}]))
    monkeypatch.setattr(em_api.db, "get_config", lambda k, default=None: default)
    assert run(em_api._fetch_latest_release()) is None


def test_fetch_latest_release_returns_none_on_a_non_200_status(monkeypatch):
    _install_fake_github(monkeypatch, lambda url: _FakeGHResponse(403, {}))
    monkeypatch.setattr(em_api.db, "get_config", lambda k, default=None: default)
    assert run(em_api._fetch_latest_release()) is None


def test_fetch_latest_release_swallows_any_exception_and_returns_none(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("DNS resolution failed")

    monkeypatch.setattr(em_api.aiohttp, "ClientSession", boom)
    monkeypatch.setattr(em_api.db, "get_config", lambda k, default=None: default)
    assert run(em_api._fetch_latest_release()) is None


async def _await_none():
    return None


def test_fetch_controller_release_picks_the_newest_tag_by_parsed_version(monkeypatch):
    # The refs API sorts lexically — controller-v2.9.0 would sort AFTER
    # controller-v2.10.0 in a naive string comparison, so this pins that the
    # parsed-version comparison is what actually decides "newest", not list
    # order (the exact regression CLAUDE.md calls out).
    refs = [
        {"ref": "refs/tags/controller-v2.9.0",
         "object": {"type": "tag", "sha": "sha-old"}},
        {"ref": "refs/tags/controller-v2.10.0",
         "object": {"type": "tag", "sha": "sha-new"}},
    ]
    tag_obj = {"message": "New controller release notes\n",
              "tagger": {"date": "2026-02-02T00:00:00Z"}}

    def responder(url):
        if "matching-refs" in url:
            return _FakeGHResponse(200, refs)
        assert "sha-new" in url, "must dereference the NEWEST tag's object, not the oldest"
        return _FakeGHResponse(200, tag_obj)

    _install_fake_github(monkeypatch, responder)
    monkeypatch.setattr(em_api.db, "set_config", lambda *_a: None)
    monkeypatch.setattr(em_api.db, "get_config",
                        lambda k, default=None: "old/repo" if k == "github_repo" else default)
    monkeypatch.setattr(em_api, "_controller_cache", None)
    monkeypatch.setattr(em_api, "_controller_cache_ts", 0.0)
    monkeypatch.setattr(em_api, "_push_event", lambda e: _await_none())

    result = run(em_api._fetch_controller_release(force=True))
    # version keeps its "v" — only the "controller-" prefix is stripped.
    assert result["version"] == "v2.10.0"
    assert result["notes"] == "New controller release notes"


def test_fetch_controller_release_returns_none_with_no_controller_tags(monkeypatch):
    _install_fake_github(monkeypatch, lambda url: _FakeGHResponse(200, []))
    monkeypatch.setattr(em_api.db, "get_config", lambda k, default=None: default)
    monkeypatch.setattr(em_api, "_controller_cache", None)
    monkeypatch.setattr(em_api, "_controller_cache_ts", 0.0)
    assert run(em_api._fetch_controller_release(force=True)) is None


def test_fetch_controller_release_serves_the_warm_cache_without_a_request(monkeypatch):
    monkeypatch.setattr(em_api, "_controller_cache", {"version": "1.2.3"})
    monkeypatch.setattr(em_api, "_controller_cache_ts", em_api.time.monotonic())

    def boom(*_a, **_kw):
        raise AssertionError("a warm cache hit must not open a session at all")

    monkeypatch.setattr(em_api.aiohttp, "ClientSession", boom)
    assert run(em_api._fetch_controller_release()) == {"version": "1.2.3"}


# ─── _install_then_switch ───────────────────────────────────────────────────
# Background task run after a config save that changes owwModel on a
# request-capable device (the em_api._hold_back_oww_model guard). Exercises
# the full install-then-switch state machine without a real shell/device.

def _install_switch_common(monkeypatch, live):
    monkeypatch.setattr(em_api, "_devices", {"dev": live})
    events = []

    async def fake_push_log_event(*a):
        events.append(a)

    monkeypatch.setattr(em_api, "_push_log_event", fake_push_log_event)
    return events


def test_install_then_switch_gives_up_quietly_if_the_device_is_gone_early(monkeypatch):
    monkeypatch.setattr(em_api, "_devices", {})  # never registered / already disconnected

    def boom(*_a, **_kw):
        raise AssertionError("a gone device must short-circuit before touching sync")

    monkeypatch.setattr(em_api, "_sync_oww_assets", boom)
    run(em_api._install_then_switch("dev", "new_model"))  # must not raise


def test_install_then_switch_leaves_the_device_on_its_old_model_when_sync_fails(monkeypatch):
    live = SimpleNamespace(oww_model="old_model", capabilities=["wake_request_v1"],
                           send_control=None)
    events = _install_switch_common(monkeypatch, live)

    async def failing_sync(*_a, **_kw):
        return {"ok": False, "error": "device disk full"}

    monkeypatch.setattr(em_api, "_sync_oww_assets", failing_sync)

    async def unexpected(*_a, **_kw):
        raise AssertionError("must not push a config the device cannot honour")

    live.send_control = unexpected
    run(em_api._install_then_switch("dev", "new_model"))
    assert live.oww_model == "old_model"  # never switched
    assert any(e[1] == "error" and "new_model" in e[3] for e in events)


def test_install_then_switch_abandons_if_the_wanted_model_changed_mid_install(monkeypatch):
    live = SimpleNamespace(oww_model="old_model", capabilities=["wake_request_v1"])
    _install_switch_common(monkeypatch, live)

    async def ok_sync(*_a, **_kw):
        return {"ok": True}

    monkeypatch.setattr(em_api, "_sync_oww_assets", ok_sync)
    # A second config save landed while the install was running and wants a
    # THIRD model now — this install's target is stale and must not apply.
    monkeypatch.setattr(em_api.db, "get_effective_device_config",
                        lambda _id: {"owwModel": "yet_another_model"})

    async def unexpected(*_a, **_kw):
        raise AssertionError("a stale install must not push its own target")

    live.send_control = unexpected
    run(em_api._install_then_switch("dev", "new_model"))
    assert live.oww_model == "old_model"


def test_install_then_switch_applies_the_config_and_switches_on_success(monkeypatch):
    live = SimpleNamespace(oww_model="old_model", capabilities=["wake_request_v1"])
    events = _install_switch_common(monkeypatch, live)

    async def ok_sync(*_a, **_kw):
        return {"ok": True}

    monkeypatch.setattr(em_api, "_sync_oww_assets", ok_sync)
    monkeypatch.setattr(em_api.db, "get_effective_device_config",
                        lambda _id: {"owwModel": "new_model", "vadThreshold": 0.5})
    sent = []

    async def record_send(msg):
        sent.append(msg)

    live.send_control = record_send
    wake_model_calls = []
    monkeypatch.setattr(em_api.ha_sidechannels, "wake_model",
                        lambda device_id, model: wake_model_calls.append((device_id, model)))
    run(em_api._install_then_switch("dev", "new_model"))
    assert live.oww_model == "new_model"
    assert sent == [{"type": "config", "owwModel": "new_model", "vadThreshold": 0.5}]
    assert wake_model_calls == [("dev", "new_model")]
    assert any(e[1] == "info" and "switched" in e[3] for e in events)
