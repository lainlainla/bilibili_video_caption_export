#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_root"
if [[ ! -x .venv/bin/python ]]; then
    exec bash "$project_root/install.sh" "$@"
fi
export PYTHONUTF8=1
exec .venv/bin/python start.py
