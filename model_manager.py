"""Choose, estimate, download, and smoke-test local multilingual Whisper models."""

from pathlib import Path
import os
import platform
import shutil
import struct
from time import perf_counter, sleep

from device_manager import configure_cuda_runtime, resolve_device


DEFAULT_MODEL = "small"
GIB = 1024 ** 3
REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")
MAX_DOWNLOAD_ATTEMPTS = 3

# Download sizes are approximate decimal GB. Memory limits are conservative
# application estimates in GiB for CPU INT8, not measured model requirements.
MODEL_CATALOG = {
    "tiny": {"label": "Tiny", "parameters_m": 39, "download_gb": 0.08,
             "minimum_ram_gb": 0.5, "recommended_ram_gb": 1.0},
    "base": {"label": "Base", "parameters_m": 74, "download_gb": 0.15,
             "minimum_ram_gb": 0.75, "recommended_ram_gb": 1.5},
    "small": {"label": "Small", "parameters_m": 244, "download_gb": 0.5,
              "minimum_ram_gb": 1.25, "recommended_ram_gb": 2.5},
    "medium": {"label": "Medium", "parameters_m": 769, "download_gb": 1.6,
               "minimum_ram_gb": 2.75, "recommended_ram_gb": 5.0},
    "large-v3": {"label": "Large v3", "parameters_m": 1550, "download_gb": 3.1,
                 "minimum_ram_gb": 4.5, "recommended_ram_gb": 8.0},
    "turbo": {"label": "Turbo", "parameters_m": 809, "download_gb": 1.7,
              "minimum_ram_gb": 3.0, "recommended_ram_gb": 6.0},
}

# CUDA INT8_FLOAT16 estimates in GiB; not measured guarantees or maximum input sizes.
GPU_MEMORY = {
    "tiny": (0.5, 1.0), "base": (0.75, 1.5), "small": (1.0, 2.0),
    "medium": (2.0, 4.0), "large-v3": (3.5, 6.0), "turbo": (2.5, 4.5),
}


def model_path(model_name, model_root, *, directory=None):
    if model_name not in MODEL_CATALOG:
        raise ValueError(f"不支持的本地模型：{model_name}。请选择 {', '.join(MODEL_CATALOG)}。")
    if directory is not None:
        return Path(directory).expanduser().resolve()
    return Path(model_root).expanduser().resolve() / model_name


def model_ready(model_name, model_root, *, directory=None):
    """Check nonempty required files; loading is checked separately by verify_model."""
    directory = model_path(model_name, model_root, directory=directory)
    try:
        return all((directory / name).is_file() and (directory / name).stat().st_size > 0
                   for name in REQUIRED_FILES)
    except OSError:
        return False


def _available_memory():
    import psutil

    return psutil.virtual_memory().available


def _disk_free(directory):
    """Use the nearest existing parent so an uncreated custom path can be assessed."""
    current = directory
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise OSError("找不到可访问的父目录。")
        current = parent
    if not current.is_dir():
        raise OSError(f"路径被文件占用：{current}")
    return shutil.disk_usage(current).free


def assess_model(model_name, model_root, *, directory=None, device="auto"):
    directory = model_path(model_name, model_root, directory=directory)
    spec = MODEL_CATALOG[model_name]
    downloaded = model_ready(model_name, model_root, directory=directory)
    device_report = resolve_device(device)
    result = {
        **device_report,
        "ok": True, "status": "ready", "model": model_name,
        "model_dir": str(directory), "downloaded": downloaded, "messages": [],
        "estimate_only": True, "architecture": platform.machine(),
        "cpu_threads": os.cpu_count() or 1,
        "available_ram_gb": None, "free_disk_gb": None,
        "minimum_ram_gb": spec["minimum_ram_gb"],
        "recommended_ram_gb": spec["recommended_ram_gb"],
        "download_gb": spec["download_gb"], "recommended_model": None,
    }
    blocked, warnings = [], []
    resource_pressure = False
    if not device_report["ok"]:
        blocked.extend(device_report["messages"])
    elif device_report["status"] == "warning":
        warnings.extend(device_report["messages"])
    if struct.calcsize("P") != 8 or result["architecture"].lower() not in {
        "amd64", "x86_64", "arm64", "aarch64",
    }:
        blocked.append("当前 CPU 架构或 Python 位数不在本项目支持范围；请使用 64 位 x86/ARM 环境，或选择 API 模式。")
    available = None
    try:
        available = _available_memory() / GIB
        result["available_ram_gb"] = round(available, 2)
        resource_pressure = available < spec["recommended_ram_gb"]
        if available < spec["minimum_ram_gb"]:
            blocked.append(f"当前可用内存 {available:.2f} GiB，低于本应用对 {model_name} 的保守下限估算 {spec['minimum_ram_gb']} GiB。请关闭其他程序或选择更小模型。")
        elif available < spec["recommended_ram_gb"]:
            warnings.append(f"当前可用内存 {available:.2f} GiB，低于建议预留的 {spec['recommended_ram_gb']} GiB；短音频可能可用，长音频可能内存不足。")
    except Exception as error:
        blocked.append(f"无法读取可用内存：{error}")

    vram = device_report.get("free_vram_gb")
    if device_report["resolved_device"] == "cuda":
        minimum, recommended = GPU_MEMORY[model_name]
        result.update(minimum_vram_gb=minimum, recommended_vram_gb=recommended)
        resource_pressure = resource_pressure or (vram is not None and vram < recommended)
        if vram is not None and vram < minimum:
            blocked.append(f"当前可用显存 {vram:.2f} GiB，低于 {model_name} 的保守下限估算 {minimum} GiB；请关闭其他 GPU 程序、选择更小模型或改用 CPU。")
        elif vram is not None and vram < recommended:
            warnings.append(f"当前可用显存 {vram:.2f} GiB，低于建议预留的 {recommended} GiB；请先做短音频自检。")

    required_bytes = 0 if downloaded else int(spec["download_gb"] * 1_000_000_000) + 256 * 1024 ** 2
    result["required_disk_gb"] = round(required_bytes / GIB, 2)
    free = None
    try:
        free = _disk_free(directory)
        result["free_disk_gb"] = round(free / GIB, 2)
        if free < required_bytes:
            resource_pressure = True
            blocked.append(f"模型目录所在磁盘剩余 {free / GIB:.2f} GiB，下载及临时文件预计至少需要 {required_bytes / GIB:.2f} GiB。请更换模型目录或选择更小模型。")
    except OSError as error:
        blocked.append(f"模型目录无法使用：{error}")

    if resource_pressure and available is not None and free is not None:
        # Offer a strictly smaller model that fits the current memory and disk.
        for name, candidate in reversed(list(MODEL_CATALOG.items())):
            if candidate["parameters_m"] >= spec["parameters_m"]:
                continue
            if device_report["resolved_device"] == "cuda" and vram is not None and vram < GPU_MEMORY[name][1]:
                continue
            needed = 0 if model_ready(name, model_root) else candidate["download_gb"] * 1_000_000_000 + 256 * 1024 ** 2
            if available >= candidate["recommended_ram_gb"] and free >= needed:
                result["recommended_model"] = name
                break
    if result["recommended_model"]:
        warnings.append(f"可尝试较小的 {result['recommended_model']} 模型。")
    result["messages"] = blocked + warnings + [
        f"使用设备：{device_report['resolved_device'] or '不可用'} / {device_report['compute_type'] or '未确定'}。",
        "以上内存、显存和下载空间为估算；实际耗时取决于设备、音频长度和内容，未测量前不预测转写速度。完整音轨解码也会占用内存。",
        "必要模型文件已存在，尚需实测验证可加载。" if downloaded else "所选模型尚未下载，只会按需下载此档位。",
    ]
    result["status"] = "blocked" if blocked else "warning" if warnings else "ready"
    result["ok"] = not blocked
    return result


