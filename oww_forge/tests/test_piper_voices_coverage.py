"""Coverage for piper_voices.py's catalogue, download and synthesis logic.

None of onnxruntime, piper_phonemize, soundfile or librosa are installed in
the test environment (see tests/test_google_and_torch_paths.py and
tests/test_coverage_gaps.py for the established pattern), so every function
that imports one of those internally is exercised against a fake module
injected into sys.modules rather than the real dependency. Network access
(urllib.request) is likewise always mocked — no test here may reach the
Hugging Face Piper voices repo.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))

import piper_voices


def _patched_modules(**modules):
    """Install fake modules for the duration of a `with` block, then restore."""
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


class CatalogueTests(unittest.TestCase):
    def test_index_reads_cache_without_fetching_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            cache_dir = assets / piper_voices.VOICES_DIR_NAME
            cache_dir.mkdir(parents=True)
            (cache_dir / piper_voices.INDEX_CACHE).write_text(json.dumps({"a": {}}))
            # A cache hit must never touch the network — urlopen would fail
            # loudly if reached.
            with patch.object(piper_voices.urllib.request, "urlopen",
                               side_effect=AssertionError("hit the network")):
                self.assertEqual(piper_voices.index(assets), {"a": {}})

    def test_index_fetches_when_missing_and_on_forced_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)

            class Resp:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return json.dumps({"fresh": {}}).encode()

            with patch.object(piper_voices.urllib.request, "urlopen", return_value=Resp()) as opener:
                # Missing cache: fetch once, directory created on demand.
                self.assertEqual(piper_voices.index(assets), {"fresh": {}})
                opener.assert_called_once()

            # Present but refresh=True must re-fetch rather than trust the
            # cache on disk.
            with patch.object(piper_voices.urllib.request, "urlopen", return_value=Resp()) as opener:
                result = piper_voices.index(assets, refresh=True)
                self.assertEqual(result, {"fresh": {}})
                opener.assert_called_once()

    def test_catalogue_orders_by_speakers_then_quality_then_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            fake_index = {
                "en_GB-vctk-medium": {
                    "language": {"code": "en_GB", "name_english": "English", "country_english": "UK"},
                    "quality": "medium", "num_speakers": 109,
                },
                "en_US-libritts_r-medium": {
                    "language": {"code": "en_US"}, "quality": "medium", "num_speakers": 904,
                },
                # No "language" dict at all: falls back to splitting the name.
                "fr_FR-tom-low": {"quality": "low"},
                # Missing num_speakers entirely defaults to 1 (single speaker).
                "de_DE-thorsten-high": {
                    "language": {"code": "de_DE", "name_english": "German"}, "quality": "high",
                },
            }
            with patch.object(piper_voices, "index", return_value=fake_index):
                cat = piper_voices.catalogue(assets)
            # Highest speaker count first regardless of quality.
            self.assertEqual(cat[0]["name"], "en_US-libritts_r-medium")
            self.assertEqual(cat[1]["name"], "en_GB-vctk-medium")
            self.assertEqual(cat[1]["language"], "en_GB")
            # No language dict: language falls back to the name's prefix.
            fr = next(v for v in cat if v["name"] == "fr_FR-tom-low")
            self.assertEqual(fr["language"], "fr_FR")
            self.assertEqual(fr["language_name"], "")
            # Missing num_speakers defaults to 1, single-speaker voices sort
            # after the multi-speaker ones and are ordered by quality rank
            # (high=0 before low=2).
            de = next(v for v in cat if v["name"] == "de_DE-thorsten-high")
            self.assertEqual(de["speakers"], 1)
            self.assertLess(cat.index(de), cat.index(fr))

    def test_languages_aggregates_best_speaker_count_and_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            fake_catalogue = [
                {"language": "en_GB", "language_name": "English", "country": "UK", "speakers": 109},
                {"language": "en_GB", "language_name": "English", "country": "UK", "speakers": 1},
                # No name/country at all: label falls back to the code.
                {"language": "xx", "language_name": "", "country": "", "speakers": 1},
            ]
            with patch.object(piper_voices, "catalogue", return_value=fake_catalogue):
                langs = piper_voices.languages(assets)
            en = next(l for l in langs if l["language"] == "en_GB")
            self.assertEqual(en["voices"], 2)
            self.assertEqual(en["max_speakers"], 109)
            self.assertEqual(en["label"], "English UK")
            xx = next(l for l in langs if l["language"] == "xx")
            self.assertEqual(xx["label"], "xx")

    def test_default_voices_picks_multi_speaker_or_falls_back_to_top_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            with patch.object(piper_voices, "catalogue", return_value=[]):
                with self.assertRaises(SystemExit):
                    piper_voices.default_voices(assets, "zz")

            multi = [{"language": "en_GB", "speakers": 109, "name": "en_GB-vctk-medium"},
                     {"language": "en_GB", "speakers": 1, "name": "other"}]
            with patch.object(piper_voices, "catalogue", return_value=multi):
                self.assertEqual(piper_voices.default_voices(assets, "en_GB"), ["en_GB-vctk-medium"])

            singles = [{"language": "fr_FR", "speakers": 1, "name": f"v{i}"} for i in range(6)]
            with patch.object(piper_voices, "catalogue", return_value=singles):
                # No voice in the language has more than one speaker: take
                # the best FOUR rather than every single-speaker voice.
                self.assertEqual(piper_voices.default_voices(assets, "fr_FR"),
                                 ["v0", "v1", "v2", "v3"])

    def test_voice_path_prefers_the_indexed_onnx_file_over_the_guessed_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            indexed = {
                "en_GB-vctk-medium": {"files": {
                    "en/en_GB/vctk/medium/en_GB-vctk-medium.onnx": {},
                    "en/en_GB/vctk/medium/en_GB-vctk-medium.onnx.json": {},
                }},
                # No usable .onnx entry in "files": falls through to the
                # guessed <lang-family>/<code>/<voice>/<quality> layout.
                "de_DE-thorsten-high": {"files": {"de_DE-thorsten-high.onnx.json": {}}},
            }
            with patch.object(piper_voices, "index", return_value=indexed):
                self.assertEqual(piper_voices._voice_path(assets, "en_GB-vctk-medium"),
                                 "en/en_GB/vctk/medium")
                self.assertEqual(piper_voices._voice_path(assets, "de_DE-thorsten-high"),
                                 "de/de_DE/thorsten/high")
                with self.assertRaises(SystemExit):
                    piper_voices._voice_path(assets, "unknown-voice")

    def test_ensure_voice_downloads_only_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            with patch.object(piper_voices, "_voice_path", return_value="en/en_GB/v/medium"):
                onnx_path = piper_voices.voice_dir(assets) / "v.onnx"
                cfg_path = piper_voices.voice_dir(assets) / "v.onnx.json"

                def fake_retrieve(url, part):
                    # Simulate the .part-then-rename discipline: the caller
                    # renames `part` to the final name, so we just need to
                    # produce that file with valid JSON for the config.
                    Path(part).write_text("{}")

                with patch.object(piper_voices.urllib.request, "urlretrieve",
                                   side_effect=fake_retrieve) as retrieve:
                    onnx, cfg = piper_voices.ensure_voice(assets, "v")
                    self.assertEqual(onnx, onnx_path)
                    self.assertEqual(cfg, {})
                    self.assertEqual(retrieve.call_count, 2)

                # Both files now exist: a second call must not download again.
                with patch.object(piper_voices.urllib.request, "urlretrieve",
                                   side_effect=AssertionError("downloaded again")):
                    piper_voices.ensure_voice(assets, "v")

    def test_session_builds_a_single_threaded_cpu_inference_session(self):
        calls = {}

        class FakeSessionOptions:
            def __init__(self):
                self.log_severity_level = None
                self.intra_op_num_threads = None
                self.inter_op_num_threads = None

        def fake_inference_session(path, opts, providers):
            calls["path"] = path
            calls["opts"] = opts
            calls["providers"] = providers
            return "session"

        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.SessionOptions = FakeSessionOptions
        fake_ort.InferenceSession = fake_inference_session
        with _patched_modules(onnxruntime=fake_ort):
            result = piper_voices._session(Path("model.onnx"))
        self.assertEqual(result, "session")
        self.assertEqual(calls["path"], "model.onnx")
        self.assertEqual(calls["providers"], ["CPUExecutionProvider"])
        self.assertEqual(calls["opts"].intra_op_num_threads, 1)
        self.assertEqual(calls["opts"].inter_op_num_threads, 1)
        self.assertEqual(calls["opts"].log_severity_level, 3)

    def test_phoneme_ids_flattens_every_sentence(self):
        fake_phonemize = types.ModuleType("piper_phonemize")
        fake_phonemize.phonemize_espeak = lambda text, voice: ["s1", "s2"]
        fake_phonemize.phoneme_ids_espeak = lambda sentence, id_map: {"s1": [1, 2], "s2": [3]}[sentence]
        with _patched_modules(piper_phonemize=fake_phonemize):
            ids = piper_voices._phoneme_ids("hello there", {"espeak": {"voice": "en"}, "phoneme_id_map": {}})
        self.assertEqual(ids, [1, 2, 3])

    def test_to_16k_passes_through_at_target_rate_and_resamples_otherwise(self):
        audio = np.array([0.1, 0.2, 0.3], dtype="float32")
        # Already at the target rate: no resample import needed at all.
        self.assertIs(piper_voices._to_16k(audio, piper_voices.TARGET_RATE), audio)

        resampled = np.array([0.9], dtype="float32")
        fake_librosa = types.ModuleType("librosa")
        recorded = {}

        def fake_resample(a, orig_sr, target_sr):
            recorded["orig_sr"] = orig_sr
            recorded["target_sr"] = target_sr
            return resampled

        fake_librosa.resample = fake_resample
        with _patched_modules(librosa=fake_librosa):
            out = piper_voices._to_16k(audio, 22050)
        self.assertIs(out, resampled)
        self.assertEqual(recorded, {"orig_sr": 22050, "target_sr": piper_voices.TARGET_RATE})


class PreviewTests(unittest.TestCase):
    def _fake_deps(self):
        fake_sf = types.ModuleType("soundfile")
        writes = []
        fake_sf.write = lambda buf, audio, rate, format, subtype: writes.append(
            (audio.copy(), rate, format, subtype))
        return fake_sf, writes

    def test_preview_caches_the_session_per_voice_and_writes_the_voice_native_rate(self):
        piper_voices._preview_cache.clear()
        cfg = {"num_speakers": 1, "audio": {"sample_rate": 22050},
               "inference": {"noise_scale": 1.0, "length_scale": 1.0, "noise_w": 1.0}}

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def run(self, _outputs, feeds):
                self.calls += 1
                self.last_feeds = feeds
                return [np.array([[0.5, -1.0, 0.25]], dtype="float32")]

        session = FakeSession()
        fake_sf, writes = self._fake_deps()

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=fake_sf), \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)) as ensure, \
                patch.object(piper_voices, "_session", return_value=session), \
                patch.object(piper_voices, "_phoneme_ids", return_value=[1, 2, 3]):
            wav_bytes = piper_voices.preview("hello", Path(tmp), voice="en_GB-vctk-medium")
            # Calling again for the SAME voice must reuse the cached session
            # rather than downloading/loading it a second time.
            piper_voices.preview("hello again", Path(tmp), voice="en_GB-vctk-medium")
            ensure.assert_called_once()

        self.assertIsInstance(wav_bytes, bytes)
        self.assertEqual(session.calls, 2)
        # Single-speaker config: no "sid" feed should be sent.
        self.assertNotIn("sid", session.last_feeds)
        # Written at the voice's own 22050Hz rate, not the 16k training rate.
        self.assertEqual(writes[0][1], 22050)
        # Peak-normalised: the loudest sample should land at exactly 0.9.
        self.assertAlmostEqual(float(np.abs(writes[0][0]).max()), 0.9, places=5)

    def test_preview_selects_a_default_voice_and_adds_the_speaker_id_feed(self):
        piper_voices._preview_cache.clear()
        cfg = {"num_speakers": 4, "audio": {"sample_rate": 16000},
               "inference": {"noise_scale": 1.0, "length_scale": 1.0, "noise_w": 1.0}}

        class FakeSession:
            def run(self, _outputs, feeds):
                self.last_feeds = feeds
                return [np.zeros((1, 1, 4), dtype="float32")]  # silent clip: peak == 0

        session = FakeSession()
        fake_sf, writes = self._fake_deps()

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=fake_sf), \
                patch.object(piper_voices, "default_voices", return_value=["picked"]) as defaults, \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)), \
                patch.object(piper_voices, "_session", return_value=session), \
                patch.object(piper_voices, "_phoneme_ids", return_value=[1]):
            piper_voices.preview("hi", Path(tmp), speaker=5, language="en_GB")
        defaults.assert_called_once_with(Path(tmp), "en_GB")
        # speaker=5 wraps modulo num_speakers=4.
        self.assertEqual(int(session.last_feeds["sid"][0]), 1)
        # A silent clip (peak == 0) must not be divided by zero.
        self.assertEqual(writes[0][0].max(), 0.0)


class SynthesizeTests(unittest.TestCase):
    def _voice_cfg(self, num_speakers=2, sample_rate=piper_voices.TARGET_RATE):
        return {"num_speakers": num_speakers, "audio": {"sample_rate": sample_rate},
                "inference": {"noise_scale": 1.0, "length_scale": 1.0, "noise_w": 1.0}}

    def _fake_soundfile(self):
        fake_sf = types.ModuleType("soundfile")
        fake_sf.write = lambda dest, audio, rate, subtype: Path(dest).write_bytes(b"wav")
        return fake_sf

    def test_generates_requested_clips_across_speakers_and_prosody_combinations(self):
        cfg = self._voice_cfg(num_speakers=2)

        class FakeSession:
            def run(self, _outputs, feeds):
                return [np.array([[0.4, -0.8, 0.2, 0.1]], dtype="float32")]

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=self._fake_soundfile()), \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)), \
                patch.object(piper_voices, "_session", return_value=FakeSession()), \
                patch.object(piper_voices, "_phoneme_ids", return_value=[1, 2]):
            base = Path(tmp)
            train, test = base / "positive_train", base / "positive_test"
            piper_voices.synthesize(["hi", "there"], n_samples=6, train_dir=train,
                                    test_dir=test, assets=base, voices=["v"])
            written = list(train.glob("piper_gb_*.wav")) + list(test.glob("piper_gb_*.wav"))
            self.assertEqual(len(written), 6)

    def test_skips_a_destination_that_already_exists_without_resynthesizing_it(self):
        cfg = self._voice_cfg(num_speakers=1)
        calls = {"n": 0}

        class FakeSession:
            def run(self, _outputs, feeds):
                calls["n"] += 1
                return [np.array([[0.3, -0.3]], dtype="float32")]

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=self._fake_soundfile()), \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)), \
                patch.object(piper_voices, "_session", return_value=FakeSession()), \
                patch.object(piper_voices, "_phoneme_ids", return_value=[1]), \
                patch.object(piper_voices.random, "random", return_value=0.99):  # force everything to train
            base = Path(tmp)
            train, test = base / "positive_train", base / "positive_test"
            train.mkdir(parents=True)
            # Pre-create the first destination the (deterministic, unshuffled)
            # combo list would otherwise produce. n_samples=2 keeps this
            # below the "existing already meets the target" early return, so
            # the loop's PER-FILE dest.exists() skip is the one under test.
            existing = train / "piper_gb_000000_v_s0.wav"
            existing.write_bytes(b"already here")
            with patch.object(piper_voices.random, "shuffle", lambda seq: None):
                piper_voices.synthesize(["hi"], n_samples=2, train_dir=train, test_dir=test,
                                        assets=base, voices=["v"])
            # The pre-existing clip must be left untouched (never resynthesized)
            # and only the genuinely new second clip should have been synthesized.
            self.assertEqual(existing.read_bytes(), b"already here")
            self.assertEqual(calls["n"], 1)
            self.assertTrue((train / "piper_gb_000001_v_s0.wav").exists())

    def test_a_synthesis_failure_is_logged_and_does_not_abort_the_batch(self):
        cfg = self._voice_cfg(num_speakers=1)

        class FakeSession:
            def run(self, _outputs, feeds):
                return [np.array([[0.5, -0.5]], dtype="float32")]

        call_count = {"n": 0}

        def flaky_phoneme_ids(phrase, cfg):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("espeak choked")
            return [1, 2]

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=self._fake_soundfile()), \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)), \
                patch.object(piper_voices, "_session", return_value=FakeSession()), \
                patch.object(piper_voices, "_phoneme_ids", side_effect=flaky_phoneme_ids), \
                patch.object(piper_voices, "log") as log:
            base = Path(tmp)
            train, test = base / "positive_train", base / "positive_test"
            piper_voices.synthesize(["hi", "there", "world"], n_samples=3, train_dir=train,
                                    test_dir=test, assets=base, voices=["v"])
            written = list(train.glob("*.wav")) + list(test.glob("*.wav"))
            # One of the three jobs failed; the other two must still be written.
            self.assertEqual(len(written), 2)
        failure_logs = [c for c in log.call_args_list if "clips failed" in c.args[0]]
        self.assertEqual(len(failure_logs), 1)

    def test_logs_progress_every_two_hundred_completed_clips(self):
        cfg = self._voice_cfg(num_speakers=1)

        class FakeSession:
            def run(self, _outputs, feeds):
                return [np.array([[0.2, -0.2]], dtype="float32")]

        with tempfile.TemporaryDirectory() as tmp, _patched_modules(soundfile=self._fake_soundfile()), \
                patch.object(piper_voices, "ensure_voice", return_value=(Path("v.onnx"), cfg)), \
                patch.object(piper_voices, "_session", return_value=FakeSession()), \
                patch.object(piper_voices, "_phoneme_ids", return_value=[1]), \
                patch.object(piper_voices, "log") as log:
            base = Path(tmp)
            train, test = base / "positive_train", base / "positive_test"
            piper_voices.synthesize(["hi"], n_samples=200, train_dir=train, test_dir=test,
                                    assets=base, voices=["v"])
        progress_logs = [c for c in log.call_args_list if c.args[0].startswith("…")]
        self.assertEqual(len(progress_logs), 1)
        self.assertIn("200/200", progress_logs[0].args[0])


if __name__ == "__main__":
    unittest.main()
