#!/usr/bin/env bash
# Cross-platform bootstrap for Stash API plugin (Linux / macOS)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

find_python() {
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

PY="$(find_python || true)"
if [ -n "$PY" ]; then
    exec "$PY" "$DIR/app.py"
fi

# Try to install Python via common package managers
echo "Python not found. Attempting to install..." >&2
if command -v apt-get >/dev/null 2>&1; then
    (sudo apt-get update && sudo apt-get install -y python3) >&2 || true
elif command -v dnf >/dev/null 2>&1; then
    (sudo dnf install -y python3) >&2 || true
elif command -v yum >/dev/null 2>&1; then
    (sudo yum install -y python3) >&2 || true
elif command -v brew >/dev/null 2>&1; then
    (brew install python@3.11) >&2 || true
fi

PY="$(find_python || true)"
if [ -n "$PY" ]; then
    exec "$PY" "$DIR/app.py"
fi

echo "ERROR: Python 3 is not installed and could not be installed automatically." >&2
echo "Please install Python 3 (https://www.python.org/downloads/) and re-run," >&2
echo "or set Stash's python_path to your python3 executable." >&2
exit 1