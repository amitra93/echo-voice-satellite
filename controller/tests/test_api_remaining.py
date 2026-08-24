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
