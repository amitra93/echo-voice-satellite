import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import piper_voices


class PiperVoiceGenerationTests(unittest.TestCase):
    def test_synthesize_skips_when_existing_target_is_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            train = base / "positive_train"
            test = base / "positive_test"
            train.mkdir()
            test.mkdir()
            existing_train = train / "piper_gb_000000_voice_s0.wav"
            existing_test = test / "piper_gb_000001_voice_s0.wav"
            existing_train.write_bytes(b"original train")
            existing_test.write_bytes(b"original test")

            # A met target must return before loading or invoking a voice model.
            with patch.object(piper_voices, "ensure_voice", side_effect=AssertionError("loaded a voice")):
                piper_voices.synthesize(
                    phrases=["hey biscuit"], n_samples=2,
                    train_dir=train, test_dir=test, assets=base,
                    voices=["voice"], language="en_US",
                )

            self.assertEqual(existing_train.read_bytes(), b"original train")
            self.assertEqual(existing_test.read_bytes(), b"original test")


if __name__ == "__main__":
    unittest.main()
