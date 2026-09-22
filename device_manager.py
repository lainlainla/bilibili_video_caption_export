"""Resolve CPU/CUDA without initializing CUDA in the web server process."""

import csv
import ctypes
import io
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
from threading import Lock
from time import monotonic


_DLL_HANDLES = {}
_PROBE_LOCK = Lock()
_PROBE_CACHE = None
PROBE_CACHE_SECONDS = 30
MANAGED_GPU_ROOT = Path(__file__).resolve().parent / ".runtime" / "gpu" / "cu12-v1"


def _runtime_directories():
    """Discover optional NVIDIA wheels only inside the active Python environment."""
    locations = [Path(value) for value in site.getsitepackages()]
    locations.append(Path(sys.prefix) / "Lib" / "site-packages")
    locations.append(MANAGED_GPU_ROOT)
    found = set()
    for location in locations:
        root = location / "nvidia"
        if root.is_dir():
            for component in root.iterdir():
                for name in ("bin", "lib"):
                    directory = component / name
                    if directory.is_dir():
                        found.add(directory.resolve())
    return sorted(found)


def cuda_environment():
    """Environment for starting a worker/probe, including Linux loader paths."""
    result = dict(os.environ)
    variable = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    previous = result.get(variable, "").split(os.pathsep)
    additions = [str(path) for path in _runtime_directories() if str(path) not in previous]
    if additions:
        result[variable] = os.pathsep.join(additions + previous)
    return result


def configure_cuda_runtime():
    """Expose optional NVIDIA wheel libraries to this process and its children only."""
    directories = _runtime_directories()
    if os.name == "nt":
        for directory in directories:
            name = str(directory)
            if name not in _DLL_HANDLES:
                # Handles must remain alive while inference may load more DLLs.
                _DLL_HANDLES[name] = os.add_dll_directory(name)
        variable = "PATH"
    else:
        # On Linux this benefits subsequently started probe/worker processes.
        # Existing processes require a loader path set before Python starts.
        variable = "LD_LIBRARY_PATH"
    prepared = cuda_environment()
    if variable in prepared:
        os.environ[variable] = prepared[variable]
    return [str(path) for path in directories]


def _runtime_library(name):
    for directory in _runtime_directories():
        path = directory / name
        if path.is_file():
            return ctypes.CDLL(str(path))
    return ctypes.CDLL(name)


def _check_library_handle(library, create_name, destroy_name):
    handle = ctypes.c_void_p()
    create = getattr(library, create_name)
    create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_int
    status = create(ctypes.byref(handle))
    if status:
        raise RuntimeError(f"{create_name} 返回错误 {status}")
    destroy = getattr(library, destroy_name)
    destroy.argtypes = [ctypes.c_void_p]
    destroy.restype = ctypes.c_int
    destroy(handle)


def _probe_in_child():
    """Native library failures/crashes are contained in this short-lived process."""
    try:
        configure_cuda_runtime()
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("未检测到可用的 NVIDIA CUDA 显卡或驱动。")
        compute_types = sorted(ctranslate2.get_supported_compute_types("cuda", 0))
        if "int8_float16" not in compute_types:
            raise RuntimeError("当前显卡不支持本项目的 CUDA INT8_FLOAT16 推理。")
        cublas_name = "cublas64_12.dll" if os.name == "nt" else "libcublas.so.12"
        cudnn_name = "cudnn64_9.dll" if os.name == "nt" else "libcudnn.so.9"
        cublas = _runtime_library(cublas_name)
        _check_library_handle(cublas, "cublasCreate_v2", "cublasDestroy_v2")
        cudnn = _runtime_library(cudnn_name)
        _check_library_handle(cudnn, "cudnnCreate", "cudnnDestroy")
        return {"ok": True, "compute_types": compute_types, "reason": None}
    except Exception as error:
        return {"ok": False, "compute_types": [], "reason": f"CUDA 运行环境未就绪：{error}"}


