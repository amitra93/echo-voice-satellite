import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))

from feature_store import CheckpointError, ResumableFeatureWriter


class ResumableFeatureWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name) / "features.npy"
        self.args = ((3, 16, 96), "a" * 64, "extractor-v1")

    def tearDown(self):
        self.temp.cleanup()

    def test_resumes_without_overwriting_completed_rows(self):
        writer = ResumableFeatureWriter(self.dest, *self.args)
        first = np.ones((1, 16, 96), dtype="float32")
        writer.append(first)

        resumed = ResumableFeatureWriter(self.dest, *self.args)
        self.assertEqual(resumed.written, 1)
        np.testing.assert_array_equal(resumed.output[0], first[0])
        resumed.append(np.full((2, 16, 96), 2, dtype="float32"))
        resumed.complete()

        self.assertTrue(self.dest.exists())
        self.assertFalse(resumed.part.exists())
        self.assertFalse(resumed.checkpoint.exists())

    def test_incompatible_checkpoint_is_preserved(self):
        writer = ResumableFeatureWriter(self.dest, *self.args)
        writer.append(np.zeros((1, 16, 96), dtype="float32"))

        with self.assertRaises(CheckpointError):
            ResumableFeatureWriter(self.dest, self.args[0], "b" * 64, self.args[2])

        self.assertTrue(writer.part.exists())
        self.assertTrue(writer.checkpoint.exists())

    def test_corrupt_checkpoint_is_preserved(self):
        writer = ResumableFeatureWriter(self.dest, *self.args)
        writer.checkpoint.write_text("not json", encoding="utf-8")

        with self.assertRaises(CheckpointError):
            ResumableFeatureWriter(self.dest, *self.args)

        self.assertTrue(writer.part.exists())
        self.assertEqual(writer.checkpoint.read_text(encoding="utf-8"), "not json")

    def test_partial_output_never_publishes(self):
        writer = ResumableFeatureWriter(self.dest, *self.args)
        writer.append(np.zeros((1, 16, 96), dtype="float32"))
        with self.assertRaises(CheckpointError):
            writer.complete()
        self.assertFalse(self.dest.exists())
        self.assertTrue(writer.part.exists())


if __name__ == "__main__":
    unittest.main()
