"""
Migration safety across a multi-version jump.

The schema version is an index into an append-only list, so a controller
several releases behind migrates all the way up in one startup — there is no
"upgrade one at a time" path and no supported-jump window. That makes the
large jump the normal case rather than the exotic one, and these guard the
two ways it can go wrong: losing data with no undo, and an OLDER controller
running against a newer schema.
"""

import json
import os
import sqlite3
import tempfile

import pytest

import em_db

VER = "SELECT value FROM system_config WHERE key = 'schema_version'"


def _legacy_db(tmp_path, upto=11, label="Kitchen"):
    """A database as a controller `upto` migrations ago would have left it."""
    p = str(tmp_path / "em.db")
    c = sqlite3.connect(p)
    for sql in em_db.MIGRATIONS[:upto]:
        c.executescript(sql)
    c.execute("INSERT INTO devices (device_id, label, approved) VALUES ('D', ?, 1)",
              (label,))
    c.commit()
    c.close()
    return p


def test_a_multi_version_jump_migrates_and_keeps_the_data(tmp_path):
    p = _legacy_db(tmp_path)
    em_db.init(p)
    c = sqlite3.connect(p)
    assert int(c.execute(VER).fetchone()[0]) == len(em_db.MIGRATIONS)
    assert c.execute("SELECT label FROM devices").fetchone()[0] == "Kitchen"


def test_a_backup_is_taken_before_migrating(tmp_path):
    """
    Every migration so far is additive, which is about as safe as schema
    change gets — but the first one that rewrites or drops data has no undo
    without this, and by then the jump may span several releases.
    """
    p = _legacy_db(tmp_path)
    em_db.init(p)

    bak = f"{p}.pre-v11.bak"
    assert os.path.exists(bak), "no pre-migration backup was written"

    # The backup must be a usable database, not just a file of the right size.
    b = sqlite3.connect(bak)
    assert int(b.execute(VER).fetchone()[0]) == 11
    assert b.execute("SELECT label FROM devices").fetchone()[0] == "Kitchen"


def test_a_fresh_database_is_not_backed_up(tmp_path):
    """There is nothing to preserve, and a stray .bak invites restoring it."""
    p = str(tmp_path / "fresh.db")
    em_db.init(p)
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".bak")]


def test_restarting_on_a_current_schema_does_not_re_backup(tmp_path):
    """
    Backups are named for the version being LEFT, so an ordinary restart —
    which is most restarts — must not write one at all.
    """
    p = _legacy_db(tmp_path)
    em_db.init(p)
    before = sorted(f for f in os.listdir(tmp_path) if f.endswith(".bak"))
    em_db.init(p)
    em_db.init(p)
    assert sorted(f for f in os.listdir(tmp_path) if f.endswith(".bak")) == before


