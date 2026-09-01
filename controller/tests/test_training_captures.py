"""
Tests for em_training_captures — wake-event capture storage.

Pure path/filesystem logic, exercised against a tmp DB path. The properties
that matter: captures are grouped by wake-word model stem; untriaged is capped
oldest-first while labelled buckets are never auto-pruned; a model stem or
filename that does not parse can never escape the captures root; and an export
is exactly the labelled positive/negative layout oww_forge imports.
"""

import io
import json
import wave
import zipfile

import em_training_captures as tc


def _db(tmp_path):
    return str(tmp_path / "echomuse.db")


def _pcm(ms: int) -> bytes:
    return b"\x00\x01" * int(tc.SAMPLE_RATE * ms / 1000)


# ─── directory resolution ────────────────────────────────────────────────────

def test_captures_dir_sits_beside_db(tmp_path):
    assert tc.captures_dir(_db(tmp_path)) == tmp_path / "training_captures"


def test_captures_dir_is_absolute_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", "echomuse.db")
    assert tc.captures_dir().is_absolute()


# ─── naming ──────────────────────────────────────────────────────────────────

def test_filename_roundtrips_through_parse():
    name = tc.filename("G090LF11", 1700000000000, "act", 0.734)
    assert name == "G090LF11_1700000000000_act_0734.wav"
    parsed = tc.parse_filename(name)
    assert parsed["device_id"] == "G090LF11"
    assert parsed["ts_ms"] == 1700000000000
    assert parsed["kind"] == "act"
    assert abs(parsed["score"] - 0.734) < 1e-9


def test_filename_rejects_unsafe_inputs():
    assert tc.filename("../../etc/passwd", 1, "act", 0.5) is None
    assert tc.filename("dev", 1, "bogus", 0.5) is None
    assert tc.filename("", 1, "act", 0.5) is None


def test_stop_capture_kinds_round_trip_through_filename():
    for kind in ("stop_act", "stop_miss", "false_stop", "playback_negative"):
        name = tc.filename("dev", 1, kind, 0.5)
        assert name is not None
        assert tc.parse_filename(name)["kind"] == kind


def test_parse_filename_rejects_junk():
    assert tc.parse_filename("G090LF11.wav") is None
    assert tc.parse_filename("G090LF11_1_act.wav") is None
    assert tc.parse_filename("G090LF11_1_walk_0500.wav") is None
    assert tc.parse_filename("../G090LF11_1_act_0500.wav") is None


def test_safe_model_rejects_traversal():
    assert tc.safe_model("hey_jarvis") == "hey_jarvis"
    assert tc.safe_model("../etc") is None
    assert tc.safe_model("a/b") is None
    assert tc.safe_model("") is None


# ─── save / retention ────────────────────────────────────────────────────────

def test_save_writes_a_playable_wav_into_untriaged(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(200), "act", 0.9,
                   db_path=db, ts_ms=1700000000000)
    assert name == "dev1_1700000000000_act_0900.wav"
    path = tmp_path / "training_captures" / "hey_jarvis" / "untriaged" / name
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == tc.SAMPLE_RATE // 5


def test_save_ignores_empty_audio(tmp_path):
    assert tc.save("hey_jarvis", "dev1", b"", "act", 0.9, db_path=_db(tmp_path)) is None
    assert not (tmp_path / "training_captures").exists()


def test_save_rejects_bad_model(tmp_path):
    assert tc.save("../evil", "dev1", _pcm(20), "act", 0.9, db_path=_db(tmp_path)) is None


def test_untriaged_cap_prunes_oldest_first(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 16):
        tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1,
                db_path=db, cap=10, ts_ms=1700000000000 + i)
    kept = tc.list_captures("hey_jarvis", "untriaged", db)
    assert len(kept) == 10
    assert kept[0]["ts_ms"] == 1700000000000 + 15   # newest first
    assert kept[-1]["ts_ms"] == 1700000000000 + 6


def test_cap_is_per_wake_word(tmp_path):
    db = _db(tmp_path)
    for i in range(1, 13):
        tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1, db_path=db, cap=3, ts_ms=1700 + i)
        tc.save("computer", "dev1", _pcm(20), "miss", 0.1, db_path=db, cap=3, ts_ms=1700 + i)
    assert len(tc.list_captures("hey_jarvis", "untriaged", db)) == 3
    assert len(tc.list_captures("computer", "untriaged", db)) == 3


# ─── labelling ───────────────────────────────────────────────────────────────

def test_label_moves_untriaged_to_positive(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.3, db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "positive", db) is True
    assert tc.list_captures("hey_jarvis", "untriaged", db) == []
    assert tc.list_captures("hey_jarvis", "positive", db)[0]["name"] == name


def test_label_moves_activation_to_negative(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "negative", db) is True
    assert tc.list_captures("hey_jarvis", "negative", db)[0]["name"] == name


