import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import forge


class FeatureFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.work = Path(self.tmpdir.name)
        self.config = {
            "augmentation_rounds": 1,
            "use_musan_background": False,
            "use_slr28_augmentation": False,
        }
        for directory in forge.CLIP_DIRS:
            (self.work / directory).mkdir()
        for filename in forge.FEATURE_FILES:
            (self.work / filename).write_bytes(b"features")
        self.clip = self.work / "positive_train" / "clip.wav"
        self.clip.write_bytes(b"one")

    def test_missing_manifest_marks_existing_features_stale(self):
        self.assertTrue(forge.features_stale(self.work, self.config))

    def test_manifest_tracks_clip_changes(self):
        forge.write_feature_manifest(self.work, self.config)
        self.assertFalse(forge.features_stale(self.work, self.config))

        self.clip.write_bytes(b"changed")
        os.utime(self.clip, None)
        self.assertTrue(forge.features_stale(self.work, self.config))

    def test_manifest_tracks_augmentation_settings(self):
        forge.write_feature_manifest(self.work, self.config)
        changed = {**self.config, "use_musan_background": True}
        self.assertTrue(forge.features_stale(self.work, changed))

    def test_missing_feature_array_is_stale(self):
        forge.write_feature_manifest(self.work, self.config)
        (self.work / forge.FEATURE_FILES[0]).unlink()
        self.assertTrue(forge.features_stale(self.work, self.config))


if __name__ == "__main__":
    unittest.main()