def test_a_failed_backup_refuses_to_migrate(tmp_path, monkeypatch):
    """
    Disk-full is exactly when the schema should be left alone. A warning
    logged at startup is one nobody reads until afterwards.

    Faulted at the backup call rather than with directory permissions: the
    container runs as root, which ignores those, so a permissions test would
    pass while proving nothing.
    """
    p = _legacy_db(tmp_path)
    real = sqlite3.connect

    def boom(path, *a, **k):
        if str(path).endswith(".bak"):
            raise sqlite3.OperationalError("disk I/O error")
        return real(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(RuntimeError, match="back up"):
        em_db.init(p)

    monkeypatch.undo()
    c = sqlite3.connect(p)
    assert int(c.execute(VER).fetchone()[0]) == 11, "schema was touched anyway"
    assert c.execute("SELECT label FROM devices").fetchone()[0] == "Kitchen"


def test_the_backup_can_be_skipped_deliberately(tmp_path, monkeypatch):
    """An escape hatch, so a broken backup cannot make a controller unstartable."""
    p = _legacy_db(tmp_path)
    real = sqlite3.connect
    monkeypatch.setenv("EM_SKIP_DB_BACKUP", "1")
    monkeypatch.setattr(sqlite3, "connect",
                        lambda path, *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x"))
                        if str(path).endswith(".bak") else real(path, *a, **k))
    em_db.init(p)
    monkeypatch.undo()
    assert int(sqlite3.connect(p).execute(VER).fetchone()[0]) == len(em_db.MIGRATIONS)


def test_a_newer_database_refuses_to_start(tmp_path, monkeypatch):
    """
    An older controller against a newer schema used to start SILENTLY —
    MIGRATIONS[current:] is empty, so nothing ran. It mostly works, because
    the newer schema is a superset, and "mostly" is the problem: the failure
    shows up as odd behaviour elsewhere, exactly when someone has rolled an
    image back and is already troubleshooting.
    """
    p = _legacy_db(tmp_path)
    em_db.init(p)   # migrate to current

    monkeypatch.setattr(em_db, "MIGRATIONS", em_db.MIGRATIONS[:-2])
    with pytest.raises(RuntimeError, match="newer version"):
        em_db.init(p)


def test_the_downgrade_message_names_both_versions(tmp_path, monkeypatch):
    """Someone reading this is troubleshooting; the numbers are the whole point."""
    p = _legacy_db(tmp_path)
    em_db.init(p)
    latest = len(em_db.MIGRATIONS)

    monkeypatch.setattr(em_db, "MIGRATIONS", em_db.MIGRATIONS[:-2])
    with pytest.raises(RuntimeError) as e:
        em_db.init(p)
    msg = str(e.value)
    assert f"v{latest}" in msg and f"v{latest - 2}" in msg
    assert ".bak" in msg, "should point at the backup it can be restored from"


def test_main_line_v19_gets_turn_state_compatibility_fix(tmp_path):
    """main used v19 for a retired column; new_impl needs turns.state there."""
    p = _legacy_db(tmp_path, upto=18)
    conn = sqlite3.connect(p)
    conn.executescript("""
        ALTER TABLE devices ADD COLUMN esphome_mac TEXT;
        UPDATE system_config SET value = '19' WHERE key = 'schema_version';
    """)
    conn.commit()
    conn.close()

    em_db.init(p)
    columns = {row[1] for row in sqlite3.connect(p).execute("PRAGMA table_info(turns)")}
    assert "state" in columns


def test_v23_removes_only_obsolete_direct_capture_config(tmp_path):
    p = _legacy_db(tmp_path, upto=22)
    conn = sqlite3.connect(p)
    obsolete = {
        "adcMicpga": 40, "adcDigitalGain": 88, "micGainDb": 24,
        "beamAngle": -1, "beamformingEnabled": True, "aecEnabled": True,
        "aecDelayMs": 0, "aecTailMs": 300, "agcEnabled": True,
        "vadChannel": 0,
    }
    global_config = {**obsolete, "afeMicGainDb": 8, "vadThreshold": 0.002}
    device_config = {**obsolete, "afeMicGainDb": 12, "ledScene": "pride"}
    conn.execute(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('global_device_config', ?)",
        (json.dumps(global_config),),
    )
    conn.execute(
        "UPDATE devices SET config = ?, config_sections = ? WHERE device_id = 'D'",
        (json.dumps(device_config), json.dumps(["microphones", "ring"])),
    )
    conn.commit()
    conn.close()

    em_db.init(p)
    conn = sqlite3.connect(p)
    fleet = json.loads(conn.execute(
        "SELECT value FROM system_config WHERE key = 'global_device_config'"
    ).fetchone()[0])
    device, sections = conn.execute(
        "SELECT config, config_sections FROM devices WHERE device_id = 'D'"
    ).fetchone()
    device = json.loads(device)

    assert not (set(fleet) | set(device)) & obsolete.keys()
    assert fleet == {"afeMicGainDb": 8, "vadThreshold": 0.002}
    assert device == {"afeMicGainDb": 12, "ledScene": "pride"}
    assert json.loads(sections) == ["microphones", "ring"]


def test_v24_removes_retired_audio_config(tmp_path):
    p = _legacy_db(tmp_path, upto=23)
    conn = sqlite3.connect(p)
    old = {"ttsGainDb": 4, "bassShelfHz": 100, "subsonicHz": 60,
           "limiterThreshold": -6, "nsAsr": True, "eqBands": [1]}
    conn.execute("UPDATE system_config SET value = ? WHERE key = 'global_device_config'",
                 (json.dumps(old),))
    conn.execute("UPDATE devices SET config = ?", (json.dumps(old),))
    conn.commit()
    conn.close()

    em_db.init(p)
    conn = sqlite3.connect(p)
    fleet = json.loads(conn.execute(
        "SELECT value FROM system_config WHERE key = 'global_device_config'").fetchone()[0])
    device = json.loads(conn.execute("SELECT config FROM devices").fetchone()[0])
    assert fleet == device == {"eqBands": [1]}
