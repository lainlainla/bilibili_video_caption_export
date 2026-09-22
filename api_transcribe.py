"""OpenAI-compatible audio transcription with bounded, temporary WAV chunks.

Decoding is streaming. A chunk contains at most 600 seconds / 19.2 MB of
16 kHz mono PCM, rather than the entire decoded recording. Prefer a short
silence near the end of each chunk; otherwise cut at the limit. Samples are
neither duplicated nor discarded, but a hard cut can affect word recognition
because independent API requests do not share acoustic context.
"""

from contextlib import closing
from email.message import Message
import ipaddress
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from urllib.parse import urlsplit, urlunsplit
import wave

import av
import numpy as np
import requests


SAMPLE_RATE = 16000
MAX_CHUNK_SECONDS = 600
MAX_ATTEMPTS = 3


def _endpoint(base_url):
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("请填写 API Base URL。")
    base_url = base_url.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in base_url):
        raise ValueError("API 地址不能包含空白或控制字符。")
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("API 地址格式无效。") from None
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("API 地址须包含主机名，且不能内嵌用户名或密码。")
    if parsed.query or parsed.fragment or "?" in base_url or "#" in base_url:
        raise ValueError("API Base URL 不能包含查询参数或片段。")
    if port == 0 or "\\" in base_url:
        raise ValueError("API 地址格式无效。")
    local = host.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("远程 API 必须使用 HTTPS；HTTP 仅允许 localhost 或回环 IP。")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/audio/transcriptions", "", ""))


def api_preflight(base_url, api_key, model):
    """Validate configuration locally; never contact a provider or expose keys."""
    messages = []
    try:
        _endpoint(base_url)
    except ValueError as error:
        messages.append(str(error))
    if not isinstance(api_key, str) or not api_key.strip():
        messages.append("请配置 API Key。")
    elif not api_key.isascii() or any(ord(character) < 32 or ord(character) == 127 for character in api_key):
        messages.append("API Key 不能包含控制字符或非 ASCII 字符。")
    if not isinstance(model, str) or not model.strip():
        messages.append("请填写服务商提供的 Model ID。")
    elif any(ord(character) < 32 for character in model):
        messages.append("Model ID 不能包含控制字符。")
    if messages:
        return {"ok": False, "status": "blocked", "messages": messages, "verified": False}
    return {
        "ok": True,
        "status": "warning",
        "messages": ["本地配置检查通过；尚未连接 API，密钥、模型权限、价格和远程可用性未经验证。"],
        "verified": False,
    }


