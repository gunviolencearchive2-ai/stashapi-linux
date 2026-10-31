#!/usr/bin/env python3
"""Background HTTP terminal server for Stash API plugin.

Starts a persistent shell (cmd.exe /K on Windows, bash on Linux) and
streams command output to the browser in real time via chunked HTTP.
Terminal + explorer only — no extra API endpoints.
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
PLUGIN_ID = os.path.basename(PLUGIN_DIR)
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


class ShellSession:
    """Persistent shell session. One command at a time, streaming output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.exec_lock = threading.Lock()
        self.reader_ok = True
        self.output_lines = []
        self.output_ready = threading.Event()
        self._last_cmd = None

        if IS_WIN:
            # Binary pipes: cmd.exe emits OEM (cp866) for some strings and
            # UTF-8 for others — text=True would crash the reader thread on
            # the first non-UTF-8 byte (it killed the whole session on `dir`
            # of any drive with a Russian volume label).
            self.proc = subprocess.Popen(
                ['cmd.exe', '/Q', '/K'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Suppress command echo and simplify prompt, then wait until the
            # shell has consumed the init commands (sentinel) so user commands
            # are never echoed with the prompt prefix.
            self.proc.stdin.write(b'@echo off\r\nprompt $P$G\r\nchcp 866>nul\r\necho __STASH_INIT_END__\r\n')
            self.proc.stdin.flush()
            deadline = time.time() + 5
            try:
                while time.time() < deadline:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    if b'__STASH_INIT_END__' in line:
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
                preexec_fn=os.setsid,
            )
        self.cwd = os.getcwd()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @staticmethod
    def _smart(b):
        """Decode shell output bytes: cmd emits cp866/UTF-8 depending on the
        program, never let decoding kill the reader."""
        for enc in ('utf-8', 'cp866', 'cp1251'):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode('utf-8', 'replace')

    def _read_loop(self):
        """Continuously drain shell stdout so the shell never blocks."""
        while True:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    _log('reader: EOF (shell exited, rc=%s)' % self.proc.poll())
                    break
                with self.lock:
                    self.output_lines.append(self._smart(line))
                self.output_ready.set()
            except Exception as ex:
                _log('reader: exception %r' % ex)
                break
        self.reader_ok = False
        self.output_ready.set()

    def execute(self, cmd, client_gone=None, cwd=''):
        """Yield output lines as they arrive from the persistent shell.

        One command at a time; client_gone (optional) aborts when the HTTP
        client has disconnected, so a dead request can never wedge the
        session for everyone else.
        """
        if not self.exec_lock.acquire(blocking=False):
            # Safety net: a lock held way too long means some edge case leaked
            # it — force-release and take over ('busy forever' is worse).
            held = time.time() - getattr(self, '_lock_since', 0)
            if held > 120:
                _log('execute: lock held %.0fs -- force release' % held)
                self.exec_lock.release()
                self.exec_lock.acquire()
            else:
                yield 'shell busy: another command is still running\n'
                return
        self._lock_since = time.time()
        if self.proc.poll() is not None or not self.reader_ok:
            # user typed `exit`, cmd died, was killed by interrupt(), or the
            # output reader crashed — give the session a fresh shell
            _log('shell dead (rc=%s, reader_ok=%s) -- respawning'
                 % (self.proc.poll(), self.reader_ok))
            Handler.shell = ShellSession()
            self.exec_lock.release()
            for chunk in Handler.shell.execute(cmd, client_gone, cwd):
                yield chunk
            return
        try:
            for chunk in self._execute_inner(cmd, client_gone, cwd):
                yield chunk
        finally:
            self.exec_lock.release()
            _log('execute: lock released')

    def _execute_inner(self, cmd, client_gone=None, cwd=''):
        self.output_ready.clear()
        self._last_cmd = cmd.strip()
        sentinel = '__STASH_CMD_END_%d__' % time.time_ns()
        with self.lock:
            self.output_lines = []
            try:
                # fresh shell (respawn): restore the working directory first
                if cwd:
                    self.proc.stdin.write(('cd /d "%s"\n' % cwd).encode('utf-8', 'replace'))
                # cmd accepts pipe input as UTF-8, BUT non-ASCII arguments are
                # re-decoded via the OEM CP internally and break (dir/type on
                # cyrillic paths -> 'file not found'). Route those through a
                # temp .bat written in the OEM codepage instead; batch files
                # are parsed as cp866 on RU Windows.
                if IS_WIN and not cmd.isascii():
                    import tempfile
                    try:
                        bat = os.path.join(tempfile.gettempdir(), 'stash_cmd.bat')
                        with open(bat, 'w', encoding='cp866', errors='replace',
                                  newline='') as f:
                            f.write('@echo off\r\n' + cmd.rstrip() + '\r\n')
                        line = 'call "%s"' % bat
                    except Exception:
                        line = cmd
                else:
                    line = cmd
                self.proc.stdin.write((line + '\n').encode('utf-8', 'replace'))
                # 'echo.' forces a line break: commands whose output lacks a
                # trailing newline (e.g. `type` of a file) would otherwise
                # swallow the sentinel into the same line
                self.proc.stdin.write(b'echo.\r\n')
                self.proc.stdin.write(('echo %s\n' % sentinel).encode())
                self.proc.stdin.flush()
            except Exception as ex:
                _log('execute: stdin write failed %r (rc=%s)' % (ex, self.proc.poll()))

        idle = 0.0
        while True:
            self.output_ready.wait(timeout=0.1)
            self.output_ready.clear()

            with self.lock:
                batch = list(self.output_lines)
                self.output_lines = []

            # NOTE: do NOT filter lines by id() — CPython reuses string object
            # ids after GC, which silently dropped random output lines.
            # The swap above guarantees every batch line is new.
            if not batch:
                idle += 0.1
            else:
                idle = 0.0
            for l in batch:
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
                    stripped = l.strip()
                    if stripped and stripped != self._last_cmd and not stripped.startswith(self._last_cmd):
                        yield l
                break

    def interrupt(self):
        """Abort the running foreground command.

        Writing \\x03 to the pipe does NOT interrupt a child (0x03 is just a
        byte there — the command kept running and wedged the whole session).
        Kill the cmd process tree instead; the session respawns on next use.
        """
        try:
            if IS_WIN:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.proc.pid)],
                               capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except Exception as ex:
            _log('interrupt: kill failed %r' % ex)


