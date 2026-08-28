"""
Tests for forge.import_labeled_dataset — importing a labelled dataset ZIP
(positive/ negative/) from the EchoMuse dashboard into a wake word's train/test
dirs. The properties that matter: the oww_forge 90/10 split is preserved and
applied independently per polarity, clips land in the right four directories,
and clips are named `custom_*` so evaluation buckets them as recorded data.

ffmpeg conversion is stubbed so the test needs no audio toolchain — the split
and placement logic is what is under test, not the resampler.
"""

import io
import sys
import tempfile
import unittest
import zipfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import forge
import google_tts


def _make_zip(n_pos: int, n_neg: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(n_pos):
            z.writestr(f"positive/p_{i:04d}.wav", b"RIFFfake")
        for i in range(n_neg):
            z.writestr(f"negative/n_{i:04d}.wav", b"RIFFfake")
    return buf.getvalue()


class ImportDatasetTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._tmp = Path(self._tmpdir.name)
        # Point forge's wakeword tree at a temp dir with one config.
        self._orig_wakewords = forge.WAKEWORDS
        forge.WAKEWORDS = self._tmp / "wakewords"
        work = self._tmp / "work"
        cfg_dir = forge.WAKEWORDS / "hey_test"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yml").write_text(
            f"model_name: hey_test\noutput_dir: {work}\n"
        )
        self._base = work / "hey_test"

        # Stub ffmpeg conversion: just create the destination file.
        self._orig_convert = forge._convert_16k
        forge._convert_16k = lambda src, dest: Path(dest).write_bytes(b"RIFFfake")

    def tearDown(self):
        forge.WAKEWORDS = self._orig_wakewords
        forge._convert_16k = self._orig_convert

    def _count(self, sub):
        d = self._base / sub
        return len(list(d.glob("*.wav"))) if d.is_dir() else 0

    def test_split_is_ten_percent_per_polarity(self):
        # Write the zip to a temp file (import takes a path).
        zpath = self._tmp / "ds.zip"
        zpath.write_bytes(_make_zip(20, 20))
        counts = forge.import_labeled_dataset("hey_test", zpath)

        every = max(2, round(1 / google_tts.TEST_FRACTION))
        self.assertEqual(every, 10)
        # idx 0 and 10 of each 20 go to test → 2 test, 18 train.
        self.assertEqual(counts["positive_test"], 2)
        self.assertEqual(counts["positive_train"], 18)
        self.assertEqual(counts["negative_test"], 2)
        self.assertEqual(counts["negative_train"], 18)
        self.assertEqual(self._count("positive_test"), 2)
        self.assertEqual(self._count("positive_train"), 18)
        self.assertEqual(self._count("negative_test"), 2)
        self.assertEqual(self._count("negative_train"), 18)

    def test_clips_named_custom_for_eval_bucketing(self):
        zpath = self._tmp / "ds.zip"
        zpath.write_bytes(_make_zip(3, 0))
        forge.import_labeled_dataset("hey_test", zpath)
        names = [p.name for p in (self._base / "positive_train").glob("*.wav")]
        names += [p.name for p in (self._base / "positive_test").glob("*.wav")]
        self.assertTrue(all(n.startswith("custom_") for n in names))
        self.assertTrue(names)

    def test_non_audio_and_stray_dirs_are_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("positive/ok.wav", b"RIFF")
            z.writestr("positive/readme.txt", b"hi")
            z.writestr("other/thing.wav", b"RIFF")
        zpath = self._tmp / "ds.zip"
        zpath.write_bytes(buf.getvalue())
        counts = forge.import_labeled_dataset("hey_test", zpath)
        self.assertEqual(counts["positive_train"] + counts["positive_test"], 1)
        self.assertEqual(counts["skipped"], 2)

    def test_repeated_imports_do_not_overwrite_each_other(self):
        zpath = self._tmp / "ds.zip"
        zpath.write_bytes(_make_zip(5, 0))
        forge.import_labeled_dataset("hey_test", zpath)
        forge.import_labeled_dataset("hey_test", zpath)
        # Two imports of 5 positives each → 10 distinct files, not 5 clobbered.
        total = self._count("positive_train") + self._count("positive_test")
        self.assertEqual(total, 10)

    def test_unknown_wake_word_raises(self):
        zpath = self._tmp / "ds.zip"
        zpath.write_bytes(_make_zip(1, 0))
        with self.assertRaises(FileNotFoundError):
            forge.import_labeled_dataset("nope", zpath)

    def test_source_inventory_counts_and_caches_wav_duration(self):
        for directory, name in (
            ("positive_train", "custom_room.wav"),
            ("positive_train", "google_en-US_voice.wav"),
            ("negative_test", "piper_adversarial.wav"),
        ):
            path = self._base / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 16000)

        inventory = forge.source_inventory(self._base)

        self.assertEqual(inventory["positive"]["custom"]["train"]["count"], 1)
        self.assertEqual(inventory["positive"]["google"]["train"]["seconds"], 1.0)
        self.assertEqual(inventory["negative"]["piper"]["test"]["count"], 1)
        self.assertTrue((self._base / forge.SOURCE_INVENTORY).exists())

    def test_resolve_training_mix_keeps_polarities_independent(self):
        inventory = {
            polarity: {source: {"train": {"count": count}, "test": {"count": 0}}
                       for source, count in counts.items()}
            for polarity, counts in {
                "positive": {"custom": 2, "piper": 8, "google": 0},
                "negative": {"custom": 5, "piper": 5, "google": 0},
            }.items()
        }
        positive = forge.resolve_training_mix(
            inventory, "positive", {"custom": 70, "piper": 30, "google": 0})
        negative = forge.resolve_training_mix(
            inventory, "negative", {"custom": 20, "piper": 80, "google": 0})

        self.assertEqual(positive["draws"], {"custom": 7, "piper": 3, "google": 0})
        self.assertEqual(negative["draws"], {"custom": 2, "piper": 8, "google": 0})


if __name__ == "__main__":
    unittest.main()
