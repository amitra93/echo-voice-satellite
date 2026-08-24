import asyncio
import json
import sqlite3
import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("websockets", types.ModuleType("websockets"))

import em_api


def run(awaitable):
    return asyncio.run(awaitable)


def request(body=None, *, match_info=None, query=None):
    class Request(dict):
        async def json(self):
            return body

    result = Request()
    result.update({"match_info": match_info or {}, "query": query or {}, "headers": {}})
    result.match_info = result["match_info"]
    result.query = result["query"]
    result.headers = result["headers"]
    result.remote = None
    return result


def test_provision_payload_handlers_and_magisk_database():
    start = run(em_api._get_provision_start_script.__wrapped__(request()))
    debloat = run(em_api._get_provision_debloat_script.__wrapped__(request()))
    packages = run(em_api._get_provision_debloat_packages.__wrapped__(request()))
    assert start.content_type == "text/plain" and "#!/system/bin/sh" in start.text
    assert debloat.content_type == "text/plain" and "#!/system/bin/sh" in debloat.text
    parsed = json.loads(packages.text)
    assert parsed["packages"] and all(not p.startswith("#") for p in parsed["packages"])

    response = run(em_api._get_provision_magisk_db.__wrapped__(request()))
    con = sqlite3.connect(":memory:")
    # The response is a real SQLite file; inspect it through a temporary file.
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        f.write(response.body)
        f.flush()
        db = sqlite3.connect(f.name)
        rows = db.execute("SELECT uid, policy FROM policies ORDER BY uid").fetchall()
        db.close()
    con.close()
    assert rows == [(0, 2), (2000, 2)]


def test_tls_credential_provisioning_requires_tls_but_mints_token(monkeypatch):
    monkeypatch.setattr(em_api.db, "ensure_device_token", lambda device_id: "token")
    monkeypatch.setattr(em_api, "_tls_dir", None)
    response = run(em_api._post_provision_tls_credentials.__wrapped__(request({"device_id": "dev"})))
    assert response.status == 503

    monkeypatch.setattr(em_api, "_tls_dir", "/tmp/tls")
    monkeypatch.setattr(em_api.em_pki, "ca_pem", lambda path: "CA")
    response = run(em_api._post_provision_tls_credentials.__wrapped__(request({"device_id": "dev"})))
    assert json.loads(response.text) == {"ca_pem": "CA", "token": "token", "dir": em_api.DEVICE_TLS_DIR}


def test_training_capture_handlers_validate_and_forward(monkeypatch):
    admin = {"role": "admin"}
    monkeypatch.setattr(em_api.em_training_captures, "safe_model", lambda model: None if model == "bad" else model)
    assert run(em_api._get_training_capture_list.__wrapped__(request(match_info={"model": "bad"}))).status == 400

    monkeypatch.setattr(em_api.em_training_captures, "list_captures", lambda model, bucket: [{"name": "x"}])
    response = run(em_api._get_training_capture_list.__wrapped__(request(match_info={"model": "hey"})))
    assert json.loads(response.text)["captures"] == [{"name": "x"}]

    monkeypatch.setattr(em_api.em_training_captures, "label", lambda *args: True)
    invalid = run(em_api._post_training_capture_label.__wrapped__(request({"label": "bad"}, match_info={"model": "hey", "name": "x"})))
    assert invalid.status == 400
    valid = run(em_api._post_training_capture_label.__wrapped__(request({"label": "positive"}, match_info={"model": "hey", "name": "x"})))
    assert json.loads(valid.text)["label"] == "positive"

    monkeypatch.setattr(em_api.em_training_captures, "discard", lambda *args: False)
    assert run(em_api._delete_training_capture.__wrapped__(request(match_info={"model": "hey", "name": "x"}))).status == 404
    monkeypatch.setattr(em_api.em_training_captures, "discard", lambda *args: True)
    assert run(em_api._delete_training_capture.__wrapped__(request(match_info={"model": "hey", "name": "x"}))).status == 200


