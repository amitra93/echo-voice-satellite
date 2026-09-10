"""Schema + CRUD tests for the v28 durable alarms table.

em_db runs against a real temporary database here (migrations included),
because a migration that fails at container start takes the whole controller
down — the same reasoning as test_db_instrumentation.
"""

import pytest

import em_alarms
import em_db as db

@pytest.fixture()
def fresh_db(tmp_path):
    path = tmp_path / "test.db"
    db.init(str(path))
    db.register_new_device("dev1", "1.2.3.4", "v2.12.0")
    yield db
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _cols(table: str) -> set:
    return {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})")}


def test_schema_has_alarms_table_and_device_timezone(fresh_db):
    assert "alarms" in {
        r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "timezone" in _cols("devices")
    cols = _cols("alarms")
    assert {"id", "device_id", "hour", "minute", "recurrence", "weekday_mask",
            "date", "tz", "enabled", "next_fire_utc", "last_fired_occurrence"} <= cols


def test_create_and_get_roundtrips_to_spec(fresh_db):
    db.create_alarm(
        "a1", "dev1", hour=7, minute=30, recurrence="weekly",
        tz="America/New_York", weekday_mask=em_alarms.WEEKDAYS_MASK,
        label="gym", next_fire_utc=1234.0, created_by="wil",
    )
    spec = db.get_alarm_spec("a1")
    assert isinstance(spec, em_alarms.AlarmSpec)
    assert (spec.hour, spec.minute, spec.recurrence, spec.tz) == (
        7, 30, "weekly", "America/New_York")
    assert spec.weekday_mask == em_alarms.WEEKDAYS_MASK
    assert spec.label == "gym" and spec.enabled is True


def test_list_alarms_scoped_and_enabled_only(fresh_db):
    db.register_new_device("dev2", "1.2.3.5", "v2.12.0")
    db.create_alarm("a1", "dev1", hour=7, minute=0, recurrence="daily", tz="UTC")
    db.create_alarm("a2", "dev1", hour=8, minute=0, recurrence="daily", tz="UTC",
                    enabled=False)
    db.create_alarm("a3", "dev2", hour=9, minute=0, recurrence="daily", tz="UTC")

    assert {r["id"] for r in db.list_alarms("dev1")} == {"a1", "a2"}
    assert {r["id"] for r in db.list_alarms()} == {"a1", "a2", "a3"}
    assert {s.id for s in db.list_enabled_alarm_specs()} == {"a1", "a3"}


def test_update_alarm_ignores_scheduler_owned_columns(fresh_db):
    db.create_alarm("a1", "dev1", hour=7, minute=0, recurrence="daily", tz="UTC")
    assert db.update_alarm("a1", {"hour": 9, "enabled": False}) is True
    # These are not in the updatable allowlist and must be dropped silently.
    assert db.update_alarm("a1", {"next_fire_utc": 5.0}) is False
    assert db.update_alarm("a1", {"last_fired_occurrence": "2026-01-01"}) is False
    row = db.get_alarm("a1")
    assert row["hour"] == 9 and row["enabled"] == 0
    assert row["next_fire_utc"] is None and row["last_fired_occurrence"] is None


def test_set_alarm_schedule_updates_bookkeeping(fresh_db):
    db.create_alarm("a1", "dev1", hour=7, minute=0, recurrence="daily", tz="UTC")
    db.set_alarm_schedule("a1", 2000.0, "2026-06-02")
    row = db.get_alarm("a1")
    assert row["next_fire_utc"] == 2000.0
    assert row["last_fired_occurrence"] == "2026-06-02"
    # A bare reschedule (None occurrence) must not clear the existing guard.
    db.set_alarm_schedule("a1", 3000.0, None)
    row = db.get_alarm("a1")
    assert row["next_fire_utc"] == 3000.0
    assert row["last_fired_occurrence"] == "2026-06-02"


def test_device_timezone_roundtrip(fresh_db):
    assert db.get_device_timezone("dev1") is None
    db.set_device_timezone("dev1", "Europe/London")
    assert db.get_device_timezone("dev1") == "Europe/London"


def test_delete_device_cascades_alarms(fresh_db):
    db.create_alarm("a1", "dev1", hour=7, minute=0, recurrence="daily", tz="UTC")
    db.delete_device("dev1")
    assert db.get_alarm("a1") is None
    assert db.list_alarms("dev1") == []
