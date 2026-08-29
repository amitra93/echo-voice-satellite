import io
import json
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge
import forge_web


class Request:
    def __init__(self, body=None, name="demo", query=None, can_read_body=True):
        self.match_info = {"name": name}
        self.body = body or {}
        self.query = query or {}
        self.can_read_body = can_read_body

    async def json(self):
        return self.body


class ForgeGapTests(unittest.TestCase):
    def test_piper_features_and_archive_extractors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = forge.PIPER_CKPT, forge.FEATURES_DIR, forge.CREDENTIALS
            forge.PIPER_CKPT = root / "piper.pt"
            forge.FEATURES_DIR = root / "features"
            forge.CREDENTIALS = root / "credentials.json"
            try:
                with patch.object(forge, "download") as download:
                    forge.fetch_piper()
                    self.assertEqual(download.call_count, 2)
                forge.PIPER_CKPT.write_bytes(b"x")
                forge.PIPER_CKPT.with_suffix(".pt.json").write_text("{}")
                with patch.object(forge, "download") as download:
                    forge.fetch_piper()
                    download.assert_not_called()

                fake_hf = types.ModuleType("huggingface_hub")
                fake_hf.hf_hub_download = lambda **kw: str(root / kw["filename"])
                old_hf = sys.modules.get("huggingface_hub")
                sys.modules["huggingface_hub"] = fake_hf
                try:
                    forge.FEATURES_DIR.mkdir()
                    (forge.FEATURES_DIR / forge.NEGATIVE_FEATURES).write_bytes(b"n")
                    with patch.object(forge, "log"):
                        forge.fetch_features()
                    self.assertEqual(fake_hf.hf_hub_download.__name__, "<lambda>")
                finally:
                    if old_hf is None:
                        sys.modules.pop("huggingface_hub", None)
                    else:
                        sys.modules["huggingface_hub"] = old_hf

                archive = root / "musan.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    for name, data in (("a/x.wav", b"a"), ("b/y.txt", b"b")):
                        info = tarfile.TarInfo(name); info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                forge.MUSAN_ARCHIVE = archive; forge.MUSAN_DIR = root / "musan"
                forge.fetch_musan()
                self.assertTrue((forge.MUSAN_DIR / "a_x.wav").exists())

                slr = root / "slr.zip"
                with zipfile.ZipFile(slr, "w") as z:
                    z.writestr("RIRS_NOISES/RIRS_/r.wav", b"r")
                    z.writestr("RIRS_NOISES/noise.wav", b"n")
                forge.SLR28_ARCHIVE = slr
                forge.SLR28_RIR_DIR = root / "slr" / "rir"
                forge.SLR28_NOISE_DIR = root / "slr" / "noise"
                forge.fetch_slr28()
                self.assertTrue(list(forge.SLR28_RIR_DIR.glob("*.wav")))
                self.assertTrue(list(forge.SLR28_NOISE_DIR.glob("*.wav")))
            finally:
                forge.PIPER_CKPT, forge.FEATURES_DIR, forge.CREDENTIALS = old

    def test_decode_prefetch_and_score_resampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_sf = sys.modules.get("soundfile")
            sf = types.ModuleType("soundfile")
            calls = []
            def read(value, **kwargs):
                calls.append(value)
                if len(calls) == 1:
                    raise RuntimeError("unsupported")
                return np.zeros(8, dtype="float32"), 16000
            sf.read = read; sys.modules["soundfile"] = sf
            try:
                fake_sub = lambda *a, **kw: (Path(a[0][-1]).write_bytes(b"wav") or None)
                with patch.object(forge.subprocess, "run", fake_sub):
                    data, rate = forge._decode_audio_bytes(b"ogg", "clip.ogg")
                self.assertEqual(rate, 16000); self.assertEqual(len(data), 8)
            finally:
                if old_sf is None: sys.modules.pop("soundfile", None)
                else: sys.modules["soundfile"] = old_sf

            fake_api = types.SimpleNamespace(list_repo_files=lambda **kw: [
                "parquet-data/c/train-00001-of-00002.parquet",
                "c/train-00000-of-00002.parquet", "ignore.txt"])
            fake_hub = types.ModuleType("huggingface_hub")
            fake_hub.HfApi = lambda: fake_api
            fake_hub.hf_hub_download = lambda **kw: "/tmp/" + Path(kw["filename"]).name
            old_hf = sys.modules.get("huggingface_hub"); sys.modules["huggingface_hub"] = fake_hub
            try:
                paths = forge._prefetch_shard("repo", "c", "train")
                self.assertEqual([p.name for p in paths], ["train-00000-of-00002.parquet", "train-00001-of-00002.parquet"])
                with self.assertRaises(FileNotFoundError): forge._prefetch_shard("repo", "x", "train")
            finally:
                if old_hf is None: sys.modules.pop("huggingface_hub", None)
                else: sys.modules["huggingface_hub"] = old_hf

            old_librosa = sys.modules.get("librosa")
            lib = types.ModuleType("librosa"); lib.resample = lambda a, **kw: np.resize(a, 32000)
            sys.modules["librosa"] = lib
            old_sf = sys.modules.get("soundfile")
            sf = types.ModuleType("soundfile"); sf.read = lambda p, **kw: (np.zeros((4, 2), dtype="int16"), 8000)
            sys.modules["soundfile"] = sf
            try:
                model = SimpleNamespace(reset=lambda: None, predict=lambda a: {"wake": .7})
                self.assertEqual(forge.score_wav_file(model, root / "anything"), .7)
            finally:
                if old_librosa is None: sys.modules.pop("librosa", None)
                else: sys.modules["librosa"] = old_librosa
                if old_sf is None: sys.modules.pop("soundfile", None)
                else: sys.modules["soundfile"] = old_sf

    def test_cmd_new_eval_and_google_dispatch_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = forge.WAKEWORDS; forge.WAKEWORDS = Path(tmp)
            try:
                args = SimpleNamespace(phrase="hello", name="demo", samples=1, samples_val=1,
                    steps=1, confusables="", google_tts_languages="en-US", google_tts_voices="",
                    google_tts_samples_per_voice=1, google_tts_qps=1, force=False)
                forge.cmd_new(args)
                with self.assertRaises(SystemExit): forge.cmd_new(args)
                with self.assertRaises(SystemExit): forge.cmd_new(SimpleNamespace(**{**vars(args), "google_tts_qps": 0, "force": True}))
                fake = types.ModuleType("google_tts"); fake.DEFAULT_QPS = 2; fake.synthesize = lambda **kw: None
                oldmod = sys.modules.get("google_tts"); sys.modules["google_tts"] = fake
                try:
                    forge.cmd_google_tts(SimpleNamespace(name="demo", samples=1, languages="en", voices="v", yes=True, qps=1))
                finally:
                    if oldmod is None: sys.modules.pop("google_tts", None)
                    else: sys.modules["google_tts"] = oldmod
            finally: forge.WAKEWORDS = old

    def test_evaluate_model_all_source_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old_m, old_w = forge.MODELS, forge.WAKEWORDS
            forge.MODELS = root / "models"; forge.WAKEWORDS = root / "ww"
            (forge.MODELS).mkdir(); (forge.MODELS / "demo.onnx").write_bytes(b"x")
            cfgdir = forge.WAKEWORDS / "demo"; cfgdir.mkdir(parents=True)
            work = root / "work"; (work / "positive_test").mkdir(parents=True); (work / "negative_test").mkdir()
            (cfgdir / "config.yml").write_text(yaml.safe_dump({"output_dir": str(root), "model_name": "work"}))
            for n in ("google_en-US_v.wav", "custom_x.wav", "piper.wav"):
                (work / "positive_test" / n).write_bytes(b"x")
            for n in ("google_x.wav", "custom_x.wav", "piper.wav"):
                (work / "negative_test" / n).write_bytes(b"x")
            with patch.object(forge, "features_stale", return_value=False), patch.object(
                forge, "_score_files_parallel", side_effect=lambda model, files: {p: .6 if p.parent.name.startswith("positive") else .1 for p in files}):
                forge.evaluate_model("demo")
            forge.MODELS, forge.WAKEWORDS = old_m, old_w

    def test_small_asset_readers_and_build_option_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = {n: getattr(forge, n) for n in (
                "FEATURES_DIR", "RIR_DIR", "FMA_DIR", "AUDIOSET_DIR", "MUSAN_DIR",
                "SLR28_RIR_DIR", "SLR28_NOISE_DIR", "WAKEWORDS", "MODELS", "PIPER_CKPT")}
            forge.FEATURES_DIR = root / "features"; forge.RIR_DIR = root / "rirs"
            forge.FMA_DIR = root / "fma"; forge.AUDIOSET_DIR = root / "audio"
            forge.MUSAN_DIR = root / "musan"; forge.SLR28_RIR_DIR = root / "slr" / "rirs"
            forge.SLR28_NOISE_DIR = root / "slr" / "noise"; forge.WAKEWORDS = root / "ww"
            forge.MODELS = root / "models"
            forge.PIPER_CKPT = root / "piper.pt"
            try:
                fake_ds = types.ModuleType("datasets")
                fake_ds.load_dataset = lambda *a, **kw: [{"audio": {"array": np.zeros(4), "sampling_rate": 16000}}]
                old_ds = sys.modules.get("datasets"); sys.modules["datasets"] = fake_ds
                old_lib = sys.modules.get("librosa"); sys.modules["librosa"] = types.ModuleType("librosa")
                old_sf = sys.modules.get("soundfile")
                fake_sf = types.ModuleType("soundfile"); fake_sf.write = lambda path, *a, **kw: Path(path).write_bytes(b"wav")
                sys.modules["soundfile"] = fake_sf
                try:
                    forge.RIR_DIR.mkdir(); forge.FMA_DIR.mkdir(); forge.AUDIOSET_DIR.mkdir()
                    forge.PIPER_CKPT.write_bytes(b"x")
                    forge.PIPER_CKPT.with_suffix(".pt.json").write_text("{}")
                    forge.fetch_rirs(); forge.fetch_fma(1)
                    self.assertTrue(list(forge.RIR_DIR.glob("*.wav")))
                    self.assertTrue(list(forge.FMA_DIR.glob("*.wav")))
                finally:
                    if old_ds is None: sys.modules.pop("datasets", None)
                    else: sys.modules["datasets"] = old_ds
                    if old_lib is None: sys.modules.pop("librosa", None)
                    else: sys.modules["librosa"] = old_lib
                    if old_sf is None: sys.modules.pop("soundfile", None)
                    else: sys.modules["soundfile"] = old_sf

                # Build's optional feature/background wiring is pure config logic.
                cfgdir = forge.WAKEWORDS / "demo"; cfgdir.mkdir(parents=True)
                work = root / "work" / "demo"; work.mkdir(parents=True)
                (cfgdir / "config.yml").write_text(yaml.safe_dump({
                    "model_name": "demo", "output_dir": str(root / "work"),
                    "use_common_voice_negatives": True, "use_fleurs_negatives": True,
                    "use_voxpopuli_negatives": True, "use_musan_background": True,
                    "use_slr28_augmentation": True, "use_ami_farfield_validation": True,
                }))
                for p in (forge.FEATURES_DIR / forge.NEGATIVE_FEATURES,
                          forge.FEATURES_DIR / forge.VALIDATION_FEATURES,
                          forge.FEATURES_DIR / forge.COMMON_VOICE_FEATURES,
                          forge.FEATURES_DIR / forge.FLEURS_FEATURES,
                          forge.FEATURES_DIR / forge.VOXPOPULI_FEATURES,
                          forge.FEATURES_DIR / forge.AMI_VALIDATION_FEATURES):
                    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"x")
                for d in (forge.AUDIOSET_DIR, forge.FMA_DIR, forge.MUSAN_DIR, forge.RIR_DIR,
                          forge.SLR28_RIR_DIR, forge.SLR28_NOISE_DIR):
                    d.mkdir(parents=True, exist_ok=True); (d / "x.wav").write_bytes(b"x")
                fake_torch = types.ModuleType("torch"); fake_torch.__version__ = "x"
                fake_torch.cuda = SimpleNamespace(is_available=lambda: True, get_device_name=lambda _: "fake")
                commands = []
                def run(cmd, check):
                    commands.append(cmd)
                    if "--train_model" in cmd:
                        (cfgdir / "demo.onnx").write_bytes(b"model")
                with patch.dict(sys.modules, {"torch": fake_torch}), patch.object(forge, "features_stale", return_value=False), patch.object(forge.subprocess, "run", run):
                    forge.cmd_build(SimpleNamespace(name="demo", from_step="train", only_step=None, overwrite=False))
                cfg = yaml.safe_load((cfgdir / "config.yml").read_text())
                self.assertEqual(cfg["batch_n_per_class"]["ACAV100M_sample"], 704)
                self.assertEqual(len(cfg["background_paths"]), 4)
                self.assertEqual(len(commands), 1)
            finally:
                for n, value in old.items(): setattr(forge, n, value)

    def test_piper_cli_and_audio_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old_w = forge.WAKEWORDS; forge.WAKEWORDS = root / "ww"
            cfgdir = forge.WAKEWORDS / "demo"; cfgdir.mkdir(parents=True)
            (cfgdir / "config.yml").write_text("model_name: demo\noutput_dir: %s\ntarget_phrase: [hi]\n" % root)
            fake = types.ModuleType("piper_voices"); fake.languages = lambda a: [{"language": "en", "voices": 1, "max_speakers": 1, "label": "English"}]
            fake.catalogue = lambda a: [{"language": "en_GB", "speakers": 1, "quality": "x", "name": "v"}]
            fake.synthesize = lambda **kw: kw
            old_p = sys.modules.get("piper_voices"); sys.modules["piper_voices"] = fake
            try:
                with patch("builtins.print"):
                    forge.cmd_voices(SimpleNamespace(language=None)); forge.cmd_voices(SimpleNamespace(language="en_GB"))
                forge.cmd_piper_voices(SimpleNamespace(name="demo", samples=2, language="en_GB", voices="v"))
                with patch.object(forge.subprocess, "run") as run:
                    forge._convert_16k(root / "a.ogg", root / "b.wav")
                    run.assert_called_once()
            finally:
                forge.WAKEWORDS = old_w
                if old_p is None: sys.modules.pop("piper_voices", None)
                else: sys.modules["piper_voices"] = old_p


class WebGapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.old = forge.WAKEWORDS, forge.MODELS
        forge.WAKEWORDS = self.root / "ww"; forge.MODELS = self.root / "models"
        (forge.WAKEWORDS / "demo").mkdir(parents=True); forge.MODELS.mkdir()
        (forge.WAKEWORDS / "demo" / "config.yml").write_text(yaml.safe_dump({"model_name": "demo", "output_dir": str(self.root / "work")}))
        self.old_tmp, self.old_logs, self.old_job = forge_web.TMP, forge_web.LOGS, forge_web._job
        forge_web.TMP = self.root / "tmp"; forge_web.LOGS = self.root / "logs"; forge_web._job = None

    def tearDown(self):
        forge.WAKEWORDS, forge.MODELS = self.old
        forge_web.TMP, forge_web.LOGS, forge_web._job = self.old_tmp, self.old_logs, self.old_job
        self.tmp.cleanup()

    async def test_create_import_and_build_guards(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_wakeword_create(Request({"phrase": ""}, name="x"))
        with patch.object(forge, "cmd_new") as new:
            response = await forge_web.api_wakeword_create(Request({"phrase": "hello", "name": "h"}, name="x"))
            self.assertEqual(response.status, 200); new.assert_called_once()
        with patch.object(forge_web, "_save_uploads", AsyncMock(return_value=[])):
            with self.assertRaises(forge_web.web.HTTPBadRequest): await forge_web.api_import_dataset(Request())
        running = SimpleNamespace(poll=lambda: None, label="demo")
        forge_web._job = running
        with self.assertRaises(forge_web.web.HTTPConflict): await forge_web.api_build(Request())
        forge_web._job = None
        with patch.object(forge, "missing_assets", return_value=["piper"]):
            with self.assertRaises(forge_web.web.HTTPConflict): await forge_web.api_build(Request())

    async def test_job_log_cancel_test_and_google_handlers(self):
        class Field:
            name = "wav"; filename = "x.wav"
            def __init__(self): self.parts = iter([b"a", b""])
            async def read_chunk(self): return next(self.parts)
        class Reader:
            def __aiter__(self): return self
            async def __anext__(self):
                if hasattr(self, "done"): raise StopAsyncIteration
                self.done = True; return Field()
        req = Request()
        async def multipart(): return Reader()
        req.multipart = multipart
        paths = await forge_web._save_uploads(req, "wav")
        self.assertEqual(len(paths), 1); paths[0].unlink()
        with patch.object(forge_web, "_start_job") as start:
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_google_tts(Request({"samples": "bad"}))
        with patch.object(forge_web.subprocess, "run", side_effect=RuntimeError("gpu")):
            forge_web._gpu_info = None
            self.assertIn("gpu", forge_web._gpu()["error"])

    async def test_job_lifecycle_and_lightweight_handler_errors(self):
        class Proc:
            pid = 123
            def __init__(self): self.rc = None
            def poll(self): return self.rc
        proc = Proc()
        with patch.object(forge_web.subprocess, "Popen", return_value=proc), patch.object(forge_web, "_job_env", return_value={"X": "1"}):
            job = forge_web.Job("build", "demo", ["build", "demo"])
        self.assertIsNone(job.poll())
        job._note("hello\n")
        proc.rc = 0
        self.assertEqual(job.poll(), 0)
        self.assertFalse(job.as_dict()["running"])
        self.assertFalse(job.as_dict()["cancelled"])
        with patch.object(forge_web, "LOGS", self.root / "newlogs"), patch.object(forge_web.subprocess, "Popen", return_value=Proc()):
            forge_web._job = None
            forge_web._start_job("x", "x", [])
            with self.assertRaises(forge_web.web.HTTPConflict): forge_web._start_job("y", "y", [])
        forge_web._job = None
        with patch.object(forge_web, "_start_job") as start:
            response = await forge_web.api_assets_download(Request({"only": "rirs"}))
            self.assertTrue(json.loads(response.body)["ok"])
            self.assertEqual(start.call_args.args[2], ["assets", "--only", "rirs"])
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_piper_voices(Request({"samples": "bad"}))
        with patch.object(forge_web, "_start_job"):
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_google_tts(Request({"samples": 1, "qps": -1}))

    async def test_job_cancel_and_successful_gpu_probe(self):
        class Proc:
            pid = 456
            def __init__(self): self.rc = None
            def poll(self): return self.rc
        proc = Proc()
        with patch.object(forge_web, "LOGS", self.root / "cancel-logs"), patch.object(
                forge_web.subprocess, "Popen", return_value=proc):
            job = forge_web.Job("build", "cancel me", [])
        def terminate(_pgid, _signal): proc.rc = -15
        with patch.object(forge_web.os, "getpgid", return_value=999), patch.object(
                forge_web.os, "killpg", side_effect=terminate):
            job.cancel()
        self.assertTrue(job.cancelled)
        self.assertEqual(job.rc, -15)

        result = '{"available": true, "device": "Fake GPU", "torch": "x"}\n'
        with patch.object(forge_web, "_gpu_info", None), patch.object(
                forge_web.subprocess, "run", return_value=SimpleNamespace(stdout=result)):
            self.assertEqual(forge_web._gpu()["device"], "Fake GPU")


if __name__ == "__main__": unittest.main()