DS_CACHE_FILE = os.path.join(PLUGIN_DIR, '.dirsizes.json')
_ds_lock = threading.Lock()


def _dirsize_batch(paths):
    """Folder sizes for a batch of paths; disk-cached, invalidated by dir mtime.
    Runs on the local server so it never starves the Stash operation queue."""
    with _ds_lock:
        try:
            with open(DS_CACHE_FILE, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        out = {}
        changed = False
        for raw in paths[:60]:
            p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(raw))))
            try:
                if not os.path.isdir(p):
                    out[p] = None
                    continue
                mt = int(os.path.getmtime(p))
            except OSError:
                out[p] = None
                continue
            ent = cache.get(p.lower())
            if ent and len(ent) == 2 and ent[1] == mt:
                out[p] = {'size': ent[0], 'cached': True}
                continue
            total = 0
            for root, _d, files in os.walk(p, onerror=lambda e: None):
                for fn in files:
                    if root == PLUGIN_DIR and fn == '.dirsizes.json':
                        continue
                    try:
                        total += os.lstat(os.path.join(root, fn)).st_size
                    except OSError:
                        pass
            cache[p.lower()] = [total, mt]
            changed = True
            out[p] = {'size': total, 'cached': False}
        if changed:
            try:
                with open(DS_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cache, f)
            except OSError:
                pass
        return {'sizes': out}


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


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    shell = None

    def log_message(self, format, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # ALWAYS close: Chrome reuses keep-alive connections that this
        # threading server half-opens, and pages then hang forever on fetch
        self.send_header('Connection', 'close')
        self.close_connection = True

    def _origin_ok(self):
        """Only local pages (Stash UI) may read us cross-origin; curl has no Origin."""
        o = self.headers.get('Origin')
        if not o:
            return True
        try:
            from urllib.parse import urlparse
            return urlparse(o).hostname in ('localhost', '127.0.0.1', '::1')
        except Exception:
            return False

    def _plain(self, code, text, ctype='text/plain; charset=utf-8'):
        body = text.encode('utf-8', 'replace')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            self._plain(403, 'forbidden')
            return
        if self.path == '/hello':
            self._plain(200, json.dumps({'port': SERVER_PORT}), 'application/json')
            return
        self._plain(404, 'not found')

    def _client_gone(self):
        """True when the HTTP peer has closed the connection."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r:
                return self.connection.recv(1, socket.MSG_PEEK) == b''
        except Exception:
            return True
        return False

    def do_POST(self):
        if not self._origin_ok():
            self._plain(403, 'forbidden')
            return
        if self.path == '/dirsize':
            # folder sizes WITHOUT going through the Stash GraphQL queue
            # (each GraphQL op spawns a PowerShell -- that starved everything)
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length))
                res = _dirsize_batch(list(data.get('paths') or []))
            except Exception as ex:
                _log('dirsize failed: %r' % ex)
                res = {'sizes': {}}
            self._plain(200, json.dumps(res), 'application/json')
            return

        if self.path != '/execute':
            self._plain(404, 'not found')
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        cmd = ''
        cwd = ''
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                cmd = str(data.get('cmd', ''))
                cwd = str(data.get('cwd', ''))
        except Exception:
            pass

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Connection', 'close')
        self._cors()
        self.end_headers()

        try:
            for chunk in Handler.shell.execute(cmd, self._client_gone, cwd):
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
    # capture ANY startup crash into the debug log (bootstrap launches us
    # hidden, so without this the reason would be invisible on remotes)
    try:
        sys.stderr = open(LOG_FILE, 'a', encoding='utf-8')
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        _log('server crashed: %s' % traceback.format_exc())
