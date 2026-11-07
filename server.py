#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stashapi local JSON API (loopback-only).

Endpoints:
    GET  /hello                 -> {"ok":true,"port":N}
    POST /execute  {"cmd","cwd","timeout"} -> {"out","err","code","cwd"}
    POST /interrupt             -> {"ok":true}   (kills the running command tree)

Stability rules:
    - binds 127.0.0.1 only; loopback peers only
    - each command = one fresh process (nothing to wedge, nothing to leak)
    - every response has Content-Length + Connection: close
    - output capped; stderr captured to the debug log
"""
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time

IS_WIN = os.name == 'nt'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(PLUGIN_DIR, '.server_port')
LOG_FILE = os.path.join(PLUGIN_DIR, '.server_debug.log')
PID_FILE = os.path.join(PLUGIN_DIR, '.cmd_pid')
BASE_PORT = 17653

MAX_OUT = 512 * 1024
MAX_ERR = 64 * 1024


def _log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + msg + '\n')
    except Exception:
        pass


PID_LOCK = threading.Lock()


def _kill_tree(pid):
    try:
        if IS_WIN:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                           capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as ex:
        _log('kill_tree: %r' % ex)


def _run_exec(cmd, cwd, timeout):
    if not cmd:
        return {'out': '', 'err': '', 'code': 0, 'cwd': cwd}
    if IS_WIN:
        shell_cmd = 'chcp 65001 >nul 2>&1 & cd /d "%s" && %s' % (cwd.replace('"', ''), cmd)
    else:
        shell_cmd = 'cd "%s" && %s' % (cwd.replace('"', ''), cmd)
    try:
        proc = subprocess.Popen(shell_cmd, shell=True, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,
                                start_new_session=not IS_WIN)
    except OSError as ex:
        return {'out': '', 'err': str(ex), 'code': 1, 'cwd': cwd}

    with PID_LOCK:
        try:
            with open(PID_FILE, 'w') as f:
                f.write(str(proc.pid))
        except OSError:
            pass

    timed_out = False
    try:
        o, e = proc.communicate(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        o, e = proc.communicate()
    finally:
        with PID_LOCK:
            try:
                cur = open(PID_FILE).read().strip()
                if cur == str(proc.pid):
                    os.remove(PID_FILE)
            except Exception:
                pass

    out = o.decode('utf-8', 'replace')
    err = e.decode('utf-8', 'replace')
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + '\n[... вывод обрезан (%d байт)]' % len(out)
    if len(err) > MAX_ERR:
        err = err[:MAX_ERR] + '\n[... stderr обрезан]'
    res = {'out': out, 'err': err, 'code': proc.returncode or 0, 'cwd': cwd}
    if timed_out:
        res['err'] += '\n[прервано по таймауту %gs]' % timeout
    return res


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def _headers(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # ALWAYS close: keep-alive sockets make browser/tooling clients hang
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()

    def _plain(self, code, text, ctype='application/json'):
        body = text.encode('utf-8', 'replace')
        self._headers(code, ctype, body)
        self.wfile.write(body)

    def _loopback(self):
        try:
            ip = self.connection.getpeername()[0]
        except Exception:
            return False
        return ip == '127.0.0.1' or ip == '::1'

    def do_GET(self):
        if not self._loopback():
            self._plain(403, '{"err":"loopback only"}')
            return
        if self.path == '/hello':
            self._plain(200, json.dumps({'ok': True, 'port': SERVER_PORT}))
            return
        self._plain(404, '{"err":"not found"}')

    def do_POST(self):
        if not self._loopback():
            self._plain(403, '{"err":"loopback only"}')
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        if self.path == '/interrupt':
            try:
                pid = int(open(PID_FILE).read().strip())
                _kill_tree(pid)
                os.remove(PID_FILE)
                self._plain(200, json.dumps({'ok': True}))
            except Exception:
                self._plain(200, json.dumps({'ok': False}))
            return

        if self.path == '/execute':
            cmd = str(data.get('cmd', ''))
            cwd = str(data.get('cwd', '') or os.getcwd())
            if not os.path.isdir(cwd):
                cwd = os.getcwd()
            try:
                timeout = float(data.get('timeout') or 0)
            except (TypeError, ValueError):
                timeout = 0
            try:
                res = _run_exec(cmd, cwd, timeout)
            except Exception as ex:
                _log('execute failed: %r' % ex)
                res = {'out': '', 'err': str(ex), 'code': 1, 'cwd': cwd}
            self._plain(200, json.dumps(res, ensure_ascii=False))
            return

        self._plain(404, '{"err":"not found"}')


SERVER_PORT = 0


def main():
    global SERVER_PORT
    socketserver.ThreadingTCPServer.allow_reuse_addr = True
    socketserver.ThreadingTCPServer.daemon_threads = True

    httpd = None
    for p in [BASE_PORT] + list(range(BASE_PORT + 1, BASE_PORT + 50)):
        try:
            httpd = socketserver.ThreadingTCPServer(('127.0.0.1', p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit(0)
    SERVER_PORT = httpd.server_address[1]

    # advertise port only after a successful bind
    try:
        with open(PORT_FILE, 'w') as f:
            f.write(str(SERVER_PORT))
    except OSError:
        pass

    _log('daemon up on %d' % SERVER_PORT)
    try:
        httpd.serve_forever()
    except Exception as ex:
        _log('daemon crashed: %r' % ex)


if __name__ == '__main__':
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
