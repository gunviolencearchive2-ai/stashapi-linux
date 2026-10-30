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


def bind_server():
    """Bind the HTTP server to the first free port and return it.

    Binds once — do NOT probe with bind+close first: on Windows a just-closed
    bound port cannot be rebound immediately (WSAEADDRINUSE), which made the
    server exit silently while the port file already advertised a dead port.
    """
    for p in [BASE_PORT] + list(range(BASE_PORT + 1, BASE_PORT + 50)):
        try:
            return socketserver.ThreadingTCPServer(('127.0.0.1', p), Handler)
        except OSError:
            continue
    return None


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
            # Suppress command echo and simplify prompt, then wait until the
            # shell has consumed the init commands (sentinel) so user commands
            # are never echoed with the prompt prefix.
            self.proc.stdin.write('@echo off\n')
            self.proc.stdin.write('prompt $P$G\n')
            self.proc.stdin.write('echo __STASH_INIT_END__\n')
            self.proc.stdin.flush()
            deadline = time.time() + 5
            try:
                while time.time() < deadline:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    if '__STASH_INIT_END__' in line:
                        break
            except Exception:
                pass
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
        idle = 0.0
        while True:
            self.output_ready.wait(timeout=0.1)
            self.output_ready.clear()

            with self.lock:
                batch = list(self.output_lines)
                self.output_lines = []

            new_lines = [l for l in batch if id(l) not in yielded_ids]
            if not new_lines:
                idle += 0.1
            else:
                idle = 0.0
            for l in new_lines:
                yielded_ids.add(id(l))
                if sentinel in l:
                    return
                # Filter out command echo (cmd.exe may echo even with @echo off)
                stripped = l.strip()
                if stripped and stripped != self._last_cmd and not stripped.startswith(self._last_cmd):
                    yield l

            if idle > 700:
                # Safety: no output and no sentinel for a very long time —
                # never let a lost sentinel block the session forever.
                return

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
            body = json.dumps({'port': SERVER_PORT}).encode()
            # Content-Length is REQUIRED: with HTTP/1.1 keep-alive a client
            # would otherwise hang waiting for the body until its timeout.
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b'not found'
            self.send_response(404)
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)


SERVER_PORT = 0


def main():
    global SERVER_PORT
    socketserver.ThreadingTCPServer.allow_reuse_addr = True
    socketserver.ThreadingTCPServer.daemon_threads = True

    httpd = bind_server()
    if httpd is None:
        sys.exit(0)
    SERVER_PORT = httpd.server_address[1]

    Handler.shell = ShellSession()

    # Advertise port ONLY after a successful bind
    try:
        with open(PORT_FILE, 'w') as f:
            f.write(str(SERVER_PORT))
    except Exception:
        pass

    try:
        httpd.serve_forever()
    except OSError:
        sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
