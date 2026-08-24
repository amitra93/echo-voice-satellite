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


class RawRequest:
    def __init__(self, body):
        self._body = body

    async def read(self):
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


class ForgeWebCredentialsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "google-credentials.json"
        self.patch = patch.object(forge_web, "GOOGLE_CREDS", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    async def test_service_account_key_is_saved_private_and_never_returned(self):
        raw = json.dumps({"type": "service_account", "project_id": "project",
                          "client_email": "forge@example.test", "private_key": "secret"}).encode()
        response = await forge_web.api_google_put(RawRequest(raw))
        state = json.loads(response.body)
        self.assertEqual(state["project_id"], "project")
        self.assertNotIn("private_key", state)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_oauth_client_secret_is_rejected(self):
        raw = json.dumps({"installed": {"client_id": "not-a-service-account"}}).encode()
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_google_put(RawRequest(raw))


if __name__ == "__main__":
    unittest.main()
