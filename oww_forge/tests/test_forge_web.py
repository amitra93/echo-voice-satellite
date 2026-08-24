import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

import forge
import forge_web


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


if __name__ == "__main__":
    unittest.main()