def test_device_activity_rolls_up_turns_shadow_counters_and_metrics(monkeypatch):
    now = 1_700_000_000
    turn = {
        "ts": now, "outcome": "ok", "total_ms": 100, "wake_score": 0.5,
        "underruns": 1, "wake_model": "hey", "dev_shadow": 1,
        "dev_threshold": 0.3, "wake_threshold": 0.5,
        "dev_wake_score": 0.4, "dev_wake_delta_ms": -10,
    }
    monkeypatch.setattr(em_api.time, "time", lambda: now + 10)
    monkeypatch.setattr(em_api.db, "get_turns", lambda *args: [turn])
    monkeypatch.setattr(em_api.db, "get_wake_counters", lambda *args: [{
        "dev_crossings": 2, "dev_frames": 10, "dev_drops": 1,
    }])
    monkeypatch.setattr(em_api.db, "get_device_metrics", lambda *args: [{"cpu": 1}])
    response = run(em_api._get_device_activity.__wrapped__(request(match_info={"id": "dev"})))
    data = json.loads(response.text)
    assert data["days"][0]["turns"] == 1
    assert data["wake_models"]["hey"]["score_avg"] == 0.5
    assert data["shadow"]["agreed"] == 1
    assert data["shadow"]["unmatched_crossings"] == 1
    assert data["metrics"] == [{"cpu": 1}]


def test_wifi_and_log_handlers_cover_validation_and_async_scan(monkeypatch):
    class Live:
        def __init__(self):
            self.wifi_scan_future = None
            self.sent = []

        async def send_control(self, msg):
            self.sent.append(msg)
            if msg["type"] == "wifi_scan":
                self.wifi_scan_future.set_result({"networks": [{"ssid": "Home"}]})

    live = Live()
    old = em_api._devices
    em_api._devices = {"dev": live}
    monkeypatch.setattr(em_api, "wifi_state", lambda device_id: {"pending": None, "last_result": None})
    monkeypatch.setattr(em_api.db, "log_device", lambda *args: None)
    monkeypatch.setattr(em_api, "_push_event", lambda *args: asyncio.sleep(0))
    try:
        bad = request({"ssid": "bad\"ssid", "psk": ""}, match_info={"id": "dev"})
        assert run(em_api._post_device_wifi.__wrapped__(bad)).status == 400
        assert run(em_api._post_device_wifi.__wrapped__(request({"ssid": "Home", "psk": "short"}, match_info={"id": "dev"}))).status == 400
        good = run(em_api._post_device_wifi.__wrapped__(request({"ssid": "Home", "psk": "password"}, match_info={"id": "dev"})))
        assert good.status == 202 and live.sent[0]["type"] == "wifi_change"
        monkeypatch.setattr(em_api, "wifi_state", lambda device_id: {"pending": {"ssid": "Other"}, "last_result": None})
        assert run(em_api._post_device_wifi.__wrapped__(request({"ssid": "Home2"}, match_info={"id": "dev"}))).status == 409
        monkeypatch.setattr(em_api, "wifi_state", lambda device_id: {"pending": None, "last_result": None})
        scan = run(em_api._post_device_wifi_scan.__wrapped__(request(match_info={"id": "dev"})))
        assert json.loads(scan.text) == {"networks": [{"ssid": "Home"}]}
        live.send_control = lambda msg: asyncio.sleep(0, result=live.wifi_scan_future.set_result({"error": "scan failed"}))
        failed_scan = run(em_api._post_device_wifi_scan.__wrapped__(request(match_info={"id": "dev"})))
        assert failed_scan.status == 502
        live.wifi_scan_future = object()
        assert run(em_api._post_device_wifi_scan.__wrapped__(request(match_info={"id": "dev"}))).status == 409
        live.wifi_scan_future = None
        em_api._devices = {}
        assert run(em_api._post_device_wifi.__wrapped__(request({"ssid": "Gone"}, match_info={"id": "dev"}))).status == 409
        em_api._devices = {"dev": live}
    finally:
        em_api._devices = old

    class URL:
        def __init__(self, values):
            self.query = values
    log_request = request(match_info={"id": "dev"})
    log_request.rel_url = URL({"limit": "nope"})
    monkeypatch.setattr(em_api.db, "get_device", lambda *args: {"id": "dev"})
    assert run(em_api._get_device_logs.__wrapped__(log_request)).status == 400
    monkeypatch.setattr(em_api.db, "get_device_logs", lambda *args: [{"id": 1, "ts": 2, "level": "info", "source": "device", "message": "ok"}])
    log_request.rel_url = URL({"limit": "1", "before": "100"})
    assert json.loads(run(em_api._get_device_logs.__wrapped__(log_request)).text)[0]["message"] == "ok"


