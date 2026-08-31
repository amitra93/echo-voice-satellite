import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge
import forge_web
from feature_store import archive_checksum, ResumableFeatureWriter, CheckpointError


class Req:
    def __init__(self, body=None, name="demo", query=None, can_read_body=True):
        self.match_info = {"name": name}; self.body = body or {}; self.query = query or {}
        self.can_read_body = can_read_body
    async def json(self): return self.body


class RemainingForgeTests(unittest.TestCase):
    def test_main_parser_builds_all_commands(self):
        old_argv = sys.argv
        try:
            sys.argv = ["forge.py", "eval", "demo"]
            with patch.object(forge, "cmd_eval") as command:
                forge.main()
                command.assert_called_once()
        finally:
            sys.argv = old_argv

    def test_archive_checksum_and_writer_rejection_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"; p.write_bytes(b"abc")
            self.assertEqual(archive_checksum(p), __import__("hashlib").sha256(b"abc").hexdigest())
            p.with_suffix(".bin.checksum").write_text("A" * 64)
            self.assertEqual(archive_checksum(p), "a" * 64)
            dest = Path(tmp) / "out.npy"
            writer = ResumableFeatureWriter(dest, (2, 3), "x", "v")
            with self.assertRaises(ValueError): writer.append(np.zeros((1, 4), dtype="float32"))
            with self.assertRaises(ValueError): writer.append(np.zeros((3, 3), dtype="float32"))
            writer.part.unlink()
            with self.assertRaises(CheckpointError): ResumableFeatureWriter(dest, (2, 3), "x", "v")

    def test_missing_assets_optional_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ("PIPER_CKPT", "FEATURES_DIR", "RIR_DIR", "AUDIOSET_DIR", "FMA_DIR", "COMMON_VOICE_DIR", "MUSAN_DIR", "SLR28_RIR_DIR")
            old = {n: getattr(forge, n) for n in names}
            forge.PIPER_CKPT = root / "p.pt"; forge.FEATURES_DIR = root / "features"; forge.RIR_DIR = root / "rir"; forge.AUDIOSET_DIR = root / "a"; forge.FMA_DIR = root / "f"; forge.COMMON_VOICE_DIR = root / "cv"; forge.MUSAN_DIR = root / "m"; forge.SLR28_RIR_DIR = root / "slr"
            try:
                cfg = {k: True for k in ("use_common_voice_negatives", "use_ami_farfield_validation", "use_fleurs_negatives", "use_voxpopuli_negatives", "use_musan_background", "use_slr28_augmentation")}
                missing = forge.missing_assets(cfg)
                self.assertEqual(len(missing), 11)
                for f in (forge.PIPER_CKPT, forge.PIPER_CKPT.with_suffix(".pt.json")): f.write_bytes(b"x")
                forge.FEATURES_DIR.mkdir()
                for f in (forge.NEGATIVE_FEATURES, forge.VALIDATION_FEATURES, forge.COMMON_VOICE_FEATURES, forge.AMI_VALIDATION_FEATURES, forge.FLEURS_FEATURES, forge.VOXPOPULI_FEATURES): (forge.FEATURES_DIR / f).write_bytes(b"x")
                forge.RIR_DIR.mkdir(); (forge.RIR_DIR / "x.wav").write_bytes(b"x")
                forge.AUDIOSET_DIR.mkdir(); (forge.AUDIOSET_DIR / "x.wav").write_bytes(b"x")
                forge.MUSAN_DIR.mkdir(); (forge.MUSAN_DIR / "x.wav").write_bytes(b"x")
                forge.SLR28_RIR_DIR.mkdir(); (forge.SLR28_RIR_DIR / "x.wav").write_bytes(b"x")
                forge.COMMON_VOICE_DIR.mkdir(); (forge.COMMON_VOICE_DIR / "x.tar.gz").write_bytes(b"x")
                self.assertEqual(forge.missing_assets(cfg), [])
            finally:
                for n, value in old.items(): setattr(forge, n, value)

    def test_score_parallel_and_evaluate(self):
        class Model:
            def __init__(self, **kw): pass
            def reset(self): pass
            def predict(self, audio): return {"wake": 0.6}
        fake_oww = types.ModuleType("openwakeword.model"); fake_oww.Model = Model
        old = sys.modules.get("openwakeword.model"); sys.modules["openwakeword.model"] = fake_oww
        try:
            with tempfile.TemporaryDirectory() as tmp:
                files = [Path(tmp) / f"{n}.wav" for n in ("google_en-US_v_000000", "custom_x", "plain")]
                for p in files: p.write_bytes(b"x")
                with patch.object(forge, "score_wav_file", return_value=.4):
                    self.assertEqual(forge._score_files_parallel(Path("model"), files, 2), {p: .4 for p in files})
                old_models, old_ww = forge.MODELS, forge.WAKEWORDS
                forge.MODELS = Path(tmp) / "models"; forge.WAKEWORDS = Path(tmp) / "ww"; forge.MODELS.mkdir(); forge.WAKEWORDS.mkdir()
                (forge.MODELS / "demo.onnx").write_bytes(b"x")
                cfgdir = forge.WAKEWORDS / "demo"; cfgdir.mkdir(); work = Path(tmp) / "work"; work.mkdir()
                (cfgdir / "config.yml").write_text(yaml.safe_dump({"output_dir": str(tmp), "model_name": "work"}))
                for d, n in (("positive_test", "google_en-US_v_000000.wav"), ("negative_test", "custom_n.wav")):
                    (work / d).mkdir(); (work / d / n).write_bytes(b"x")
                with patch.object(forge, "_score_files_parallel", return_value={p: .8 for p in (work / "positive_test").glob("*")} | {p: .1 for p in (work / "negative_test").glob("*")}), patch.object(forge, "features_stale", return_value=True):
                    forge.evaluate_model("demo")
                (work / "positive_test").rename(work / "positive_test_old")
                with patch.object(forge, "score_wav_file", return_value=.1):
                    forge.evaluate_model("demo")
                forge.MODELS, forge.WAKEWORDS = old_models, old_ww
        finally:
            if old is None: sys.modules.pop("openwakeword.model", None)
            else: sys.modules["openwakeword.model"] = old

    def test_cmd_test_and_simple_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = forge.MODELS; forge.MODELS = Path(tmp); (forge.MODELS / "demo.onnx").write_bytes(b"x")
            fake = types.ModuleType("openwakeword.model")
            fake.Model = lambda **kw: SimpleNamespace()
            oldmod = sys.modules.get("openwakeword.model"); sys.modules["openwakeword.model"] = fake
            try:
                (Path(tmp) / "sample.wav").write_bytes(b"wav")
                with patch.object(forge, "score_wav_file", return_value=.25), patch("builtins.print") as out:
                    forge.cmd_test(SimpleNamespace(name="demo", wav=[tmp]))
                    out.assert_called()
                empty = Path(tmp) / "empty"; empty.mkdir()
                with self.assertRaises(SystemExit): forge.cmd_test(SimpleNamespace(name="demo", wav=[str(empty)]))
            finally:
                forge.MODELS = old
                if oldmod is None: sys.modules.pop("openwakeword.model", None)
                else: sys.modules["openwakeword.model"] = oldmod


class RemainingWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.old = {n: getattr(forge, n) for n in ("WAKEWORDS", "MODELS", "FEATURES_DIR", "PIPER_CKPT", "CREDENTIALS")}
        self.old_logs = forge_web.LOGS
        forge.WAKEWORDS = self.root / "ww"; forge.MODELS = self.root / "models"; forge.FEATURES_DIR = self.root / "features"; forge.PIPER_CKPT = self.root / "piper"
        cfg = forge.WAKEWORDS / "demo"; cfg.mkdir(parents=True)
        self.config = cfg / "config.yml"; self.config.write_text(yaml.safe_dump({"model_name": "demo", "output_dir": str(self.root / "work")}))
        forge_web._job = None
        forge_web.LOGS = self.root / "logs"
        forge.CREDENTIALS = self.root / "credentials.json"
    def tearDown(self):
        for n, value in self.old.items(): setattr(forge, n, value)
        forge_web.LOGS = self.old_logs
        self.tmp.cleanup()

    async def test_state_credentials_logs_and_delete_conflict(self):
        with patch.object(forge_web, "_gpu", return_value={"available": False}):
            response = await forge_web.api_state(Req())
            self.assertIn("wakewords", json.loads(response.body))
        response = await forge_web.api_mdc_api_key(Req({"api_key": " secret "}))
        self.assertEqual(response.status, 200)
        with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_mdc_api_key(Req({}))
        response = await forge_web.api_log(Req())
        self.assertEqual(json.loads(response.body)["data"], "")
        class Running:
            label = "demo build"
            def poll(self): return None
        forge_web._job = Running()
        with self.assertRaises(forge_web.web.HTTPConflict): await forge_web.api_delete(Req())

    async def test_piper_preview_voices_and_evaluate(self):
        with patch.object(forge_web, "_start_job") as start:
            response = await forge_web.api_piper_voices(Req({"samples": 2, "language": "en_GB", "voices": "a"}))
            self.assertEqual(response.status, 200); self.assertIn("--voices", start.call_args.args[2])
        with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_piper_voices(Req({"samples": -1}))
        piper = types.ModuleType("piper_voices"); piper.catalogue = lambda assets: [{"language": "en", "name": "a"}]; piper.languages = lambda assets: [{"language": "en"}]
        old = sys.modules.get("piper_voices"); sys.modules["piper_voices"] = piper
        try:
            self.assertEqual(json.loads((await forge_web.api_voices(Req(query={"language": "en"}))).body), [{"language": "en", "name": "a"}])
            self.assertEqual(json.loads((await forge_web.api_voices(Req())).body), [{"language": "en"}])
        finally:
            if old is None: sys.modules.pop("piper_voices", None)
            else: sys.modules["piper_voices"] = old
        with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_preview(Req({"text": ""}))
        with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_preview(Req({"text": "x" * 201}))
        with patch.object(forge_web.asyncio, "to_thread", return_value=b"wav"):
            self.assertEqual((await forge_web.api_preview(Req({"text": "hello"}))).content_type, "audio/wav")
        (forge.MODELS).mkdir(); (forge.MODELS / "demo.onnx").write_bytes(b"x")
        with patch.object(forge_web, "_start_job") as start:
            response = await forge_web.api_evaluate(Req())
            self.assertTrue(json.loads(response.body)["ok"]); start.assert_called_once()


if __name__ == "__main__": unittest.main()
