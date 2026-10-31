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
import struct
import zlib
import string
import mimetypes
import shutil
import tempfile

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

    def execute(self, cmd, client_gone=None):
        """Yield output lines as they arrive from the persistent shell.

        One command at a time; client_gone (optional) aborts when the HTTP
        client has disconnected, so a dead request can never wedge the
        session for everyone else.
        """
        if not self.exec_lock.acquire(blocking=False):
            yield 'shell busy: another command is still running\n'
            return
        if self.proc.poll() is not None or not self.reader_ok:
            # user typed `exit`, cmd died, or the output reader crashed —
            # give the session a fresh shell
            _log('shell dead (rc=%s, reader_ok=%s) — respawning'
                 % (self.proc.poll(), self.reader_ok))
            Handler.shell = ShellSession()
            self.exec_lock.release()
            for chunk in Handler.shell.execute(cmd, client_gone):
                yield chunk
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
                # cmd accepts pipe input as UTF-8, BUT non-ASCII arguments are
                # re-decoded via the OEM CP internally and break (dir/type on
                # cyrillic paths -> 'file not found'). Route those through a
                # temp .bat written in the OEM codepage instead; batch files
                # are parsed as cp866 on RU Windows.
                if IS_WIN and not cmd.isascii():
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
            self.proc.stdin.write(b'\x03')
            self.proc.stdin.flush()
        except Exception:
            pass


# ------------------------------------------------- media: PNG / capture ----

CACHE_DIR = os.path.join(PLUGIN_DIR, 'assets', '_cache')


def png_from_bgra(w, h, bgra):
    """Encode a top-down 32bpp BGRA buffer as PNG bytes (stdlib only)."""
    buf = bytearray(bgra)
    buf[0::4], buf[2::4] = bytes(buf[2::4]), bytes(buf[0::4])  # BGRA -> RGBA
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: none
        raw += buf[y * stride:(y + 1) * stride]

    def chunk(typ, data):
        c = struct.pack('>I', len(data)) + typ + data
        return c + struct.pack('>I', zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + chunk(b'IEND', b''))


def _bih(w, h):
    import ctypes

    class BIH(ctypes.Structure):
        _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                    ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                    ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                    ('biSizeImage', ctypes.c_uint32), ('biXPels', ctypes.c_int32),
                    ('biYPels', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                    ('biClrImportant', ctypes.c_uint32)]
    return BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # negative = top-down