def test_release_and_system_config_handlers(monkeypatch):
    monkeypatch.setattr(em_api, "_get_cached_release", lambda: asyncio.sleep(0, result=None))
    assert run(em_api._get_latest_release.__wrapped__(request())).status == 404
    monkeypatch.setattr(em_api, "_fetch_latest_release", lambda force=False: asyncio.sleep(0, result={"version": "v1"}))
    assert json.loads(run(em_api._post_check_release.__wrapped__(request())).text) == {"version": "v1"}

    stored = []
    monkeypatch.setattr(em_api.db, "get_config", lambda key, default=None: None)
    monkeypatch.setattr(em_api.db, "set_config", lambda key, value: stored.append((key, value)))
    monkeypatch.setattr(em_api.auth, "generate_api_key", lambda: "em_key")
    assert json.loads(run(em_api._post_api_key_generate.__wrapped__(request())).text)["rotated"] is False
    monkeypatch.setattr(em_api.db, "get_config", lambda key, default=None: "existing")
    assert run(em_api._post_api_key_generate.__wrapped__(request())).status == 409
    monkeypatch.setattr(em_api.db, "get_config", lambda key, default=None: None)
    assert json.loads(run(em_api._post_api_key_rotate.__wrapped__(request())).text)["rotated"] is True
    assert json.loads(run(em_api._delete_api_key.__wrapped__(request())).text) == {"api_key_configured": False}
    assert stored[-1] == ("ha_api_key", None)

    monkeypatch.setattr(em_api.db, "set_config", lambda key, value: stored.append((key, value)))
    monkeypatch.setattr(em_api.em_sendspin, "configure", lambda value: asyncio.sleep(0))
    response = run(em_api._patch_system_config.__wrapped__(request({"device_approval": "auto", "music_assistant_url": "ma.local"})))
    assert response.status == 200
    assert run(em_api._patch_system_config.__wrapped__(request({"immutable": True}))).status == 400


def test_ota_update_rollback_and_fleet_deploy_decisions(monkeypatch):
    async def cached():
        return {"version": "v2", "url": "url"}

    rows = {
        "dev": {"id": "dev", "approved": 1, "firmware_ver": "v1", "firmware_previous": "v0"},
        "old": {"id": "old", "approved": 1, "firmware_ver": "v2", "firmware_previous": None},
        "pending": {"id": "pending", "approved": 0, "firmware_ver": "v1", "firmware_previous": None},
    }
    monkeypatch.setattr(em_api, "_get_cached_release", cached)
    monkeypatch.setattr(em_api.db, "get_device", lambda device_id: rows.get(device_id))
    monkeypatch.setattr(em_api, "_devices", {"dev": object(), "old": object(), "pending": object()})
    started = []

    async def update(*args):
        started.append(("update", args))

    async def rollback(*args):
        started.append(("rollback", args))

    monkeypatch.setattr(em_api, "_run_update", update)
    monkeypatch.setattr(em_api, "_run_rollback", rollback)
    monkeypatch.setattr(em_api, "_updates_in_progress", set())

    response = run(em_api._post_device_update.__wrapped__(request({}, match_info={"id": "dev"})))
    assert response.status == 202
    run(asyncio.sleep(0))
    assert started[0][0] == "update"

    assert run(em_api._post_device_update.__wrapped__(request({"upload_token": "missing"}, match_info={"id": "dev"}))).status == 404
    assert run(em_api._post_device_update.__wrapped__(request({}, match_info={"id": "missing"}))).status == 404
    monkeypatch.setattr(em_api, "_devices", {})
    assert run(em_api._post_device_update.__wrapped__(request({}, match_info={"id": "dev"}))).status == 409
    monkeypatch.setattr(em_api, "_devices", {"dev": object()})

    response = run(em_api._post_device_rollback.__wrapped__(request(match_info={"id": "dev"})))
    assert response.status == 202
    run(asyncio.sleep(0))
    assert any(item[0] == "rollback" for item in started)
    assert run(em_api._post_device_rollback.__wrapped__(request(match_info={"id": "pending"}))).status == 404
    assert run(em_api._post_device_rollback.__wrapped__(request(match_info={"id": "missing"}))).status == 404

    monkeypatch.setattr(em_api, "_devices", {"dev": object(), "old": object(), "pending": object()})
    em_api._updates_in_progress.add("dev")
    assert run(em_api._post_device_update.__wrapped__(request({}, match_info={"id": "dev"}))).status == 409
    em_api._updates_in_progress.clear()
    response = run(em_api._post_deploy_all.__wrapped__(request({})))
    data = json.loads(response.text)
    assert data["started"] == ["dev"]
    assert {item["device_id"] for item in data["skipped"]} == {"old", "pending"}


