import io
import json
import sys
import tarfile
import tempfile
import types
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge


class ForgeHelperTests(unittest.TestCase):
    def test_small_helpers_and_credentials(self):
        self.assertEqual(forge._duration(None), "ETA unavailable")
        self.assertEqual(forge._duration(-1), "ETA unavailable")
        self.assertEqual(forge._duration(3661), "1h 01m")
        self.assertEqual(forge._duration(61), "1m 01s")
        self.assertEqual(forge._duration(3), "3s")
        self.assertEqual(forge.slugify(" Hey, Biscuit! "), "hey_biscuit")
        with tempfile.TemporaryDirectory() as tmp:
            old = forge.CREDENTIALS
            forge.CREDENTIALS = Path(tmp) / "credentials.json"
            try:
                self.assertIsNone(forge.mdc_api_key())
                with patch.dict("os.environ", {"MDC_API_KEY": "env-key"}):
                    self.assertEqual(forge.mdc_api_key(), "env-key")
                    self.assertEqual(forge.masked_mdc_api_key(), "*******")
                forge.save_mdc_api_key("saved-key")
                self.assertEqual(forge.mdc_api_key(), "saved-key")
                self.assertEqual(forge.masked_mdc_api_key(), "*********")
                forge.CREDENTIALS.write_text("not json")
                self.assertIsNone(forge.mdc_api_key())
            finally:
                forge.CREDENTIALS = old

    def test_progress_rate_limit_and_download_part(self):
        with patch.object(forge, "monotonic", side_effect=[10.0, 10.0, 12.0]), patch.object(forge, "log") as log:
            self.assertEqual(forge._progress("x", 1, 2, 10, 10), 10)
            self.assertNotEqual(forge._progress("x", 2, 2, 10, 0), 0)
            self.assertEqual(forge._progress("x", 2, None, 10, 0, force=True), 12)
            self.assertEqual(log.call_count, 2)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "nested" / "file.bin"
            def fake_urlretrieve(url, part, reporthook):
                Path(part).write_bytes(b"payload")
                reporthook(1, 7, 7)
            with patch.object(forge.urllib.request, "urlretrieve", fake_urlretrieve):
                forge.download("https://example.test/file", dest)
            self.assertEqual(dest.read_bytes(), b"payload")
            self.assertFalse(dest.with_suffix(".bin.part").exists())

    def test_audio_clip_and_tar_member_decoder(self):
        class FakeLibrosa:
            @staticmethod
            def resample(audio, orig_sr, target_sr):
                return np.resize(audio, 32000)
        old = sys.modules.get("librosa")
        sys.modules["librosa"] = FakeLibrosa
        clip = forge._audio_16k_clip(np.array([[2.0], [-2.0]]), 8000)
        self.assertEqual(clip.dtype, np.int16)
        self.assertEqual(len(clip), forge.FEATURE_CLIP_SAMPLES)
        self.assertEqual(clip.max(), 32767)
        self.assertEqual(clip.min(), -32767)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "clips.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                data = io.BytesIO(b"not audio")
                info = tarfile.TarInfo("skip.txt")
                info.size = len(data.getvalue())
                tar.addfile(info, data)
                data = io.BytesIO(b"bad audio")
                info = tarfile.TarInfo("keep.wav")
                info.size = len(data.getvalue())
                tar.addfile(info, data)
            old_sf = sys.modules.get("soundfile")
            sf = types.ModuleType("soundfile")
            sf.read = lambda stream, dtype=None: (np.zeros(32000, dtype="float32"), 16000)
            sys.modules["soundfile"] = sf
            try:
                rows = list(forge.iter_decoded_audio_members(archive, {"keep.wav"}, max_workers=1))
                self.assertEqual(rows[0][0], "keep.wav")
                self.assertEqual(rows[0][1].shape, (32000,))
                self.assertIsNone(rows[0][2])
            finally:
                if old_sf is None:
                    sys.modules.pop("soundfile", None)
                else:
                    sys.modules["soundfile"] = old_sf
                if old is None:
                    sys.modules.pop("librosa", None)
                else:
                    sys.modules["librosa"] = old

    def test_missing_assets_and_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = {name: getattr(forge, name) for name in ("PIPER_CKPT", "FEATURES_DIR", "RIR_DIR", "AUDIOSET_DIR", "FMA_DIR", "WAKEWORDS")}
            forge.PIPER_CKPT = root / "piper.pt"
            forge.FEATURES_DIR = root / "features"
            forge.RIR_DIR = root / "rirs"
            forge.AUDIOSET_DIR = root / "audio"
            forge.FMA_DIR = root / "fma"
            forge.WAKEWORDS = root / "wakewords"
            try:
                missing = forge.missing_assets({"use_common_voice_negatives": True, "use_musan_background": True})
                self.assertGreaterEqual(len(missing), 5)
                (forge.PIPER_CKPT).write_bytes(b"x")
                forge.PIPER_CKPT.with_suffix(".pt.json").write_text("{}")
                (forge.FEATURES_DIR).mkdir()
                for f in (forge.NEGATIVE_FEATURES, forge.VALIDATION_FEATURES):
                    (forge.FEATURES_DIR / f).write_bytes(b"x")
                forge.RIR_DIR.mkdir(); (forge.RIR_DIR / "r.wav").write_bytes(b"x")
                forge.AUDIOSET_DIR.mkdir(); (forge.AUDIOSET_DIR / "a.wav").write_bytes(b"x")
                self.assertEqual(forge.missing_assets({}), [])
                args = SimpleNamespace(phrase="Hey Biscuit, hey bis", name=None, samples=4,
                    samples_val=2, steps=9, confusables="Stop, STOP, ", google_tts_languages="en-US",
                    google_tts_voices="Voice", google_tts_samples_per_voice=3, google_tts_qps=1, force=False)
                forge.cmd_new(args)
                cfg = (forge.WAKEWORDS / "hey_biscuit" / "config.yml").read_text()
                self.assertIn('"hey biscuit"', cfg)
                self.assertIn('custom_negative_phrases: ["stop", "stop"]', cfg)
                with self.assertRaises(SystemExit):
                    forge.cmd_new(SimpleNamespace(**{**vars(args), "phrase": "", "name": "bad"}))
            finally:
                for name, value in old.items():
                    setattr(forge, name, value)

    def test_assets_dispatch_and_score_format(self):
        calls = []
        args = SimpleNamespace(only="piper,features", feature_backend="onnx", audioset_clips=1, fma_clips=1)
        with patch.object(forge, "fetch_piper", lambda: calls.append("piper")), patch.object(forge, "fetch_features", lambda: calls.append("features")):
            forge.cmd_assets(args)
        self.assertEqual(calls, ["piper", "features"])
        with self.assertRaises(SystemExit):
            forge.cmd_assets(SimpleNamespace(**{**vars(args), "only": "bogus"}))
        self.assertIn("0 clips", forge._format_eval_row("none", []))
        self.assertIn("50.0%", forge._format_eval_row("x", [0.1, 0.7], 0.5, 0.2))
        class Model:
            def reset(self): pass
            def predict(self, audio): return {"wake": len(audio) / 10000}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.wav"
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(b"\0" * 3200)
            old = sys.modules.get("librosa")
            old_sf = sys.modules.get("soundfile")
            sys.modules["librosa"] = types.ModuleType("librosa")
            sf = types.ModuleType("soundfile")
            sf.read = lambda path, dtype=None: (np.zeros(1600, dtype="int16"), 16000)
            sys.modules["soundfile"] = sf
            try:
                self.assertGreaterEqual(forge.score_wav_file(Model(), path), 0)
            finally:
                if old is None: sys.modules.pop("librosa", None)
                else: sys.modules["librosa"] = old
                if old_sf is None: sys.modules.pop("soundfile", None)
                else: sys.modules["soundfile"] = old_sf

    def test_cmd_google_tts_and_test_eval_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old = forge.WAKEWORDS; forge.WAKEWORDS = root / "ww"
            cfgdir = forge.WAKEWORDS / "demo"; cfgdir.mkdir(parents=True)
            (cfgdir / "config.yml").write_text("model_name: demo\noutput_dir: %s\ntarget_phrase: [hi]\ncustom_negative_phrases: [no]\n" % root)
            calls = []
            fake = types.ModuleType("google_tts")
            fake.DEFAULT_QPS = 2
            fake.synthesize = lambda **kw: calls.append(kw)
            oldmod = sys.modules.get("google_tts"); sys.modules["google_tts"] = fake
            try:
                forge.cmd_google_tts(SimpleNamespace(name="demo", samples=2, languages="en-US", voices="", yes=True, qps=None))
                self.assertEqual([c["train_dir"].name for c in calls], ["positive_train", "negative_train"])
                with patch.object(forge, "evaluate_model") as evaluate:
                    forge.cmd_eval(SimpleNamespace(name="demo")); evaluate.assert_called_once_with("demo")
            finally:
                forge.WAKEWORDS = old
                if oldmod is None: sys.modules.pop("google_tts", None)
                else: sys.modules["google_tts"] = oldmod


if __name__ == "__main__":
    unittest.main()
