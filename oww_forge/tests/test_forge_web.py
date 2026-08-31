import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import yaml

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import forge
import forge_web
import google_tts


class Request:
    def __init__(self, body, name="demo"):
        self.match_info = {"name": name}
        self._body = body

    async def json(self):
        return self._body


class ForgeWebGoogleTtsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.wakewords = Path(self.directory.name)
        self.config = self.wakewords / "demo" / "config.yml"
        self.config.parent.mkdir()
        self.config.write_text(yaml.safe_dump({
            "model_name": "demo",
            "output_dir": str(self.wakewords / "work"),
            "custom_negative_phrases": ["old phrase"],
            "google_tts_samples_per_voice": 30,
            "google_tts_languages": "en-US",
            "google_tts_voices": "",
            "unrelated": "preserved",
        }, sort_keys=False))
        self.wakeword_patch = patch.object(forge, "WAKEWORDS", self.wakewords)
        self.wakeword_patch.start()
        self.require_patch = patch.object(forge_web, "_require_wakeword")
        self.require_patch.start()

    def tearDown(self):
        self.require_patch.stop()
        self.wakeword_patch.stop()
        self.directory.cleanup()

    def read_config(self):
        return yaml.safe_load(self.config.read_text())

    async def test_save_google_tts_config_persists_fields_without_starting_job(self):
        response = await forge_web.api_save_google_tts_config(Request({
            "samples": 125,
            "languages": "en-US, en-GB",
            "voices": "en-US-Chirp3-HD-Achernar",
            "qps": 3.5,
        }))

        self.assertEqual(response.status, 200)
        config = self.read_config()
        self.assertEqual(config["google_tts_samples_per_voice"], 125)
        self.assertEqual(config["google_tts_languages"], "en-US, en-GB")
        self.assertEqual(config["google_tts_voices"], "en-US-Chirp3-HD-Achernar")
        self.assertEqual(config["google_tts_qps"], 3.5)
        self.assertEqual(config["unrelated"], "preserved")

    async def test_confusables_save_does_not_change_google_tts_config(self):
        await forge_web.api_confusables(Request({"phrases": [" New phrase "]}))

        config = self.read_config()
        self.assertEqual(config["custom_negative_phrases"], ["new phrase"])
        self.assertEqual(config["google_tts_samples_per_voice"], 30)
        self.assertEqual(config["google_tts_languages"], "en-US")

    async def test_save_google_tts_config_rejects_invalid_samples(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_save_google_tts_config(Request({
                "samples": 0,
                "languages": "en-US",
                "voices": "",
                "qps": 2,
            }))

    async def test_save_google_tts_config_rejects_invalid_qps(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_save_google_tts_config(Request({
                "samples": 250,
                "languages": "en-US",
                "voices": "",
                "qps": 0,
            }))

    async def test_training_mix_persists_separate_positive_and_negative_weights(self):
        base = self.wakewords / "work" / "demo"
        for directory, filename in (
            ("positive_train", "custom_a.wav"),
            ("positive_train", "piper_a.wav"),
            ("negative_train", "custom_n.wav"),
            ("negative_train", "piper_n.wav"),
        ):
            path = base / directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not-a-wav")
        response = await forge_web.api_training_mix(Request({"training_mix": {
            "positive": {"custom": 60, "piper": 40, "google": 0},
            "negative": {"custom": 20, "piper": 80, "google": 0},
        }}))

        self.assertEqual(response.status, 200)
        config = self.read_config()
        self.assertEqual(config["training_mix"]["positive"]["custom"], 60)
        self.assertEqual(config["training_mix"]["negative"]["piper"], 80)

    async def test_delete_piper_samples_keeps_other_training_audio(self):
        base = self.wakewords / "work" / "demo"
        train = base / "positive_train"
        test = base / "positive_test"
        train.mkdir(parents=True)
        test.mkdir(parents=True)
        generated = [train / "piper_gb_000001_voice_s0.wav", test / "piper_gb_000002_voice_s0.wav"]
        keep = train / "custom_recording.wav"
        google = train / "google_voice.wav"
        for path in generated + [keep, google]:
            path.write_bytes(b"wav")

        response = await forge_web.api_delete_piper_samples(Request({}))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["deleted"], 2)
        self.assertFalse(any(path.exists() for path in generated))
        self.assertTrue(keep.exists())
        self.assertTrue(google.exists())

    async def test_prune_google_tts_previews_then_deletes_only_unselected_clips(self):
        base = self.wakewords / "work" / "demo"
        for directory in ("positive_train", "positive_test", "negative_train", "negative_test"):
            (base / directory).mkdir(parents=True, exist_ok=True)
        kept = base / "positive_train" / "google_en-US_Chirp3-HD-A_000000.wav"
        removed = base / "negative_train" / "google_ja-JP_Chirp3-HD-B_000000.wav"
        custom = base / "positive_train" / "custom_recording.wav"
        for path in (kept, removed, custom):
            path.write_bytes(b"wav")

        with patch.object(google_tts, "selected_chirp3_pairs", return_value=[("en-US", "Chirp3-HD-A")]):
            preview = await forge_web.api_prune_google_tts(Request({}))
            payload = json.loads(preview.body)
            self.assertEqual(payload["clips"], 1)
            self.assertEqual(payload["deleted"], 0)
            self.assertTrue(removed.exists())

            confirmed = await forge_web.api_prune_google_tts(Request({"confirm": True}))
            payload = json.loads(confirmed.body)
            self.assertEqual(payload["deleted"], 1)

        self.assertTrue(kept.exists())
        self.assertTrue(custom.exists())
        self.assertFalse(removed.exists())


if __name__ == "__main__":
    unittest.main()