def _probe_cuda():
    configure_cuda_runtime()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=flags, env=cuda_environment(),
        )
        if completed.returncode:
            return {"ok": False, "compute_types": [], "reason": f"CUDA 独立探测未完成（退出码 {completed.returncode}），已保护主程序。"}
        data = json.loads(completed.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise ValueError("invalid probe response")
        return data
    except subprocess.TimeoutExpired:
        return {"ok": False, "compute_types": [], "reason": "CUDA 独立探测超过 20 秒，可能存在驱动或运行库问题。"}
    except (OSError, ValueError):
        return {"ok": False, "compute_types": [], "reason": "无法启动或读取 CUDA 独立探测，请检查 Python 和显卡运行环境。"}


def _cached_probe(refresh=False):
    global _PROBE_CACHE
    directories = _runtime_directories()
    # Installing optional runtime wheels invalidates a previous missing-library result.
    fingerprint = tuple((str(path), path.stat().st_mtime_ns) for path in directories)
    with _PROBE_LOCK:
        now = monotonic()
        if refresh or _PROBE_CACHE is None or _PROBE_CACHE[0] != fingerprint or now - _PROBE_CACHE[1] >= PROBE_CACHE_SECONDS:
            _PROBE_CACHE = (fingerprint, now, _probe_cuda())
        return dict(_PROBE_CACHE[2])


def _gpu_info():
    command = shutil.which("nvidia-smi")
    if not command and os.name == "nt":
        candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            command = str(candidate)
    if not command:
        return {}
    try:
        result = subprocess.run(
            [command, "--query-gpu=index,uuid,name,memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            return {}
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")[0].strip()
        rows = list(csv.reader(io.StringIO(result.stdout)))
        # CUDA's default ordinal order can differ from nvidia-smi on multi-GPU
        # hosts. Do not attribute another adapter's free memory to device 0.
        if len(rows) > 1 and not visible and os.environ.get("CUDA_DEVICE_ORDER") != "PCI_BUS_ID":
            return {}
        for row in rows:
            if len(row) != 5:
                continue
            index, uuid, name, free, total = [value.strip() for value in row]
            if visible and visible != index and not uuid.startswith(visible):
                continue
            # Without an override use the first NVIDIA device, matching this app's device 0.
            return {"gpu_name": name, "free_vram_gb": round(float(free) / 1024, 2),
                    "total_vram_gb": round(float(total) / 1024, 2)}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {}


def resolve_device(requested="auto", *, refresh=False):
    if requested not in ("auto", "cpu", "cuda"):
        raise ValueError("请选择 auto、cpu 或 cuda 计算设备。")
    result = {"ok": True, "status": "ready", "device": requested,
              "resolved_device": None, "device_index": 0, "compute_type": None, "compute_types": [],
              "gpu_name": None, "free_vram_gb": None, "total_vram_gb": None,
              "messages": [], "fallback_reason": None}
    if requested != "cpu":
        probe = _cached_probe(refresh=refresh)
        result.update(_gpu_info())
        if probe["ok"]:
            result.update(resolved_device="cuda", compute_type="int8_float16",
                          compute_types=probe["compute_types"])
            result["messages"] = ["CUDA 设备及 cuBLAS/cuDNN 运行库探测通过，使用 GPU INT8_FLOAT16；完整模型运行仍需自检。"]
            if result["free_vram_gb"] is None:
                result["status"] = "warning"
                result["messages"].append("无法读取显卡剩余显存，暂不能作显存容量预估；可运行模型自检确认。")
            return result
        reason = probe["reason"]
        if requested == "cuda":
            result.update(ok=False, status="blocked")
            result["messages"] = [reason, "已指定 GPU，不会自动改用 CPU。请安装可选 GPU 运行库或修复显卡驱动后重试。"]
            return result
        result.update(status="warning", fallback_reason=reason)
        result["messages"] = [f"自动选择已回退 CPU：{reason}"]
    try:
        import ctranslate2

        result["compute_types"] = sorted(ctranslate2.get_supported_compute_types("cpu"))
        if "int8" not in result["compute_types"]:
            raise RuntimeError("当前 CTranslate2 不支持 CPU INT8。")
        result.update(resolved_device="cpu", compute_type="int8")
    except Exception as error:
        result.update(ok=False, status="blocked")
        result["messages"].append(f"CPU 推理不可用：{error}")
    return result


if __name__ == "__main__" and sys.argv[1:] == ["--probe"]:
    print(json.dumps(_probe_in_child(), ensure_ascii=True))