def test_label_rejects_bad_target_and_missing(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "maybe", db) is False
    assert tc.label("hey_jarvis", "dev1_9999_act_0100.wav", "positive", db) is False


def test_label_relabels_between_positive_and_negative(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "positive", db) is True
    # A mislabel must be correctable, wherever the clip currently sits.
    assert tc.label("hey_jarvis", name, "negative", db) is True
    assert tc.list_captures("hey_jarvis", "positive", db) == []
    assert tc.list_captures("hey_jarvis", "negative", db)[0]["name"] == name


def test_label_sends_a_clip_back_to_untriaged(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    tc.label("hey_jarvis", name, "positive", db)
    assert tc.label("hey_jarvis", name, "untriaged", db) is True
    assert tc.list_captures("hey_jarvis", "untriaged", db)[0]["name"] == name
    assert tc.list_captures("hey_jarvis", "positive", db) == []


def test_label_is_idempotent(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    tc.label("hey_jarvis", name, "positive", db)
    # A double-click on the same bucket is a no-op success, not a failure.
    assert tc.label("hey_jarvis", name, "positive", db) is True
    assert len(tc.list_captures("hey_jarvis", "positive", db)) == 1


def test_label_stores_non_destructive_trim_and_export_crops_it(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(1000), "act", 0.8,
                   db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "positive", db, 250, 750) is True

    source = tc.resolve("hey_jarvis", name, db_path=db)
    assert source is not None
    with wave.open(str(source), "rb") as w:
        assert w.getnframes() == tc.SAMPLE_RATE

    data = tc.export_zip("hey_jarvis", db)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        cropped = z.read(f"positive/{name}")
        manifest = json.loads(z.read("manifest.json"))
    with wave.open(io.BytesIO(cropped), "rb") as w:
        assert w.getnframes() == tc.SAMPLE_RATE // 2
    assert manifest["clips"][0]["trim"] == {"start_ms": 250.0, "end_ms": 750.0}


def test_trim_survives_relabel_and_undo(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(500), "act", 0.8,
                   db_path=db, ts_ms=1700)
    assert tc.label("hey_jarvis", name, "positive", db, 100, 400)
    assert tc.label("hey_jarvis", name, "untriaged", db)
    data = tc.export_zip("hey_jarvis", db)
    assert data  # the metadata remains attached even after moving buckets


def test_labelled_captures_are_not_pruned(tmp_path):
    db = _db(tmp_path)
    kept = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1)
    tc.label("hey_jarvis", kept, "positive", db)
    for i in range(2, 20):
        tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1, db_path=db, cap=5, ts_ms=1000 + i)
    assert tc.list_captures("hey_jarvis", "positive", db)[0]["name"] == kept


def test_discard_removes_from_any_bucket(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    tc.label("hey_jarvis", name, "positive", db)
    assert tc.discard("hey_jarvis", name, db) is True
    assert tc.list_captures("hey_jarvis", "positive", db) == []


# ─── resolve (the API's lookup) ──────────────────────────────────────────────

def test_resolve_finds_across_buckets(tmp_path):
    db = _db(tmp_path)
    name = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    assert tc.resolve("hey_jarvis", name, db_path=db) is not None
    tc.label("hey_jarvis", name, "negative", db)
    assert tc.resolve("hey_jarvis", name, db_path=db) is not None
    assert tc.resolve("hey_jarvis", name, bucket="untriaged", db_path=db) is None


def test_resolve_refuses_traversal(tmp_path):
    db = _db(tmp_path)
    tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1700)
    assert tc.resolve("hey_jarvis", "../../echomuse.db", db_path=db) is None
    assert tc.resolve("../evil", "dev1_1700_act_0800.wav", db_path=db) is None


# ─── export ──────────────────────────────────────────────────────────────────

def test_export_zip_lays_out_positive_and_negative(tmp_path):
    db = _db(tmp_path)
    pos = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.3, db_path=db, ts_ms=1)
    neg = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=2)
    unt = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1, db_path=db, ts_ms=3)
    tc.label("hey_jarvis", pos, "positive", db)
    tc.label("hey_jarvis", neg, "negative", db)
    data = tc.export_zip("hey_jarvis", db)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
    assert f"positive/{pos}" in names
    assert f"negative/{neg}" in names
    # Untriaged is the triage queue, never part of a finished dataset.
    assert all(not n.endswith(unt) for n in names)


def test_export_zip_includes_a_provenance_manifest(tmp_path):
    db = _db(tmp_path)
    pos = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.32, db_path=db, ts_ms=5)
    tc.label("hey_jarvis", pos, "positive", db)
    data = tc.export_zip("hey_jarvis", db)
    import json
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "manifest.json" in z.namelist()
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["model"] == "hey_jarvis"
    assert manifest["buckets"] == {"positive": 1, "negative": 0}
    clip = manifest["clips"][0]
    assert clip["bucket"] == "positive" and clip["kind"] == "miss"
    assert abs(clip["score"] - 0.32) < 1e-9


