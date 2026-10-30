#!/usr/bin/env python3
"""Background HTTP terminal server for Stash API plugin.

Starts a persistent shell (cmd.exe /K on Windows, bash on Linux) and
streams command output to the browser in real time via chunked HTTP.
"""
import http.server
import socketserver
import subprocess
import threading
import json
import os
import sys
import time

IS_WIN = os.name == 'nt'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(PLUGIN_DIR, '.server_port')

# Fixed port with fallback range
BASE_PORT = 17653


def find_port():
    for p in [BASE_PORT] + list(range(BASE_PORT + 1, BASE_PORT + 50)):
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', p))
                return p
        except OSError:
            continue
    return 0


class ShellSession:
    """Persistent shell session. One command at a time, streaming output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.output_lines = []
        self.output_ready = threading.Event()
        self._last_cmd = None

        if IS_WIN:
            self.proc = subprocess.Popen(
                ['cmd.exe', '/Q', '/K'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Suppress command echo and simplify prompt
            self.proc.stdin.write('@echo off\n')
            self.proc.stdin.write('prompt $P$G\n')
            self.proc.stdin.flush()
            time.sleep(0.2)
            # Drain initialization output
            with self.lock:
                self.output_lines = []
        else:
            self.proc = subprocess.Popen(
                ['bash', '--noprofile', '--norc'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                preexec_fn=os.setsid,
            )
        self.cwd = os.getcwd()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        """Continuously drain shell stdout so the shell never blocks."""
        while True:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                with self.lock:
                    self.output_lines.append(line)
                self.output_ready.set()
            except Exception:
                break

    def execute(self, cmd):
        """Yield output lines as they arrive from the persistent shell."""
        self.output_ready.clear()
        self._last_cmd = cmd.strip()
        sentinel = '__STASH_CMD_END_%d__' % time.time_ns()
        with self.lock:
            self.output_lines = []
            self.proc.stdin.write(cmd + '\n')
            self.proc.stdin.write('echo %s\n' % sentinel)
            self.proc.stdin.flush()

        yielded_ids = set()
        while True:
            self.output_ready.wait(timeout=0.1)
            self.output_ready.clear()

            with self.lock:
                batch = list(self.output_lines)
                self.output_lines = []

            new_lines = [l for l in batch if id(l) not in yielded_ids]
            for l in new_lines:
                yielded_ids.add(id(l))
                if sentinel in l:
                    return
                # Filter out command echo (cmd.exe may echo even with @echo off)
                stripped = l.strip()
                if stripped and stripped != self._last_cmd and not stripped.startswith(self._last_cmd):
                    yield l

            if self.proc.poll() is not None:
                with self.lock:
                    remaining = list(self.output_lines)
                    self.output_lines = []
                for l in remaining:
                    if id(l) not in yielded_ids:
                        stripped = l.strip()
                        if stripped and stripped != self._last_cmd and not stripped.startswith(self._last_cmd):
                            yield l
                break

    def interrupt(self):
        """Send Ctrl+C to kill a running foreground command."""
        try:
            self.proc.stdin.write('\x03')
            self.proc.stdin.flush()
        except Exception:
            pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    shell = None

    def log_message(self, format, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != '/execute':
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            cmd = data.get('cmd', '')
        except Exception:
            cmd = ''

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Connection', 'close')
        self._cors()
        self.end_headers()

        try:
            for chunk in Handler.shell.execute(cmd):
                try:
                    self.wfile.write(chunk.encode('utf-8', 'replace'))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Client aborted (Ctrl+C) — kill foreground command
                    Handler.shell.interrupt()
                    return
        except (BrokenPipeError, ConnectionResetError):
            try:
                Handler.shell.interrupt()
            except Exception:
                pass

    def do_GET(self):
        if self.path == '/hello':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({'port': SERVER_PORT}).encode())
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()


SERVER_PORT = 0


def main():
    global SERVER_PORT
    SERVER_PORT = find_port()

    # Advertise port so the frontend can discover it via Stash assets
    try:
        with open(PORT_FILE, 'w') as f:
            f.write(str(SERVER_PORT))
    except Exception:
        pass

    Handler.shell = ShellSession()

    try:
        with socketserver.ThreadingTCPServer(('127.0.0.1', SERVER_PORT), Handler) as httpd:
            httpd.serve_forever()
    except OSError:
        # Port held by another instance — silently exit
        sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
