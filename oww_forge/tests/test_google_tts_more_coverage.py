"""Further google_tts.py coverage: retry exhaustion, the real 16k-mono
normalization path (soundfile/librosa mocked — neither is installed in this
environment, see tests/test_google_tts.py's own skip), the "package not
installed" branches, and synthesize()'s validation/execution paths.

The `google` package itself is not installed here (confirmed: `import
google` raises ModuleNotFoundError), so tests that want the "not installed"
behaviour simply call the real code with nothing injected into sys.modules,
and tests that want a working client inject fakes exactly as
tests/test_google_and_torch_paths.py already does.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))
import google_tts


def _patched_modules(**modules):
    class _Ctx:
        def __enter__(self):
            self.old = {name: sys.modules.get(name) for name in modules}
            sys.modules.update(modules)
            return self

        def __exit__(self, *exc):
            for name, value in self.old.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
            return False
    return _Ctx()


class RetryExhaustionTests(unittest.TestCase):
    def test_list_voices_with_retry_reraises_once_every_attempt_is_exhausted(self):
        class Exhausted(Exception):
            pass
        fake_exceptions = types.ModuleType("google.api_core.exceptions")
        fake_exceptions.ResourceExhausted = Exhausted
        with _patched_modules(**{"google.api_core.exceptions": fake_exceptions}):
            client = SimpleNamespace(list_voices=lambda: (_ for _ in ()).throw(Exhausted()))
            with patch.object(google_tts.time, "sleep"):
                with self.assertRaises(Exhausted):
                    google_tts._list_voices_with_retry(client)


class WriteWav16kTests(unittest.TestCase):
    def test_downmixes_stereo_and_resamples_to_16k(self):
        recorded = {}
        fake_sf = types.ModuleType("soundfile")
        fake_sf.read = lambda buf, dtype: (np.ones((10, 2), dtype="float32") * 2.0, 8000)

        def fake_write(dest, audio, rate, subtype):
            recorded["audio"] = audio
            recorded["rate"] = rate
            recorded["subtype"] = subtype
            Path(dest).write_bytes(b"wav")

        fake_sf.write = fake_write
        fake_librosa = types.ModuleType("librosa")

        def fake_resample(audio, orig_sr, target_sr):
            recorded["orig_sr"] = orig_sr
            recorded["target_sr"] = target_sr
            return audio  # already-mono input; identity is enough for the assertion

        fake_librosa.resample = fake_resample
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=fake_sf, librosa=fake_librosa):
            dest = Path(tmp) / "nested" / "clip.wav"
            google_tts._write_wav16k(b"raw wav bytes", dest)
            self.assertTrue(dest.exists())  # parent directories created on demand
        self.assertEqual(recorded["rate"], 16000)
        self.assertEqual(recorded["subtype"], "PCM_16")
        self.assertEqual(recorded["orig_sr"], 8000)
        self.assertEqual(recorded["target_sr"], 16000)
        # Stereo input (2.0, 2.0) averages to mono 2.0, then clips to [-1, 1].
        self.assertTrue(np.all(recorded["audio"] <= 1.0))

    def test_a_clip_already_at_16k_skips_the_resample_call(self):
        fake_sf = types.ModuleType("soundfile")
        fake_sf.read = lambda buf, dtype: (np.zeros(10, dtype="float32"), 16000)
        fake_sf.write = lambda dest, audio, rate, subtype: Path(dest).write_bytes(b"wav")
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(
                soundfile=fake_sf, librosa=types.ModuleType("librosa")):
            # librosa.resample is deliberately left undefined: calling it
            # would raise AttributeError and fail the test.
            google_tts._write_wav16k(b"raw", Path(tmp) / "clip.wav")


class PackageUnavailableTests(unittest.TestCase):
    """`google-cloud-texttospeech` genuinely is not installed here."""

    def test_list_chirp3_voices_reports_the_missing_package(self):
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            google_tts.list_chirp3_voices(["en-US"])

    def test_synthesize_exits_cleanly_when_the_package_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "not installed"):
                google_tts.synthesize(["hi"], 1, Path(tmp) / "train", Path(tmp) / "test", ["en-US"])


class SynthesizeValidationTests(unittest.TestCase):
    """synthesize()'s argument checks, which all run before any client call."""

    def _fake_google_modules(self, client):
        tts = types.ModuleType("google.cloud.texttospeech")
        tts.TextToSpeechClient = lambda: client
        tts.SynthesisInput = lambda **kw: kw
        tts.VoiceSelectionParams = lambda **kw: kw
        tts.AudioConfig = lambda **kw: kw
        tts.AudioEncoding = SimpleNamespace(LINEAR16="linear16")
        cloud = types.ModuleType("google.cloud")
        cloud.texttospeech = tts
        google = types.ModuleType("google")
        google.cloud = cloud
        exceptions = types.ModuleType("google.api_core.exceptions")
        exceptions.ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        api_core = types.ModuleType("google.api_core")
        api_core.exceptions = exceptions
        return {"google": google, "google.cloud": cloud, "google.cloud.texttospeech": tts,
                "google.api_core": api_core, "google.api_core.exceptions": exceptions}

    def test_rejects_empty_locales_non_positive_samples_and_non_positive_qps(self):
        client = SimpleNamespace(list_voices=lambda: SimpleNamespace(voices=[]))
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(**self._fake_google_modules(client)):
            train, test = Path(tmp) / "train", Path(tmp) / "test"
            with self.assertRaisesRegex(SystemExit, "at least one locale"):
                google_tts.synthesize(["hi"], 1, train, test, [])
            with self.assertRaisesRegex(SystemExit, "samples per voice"):
                google_tts.synthesize(["hi"], 0, train, test, ["en-US"])
            with self.assertRaisesRegex(SystemExit, "queries per second"):
                google_tts.synthesize(["hi"], 1, train, test, ["en-US"], qps=0)

    def test_exits_when_voice_listing_fails_or_requested_voices_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            train, test = Path(tmp) / "train", Path(tmp) / "test"

            failing_client = SimpleNamespace(
                list_voices=lambda: (_ for _ in ()).throw(RuntimeError("quota exhausted")))
            with _patched_modules(**self._fake_google_modules(failing_client)), \
                    patch.object(google_tts.time, "sleep"):
                with self.assertRaisesRegex(SystemExit, "could not list Chirp 3 voices"):
                    google_tts.synthesize(["hi"], 1, train, test, ["en-US"])

            voice = SimpleNamespace(name="Chirp3-HD-A", language_codes=["en-US"], ssml_gender="NEUTRAL")
            ok_client = SimpleNamespace(list_voices=lambda: SimpleNamespace(voices=[voice]))
            with _patched_modules(**self._fake_google_modules(ok_client)):
                with self.assertRaisesRegex(SystemExit, "unavailable or not Chirp 3"):
                    google_tts.synthesize(["hi"], 1, train, test, ["en-US"], voice_names=["NoSuchVoice"])
                with self.assertRaisesRegex(SystemExit, "no Chirp 3 voices matched"):
                    google_tts.synthesize(["hi"], 1, train, test, ["ja-JP"])

    def test_prompts_for_confirmation_and_aborts_on_a_negative_reply(self):
        voice = SimpleNamespace(name="Chirp3-HD-A", language_codes=["en-US"], ssml_gender="NEUTRAL")
        client = SimpleNamespace(list_voices=lambda: SimpleNamespace(voices=[voice]),
                                 synthesize_speech=lambda **kw: SimpleNamespace(audio_content=b"wav"))
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(**self._fake_google_modules(client)):
            train, test = Path(tmp) / "train", Path(tmp) / "test"
            with patch("builtins.input", return_value="n"):
                with self.assertRaisesRegex(SystemExit, "aborted"):
                    google_tts.synthesize(["hi"], 1, train, test, ["en-US"])
            # A "yes" reply must let the run continue into the actual work —
            # covered end-to-end (with assume_yes) by
            # tests/test_google_and_torch_paths.py, so here we only need to
            # prove the prompt itself does not block a "yes".
            with patch("builtins.input", return_value="yes"), \
                    patch.object(google_tts, "_write_wav16k", lambda data, dest: Path(dest).write_bytes(data)):
                google_tts.synthesize(["hi"], 1, train, test, ["en-US"])
            self.assertEqual(len(list(train.glob("*.wav")) + list(test.glob("*.wav"))), 1)


