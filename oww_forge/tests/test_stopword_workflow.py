import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge


def test_stop_new_writes_purpose_and_default_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "WAKEWORDS", tmp_path / "wakewords")
    monkeypatch.setattr(forge, "FORGE_DIR", Path(__file__).parents[1])
    forge.cmd_new(SimpleNamespace(
        phrase="stop", name="stop_test", samples=10, samples_val=2, steps=3,
        confusables="top,shop", google_tts_languages="en-US", google_tts_voices="",
        google_tts_samples_per_voice=1, google_tts_qps=1, force=False, kind="stop",
    ))
    cfg = yaml.safe_load((forge.WAKEWORDS / "stop_test" / "config.yml").read_text())
    assert cfg["model_kind"] == "stop"
    assert cfg["target_phrase"] == ["stop"]


def test_model_manifest_carries_purpose_and_checksum(tmp_path):
    model = tmp_path / "stop.onnx"
    model.write_bytes(b"model")
    manifest = forge.write_model_manifest("stop", {"model_kind": "stop", "target_phrase": ["stop"]}, model)
    data = __import__("json").loads(manifest.read_text())
    assert data["kind"] == "stop"
    assert data["model_sha256"] == hashlib.sha256(b"model").hexdigest()
