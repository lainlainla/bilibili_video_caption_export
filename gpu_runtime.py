"""Install optional NVIDIA wheels in a task sandbox, then publish on success."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime" / "gpu" / "cu12-v1"
REQUIREMENTS = ROOT / "requirements-gpu.txt"


def _fingerprint():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _complete(directory):
    marker = directory / "v2t-runtime.json"
    try:
        if json.loads(marker.read_text(encoding="utf-8"))["requirements_sha256"] != _fingerprint():
            return False
        filenames = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll") if os.name == "nt" else (
            "libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9")
        return all(any(path.stat().st_size > 0 for path in directory.rglob(name)) for name in filenames)
    except (OSError, ValueError, KeyError):
        return False


def _installer():
    binary = "uv.exe" if os.name == "nt" else "uv"
    bundled = ROOT / ".runtime" / "uv" / "0.12.7" / binary
    if not bundled.is_file():
        bundled = ROOT / ".runtime" / "uv" / "0.12.7" / "uv-x86_64-unknown-linux-gnu" / binary
    uv = str(bundled) if bundled.is_file() else shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--no-config", "--no-cache", "--python", sys.executable,
                "--default-index", "https://pypi.org/simple"]
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip", "--isolated", "install", "--no-cache-dir",
                "--index-url", "https://pypi.org/simple"]
    raise RuntimeError("未找到项目安装器；请先运行 install.bat 或 bash install.sh，再安装 GPU 运行库。")


def install_runtime(directory):
    """Called only inside a cancellable worker; never modify the active environment."""
    if platform.system() not in {"Windows", "Linux"} or platform.machine().lower() not in {"amd64", "x86_64", "aarch64"}:
        raise RuntimeError("此 GPU 安装入口仅支持带 NVIDIA 显卡的 Windows x64 / Linux；其他平台请使用 CPU 或 API。")
    if _complete(RUNTIME):
        return {"ok": True, "messages": ["项目内 GPU 运行库已安装，可选择 GPU 后执行模型自检。"]}
    import ctranslate2
    if ctranslate2.get_cuda_device_count() < 1:
        raise RuntimeError("未检测到可用 NVIDIA CUDA 设备；请先安装显卡驱动，或使用 CPU / API。")
    directory = Path(directory)
    if shutil.disk_usage(directory).free < 6 * 1024 ** 3:
        raise RuntimeError("GPU 运行库下载和解压建议至少预留 6 GiB 磁盘空间。")
    staging = directory / "gpu-site"
    command = _installer() + ["--require-hashes", "--only-binary", ":all:", "--target", str(staging),
                              "-r", str(REQUIREMENTS)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    env = dict(os.environ)
    # These package downloads never need transcription credentials.
    for name in ("V2T_API_KEY", "OPENAI_API_KEY"):
        env.pop(name, None)
    with (directory / "gpu-install.log").open("wb") as log:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                env=env, **options)
    if result.returncode:
        raise RuntimeError(f"GPU 运行库安装失败（退出码 {result.returncode}）。请检查 PyPI 网络连接、磁盘空间后重试；当前运行环境未改动。")
    (staging / "v2t-runtime.json").write_text(json.dumps({"requirements_sha256": _fingerprint()}), encoding="utf-8")
    if not _complete(staging):
        raise RuntimeError("下载完成但必要 GPU 动态库不完整；当前运行环境未改动。")
    return {"ok": True, "_runtime_staging": str(staging),
            "messages": ["GPU 运行库已安装到项目私有目录。选择 GPU 并执行模型自检可验证实际推理。"]}


def publish_runtime(result):
    """Parent-process commit point, protected by the same lock as cancellation."""
    result = dict(result)
    staging_path = result.pop("_runtime_staging", None)
    if not staging_path:
        return result
    staging = Path(staging_path).resolve()
    task_root = (ROOT / ".cache" / "tasks").resolve()
    if not staging.is_relative_to(task_root) or staging.name != "gpu-site" or not _complete(staging):
        raise ValueError("GPU 运行库暂存目录无效。")
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    if RUNTIME.exists():
        if _complete(RUNTIME):
            return result
        raise RuntimeError(f"原 GPU 运行库目录不完整，已保留；请检查 {RUNTIME} 后重试。")
    staging.rename(RUNTIME)
    return result
