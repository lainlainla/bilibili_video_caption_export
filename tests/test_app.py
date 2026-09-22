import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import app as web
import settings
from task_manager import TaskManager
from worker import finalize_result, run_task


def web_fixture_worker(kind, config, payload, directory):
    if payload.get("url", "").endswith("/wait"):
        time.sleep(60)
    if payload.get("url", "").endswith("/error"):
        raise RuntimeError("bad " + config.get("api_key", ""))
    if kind != "transcribe":
        return {"ok": True, "device": config["device"], "_kind": kind}
    text = Path(payload["audio"]).read_text(encoding="utf-8") if payload.get("audio") else "中文\nEnglish"
    return {"_kind": kind, "title": payload.get("title", "字幕"), "text": text, "method": "fixture",
            "selection": {"model": config["model"], "device": config["device"], "language": payload["language"]}}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path_patch = patch.object(settings, "SETTINGS_PATH", self.root / "settings.local.json")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        settings.save_settings({"output_dir": str(self.root / "中文 output")})
        self.manager = TaskManager(web_fixture_worker, finalize_result, temporary_parent=self.root / "tasks")
        self.addCleanup(self.manager.close)
        manager_patch = patch.object(web, "tasks", self.manager)
        manager_patch.start()
        self.addCleanup(manager_patch.stop)
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self.headers = {"X-V2T-Token": web.app.config["LOCAL_TOKEN"]}

    def post(self, path, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def finish(self, response):
        self.assertEqual(response.status_code, 202)
        task = response.json
        deadline = time.monotonic() + 15
        while task["status"] in {"starting", "running", "cancelling"}:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
            response = self.client.get("/api/tasks/" + task["task_id"])
            self.assertEqual(response.status_code, 200)
            task = response.json
        return task

    def finish_queue(self):
        deadline = time.monotonic() + 15
        while True:
            state = self.client.get("/api/queue").json
            if state["active"] is None and not state["pending"]:
                return state
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_local_write_requires_current_page_token_and_origin(self):
        self.assertEqual(self.client.post("/api/settings", json={"model": "tiny"}).status_code, 403)
        response = self.client.post("/api/settings", json={"model": "tiny"},
                                    headers={**self.headers, "Origin": "https://unrelated.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/tasks/unknown/cancel", json={}).status_code, 403)

    def test_settings_response_redacts_saved_key_and_defaults_to_auto(self):
        response = self.post("/api/settings", json={"api_key": "test-only-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-only-secret", response.get_data(as_text=True))
        self.assertEqual(response.json["settings"]["device"], "auto")
        self.assertTrue(response.json["settings"]["api_key_set"])
        self.assertEqual(len(self.client.get("/api/settings").json["models"]), 6)

    def test_video_url_prefixes_preserve_hosts_and_video_parameters(self):
        examples = {
            "  bilibili.com/video/BV123?p=2&t=30#reply  ": "https://www.bilibili.com/video/BV123?p=2&t=30#reply",
            "www.bilibili.com/video/BV123": "https://www.bilibili.com/video/BV123",
            "https://bilibili.com/video/BV123": "https://www.bilibili.com/video/BV123",
            "youtube.com/watch?v=example": "https://www.youtube.com/watch?v=example",
            "b23.tv/example": "https://b23.tv/example",
            "m.bilibili.com/video/BV123": "https://m.bilibili.com/video/BV123",
            "//youtu.be/example": "https://youtu.be/example",
            "http://video.example:8080/watch": "http://video.example:8080/watch",
            "video.example:8080/watch": "https://video.example:8080/watch",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                with patch.object(self.manager, "start", return_value={"task_id": "fixture"}) as start:
                    response = self.post("/api/transcribe", data={"url": value})
                    self.assertEqual(response.status_code, 202)
                    payload = start.call_args.args[2](self.root)
                    self.assertEqual(payload["url"], expected)

    def test_bad_video_urls_do_not_start_a_task(self):
        for value in ("", "not a link", "https:/bilibili.com/video/BV", "https://", "ftp://video.example/file",
                      "javascript:alert(1)", "https://video.example:99999/watch"):
            with self.subTest(value=value), patch.object(self.manager, "start") as start:
                self.assertEqual(self.post("/api/transcribe", data={"url": value}).status_code, 400)
                start.assert_not_called()

    def test_queue_upload_move_remove_and_sequential_save(self):
        entries = []
        for name in ("first", "second", "removed"):
            response = self.post("/api/queue", data={"source_type": "file", "language": "en",
                                 "media": (io.BytesIO(name.encode()), name + ".wav")})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["status"], "queued")
            entries.append(response.json["task_id"])
        initial = self.client.get("/api/queue").json
        self.assertFalse(initial["running"])
        self.assertIsNone(initial["active"])
        self.assertEqual(self.client.get("/api/tasks/" + entries[1]).json["status"], "queued")
        moved = self.post(f"/api/queue/{entries[1]}/move", json={"direction": "up"})
        self.assertEqual([item["task_id"] for item in moved.json["pending"]], [entries[1], entries[0], entries[2]])
        removed = self.post(f"/api/queue/{entries[2]}/remove", json={})
        self.assertEqual(len(removed.json["pending"]), 2)
        self.assertEqual(len(list((self.root / "tasks").iterdir())), 2)
        self.assertEqual(self.post("/api/queue/start", json={}).status_code, 200)
        finished = self.finish_queue()
        completed = [item for item in finished["recent"] if item["status"] == "completed"]
        self.assertEqual([item["result"]["text"] for item in completed], ["first", "second"])
        self.assertTrue(all(item["result"]["save"]["saved"] for item in completed))
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_queue_accepts_during_work_then_stop_preserves_captured_configuration(self):
        waiting = self.post("/api/queue", data={"url": "video.example/wait"})
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json["label"], "https://video.example/wait")
        self.post("/api/queue/start", json={})
        output = self.root / "captured output"
        settings.save_settings({"model": "tiny", "device": "cpu", "output_dir": str(output), "api_key": "queue-test-private-key"})
        added = self.post("/api/queue", data={"source_type": "file", "language": "zh",
                          "media": (io.BytesIO("排队文字".encode()), "queued.wav")})
        self.assertEqual(added.status_code, 202)
        settings.save_settings({"model": "small", "device": "auto", "output_dir": str(self.root / "later output")})
        refreshed = self.client.get("/api/queue")
        self.assertNotIn("queue-test-private-key", refreshed.get_data(as_text=True))
        self.assertEqual(len(refreshed.json["pending"]), 1)
        active_id = refreshed.json["active"]["task_id"]
        self.assertEqual(self.post(f"/api/queue/{active_id}/move", json={"direction": "down"}).status_code, 409)
        stopped = self.post("/api/queue/stop", json={})
        self.assertFalse(stopped.json["running"])
        self.assertIsNone(stopped.json["active"])
        self.assertEqual([item["task_id"] for item in stopped.json["pending"]], [added.json["task_id"]])
        self.assertEqual(self.client.get("/api/tasks/" + active_id).json["status"], "cancelled")
        self.post("/api/queue/start", json={})
        completed = self.finish_queue()["recent"][0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["selection"], {"model": "tiny", "device": "cpu", "language": "zh"})
        self.assertEqual(Path(completed["result"]["save"]["path"]).parent, output)
        self.assertFalse((self.root / "later output").exists())

    def test_queue_continues_after_failure_and_controls_validate(self):
        self.assertEqual(self.client.post("/api/queue/start", json={}).status_code, 403)
        self.assertEqual(self.post("/api/queue/missing/move", json={"direction": "up"}).status_code, 404)
        self.assertEqual(self.post("/api/queue/missing/move", json={"direction": "sideways"}).status_code, 400)
        self.assertEqual(self.post("/api/queue/missing/remove", json={}).status_code, 404)
        for suffix in ("error", "watch"):
            self.assertEqual(self.post("/api/queue", data={"url": "video.example/" + suffix}).status_code, 202)
        self.post("/api/queue/start", json={})
        finished = self.finish_queue()
        self.assertEqual([task["status"] for task in finished["recent"]], ["completed", "failed"])

    def test_async_upload_saves_complete_result_and_cleans_inputs(self):
        response = self.post("/api/transcribe", data={"source_type": "file", "language": "en",
                             "media": (io.BytesIO("中文\nEnglish".encode()), "sample.wav")})
        task = self.finish(response)
        self.assertEqual(task["status"], "completed")
        result = task["result"]
        self.assertEqual(Path(result["save"]["path"]).read_text(encoding="utf-8"), "中文\nEnglish\n")
        self.assertEqual(list((self.root / "tasks").iterdir()), [])
        self.assertEqual(self.client.get("/api/tasks/current").json["task"]["task_id"], task["task_id"])

    def test_failed_save_keeps_text_and_only_retries_writing(self):
        with patch("storage._write_new_file", side_effect=PermissionError("denied")) as write, patch("storage.sleep"):
            task = self.finish(self.post("/api/transcribe", data={"url": "https://video.example/watch"}))
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["text"], "中文\nEnglish")
        self.assertFalse(task["result"]["save"]["saved"])
        self.assertEqual(write.call_count, 3)

    def test_cancel_busy_task_without_saving_then_restart(self):
        running = self.post("/api/transcribe", data={"url": "https://video.example/wait"})
        self.assertEqual(running.status_code, 202)
        self.assertEqual(self.post("/api/models/prepare", json={}).status_code, 409)
        task_id = running.json["task_id"]
        cancelled = self.post(f"/api/tasks/{task_id}/cancel", json={})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json["status"], "cancelled")
        self.assertFalse((self.root / "中文 output").exists())
        self.assertEqual(list((self.root / "tasks").iterdir()), [])
        task = self.finish(self.post("/api/transcribe", data={"url": "https://video.example/watch"}))
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.post(f"/api/tasks/{task['task_id']}/cancel", json={}).json["status"], "completed")

    def test_preparation_and_runtime_install_keep_device_and_use_tasks(self):
        for path in ("/api/models/prepare", "/api/runtime/install"):
            task = self.finish(self.post(path, json={"device": "cuda"}))
            self.assertEqual(task["result"]["device"], "cuda")
        self.assertEqual(self.post("/api/runtime/install", json={"backend": "api"}).status_code, 400)

    def test_error_redacts_key_and_releases_task(self):
        settings.save_settings({"backend": "api", "api_key": "test-private-key"})
        task = self.finish(self.post("/api/transcribe", data={"url": "https://video.example/error"}))
        self.assertEqual(task["status"], "failed")
        self.assertNotIn("test-private-key", str(task))
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_edited_save_does_not_overwrite_and_invalid_tasks_are_clear(self):
        first = self.post("/api/save", json={"title": "edited", "text": "first"}).json["save"]
        second = self.post("/api/save", json={"title": "edited", "text": "second"}).json["save"]
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(self.client.get("/api/tasks/missing").status_code, 404)
        self.assertEqual(self.post("/api/tasks/missing/cancel", json={}).status_code, 404)


