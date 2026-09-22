"""Real spawn workers and real child processes; no model downloads or API calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

import psutil

from task_manager import TaskBusyError, TaskManager


def worker(kind, config, payload, directory):
    if kind == "error":
        raise RuntimeError("worker rejected " + config["api_key"])
    if kind == "large":
        return {"text": "中文结果" * 500000, "_private": "do not expose"}
    if kind == "wait":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        (directory / "running.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid,
                                                            "temporary": os.environ["TEMP"]}), encoding="utf-8")
        # Hold a real file open: Windows cannot clean it before process death.
        with (directory / "held.tmp").open("w") as held:
            held.write("processing")
            held.flush()
            while True:
                sleep(0.05)
    if kind == "marker":
        (directory / "ready.marker").write_text("ready", encoding="utf-8")
        while not (directory / "release.marker").exists():
            sleep(0.01)
    if kind == "configured":
        return {"text": config["model"]["name"], "_staging": str(directory)}
    return {"text": payload.get("text", "完成"), "_staging": str(directory), "api_key": config.get("api_key", "")}


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="v2t-task-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = []
        self.finalized = []
        self.manager = TaskManager(worker, self.finalize, temporary_parent=self.root, history_limit=2)
        self.addCleanup(self.manager.close)

    def setup(self, directory):
        self.paths.append(directory)
        (directory / "input.txt").write_text("parent owned", encoding="utf-8")
        return {"text": "测试文字"}

    def finalize(self, result, config):
        self.assertTrue(Path(result["_staging"]).is_dir())
        self.finalized.append(result["text"])
        return {**result, "saved": True}

    def wait_for(self, check, timeout=15):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            value = check()
            if value:
                return value
            sleep(0.02)
        self.fail("Timed out waiting for real worker state")

    def terminal(self, identifier):
        return self.wait_for(lambda: (state if (state := self.manager.get(identifier))["status"] in
                                     {"completed", "failed", "cancelled"} else None))

    def test_complete_finalizes_and_cleans_without_private_data(self):
        task = self.manager.start("complete", {"api_key": "fake-secret"}, self.setup)
        result = self.terminal(task["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"]["text"], "测试文字")
        self.assertTrue(result["result"]["saved"])
        self.assertIsInstance(result["result"]["seconds"], (int, float))
        self.assertNotIn("_staging", result["result"])
        self.assertNotIn("api_key", result["result"])
        self.assertNotIn("fake-secret", json.dumps(result))
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.finalized, ["测试文字"])

    def test_cancel_kills_worker_and_child_then_allows_next_task(self):
        task = self.manager.start("wait", {}, self.setup)
        directory = self.paths[0]
        self.wait_for(lambda: (directory / "running.json").exists())
        details = json.loads((directory / "running.json").read_text(encoding="utf-8"))
        self.assertEqual(details["temporary"], str(directory))
        self.assertTrue(alive(details["parent"]))
        self.assertTrue(alive(details["child"]))
        with self.assertRaises(TaskBusyError):
            self.manager.start("complete", {}, self.setup)
        result = self.manager.cancel(task["task_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(alive(details["parent"]))
        self.assertFalse(alive(details["child"]))
        self.assertFalse(directory.exists())
        self.assertEqual(self.finalized, [])
        next_task = self.manager.start("complete", {}, self.setup)
        self.assertEqual(self.terminal(next_task["task_id"])["status"], "completed")

    def test_cancel_before_completion_never_finalizes(self):
        task = self.manager.start("marker", {}, self.setup)
        self.wait_for(lambda: (self.paths[0] / "ready.marker").exists())
        result = self.manager.cancel(task["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.finalized, [])
        self.assertFalse(self.paths[0].exists())

    def test_cancel_after_finalization_wins_reports_completed(self):
        entered, release = Event(), Event()

        def finalizer(result, config):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release finalizer")
            return self.finalize(result, config)

        self.manager._finalize = finalizer
        task = self.manager.start("complete", {}, self.setup)
        self.assertTrue(entered.wait(10))
        replies = []
        cancel_thread = Thread(target=lambda: replies.append(self.manager.cancel(task["task_id"])))
        cancel_thread.start()
        release.set()
        cancel_thread.join(timeout=10)
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(replies[0]["status"], "completed")
        self.assertEqual(self.finalized, ["测试文字"])

    def test_large_pipe_result_does_not_deadlock(self):
        self.manager._finalize = None
        task = self.manager.start("large", {}, self.setup)
        result = self.terminal(task["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["result"]["text"]), 2000000)
        self.assertNotIn("_private", result["result"])
        self.assertFalse(self.paths[0].exists())

    def test_worker_failure_is_redacted_and_releases_slot(self):
        task = self.manager.start("error", {"api_key": "fake-secret"}, self.setup)
        result = self.terminal(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("fake-secret", json.dumps(result))
        self.assertIn("已隐藏密钥", result["error"])
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.finalized, [])

    def test_cleanup_failure_keeps_slot_without_repeating_finalization(self):
        failed, allow_cleanup = Event(), Event()
        dispose = self.manager._dispose

        def temporary_failure(task):
            if not allow_cleanup.is_set():
                failed.set()
                raise PermissionError("temporary file is busy")
            dispose(task)

        with patch.object(self.manager, "_dispose", side_effect=temporary_failure):
            task = self.manager.start("complete", {}, self.setup)
            self.assertTrue(failed.wait(10))
            try:
                self.assertTrue(self.paths[0].exists())
                with self.assertRaises(TaskBusyError):
                    self.manager.start("complete", {}, self.setup)
            finally:
                allow_cleanup.set()
            result = self.terminal(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.finalized, ["测试文字"])
        self.assertFalse(self.paths[0].exists())

    def test_setup_failure_cleans_and_releases_slot(self):
        def broken_setup(directory):
            self.setup(directory)
            raise ValueError("invalid input")

        with self.assertRaisesRegex(ValueError, "invalid input"):
            self.manager.start("complete", {}, broken_setup)
        self.assertFalse(self.paths[0].exists())
        self.assertIsNone(self.manager.current())
        task = self.manager.start("complete", {}, self.setup)
        self.assertEqual(self.terminal(task["task_id"])["status"], "completed")

    def test_history_is_bounded_and_unknown_ids_raise(self):
        identifiers = []
        for _ in range(3):
            task = self.manager.start("complete", {}, self.setup)
            identifiers.append(task["task_id"])
            self.terminal(task["task_id"])
        with self.assertRaises(KeyError):
            self.manager.get(identifiers[0])
        with self.assertRaises(KeyError):
            self.manager.cancel("unknown")
        self.assertEqual(self.manager.current()["task_id"], identifiers[-1])

    def test_close_kills_running_task_and_rejects_new_work(self):
        task = self.manager.start("wait", {}, self.setup)
        self.wait_for(lambda: (self.paths[0] / "running.json").exists())
        details = json.loads((self.paths[0] / "running.json").read_text(encoding="utf-8"))
        self.manager.close()
        self.assertEqual(self.manager.get(task["task_id"])["status"], "cancelled")
        self.assertFalse(alive(details["parent"]))
        self.assertFalse(alive(details["child"]))
        with self.assertRaises(RuntimeError):
            self.manager.start("complete", {}, self.setup)

    def test_queue_reorder_runs_serially_and_accepts_new_items(self):
        def input_for(text):
            def setup(directory):
                self.setup(directory)
                return {"text": text}
            return setup

        first = self.manager.enqueue("marker", {}, input_for("first"), label="视频 1")
        second = self.manager.enqueue("marker", {}, input_for("second"), label="视频 2")
        self.assertEqual(self.manager.get(first["task_id"])["seconds"], 0)
        self.assertIsNone(self.manager.current())
        state = self.manager.move_queued(second["task_id"], "up")
        self.assertEqual([item["label"] for item in state["pending"]], ["视频 2", "视频 1"])
        self.assertEqual(self.manager.move_queued(second["task_id"], "up"), state)
        self.manager.start_queue()
        self.wait_for(lambda: (self.paths[1] / "ready.marker").exists())
        self.assertFalse((self.paths[0] / "ready.marker").exists())
        self.manager.enqueue("complete", {}, input_for("third"), label="视频 3")
        with self.assertRaises(TaskBusyError):
            self.manager.move_queued(second["task_id"], "down")
        (self.paths[1] / "release.marker").touch()
        self.wait_for(lambda: (self.paths[0] / "ready.marker").exists())
        self.assertFalse(self.paths[1].exists())
        self.assertEqual(self.finalized, ["second"])
        (self.paths[0] / "release.marker").touch()
        state = self.wait_for(lambda: (value if not (value := self.manager.queue_state())["running"] else None))
        self.assertEqual(self.finalized, ["second", "first", "third"])
        self.assertIsNone(state["active"])
        self.assertEqual(state["pending"], [])
        self.assertEqual([item["label"] for item in state["recent"]], ["视频 3", "视频 1"])
        self.assertFalse(any(path.exists() for path in self.paths))

    def test_queue_failure_continues_and_settings_are_frozen(self):
        config = {"model": {"name": "small"}, "api_key": "fake-queue-key"}
        failed = self.manager.enqueue("error", config, self.setup)
        task = self.manager.enqueue("configured", config, self.setup, label="small model")
        config["model"]["name"] = "large-v3"
        config["api_key"] = "different-secret"
        state = self.manager.queue_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["pending"][1], self.manager.get(task["task_id"]))
        self.assertNotIn("fake-queue-key", json.dumps(state))
        self.manager.start_queue()
        self.assertEqual(self.terminal(task["task_id"])["result"]["text"], "small")
        error = self.manager.get(failed["task_id"])
        self.assertEqual(error["status"], "failed")
        self.assertNotIn("fake-queue-key", error["error"])
        self.assertEqual(self.finalized, ["small"])

    def test_queue_spawn_failure_cleans_before_continuing(self):
        failed = self.manager.enqueue("complete", {}, self.setup)
        second = self.manager.enqueue("complete", {}, self.setup)
        create_process = self.manager._context.Process
        calls = 0

        def first_spawn_fails(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("test could not spawn a worker")
            self.assertFalse(self.paths[0].exists())
            return create_process(*args, **kwargs)

        with patch.object(self.manager._context, "Process", side_effect=first_spawn_fails):
            self.manager.start_queue()
            self.assertEqual(self.terminal(second["task_id"])["status"], "completed")
        self.assertEqual(self.manager.get(failed["task_id"])["status"], "failed")
        self.assertEqual(self.finalized, ["测试文字"])

    def test_queue_cancel_pauses_then_allows_direct_work_and_resume(self):
        active = self.manager.enqueue("wait", {}, self.setup)
        pending = self.manager.enqueue("marker", {}, self.setup)
        self.manager.start_queue()
        self.wait_for(lambda: (self.paths[0] / "running.json").exists())
        state = self.manager.stop_queue()
        self.assertFalse(state["running"])
        self.assertIsNone(state["active"])
        self.assertEqual(state["pending"][0]["task_id"], pending["task_id"])
        self.assertEqual(self.manager.get(active["task_id"])["status"], "cancelled")
        self.assertFalse((self.paths[1] / "ready.marker").exists())
        direct = self.manager.start("complete", {}, self.setup)
        self.terminal(direct["task_id"])
        self.manager.start_queue()
        self.wait_for(lambda: (self.paths[1] / "ready.marker").exists())
        # Retrying an old cancel must not stop a different, later task.
        self.assertEqual(self.manager.cancel(active["task_id"])["status"], "cancelled")
        self.assertTrue(self.manager.queue_state()["running"])
        self.assertEqual(self.manager.current()["task_id"], pending["task_id"])
        (self.paths[1] / "release.marker").touch()
        self.assertEqual(self.terminal(pending["task_id"])["status"], "completed")

    def test_remove_queued_cleans_and_rejects_started_or_unknown_tasks(self):
        queued = self.manager.enqueue("complete", {}, self.setup)
        self.manager.remove_queued(queued["task_id"])
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.manager.queue_state()["pending"], [])
        with self.assertRaises(KeyError):
            self.manager.get(queued["task_id"])
        with self.assertRaises(KeyError):
            self.manager.remove_queued("unknown")
        with self.assertRaises(KeyError):
            self.manager.move_queued("unknown", "up")
        active = self.manager.start("marker", {}, self.setup)
        with self.assertRaises(TaskBusyError):
            self.manager.remove_queued(active["task_id"])
        self.manager.cancel(active["task_id"])
        with self.assertRaises(TaskBusyError):
            self.manager.remove_queued(active["task_id"])

    def test_close_removes_pending_inputs_and_never_starts_them(self):
        self.manager.enqueue("wait", {}, self.setup)
        self.manager.enqueue("marker", {"api_key": "queued-secret"}, self.setup)
        self.manager.start_queue()
        self.wait_for(lambda: (self.paths[0] / "running.json").exists())
        self.manager.close()
        state = self.manager.queue_state()
        self.assertFalse(state["running"])
        self.assertIsNone(state["active"])
        self.assertEqual(state["pending"], [])
        self.assertFalse(any(path.exists() for path in self.paths))
        self.assertEqual(self.finalized, [])
        with self.assertRaises(RuntimeError):
            self.manager.enqueue("complete", {}, self.setup)
        with self.assertRaises(RuntimeError):
            self.manager.start_queue()

    def test_pending_queue_limit_is_checked_before_copying_input(self):
        for _ in range(50):
            self.manager.enqueue("complete", {}, self.setup)
        with self.assertRaises(ValueError):
            self.manager.enqueue("complete", {}, self.setup)
        self.assertEqual(len(self.paths), 50)
        self.assertFalse(self.manager.queue_state()["running"])


if __name__ == "__main__":
    unittest.main()
