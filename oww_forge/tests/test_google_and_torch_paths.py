import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))
import google_tts
import torch_features


class GooglePathsTests(unittest.TestCase):
    def test_voice_listing_and_selection_validation(self):
        voice = types.SimpleNamespace(name="en-US-Chirp3-HD-A", language_codes=["en-US"])
        response = types.SimpleNamespace(voices=[voice, types.SimpleNamespace(name="Neural2-X", language_codes=["en-US"])])
        client = types.SimpleNamespace(list_voices=lambda: response)
        cloud = types.ModuleType("google.cloud")
        cloud.texttospeech = types.ModuleType("google.cloud.texttospeech")
        cloud.texttospeech.TextToSpeechClient = lambda: client
        google = types.ModuleType("google"); google.cloud = cloud
        old_google = {k: sys.modules.get(k) for k in ("google", "google.cloud", "google.cloud.texttospeech")}
        sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.texttospeech": cloud.texttospeech})
        with patch.object(google_tts, "_list_voices_with_retry", return_value=response):
            try:
                self.assertEqual(google_tts.list_chirp3_voices(["en-US", "en-US", ""]), {"en-US": ["en-US-Chirp3-HD-A"]})
            finally:
                for key, value in old_google.items():
                    if value is None: sys.modules.pop(key, None)
                    else: sys.modules[key] = value
        with patch.object(google_tts, "list_chirp3_voices", return_value={"en-US": ["A"]}):
            self.assertEqual(google_tts.selected_chirp3_pairs([" en-US ", "en-US"], ["A", "A"]), [("en-US", "A")])
            with self.assertRaisesRegex(ValueError, "at least one"):
                google_tts.selected_chirp3_pairs([], [])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                google_tts.selected_chirp3_pairs(["en-US"], ["B"])
        with patch.object(google_tts, "list_chirp3_voices", return_value={"en-US": []}):
            with self.assertRaisesRegex(ValueError, "no Chirp"):
                google_tts.selected_chirp3_pairs(["en-US"], [])

    def test_retry_pacer_and_client_error_paths(self):
        class Exhausted(Exception): pass
        fake_exceptions = types.ModuleType("google.api_core.exceptions")
        fake_exceptions.ResourceExhausted = Exhausted
        old = {k: sys.modules.get(k) for k in ("google", "google.api_core", "google.api_core.exceptions")}
        sys.modules["google.api_core.exceptions"] = fake_exceptions
        calls = [0]
        def list_voices():
            calls[0] += 1
            if calls[0] == 1: raise Exhausted()
            return "ok"
        try:
            with patch.object(google_tts.time, "sleep"):
                self.assertEqual(google_tts._list_voices_with_retry(types.SimpleNamespace(list_voices=list_voices)), "ok")
            self.assertEqual(calls[0], 2)
        finally:
            for key, value in old.items():
                if value is None: sys.modules.pop(key, None)
                else: sys.modules[key] = value
        pacer = google_tts._RequestPacer(1)
        with patch.object(google_tts.time, "monotonic", side_effect=[0, 0, 0.1, 1]):
            with patch.object(google_tts.time, "sleep") as sleep:
                pacer.wait(); pacer.wait(); self.assertTrue(sleep.called)

    def test_synthesize_validation_and_permanent_voice_failure(self):
        cloud = types.ModuleType("google.cloud")
        tts = types.ModuleType("google.cloud.texttospeech")
        tts.TextToSpeechClient = lambda: (_ for _ in ()).throw(RuntimeError("bad credentials"))
        cloud.texttospeech = tts
        google = types.ModuleType("google"); google.cloud = cloud
        old = {k: sys.modules.get(k) for k in ("google", "google.cloud", "google.cloud.texttospeech")}
        sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.texttospeech": tts})
        api_core = types.ModuleType("google.api_core")
        exceptions = types.ModuleType("google.api_core.exceptions")
        exceptions.ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        api_core.exceptions = exceptions
        sys.modules.update({"google.api_core": api_core, "google.api_core.exceptions": exceptions})
        try:
            with self.assertRaises(SystemExit) as err:
                google_tts.synthesize(["hi"], 1, Path("train"), Path("test"), ["en-US"], assume_yes=True)
            self.assertIn("credentials", str(err.exception))
        finally:
            for key, value in old.items():
                if value is None: sys.modules.pop(key, None)
                else: sys.modules[key] = value

    def test_synthesize_uses_stable_split_and_existing_file(self):
        class Voice:
            name = "Chirp3-HD-A"; language_codes = ["en-US"]; ssml_gender = "NEUTRAL"
        voice = Voice()
        class Client:
            def __init__(self): self.calls = 0
            def list_voices(self): return types.SimpleNamespace(voices=[voice])
            def synthesize_speech(self, **kwargs):
                self.calls += 1
                return types.SimpleNamespace(audio_content=b"wav")
        client = Client()
        tts = types.ModuleType("google.cloud.texttospeech")
        tts.TextToSpeechClient = lambda: client
        tts.SynthesisInput = lambda **x: x; tts.VoiceSelectionParams = lambda **x: x
        tts.AudioConfig = lambda **x: x; tts.AudioEncoding = types.SimpleNamespace(LINEAR16="linear16")
        cloud = types.ModuleType("google.cloud"); cloud.texttospeech = tts
        google = types.ModuleType("google"); google.cloud = cloud
        old = {k: sys.modules.get(k) for k in ("google", "google.cloud", "google.cloud.texttospeech")}
        sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.texttospeech": tts})
        api_core = types.ModuleType("google.api_core")
        exceptions = types.ModuleType("google.api_core.exceptions")
        exceptions.ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        api_core.exceptions = exceptions
        sys.modules.update({"google.api_core": api_core, "google.api_core.exceptions": exceptions})
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(google_tts, "_write_wav16k", lambda data, dest: Path(dest).write_bytes(data)):
                train, test = Path(tmp) / "train", Path(tmp) / "test"
                (test).mkdir(); (test / "google_en-US_Chirp3-HD-A_000000.wav").write_bytes(b"existing")
                google_tts.synthesize(["one", "two"], 2, train, test, ["en-US"], assume_yes=True, qps=1000)
                self.assertEqual(client.calls, 1)
                self.assertTrue((train / "google_en-US_Chirp3-HD-A_000001.wav").exists())
        finally:
            for key, value in old.items():
                if value is None: sys.modules.pop(key, None)
                else: sys.modules[key] = value


