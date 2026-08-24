import json

import pytest

import em_db


@pytest.fixture
def database(tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "registry.db"))
    yield
    if em_db._conn is not None:
        em_db._conn.close()
    em_db._conn, em_db._db_path = old_conn, old_path


def test_device_registry_lifecycle(database):
    assert em_db.get_device("missing") is None
    em_db.register_new_device("dev-1", "192.0.2.1", "v1")
    row = em_db.get_device("dev-1")
    assert row["approved"] == 0
    assert row["ip"] == "192.0.2.1"
    assert len(em_db.get_all_devices()) == 1
    assert [r["device_id"] for r in em_db.get_pending_devices()] == ["dev-1"]

    config = {"owwThreshold": 0.3}
    em_db.approve_device("dev-1", "Kitchen", config)
    row = em_db.get_device("dev-1")
    assert row["approved"] == 1
    assert row["label"] == "Kitchen"
    assert json.loads(row["config"]) == config
    with pytest.raises(ValueError):
        em_db.approve_device("missing", "Nope")

    em_db.set_device_label("dev-1", "Office")
    em_db.upsert_device_seen("dev-1", "192.0.2.2", "v2")
    em_db.touch_device_seen("dev-1")
    row = em_db.get_device("dev-1")
    assert row["label"] == "Office"
    assert row["ip"] == "192.0.2.2"
    assert row["firmware_ver"] == "v2"


def test_device_token_lifecycle(database):
    assert em_db.get_device_token("new") is None
    first = em_db.ensure_device_token("new")
    assert first and em_db.get_device_token("new") == first
    assert em_db.ensure_device_token("new") == first
    em_db.clear_device_token("new")
    assert em_db.get_device_token("new") is None
    second = em_db.ensure_device_token("new")
    assert second and second != first
    assert em_db.get_device("new")["approved"] == 0


def test_device_config_fallbacks_and_global_config(database):
    defaults = em_db.DEFAULT_DEVICE_CONFIG
    assert em_db.get_device_config("missing") == defaults
    em_db.register_new_device("dev-1", "192.0.2.1", None)
    em_db.set_device_config("dev-1", {"owwThreshold": 0.2})
    assert em_db.get_device_config("dev-1") == {"owwThreshold": 0.2}

    with em_db._tx() as conn:
        conn.execute("UPDATE devices SET config = ? WHERE device_id = ?", ("bad", "dev-1"))
    assert em_db.get_device_config("dev-1") == defaults
    with em_db._tx() as conn:
        conn.execute("UPDATE devices SET config = ? WHERE device_id = ?", ("null", "dev-1"))
    assert em_db.get_device_config("dev-1") == defaults

    assert em_db.get_global_device_config() == defaults
    assert em_db.get_global_device_config_raw() == defaults
    em_db.set_global_device_config({"owwThreshold": 0.4})
    assert em_db.get_global_device_config()["owwThreshold"] == 0.4
    assert em_db.get_global_device_config()["micGainDb"] == defaults["micGainDb"]
    assert em_db.get_global_device_config_raw() == {"owwThreshold": 0.4}
    with em_db._tx() as conn:
        conn.execute("UPDATE system_config SET value = ? WHERE key = ?", ("bad", "global_device_config"))
    assert em_db.get_global_device_config() == defaults
    assert em_db.get_global_device_config_raw() == {}
