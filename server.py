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
import select
import socket

IS_WIN = os.name == 'nt'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(PLUGIN_DIR, '.server_port')
LOG_FILE = os.path.join(PLUGIN_DIR, '.server_debug.log')


def _log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + msg + '\n')
    except Exception:
        pass

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
        self.exec_lock = threading.Lock()
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
                    _log('reader: EOF (shell exited, rc=%s)' % self.proc.poll())
                    break
                with self.lock:
                    self.output_lines.append(line)
                self.output_ready.set()
            except Exception as ex:
                _log('reader: exception %r' % ex)
                break

    def execute(self, cmd, client_gone=None):
        """Yield output lines as they arrive from the persistent shell.

        One command at a time; client_gone (optional) aborts when the HTTP
        client has disconnected, so a dead request can never wedge the
        session for everyone else.
        """
        if not self.exec_lock.acquire(blocking=False):
            yield 'shell busy: another command is still running\n'
            return
        try:
            for chunk in self._execute_inner(cmd, client_gone):
                yield chunk
        finally:
            self.exec_lock.release()

    def _execute_inner(self, cmd, client_gone=None):
        self.output_ready.clear()
        self._last_cmd = cmd.strip()
        sentinel = '__STASH_CMD_END_%d__' % time.time_ns()
        with self.lock:
            self.output_lines = []
            try:
                self.proc.stdin.write(cmd + '\n')
                self.proc.stdin.write('echo %s\n' % sentinel)
                self.proc.stdin.flush()
            except Exception as ex:
                _log('execute: stdin write failed %r (rc=%s)' % (ex, self.proc.poll()))

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

            if client_gone is not None and client_gone():
                _log('execute: client disconnected, interrupting')
                self.interrupt()
                return

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
        if self.path not in ('/execute', '/run'):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        cmd = ''
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                cmd = str(data.get('cmd', ''))
        except Exception:
            pass
        if not cmd and body:
            # Raw text body — the whole body IS the command.
            # Lets a plain `curl ... -d "start notepad"` drive the shell.
            cmd = body.decode('utf-8', 'replace')

        self._stream_exec(cmd)

    def _client_gone(self):
        """True when the HTTP peer has closed the connection."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r:
                return self.connection.recv(1, socket.MSG_PEEK) == b''
        except Exception:
            return True
        return False

    def _stream_exec(self, cmd):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Connection', 'close')
        self._cors()
        self.end_headers()

        try:
            for chunk in Handler.shell.execute(cmd, self._client_gone):
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
        if self.path.startswith('/run'):
            # GET /run?cmd=start%20notepad — one-liner friendly
            cmd = ''
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                cmd = (q.get('cmd') or [''])[0]
            except Exception:
                pass
            self._stream_exec(cmd)
            return
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
