#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
check_only=0
no_launch=0
for arg in "$@"; do
    case "$arg" in
        --check) check_only=1 ;;
        --no-launch) no_launch=1 ;;
        *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 1 ;;
    esac
done
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

system="$(uname -s)"
arch="$(uname -m)"
uv_version='0.12.7'
# SHA256 values published on https://github.com/astral-sh/uv/releases/tag/0.12.7
case "$system/$arch" in
    Darwin/arm64)
        mac_version="$(sw_vers -productVersion)"
        [[ "${mac_version%%.*}" -ge 14 ]] || fail 'macOS 14 or newer is required by the bundled dependency versions.'
        target='aarch64-apple-darwin'
        uv_sha256='127ebdda7ad953cdf198e964b570ea5771b85467ea93eb7cb6d6f8e6f55408f3'
        ;;
    Linux/x86_64|Linux/aarch64)
        libc="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
        [[ "$libc" == glibc\ * ]] || fail 'Linux requires glibc; Alpine/musl is not supported by all dependencies.'
        libc_version="${libc#glibc }"
        libc_major="${libc_version%%.*}"
        libc_minor="${libc_version#*.}"
        libc_minor="${libc_minor%%.*}"
        (( libc_major > 2 || (libc_major == 2 && libc_minor >= 28) )) || fail 'Linux requires glibc 2.28 or newer.'
        target="$arch-unknown-linux-gnu"
        if [[ "$arch" == x86_64 ]]; then
            uv_sha256='788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21'
        else
            uv_sha256='66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c'
        fi
        ;;
    *) fail 'Supported: macOS 14+ Apple Silicon, Linux x86_64/aarch64 with glibc 2.28+. On Windows use install.bat. macOS Intel is not supported by the locked ONNX Runtime version.' ;;
esac
for command in curl tar tee; do
    command -v "$command" >/dev/null || fail "Required system utility not found: $command"
done
if command -v sha256sum >/dev/null; then
    checksum() { sha256sum "$1" | cut -d ' ' -f 1; }
elif command -v shasum >/dev/null; then
    checksum() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
else
    fail 'A SHA256 utility (sha256sum or shasum) is required.'
fi
for file in pyproject.toml uv.lock start.py; do
    [[ -f "$project_root/$file" ]] || fail "Missing $file. Extract the complete source ZIP first."
done
venv_dir="$project_root/.venv"
venv_python="$venv_dir/bin/python"
if [[ -e "$venv_dir" ]]; then
    [[ -x "$venv_python" ]] || fail 'An incomplete .venv exists and was preserved. Use a new extraction directory or inspect/move it manually.'
    "$venv_python" -I -S -c 'import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8 else 1)' || fail 'Existing .venv is not a working 64-bit Python 3.12 environment. It was preserved; use a new extraction directory.'
    printf 'Existing Python 3.12 environment will be reused; extra packages will be kept.\n'
fi
printf 'Platform check passed: %s/%s. Project: %s\n' "$system" "$arch" "$project_root"
if (( check_only )); then
    printf 'Check only: no downloads, installation, or application launch performed.\n'
    exit 0
fi

mkdir -p "$project_root/.cache/install"
log_path="$project_root/.cache/install/install-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$log_path") 2>&1
trap 'code=$?; printf "Installation failed (exit %s). Log: %s\n" "$code" "$log_path" >&2; exit "$code"' ERR
printf 'Installation log: %s\n' "$log_path"
cd -- "$project_root"
export PYTHONUTF8=1
export UV_CACHE_DIR="$project_root/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$project_root/.runtime/python"
export UV_PROJECT_ENVIRONMENT="$venv_dir"
export UV_PYTHON_INSTALL_BIN=0
export UV_PYTHON_INSTALL_REGISTRY=0
uv_dir="$project_root/.runtime/uv/$uv_version"
mkdir -p "$uv_dir"
archive="uv-$target.tar.gz"
archive_path="$uv_dir/$archive"
if [[ ! -f "$archive_path" ]] || [[ "$(checksum "$archive_path")" != "$uv_sha256" ]]; then
    curl --fail --location --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 30 --max-time 600 \
        "https://github.com/astral-sh/uv/releases/download/$uv_version/$archive" -o "$archive_path.part"
    [[ "$(checksum "$archive_path.part")" == "$uv_sha256" ]] || fail 'Downloaded uv archive failed SHA256 verification.'
    mv -- "$archive_path.part" "$archive_path"
fi
uv_path="$uv_dir/uv"
if [[ ! -x "$uv_path" ]]; then
    tar -xzf "$archive_path" -C "$uv_dir" --strip-components=1
fi
"$uv_path" --version
if [[ ! -e "$venv_dir" ]]; then
    "$uv_path" python install 3.12 --no-bin --no-registry --no-config
    "$uv_path" venv "$venv_dir" --python 3.12 --managed-python --no-config
fi
"$uv_path" sync --locked --inexact --no-install-project --no-dev --no-build --no-config \
    --no-python-downloads --python "$venv_python" --project "$project_root"
printf 'Installation complete. Model files are downloaded only when a local model is selected and needed.\n'
trap - ERR
if (( ! no_launch )); then
    "$venv_python" "$project_root/start.py"
fi
