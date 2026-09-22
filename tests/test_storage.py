import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import storage


class StorageTests(unittest.TestCase):
    def test_unicode_nested_path_and_collision_preserve_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "中文 path" / "稿件"
            first = storage.save_transcript("你好\nHello", "../访谈:one", folder)
            second = storage.save_transcript("new", "../访谈:one", folder)
            self.assertTrue(first["saved"])
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(Path(first["path"]).parent, folder)
            self.assertEqual(Path(first["path"]).read_text(encoding="utf-8"), "你好\nHello\n")

    @patch("storage.sleep")
    @patch("storage._write_new_file", side_effect=PermissionError("denied"))
    def test_exactly_three_attempts(self, write, sleep):
        with tempfile.TemporaryDirectory() as folder:
            result = storage.save_transcript("keep this text", "title", folder)
        self.assertFalse(result["saved"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(write.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("storage.sleep")
    def test_recovery_does_not_repeat_successful_write(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            real_write = storage._write_new_file
            calls = []
            def fail_once(*args):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("temporarily unavailable")
                return real_write(*args)
            with patch("storage._write_new_file", side_effect=fail_once):
                result = storage.save_transcript("text", "title", folder)
            self.assertTrue(result["saved"])
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(len(list(Path(folder).glob("*.txt"))), 1)

    def test_cli_named_output_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            first = storage.save_transcript("one", "ignored", folder, filename="chosen.txt")
            second = storage.save_transcript("two", "ignored", folder, filename="chosen.txt")
            self.assertEqual(Path(first["path"]).name, "chosen.txt")
            self.assertEqual(Path(second["path"]).name, "chosen_1.txt")


if __name__ == "__main__":
    unittest.main()