def _cut_samples(buffer, maximum):
    """Choose a midpoint of >=200 ms silence in the last <=5 seconds."""
    window = SAMPLE_RATE // 50  # 20 ms, measured as PCM root-mean-square energy.
    start = maximum - min(5 * SAMPLE_RATE, maximum // 4)
    samples = np.frombuffer(buffer, dtype="<i2", count=maximum)
    count = (maximum - start) // window
    if not count:
        return maximum
    frames = samples[start:start + count * window].reshape(count, window).astype(np.float32)
    quiet = np.sqrt(np.mean(frames * frames, axis=1)) < 300
    run_start = None
    cut = maximum
    for index in range(count + 1):
        if index < count and quiet[index]:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            if index - run_start >= 10:
                cut = start + ((run_start + index) // 2) * window
            run_start = None
    return cut


def _wav_chunks(audio, directory, chunk_seconds):
    maximum = SAMPLE_RATE * chunk_seconds
    buffer = bytearray()
    index = 0

    def write_chunk(sample_count):
        nonlocal index
        index += 1
        path = directory / f"chunk-{index:06d}.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            output.writeframes(buffer[:sample_count * 2])
        del buffer[:sample_count * 2]
        return path

    with av.open(str(audio)) as container:
        if not container.streams.audio:
            raise ValueError("媒体文件不包含音轨。")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                buffer.extend(converted.to_ndarray().astype("<i2", copy=False).tobytes())
                while len(buffer) >= maximum * 2:
                    yield write_chunk(_cut_samples(buffer, maximum))
        for converted in resampler.resample(None):
            buffer.extend(converted.to_ndarray().astype("<i2", copy=False).tobytes())
            while len(buffer) >= maximum * 2:
                yield write_chunk(_cut_samples(buffer, maximum))
    if buffer:
        yield write_chunk(len(buffer) // 2)
    if not index:
        raise ValueError("媒体文件没有可读取的音频样本。")


def _request_chunk(session, endpoint, api_key, model, language, chunk):
    fields = {"model": model.strip(), "response_format": "json"}
    if language != "auto":
        fields["language"] = language
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with chunk.open("rb") as audio:
                response = session.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key.strip()}"},
                    data=fields,
                    files={"file": (chunk.name, audio, "audio/wav")},
                    timeout=(10, 300),
                    allow_redirects=False,
                )
            with response:
                status = response.status_code
                if status in (408, 429) or 500 <= status <= 599:
                    if attempt < MAX_ATTEMPTS:
                        sleep(attempt)
                        continue
                    raise RuntimeError(f"API 临时错误（HTTP {status}），已尝试 {MAX_ATTEMPTS} 次。")
                if 300 <= status <= 399:
                    raise RuntimeError("API 返回重定向；为保护密钥未跟随跳转，请填写最终服务地址。")
                if status in (401, 403):
                    raise RuntimeError(f"API 认证或权限失败（HTTP {status}），请检查密钥和模型权限。")
                if status == 413:
                    raise RuntimeError("API 拒绝文件大小（HTTP 413），请缩短 API 分片时长。")
                if not 200 <= status < 300:
                    raise RuntimeError(f"API 请求失败（HTTP {status}），请检查地址、Model ID 和服务商要求。")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type == "text/plain":
                    # requests assumes ISO-8859-1 for text/* without charset.
                    # Transcription APIs commonly send UTF-8 even when omitted.
                    content_header = Message()
                    content_header["Content-Type"] = response.headers.get("Content-Type", "")
                    if not content_header.get_content_charset():
                        response.encoding = "utf-8"
                    return response.text.strip()
                if content_type and content_type != "application/json" and not content_type.endswith("+json"):
                    raise RuntimeError("API 返回了不支持的内容类型；需要 JSON text 字段或 text/plain。")
                try:
                    payload = response.json()
                except ValueError:
                    raise RuntimeError("API 返回的 JSON 无效。") from None
                if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                    raise RuntimeError("API 响应缺少字符串类型的 text 字段。")
                return payload["text"].strip()
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"API 网络连接失败，已尝试 {MAX_ATTEMPTS} 次；请检查网络和服务地址。") from None
            sleep(attempt)
        except requests.RequestException:
            raise RuntimeError("API 请求无法完成，请检查配置、网络和代理设置。") from None


def transcribe_api(audio: Path, language: str, base_url: str, api_key: str,
                   model: str, chunk_seconds: int = 600) -> str:
    """Transcribe sequential chunks. Retried requests may incur provider charges."""
    assessment = api_preflight(base_url, api_key, model)
    if not assessment["ok"]:
        raise ValueError(" ".join(assessment["messages"]))
    if language not in ("auto", "zh", "en"):
        raise ValueError("仅支持 auto、zh、en 三种语言设置。")
    if type(chunk_seconds) is not int or not 1 <= chunk_seconds <= MAX_CHUNK_SECONDS:
        raise ValueError("API 分片时长须为 1 到 600 之间的整数秒。")
    endpoint = _endpoint(base_url)
    lines = []
    with TemporaryDirectory(prefix="v2t-api-") as temporary, requests.Session() as session:
        # requests can otherwise replace an explicit Authorization header with
        # credentials from .netrc. Keep proxy support but suppress .netrc auth.
        session.auth = lambda request: request
        with closing(_wav_chunks(Path(audio), Path(temporary), chunk_seconds)) as chunks:
            for chunk in chunks:
                try:
                    text = _request_chunk(session, endpoint, api_key, model, language, chunk)
                    if text:
                        lines.append(text)
                finally:
                    chunk.unlink(missing_ok=True)
    if not lines:
        raise ValueError("API 未返回识别文字，请检查音轨或语言设置。")
    return "\n".join(lines)
