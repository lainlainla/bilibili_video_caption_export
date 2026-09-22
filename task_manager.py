"""An in-memory queue, with one spawn worker and process-tree cancellation.

Workers compute results; only the parent's finalize callback publishes files.
Cancellation and finalization compete for the same lock. A cancellation that
wins never calls finalize. A finalization already holding the lock completes
before cancel can return, so saved output is never reported as cancelled.
"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import multiprocessing
import multiprocessing.spawn
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4


ACTIVE_STATUSES = frozenset({"starting", "running", "cancelling"})
_SPAWN_LOCK = Lock()


class TaskBusyError(RuntimeError):
    pass


def _secret_values(config):
    values = (config.get("api_key"), os.environ.get("V2T_API_KEY"), os.environ.get("OPENAI_API_KEY"))
    return tuple(value for value in values if isinstance(value, str) and value)


def _safe_text(value, config):
    text = str(value)
    for secret in _secret_values(config):
        text = text.replace(secret, "[已隐藏密钥]")
    return text


def _public_result(value, config):
    if isinstance(value, dict):
        return {key: _public_result(item, config) for key, item in value.items()
                if isinstance(key, str) and not key.startswith("_") and key.lower() != "api_key"}
    if isinstance(value, (tuple, list)):
        return [_public_result(item, config) for item in value]
    if isinstance(value, str):
        return _safe_text(value, config)
    return value


def _worker_entry(connection, worker, kind, config, payload, directory):
    """Do not execute user work before the parent establishes containment."""
    try:
        if os.name != "nt":
            os.setsid()
        directory = Path(directory)
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(directory)
        tempfile.tempdir = str(directory)
        # pythonw has no console streams; discard incidental library progress.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        connection.send(("ready", None))
        if connection.recv() != "run":
            return
        result = worker(kind, config, payload, directory)
        if not isinstance(result, dict):
            raise TypeError("任务处理函数必须返回字典。")
        connection.send(("result", result))
    except BaseException as error:
        try:
            connection.send(("error", _safe_text(error, config)))
        except (EOFError, OSError, BrokenPipeError):
            pass
    finally:
        connection.close()


class _WindowsJob:
    """An owned, non-inherited Job Object; closing it kills all assigned workers."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        self._ctypes = ctypes
        self._accounting = Accounting
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        functions = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in functions.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self._kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        # SET_QUOTA | TERMINATE are the rights required by AssignProcessToJobObject.
        handle = self._kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self.handle, handle):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
        finally:
            self._kernel.CloseHandle(handle)

    def terminate(self):
        if not self._kernel.TerminateJobObject(self.handle, 1):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        deadline = monotonic() + 10
        while True:
            accounting = self._accounting()
            if not self._kernel.QueryInformationJobObject(self.handle, 1, self._ctypes.byref(accounting), self._ctypes.sizeof(accounting), None):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            if monotonic() >= deadline:
                raise RuntimeError("仍未确认任务子进程全部退出，暂不能启动下一任务。")
            sleep(0.02)

    def close(self):
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def _group_alive(group):
    """Ignore reparented zombies: they cannot execute or retain open media files."""
    import psutil

    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.info["status"] != psutil.STATUS_ZOMBIE and os.getpgid(process.pid) == group:
                return True
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return False


@dataclass
class _Task:
    task_id: str
    kind: str
    config: dict
    temporary: object
    label: str = ""
    payload: object = None
    started: float = field(default_factory=monotonic)
    status: str = "starting"
    stage: str = "正在启动独立任务进程"
    result: object = None
    error: object = None
    process: object = None
    connection: object = None
    job: object = None
    job_assigned: bool = False
    terminated: bool = False
    stop_lock: object = field(default_factory=Lock)
    done: object = field(default_factory=Event)
    monitor: object = None
    outcome: object = None