def prepare_model(model_name, model_root, *, directory=None, device="auto"):
    """Use root/model by default, or the exact legacy --model-dir when supplied."""
    directory = model_path(model_name, model_root, directory=directory)
    assessment = assess_model(model_name, model_root, directory=directory, device=device)
    if not assessment["ok"]:
        raise RuntimeError("本地模型检查未通过：" + " ".join(assessment["messages"]))
    if assessment["downloaded"]:
        return directory

    from faster_whisper.utils import download_model

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"无法创建模型目录 {directory}：{error}") from error
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            download_model(model_name, output_dir=str(directory))
            if not model_ready(model_name, model_root, directory=directory):
                raise RuntimeError("下载结束后仍缺少必要模型文件，或文件为空。")
            return directory
        except Exception as error:
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                raise RuntimeError(f"下载 {model_name} 失败，已尝试 {MAX_DOWNLOAD_ATTEMPTS} 次：{error}。检查网络后可再次尝试，已下载部分会保留。") from error
            sleep(attempt)


def verify_model(model_name, model_root, *, directory=None, device="auto"):
    """Run real encode/decode on a short generated tone; never claim ASR accuracy."""
    started = perf_counter()
    result = {"ok": False, "status": "blocked", "model": model_name,
              "verified": False, "messages": [], "audio_seconds": 1.0}
    try:
        device_report = resolve_device(device)
        result.update(device_report)
        result.update(ok=False, status="blocked", verified=False)
        if not device_report["ok"]:
            raise RuntimeError(" ".join(device_report["messages"]))
        directory = prepare_model(model_name, model_root, directory=directory, device=device)
        if device_report["resolved_device"] == "cuda":
            configure_cuda_runtime()
        from faster_whisper import WhisperModel
        import numpy as np

        load_started = perf_counter()
        model = WhisperModel(str(directory), device=device_report["resolved_device"],
                             compute_type=device_report["compute_type"], local_files_only=True)
        result["load_seconds"] = round(perf_counter() - load_started, 3)
        audio = (0.01 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
        inference_started = perf_counter()
        segments, _ = model.transcribe(
            audio, language="en", task="transcribe", vad_filter=False,
            beam_size=1, best_of=1, temperature=0, max_new_tokens=8,
            without_timestamps=True, condition_on_previous_text=False,
        )
        list(segments)  # The generator must be consumed to actually run inference.
        result["inference_seconds"] = round(perf_counter() - inference_started, 3)
        result.update(ok=True, status="ready", verified=True, model_dir=str(directory))
        result["messages"] = [
            *device_report["messages"],
            f"已在本机 {device_report['resolved_device'].upper()} / {device_report['compute_type']} 完成模型加载及 1 秒合成音频的实际推理。",
            "此结果只验证短输入的运行能力，不代表语音识别准确率，也不保证长视频内存或转写速度。",
        ]
    except Exception as error:
        result["error"] = str(error)
        result["messages"] = [f"本地模型实测未通过：{error}", "可释放内存、更换较小模型，或改用 API 模式。"]
    result["seconds"] = round(perf_counter() - started, 3)
    return result
