#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stashapi.py — CLI-обёртка над плагином "Stash API" (id: stashapi) для Stash.

НАЗНАЧЕНИЕ (для LLM-агента)
===========================
Этот скрипт даёт удалённый доступ к машине, на которой запущен Stash с
плагином stashapi: выполнение shell-команд и работа с файлами. Весь трафик
идёт через GraphQL самого Stash (порт Stash, обычно 9999) — никаких
отдельных портов плагина нет и не нужно.

ИСПОЛЬЗОВАНИЕ
=============
    python stashapi.py [--addr URL] [--user U:P | --api-key KEY] <action> ...

Адрес Stash берётся из (по приоритету):
    1) --addr http://host:9999
    2) переменная окружения STASH (например http://10.0.0.5:9999)
    3) по умолчанию http://127.0.0.1:9999

ДЕЙСТВИЯ (action)
=================
  hello                      — информация о сервере (os, user, cwd); пинг-проверка
  exec "КОМАНДА" [--cwd П]   — выполнить shell-команду (cmd.exe на Windows,
                               /bin/sh на Linux); вывод -> stdout, ошибки -> stderr,
                               код выхода скрипта = коду команды
  list ПУТЬ                  — содержимое папки: тип D/F, имя, размер, mtime
  dirsize ПУТЬ [ПУТЬ...]     — размеры папок (с дисковым кэшем)
  mkdir ПУТЬ                 — создать папку
  rename ПУТЬ НОВОЕ_ИМЯ      — переименовать
  delete ПУТЬ [--recursive]  — удалить файл/папку
  upload ЛОКАЛЬНЫЙ_ФАЙЛ В_ПАПКУ   — залить файл (base64 через GraphQL)
  download ПУТЬ [--save ФАЙЛ]     — ссылка на скачивание; с --save сохраняет
                               файл локально (base64-режим плагина)
  interrupt                  — убить выполняемую команду (процессное дерево)
  reset                      — сбросить рабочую папку сессии

ФЛАГИ
=====
  --json       — печатать сырой JSON-ответ вместо текста (для парсинга)
  --timeout N  — таймаут запроса в секундах (по умолчанию 620)
  --cwd ПУТЬ   — (для exec) рабочая папка команды; запоминайте cwd из ответа,
                 чтобы следующие команды продолжали в том же месте

КОДЫ ВЫХОДА
===========
  0 — успех; 1 — команда завершилась с ошибкой (её код); 2 — транспорт/ошибка
  инструмента (Stash недоступен, плагин не загружен, неверный путь и т.п.)

ПРИМЕРЫ
=======
    python stashapi.py hello
    python stashapi.py exec "dir C:\\"
    python stashapi.py --addr http://10.0.0.5:9999 exec "uptime && df -h"
    python stashapi.py exec "cd C:\\Users && dir" --cwd "C:\\\\"
    python stashapi.py list "/root/.stash" --addr http://server:9999
    python stashapi.py upload report.pdf "C:\\Users\\Public" --addr http://...
    python stashapi.py interrupt

ЗАМЕЧАНИЯ ДЛЯ LLM
=================
* Ответ exec содержит поля out/err/code/cwd — печатайте cwd, если планируете
  следующие команды в той же папке (плагин выполняет каждую команду в новом
  процессе; между командами сохраняется только cwd, не переменные окружения).
* Команды выполняются от имени процесса Stash. В Windows это cmd.exe
  (chcp 65001), в Linux — /bin/sh. Кириллица в путях поддерживается.
* Если Stash защищён паролём: --user login:pass (Basic) или --api-key КЛЮЧ
  (Settings -> Security -> API Keys), либо переменная STASH_API_KEY.
* Длинный вывод не стримится — ответ приходит целиком по завершении команды.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PLUGIN_ID = 'stashapi'
QUERY = ('mutation($p:ID!,$a:Map!){runPluginOperation(plugin_id:$p,args:$a)}')

GQL_ERR = 2


def base_url(args):
    url = args.addr or os.environ.get('STASH') or 'http://127.0.0.1:9999'
    return url.rstrip('/')


