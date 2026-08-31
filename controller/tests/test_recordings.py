"""
Tests for em_recordings — utterance capture storage.

Pure path/filesystem logic, so all of it is exercised here against a tmp
DB path. The properties that matter: retention is a hard per-device count,
one device's turn id can never reach another device's audio, and a
half-written file is never left where the API would serve it.
"""

import wave

import pytest

import em_recordings as rec


def _db(tmp_path):
    return str(tmp_path / "echomuse.db")


def _pcm(ms: int) -> bytes:
    return b"\x00\x01" * int(rec.SAMPLE_RATE * ms / 1000)


# ─── directory resolution ────────────────────────────────────────────────────

def test_recordings_dir_sits_beside_db(tmp_path):
    assert rec.recordings_dir(_db(tmp_path)) == tmp_path / "recordings"


def test_recordings_dir_is_absolute_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", "echomuse.db")
    assert rec.recordings_dir().is_absolute()


# ─── naming ──────────────────────────────────────────────────────────────────

def test_filename_roundtrips_through_parse():
    name = rec.filename("G090LF11", 42)
    assert name == "G090LF11_42.wav"
    assert rec.parse_filename(name) == ("G090LF11", 42, "stt")
    assert rec.filename("G090LF11", 42, "tts") == "G090LF11_42_tts.wav"
    assert rec.parse_filename("G090LF11_42_tts.wav") == ("G090LF11", 42, "tts")


def test_filename_rejects_unsafe_device_ids():
    assert rec.filename("../../etc/passwd", 1) is None
    assert rec.filename("dev/ice", 1) is None
    assert rec.filename("", 1) is None


def test_parse_filename_rejects_junk():
    assert rec.parse_filename("G090LF11.wav") is None
    assert rec.parse_filename("G090LF11_x.wav") is None
    assert rec.parse_filename("G090LF11_1.mp3") is None
    assert rec.parse_filename("../G090LF11_1.wav") is None


# ─── encoding ────────────────────────────────────────────────────────────────

def test_encode_wav_declares_the_wire_format(tmp_path):
    path = tmp_path / "a.wav"
    path.write_bytes(rec.encode_wav(_pcm(500)))
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == rec.SAMPLE_RATE // 2


def test_duration_ms_matches_the_wire_format():
    assert rec.duration_ms(len(_pcm(1500))) == 1500


def test_max_bytes_matches_max_seconds():
    assert rec.duration_ms(rec.MAX_UTTERANCE_BYTES) == rec.MAX_UTTERANCE_SECONDS * 1000


# ─── save / retention ────────────────────────────────────────────────────────

def test_save_writes_a_playable_wav(tmp_path):
    name = rec.save("dev1", 7, _pcm(200), db_path=_db(tmp_path))
    assert name == "dev1_7.wav"
    with wave.open(str(tmp_path / "recordings" / name), "rb") as w:
        assert w.getnframes() == rec.SAMPLE_RATE // 5


def test_save_writes_tts_at_24khz(tmp_path):
    name = rec.save("dev1", 7, b"\x00\x01" * 2400, db_path=_db(tmp_path),
                    kind="tts", sample_rate=24000)
    assert name == "dev1_7_tts.wav"
    with wave.open(str(tmp_path / "recordings" / name), "rb") as w:
        assert w.getframerate() == 24000


def test_save_ignores_empty_audio(tmp_path):
    assert rec.save("dev1", 7, b"", db_path=_db(tmp_path)) is None
    assert not (tmp_path / "recordings").exists()


def test_save_leaves_no_partial_files_behind(tmp_path):
    rec.save("dev1", 7, _pcm(50), db_path=_db(tmp_path))
    names = [p.name for p in (tmp_path / "recordings").iterdir()]
    assert names == ["dev1_7.wav"]


def test_retention_keeps_the_newest_n_per_device(tmp_path):
    db = _db(tmp_path)
    for turn in range(1, 16):
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=10)
    kept = rec.list_for("dev1", db)
    assert len(kept) == 10
    # Newest turn first, and it is the ids that decide — not mtime.
    assert kept[0] == "dev1_15.wav"
    assert kept[-1] == "dev1_6.wav"


