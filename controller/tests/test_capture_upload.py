import hashlib
import json
import struct
import zipfile
from io import BytesIO

import pytest

import em_capture_upload as upload
import em_training_captures as captures


def metadata(**overrides):
    value = {
        "captureId": "boot:1", "kind": "act", "model": "wake",
        "classifierMd5": "0123456789abcdef0123456789abcdef",
        "score": 0.8, "threshold": 0.5, "nearMissFloor": 0.1,
        "activationSeq": 20, "requestedPrerollMs": 160,
        "actualPrerollMs": 160, "complete": True,
        "sampleRate": 16000, "sampleWidth": 2, "channels": 1,
        "frameBytes": upload.FRAME_BYTES, "bargeThresholdActive": False,
    }
    value.update(overrides)
    return value


def begin(meta=None):
    encoded = json.dumps(meta or metadata()).encode()
    return bytes([upload.CAPTURE_BEGIN, upload.PROTOCOL_VERSION]) + struct.pack(">H", len(encoded)) + encoded


def pcm(index, value=1):
    return bytes([upload.CAPTURE_PCM]) + struct.pack(">H", index) + bytes([value]) * upload.FRAME_BYTES


def end(chunks):
    body = b"".join(chunks)
    return bytes([upload.CAPTURE_END]) + struct.pack(">HI", len(chunks), len(body)) + hashlib.md5(body).digest()


def complete(receiver=None, meta=None):
    receiver = receiver or upload.Receiver()
    chunks = [bytes([1]) * upload.FRAME_BYTES, bytes([2]) * upload.FRAME_BYTES]
    assert receiver.feed(begin(meta)) is None
    for index, chunk in enumerate(chunks):
        assert receiver.feed(bytes([upload.CAPTURE_PCM]) + struct.pack(">H", index) + chunk) is None
    return receiver.feed(end(chunks))


def test_valid_upload_round_trip():
    completed = complete()
    assert completed.metadata["captureId"] == "boot:1"
    assert len(completed.pcm) == 2 * upload.FRAME_BYTES


@pytest.mark.parametrize("bad", [
    lambda: b"",
    lambda: begin(metadata(kind="other")),
    lambda: begin(metadata(score=0.2)),
    lambda: begin(metadata(classifierMd5="bad")),
    lambda: bytes([upload.CAPTURE_BEGIN, 99, 0, 0]),
])
def test_malformed_begin_is_rejected_and_resets(bad):
    receiver = upload.Receiver()
    with pytest.raises(upload.CaptureProtocolError):
        receiver.feed(bad())
    assert receiver.metadata is None and receiver.chunks == []


@pytest.mark.parametrize("meta", [
    metadata(actualPrerollMs=80, complete=True),
    metadata(actualPrerollMs=160, complete=False),
    metadata(requestedPrerollMs=80, actualPrerollMs=160, complete=False),
])
def test_contradictory_preroll_completeness_is_rejected(meta):
    with pytest.raises(upload.CaptureProtocolError):
        upload.Receiver().feed(begin(meta))


def test_partial_out_of_order_oversized_and_digest_mismatch_are_rejected():
    receiver = upload.Receiver()
    receiver.feed(begin())
    with pytest.raises(upload.CaptureProtocolError):
        receiver.feed(pcm(1))

    receiver.feed(begin())
    for index in range(upload.MAX_FRAMES):
        receiver.feed(pcm(index))
    with pytest.raises(upload.CaptureProtocolError):
        receiver.feed(pcm(upload.MAX_FRAMES))

    receiver.feed(begin())
    receiver.feed(pcm(0))
    bad_end = bytes([upload.CAPTURE_END]) + struct.pack(">HI", 1, upload.FRAME_BYTES) + b"x" * 16
    with pytest.raises(upload.CaptureProtocolError):
        receiver.feed(bad_end)


def test_uploaded_capture_is_durable_deduplicated_and_sidecars_follow_lifecycle(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    completed = complete()
    name = captures.save_uploaded("wake", "device", completed.metadata, completed.pcm, db_path)
    assert name is not None
    path = captures.resolve("wake", name, db_path=db_path)
    assert path is not None and captures._meta_path(path).is_file()

    assert captures.save_uploaded("wake", "device", completed.metadata, completed.pcm, db_path) == name
    assert len(captures.list_captures("wake", "untriaged", db_path)) == 1

    assert captures.label("wake", name, "positive", db_path)
    moved = captures.resolve("wake", name, "positive", db_path)
    assert moved is not None and captures._meta_path(moved).is_file()
    archive = zipfile.ZipFile(BytesIO(captures.export_zip("wake", db_path)))
    assert f"positive/{name}" in archive.namelist()
    assert f"positive/{name}{captures.META_SUFFIX}" in archive.namelist()
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["clips"][0]["upload"]["captureId"] == "boot:1"

    assert captures.discard("wake", name, db_path)
    assert not moved.exists() and not captures._meta_path(moved).exists()


def test_conflicting_duplicate_is_not_acknowledgeable(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    completed = complete()
    assert captures.save_uploaded("wake", "device", completed.metadata, completed.pcm, db_path)
    conflict = dict(completed.metadata, score=0.9)
    assert captures.save_uploaded("wake", "device", conflict, completed.pcm, db_path) is None


def test_prune_and_device_delete_remove_uploaded_sidecars(tmp_path):
    db_path = str(tmp_path / "db.sqlite")
    first = complete(meta=metadata(captureId="boot:old"))
    second = complete(meta=metadata(captureId="boot:new"))
    old_name = captures.save_uploaded("wake", "device", first.metadata, first.pcm, db_path, cap=10)
    new_name = captures.save_uploaded("wake", "device", second.metadata, second.pcm, db_path, cap=10)
    removed = captures.prune_untriaged("wake", db_path, cap=1)
    assert old_name in removed
    directory = captures._bucket_dir("wake", "untriaged", db_path)
    assert not captures._meta_path(directory / old_name).exists()
    assert captures.delete_device("device", db_path) == 1
    assert not (directory / new_name).exists()
    assert not captures._meta_path(directory / new_name).exists()
