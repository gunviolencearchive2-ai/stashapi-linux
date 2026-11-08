#!/bin/sh
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYEXE=$(command -v python3 || command -v python)
if [ -z "$PYEXE" ]; then
    echo '{"output":{"out":"","err":"Python not found (apt install python3)","code":1,"cwd":""}}'
    exit 1
fi

# All actions (hello, exec, list, download, ...) are handled by app.py:
# one process per request, no background servers, no extra ports.
# app.py reads the request JSON from stdin itself.
$PYEXE "$DIR/app.py"