def test_export_zip_preserves_stop_capture_provenance(tmp_path):
    db = _db(tmp_path)
    name = tc.save("stop", "dev1", _pcm(20), "playback_negative", 0.32,
                   db_path=db, ts_ms=5)
    tc.label("stop", name, "negative", db)
    with zipfile.ZipFile(io.BytesIO(tc.export_zip("stop", db))) as z:
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["clips"] == [
        {"bucket": "negative", "name": name, "kind": "playback_negative",
         "score": 0.32, "device_id": "dev1", "ts_ms": 5, "trim": None},
    ]


def test_export_zip_can_delete_exported_labelled_captures(tmp_path):
    db = _db(tmp_path)
    pos = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.3, db_path=db, ts_ms=1)
    unt = tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1, db_path=db, ts_ms=2)
    tc.label("hey_jarvis", pos, "positive", db)
    tc.export_zip("hey_jarvis", db, delete_after=True)
    assert tc.list_captures("hey_jarvis", "positive", db) == []
    assert tc.list_captures("hey_jarvis", "untriaged", db)[0]["name"] == unt


# ─── plan_snapshot (the wake-listener's hot-path decision) ───────────────────

def _ring(sec: float) -> bytes:
    return b"\x01\x02" * int(tc.SAMPLE_RATE * sec)


def test_plan_snapshot_returns_the_configured_seconds():
    ring = _ring(5.0)
    pcm = tc.plan_snapshot(ring, 2.0, now=100.0, last_capture_mono=0.0)
    assert pcm is not None
    assert len(pcm) == int(2.0 * tc.SAMPLE_RATE * 2)
    assert isinstance(pcm, bytes)


def test_plan_snapshot_debounces_within_the_window():
    ring = _ring(5.0)
    # 1s after a capture at t=100 is inside the 3s refractory window.
    assert tc.plan_snapshot(ring, 2.0, now=101.0, last_capture_mono=100.0) is None
    # Past the window it fires again.
    assert tc.plan_snapshot(ring, 2.0, now=104.0, last_capture_mono=100.0) is not None


def test_plan_snapshot_clamps_to_the_ring_max():
    ring = _ring(tc.RING_MAX_SEC + 3.0)
    pcm = tc.plan_snapshot(ring, 99.0, now=100.0, last_capture_mono=0.0)
    assert len(pcm) == int(tc.RING_MAX_SEC * tc.SAMPLE_RATE * 2)


def test_plan_snapshot_skips_when_too_little_audio_buffered():
    # Only ~20ms in the ring — below MIN_CAPTURE_BYTES, so no clip AND (by
    # returning None) the caller must not arm its refractory window.
    ring = b"\x01\x02" * int(tc.SAMPLE_RATE * 0.02)
    assert tc.plan_snapshot(ring, 2.0, now=100.0, last_capture_mono=0.0) is None


def test_plan_snapshot_handles_zero_config_without_crashing():
    ring = _ring(5.0)
    # 0 / None fall back to the 0.1s floor rather than producing an empty slice.
    pcm = tc.plan_snapshot(ring, 0.0, now=100.0, last_capture_mono=0.0)
    assert pcm is not None and len(pcm) == int(0.1 * tc.SAMPLE_RATE * 2)
    assert tc.plan_snapshot(ring, None, now=100.0, last_capture_mono=0.0) is not None


def test_plan_snapshot_accepts_a_bytearray_ring():
    ring = bytearray(_ring(3.0))
    pcm = tc.plan_snapshot(ring, 1.0, now=100.0, last_capture_mono=0.0)
    assert isinstance(pcm, bytes) and len(pcm) == int(1.0 * tc.SAMPLE_RATE * 2)


# ─── models listing + deletion ───────────────────────────────────────────────

def test_list_models_reports_bucket_counts(tmp_path):
    db = _db(tmp_path)
    tc.save("hey_jarvis", "dev1", _pcm(20), "miss", 0.1, db_path=db, ts_ms=1)
    tc.save("computer", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=2)
    models = {m["model"]: m["counts"] for m in tc.list_models(db)}
    assert models["hey_jarvis"]["untriaged"] == 1
    assert models["computer"]["untriaged"] == 1


def test_delete_device_removes_only_its_own_across_models(tmp_path):
    db = _db(tmp_path)
    a = tc.save("hey_jarvis", "dev1", _pcm(20), "act", 0.8, db_path=db, ts_ms=1)
    tc.label("hey_jarvis", a, "positive", db)
    tc.save("computer", "dev1", _pcm(20), "miss", 0.1, db_path=db, ts_ms=2)
    tc.save("hey_jarvis", "dev2", _pcm(20), "miss", 0.1, db_path=db, ts_ms=3)
    assert tc.delete_device("dev1", db) == 2
    assert tc.list_captures("hey_jarvis", "positive", db) == []
    assert tc.list_captures("computer", "untriaged", db) == []
    assert len(tc.list_captures("hey_jarvis", "untriaged", db)) == 1
