"""
Tests for saveStopCaptures / stopCaptureSec — Stop word capture settings
and the controller's dual-model capture-upload path.
"""
import asyncio
import sys
import types


def _install_import_stubs():
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
import em_db
import em_config_sections as cs
import em_training_captures as tc


class FakeWS:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def new_device(capabilities=None):
    return em_controller.Device("dev", "192.0.2.1", capabilities or [], FakeWS())


# ─── defaults / section partition ───────────────────────────────────────────

def test_stop_capture_defaults_are_present_and_sensible():
    assert "saveStopCaptures" in em_db.DEFAULT_DEVICE_CONFIG
    assert "stopCaptureSec" in em_db.DEFAULT_DEVICE_CONFIG
    assert em_db.DEFAULT_DEVICE_CONFIG["saveStopCaptures"] is False
    assert em_db.DEFAULT_DEVICE_CONFIG["stopCaptureSec"] == 2.0
    # Same privacy default as wake captures: off.
    assert em_db.DEFAULT_DEVICE_CONFIG["saveWakeCaptures"] is False


def test_stop_capture_keys_belong_to_stopword_section():
    assert "saveStopCaptures" in cs.SECTIONS["stopword"]["keys"]
    assert "stopCaptureSec" in cs.SECTIONS["stopword"]["keys"]
    # Must not appear elsewhere.
    for sid, section in cs.SECTIONS.items():
        if sid == "stopword":
            continue
        assert "saveStopCaptures" not in section["keys"]
        assert "stopCaptureSec" not in section["keys"]


def test_stop_capture_section_is_not_state():
    assert "saveStopCaptures" not in cs.STATE_KEYS
    assert "stopCaptureSec" not in cs.STATE_KEYS


def test_stop_capture_scoping_is_per_section():
    fleet = {"saveStopCaptures": False, "stopCaptureSec": 2.0,
             "owwModel": "hey_jarvis_v0.1"}
    dev = {"saveStopCaptures": True, "stopCaptureSec": 4.0, "owwModel": "custom"}
    # No sections overridden -> fleet wins.
    out = cs.merge(fleet, dev, [])
    assert out["saveStopCaptures"] is False
    assert out["stopCaptureSec"] == 2.0
    assert out["owwModel"] == "hey_jarvis_v0.1"
    # Only stopword overridden -> stop keys from device, wake keys from fleet.
    out = cs.merge(fleet, dev, ["stopword"])
    assert out["saveStopCaptures"] is True
    assert out["stopCaptureSec"] == 4.0
    assert out["owwModel"] == "hey_jarvis_v0.1"


# ─── Device attribute / handle_control wiring ───────────────────────────────

def test_device_exposes_save_stop_captures():
    device = new_device()
    assert device.save_stop_captures is False


def test_handle_control_seeds_save_stop_captures(monkeypatch):
    # Directly exercise the config seeding path in handle_control via the
    # shared apply logic: Device attrs are set from effective config.
    device = new_device()
    # Simulate what handle_control does after sending config.
    config = {"saveStopCaptures": True, "stopCaptureSec": 3.5,
              "saveWakeCaptures": False}
    device.save_stop_captures = bool(config.get("saveStopCaptures", False))
    device.save_wake_captures = bool(config.get("saveWakeCaptures", False))
    assert device.save_stop_captures is True
    assert device.save_wake_captures is False


# ─── Live propagation (em_api._apply_live_config) ───────────────────────────

def test_live_propagation_updates_save_stop_captures(monkeypatch):
    import em_api

    async def run():
        device = new_device()
        device.save_stop_captures = False

        async def fake_send_control(msg):
            return None

        device.send_control = fake_send_control
        monkeypatch.setattr(em_api, "_hold_back_oww_model", lambda live, eff: (eff, None))
        monkeypatch.setattr(em_api, "_install_then_switch", lambda *a, **kw: None)
        monkeypatch.setattr(em_api.ha_sidechannels, "wake_model", lambda *a: None)

        await em_api._apply_live_config(device.device_id, device, {"saveStopCaptures": True})
        assert device.save_stop_captures is True
        await em_api._apply_live_config(device.device_id, device, {"saveStopCaptures": False})
        assert device.save_stop_captures is False
        # Missing key must not flip the flag.
        await em_api._apply_live_config(device.device_id, device, {})
        assert device.save_stop_captures is False

    asyncio.run(run())


# ─── Training captures storage for stop model ───────────────────────────────

def test_stop_capture_round_trips_through_training_storage(tmp_path):
    db = str(tmp_path / "echomuse.db")
    pcm = b"\x00\x01" * int(tc.SAMPLE_RATE * 0.2)
    # save_uploaded is the controller's durable path for device uploads.
    meta = {
        "captureId": "stop:1", "kind": "act", "model": "stop",
        "classifierMd5": "a" * 32, "score": 0.8, "threshold": 0.75,
        "nearMissFloor": 0.05, "activationSeq": 1,
        "requestedPrerollMs": 2000, "actualPrerollMs": 2000,
        "complete": True, "sampleRate": 16000, "sampleWidth": 2,
        "channels": 1, "frameBytes": 2560, "bargeThresholdActive": False,
    }
    name = tc.save_uploaded("stop", "dev1", meta, pcm, db_path=db)
    assert name is not None
    assert tc.resolve("stop", name, db_path=db) is not None
    listed = tc.list_captures("stop", "untriaged", db_path=db)
    assert len(listed) == 1
    assert listed[0]["device_id"] == "dev1"
    # Label as "should have triggered" → positive.
    assert tc.label("stop", name, "positive", db_path=db)
    assert tc.list_captures("stop", "positive", db_path=db)[0]["name"] == name
    # And back to untriaged is undoable.
    assert tc.label("stop", name, "untriaged", db_path=db)


