import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import device_manager as devices


class ResolveTests(unittest.TestCase):
    def setUp(self):
        cpu = patch("ctranslate2.get_supported_compute_types", return_value={"int8", "float32"})
        cpu.start()
        self.addCleanup(cpu.stop)
        gpu = patch.object(devices, "_gpu_info", return_value={"gpu_name": "Test GPU", "free_vram_gb": 7.5})
        gpu.start()
        self.addCleanup(gpu.stop)

    def test_cpu_override_never_probes_cuda(self):
        with patch.object(devices, "_cached_probe") as probe:
            report = devices.resolve_device("cpu")
        probe.assert_not_called()
        self.assertTrue(report["ok"])
        self.assertEqual(report["resolved_device"], "cpu")
        self.assertEqual(report["compute_type"], "int8")

    def test_auto_uses_verified_runtime_cuda(self):
        with patch.object(devices, "_cached_probe", return_value={"ok": True, "compute_types": ["int8_float16"]}):
            report = devices.resolve_device("auto")
        self.assertTrue(report["ok"])
        self.assertEqual(report["resolved_device"], "cuda")
        self.assertEqual(report["compute_type"], "int8_float16")
        self.assertEqual(report["gpu_name"], "Test GPU")
        json.dumps(report)

    def test_auto_missing_runtime_falls_back_with_reason(self):
        with patch.object(devices, "_cached_probe", return_value={"ok": False, "reason": "cublas64_12.dll missing"}):
            report = devices.resolve_device("auto")
        self.assertTrue(report["ok"])
        self.assertEqual(report["resolved_device"], "cpu")
        self.assertEqual(report["status"], "warning")
        self.assertIn("cublas64_12.dll", report["fallback_reason"])

    def test_explicit_cuda_never_silently_falls_back(self):
        with patch.object(devices, "_cached_probe", return_value={"ok": False, "reason": "no GPU"}):
            report = devices.resolve_device("cuda")
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["resolved_device"])

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(ValueError):
            devices.resolve_device("metal")


class RuntimeTests(unittest.TestCase):
    def test_optional_managed_directory_is_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nvidia" / "cublas" / "bin"
            target.mkdir(parents=True)
            with patch.object(devices, "MANAGED_GPU_ROOT", Path(temporary)):
                self.assertIn(target.resolve(), devices._runtime_directories())

    def test_child_environment_does_not_mutate_global_environment(self):
        with patch.object(devices, "_runtime_directories", return_value=[Path("runtime-bin")]):
            before = dict(os.environ)
            prepared = devices.cuda_environment()
            self.assertEqual(dict(os.environ), before)
            variable = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
            self.assertIn("runtime-bin", prepared[variable])

    def test_probe_runs_isolated_and_native_crash_is_reported(self):
        with patch.object(devices, "configure_cuda_runtime"), patch.object(devices, "cuda_environment", return_value={}), patch.object(devices.subprocess, "run", return_value=SimpleNamespace(returncode=3221225477, stdout="")) as run:
            result = devices._probe_cuda()
        self.assertFalse(result["ok"])
        self.assertIn("退出码", result["reason"])
        self.assertIn("--probe", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_probe_timeout_returns_unavailable(self):
        with patch.object(devices, "configure_cuda_runtime"), patch.object(devices, "cuda_environment", return_value={}), patch.object(devices.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            self.assertFalse(devices._probe_cuda()["ok"])

    def test_repeated_probe_is_cached_but_refresh_rechecks(self):
        with patch.object(devices, "_PROBE_CACHE", None), patch.object(devices, "_runtime_directories", return_value=[]), patch.object(devices, "_probe_cuda", return_value={"ok": True}) as probe:
            devices._cached_probe()
            devices._cached_probe()
            self.assertEqual(probe.call_count, 1)
            devices._cached_probe(refresh=True)
            self.assertEqual(probe.call_count, 2)

    def test_transcription_uses_gpu_and_keeps_string_return(self):
        from transcribe import transcribe_audio

        selected = {"ok": True, "resolved_device": "cuda", "compute_type": "int8_float16", "messages": []}
        with patch.object(devices, "resolve_device", return_value=selected), patch.object(devices, "configure_cuda_runtime") as configure, patch("transcribe.prepare_model", return_value=Path("unused-model")), patch("faster_whisper.WhisperModel") as whisper:
            whisper.return_value.transcribe.return_value = (iter([SimpleNamespace(text=" Hello ", end=1)]), SimpleNamespace(language="en", duration=1))
            text = transcribe_audio(Path("unused.wav"), Path("unused-model"), "en", device="cuda")
        self.assertEqual(text, "Hello")
        configure.assert_called_once()
        self.assertEqual(whisper.call_args.kwargs["device"], "cuda")
        self.assertEqual(whisper.call_args.kwargs["compute_type"], "int8_float16")


if __name__ == "__main__":
    unittest.main()
