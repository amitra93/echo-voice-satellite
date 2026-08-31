import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))

from torch_features import EMBEDDING_DIM, MEL_STEP, MEL_WINDOW, TorchAudioFeatures


class FeatureContractTests(unittest.TestCase):
    def test_constants_match_openwakeword_contract(self):
        self.assertEqual(MEL_WINDOW, 76)
        self.assertEqual(MEL_STEP, 8)
        self.assertEqual(EMBEDDING_DIM, 96)

    def test_torch_features_match_onnx_for_two_second_pcm(self):
        try:
            import torch
            from openwakeword.utils import AudioFeatures
        except ImportError as error:
            self.skipTest(str(error))
        if not torch.cuda.is_available():
            self.skipTest("ROCm/CUDA GPU required for torch backend parity")

        rng = np.random.default_rng(7)
        clips = rng.integers(-32768, 32767, size=(2, 32000), dtype="int16")
        onnx = AudioFeatures(inference_framework="onnx", device="cpu")
        expected_raw = onnx.melspec_model.run(None, {"input": clips.astype("float32")})[0].squeeze(1)
        torch_features = TorchAudioFeatures()
        actual_raw = torch_features._raw_mels(clips).cpu().numpy()

        scale = max(float(np.abs(expected_raw).max()), 1.0)
        np.testing.assert_allclose(actual_raw, expected_raw, rtol=1e-4,
                                   atol=1e-5 + 1e-4 * scale)

        expected_mels = expected_raw / 10 + 2
        actual_mels = torch_features._mels(clips).cpu().numpy()
        scale = max(float(np.abs(expected_mels).max()), 1.0)
        np.testing.assert_allclose(actual_mels, expected_mels, rtol=1e-4,
                                   atol=1e-5 + 1e-4 * scale)

        expected_windows = np.stack([
            expected_mels[:, i:i + MEL_WINDOW, :]
            for i in range(0, expected_mels.shape[1] - MEL_WINDOW + 1, MEL_STEP)
        ], axis=1)
        actual_windows = torch_features._mels(clips).unfold(
            1, MEL_WINDOW, MEL_STEP).permute(0, 1, 3, 2).cpu().numpy()
        np.testing.assert_allclose(actual_windows, expected_windows, rtol=1e-5, atol=1e-5)

        expected = onnx.embed_clips(clips, batch_size=2, ncpu=1)
        actual = torch_features.embed_clips(clips, batch_size=2)

        self.assertEqual(expected.shape, (2, 16, 96))
        self.assertEqual(actual.shape, expected.shape)
        self.assertEqual(actual.dtype, np.float32)
        scale = max(float(np.abs(expected).max()), 1.0)
        np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-5 + 1e-4 * scale)


if __name__ == "__main__":
    unittest.main()
