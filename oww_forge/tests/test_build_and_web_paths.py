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

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge
import forge_web


class Request:
    def __init__(self, body=None, name="demo", can_read_body=True, query=None):
        self.match_info = {"name": name}
        self.body = body or {}
        self.can_read_body = can_read_body
        self.query = query or {}

    async def json(self):
        return self.body


class BuildAndWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = {n: getattr(forge, n) for n in ("WAKEWORDS", "MODELS", "FEATURES_DIR", "RIR_DIR", "AUDIOSET_DIR", "FMA_DIR", "MUSAN_DIR", "SLR28_RIR_DIR", "SLR28_NOISE_DIR", "PIPER_CKPT")}
        forge.WAKEWORDS = self.root / "wakewords"
        forge.MODELS = self.root / "models"
        forge.FEATURES_DIR = self.root / "features"
        forge.RIR_DIR = self.root / "rirs"
        forge.AUDIOSET_DIR = self.root / "audio"
        forge.FMA_DIR = self.root / "fma"
        forge.MUSAN_DIR = self.root / "musan"
        forge.SLR28_RIR_DIR = self.root / "slr28" / "rirs"
        forge.SLR28_NOISE_DIR = self.root / "slr28" / "noise"
        forge.PIPER_CKPT = self.root / "piper.pt"
        self.cfg_dir = forge.WAKEWORDS / "demo"
        self.cfg_dir.mkdir(parents=True)
        self.work = self.root / "work" / "demo"
        self.config_path = self.cfg_dir / "config.yml"
        self.config_path.write_text(yaml.safe_dump({
            "model_name": "demo", "output_dir": str(self.root / "work"),
            "target_phrase": ["hello"], "augmentation_rounds": 1,
        }))
        self.old_job = forge_web._job
        forge_web._job = None

    def tearDown(self):
        forge_web._job = self.old_job
        for n, value in self.old.items(): setattr(forge, n, value)
        self.tmp.cleanup()

    def _assets_ready(self):
        forge.PIPER_CKPT.write_bytes(b"x"); forge.PIPER_CKPT.with_suffix(".pt.json").write_text("{}")
        forge.FEATURES_DIR.mkdir(); (forge.FEATURES_DIR / forge.NEGATIVE_FEATURES).write_bytes(b"x")
        (forge.FEATURES_DIR / forge.VALIDATION_FEATURES).write_bytes(b"x")
        forge.RIR_DIR.mkdir(); (forge.RIR_DIR / "r.wav").write_bytes(b"x")
        forge.AUDIOSET_DIR.mkdir(); (forge.AUDIOSET_DIR / "a.wav").write_bytes(b"x")
        forge.FMA_DIR.mkdir(); (forge.FMA_DIR / "f.wav").write_bytes(b"x")
        forge.MUSAN_DIR.mkdir(); (forge.MUSAN_DIR / "m.wav").write_bytes(b"x")
        forge.SLR28_RIR_DIR.mkdir(parents=True); (forge.SLR28_RIR_DIR / "s.wav").write_bytes(b"x")

    def test_cmd_build_resolves_options_and_runs_steps(self):
        self._assets_ready()
        for d in forge.CLIP_DIRS: (self.work / d).mkdir(parents=True, exist_ok=True)
        for f in forge.FEATURE_FILES: (self.work / f).write_bytes(b"x")
        forge.write_feature_manifest(self.work, {"augmentation_rounds": 1})
        calls = []
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
        fake_torch.__version__ = "test"
        args = SimpleNamespace(name="demo", from_step="generate", only_step=None, overwrite=False)
        def run(cmd, check):
            calls.append(cmd)
            if "--train_model" in cmd:
                (forge.WAKEWORDS / "demo" / "demo.onnx").write_bytes(b"model")
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.object(forge, "missing_assets", return_value=[]), patch.object(forge.subprocess, "run", run), patch.object(forge, "evaluate_model"):
            forge.cmd_build(args)
        self.assertEqual([c[-1] for c in calls], ["--generate_clips", "--augment_clips", "--train_model"])
        cfg = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(cfg["background_paths_duplication_rate"], [1, 1])
        self.assertEqual(cfg["batch_n_per_class"]["ACAV100M_sample"], 1024)

    def test_cmd_build_stale_train_refuses_and_missing_name_refuses(self):
        with self.assertRaises(SystemExit):
            forge.cmd_build(SimpleNamespace(name="missing", from_step="generate", only_step=None, overwrite=False))
        self._assets_ready()
        self.config_path.write_text(yaml.safe_dump({"model_name": "demo", "output_dir": str(self.root / "work")}))
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
        fake_torch.__version__ = "test"
        with patch.dict(sys.modules, {"torch": fake_torch}), self.assertRaises(SystemExit) as caught:
            forge.cmd_build(SimpleNamespace(name="demo", from_step="train", only_step=None, overwrite=False))
        self.assertIn("rerun from augment", str(caught.exception))

    async def test_web_dataset_options_confusables_and_build_validation(self):
        await forge_web.api_dataset_options(Request({"use_musan_background": True, "use_slr28_augmentation": True}))
        cfg = yaml.safe_load(self.config_path.read_text())
        self.assertTrue(cfg["use_musan_background"])
        response = await forge_web.api_confusables(Request({"phrases": [" B", "a", "b", " ", "A"]}))
        self.assertEqual(json.loads(response.body)["phrases"], ["b", "a"])
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_confusables(Request({"phrases": "bad"}))
        with patch.object(forge_web, "_start_job") as start:
            self._assets_ready()
            response = await forge_web.api_build(Request({"from_step": "augment"}))
            self.assertEqual(response.status, 200)
            start.assert_called_once()
        forge.PIPER_CKPT.unlink()
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_build(Request({}))

    async def test_web_google_tts_save_and_error_validation(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_save_google_tts_config(Request({"samples": "x", "languages": "en", "qps": 1}))
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_save_google_tts_config(Request({"samples": 1, "languages": "", "qps": 1}))
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_save_google_tts_config(Request({"samples": 1, "languages": "en", "qps": "x"}))
        with patch.object(forge_web, "_start_job") as start:
            response = await forge_web.api_google_tts(Request({"samples": 2, "languages": "en-US", "voices": "Voice", "qps": 1}))
            self.assertEqual(response.status, 200)
            self.assertIn("--voices", start.call_args.args[2])

    async def test_web_misc_helpers_and_state(self):
        self.assertEqual(forge_web._count(self.root / "none"), 0)
        self.assertEqual(forge_web._size_mb(self.root / "none"), 0)
        with self.assertRaises(forge_web.web.HTTPNotFound): forge_web._require_wakeword("nope")
        with patch.object(forge_web, "_gpu_info", None), patch.object(forge_web.subprocess, "run", side_effect=RuntimeError("no torch")):
            self.assertFalse(forge_web._gpu()["available"])
            self.assertFalse(forge_web._gpu()["available"])
        app = forge_web.make_app()
        self.assertGreater(len(list(app.router.routes())), 10)

    async def test_web_delete_and_model_download_guards(self):
        with self.assertRaises(forge_web.web.HTTPNotFound):
            await forge_web.api_model_download(Request(name="demo"))
        (forge.MODELS).mkdir()
        (forge.MODELS / "demo.onnx").write_bytes(b"model")
        response = await forge_web.api_model_download(Request(name="demo"))
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="demo.onnx"')
        (forge.WAKEWORDS / "demo" / "extra").mkdir()
        await forge_web.api_delete(Request(name="demo"))
        self.assertFalse((forge.WAKEWORDS / "demo").exists())
        self.assertFalse((forge.MODELS / "demo.onnx").exists())


if __name__ == "__main__":
    unittest.main()