def call(args, payload):
    """POST runPluginOperation; returns plugin response dict (unwrapped)."""
    body = json.dumps({'query': QUERY,
                       'variables': {'p': PLUGIN_ID, 'a': payload}}).encode()
    req = urllib.request.Request(base_url(args) + '/graphql', data=body,
                                 headers={'Content-Type': 'application/json'})
    if getattr(args, 'user', None):
        tok = base64.b64encode(args.user.encode()).decode()
        req.add_header('Authorization', 'Basic ' + tok)
    key = getattr(args, 'api_key', None) or os.environ.get('STASH_API_KEY')
    if key:
        req.add_header('ApiKey', key)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:200]
        except Exception:
            pass
        die('HTTP %s от Stash %s\n%s\n(если 401/403 — задайте --user или --api-key)'
            % (e.code, base_url(args), detail))
    except urllib.error.URLError as e:
        die('Stash недоступен: %s (%s)\nПроверьте адрес: %s'
            % (e.reason, base_url(args), base_url(args)))
    if data.get('errors'):
        die('Stash GraphQL error: ' + '; '.join(
            str(x.get('message', x)) for x in data['errors'])
            + '\n(если плагин не найден — установлен ли stashapi на этом Stash?)')
    res = data.get('data', {}).get('runPluginOperation')
    if res is None:
        die('пустой ответ runPluginOperation (плагин stashapi не загружен?)')
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            return {'out': res}
    if isinstance(res, dict) and isinstance(res.get('output'), (dict, str)) \
            and ('out' in res['output'] or 'cwd' in res['output']):
        res = res['output']  # bootstrap-wrapped form
    if isinstance(res, str):
        return {'out': res}
    return res if isinstance(res, dict) else {'out': str(res)}


def die(msg, code=GQL_ERR):
    print(msg, file=sys.stderr)
    sys.exit(code)


def out_json(args, res):
    if getattr(args, 'json', False):
        print(json.dumps(res, ensure_ascii=False))


def fmt_size(n):
    if n is None:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0


def cmd_hello(args):
    res = call(args, {'action': 'hello'})
    out_json(args, res)
    if args.json:
        return
    for k in ('cwd', 'user', 'host', 'os', 'platform', 'python', 'sep'):
        if k in res:
            print('%-8s %s' % (k + ':', res[k]))
    if res.get('drives'):
        print('drives:  ' + '  '.join(res['drives']))


def cmd_exec(args):
    res = call(args, {'action': 'exec', 'cmd': ' '.join(args.cmd),
                      'cwd': args.cwd or ''})
    out_json(args, res)
    if args.json:
        sys.exit(int(res.get('code') or 0))
    if res.get('out'):
        sys.stdout.write(res['out'])
        if not res['out'].endswith('\n'):
            sys.stdout.write('\n')
    if res.get('err'):
        sys.stderr.write(res['err'])
    if res.get('cwd'):
        print('[cwd: %s]' % res['cwd'], file=sys.stderr)
    sys.exit(int(res.get('code') or 0))


def cmd_list(args):
    res = call(args, {'action': 'list', 'path': args.path})
    out_json(args, res)
    if args.json:
        return
    if res.get('error'):
        die(res['error'])
    for it in res.get('items', []):
        print('%s %-40s %12s  %s' % (
            'D' if it.get('dir') else 'F',
            it.get('name', ''),
            '' if it.get('dir') else fmt_size(it.get('size')),
            it.get('mtime', '')))
    print('[%d элементов, parent: %s]' % (len(res.get('items', [])),
                                          res.get('parent') or '-'))


def cmd_dirsize(args):
    res = call(args, {'action': 'dirsize', 'paths': args.paths})
    out_json(args, res)
    if args.json:
        return
    for p, v in (res.get('sizes') or {}).items():
        if v:
            print('%-60s %12s%s' % (p, fmt_size(v.get('size')),
                                    ' (cache)' if v.get('cached') else ''))
        else:
            print('%-60s %12s' % (p, '—'))


def cmd_mkdir(args):
    res = call(args, {'action': 'mkdir', 'path': args.path})
    out_json(args, res)
    if not args.json:
        print('created: ' + str(res.get('path', args.path)))


def cmd_rename(args):
    res = call(args, {'action': 'rename', 'path': args.path, 'to': args.to})
    out_json(args, res)
    if not args.json:
        print('renamed -> ' + str(res.get('path', args.to)))


def cmd_delete(args):
    res = call(args, {'action': 'delete', 'path': args.path,
                      'recursive': bool(args.recursive)})
    out_json(args, res)
    if not args.json:
        print('deleted: ' + args.path)


