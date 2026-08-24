import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import em_auth


def run(awaitable):
    return asyncio.run(awaitable)


def test_password_helpers_and_async_wrappers(monkeypatch):
    hashed = em_auth.hash_password("correct horse")
    assert em_auth.verify_password("correct horse", hashed)
    assert not em_auth.verify_password("wrong", hashed)
    assert not em_auth.verify_password("anything", "not-a-bcrypt-hash")

    monkeypatch.setattr(em_auth, "hash_password", lambda password: "hashed:" + password)
    monkeypatch.setattr(em_auth, "verify_password", lambda password, hashed: password == hashed)
    assert run(em_auth.hash_password_async("x")) == "hashed:x"
    assert run(em_auth.verify_password_async("x", "x"))
    assert not run(em_auth.verify_password_async("x", "y"))


def test_token_and_request_extraction():
    token = em_auth.generate_token()
    assert len(token) == em_auth.TOKEN_BYTES * 2
    assert all(c in "0123456789abcdef" for c in token)

    header = make_mocked_request("GET", "/", headers={"Authorization": "bEaReR  token "})
    assert em_auth._extract_token(header) == "token"
    cookie = make_mocked_request("GET", "/", headers={"Cookie": "session=cookie-token"})
    assert em_auth._extract_token(cookie) == "cookie-token"
    empty = make_mocked_request("GET", "/", headers={"Authorization": "Basic abc"})
    assert em_auth._extract_token(empty) is None


def test_login_success_and_failures(monkeypatch):
    user = {"id": 7, "username": "admin", "role": "admin", "password_hash": "hash"}
    sessions = []
    monkeypatch.setattr(em_auth.db, "get_user_by_username", lambda name: user if name == "admin" else None)
    monkeypatch.setattr(em_auth, "verify_password_async", lambda password, hashed: asyncio.sleep(0, result=password == "secret"))
    monkeypatch.setattr(em_auth, "generate_token", lambda: "session-token")
    monkeypatch.setattr(em_auth.db, "get_config", lambda name, default=None: "14" if name == "session_expiry_days" else default)
    monkeypatch.setattr(em_auth.db, "create_session", lambda token, user_id, days: sessions.append((token, user_id, days)))

    assert run(em_auth.login("admin", "secret")) == ("session-token", "admin")
    assert sessions == [("session-token", 7, 14)]
    with pytest.raises(em_auth.AuthError) as wrong:
        run(em_auth.login("admin", "bad"))
    assert wrong.value.code == "invalid_credentials"
    with pytest.raises(em_auth.AuthError) as missing:
        run(em_auth.login("missing", "bad"))
    assert missing.value.status == 401


def test_ingress_login_provisions_first_user_and_preserves_existing_role(monkeypatch):
    identity = SimpleNamespace(user_id="ha-1", username="Alice")
    created = []
    monkeypatch.setattr(em_auth, "generate_token", lambda: "ingress-token")
    monkeypatch.setattr(em_auth.db, "get_config", lambda name, default=None: default)
    monkeypatch.setattr(em_auth.db, "create_session", lambda *args: created.append(args))
    monkeypatch.setattr(em_auth.db, "get_user_by_ha_id", lambda user_id: None)
    monkeypatch.setattr(em_auth.db, "user_count", lambda: 0)
    monkeypatch.setattr(em_auth.db, "create_ha_user", lambda *args: 9)
    monkeypatch.setattr(em_auth.db, "get_user_by_id", lambda user_id: {"id": 9, "username": "Alice", "role": "admin"})
    assert run(em_auth.login_via_ingress(identity)) == ("ingress-token", "admin")
    assert created == [("ingress-token", 9, 30)]

    existing = {"id": 9, "username": "Alice", "role": "readonly"}
    monkeypatch.setattr(em_auth.db, "get_user_by_ha_id", lambda user_id: existing)
    assert run(em_auth.login_via_ingress(identity)) == ("ingress-token", "readonly")


