"""Video URL or local media -> Chinese/English TXT, using local Whisper or an API."""

import argparse
import html
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import pysubs2
from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadError


ROOT = Path(__file__).resolve().parent
MODEL_NAME = "small"
DEFAULT_MODEL_DIR = ROOT / "models" / MODEL_NAME


def language_code(tag):
    return (tag or "").lower().removeprefix("ai-").split("-")[0]


def subtitle_text(data, extension):
    """Remove markup and only deduplicate words in overlapping rolling cues."""
    lines = []
    previous_words, previous_end = [], -1
    for cue in pysubs2.SSAFile.from_string(data, format_=extension):
        words = html.unescape(cue.plaintext).split()
        original_words = words
        if cue.start < previous_end:
            for count in range(min(len(previous_words), len(words)), 0, -1):
                if previous_words[-count:] == words[:count]:
                    words = words[count:]
                    break
        if words:
            lines.append(" ".join(words))
        previous_words, previous_end = original_words, cue.end
    return "\n".join(lines)


def get_subtitles(ydl, info, language):
    target = language if language != "auto" else language_code(info.get("language"))
    tracks = []
    for automatic, field in enumerate(("subtitles", "automatic_captions")):
        for tag, formats in (info.get(field) or {}).items():
            code = language_code(tag)
            if code not in ("zh", "en") or (target and code != target):
                continue
            for track in formats:
                if track.get("ext") not in ("srt", "vtt"):
                    continue
                # YouTube exposes machine translations alongside original captions.
                if "tlang" in parse_qs(urlparse(track.get("url", "")).query):
                    continue
                tracks.append((bool(automatic or tag.startswith("ai-")), tag, track))

    for _, tag, track in sorted(tracks, key=lambda item: item[0]):
        try:
            data = track.get("data")
            if data is None:
                headers = {**(info.get("http_headers") or {}), **track.get("http_headers", {})}
                with ydl.urlopen(Request(track["url"], headers=headers)) as response:
                    data = response.read().decode("utf-8-sig")
            text = subtitle_text(data, track["ext"])
            if text.strip():
                print(f"使用站内字幕：{tag}", file=sys.stderr)
                return text
        except Exception as error:
            # A failed subtitle track must not prevent downloading accessible audio.
            print(f"字幕 {tag} 读取失败：{error}", file=sys.stderr)
    return None


def single_video(info):
    if info and info.get("_type") in ("playlist", "multi_video"):
        entries = iter(info.get("entries") or ())
        first = next(entries, None)
        if first and next(entries, None) is None:
            return single_video(first)
        info = None
    if not info:
        raise ValueError("请输入单个视频页面链接；B站分P请在链接中指定 ?p=页码。")
    return info


def fetch_video(url, directory, language, cookies, force_asr):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(directory / "audio.%(ext)s"),
        "noplaylist": True,
        "lazy_playlist": True,
        "quiet": True,
        "retries": 3,
        "socket_timeout": 20,
        "fixup": "never",
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if cookies:
        options["cookiefile"] = str(cookies)

    info = None
    if not force_asr:
        print("查询视频字幕……", file=sys.stderr)
        with YoutubeDL({**options, "writesubtitles": True, "writeautomaticsub": True}) as ydl:
            try:
                info = single_video(ydl.extract_info(url, download=False))
                text = get_subtitles(ydl, info, language)
                if text:
                    return info.get("title") or info["id"], None, text
            except DownloadError as error:
                print(f"字幕查询失败，尝试直接获取音频：{error}", file=sys.stderr)

    with YoutubeDL(options) as ydl:
        # Validate a single video before any media download, including --force-asr.
        if info is None:
            info = single_video(ydl.extract_info(url, download=False))
        print("下载音频（无独立音轨时下载视频）……", file=sys.stderr)
        info = ydl.process_ie_result(info, download=True)
        audio = Path(ydl.prepare_filename(info))
    if not audio.is_file():
        raise FileNotFoundError(f"下载后未找到媒体文件：{audio}")
    return info.get("title") or info["id"], audio, None


def prepare_model(directory, model_name=MODEL_NAME, *, device="auto"):
    from model_manager import prepare_model as prepare_local_model

    directory = Path(directory)
    return prepare_local_model(model_name, directory.parent, directory=directory, device=device)