class TaskManager:
    def __init__(self, worker, finalize=None, *, history_limit=8, temporary_parent=None):
        if not callable(worker) or (finalize is not None and not callable(finalize)):
            raise TypeError("worker 与 finalize 必须可调用。")
        self._worker = worker
        self._finalize = finalize
        self._history_limit = max(1, int(history_limit))
        self._temporary_parent = Path(temporary_parent) if temporary_parent is not None else None
        self._lock = Lock()
        self._active = None
        self._pending = []
        self._queue_running = False
        self._history = OrderedDict()
        self._closed = False
        self._context = multiprocessing.get_context("spawn")

    @staticmethod
    def _snapshot(task):
        return {"task_id": task.task_id, "kind": task.kind, "label": task.label, "status": task.status,
                "seconds": 0 if task.status == "queued" else round(monotonic() - task.started, 1), "result": deepcopy(task.result),
                "error": task.error, "stage": task.stage}

    def current(self):
        with self._lock:
            if self._active is not None:
                return self._snapshot(self._active)
            return deepcopy(next(reversed(self._history.values()))) if self._history else None

    def get(self, task_id):
        with self._lock:
            if self._active is not None and self._active.task_id == task_id:
                return self._snapshot(self._active)
            for task in self._pending:
                if task.task_id == task_id:
                    return self._snapshot(task)
            return deepcopy(self._history[task_id])

    def _queue_state_locked(self):
        return {"running": self._queue_running,
                "active": self._snapshot(self._active) if self._active is not None else None,
                "pending": [self._snapshot(task) for task in self._pending],
                "recent": deepcopy(list(reversed(self._history.values())))}

    def queue_state(self):
        with self._lock:
            return self._queue_state_locked()

    def _create_locked(self, kind, config, setup, *, label="", queued=False):
        if self._closed:
            raise RuntimeError("任务管理器已经关闭。")
        if self._temporary_parent is not None:
            self._temporary_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="v2t-task-", dir=self._temporary_parent)
        task = _Task(uuid4().hex, str(kind), deepcopy(config), temporary,
                     label=_safe_text(label, config))
        try:
            task.payload = setup(Path(temporary.name)) if setup is not None else {}
        except BaseException:
            temporary.cleanup()
            task.config.clear()
            raise
        if queued:
            task.status, task.stage = "queued", "等待开始队列"
        return task

    def enqueue(self, kind, config, setup=None, *, label=""):
        """Freeze settings and own the uploaded input before returning its ID."""
        with self._lock:
            if len(self._pending) >= 50:
                raise ValueError("最多保留 50 个待执行任务，请先处理或移除部分任务。")
            task = self._create_locked(kind, config, setup, label=label, queued=True)
            self._pending.append(task)
            self._schedule_locked()
            return self._snapshot(task) if task.task_id not in self._history else deepcopy(self._history[task.task_id])

    def start_queue(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("任务管理器已经关闭。")
            self._queue_running = True
            self._schedule_locked()
            return self._queue_state_locked()

    def stop_queue(self):
        """Pause scheduling before capturing the process to stop."""
        with self._lock:
            self._queue_running = False
            task_id = self._active.task_id if self._active is not None else None
        if task_id is not None:
            self.cancel(task_id)
        return self.queue_state()

    def _pending_index_locked(self, task_id):
        for index, task in enumerate(self._pending):
            if task.task_id == task_id:
                return index
        if task_id in self._history or (self._active is not None and self._active.task_id == task_id):
            raise TaskBusyError("只能调整或移除尚未开始的任务。")
        raise KeyError(task_id)

    def move_queued(self, task_id, direction):
        if direction not in {"up", "down"}:
            raise ValueError("移动方向必须是 up 或 down。")
        with self._lock:
            index = self._pending_index_locked(task_id)
            target = index + (-1 if direction == "up" else 1)
            if 0 <= target < len(self._pending):
                self._pending[index], self._pending[target] = self._pending[target], self._pending[index]
            return self._queue_state_locked()

    def remove_queued(self, task_id):
        with self._lock:
            index = self._pending_index_locked(task_id)
            task = self._pending[index]
            # Keep the entry if cleanup fails, so it can be retried explicitly.
            task.temporary.cleanup()
            self._pending.pop(index)
            task.payload = None
            task.config.clear()
            return self._queue_state_locked()

    def _remember_locked(self, task):
        self._history[task.task_id] = self._snapshot(task)
        while len(self._history) > self._history_limit:
            self._history.popitem(last=False)

    def _schedule_locked(self):
        while self._queue_running and not self._closed and self._active is None:
            if not self._pending:
                self._queue_running = False
                return
            task = self._pending.pop(0)
            try:
                self._launch_locked(task)
            except Exception:
                # A failed spawn must not strand the rest of the queue. If
                # process cleanup failed, retain ownership and do not advance.
                if self._active is not None:
                    return
                self._remember_locked(task)

    def start(self, kind, config, setup=None):
        """setup(Path) creates parent-owned inputs; worker must be importable."""
        with self._lock:
            if self._active is not None:
                raise TaskBusyError("已有任务正在处理或终止，请稍后再试。")
            task = self._create_locked(kind, config, setup)
            self._launch_locked(task)
            return self._snapshot(task)

    def _launch_locked(self, task):
        task.started = monotonic()
        task.status, task.stage = "starting", "正在启动独立任务进程"
        self._active = task
        child = None
        try:
            parent, child = self._context.Pipe(duplex=True)
            task.connection = parent
            if os.name == "nt":
                task.job = _WindowsJob()
            task.process = self._context.Process(
                target=_worker_entry,
                args=(child, self._worker, task.kind, task.config, task.payload, task.temporary.name),
                name=f"v2t-{task.kind}",
            )
            # set_executable is a public multiprocessing API, but global.
            # Serialize our temporary override and restore it after spawn.
            with _SPAWN_LOCK:
                old_executable = multiprocessing.spawn.get_executable()
                if os.name == "nt":
                    windowless = Path(sys.executable).with_name("pythonw.exe")
                    if not windowless.is_file():
                        raise RuntimeError("此 Python 安装缺少 pythonw.exe，无法无窗口启动任务。请重新安装项目环境。")
                    multiprocessing.set_executable(str(windowless))
                try:
                    task.process.start()
                finally:
                    multiprocessing.set_executable(old_executable)
            child.close()
            child = None
            if task.job is not None:
                task.job.assign(task.process)
                task.job_assigned = True
            task.monitor = Thread(target=self._watch, args=(task,), name=f"watch-{task.task_id[:8]}", daemon=True)
            task.monitor.start()
        except BaseException as error:
            if child is not None:
                child.close()
            task.error = _safe_text(error, task.config)
            try:
                self._stop(task)
                self._dispose(task)
            except Exception:
                task.monitor = Thread(target=self._recover_failed_start, args=(task, task.error),
                                      name=f"cleanup-{task.task_id[:8]}", daemon=True)
                task.monitor.start()
                raise error
            self._active = None
            task.status, task.stage = "failed", "任务进程启动失败"
            task.config.clear()
            task.payload = None
            raise

    def _recover_failed_start(self, task, error):
        while not self._finish(task, error=error):
            sleep(0.1)

    def _stop(self, task):
        """Confirm the entire owned process group/job is dead before cleanup."""
        with task.stop_lock:
            if task.terminated:
                return
            process = task.process
            if process is not None and process.pid is not None:
                if task.job is not None and task.job_assigned:
                    task.job.terminate()
                elif os.name == "nt":
                    # Assignment failed; the child is still waiting for 'run'.
                    if process.is_alive():
                        process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        # Child may not have reached setsid/ready yet.
                        if process.is_alive():
                            process.kill()
                process.join(timeout=10)
                if process.is_alive():
                    raise RuntimeError("任务进程仍未退出，临时文件保留，暂不能启动下一任务。")
                if os.name != "nt":
                    deadline = monotonic() + 10
                    while _group_alive(process.pid):
                        if monotonic() >= deadline:
                            raise RuntimeError("任务子进程仍未全部退出，暂不能启动下一任务。")
                        sleep(0.02)
            task.terminated = True

    @staticmethod
    def _dispose(task):
        if task.connection is not None:
            task.connection.close()
        if task.process is not None:
            task.process.close()
        if task.job is not None:
            task.job.close()
        task.temporary.cleanup()

    def _finish(self, task, result=None, error=None):
        with self._lock:
            if task is not self._active:
                return True
            if task.outcome is None:
                cancelled = task.status == "cancelling"
                try:
                    self._stop(task)
                except Exception as stop_error:
                    task.status = "cancelling"
                    task.stage = "等待确认所有任务进程退出"
                    task.error = _safe_text(stop_error, task.config)
                    return False  # Keep ownership; never free the slot prematurely.
                try:
                    if cancelled:
                        task.status, task.stage = "cancelled", "已强制终止并清理临时文件"
                        task.result, task.error = None, None
                    elif error is not None:
                        task.status, task.stage = "failed", "任务失败"
                        task.error = _safe_text(error, task.config)
                    else:
                        task.stage = "保存完整结果"
                        if self._finalize is not None:
                            replacement = self._finalize(result, task.config)
                            if replacement is not None:
                                result = replacement
                        if isinstance(result, dict):
                            result.setdefault("seconds", round(monotonic() - task.started, 1))
                        task.result = _public_result(result, task.config)
                        task.status, task.stage = "completed", "任务已完成"
                        task.error = None
                except Exception as final_error:
                    task.status, task.stage = "failed", "结果提交失败"
                    task.error = _safe_text(final_error, task.config)
                # Finalization is committed exactly once, even if cleanup needs
                # retrying (e.g. a transient antivirus file handle on Windows).
                task.outcome = (task.status, task.stage, task.error)
            try:
                self._dispose(task)
            except Exception as cleanup_error:
                # Retain the execution slot until parent-owned inputs are gone.
                task.error = "临时文件清理失败：" + _safe_text(cleanup_error, task.config)
                task.status = "cancelling" if task.outcome[0] == "cancelled" else "running"
                task.stage = "进程已停止，等待清理临时文件；暂不能启动下一任务"
                return False
            task.status, task.stage, task.error = task.outcome
            self._remember_locked(task)
            self._active = None
            task.config.clear()
            task.payload = None
            task.done.set()
            self._schedule_locked()
            return True

    def _watch(self, task):
        startup_deadline = monotonic() + 30
        while True:
            with self._lock:
                cancelling = task.status == "cancelling"
                committed = task.outcome is not None
            if cancelling or committed:
                if self._finish(task):
                    return
                sleep(0.1)
                continue
            try:
                if task.connection.poll(0.1):
                    # Drain before joining: a long transcript can fill the pipe.
                    kind, value = task.connection.recv()
                    if kind == "ready":
                        with self._lock:
                            if task.status == "starting":
                                task.status, task.stage = "running", "正在处理"
                                task.connection.send("run")
                    elif kind == "result":
                        if self._finish(task, result=value):
                            return
                    elif kind == "error":
                        if self._finish(task, error=value):
                            return
                    else:
                        raise RuntimeError("任务进程返回了无效消息。")
                elif not task.process.is_alive():
                    raise RuntimeError(f"任务进程意外退出（代码 {task.process.exitcode}）。")
                elif task.status == "starting" and monotonic() > startup_deadline:
                    raise RuntimeError("任务进程启动超时。")
            except (EOFError, OSError, RuntimeError, ValueError) as error:
                if self._finish(task, error=str(error) or "任务进程通信中断。"):
                    return

    def cancel(self, task_id):
        with self._lock:
            if self._active is None or self._active.task_id != task_id:
                if any(task.task_id == task_id for task in self._pending):
                    raise TaskBusyError("此任务尚未开始，请使用移除操作。")
                return deepcopy(self._history[task_id])
            task = self._active
            self._queue_running = False
            if task.outcome is None:
                task.status, task.stage = "cancelling", "正在强制终止任务进程"
        # Killing here also interrupts a monitor blocked inside a large recv().
        try:
            self._stop(task)
        except Exception as error:
            with self._lock:
                if self._active is task:
                    task.error = _safe_text(error, task.config)
            return self.get(task_id)
        task.done.wait(timeout=15)
        return self.get(task_id)

    def close(self):
        with self._lock:
            self._closed = True
            self._queue_running = False
            task = self._active
            pending = self._pending
            self._pending = []
        try:
            if task is not None:
                self.cancel(task.task_id)
        finally:
            cleanup_error = None
            for item in pending:
                try:
                    item.temporary.cleanup()
                except Exception as error:
                    cleanup_error = cleanup_error or error
                finally:
                    item.payload = None
                    item.config.clear()
            if cleanup_error is not None:
                raise cleanup_error