def test_retention_keeps_both_audio_kinds_for_the_newest_turns(tmp_path):
    db = _db(tmp_path)
    for turn in range(1, 5):
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=2)
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=2, kind="tts", sample_rate=24000)
    assert set(rec.list_for("dev1", db)) == {
        "dev1_3.wav", "dev1_3_tts.wav", "dev1_4.wav", "dev1_4_tts.wav",
    }


def test_retention_is_per_device_not_global(tmp_path):
    db = _db(tmp_path)
    for turn in range(1, 13):
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=3)
        rec.save("dev2", turn, _pcm(20), db_path=db, keep=3)
    assert len(rec.list_for("dev1", db)) == 3
    assert len(rec.list_for("dev2", db)) == 3


def test_retention_survives_out_of_order_ids(tmp_path):
    # Filesystem timestamps can collide or be flattened by a restore; the
    # turn id is what orders these.
    db = _db(tmp_path)
    for turn in (50, 10, 30, 20, 40):
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=2)
    assert rec.list_for("dev1", db) == ["dev1_50.wav", "dev1_40.wav"]


def test_list_for_ignores_other_devices_and_strays(tmp_path):
    db = _db(tmp_path)
    rec.save("dev1", 1, _pcm(20), db_path=db)
    rec.save("dev2", 2, _pcm(20), db_path=db)
    (tmp_path / "recordings" / "notes.txt").write_text("hi")
    assert rec.list_for("dev1", db) == ["dev1_1.wav"]


def test_list_for_on_missing_dir_is_empty(tmp_path):
    assert rec.list_for("dev1", _db(tmp_path)) == []


# ─── resolve (the API's ownership check) ─────────────────────────────────────

def test_resolve_returns_an_existing_file(tmp_path):
    db = _db(tmp_path)
    name = rec.save("dev1", 3, _pcm(20), db_path=db)
    assert rec.resolve("dev1", name, db) == tmp_path / "recordings" / name


def test_resolve_refuses_another_devices_recording(tmp_path):
    db = _db(tmp_path)
    name = rec.save("dev2", 3, _pcm(20), db_path=db)
    assert rec.resolve("dev1", name, db) is None


def test_resolve_refuses_traversal(tmp_path):
    db = _db(tmp_path)
    rec.save("dev1", 3, _pcm(20), db_path=db)
    assert rec.resolve("dev1", "../echomuse.db", db) is None
    assert rec.resolve("dev1", "dev1_3.wav/../../echomuse.db", db) is None


def test_resolve_missing_file_is_none(tmp_path):
    # The retention window is far shorter than TURN_RETENTION, so a turn row
    # naming a pruned file is ordinary — it must read as "no recording".
    assert rec.resolve("dev1", "dev1_3.wav", _db(tmp_path)) is None


# ─── deletion ────────────────────────────────────────────────────────────────

def test_delete_device_removes_only_its_own(tmp_path):
    db = _db(tmp_path)
    for turn in range(1, 4):
        rec.save("dev1", turn, _pcm(20), db_path=db)
        rec.save("dev2", turn, _pcm(20), db_path=db)
    assert rec.delete_device("dev1", db) == 3
    assert rec.list_for("dev1", db) == []
    assert len(rec.list_for("dev2", db)) == 3


def test_prune_all_keeps_newest_files_across_devices_and_kinds(tmp_path):
    db = _db(tmp_path)
    for turn in range(1, 13):
        rec.save("dev1", turn, _pcm(20), db_path=db, keep=100, kind="stt")
        rec.save("dev2", turn, _pcm(20), db_path=db, keep=100, kind="tts")

    removed = rec.prune_all(db, keep=20)

    assert len(removed) == 4
    assert rec.list_all(db) == [
        name for turn in range(12, 2, -1)
        for name in (f"dev2_{turn}_tts.wav", f"dev1_{turn}.wav")
    ]
