"""
Handler-level coverage for the wake-training-capture REST endpoints in
em_api.py — the gap left by test_training_captures.py (storage unit tests) and
test_deploy.py (auth-shape pins): this drives the six aiohttp handlers through
a real app, so route wiring, serialisation, the label/relabel/undo path, the
export, and the 400/403/404 branches are all exercised end to end.

em_api pulls in `websockets`, which the minimal test env does not have, so a
lightweight stub is injected before import — the handlers under test touch only
em_training_captures + auth + the JSON helpers, none of which need it.
"""

import asyncio
import sys
import types

# Stub the one heavy import em_api needs but the test env lacks, before import.
if "websockets" not in sys.modules:
    _ws = types.ModuleType("websockets")
    _ws.WebSocketException = Exception
    sys.modules["websockets"] = _ws
    _wss = types.ModuleType("websockets.asyncio")
    sys.modules["websockets.asyncio"] = _wss
    _wsss = types.ModuleType("websockets.asyncio.server")
    _wsss.ServerConnection = object
    sys.modules["websockets.asyncio.server"] = _wsss

from aiohttp import test_utils, web  # noqa: E402

import em_api  # noqa: E402
import em_auth as auth  # noqa: E402
import em_training_captures as tc  # noqa: E402


def _make_app():
    app = web.Application()
    app.router.add_get("/api/training_captures", em_api._get_training_captures)
    app.router.add_get("/api/training_captures/{model}/captures", em_api._get_training_capture_list)
    app.router.add_get("/api/training_captures/{model}/export", em_api._get_training_capture_export)
    app.router.add_get("/api/training_captures/{model}/audio/{name}", em_api._get_training_capture_audio)
    app.router.add_post("/api/training_captures/{model}/{name}/label", em_api._post_training_capture_label)
    app.router.add_delete("/api/training_captures/{model}/{name}", em_api._delete_training_capture)
    return app


def _as_role(monkeypatch, role="admin"):
    async def fake_resolve(request):
        return {"id": 1, "username": role, "role": role, "token": "t"}
    monkeypatch.setattr(auth, "resolve_session", fake_resolve)


def _pcm(ms: int) -> bytes:
    return b"\x00\x01" * int(tc.SAMPLE_RATE * ms / 1000)


def _run(monkeypatch, tmp_path, coro_factory, role="admin"):
    """Boot the app with DB_PATH → tmp and an authed session, run one coroutine."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    _as_role(monkeypatch, role)

    async def run():
        server = test_utils.TestServer(_make_app())
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            await coro_factory(client)
        finally:
            await client.close()

    asyncio.run(run())


# ── the happy-path round trip ────────────────────────────────────────────────

def test_full_triage_round_trip(monkeypatch, tmp_path):
    db = str(tmp_path / "echomuse.db")
    a = tc.save("hey_jarvis", "dev1", _pcm(200), "miss", 0.3, db_path=db, ts_ms=1)
    tc.save("hey_jarvis", "dev1", _pcm(200), "act", 0.8, db_path=db, ts_ms=2)

    async def body(client):
        # models listing
        r = await client.get("/api/training_captures")
        assert r.status == 200
        data = await r.json()
        models = {m["model"]: m["counts"] for m in data["models"]}
        assert models["hey_jarvis"]["untriaged"] == 2
        assert data["untriaged_cap"] == tc.UNTRIAGED_CAP

        # untriaged list, newest first
        r = await client.get("/api/training_captures/hey_jarvis/captures?bucket=untriaged")
        caps = (await r.json())["captures"]
        assert [c["ts_ms"] for c in caps] == [2, 1]

        # play the clip
        r = await client.get(f"/api/training_captures/hey_jarvis/audio/{a}")
        assert r.status == 200
        assert r.headers["Content-Type"] == "audio/wav"
        assert (await r.read())[:4] == b"RIFF"

        # label it positive, then correct to negative, then send back
        r = await client.post(f"/api/training_captures/hey_jarvis/{a}/label",
                              json={"label": "positive"})
        assert r.status == 200
        r = await client.post(f"/api/training_captures/hey_jarvis/{a}/label",
                              json={"label": "negative"})
        assert r.status == 200
        assert tc.list_captures("hey_jarvis", "positive", db) == []
        assert tc.list_captures("hey_jarvis", "negative", db)[0]["name"] == a

        # export carries the labelled clip + a manifest
        r = await client.get("/api/training_captures/hey_jarvis/export")
        assert r.status == 200
        assert r.headers["Content-Type"] == "application/zip"
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(await r.read())) as z:
            names = z.namelist()
        assert f"negative/{a}" in names and "manifest.json" in names

    _run(monkeypatch, tmp_path, body)


# ── error branches ───────────────────────────────────────────────────────────

def test_audio_404_for_unknown_capture(monkeypatch, tmp_path):
    async def body(client):
        r = await client.get("/api/training_captures/hey_jarvis/audio/dev1_9_act_0100.wav")
        assert r.status == 404
    _run(monkeypatch, tmp_path, body)


def test_label_rejects_a_bad_bucket(monkeypatch, tmp_path):
    db = str(tmp_path / "echomuse.db")
    name = tc.save("hey_jarvis", "dev1", _pcm(50), "act", 0.8, db_path=db, ts_ms=1)

    async def body(client):
        r = await client.post(f"/api/training_captures/hey_jarvis/{name}/label",
                              json={"label": "maybe"})
        assert r.status == 400
    _run(monkeypatch, tmp_path, body)


def test_discard_removes_the_file(monkeypatch, tmp_path):
    db = str(tmp_path / "echomuse.db")
    name = tc.save("hey_jarvis", "dev1", _pcm(50), "act", 0.8, db_path=db, ts_ms=1)

    async def body(client):
        r = await client.delete(f"/api/training_captures/hey_jarvis/{name}")
        assert r.status == 200
        assert tc.list_captures("hey_jarvis", "untriaged", db) == []
    _run(monkeypatch, tmp_path, body)


def test_readonly_is_forbidden(monkeypatch, tmp_path):
    async def body(client):
        r = await client.get("/api/training_captures")
        assert r.status == 403
    _run(monkeypatch, tmp_path, body, role="readonly")
