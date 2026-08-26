import asyncio
import json
import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("websockets", types.ModuleType("websockets"))

import em_api


def run(awaitable):
    return asyncio.run(awaitable)


def request(body=None, **kwargs):
    class Request(dict):
        async def json(self):
            return body

    result = Request()
    result.update(kwargs)
    result.headers = {}
    result.remote = None
    for key, value in kwargs.items():
        setattr(result, key, value)
    return result


def test_setup_state_reflects_bootstrap_token(monkeypatch):
    monkeypatch.setattr(em_api.auth, "get_bootstrap_token", lambda: "token")
    response = run(em_api._get_setup_state(request()))
    assert json.loads(response.text) == {"needs_setup": True}
    monkeypatch.setattr(em_api.auth, "get_bootstrap_token", lambda: None)
    assert json.loads(run(em_api._get_setup_state(request())).text) == {"needs_setup": False}


def test_post_setup_creates_admin_then_logs_in(monkeypatch):
    calls = []

    async def create(token, username, password):
        calls.append((token, username, password))

    async def login(username, password):
        return "session", "admin"

    monkeypatch.setattr(em_api.auth, "create_first_admin", create)
    monkeypatch.setattr(em_api.auth, "login", login)
    response = run(em_api._post_setup(request({"token": "boot", "username": "admin", "password": "password"})))
    assert response.status == 201
    assert json.loads(response.text) == {"token": "session", "role": "admin"}
    assert calls == [("boot", "admin", "password")]


def test_post_login_and_logout(monkeypatch):
    monkeypatch.setattr(em_api.auth, "login", lambda username, password: asyncio.sleep(0, result=("t", "readonly")))
    response = run(em_api._post_login(request({"username": "viewer", "password": "password"})))
    assert json.loads(response.text) == {"token": "t", "role": "readonly"}

    logged_out = []
    monkeypatch.setattr(em_api.auth, "resolve_session", lambda req: asyncio.sleep(0, result={"token": "t"}))
    monkeypatch.setattr(em_api.auth, "logout", lambda token: asyncio.sleep(0, result=logged_out.append(token)))
    assert json.loads(run(em_api._post_logout(request())).text) == {}
    assert logged_out == ["t"]
    monkeypatch.setattr(em_api.auth, "resolve_session", lambda req: asyncio.sleep(0, result=None))
    run(em_api._post_logout(request()))
    assert logged_out == ["t"]


def test_ingress_login_rejects_missing_identity_and_returns_session(monkeypatch):
    monkeypatch.setattr(em_api.em_ingressauth, "decide", lambda **kwargs: None)
    response = run(em_api._post_ingress_login(request()))
    assert response.status == 401

    identity = SimpleNamespace(user_id="ha", username="Alice")
    monkeypatch.setattr(em_api.em_ingressauth, "decide", lambda **kwargs: identity)
    monkeypatch.setattr(em_api.auth, "login_via_ingress", lambda value: asyncio.sleep(0, result=("t", "readonly")))
    response = run(em_api._post_ingress_login(request()))
    assert json.loads(response.text) == {"token": "t", "role": "readonly", "via": "ingress"}


def test_get_me_returns_only_public_user_fields():
    handler = em_api._get_me.__wrapped__
    req = request()
    req.user = {"id": 1, "username": "admin", "role": "admin", "token": "secret"}
    req.__getitem__ = lambda key: req.user
    # aiohttp request storage is mapping-like; use a tiny mapping-shaped object.
    class Request(dict):
        pass
    actual = Request(user=req.user)
    response = run(handler(actual))
    assert json.loads(response.text) == {"id": 1, "username": "admin", "role": "admin"}


def test_user_role_patch_validates_and_protects_last_admin(monkeypatch):
    handler = em_api._patch_user.__wrapped__
    base = {"id": 1, "username": "admin", "role": "admin"}

    bad_role = request({"role": "owner"}, match_info={"id": "1"}, user={"username": "admin"})
    assert run(handler(bad_role)).status == 400
    bad_id = request({"role": "readonly"}, match_info={"id": "x"}, user={"username": "admin"})
    assert run(handler(bad_id)).status == 400

    monkeypatch.setattr(em_api.db, "get_user_by_id", lambda user_id: None)
    missing = request({"role": "readonly"}, match_info={"id": "1"}, user={"username": "admin"})
    assert run(handler(missing)).status == 404

    monkeypatch.setattr(em_api.db, "get_user_by_id", lambda user_id: base)
    same = request({"role": "admin"}, match_info={"id": "1"}, user={"username": "admin"})
    assert json.loads(run(handler(same)).text)["changed"] is False

    monkeypatch.setattr(em_api.db, "admin_count", lambda: 1)
    last = request({"role": "readonly"}, match_info={"id": "1"}, user={"username": "admin"})
    assert run(handler(last)).status == 409

    changed = []
    monkeypatch.setattr(em_api.db, "admin_count", lambda: 2)
    monkeypatch.setattr(em_api.db, "set_user_role", lambda user_id, role: changed.append((user_id, role)))
    response = run(handler(last))
    assert json.loads(response.text)["changed"] is True
    assert changed == [(1, "readonly")]