class SynthOneBranchTests(unittest.TestCase):
    """Exercise synth_one's per-job branches through the public synthesize()
    entry point, using voices engineered to hit one branch each."""

    def _fake_google_modules(self, client):
        tts = types.ModuleType("google.cloud.texttospeech")
        tts.TextToSpeechClient = lambda: client
        tts.SynthesisInput = lambda **kw: kw
        tts.VoiceSelectionParams = lambda **kw: kw
        tts.AudioConfig = lambda **kw: kw
        tts.AudioEncoding = SimpleNamespace(LINEAR16="linear16")
        cloud = types.ModuleType("google.cloud")
        cloud.texttospeech = tts
        google = types.ModuleType("google")
        google.cloud = cloud
        exceptions = types.ModuleType("google.api_core.exceptions")
        exceptions.ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        api_core = types.ModuleType("google.api_core")
        api_core.exceptions = exceptions
        return {"google": google, "google.cloud": cloud, "google.cloud.texttospeech": tts,
                "google.api_core": api_core, "google.api_core.exceptions": exceptions}

    def test_existing_destination_transient_retry_permanent_failure_and_summary(self):
        # Three voices, one Chirp3 each, all serving "en-US":
        #   A — its one sample already exists on disk (dest.exists() -> 1)
        #   B — fails once with a transient error, then succeeds (retry log)
        #   C — always fails with a permanent error (retired after one try)
        voice_a = SimpleNamespace(name="Chirp3-HD-A", language_codes=["en-US"], ssml_gender="NEUTRAL")
        voice_b = SimpleNamespace(name="Chirp3-HD-B", language_codes=["en-US"], ssml_gender="NEUTRAL")
        voice_c = SimpleNamespace(name="Chirp3-HD-C", language_codes=["en-US"], ssml_gender="NEUTRAL")
        attempts = {"b": 0}

        class Permanent(Exception):
            pass
        # _is_permanent_error keys off the *class name*.
        Permanent.__name__ = "InvalidArgument"

        def synthesize_speech(**kwargs):
            name = kwargs["voice"]["name"]
            if name == voice_b.name:
                attempts["b"] += 1
                if attempts["b"] == 1:
                    raise RuntimeError("transient hiccup")
                return SimpleNamespace(audio_content=b"wav-b")
            if name == voice_c.name:
                raise Permanent("blocked")
            raise AssertionError(f"voice A's only sample already existed; should not be re-synthesized")

        client = SimpleNamespace(
            list_voices=lambda: SimpleNamespace(voices=[voice_a, voice_b, voice_c]),
            synthesize_speech=synthesize_speech,
        )
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(**self._fake_google_modules(client)), \
                patch.object(google_tts, "_write_wav16k", lambda data, dest: Path(dest).write_bytes(data)), \
                patch.object(google_tts.time, "sleep"), \
                patch.object(google_tts, "log") as log:
            train, test = Path(tmp) / "train", Path(tmp) / "test"
            test.mkdir(parents=True)
            # Voice A is pair_index 0, sample_index 0 → global index 0, and
            # index % round(1/TEST_FRACTION) == 0 routes index 0 to the TEST
            # split (the "stable split" rule), not train.
            existing = test / "google_en-US_Chirp3-HD-A_000000.wav"
            existing.write_bytes(b"already here")
            google_tts.synthesize(["hi"], samples_per_voice=1, train_dir=train, test_dir=test,
                                  languages=["en-US"], assume_yes=True, qps=1000)

            # A's clip is untouched, B eventually succeeded, C never produced a file.
            self.assertEqual(existing.read_bytes(), b"already here")
            self.assertTrue((train / "google_en-US_Chirp3-HD-B_000000.wav").exists())
            self.assertFalse((train / "google_en-US_Chirp3-HD-C_000000.wav").exists())
        # A retry warning was logged for B, and a permanently-rejected-voices
        # summary was logged mentioning C.
        messages = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any("retrying" in m for m in messages))
        self.assertTrue(any("permanently rejected" in m for m in messages))
        self.assertTrue(any("Chirp3-HD-C" in m for m in messages))

    def test_a_voice_that_exhausts_every_retry_without_a_permanent_error_gives_up(self):
        voice = SimpleNamespace(name="Chirp3-HD-Z", language_codes=["en-US"], ssml_gender="NEUTRAL")

        def always_transient(**kwargs):
            raise RuntimeError("still rate limited")

        client = SimpleNamespace(
            list_voices=lambda: SimpleNamespace(voices=[voice]),
            synthesize_speech=always_transient,
        )
        with tempfile.TemporaryDirectory() as tmp, _patched_modules(**self._fake_google_modules(client)), \
                patch.object(google_tts.time, "sleep"), patch.object(google_tts, "log") as log:
            train, test = Path(tmp) / "train", Path(tmp) / "test"
            google_tts.synthesize(["hi"], samples_per_voice=1, train_dir=train, test_dir=test,
                                  languages=["en-US"], assume_yes=True, qps=1000)
        self.assertEqual(list(train.glob("*.wav")) + list(test.glob("*.wav")), [])
        messages = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any("gave up after retries" in m for m in messages))
        # Every request failed: the "more than half failed" warning must fire.
        self.assertTrue(any("WARNING" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
