"""Connect-time on-device wake-model reconciliation."""

import asyncio
import sys
import types
from pathlib import Path

if "websockets" not in sys.modules:
    ws = types.ModuleType("websockets")
    ws.WebSocketException = Exception
    sys.modules["websockets"] = ws
    sys.modules["websockets.asyncio"] = types.ModuleType("websockets.asyncio")
    server = types.ModuleType("websockets.asyncio.server")
    server.ServerConnection = object
    sys.modules["websockets.asyncio.server"] = server

import em_api
import em_oww_assets


class Live:
    def __init__(self):
        self.capabilities = ["wake_request_v1"]
        self.oww_model_ready = True
        self.controls = []

    async def send_control(self, message):
        self.controls.append(message)


def _asset():
    return em_oww_assets.Asset(
        "selected.onnx", Path("/selected.onnx"), "a" * 32, 1, "classifier"
    )


def test_incomplete_inventory_never_stands_a_device_down(monkeypatch):
    live = Live()
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda _id: {
        "owwModel": "selected",
    })
    monkeypatch.setattr(em_api, "_oww_wanted_models", lambda _id: ["selected"])
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets", lambda _models: ([_asset()], []))
    monkeypatch.setattr(em_api, "_oww_device_state", lambda _live: asyncio.sleep(
        0, result={"inventory_ok": False, "installed": {}, "free_mb": None}
    ))
    synced = []
    monkeypatch.setattr(em_api, "_sync_oww_assets", lambda *_args: synced.append(True))

    asyncio.run(em_api.reconcile_oww_assets("device-1", live))

    assert live.oww_model_ready is True
    assert not live.controls
    assert not synced


def test_missing_selected_md5_stands_down_then_restores_after_repair(monkeypatch):
    live = Live()
    monkeypatch.setitem(em_api._devices, "device-1", live)
    monkeypatch.setattr(em_api.db, "get_effective_device_config", lambda _id: {
        "owwModel": "selected",
    })
    monkeypatch.setattr(em_api, "_oww_wanted_models", lambda _id: ["selected"])
    monkeypatch.setattr(em_api.em_oww_assets, "desired_assets", lambda _models: ([_asset()], []))
    monkeypatch.setattr(em_api, "_oww_device_state", lambda _live: asyncio.sleep(
        0, result={"inventory_ok": True, "installed": {}, "free_mb": None}
    ))
    states_during_sync = []

    async def sync(_live, _device_id):
        states_during_sync.append(live.oww_model_ready)
        return {"ok": True}

    async def log(*_args):
        pass

    monkeypatch.setattr(em_api, "_sync_oww_assets", sync)
    monkeypatch.setattr(em_api, "_push_log_event", log)
    try:
        asyncio.run(em_api.reconcile_oww_assets("device-1", live))
    finally:
        em_api._devices.pop("device-1", None)

    assert states_during_sync == [False]
    assert live.oww_model_ready is False
    assert live.controls == [{"type": "config", "owwModel": "selected"}]