def test_get_users_omits_password_hash(monkeypatch):
    handler = em_api._get_users.__wrapped__
    monkeypatch.setattr(em_api.db, "get_all_users", lambda: [{
        "id": 1, "username": "admin", "role": "admin", "ha_user_id": None,
        "created_at": "now", "password_hash": "secret",
    }])
    response = run(handler({}))
    assert json.loads(response.text) == [{
        "id": 1, "username": "admin", "role": "admin", "ha_linked": False,
        "created_at": "now",
    }]


def test_global_config_refuses_dropped_keys_and_pushes_effective_values(monkeypatch):
    handler = em_api._post_global_config.__wrapped__
    monkeypatch.setattr(em_api.db, "get_global_device_config_raw", lambda: {"old": 1, "keep": 2})
    dropped = request({"keep": 2})
    assert run(handler(dropped)).status == 409

    saved = []
    pushed = []
    monkeypatch.setattr(em_api.db, "set_global_device_config", lambda config: saved.append(config))
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda device_id: {"device": device_id})
    monkeypatch.setattr(em_api, "_apply_live_config", lambda device_id, live, config: asyncio.sleep(0, result=pushed.append((device_id, config))))
    old_devices = em_api._devices
    em_api._devices = {"dev-1": object(), "dev-2": object()}
    try:
        response = run(handler(request({"keep": 3, "replace": True})))
    finally:
        em_api._devices = old_devices
    assert json.loads(response.text) == {"config": {"keep": 3}, "pushed_to": ["dev-1", "dev-2"]}
    assert saved == [{"keep": 3}]
    assert pushed == [("dev-1", {"device": "dev-1"}), ("dev-2", {"device": "dev-2"})]


def test_live_config_applies_and_clamps_tts_gain(monkeypatch):
    sent = []

    async def send_control(message):
        sent.append(message)

    live = SimpleNamespace(send_control=send_control, led_anim_capable=False)
    monkeypatch.setattr(em_api, "_hold_back_oww_model", lambda live, config: (config, None))
    monkeypatch.setattr(em_api.em_scenes, "resolve", lambda config: {})

    run(em_api._apply_live_config("dev", live, {"ttsGainDb": 99.0}))

    assert sent == [{"type": "config", "ttsGainDb": 99.0}]
    assert live.tts_gain_db == 12.0


def test_device_listing_and_crud_handlers(monkeypatch):
    monkeypatch.setattr(em_api.db, "get_all_devices", lambda: [{"id": 1}])
    monkeypatch.setattr(em_api.db, "get_pending_devices", lambda: [{"id": 2}])
    monkeypatch.setattr(em_api, "_merge_device", lambda row: {"id": row["id"], "live": True})
    assert json.loads(run(em_api._get_devices.__wrapped__(request())).text) == [{"id": 1, "live": True}]
    assert json.loads(run(em_api._get_pending.__wrapped__(request())).text) == [{"id": 2, "live": True}]

    monkeypatch.setattr(em_api.db, "get_device", lambda device_id: None)
    missing = request(match_info={"id": "missing"})
    assert run(em_api._get_device.__wrapped__(missing)).status == 404
    monkeypatch.setattr(em_api.db, "get_device", lambda device_id: {"id": device_id})
    assert json.loads(run(em_api._get_device.__wrapped__(missing)).text) == {"id": "missing", "live": True}

    events = []
    monkeypatch.setattr(em_api, "_push_event", lambda event: asyncio.sleep(0, result=events.append(event)))
    labels = []
    monkeypatch.setattr(em_api.db, "set_device_label", lambda device_id, label: labels.append((device_id, label)))
    patch = request({"label": "Office"}, match_info={"id": "dev"})
    response = run(em_api._patch_device.__wrapped__(patch))
    assert json.loads(response.text) == {"device_id": "dev", "label": "Office"}
    assert labels == [("dev", "Office")]

    deleted = []
    monkeypatch.setattr(em_api.db, "delete_device", lambda device_id: deleted.append(device_id))
    assert json.loads(run(em_api._delete_device.__wrapped__(request(match_info={"id": "dev"}))).text) == {}
    assert deleted == ["dev"]


def test_device_config_handler_validates_scope_and_pushes_effective_config(monkeypatch):
    handler = em_api._post_device_config.__wrapped__
    monkeypatch.setattr(em_api.db, "get_device", lambda device_id: {"device_id": device_id})
    monkeypatch.setattr(em_api.db, "get_device_config_sections", lambda device_id: [])
    monkeypatch.setattr(em_api.db, "get_device_config", lambda device_id: {"owwThreshold": 0.3})
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda device_id: {"owwThreshold": 0.4})
    monkeypatch.setattr(em_api.db, "set_device_config_sections", lambda *args: None)
    monkeypatch.setattr(em_api, "_push_event", lambda event: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_apply_live_config", lambda *args: asyncio.sleep(0))

    invalid = request({"config_sections": "ring"}, match_info={"id": "dev"})
    assert run(handler(invalid)).status == 400
    unknown = request({"config_sections": ["unknown"]}, match_info={"id": "dev"})
    assert run(handler(unknown)).status == 400
    dropped = request({"config_sections": [], "owwThreshold": 0.5}, match_info={"id": "dev"})
    # No device sections means the wake-word key is out of scope and therefore
    # does not trigger a dropped-key check.
    response = run(handler(dropped))
    assert response.status == 200
