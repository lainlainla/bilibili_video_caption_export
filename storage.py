"""Save complete transcripts without overwriting an existing user file."""

from pathlib import Path
import re
from time import sleep


MAX_SAVE_ATTEMPTS = 3


def transcript_filename(title):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .")[:100]
    return f"transcript_{title or 'video'}"


def _write_new_file(directory, stem, text):
    for number in range(10000):
        suffix = f"_{number}" if number else ""
        path = directory / f"{stem}{suffix}.txt"
        try:
            stream = path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            continue
        try:
            with stream:
                stream.write(text.rstrip("\r\n") + "\n")
        except OSError:
            # This path was exclusively created by this attempt.
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return path
    raise OSError("同名文字稿过多，请更换保存目录或标题。")


def save_transcript(text, title, output_dir, *, filename=None):
    """At most three total write attempts; never repeat transcription."""
    directory = Path(output_dir).expanduser()
    stem = transcript_filename(title)
    if filename is not None:
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).stem).strip(" .")[:100] or "transcript"
        if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            stem = "_" + stem
    error_message = ""
    for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
        try:
            directory = directory.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            path = _write_new_file(directory, stem, text)
            return {"saved": True, "path": str(path), "attempts": attempt, "error": None}
        except OSError as error:
            error_message = str(error)
            if attempt < MAX_SAVE_ATTEMPTS:
                sleep(0.2 * attempt)
    return {"saved": False, "path": None, "attempts": MAX_SAVE_ATTEMPTS,
            "error": f"自动保存失败（已尝试 3 次）：{error_message}。文字稿仍可在页面复制或下载。"}
