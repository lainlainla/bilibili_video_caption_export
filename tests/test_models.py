"""Resource checks and bounded download retries; never download model weights."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import model_manager as models


CPU_REPORT = {"ok": True, "status": "ready", "device": "cpu", "resolved_device": "cpu",
              "compute_type": "int8", "compute_types": ["int8", "float32"],
              "gpu_name": None, "free_vram_gb": None, "messages": []}


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in (("platform.machine", "AMD64"), ("resolve_device", CPU_REPORT),
                              ("_available_memory", 16 * models.GIB)):
            mocked = patch(f"model_manager.{target}", return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def create_model(self, name="small"):
        directory = models.model_path(name, self.root)
        directory.mkdir(parents=True)
        for filename in models.REQUIRED_FILES:
            (directory / filename).write_bytes(b"stub")
        return directory

    def test_insufficient_memory_blocks_and_suggests_smaller(self):
        with patch.object(models, "_available_memory", return_value=1.6 * models.GIB):
            result = models.assess_model("large-v3", self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["recommended_model"], "base")
        self.assertTrue(any("可用内存" in item for item in result["messages"]))

    def test_low_but_not_minimum_memory_warns(self):
        with patch.object(models, "_available_memory", return_value=2 * models.GIB):
            result = models.assess_model("small", self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "warning")
        self.assertTrue(result["estimate_only"])
        self.assertEqual(result["recommended_model"], "base")

    def test_downloaded_model_does_not_require_second_copy_disk_space(self):
        self.create_model()
        with patch.object(models, "_disk_free", return_value=10):
            result = models.assess_model("small", self.root)
        self.assertTrue(result["downloaded"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["required_disk_gb"], 0)
        json.dumps(result)

    def test_not_downloaded_and_disk_full_blocks(self):
        with patch.object(models, "_disk_free", return_value=1):
            result = models.assess_model("small", self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["downloaded"])
        self.assertGreater(result["required_disk_gb"], 0)

    def test_disk_pressure_can_still_suggest_smaller_model(self):
        with patch.object(models, "_disk_free", return_value=600_000_000):
            result = models.assess_model("small", self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["recommended_model"], "base")

    def test_exact_model_directory_reuses_nonstandard_name(self):
        directory = self.root / "my existing whisper weights"
        directory.mkdir()
        for filename in models.REQUIRED_FILES:
            (directory / filename).write_bytes(b"stub")
        with patch.object(models, "_disk_free", return_value=1) as disk, patch("faster_whisper.utils.download_model") as download:
            assessment = models.assess_model("small", self.root, directory=directory)
            prepared = models.prepare_model("small", self.root, directory=directory)
        self.assertTrue(assessment["downloaded"])
        self.assertEqual(assessment["model_dir"], str(directory))
        self.assertEqual(assessment["required_disk_gb"], 0)
        self.assertEqual(prepared, directory)
        self.assertEqual(disk.call_args.args, (directory,))
        download.assert_not_called()

    def test_new_custom_path_checks_nearest_existing_directory(self):
        with patch("model_manager.shutil.disk_usage", return_value=SimpleNamespace(free=20 * models.GIB)) as usage:
            result = models.assess_model("small", self.root / "new" / "folder")
        self.assertTrue(result["ok"])
        usage.assert_called_once_with(self.root)
        self.assertFalse((self.root / "new").exists())

    def test_file_blocking_custom_directory_is_reported(self):
        blocker = self.root / "file"
        blocker.write_text("occupied")
        result = models.assess_model("small", blocker / "child")
        self.assertFalse(result["ok"])
        self.assertTrue(any("路径被文件占用" in item for item in result["messages"]))

    def test_no_int8_cpu_support_blocks(self):
        with patch.object(models, "resolve_device", return_value={**CPU_REPORT, "ok": False, "status": "blocked", "messages": ["CPU INT8 unavailable"]}):
            self.assertEqual(models.assess_model("small", self.root)["status"], "blocked")

    def test_insufficient_vram_blocks_gpu_and_suggests_smaller(self):
        gpu = {**CPU_REPORT, "device": "cuda", "resolved_device": "cuda", "compute_type": "int8_float16",
               "gpu_name": "Test GPU", "free_vram_gb": 2.1}
        with patch.object(models, "resolve_device", return_value=gpu):
            result = models.assess_model("large-v3", self.root, device="cuda")
        self.assertFalse(result["ok"])
        self.assertEqual(result["recommended_model"], "small")
        self.assertEqual(result["resolved_device"], "cuda")
        self.assertTrue(any("可用显存" in item for item in result["messages"]))

    def test_auto_cpu_fallback_reason_is_not_hidden(self):
        with patch.object(models, "resolve_device", return_value={**CPU_REPORT, "device": "auto", "status": "warning", "messages": ["missing GPU runtime; CPU fallback"]}):
            result = models.assess_model("small", self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "warning")
        self.assertIn("missing GPU runtime; CPU fallback", result["messages"])
        self.assertIsNone(result["recommended_model"])
        self.assertFalse(any("可尝试较小的" in message for message in result["messages"]))

    def test_empty_model_file_is_not_ready(self):
        directory = self.create_model()
        (directory / "model.bin").write_bytes(b"")
        self.assertFalse(models.model_ready("small", self.root))

    def test_unknown_model_does_not_escape_root(self):
        with self.assertRaises(ValueError):
            models.model_path("../other", self.root)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        mocked = patch.object(models, "assess_model", return_value={"ok": True, "downloaded": False})
        self.assess = mocked.start()
        self.addCleanup(mocked.stop)
        mocked = patch.object(models, "sleep")
        mocked.start()
        self.addCleanup(mocked.stop)
        mocked = patch.object(models, "resolve_device", return_value=CPU_REPORT)
        mocked.start()
        self.addCleanup(mocked.stop)

    def complete_download(self, name, output_dir):
        for filename in models.REQUIRED_FILES:
            (Path(output_dir) / filename).write_bytes(b"stub")

    def test_download_retries_stop_at_three_total(self):
        with patch("faster_whisper.utils.download_model", side_effect=OSError("offline")) as download:
            with self.assertRaisesRegex(RuntimeError, "已尝试 3 次"):
                models.prepare_model("small", self.root)
        self.assertEqual(download.call_count, 3)

    def test_retry_then_success_downloads_only_selected_model(self):
        attempts = 0

        def flaky(name, output_dir):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary failure")
            self.complete_download(name, output_dir)

        with patch("faster_whisper.utils.download_model", side_effect=flaky) as download:
            directory = models.prepare_model("base", self.root)
        self.assertEqual(directory, self.root / "base")
        self.assertEqual(download.call_count, 2)
        self.assertEqual({call.args[0] for call in download.call_args_list}, {"base"})

    def test_incomplete_download_is_retried_with_same_bound(self):
        with patch("faster_whisper.utils.download_model") as download:
            with self.assertRaisesRegex(RuntimeError, "已尝试 3 次"):
                models.prepare_model("small", self.root)
        self.assertEqual(download.call_count, 3)

    def test_exact_directory_download_uses_that_directory(self):
        directory = self.root / "my custom weights"
        with patch("faster_whisper.utils.download_model", side_effect=self.complete_download) as download:
            prepared = models.prepare_model("small", self.root, directory=directory)
        self.assertEqual(prepared, directory)
        download.assert_called_once_with("small", output_dir=str(directory))
        self.assertTrue(models.model_ready("small", self.root, directory=directory))

    def test_existing_model_never_downloads(self):
        self.assess.return_value = {"ok": True, "downloaded": True}
        with patch("faster_whisper.utils.download_model") as download:
            self.assertEqual(models.prepare_model("small", self.root), self.root / "small")
        download.assert_not_called()

    def test_blocked_assessment_never_downloads(self):
        self.assess.return_value = {"ok": False, "messages": ["insufficient RAM"]}
        with patch("faster_whisper.utils.download_model") as download:
            with self.assertRaisesRegex(RuntimeError, "insufficient RAM"):
                models.prepare_model("small", self.root)
        download.assert_not_called()

    def test_verification_consumes_inference_and_labels_its_limits(self):
        consumed = []

        def inference():
            consumed.append(True)
            yield SimpleNamespace(text="irrelevant synthetic result")

        with patch.object(models, "prepare_model", return_value=self.root), patch("faster_whisper.WhisperModel") as model:
            model.return_value.transcribe.return_value = (inference(), None)
            result = models.verify_model("small", self.root)
        self.assertTrue(result["verified"])
        self.assertEqual(consumed, [True])
        self.assertTrue(any("不代表语音识别准确率" in item for item in result["messages"]))

    def test_verification_loads_resolved_cuda_device(self):
        gpu = {**CPU_REPORT, "device": "auto", "resolved_device": "cuda", "compute_type": "int8_float16"}
        with patch.object(models, "prepare_model", return_value=self.root), patch.object(models, "resolve_device", return_value=gpu), patch.object(models, "configure_cuda_runtime") as configure, patch("faster_whisper.WhisperModel") as model:
            model.return_value.transcribe.return_value = (iter(()), None)
            result = models.verify_model("small", self.root, device="auto")
        configure.assert_called_once()
        self.assertTrue(result["verified"])
        self.assertEqual(result["resolved_device"], "cuda")
        self.assertEqual(model.call_args.kwargs["device"], "cuda")
        self.assertEqual(model.call_args.kwargs["compute_type"], "int8_float16")


if __name__ == "__main__":
    unittest.main()
