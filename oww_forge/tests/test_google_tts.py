import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).parents[1]))

from google_tts import (DEFAULT_QPS, _RequestPacer, _write_wav16k,
                        plan_prune_clips)


class GoogleTtsAudioTests(unittest.TestCase):
    def test_default_qps_is_two_and_pacer_interval_is_inverse_qps(self):
        self.assertEqual(DEFAULT_QPS, 2.0)
        self.assertAlmostEqual(_RequestPacer(1 / 4).interval, 0.25)

    def test_response_is_normalized_to_16khz_mono(self):
        try:
            import soundfile as sf
        except ImportError as error:
            self.skipTest(str(error))
        source = BytesIO()
        sf.write(source, np.zeros((2400, 2), dtype="float32"), 24000, format="WAV")
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "clip.wav"
            _write_wav16k(source.getvalue(), dest)
            info = sf.info(dest)
            self.assertEqual(info.samplerate, 16000)
            self.assertEqual(info.channels, 1)
            self.assertEqual(info.subtype, "PCM_16")

    def test_prune_plan_keeps_selected_pair_and_non_google_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for part in ("positive_train", "positive_test", "negative_train", "negative_test"):
                (base / part).mkdir()
            kept = base / "positive_train" / "google_en-US_Chirp3-HD-A_000000.wav"
            old_pos = base / "positive_train" / "google_ja-JP_Chirp3-HD-B_000000.wav"
            old_neg = base / "negative_test" / "google_ja-JP_Chirp3-HD-B_000001.wav"
            custom = base / "positive_train" / "custom_recording.wav"
            for path in (kept, old_pos, old_neg, custom):
                path.write_bytes(b"wav")

            plan = plan_prune_clips(base, [("en-US", "Chirp3-HD-A")])

            self.assertEqual(plan.paths, [old_pos, old_neg])
            self.assertEqual(sum(plan.groups.values()), 2)
            self.assertTrue(kept.exists())
            self.assertTrue(custom.exists())


if __name__ == "__main__":
    unittest.main()