def transcribe_audio(audio, model_directory, language, model_name=MODEL_NAME, *, device="auto"):
    from device_manager import configure_cuda_runtime, resolve_device

    selected = resolve_device(device)
    if not selected["ok"]:
        raise RuntimeError(" ".join(selected["messages"]))
    for message in selected["messages"]:
        print(message, file=sys.stderr)
    if selected["resolved_device"] == "cuda":
        configure_cuda_runtime()
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(prepare_model(model_directory, model_name, device=device)),
        device=selected["resolved_device"],
        compute_type=selected["compute_type"],
        local_files_only=True,
    )
    segments, info = model.transcribe(
        str(audio),
        language=None if language == "auto" else language,
        task="transcribe",
        vad_filter=True,
        condition_on_previous_text=False,
    )
    if info.language not in ("zh", "en"):
        raise ValueError(f"检测到语言 {info.language}；仅支持中英文，可用 --language zh 或 en 指定。")
    print(f"本地 {model_name} 转写，语言：{info.language}，{selected['resolved_device'].upper()} / {selected['compute_type']}", file=sys.stderr)
    lines = []
    for segment in segments:
        if segment.text.strip():
            lines.append(segment.text.strip())
        print(f"\r已处理 {segment.end:.1f} / {info.duration:.1f} 秒", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    if not lines:
        raise ValueError("未识别到语音，请检查音轨或用 --language 指定语言。")
    return "\n".join(lines)


def _prepare_cli_environment(device):
    """Linux's loader must see optional GPU library paths before Python starts."""
    if sys.platform != "linux" or device == "cpu":
        return
    from device_manager import cuda_environment

    environment = cuda_environment()
    if environment.get("LD_LIBRARY_PATH", "") != os.environ.get("LD_LIBRARY_PATH", ""):
        # cuda_environment de-duplicates paths, so the restarted process will
        # observe an unchanged value and continue instead of restarting again.
        os.execve(sys.executable, [sys.executable, *sys.argv], environment)


def main():
    from api_transcribe import transcribe_api
    from model_manager import MODEL_CATALOG, assess_model, model_path
    from settings import effective_api_key, load_settings
    from storage import save_transcript

    config = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="视频页面 URL，或本地音视频路径")
    parser.add_argument("-o", "--output", type=Path, help="TXT 保存路径；默认 output/视频标题.txt")
    parser.add_argument("--language", choices=("auto", "zh", "en"), default=config["language"])
    parser.add_argument("--backend", choices=("local", "api"), default=config["backend"])
    parser.add_argument("--model", choices=tuple(MODEL_CATALOG), default=config["model"])
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default=config["device"], help="自动选择、CPU 或 NVIDIA CUDA GPU")
    parser.add_argument("--model-root", type=Path, default=Path(config["model_root"]))
    parser.add_argument("--model-dir", type=Path, help="可选，直接指定完整模型目录")
    parser.add_argument("--output-dir", type=Path, default=Path(config["output_dir"]))
    parser.add_argument("--api-base-url", default=config["api_base_url"])
    parser.add_argument("--api-model", default=config["api_model"])
    parser.add_argument("--api-chunk-seconds", type=int, default=config["api_chunk_seconds"])
    parser.add_argument("--check-model", action="store_true", help="仅评估本地模型资源，不下载")
    parser.add_argument("--download-model", action="store_true", help="预先下载所选本地模型")
    parser.add_argument("--cookies", type=Path, help="可选，Netscape 格式的 cookies.txt")
    parser.add_argument("--force-asr", action="store_true", help="忽略站内字幕，强制从音频转写")
    args = parser.parse_args()
    if args.output and args.output.suffix.lower() != ".txt":
        parser.error("输出文件请使用 .txt 后缀。")
    if args.backend == "local" or args.check_model or args.download_model:
        _prepare_cli_environment(args.device)

    args.model_dir = args.model_dir or model_path(args.model, args.model_root)
    if args.check_model:
        import json
        print(json.dumps(assess_model(args.model, args.model_root, directory=args.model_dir, device=args.device),
                         ensure_ascii=False, indent=2))
        if not args.source and not args.download_model:
            return

    if args.download_model:
        print(f"模型已就绪：{prepare_model(args.model_dir, args.model, device=args.device)}")
        if not args.source:
            return
    if not args.source:
        parser.error("请提供视频链接或本地文件；也可单独运行 --download-model。")
    if args.cookies and not args.cookies.is_file():
        parser.error(f"Cookie 文件不存在：{args.cookies}")

    with TemporaryDirectory(prefix="v2t-") as temporary:
        if urlparse(args.source).scheme in ("http", "https"):
            title, audio, text = fetch_video(
                args.source, Path(temporary), args.language, args.cookies, args.force_asr
            )
        else:
            audio = Path(args.source).expanduser()
            if not audio.is_file():
                parser.error(f"本地文件不存在：{audio}")
            title, text = audio.stem, None
        if text is None:
            if args.backend == "api":
                text = transcribe_api(audio, args.language, args.api_base_url, effective_api_key(config),
                                      args.api_model, chunk_seconds=args.api_chunk_seconds)
            else:
                text = transcribe_audio(audio, args.model_dir, args.language, args.model, device=args.device)

    saved = save_transcript(text, title, args.output.parent if args.output else args.output_dir,
                            filename=args.output.name if args.output else None)
    if not saved["saved"]:
        print(text)
        raise OSError(saved["error"])
    print(f"文字稿已保存：{saved['path']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"失败（{type(error).__name__}）：{error}", file=sys.stderr)
        sys.exit(1)
