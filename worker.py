"""Task computation in a disposable process; final output is committed by its parent."""

import os
from pathlib import Path

from gpu_runtime import install_runtime, publish_runtime
from model_manager import model_path, verify_model
from settings import effective_api_key
from storage import save_transcript


def refresh_cuda_environment():
    from device_manager import cuda_environment

    environment = cuda_environment()
    for key in ("PATH", "LD_LIBRARY_PATH"):
        if key in environment:
            os.environ[key] = environment[key]


def run_task(kind, config, payload, directory):
    if kind == "runtime_install":
        return {**install_runtime(directory), "_kind": kind}
    if kind == "prepare":
        return {**verify_model(config["model"], Path(config["model_root"]),
                               device=config["device"]), "_kind": kind}
    if kind != "transcribe":
        raise ValueError("未知任务类型。")

    from api_transcribe import transcribe_api
    from transcribe import fetch_video, transcribe_audio

    language = payload["language"]
    if payload["source_type"] == "url":
        cookies = Path(payload["cookies"]) if payload.get("cookies") else None
        title, audio, text = fetch_video(payload["url"], directory, language, cookies, payload["force_asr"])
    else:
        title, audio, text = payload["title"], Path(payload["audio"]), None

    method, device = "站内字幕", None
    if text is None:
        if config["backend"] == "api":
            method = f"API · {config['api_model']}"
            text = transcribe_api(audio, language, config["api_base_url"], effective_api_key(config),
                                  config["api_model"], chunk_seconds=config["api_chunk_seconds"])
        else:
            from device_manager import resolve_device

            device = resolve_device(config["device"])
            if not device["ok"]:
                raise RuntimeError(" ".join(device["messages"]))
            selected = device["resolved_device"]
            method = f"本地 {config['model']} · {'GPU' if selected == 'cuda' else 'CPU'} {device['compute_type'].upper()}"
            text = transcribe_audio(audio, model_path(config["model"], config["model_root"]),
                                    language, model_name=config["model"], device=selected)
    return {"_kind": kind, "title": title, "text": text, "method": method, "device": device}


def finalize_result(result, config):
    """Runs under TaskManager's state lock, so cancellation cannot publish a result."""
    result = dict(result)
    kind = result.pop("_kind", None)
    if kind == "transcribe":
        result["save"] = save_transcript(result["text"], result["title"], config["output_dir"])
    elif kind == "runtime_install":
        result = publish_runtime(result)
        refresh_cuda_environment()
    return result
