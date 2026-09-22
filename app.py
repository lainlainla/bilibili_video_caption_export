"""Local web UI with selectable devices and cancellable process-isolated tasks."""

import atexit
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, jsonify, render_template, request

from api_transcribe import api_preflight
from model_manager import MODEL_CATALOG, assess_model
from settings import ROOT, effective_api_key, load_settings, public_settings, save_settings, validate_settings
from storage import save_transcript
from task_manager import TaskBusyError, TaskManager
from worker import finalize_result, refresh_cuda_environment, run_task


app = Flask(__name__)
MAX_UPLOAD = 1024 ** 3
app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD + 1024 ** 2,
                  TRUSTED_HOSTS=["127.0.0.1", "localhost"],
                  LOCAL_TOKEN=secrets.token_urlsafe(32))
refresh_cuda_environment()
tasks = TaskManager(run_task, finalize_result, temporary_parent=ROOT / ".cache" / "tasks")
atexit.register(tasks.close)


def normalize_video_url(value):
    """Accept pasted domains without changing short-link or subdomain hosts."""
    url = value.strip()
    invalid = "请输入有效的视频链接，例如 bilibili.com/video/BV… 。"
    if not url or any(character.isspace() for character in url):
        raise ValueError(invalid)
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        # A domain with a numeric port is not a URI scheme.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) and not re.match(r"^[^/:]+:\d+(?:[/#?]|$)", url):
            raise ValueError(invalid)
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(invalid)
        parsed.port  # Reject invalid and out-of-range ports before starting work.
    except ValueError:
        raise ValueError(invalid) from None
    if parsed.hostname in {"bilibili.com", "youtube.com"}:
        # www is a hostname component, not a universal URL prefix.
        userinfo, separator, host = parsed.netloc.rpartition("@")
        parsed = parsed._replace(netloc=(userinfo + separator if separator else "") + "www." + host)
    return urlunsplit(parsed)


@app.before_request
def protect_local_writes():
    if request.path.startswith("/api/") and request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="请从本机页面发起操作。"), 403
        token = request.headers.get("X-V2T-Token", "")
        if not secrets.compare_digest(token, app.config["LOCAL_TOKEN"]):
            return jsonify(error="页面连接已过期，请刷新后再试。"), 403


@app.get("/")
def index():
    return render_template("index.html", local_token=app.config["LOCAL_TOKEN"])


@app.get("/api/settings")
def get_settings():
    models = [{"id": name, "label": item.get("label", name)} for name, item in MODEL_CATALOG.items()]
    return jsonify(settings=public_settings(load_settings()), models=models)


@app.post("/api/settings")
def update_settings():
    return jsonify(settings=public_settings(save_settings(request.get_json())))


def request_config():
    return validate_settings(request.get_json(), load_settings())


@app.post("/api/assess")
def assess():
    config = request_config()
    if config["backend"] == "api":
        report = api_preflight(config["api_base_url"], effective_api_key(config), config["api_model"])
    else:
        report = assess_model(config["model"], Path(config["model_root"]), device=config["device"])
    return jsonify(report)


@app.post("/api/models/prepare")
def prepare():
    config = request_config()
    if config["backend"] != "local":
        return jsonify(error="API 模式不需要下载本地 Whisper 权重。"), 400
    return jsonify(tasks.start("prepare", config)), 202


@app.post("/api/runtime/install")
def install_gpu():
    config = request_config()
    if config["backend"] != "local":
        return jsonify(error="API 模式不需要安装 GPU 运行库。"), 400
    return jsonify(tasks.start("runtime_install", config)), 202


@app.get("/api/tasks/current")
def current_task():
    return jsonify(task=tasks.current())


@app.get("/api/tasks/<task_id>")
def task_status(task_id):
    try:
        return jsonify(tasks.get(task_id))
    except KeyError:
        return jsonify(error="任务不存在或服务已重启，请刷新页面。"), 404


@app.post("/api/tasks/<task_id>/cancel")
def cancel_task(task_id):
    try:
        return jsonify(tasks.cancel(task_id))
    except KeyError:
        return jsonify(error="任务不存在或服务已重启，请刷新页面。"), 404