def test_resolve_session_caches_and_handles_missing_user(monkeypatch):
    request = make_mocked_request("GET", "/", headers={"Authorization": "Bearer session"})
    calls = []
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: {"user_id": 4})
    monkeypatch.setattr(em_auth.db, "get_user_by_id", lambda user_id: calls.append(user_id) or {"id": 4, "username": "u", "role": "readonly"})
    first = run(em_auth.resolve_session(request))
    assert first["token"] == "session"
    assert run(em_auth.resolve_session(request)) is first
    assert calls == [4]

    missing = make_mocked_request("GET", "/", headers={"Authorization": "Bearer missing"})
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: {"user_id": 99})
    monkeypatch.setattr(em_auth.db, "get_user_by_id", lambda user_id: None)
    assert run(em_auth.resolve_session(missing)) is None


def test_ws_resolution_uses_query_and_rejects_empty_api_key(monkeypatch):
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: None)
    monkeypatch.setattr(em_auth.db, "get_config", lambda name, default=None: "key" if name == "ha_api_key" else default)
    request = make_mocked_request("GET", "/events?token=bad")
    assert run(em_auth.ws_resolve_session(request)) is None
    request = make_mocked_request("GET", "/events")
    assert run(em_auth.ws_resolve_session(request)) is None


def test_access_decorators_return_auth_and_role_errors(monkeypatch):
    async def handler(request):
        return request["user"]["role"]

    monkeypatch.setattr(em_auth, "resolve_session", lambda request: asyncio.sleep(0, result=None))
    assert run(em_auth.require_auth(handler)(make_mocked_request("GET", "/"))).status == 401
    assert run(em_auth.require_admin(handler)(make_mocked_request("GET", "/"))).status == 401

    monkeypatch.setattr(em_auth, "resolve_session", lambda request: asyncio.sleep(0, result={"role": "readonly"}))
    assert run(em_auth.require_admin(handler)(make_mocked_request("GET", "/"))).status == 403
    assert run(em_auth.require_integration_or_admin(handler)(make_mocked_request("GET", "/"))).status == 403

    monkeypatch.setattr(em_auth, "resolve_session", lambda request: asyncio.sleep(0, result={"role": "admin"}))
    assert run(em_auth.require_auth(handler)(make_mocked_request("GET", "/"))) == "admin"
    assert run(em_auth.require_admin(handler)(make_mocked_request("GET", "/"))) == "admin"
    assert run(em_auth.require_integration_or_admin(handler)(make_mocked_request("GET", "/"))) == "admin"


def test_bootstrap_validation_and_first_admin(monkeypatch):
    monkeypatch.setattr(em_auth.db, "user_count", lambda: 0)
    token = em_auth.maybe_generate_bootstrap_token()
    assert token == em_auth.get_bootstrap_token()
    with pytest.raises(em_auth.AuthError) as invalid:
        run(em_auth.create_first_admin("wrong", "admin", "long-password"))
    assert invalid.value.code == "invalid_token"
    monkeypatch.setattr(em_auth, "hash_password_async", lambda password: asyncio.sleep(0, result="hashed"))
    created = []
    monkeypatch.setattr(em_auth.db, "create_user", lambda *args: created.append(args))
    run(em_auth.create_first_admin(token, "admin", "long-password"))
    assert created == [("admin", "hashed", "admin")]
    assert em_auth.get_bootstrap_token() is None

    for username, password in [("", "long-password"), ("a", "long-password"), ("admin", "short")]:
        with pytest.raises(em_auth.AuthError):
            em_auth._validate_credentials(username, password)


def test_auth_error_response_and_logout(monkeypatch):
    error = em_auth.AuthError("bad", "Nope", 418)
    response = error.to_response()
    assert response.status == 418
    assert '"code": "bad"' in response.text
    deleted = []
    monkeypatch.setattr(em_auth.db, "delete_session", lambda token: deleted.append(token))
    run(em_auth.logout("token"))
    assert deleted == ["token"]