class TorchFallbackTests(unittest.TestCase):
    def test_backend_selection_and_validation_without_gpu(self):
        class Cuda:
            @staticmethod
            def is_available(): return False
        torch = types.ModuleType("torch"); torch.cuda = Cuda
        old = sys.modules.get("torch"); sys.modules["torch"] = torch
        try:
            with patch.object(torch_features, "OnnxAudioFeatures") as onnx:
                self.assertIs(torch_features.make_feature_extractor("auto", 3), onnx.return_value)
                onnx.assert_called_once_with(ncpu=3)
            with self.assertRaises(ValueError): torch_features.make_feature_extractor("bad")
            with patch.object(torch_features, "TorchAudioFeatures") as torch_cls:
                self.assertIs(torch_features.make_feature_extractor("torch"), torch_cls.return_value)
        finally:
            if old is None: sys.modules.pop("torch", None)
            else: sys.modules["torch"] = old

    def test_onnx_adapter_contract_and_torch_constructor_guards(self):
        fake_features = types.SimpleNamespace(embed_clips=lambda clips, batch_size, ncpu: np.ones((len(clips), 2, 96), dtype="float32"))
        utils = types.ModuleType("openwakeword.utils")
        utils.AudioFeatures = lambda **kwargs: fake_features
        old_utils = sys.modules.get("openwakeword.utils"); sys.modules["openwakeword.utils"] = utils
        try:
            adapter = torch_features.OnnxAudioFeatures(ncpu=4)
            self.assertEqual(adapter.describe()["workers"], 4)
            self.assertEqual(adapter.embed_clips(np.zeros((1, 10), dtype="int16")).shape, (1, 2, 96))
        finally:
            if old_utils is None: sys.modules.pop("openwakeword.utils", None)
            else: sys.modules["openwakeword.utils"] = old_utils
        torch = types.ModuleType("torch"); torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        old_torch = sys.modules.get("torch"); sys.modules["torch"] = torch
        try:
            with self.assertRaises(ValueError): torch_features.TorchAudioFeatures(device="cpu")
            with self.assertRaises(RuntimeError): torch_features.TorchAudioFeatures()
        finally:
            if old_torch is None: sys.modules.pop("torch", None)
            else: sys.modules["torch"] = old_torch


if __name__ == "__main__":
    unittest.main()