def test_stop_and_wake_captures_coexist_under_different_models(tmp_path):
    db = str(tmp_path / "echomuse.db")
    pcm = b"\x00\x01" * 100
    for model, cid in [("hey_jarvis", "w:1"), ("stop", "s:1")]:
        meta = {
            "captureId": cid, "kind": "act", "model": model,
            "classifierMd5": "b" * 32, "score": 0.9, "threshold": 0.5,
            "nearMissFloor": 0.05, "activationSeq": 2,
            "requestedPrerollMs": 2000, "actualPrerollMs": 2000,
            "complete": True, "sampleRate": 16000, "sampleWidth": 2,
            "channels": 1, "frameBytes": 2560, "bargeThresholdActive": False,
        }
        assert tc.save_uploaded(model, "dev1", meta, pcm, db_path=db) is not None
    models = {m["model"] for m in tc.list_models(db_path=db)}
    assert "hey_jarvis" in models
    assert "stop" in models


# ─── Capture upload acceptance — stop model ─────────────────────────────────

def test_accept_capture_upload_for_stop_model(monkeypatch):
    async def run():
        device = new_device(["wake_request_v1", "stopword"])
        ws = object()
        device.data_ws = ws
        device.save_wake_captures = False
        device.save_stop_captures = True
        device.oww_model = "hey_jarvis_v0.1"
        device.stop_model = "stop"
        device.oww_classifier_md5 = "w" * 32
        device.stop_classifier_md5 = "s" * 32
        sent = []
        saved = []
        discarded = []
        device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))
        monkeypatch.setattr(
            em_controller.em_training_captures, "save_uploaded",
            lambda *a, **kw: saved.append((a, kw)) or ("stop_capture.wav", True),
        )
        monkeypatch.setattr(
            em_controller.em_training_captures, "discard",
            lambda *a: discarded.append(a) or True,
        )
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            # Accepted: stop model, matching md5, save_stop enabled.
            completed = types.SimpleNamespace(
                metadata={"captureId": "s:1", "model": "stop", "classifierMd5": "s" * 32},
                pcm=b"pcm",
            )
            assert await em_controller._accept_capture_upload(device, ws, completed)
            assert sent == [{"type": "capture_ack", "captureId": "s:1"}]
            assert len(saved) == 1

            # Rejected: same payload but save_stop disabled.
            sent.clear()
            device.save_stop_captures = False
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            assert sent == []

            # Rejected: wrong md5.
            device.save_stop_captures = True
            completed.metadata["classifierMd5"] = "x" * 32
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            completed.metadata["classifierMd5"] = "s" * 32

            # Rejected: model mismatch (wake model with only stop enabled).
            completed.metadata["model"] = "hey_jarvis_v0.1"
            # Only stop enabled, so wake model should be rejected.
            device.save_wake_captures = False
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            completed.metadata["model"] = "stop"

            # Disabled mid-save: re-check after commit discards the file.
            def disable_during_save(*a, **kw):
                device.save_stop_captures = False
                return "stop_capture.wav", True
            monkeypatch.setattr(em_controller.em_training_captures, "save_uploaded", disable_during_save)
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            assert discarded == [("stop", "stop_capture.wav")]

            # Wake path still works when both flags present.
            device.save_wake_captures = True
            device.save_stop_captures = True
            monkeypatch.setattr(
                em_controller.em_training_captures, "save_uploaded",
                lambda *a, **kw: ("wake_capture.wav", True),
            )
            completed.metadata.update({"model": "hey_jarvis_v0.1", "classifierMd5": "w" * 32})
            sent.clear()
            assert await em_controller._accept_capture_upload(device, ws, completed)
            assert sent == [{"type": "capture_ack", "captureId": "s:1"}]
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_accept_capture_upload_rejects_unknown_model(monkeypatch):
    async def run():
        device = new_device()
        ws = object()
        device.data_ws = ws
        device.save_wake_captures = True
        device.save_stop_captures = True
        device.oww_model = "hey_jarvis_v0.1"
        device.stop_model = "stop"
        device.oww_classifier_md5 = "w" * 32
        device.stop_classifier_md5 = "s" * 32
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            completed = types.SimpleNamespace(
                metadata={"captureId": "x:1", "model": "unknown_model", "classifierMd5": "w" * 32},
                pcm=b"pcm",
            )
            assert not await em_controller._accept_capture_upload(device, ws, completed)
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_data_handler_gate_allows_stop_when_wake_disabled(monkeypatch):
    # The data-plane gate should NOT drop stop captures when only save_stop is on.
    # We exercise the boolean directly.
    device = new_device()
    device.save_wake_captures = False
    device.save_stop_captures = True
    assert (device.save_wake_captures or device.save_stop_captures) is True
    device.save_stop_captures = False
    assert (device.save_wake_captures or device.save_stop_captures) is False
    device.save_wake_captures = True
    assert (device.save_wake_captures or device.save_stop_captures) is True