def cmd_upload(args):
    try:
        data = open(args.file, 'rb').read()
    except OSError as e:
        die('не читается локальный файл: %s' % e)
    res = call(args, {'action': 'upload', 'path': args.to_dir,
                      'name': os.path.basename(args.file),
                      'data': base64.b64encode(data).decode()})
    out_json(args, res)
    if not args.json:
        print('uploaded: %s (%d bytes) -> %s'
              % (os.path.basename(args.file), len(data), res.get('path', args.to_dir)))


def cmd_download(args):
    res = call(args, {'action': 'download', 'path': args.path})
    out_json(args, res)
    if args.json:
        return
    if res.get('url'):
        print(base_url(args).replace(':9999', ':9999') + res['url']
              if res['url'].startswith('/') else res['url'])
        return
    if not res.get('data'):
        die('плагин не вернул ни url, ни data: ' + json.dumps(res, ensure_ascii=False)[:200])
    if not args.save:
        die('получен base64-контент (%s байт); укажите --save ФАЙЛ'
            % fmt_size(res.get('size')))
    blob = base64.b64decode(res['data'])
    open(args.save, 'wb').write(blob)
    print('saved: %s (%d bytes)' % (args.save, len(blob)))


def cmd_interrupt(args):
    res = call(args, {'action': 'interrupt'})
    out_json(args, res)
    if not args.json:
        print('interrupt: ' + ('ok' if res.get('ok') else str(res)))


def cmd_reset(args):
    res = call(args, {'action': 'reset'})
    out_json(args, res)
    if not args.json:
        print('cwd reset -> ' + str(res.get('cwd', '?')))


def main():
    p = argparse.ArgumentParser(
        prog='stashapi.py',
        description='Удалённое управление Stash-хостом через плагин stashapi '
                    '(exec/файлы). Полное описание — в начале этого файла.',
        epilog='Примеры: stashapi.py exec "dir C:\\\\" | stashapi.py --addr '
               'http://10.0.0.5:9999 exec "uptime" | stashapi.py list /root')
    p.add_argument('--addr', help='адрес Stash (default: $STASH или http://127.0.0.1:9999)')
    p.add_argument('--user', help='логин:пароль, если Stash закрыт авторизацией')
    p.add_argument('--api-key', help='API-ключ Stash (Settings -> Security)')
    p.add_argument('--json', action='store_true', help='сырой JSON-ответ')
    p.add_argument('--timeout', type=int, default=620, help='таймаут, сек (default 620)')
    sub = p.add_subparsers(dest='action', required=True)

    s = sub.add_parser('hello', help='инфо о сервере')
    s.set_defaults(fn=cmd_hello)

    s = sub.add_parser('exec', help='выполнить shell-команду')
    s.add_argument('cmd', nargs='+', help='команда (как в cmd.exe / sh)')
    s.add_argument('--cwd', help='рабочая папка (из прошлого ответа exec)')
    s.set_defaults(fn=cmd_exec)

    s = sub.add_parser('list', help='содержимое папки')
    s.add_argument('path', help='путь на сервере')
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('dirsize', help='размеры папок')
    s.add_argument('paths', nargs='+', help='пути')
    s.set_defaults(fn=cmd_dirsize)

    s = sub.add_parser('mkdir', help='создать папку')
    s.add_argument('path')
    s.set_defaults(fn=cmd_mkdir)

    s = sub.add_parser('rename', help='переименовать')
    s.add_argument('path')
    s.add_argument('to', help='новый полный путь')
    s.set_defaults(fn=cmd_rename)

    s = sub.add_parser('delete', help='удалить')
    s.add_argument('path')
    s.add_argument('--recursive', action='store_true', help='папку целиком')
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser('upload', help='залить локальный файл на сервер')
    s.add_argument('file', help='локальный файл')
    s.add_argument('to_dir', help='папка на сервере')
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser('download', help='ссылка/сохранить файл с сервера')
    s.add_argument('path', help='файл на сервере')
    s.add_argument('--save', help='локальное имя для сохранения base64-контента')
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser('interrupt', help='убить выполняемую команду')
    s.set_defaults(fn=cmd_interrupt)

    s = sub.add_parser('reset', help='сбросить cwd сессии')
    s.set_defaults(fn=cmd_reset)

    args = p.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