def test_ota_and_shell_helpers_cover_failure_stages(monkeypatch):
    class ShellWS:
        def __init__(self):
            self.sent = []
            self.parts = [b"output\n", "__CMD_DONE_9f3a__\n"]

        async def send(self, value):
            self.sent.append(value)

        async def recv(self):
            return self.parts.pop(0)

        async def close(self):
            return None

    shell = ShellWS()
    live = SimpleNamespace(device_id="dev")
    monkeypatch.setattr(em_api, "_get_device_shell_ws", lambda value: asyncio.sleep(0, result=shell))
    released = []
    monkeypatch.setattr(em_api, "_release_shell_ws", lambda *args: asyncio.sleep(0, result=released.append(args)))
    assert run(em_api._shell_run(live, "echo ok")) == "output"
    assert shell.sent and released
    monkeypatch.setattr(em_api, "_stream_file_to_device", lambda *args, **kwargs: asyncio.sleep(0, result=True))
    assert run(em_api._stream_binary_to_slot(live, b"bin", "server_b")) is True

    failures = []
    monkeypatch.setattr(em_api, "_push_log_event", lambda *args: asyncio.sleep(0))
    monkeypatch.setattr(em_api, "_update_failed", lambda *args: asyncio.sleep(0, result=failures.append(args)))
    monkeypatch.setattr(em_api, "_fetch_binary", lambda *args: asyncio.sleep(0, result=None))
    monkeypatch.setattr(em_api, "_devices", {"dev": live})
    monkeypatch.setattr(em_api.db, "get_device", lambda *args: {"firmware_ver": "v1"})
    monkeypatch.setattr(em_api.db, "set_firmware_previous", lambda *args: None)
    run(em_api._run_update("dev", {"version": "v2", "url": "u"}))
    assert "fetch binary" in failures[-1][1].lower()

    failures.clear()
    monkeypatch.setattr(em_api, "_shell_run", lambda *args, **kwargs: asyncio.sleep(0, result="MIGRATE_FAILED"))
    run(em_api._run_update("dev", {"version": "v2", "url": "u"}, b"bin"))
    assert "migration failed" in failures[-1][1].lower()

    monkeypatch.setattr(em_api, "_devices", {})
    run(em_api._run_rollback("dev", "v1"))
    assert "disconnected" in failures[-1][1].lower()