class WorkerRoutingTests(unittest.TestCase):
    def test_caption_route_skips_models(self):
        payload = {"source_type": "url", "url": "https://video.example/watch", "language": "zh", "force_asr": False}
        with tempfile.TemporaryDirectory() as directory, \
             patch("transcribe.fetch_video", return_value=("字幕", None, "字幕文字")), \
             patch("transcribe.transcribe_audio") as local, patch("api_transcribe.transcribe_api") as api:
            result = run_task("transcribe", dict(settings.DEFAULTS), payload, Path(directory))
        local.assert_not_called()
        api.assert_not_called()
        self.assertEqual(result["text"], "字幕文字")
        self.assertNotIn("save", result)

    def test_local_worker_passes_actual_gpu_device_to_model(self):
        report = {"ok": True, "resolved_device": "cuda", "compute_type": "int8_float16", "messages": []}
        config = {**settings.DEFAULTS, "device": "auto"}
        payload = {"source_type": "file", "audio": "fixture.wav", "title": "sample", "language": "en"}
        with patch("device_manager.resolve_device", return_value=report), \
             patch("transcribe.transcribe_audio", return_value="GPU words") as local:
            result = run_task("transcribe", config, payload, Path("."))
        self.assertEqual(local.call_args.kwargs["device"], "cuda")
        self.assertIn("GPU", result["method"])
        self.assertNotIn("save", result)

    def test_api_route_uses_configured_provider_without_loading_local_model(self):
        config = {**settings.DEFAULTS, "backend": "api", "api_base_url": "https://provider.example/v1",
                  "api_key": "test-key", "api_model": "provider-model"}
        payload = {"source_type": "file", "audio": "fixture.wav", "title": "sample", "language": "en"}
        with patch("api_transcribe.transcribe_api", return_value="API words") as api, \
             patch("transcribe.transcribe_audio") as local:
            result = run_task("transcribe", config, payload, Path("."))
        local.assert_not_called()
        self.assertEqual(api.call_args.args[2:5], ("https://provider.example/v1", "test-key", "provider-model"))
        self.assertEqual(result["text"], "API words")


if __name__ == "__main__":
    unittest.main()
