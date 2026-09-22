"""Per-user settings; API keys are never returned to the browser."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "settings.local.json"
SETTINGS_LOCK = Lock()
DEFAULTS = {
    "backend": "local",
    "model": "small",
    "device": "auto",
    "model_root": str(ROOT / "models"),
    "output_dir": str(ROOT / "output"),
    "language": "auto",
    "api_base_url": "",
    "api_model": "",
    "api_key": "",
    "api_chunk_seconds": 600,
}


def directory_path(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("请填写有效的本地目录路径。")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"路径是文件，请选择目录：{path}")
    return path


def validate_settings(data, current=None):
    from model_manager import MODEL_CATALOG

    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象。")
    unknown = set(data) - set(DEFAULTS) - {"clear_api_key"}
    if unknown:
        raise ValueError("配置包含不支持的字段。")
    result = {**DEFAULTS, **(current or {})}
    for key, value in data.items():
        if key == "clear_api_key":
            continue
        if key == "api_key" and value == "":
            continue  # An empty password input keeps an existing saved key.
        result[key] = value
    if data.get("clear_api_key") is True:
        result["api_key"] = ""
    if result["backend"] not in ("local", "api"):
        raise ValueError("请选择本地模型或 API。")
    if not isinstance(result["model"], str) or result["model"] not in MODEL_CATALOG:
        raise ValueError("请选择受支持的多语言 Whisper 模型。")
    if result["device"] not in ("auto", "cpu", "cuda"):
        raise ValueError("计算设备须为 auto、cpu 或 cuda。")
    if result["language"] not in ("auto", "zh", "en"):
        raise ValueError("仅支持自动识别、中文和英文。")
    for key in ("model_root", "output_dir"):
        result[key] = str(directory_path(result[key]))
    for key in ("api_base_url", "api_model", "api_key"):
        if not isinstance(result[key], str):
            raise ValueError("API 地址、模型和密钥必须是文字。")
        result[key] = result[key].strip()
        if "\r" in result[key] or "\n" in result[key]:
            raise ValueError("API 配置不能包含换行。")
    seconds = result["api_chunk_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 600:
        raise ValueError("API 分片长度应为 1–600 秒的整数。")
    return result


def load_settings():
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("本地配置无法读取；请检查 settings.local.json，原文件未被改写。") from error
    return validate_settings(data)


def effective_api_key(config):
    return config.get("api_key") or os.environ.get("V2T_API_KEY") or os.environ.get("OPENAI_API_KEY", "")


def public_settings(config):
    return {**{key: value for key, value in config.items() if key != "api_key"},
            "api_key_set": bool(effective_api_key(config)),
            "configured": SETTINGS_PATH.is_file()}


def save_settings(data):
    with SETTINGS_LOCK:
        result = validate_settings(data, load_settings())
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent,
                                    prefix=".settings-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.chmod(0o600)
            temporary.replace(SETTINGS_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return result
