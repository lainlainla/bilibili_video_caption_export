import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "settings.local.json"
        self.patch = patch.object(settings, "SETTINGS_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_secret_is_retained_but_never_exposed(self):
        with patch.dict(os.environ, {}, clear=True):
            config = settings.save_settings({"api_key": "test-only-secret"})
            config = settings.save_settings({"api_key": "", "model": "tiny"})
            self.assertEqual(config["api_key"], "test-only-secret")
            public = settings.public_settings(config)
            self.assertTrue(public["api_key_set"])
            self.assertNotIn("test-only-secret", json.dumps(public))
            cleared = settings.save_settings({"clear_api_key": True})
            self.assertFalse(settings.public_settings(cleared)["api_key_set"])

    def test_invalid_update_preserves_file(self):
        settings.save_settings({"model": "small"})
        previous = self.path.read_bytes()
        for update in ({"model": "small.en"}, {"device": "gpu"}, {"device": None}, {"language": "fr"}, {"api_chunk_seconds": True}, {"api_key": "line\nbreak"}):
            with self.assertRaises(ValueError):
                settings.save_settings(update)
            self.assertEqual(self.path.read_bytes(), previous)

    def test_older_settings_default_to_auto_and_device_is_saved(self):
        self.path.write_text('{"model": "small"}', encoding="utf-8")
        self.assertEqual(settings.load_settings()["device"], "auto")
        for device in ("cpu", "cuda", "auto"):
            self.assertEqual(settings.save_settings({"device": device})["device"], device)

    def test_corrupted_config_is_not_silently_replaced(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            settings.save_settings({"model": "tiny"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_environment_fallback(self):
        with patch.dict(os.environ, {"V2T_API_KEY": "test-environment-key"}, clear=True):
            self.assertEqual(settings.effective_api_key(settings.load_settings()), "test-environment-key")


if __name__ == "__main__":
    unittest.main()
