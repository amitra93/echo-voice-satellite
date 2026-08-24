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
    async def json_body():
        return body

    return SimpleNamespace(json=json_body, headers={}, remote=None, **kwargs)


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
