#!/bin/sh
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYEXE=$(command -v python3 || command -v python)
if [ -z "$PYEXE" ]; then
    echo '{"output":{"out":"","err":"Python not found","code":1,"cwd":""}}'
    exit 1
fi

PORT_FILE="$DIR/.server_port"

# Ensure server.py is running
PORT=""
if [ -f "$PORT_FILE" ]; then
    PORT=$(cat "$PORT_FILE" 2>/dev/null)
fi
SERVER_UP=false
if [ -n "$PORT" ]; then
    if $PYEXE -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/hello', timeout=2)" 2>/dev/null; then
        SERVER_UP=true
    fi
fi

if [ "$SERVER_UP" = false ]; then
    nohup "$PYEXE" "$DIR/server.py" >/dev/null 2>&1 &
    for i in $(seq 1 30); do
        if [ -f "$PORT_FILE" ]; then
            P2=$(cat "$PORT_FILE" 2>/dev/null)
            if [ -n "$P2" ] && $PYEXE -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$P2/hello', timeout=2)" 2>/dev/null; then
                PORT="$P2"
                SERVER_UP=true
                break
            fi
        fi
        sleep 0.2
    done
fi

# Read JSON input from stdin (from Stash)
INPUT_STR=$(cat)

# Handle 'hello' directly
ACTION=$(echo "$INPUT_STR" | $PYEXE -c "import sys,json; d=json.load(sys.stdin); d=d.get('args') or d; print(d.get('action',''))" 2>/dev/null)

if [ "$ACTION" = "hello" ]; then
    $PYEXE -c "
import json, os
port = int(open('$PORT_FILE').read().strip()) if os.path.exists('$PORT_FILE') else 0
print(json.dumps({'output': {'cwd': os.getcwd(), 'user': os.environ.get('USER','?'), 'host': os.uname().nodename, 'os': os.uname().sysname + ' ' + os.uname().release, 'platform': os.uname().sysname, 'python': '$PYEXE', 'sep': '/', 'drives': ['/'], 'server_port': port}}))
" 2>/dev/null
    exit 0
fi

# Forward 'exec' and other actions
if [ "$ACTION" = "exec" ]; then
    if [ "$SERVER_UP" = true ] && [ -n "$PORT" ]; then
        # Map Stash format -> server format {cmd}
        SERVER_BODY=$(echo "$INPUT_STR" | $PYEXE -c "
import sys, json
d = json.load(sys.stdin)
d = d.get('args') or d
if d.get('action') == 'exec':
    print(json.dumps({'cmd': d.get('cmd','')}, separators=(',',':')))
else:
    print(json.dumps({'cmd': ''}, separators=(',',':')))
" 2>/dev/null)
        RESP=$($PYEXE -c "
import urllib.request, json
data = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
req = urllib.request.Request('http://127.0.0.1:$PORT/execute', data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(req, timeout=600) as r:
    print(r.read().decode())
" "$SERVER_BODY" 2>/dev/null)
        if [ -n "$RESP" ]; then
            $PYEXE -c "
import json, os
print(json.dumps({'output': {'out': json.loads(sys.argv[1]) if sys.argv[1] else '', 'err': '', 'code': 0, 'cwd': os.getcwd()}}, separators=(',',':)))
" "$RESP" 2>/dev/null
        else
            echo '{"output":{"out":"","err":"no response from server","code":1,"cwd":""}}'
        fi
    else
        echo '{"output":{"out":"","err":"terminal server not available","code":1,"cwd":""}}'
    fi
else
    # Other actions (list, download, etc.) — forward to app.py
    echo "$INPUT_STR" | $PYEXE "$DIR/app.py"
fi
exit 0
