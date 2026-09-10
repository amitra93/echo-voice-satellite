"""Handler + delivery tests for the alarm REST endpoints and fire path.

em_api pulls in `websockets`, absent from the minimal test env, so a stub is
injected before import (same pattern as test_training_captures_api). These
drive the CRUD handlers through a real aiohttp app and exercise fire_alarm /
_deliver_finished directly for the timer/alarm parity that matters most.
"""

import asyncio
import sys
import types

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
import em_db as db  # noqa: E402
import em_timers  # noqa: E402


def _make_app():
    app = web.Application()
    app.router.add_get("/api/alarms", em_api._get_all_alarms)
    app.router.add_get("/api/devices/{id}/alarms", em_api._get_device_alarms)
    app.router.add_post("/api/devices/{id}/alarms", em_api._post_alarm)
    app.router.add_delete("/api/devices/{id}/alarms/{aid}", em_api._delete_alarm)
    return app


def _run(tmp_path, coro_factory):
    db.init(str(tmp_path / "echomuse.db"))
    db.register_new_device("dev1", "1.2.3.4", "v2.12.0")

    async def run():
        server = test_utils.TestServer(_make_app())
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            await coro_factory(client)
        finally:
            await client.close()

    try:
        asyncio.run(run())
    finally:
        if db._conn is not None:
            db._conn.close()
            db._conn = None


def test_crud_round_trip(tmp_path):
    async def body(client):
        # create
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 7, "minute": 30, "recurrence": "weekly",
            "weekday_mask": 0b0011111, "tz": "America/New_York", "label": "gym",
        })
        assert r.status == 201
        alarm = (await r.json())["alarm"]
        aid = alarm["id"]
        assert alarm["hour"] == 7 and alarm["enabled"] is True
        assert alarm["next_fire_utc"] is not None    # scheduled on create

        # list (device + fleet)
        r = await client.get("/api/devices/dev1/alarms")
        assert {a["id"] for a in (await r.json())["alarms"]} == {aid}
        r = await client.get("/api/alarms")
        assert {a["id"] for a in (await r.json())["alarms"]} == {aid}

        # Alarms are immutable. Recreate one instead of mutating its schedule.
        r = await client.patch(f"/api/devices/dev1/alarms/{aid}", json={"enabled": False})
        assert r.status == 405

        # delete
        r = await client.delete(f"/api/devices/dev1/alarms/{aid}")
        assert r.status == 200 and (await r.json())["deleted"] is True
        r = await client.get("/api/devices/dev1/alarms")
        assert (await r.json())["alarms"] == []

    _run(tmp_path, body)


def test_create_rejects_bad_spec(tmp_path):
    async def body(client):
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 25, "minute": 0, "recurrence": "daily", "tz": "UTC",
        })
        assert r.status == 400
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 7, "minute": 0, "recurrence": "weekly", "weekday_mask": 0, "tz": "UTC",
        })
        assert r.status == 400
        r = await client.post("/api/devices/dev1/alarms", json={"minute": 0})
        assert r.status == 400

    _run(tmp_path, body)


def test_create_once_without_date_resolves_to_next_occurrence(tmp_path):
    async def body(client):
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 7, "minute": 0, "recurrence": "once", "tz": "UTC",
        })
        assert r.status == 201
        alarm = (await r.json())["alarm"]
        assert alarm["date"] is not None       # resolved server-side
        assert alarm["next_fire_utc"] is not None

    _run(tmp_path, body)


def test_create_uses_device_timezone_default(tmp_path):
    async def body(client):
        db.set_device_timezone("dev1", "Europe/London")
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 6, "minute": 0, "recurrence": "daily",
        })
        assert (await r.json())["alarm"]["tz"] == "Europe/London"

    _run(tmp_path, body)


def test_delete_rejects_foreign_device(tmp_path):
    async def body(client):
        r = await client.post("/api/devices/dev1/alarms", json={
            "hour": 7, "minute": 0, "recurrence": "daily", "tz": "UTC"})
        aid = (await r.json())["alarm"]["id"]
        db.register_new_device("dev2", "1.2.3.5", "v2.12.0")
        r = await client.delete(f"/api/devices/dev2/alarms/{aid}")
        assert r.status == 404

    _run(tmp_path, body)


def test_create_unknown_device_404(tmp_path):
    async def body(client):
        r = await client.post("/api/devices/ghost/alarms", json={
            "hour": 7, "minute": 0, "recurrence": "daily", "tz": "UTC"})
        assert r.status == 404

    _run(tmp_path, body)


# ── fire_alarm / _deliver_finished parity ────────────────────────────────────

class _FakeDevice:
    def __init__(self, muted=False):
        self.muted = muted


class _FakeRunner:
    def __init__(self):
        self.started = []

    def start(self, device_id, session):
        self.started.append(device_id)
        return True


def _fire_harness(monkeypatch, device, muted=False):
    events = []

    async def fake_push(event):
        events.append(event)

    monkeypatch.setattr(em_api, "_push_event", fake_push)
    em_api._timer_sessions.clear()
    em_api._devices.clear()
    if device is not None:
        em_api._devices["dev"] = device
    runner = _FakeRunner()
    monkeypatch.setattr(em_api, "_timer_alarm_runner", runner)
    return events, runner


def test_fire_alarm_rings_present_unmuted_device(monkeypatch):
    events, runner = _fire_harness(monkeypatch, _FakeDevice(muted=False))
    outcome = asyncio.run(em_api.fire_alarm("dev", "alarm:a1:2026-06-01", "gym"))
    assert outcome == "ringing"
    assert runner.started == ["dev"]
    assert any(e["type"] == "timer.alarm" and e["state"] == "ringing" for e in events)


def test_fire_alarm_muted_device_is_discarded(monkeypatch):
    events, runner = _fire_harness(monkeypatch, _FakeDevice(muted=True))
    outcome = asyncio.run(em_api.fire_alarm("dev", "alarm:a1:2026-06-01", "gym"))
    assert outcome == "muted"
    assert runner.started == []              # no ring
    assert any(e["type"] == "timer.alarm" and e["state"] == "idle" for e in events)


def test_fire_alarm_offline_device_is_discarded(monkeypatch):
    events, runner = _fire_harness(monkeypatch, None)
    outcome = asyncio.run(em_api.fire_alarm("dev", "alarm:a1:2026-06-01", "gym"))
    assert outcome == "offline"
    assert runner.started == []


def test_recurring_alarm_two_days_are_distinct_ids_not_deduped(monkeypatch):
    # The whole point of the per-occurrence id: yesterday's ring must not
    # suppress today's via AlarmSession's fingerprint dedup.
    events, runner = _fire_harness(monkeypatch, _FakeDevice(muted=False))
    o1 = asyncio.run(em_api.fire_alarm("dev", "alarm:a1:2026-06-01", "gym"))
    # first alarm still ringing; dismiss it to clear current
    session = em_api._timer_sessions["dev"]
    session.dismiss_all()
    o2 = asyncio.run(em_api.fire_alarm("dev", "alarm:a1:2026-06-02", "gym"))
    assert o1 == "ringing" and o2 == "ringing"
    assert runner.started == ["dev", "dev"]