def test_upload_handlers_cover_multipart_validation_and_success(monkeypatch, tmp_path):
    class Field:
        def __init__(self, name, data=b"", filename=None):
            self.name = name
            self.data = data
            self.filename = filename

        async def read(self):
            return self.data

    class Reader:
        def __init__(self, field):
            self.field = field

        async def next(self):
            return self.field

    class UploadRequest(dict):
        def __init__(self, field, body=b"", content_type="multipart/form-data"):
            super().__init__()
            self.field = field
            self.body = body
            self.content_type = content_type

        async def multipart(self):
            return Reader(self.field)

        async def read(self):
            return self.body

    assert run(em_api._post_upload_binary.__wrapped__(UploadRequest(Field("wrong", b"x")))).status == 400
    assert run(em_api._post_upload_binary.__wrapped__(UploadRequest(Field("binary", b"")))).status == 400
    uploaded = run(em_api._post_upload_binary.__wrapped__(UploadRequest(Field("binary", b"abc"))))
    assert uploaded.status == 200
    token = json.loads(uploaded.text)["upload_token"]
    assert em_api._pending_uploads[token] == b"abc"

    monkeypatch.setattr(em_api.em_test_audio, "decode_test_audio", lambda data: asyncio.sleep(0, result=b"pcm"))
    monkeypatch.setattr(em_api.em_test_audio, "pcm_to_wav", lambda data: b"wav")
    invalid = UploadRequest(Field("wrong", b"x"))
    invalid.match_info = {"id": "dev"}
    assert run(em_api._post_test_audio.__wrapped__(invalid)).status == 400
    raw = UploadRequest(None, body=b"raw", content_type="application/octet-stream")
    raw.match_info = {"id": "dev"}
    response = run(em_api._post_test_audio.__wrapped__(raw))
    assert response.status == 201 and em_api._test_audio["dev"] == b"wav"

    monkeypatch.setattr(em_api.em_oww_models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(em_api.em_oww_models, "safe_model_filename", lambda name: name if name.endswith(".onnx") and "/" not in name else None)
    monkeypatch.setattr(em_api.em_oww_models, "scan", lambda: [{"file": "custom.onnx"}])
    bad_model = UploadRequest(Field("model", b"x", "bad.txt"))
    assert run(em_api._post_oww_model_upload.__wrapped__(bad_model)).status == 400
    good_model = UploadRequest(Field("model", b"onnx", "custom.onnx"))
    response = run(em_api._post_oww_model_upload.__wrapped__(good_model))
    assert response.status == 201 and (tmp_path / "custom.onnx").read_bytes() == b"onnx"

    empty_model = UploadRequest(Field("model", b"", "empty.onnx"))
    assert run(em_api._post_oww_model_upload.__wrapped__(empty_model)).status == 400
    monkeypatch.setattr(em_api.em_test_audio, "decode_test_audio", lambda data: (_ for _ in ()).throw(ValueError("bad wav")))
    invalid_audio = UploadRequest(None, body=b"bad", content_type="application/octet-stream")
    invalid_audio.match_info = {"id": "dev"}
    assert run(em_api._post_test_audio.__wrapped__(invalid_audio)).status == 400

    assert run(em_api._delete_oww_model.__wrapped__(request(match_info={"file": "bad/name.onnx"}))).status == 400
    assert run(em_api._delete_oww_model.__wrapped__(request(match_info={"file": "missing.onnx"}))).status == 404
    (tmp_path / "custom.onnx").write_bytes(b"x")
    monkeypatch.setattr(em_api.db, "get_global_device_config", lambda: {})
    monkeypatch.setattr(em_api.db, "get_all_devices", lambda: [])
    monkeypatch.setattr(em_api.em_oww_models, "in_use_by", lambda *args: ["global"])
    assert run(em_api._delete_oww_model.__wrapped__(request(match_info={"file": "custom.onnx"}))).status == 409
    monkeypatch.setattr(em_api.em_oww_models, "in_use_by", lambda *args: [])
    assert run(em_api._delete_oww_model.__wrapped__(request(match_info={"file": "custom.onnx"}))).status == 200


def test_test_turn_handler_reports_device_preconditions(monkeypatch):
    device = SimpleNamespace(muted=False, capabilities=["test_audio"])
    em_api._test_audio.pop("dev", None)
    old = em_api._devices
    em_api._devices = {}
    try:
        assert run(em_api._post_test_turn.__wrapped__(request(match_info={"id": "dev"}))).status == 409
        em_api._devices = {"dev": device}
        monkeypatch.setattr(em_api.db, "get_device", lambda *args: None)
        assert run(em_api._post_test_turn.__wrapped__(request(match_info={"id": "dev"}))).status == 409
        monkeypatch.setattr(em_api.db, "get_device", lambda *args: {"approved": 1})
        device.capabilities = []
        assert run(em_api._post_test_turn.__wrapped__(request(match_info={"id": "dev"}))).status == 409
        device.capabilities = ["test_audio"]
        device.muted = True
        assert run(em_api._post_test_turn.__wrapped__(request(match_info={"id": "dev"}))).status == 409
        device.muted = False
        assert run(em_api._post_test_turn.__wrapped__(request(match_info={"id": "dev"}))).status == 404
    finally:
        em_api._devices = old