@app.get("/api/queue")
def queue_status():
    return jsonify(tasks.queue_state())


@app.post("/api/queue/start")
def start_queue():
    return jsonify(tasks.start_queue())


@app.post("/api/queue/stop")
def stop_queue():
    return jsonify(tasks.stop_queue())


@app.post("/api/queue/<task_id>/move")
def move_queued(task_id):
    data = request.get_json()
    if not isinstance(data, dict) or data.get("direction") not in ("up", "down"):
        raise ValueError("请选择上移或下移。")
    try:
        return jsonify(tasks.move_queued(task_id, data["direction"]))
    except KeyError:
        return jsonify(error="队列项不存在，可能已被移除或服务已重启。"), 404


@app.post("/api/queue/<task_id>/remove")
def remove_queued(task_id):
    try:
        return jsonify(tasks.remove_queued(task_id))
    except KeyError:
        return jsonify(error="队列项不存在，可能已被移除或服务已重启。"), 404


def transcription_request():
    """Capture configuration and copy request-owned uploads before returning."""
    config = load_settings()
    config["api_key"] = effective_api_key(config)
    language = request.form.get("language", config["language"])
    source_type = request.form.get("source_type", "url")
    if language not in ("auto", "zh", "en"):
        raise ValueError("请选择自动识别、中文或英文。")
    if source_type not in ("url", "file"):
        raise ValueError("请选择视频链接或本地文件。")
    url = request.form.get("url", "").strip()
    if source_type == "url":
        url = normalize_video_url(url)
    media = request.files.get("media")
    if source_type == "file" and (not media or not media.filename):
        raise ValueError("请选择一个音频或视频文件。")
    force_asr = request.form.get("force_asr") == "true"
    uploaded_cookies = request.files.get("cookies")

    def setup(directory):
        payload = {"language": language, "source_type": source_type, "url": url,
                   "force_asr": force_asr, "cookies": None}
        if uploaded_cookies and uploaded_cookies.filename:
            data = uploaded_cookies.stream.read(1024 ** 2 + 1)
            if len(data) > 1024 ** 2:
                raise ValueError("Cookie 文件不能超过 1 MiB。")
            cookies = directory / "cookies.txt"
            cookies.write_bytes(data)
            payload["cookies"] = str(cookies)
        if source_type == "file":
            audio = directory / ("media" + Path(media.filename).suffix)
            media.save(audio)
            if not 0 < audio.stat().st_size <= MAX_UPLOAD:
                raise ValueError("请选择大小在 0 到 1 GiB 之间的音视频文件。")
            payload.update(title=Path(media.filename).stem, audio=str(audio))
        return payload

    return config, setup, url if source_type == "url" else Path(media.filename).name


@app.post("/api/transcribe")
def transcribe():
    config, setup, _ = transcription_request()
    return jsonify(tasks.start("transcribe", config, setup)), 202


@app.post("/api/queue")
def enqueue_transcription():
    config, setup, label = transcription_request()
    return jsonify(tasks.enqueue("transcribe", config, setup, label=label)), 202


@app.post("/api/save")
def save_edited():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
        return jsonify(error="没有可保存的文字。"), 400
    if not isinstance(data.get("title", "文字稿"), str) or len(data["text"].encode("utf-8")) > 10 * 1024 ** 2:
        return jsonify(error="文字稿过大或标题无效。"), 400
    result = save_transcript(data["text"], data.get("title", "文字稿"), load_settings()["output_dir"])
    return jsonify(save=result), 200 if result["saved"] else 422


@app.errorhandler(TaskBusyError)
def task_busy(error):
    return jsonify(error=str(error)), 409


@app.errorhandler(ValueError)
def invalid_value(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(OSError)
def filesystem_error(error):
    return jsonify(error=f"本地文件操作失败：{error}"), 422


@app.errorhandler(RuntimeError)
def task_start_error(error):
    return jsonify(error=str(error)), 422


@app.errorhandler(413)
def too_large(error):
    return jsonify(error="文件过大，请选择不超过 1 GiB 的音视频文件。"), 413


if __name__ == "__main__":
    print("打开 http://127.0.0.1:7860 使用视频转文字。", flush=True)
    app.run(host="127.0.0.1", port=7860, debug=False)
