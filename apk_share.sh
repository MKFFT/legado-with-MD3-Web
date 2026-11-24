#!/usr/bin/env bash
# Legado LAN APK share launcher (Git Bash).
# Starts tools/apk_share/apk_share.py with Python 3.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if command -v python >/dev/null 2>&1; then
    PY=python
elif command -v py >/dev/null 2>&1; then
    PY=py
else
    echo "[ERROR] Python 3 not found on PATH." >&2
    echo "        Install Python 3.x, or add python.exe to PATH." >&2
    exit 1
fi

exec "$PY" tools/apk_share/apk_share.py "$@"