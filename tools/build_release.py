"""Build a source-only ZIP from an explicit allowlist; never include local data."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore",
    ".github/workflows/check.yml",
    "LICENSE",
    "README.md",
    "INSTALL.md",
    "TECH_STACK.md",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "requirements-gpu.txt",
    "settings.example.json",
    "app.py",
    "transcribe.py",
    "start.py",
    "settings.py",
    "storage.py",
    "model_manager.py",
    "device_manager.py",
    "gpu_runtime.py",
    "task_manager.py",
    "worker.py",
    "api_transcribe.py",
    "install.bat",
    "install.ps1",
    "install.sh",
    "start.bat",
    "start.sh",
    "tools/build_release.py",
)
# Only source formats are accepted in these directories, never media or settings.
DIRECTORIES = {
    "static": {".css", ".js"},
    "templates": {".html"},
    "tests": {".py"},
}


def source_files():
    paths = {ROOT / name for name in FILES}
    for directory, extensions in DIRECTORIES.items():
        base = ROOT / directory
        if not base.is_dir():
            raise FileNotFoundError(f"Missing source directory: {directory}")
        for path in base.rglob("*"):
            relative = path.relative_to(base)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.is_file() and path.suffix in extensions:
                paths.add(path)
    for path in sorted(paths):
        if not path.is_file():
            raise FileNotFoundError(f"Missing release source: {path.relative_to(ROOT)}")
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Release source must be a regular project file: {path}")
        if any(parent.is_symlink() for parent in path.parents if parent != ROOT and parent.is_relative_to(ROOT)):
            raise ValueError(f"Symlink source directory is not allowed: {path}")
        yield path


def main():
    files = list(source_files())  # Validate everything before creating a ZIP.
    destination = ROOT / "dist" / "v2t-source.zip"
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, f"v2t/{path.relative_to(ROOT).as_posix()}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Built {destination} ({len(files)} source files, {destination.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