def capture_screen_png():
    """Full virtual screen (all monitors) via GDI. Windows only."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        ctypes.windll.user32.SetProcessDPIAware()
        x = u.GetSystemMetrics(76)
        y = u.GetSystemMetrics(77)
        w = u.GetSystemMetrics(78)
        h = u.GetSystemMetrics(79)
        if not w or not h:
            return None
        src = u.GetDC(0)
        mem = g.CreateCompatibleDC(src)
        hbm = g.CreateCompatibleBitmap(src, w, h)
        g.SelectObject(mem, hbm)
        g.BitBlt(mem, 0, 0, w, h, src, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
        bi = _bih(w, h)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mem, hbm, 0, h, buf, ctypes.byref(bi), 0)
        png = png_from_bgra(w, h, buf.raw)
        g.DeleteObject(hbm)
        g.DeleteDC(mem)
        u.ReleaseDC(0, src)
        return png
    except Exception as ex:
        _log('screenshot failed: %r' % ex)
        return None


def screenshot_reply():
    """Capture, save into the served cache dir, return its URL as text."""
    png = capture_screen_png()
    if not png:
        return 'screenshot failed (no display?)'
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fn = 'shot-%s.png' % time.strftime('%Y%m%d-%H%M%S')
        with open(os.path.join(CACHE_DIR, fn), 'wb') as f:
            f.write(png)
        # keep the cache dir tidy: drop shots older than 10 min
        try:
            now = time.time()
            for n in os.listdir(CACHE_DIR):
                if n.startswith('shot-'):
                    p = os.path.join(CACHE_DIR, n)
                    if now - os.path.getmtime(p) > 600:
                        os.remove(p)
        except OSError:
            pass
        return 'http://127.0.0.1:%d/cache/%s' % (SERVER_PORT, fn)
    except OSError as ex:
        _log('screenshot save failed: %r' % ex)
        return 'screenshot save failed'


_thumb_sema = threading.Semaphore(2)  # COM thumbnail work is expensive


def shell_thumbnail_png(path, size=256):
    """Windows Explorer thumbnail via IShellItemImageFactory (system COM call).
    Works for images AND videos. Returns PNG bytes or None.
    Disk-cached by path+mtime; COM generation capped at 2 parallel calls."""
    if not os.path.isfile(path):
        return None
    import hashlib
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = hashlib.md5(('%s|%d|%d' % (path.lower(), mtime, size))
                      .encode('utf-8', 'replace')).hexdigest()
    cache_dir = os.path.join(CACHE_DIR, 'thumbs')
    cache_file = os.path.join(cache_dir, key + '.png')
    try:
        with open(cache_file, 'rb') as f:
            return f.read()
    except OSError:
        pass
    if not IS_WIN:
        return None
    with _thumb_sema:
        try:
            import ctypes
            import uuid
            from ctypes import c_void_p, byref, Structure, c_uint32, c_uint16, c_uint8, c_long, c_uint

            class GUID(Structure):
                _fields_ = [('Data1', c_uint32), ('Data2', c_uint16),
                            ('Data3', c_uint16), ('Data4', c_uint8 * 8)]

            def mkguid(s):
                hx = uuid.UUID(s).hex
                return GUID(int(hx[0:8], 16), int(hx[8:12], 16), int(hx[12:16], 16),
                            (c_uint8 * 8)(*(int(hx[i:i + 2], 16) for i in range(16, 32, 2))))

            ole32 = ctypes.windll.ole32
            ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
            try:
                shell32 = ctypes.windll.shell32
                shell32.SHCreateItemFromParsingName.argtypes = [
                    ctypes.c_wchar_p, c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p)]
                shell32.SHCreateItemFromParsingName.restype = ctypes.HRESULT
                item = c_void_p()
                hr = shell32.SHCreateItemFromParsingName(
                    str(path), None, byref(mkguid('bcc18b79-ba16-442f-80c4-8a59c30c463b')), byref(item))
                if hr != 0 or not item.value:
                    return None

                class SIZE(Structure):
                    _fields_ = [('cx', c_long), ('cy', c_long)]

                vtbl = ctypes.cast(ctypes.cast(item, ctypes.POINTER(c_void_p)).contents.value,
                                   ctypes.POINTER(c_void_p))
                release = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtbl[2])
                getimage = ctypes.WINFUNCTYPE(
                    ctypes.HRESULT, c_void_p, SIZE, c_uint, ctypes.POINTER(c_void_p))(vtbl[3])
                hbm = c_void_p()
                hr = getimage(item, SIZE(size, size), 0, byref(hbm))
                if hr != 0 or not hbm.value:
                    release(item)
                    return None

                class BITMAP(Structure):
                    _fields_ = [('bmType', c_long), ('bmWidth', c_long), ('bmHeight', c_long),
                                ('bmWidthBytes', c_long), ('bmPlanes', c_uint16),
                                ('bmBitsPixel', c_uint16), ('bmBits', c_void_p)]

                u, g = ctypes.windll.user32, ctypes.windll.gdi32
                bm = BITMAP()
                g.GetObjectW(hbm, ctypes.sizeof(BITMAP), byref(bm))
                w, h = bm.bmWidth, bm.bmHeight
                if w <= 0 or h <= 0 or w * h > 16_000_000:
                    g.DeleteObject(hbm)
                    release(item)
                    return None
                src = u.GetDC(0)
                mem = g.CreateCompatibleDC(src)
                bi = _bih(w, h)
                buf = ctypes.create_string_buffer(w * h * 4)
                g.GetDIBits(mem, hbm, 0, h, buf, ctypes.byref(bi), 0)
                png = png_from_bgra(w, h, buf.raw)
                g.DeleteObject(hbm)
                g.DeleteDC(mem)
                u.ReleaseDC(0, src)
                release(item)
                if png:
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(cache_file, 'wb') as f:
                            f.write(png)
                        names = os.listdir(cache_dir)
                        if len(names) > 1000:  # cap cache growth
                            for n in sorted(names)[:200]:
                                try:
                                    os.remove(os.path.join(cache_dir, n))
                                except OSError:
                                    pass
                    except OSError:
                        pass
                return png
            finally:
                ole32.CoUninitialize()
        except Exception:
            import traceback
            _log('thumbnail failed: %s' % traceback.format_exc())
            return None


# -------------------------------------------------------- media: status ----

_prev_cpu = None
_gpu_name = None


def _human_uptime(sec):
    sec = int(sec)
    return '%dd %dh %dm' % (sec // 86400, sec % 86400 // 3600, sec % 3600 // 60)


def system_status():
    """One-page host status as plain text."""
    global _prev_cpu, _gpu_name
    lines = []
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        idle, kern, usr = (ctypes.c_ulonglong() for _ in range(3))
        if k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(usr)):
            cur = (idle.value, kern.value + usr.value)
            if _prev_cpu:
                dt = cur[1] - _prev_cpu[1]
                di = cur[0] - _prev_cpu[0]
                pct = 100.0 * (1 - di / dt) if dt else 0.0
                lines.append('CPU:      %5.1f %%' % max(0.0, min(100.0, pct)))
            else:
                lines.append('CPU:      n/a (запусти ещё раз)')
            _prev_cpu = cur

        class MS(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_uint32), ('dwMemoryLoad', ctypes.c_uint32),
                        ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                        ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                        ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                        ('ullAvailExtendedVirtual', ctypes.c_uint64)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        if k.GlobalMemoryStatusEx(ctypes.byref(ms)):
            lines.append('RAM:      %.1f / %.1f GB занято (%d%%)'
                         % ((ms.ullTotalPhys - ms.ullAvailPhys) / 2**30,
                            ms.ullTotalPhys / 2**30, ms.dwMemoryLoad))
        lines.append('Uptime:   %s' % _human_uptime(k.GetTickCount64() / 1000))

        if _gpu_name is None:
            try:
                r = subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     '(Get-CimInstance Win32_VideoController).Name -join ", "'],
                    capture_output=True, text=True, timeout=15)
                _gpu_name = r.stdout.strip() or 'n/a'
            except Exception:
                _gpu_name = 'n/a'
        lines.append('GPU:      %s' % _gpu_name)

        disks = []
        for c in string.ascii_uppercase:
            root = c + ':\\'
            if os.path.exists(root):
                try:
                    du = shutil.disk_usage(root)
                    disks.append('%s %.0f/%.0f GB (%d%%)'
                                 % (c + ':', du.used / 2**30, du.total / 2**30,
                                    100 * du.used / du.total if du.total else 0))
                except OSError:
                    pass
        lines.append('Disks:    ' + '   '.join(disks))
    else:
        try:
            la = os.getloadavg()
            lines.append('Load:     %.2f %.2f %.2f' % la)
        except (AttributeError, OSError):
            pass
        try:
            with open('/proc/meminfo') as f:
                mi = {l.split(':')[0]: int(l.split()[1]) for l in f if ':' in l}
            t, a = mi.get('MemTotal', 0), mi.get('MemAvailable', 0)
            lines.append('RAM:      %.1f / %.1f GB занято (%d%%)'
                         % ((t - a) / 2**20, t / 2**20, 100 * (t - a) / t if t else 0))
        except (OSError, ValueError):
            pass
        try:
            with open('/proc/uptime') as f:
                lines.append('Uptime:   %s' % _human_uptime(float(f.read().split()[0])))
        except (OSError, ValueError, IndexError):
            pass
        try:
            du = shutil.disk_usage('/')
            lines.append('Disk /:   %.0f/%.0f GB (%d%%)'
                         % (du.used / 2**30, du.total / 2**30,
                            100 * du.used / du.total if du.total else 0))
        except OSError:
            pass
    return '\n'.join(lines)


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
        if not self._origin_ok():
            self._plain(403, 'forbidden')
            return
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
        low = cmd.strip().lower()
        if low in ('screenshot', 'скриншот', 'скрин', 'shot'):
            self._plain(200, screenshot_reply())
            return
        if low == 'status':
            self._plain(200, system_status())
            return

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

    def _qs(self):
        from urllib.parse import urlparse, parse_qs, unquote
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        if not self._origin_ok():
            self._plain(403, 'forbidden')
            return
        p = self.path

        if p.startswith('/run/'):
            # GET /run/<command> — the WHOLE rest of the path is the command.
            # Lets you paste:  curl.exe "http://127.0.0.1:17653/run/start notepad"
            # (browsers auto-encode spaces; only the URL needs quoting)
            from urllib.parse import unquote
            self._stream_exec(unquote(p[5:]))
            return

        if p.startswith('/run'):
            cmd = (self._qs().get('cmd') or [''])[0]
            self._stream_exec(cmd)
            return

        if p == '/hello':
            self._plain(200, json.dumps({'port': SERVER_PORT}), 'application/json')
            return

        if p == '/status':
            self._plain(200, system_status())
            return

        if p == '/shot':
            self._plain(200, screenshot_reply())
            return

        if p == '/shot.png':
            # screenshot straight into the browser — bookmarkable
            png = capture_screen_png()
            if png:
                self._send_bytes(200, 'image/png', png)
            else:
                self._plain(500, 'screenshot failed (no display?)')
            return

        if p.startswith('/thumb'):
            path = (self._qs().get('path') or [''])[0]
            if path and os.path.isfile(path):
                png = shell_thumbnail_png(path)
                if png:
                    self._send_bytes(200, 'image/png', png)
                    return
                ext = os.path.splitext(path)[1].lower()
                if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico', '.avif'):
                    # no shell thumbnail — let the browser scale the original
                    self._serve_file(path)
                    return
            self._plain(404, 'no thumbnail')
            return

        if p.startswith('/file'):
            path = (self._qs().get('path') or [''])[0]
            if path and os.path.isfile(path):
                self._serve_file(path)
            else:
                self._plain(404, 'not a file')
            return

        if p.startswith('/cache/'):
            from urllib.parse import unquote
            name = os.path.basename(unquote(p[len('/cache/'):]))
            path = os.path.join(CACHE_DIR, name)
            if os.path.isfile(path):
                self._serve_file(path)
            else:
                self._plain(404, 'not in cache')
            return

        self._plain(404, 'not found')

    def _send_bytes(self, code, ctype, data):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, path):
        """Stream a file with mime detection and basic Range support (video seeking)."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self._plain(404, 'stat failed')
            return
        mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith('bytes='):
            try:
                s, e = rng[6:].split('-', 1)
                if s:
                    start = int(s)
                if e:
                    end = min(int(e), size - 1)
                elif not s:
                    start = max(0, size - int(e))  # suffix range: last N bytes
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        if start > end or start >= size:
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % size)
            self._cors()
            self.end_headers()
            return
        length = end - start + 1
        try:
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if code == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self._cors()
            self.end_headers()
            with open(path, 'rb') as f:
                f.seek(start)
                sent = 0
                while sent < length:
                    chunk = f.read(min(65536, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as ex:
            _log('serve_file %s failed: %r' % (path, ex))


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
