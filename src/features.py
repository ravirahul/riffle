"""Preview, icons, duplicates, cleanup and updates: HTTP routes added to riffle.py.

riffle.py calls init() once, then get()/post() for every request; they return
True when they answered. Kept apart so riffle.py stays close to the recovered build.
"""
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.request
from urllib.parse import parse_qs, unquote

import numpy as np

TOKEN = secrets.token_urlsafe(18)       # gate for anything that returns file contents
UPDATE_URL = 'https://api.github.com/repos/{repo}/releases/latest'
_ctx = {}
_dupes = None
_cleaner = None
_update = {'checked': 0, 'latest': None, 'url': None, 'notes': ''}

TEXT_EXT = {'txt', 'md', 'markdown', 'log', 'csv', 'tsv', 'json', 'xml', 'yaml', 'yml', 'toml', 'ini',
            'cfg', 'conf', 'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'css', 'scss', 'html', 'htm',
            'php', 'java', 'c', 'h', 'cpp', 'hpp', 'cs', 'go', 'rs', 'rb', 'sh', 'bat', 'ps1', 'sql',
            'liquid', 'vue', 'svelte', 'swift', 'kt', 'lua', 'r', 'env', 'gitignore', 'srt', 'vtt'}
INLINE_EXT = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif',
              'webp': 'image/webp', 'avif': 'image/avif', 'bmp': 'image/bmp', 'svg': 'image/svg+xml',
              'ico': 'image/x-icon', 'mp4': 'video/mp4', 'webm': 'video/webm', 'm4v': 'video/mp4',
              'mov': 'video/mp4', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'wav': 'audio/wav',
              'ogg': 'audio/ogg', 'opus': 'audio/ogg', 'flac': 'audio/flac', 'aac': 'audio/aac',
              'pdf': 'application/pdf'}

def init(engine, cat_dir, data_dir, version, repo=None, res=None):
    global _dupes, _cleaner
    _ctx.update(engine=engine, cat_dir=cat_dir, data_dir=data_dir, version=version, repo=repo,
                res=res or os.path.dirname(os.path.abspath(__file__)))
    import dupes
    import cleanup
    _dupes = dupes.DupeScan(os.path.join(data_dir, 'hashes.db'))
    _cleaner = cleanup.Cleaner()
    import license
    license.init(data_dir)
    threading.Thread(target=_upkeep, daemon=True, name='upkeep').start()

# -- settings ------------------------------------------------------------------------
DEFAULTS = {
    'hideSystem': True,          # Windows, Program Files, AppData... out of results
    'hideHidden': True,          # dot-folders and dot-files (.git, .cache, .gitignore)
    'excludeDirs': [],           # folders never shown in results
    'hotkey': 'ctrl+shift+space',
    'hotkeyOpens': 'bar',        # bar: quick search bar | window: the full Riffle window
    'animation': True,           # the summon riffle
    'theme': 'system',           # system | light | dark
    'previewOnStart': False,
    'homeRecent': True,          # empty search shows recently changed files
    'quickPreview': True,        # quick bar shows a preview of the selected file
    'quickLook': 'paper',        # paper | glass
    'quickTint': 40,             # Glass transparency, 0-100
    'quickStart': 'recent',      # what the empty quick bar shows: empty | recent | last | both
    'quickApps': True,           # quick bar: apps
    'quickSettings': True,       # quick bar: Windows settings pages
    'quickHistory': True,        # quick bar: browser history + bookmarks (read on this PC only)
    'quickWeb': True,            # quick bar: open addresses, web search row, shortcuts
    'searchEngine': 'google',    # google | bing | duckduckgo
    'updates': True,             # daily check for a new version
    'dupesMin': 1048576,
    'cleanDownloadsDays': 90,
    'cleanBigMB': 500,
    'cleanInstallerDays': 14,
    'autoClean': False,          # Pro: monthly temp + cache clean-up
    'autoCleanLast': 0,
    'autoCleanFreed': 0,
}
HOTKEYS = ['ctrl+shift+space', 'alt+space', 'ctrl+space', 'ctrl+alt+space', 'win+shift+f',
           'ctrl+alt+f', 'ctrl+shift+f', 'ctrl+`', 'f8', 'f9']
_settings = {}
_settings_lock = threading.Lock()

def _settings_path():
    return os.path.join(_ctx.get('data_dir') or os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'Riffle'),
                        'settings.json')

def settings():
    with _settings_lock:
        if not _settings:
            _settings.update(DEFAULTS)
            try:
                with open(_settings_path(), encoding='utf-8') as f:
                    saved = json.load(f)
                _settings.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
        return dict(_settings)

def _clean_setting(k, v):
    """Validate one incoming value; raise ValueError if it doesn't fit."""
    d = DEFAULTS[k]
    if isinstance(d, bool):
        return bool(v)
    if isinstance(d, int):
        v = int(v)
        lo, hi = {'quickTint': (0, 100), 'dupesMin': (0, 1 << 40), 'cleanDownloadsDays': (1, 3650), 'cleanBigMB': (10, 1 << 20),
                  'cleanInstallerDays': (0, 3650), 'autoCleanLast': (0, 1 << 40), 'autoCleanFreed': (0, 1 << 60)}[k]
        return max(lo, min(hi, v))
    if k == 'excludeDirs':
        return [str(x).strip() for x in v if str(x).strip()][:200]
    if k == 'hotkey':
        v = str(v).lower().replace(' ', '')
        if v not in HOTKEYS:
            raise ValueError('unknown hotkey')
        return v
    allowed = {'theme': ('system', 'light', 'dark'), 'hotkeyOpens': ('bar', 'window'),
               'quickLook': ('paper', 'glass'), 'searchEngine': ('google', 'bing', 'duckduckgo'),
               'quickStart': ('empty', 'recent', 'last', 'both')}[k]
    if v not in allowed:
        raise ValueError(f'bad value for {k}')
    return v

def save_settings(patch):
    before = settings()
    clean = {}
    for k, v in (patch or {}).items():
        if k in DEFAULTS:
            clean[k] = _clean_setting(k, v)
    with _settings_lock:
        _settings.update(clean)
        tmp = _settings_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_settings, f, indent=1)
        os.replace(tmp, _settings_path())
        after = dict(_settings)
    if after['hotkey'] != before['hotkey'] and _ctx.get('restart_hotkey'):
        _ctx['restart_hotkey']()
        time.sleep(0.3)                     # let the new registration report back
    return after

def settings_view():
    """Everything the Settings dialog shows."""
    eng = _ctx['engine']
    ix = eng.ix
    cache = os.path.join(_ctx['data_dir'], 'riffle-index.bin')
    hk = _ctx.get('hotkey_state', {})
    drives = [f'{c}:\\' for c in 'CDEFGHIJKLMNOPQRSTUVWXYZ' if os.path.isdir(f'{c}:\\')]
    try:
        auto = autostart()
    except OSError:
        auto = False
    import sys
    return {
        'settings': settings(), 'hotkeys': HOTKEYS,
        'hotkey': {'combo': hk.get('combo', ''), 'ok': hk.get('ok', False)},
        'index': {'count': int(ix.n - ix.ndead), 'roots': ix.roots, 'drives': drives,
                  'state': eng.state, 'message': eng.message,
                  'savedAt': getattr(eng, 'saved_at', 0),
                  'bytes': os.path.getsize(cache) if os.path.exists(cache) else 0},
        'autostart': auto, 'canAutostart': bool(getattr(sys, 'frozen', False)),
        'version': _ctx.get('version'), 'dataDir': _ctx['data_dir'],
        'visualPack': 'installed' if not pack_needed() else _pack['state'],
        'voicePack': 'installed' if voice_models() else dict(_packs['voice']),
    }

def inject(html: bytes) -> bytes:
    """Hand the page its token and version."""
    tag = (f'<meta name="riffle-token" content="{TOKEN}">'
           f'<meta name="riffle-version" content="{_ctx.get("version", "")}">').encode()
    return html.replace(b'<head>', b'<head>' + tag, 1)

def records(min_size=0):
    """Snapshot of indexed files as a generator factory of (path, size, mtime)."""
    eng = _ctx['engine']
    with eng.lock:
        ix = eng.ix
        n = ix.n
        keep = np.flatnonzero(~ix.dead[:n] & (ix.cat[:n] != _ctx['cat_dir']) & (ix.size[:n] >= min_size))
        names = [ix.name(int(i)) for i in keep]
        parents = ix.parent[keep].tolist()
        sizes = ix.size[keep].tolist()
        mtimes = ix.mtime[keep].tolist()
        dirs = ix.dirs
    def gen():
        for name, p, s, m in zip(names, parents, sizes, mtimes):
            d = dirs[p]
            yield (d + ('' if d.endswith('\\') else '\\') + name, s, m)
    return gen

def junk_dirs():
    """Indexed folders named like rebuildable dev output (node_modules, venv...)."""
    import cleanup
    eng = _ctx['engine']
    names = set(cleanup.DEV_JUNK)
    with eng.lock:
        ix = eng.ix
        n = ix.n
        ids = np.flatnonzero(~ix.dead[:n] & (ix.cat[:n] == _ctx['cat_dir']))
        return [ix.path(i) for i in ids.tolist() if ix.name(i).lower() in names]

def _trusted(h):
    """File-content routes: token plus a same-origin fetch (blocks <img> probes from web pages)."""
    q = parse_qs(h.path.partition('?')[2])
    site = h.headers.get('Sec-Fetch-Site', 'same-origin')
    return q.get('t', [''])[0] == TOKEN and site in ('same-origin', 'none')

def _qpath(h):
    return unquote(parse_qs(h.path.partition('?')[2]).get('p', [''])[0])

def _ext(p):
    return os.path.splitext(p)[1][1:].lower()

def _send_file(h, path, ctype):
    """Serve a file with single-range support so video/audio can seek."""
    size = os.path.getsize(path)
    start, end = 0, size - 1
    rng = h.headers.get('Range', '')
    if rng.startswith('bytes='):
        a, _, b = rng[6:].partition('-')
        try:
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(0, size - int(b))
        except ValueError:
            start, end = 0, size - 1
    length = max(0, end - start + 1)
    h.send_response(206 if rng and length < size else 200)
    h.send_header('Content-Type', ctype)
    h.send_header('Content-Length', str(length))
    h.send_header('Accept-Ranges', 'bytes')
    h.send_header('Cache-Control', 'private, max-age=300')
    h.send_header('X-Content-Type-Options', 'nosniff')
    h.send_header('Content-Security-Policy', "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'")
    if rng and length < size:
        h.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    h.end_headers()
    with open(path, 'rb') as f:
        f.seek(start)
        left = length
        while left > 0:
            chunk = f.read(min(left, 256 * 1024))
            if not chunk:
                break
            try:
                h.wfile.write(chunk)
            except (ConnectionError, OSError):
                return
            left -= len(chunk)

def _online(path):
    import cloud
    return cloud.online_only(path)

def _peek_text(path, limit=96 * 1024):
    with open(path, 'rb') as f:
        raw = f.read(limit + 1)
    if b'\0' in raw[:4096]:
        return None, False
    for enc in ('utf-8', 'utf-16', 'cp1252'):
        try:
            return raw[:limit].decode(enc), len(raw) > limit
        except UnicodeDecodeError:
            continue
    return raw[:limit].decode('utf-8', 'replace'), len(raw) > limit

def _json(h, obj, code=200):
    h._send(code, json.dumps(obj))
    return True

# -- GET ---------------------------------------------------------------------------
def get(h, path):
    if path in ('/api/icon', '/api/thumb', '/api/file'):
        if not _trusted(h):
            h._send(403, 'forbidden', 'text/plain')
            return True
        p = _qpath(h)
        if not os.path.isfile(p) and not (path == '/api/icon' and os.path.isdir(p)):
            h._send(404, 'missing', 'text/plain')
            return True
        if path == '/api/file':
            ctype = INLINE_EXT.get(_ext(p))
            if not ctype:
                h._send(415, 'not previewable', 'text/plain')
                return True
            if _online(p) and 'fetch=1' not in h.path:
                h._send(409, 'online only', 'text/plain')
                return True
            _send_file(h, p, ctype)
            return True
        import shellthumb
        q = parse_qs(h.path.partition('?')[2])
        size = max(16, min(1024, int(q.get('s', ['256'])[0])))
        if path == '/api/icon':
            png = shellthumb.icon(p, _ext(p) if os.path.isfile(p) else '<dir>', size)
        elif _online(p):      # cloud-only: a cached thumbnail or the icon, never a download
            png = shellthumb.image(p, size, cache_only=True) or shellthumb.image(p, size, icon_only=True)
        else:
            png = shellthumb.image(p, size)
        if not png:
            h._send(404, 'no image', 'text/plain')
            return True
        h.send_response(200)
        h.send_header('Content-Type', 'image/png')
        h.send_header('Content-Length', str(len(png)))
        h.send_header('Cache-Control', 'private, max-age=86400')
        h.end_headers()
        h.wfile.write(png)
        return True
    if path == '/api/dupes':
        return _json(h, _dupes.status(full=True))
    if path == '/api/dupes/status':
        return _json(h, _dupes.status())
    if path == '/api/cleanup':
        return _json(h, _cleaner.summary())
    if path == '/api/update':
        return _json(h, check_update())
    if path == '/api/autostart':
        return _json(h, {'on': autostart()})
    if path == '/api/settings':
        return _json(h, settings_view())
    if path == '/api/license':
        return _json(h, license_view())
    if path == '/api/inbox':                       # a query handed over by the quick bar, once
        q, _ctx['inbox'] = _ctx.get('inbox'), None
        return _json(h, {'q': q})
    if path == '/api/visual/status' and pack_needed():
        return _json(h, {'available': False, 'pack': dict(_pack), 'suggested': []})
    if path.startswith('/ui/'):
        name = path[4:]
        if name not in STATIC:
            h._send(404, 'not found', 'text/plain')
            return True
        with open(os.path.join(_ctx['res'], *name.split('/')), 'rb') as f:
            data = f.read()
        h.send_response(200)
        h.send_header('Content-Type', STATIC[name])
        h.send_header('Content-Length', str(len(data)))
        h.send_header('Cache-Control', 'no-cache')
        h.end_headers()
        h.wfile.write(data)
        return True
    return False

STATIC = {'ui-extra.css': 'text/css; charset=utf-8', 'ui-extra.js': 'text/javascript; charset=utf-8',
          'ui-settings.css': 'text/css; charset=utf-8', 'ui-settings.js': 'text/javascript; charset=utf-8',
          'ui-insights.css': 'text/css; charset=utf-8', 'ui-insights.js': 'text/javascript; charset=utf-8',
          'ui-cloud.css': 'text/css; charset=utf-8', 'ui-cloud.js': 'text/javascript; charset=utf-8',
          'ui-v2.css': 'text/css; charset=utf-8', 'ui-v2.js': 'text/javascript; charset=utf-8',
          'ui-pro.css': 'text/css; charset=utf-8', 'ui-pro.js': 'text/javascript; charset=utf-8',
          'fonts/fonts.css': 'text/css; charset=utf-8',
          'fonts/instrument-sans.woff2': 'font/woff2', 'fonts/dm-mono-300.woff2': 'font/woff2',
          'fonts/dm-mono-400.woff2': 'font/woff2', 'fonts/dm-mono-500.woff2': 'font/woff2'}

# -- start with Windows ------------------------------------------------------------
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'

def _launch_cmd():
    import sys
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" --no-window'
    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(os.path.join(_ctx["res"], "riffle.py"))}" --no-window'

def autostart(on=None):
    """Read, or (installed builds only) set, the start-with-Windows entry.
    A copy run from source never writes it, so testing can't redirect the
    real app's startup entry to a dev folder."""
    import sys
    import winreg
    if not getattr(sys, 'frozen', False):
        on = None
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if on is True:
            winreg.SetValueEx(k, 'Riffle', 0, winreg.REG_SZ, _launch_cmd())
        elif on is False:
            try:
                winreg.DeleteValue(k, 'Riffle')
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(k, 'Riffle')
            return True
        except FileNotFoundError:
            return False

# -- POST --------------------------------------------------------------------------
def post(h, path, body):
    if path == '/api/peek':
        p = body.get('path', '')
        if not os.path.isfile(p):
            return _json(h, {'error': 'File not found'})
        st = os.stat(p)
        info = {'size': st.st_size, 'mtime': st.st_mtime, 'ctime': st.st_ctime,
                'kind': mimetypes.guess_type(p)[0] or ''}
        ext = _ext(p)
        import cloud
        prov = cloud.provider(p)
        if prov:
            info['cloud'] = prov
            # Reading an online-only file downloads it: wait until the user asks.
            if cloud.online_only(p, prov) and not body.get('fetch'):
                info['online'] = True
                return _json(h, info)
        if ext in INLINE_EXT:
            info['inline'] = INLINE_EXT[ext].split('/')[0] if ext != 'pdf' else 'pdf'
        elif ext in TEXT_EXT or (st.st_size < 2_000_000 and not ext):
            try:
                text, more = _peek_text(p)
            except OSError as e:
                return _json(h, {**info, 'error': str(e)})
            if text is not None:
                info.update(inline='text', text=text, more=more)
        return _json(h, info)
    if path == '/api/dupes/scan':
        if body.get('stop'):
            _dupes.stop()
            return _json(h, _dupes.status())
        min_size = int(body.get('minSize', 1 << 20))
        roots = body.get('roots') or None
        skip = ('\\appdata\\', '\\windows\\', '\\program files', '\\programdata\\', '\\$recycle.bin\\',
                '\\node_modules\\', '\\.venv\\', '\\site-packages\\', '\\.git\\')
        _dupes.start(records(min_size), min_size=min_size, roots=roots,
                     skip=() if body.get('everything') else skip)
        return _json(h, _dupes.status())
    if path == '/api/dupes/remove':
        # Only files from the latest scan, and never the last copy of a set.
        groups = {f['path'].lower(): g for g in _dupes.state['groups'] for f in g['files']}
        if not pro():
            sets = {id(groups[p.lower()]) for p in body.get('paths', []) if p.lower() in groups}
            if len(sets) > 1:
                return _json(h, {'error': 'pro', 'feature': 'dupes-bulk'}, 402)
        want = {p.lower() for p in body.get('paths', [])}
        ok = []
        for p in body.get('paths', []):
            g = groups.get(p.lower())
            if not g:
                continue
            left = [f for f in g['files'] if f['path'].lower() not in want and os.path.exists(f['path'])]
            if left:
                ok.append(p)
        import cleanup
        freed = sum(os.path.getsize(p) for p in ok if os.path.exists(p))
        done = cleanup.recycle(ok)
        if done:
            gone = {p.lower() for p in ok}
            for g in _dupes.state['groups']:
                g['files'] = [f for f in g['files'] if f['path'].lower() not in gone]
            _dupes.state['groups'] = [g for g in _dupes.state['groups'] if len(g['files']) > 1]
        return _json(h, {'freed': freed if done else 0, 'removed': len(ok) if done else 0,
                         'refused': len(body.get('paths', [])) - len(ok)})
    if path == '/api/cleanup/scan':
        _cleaner.scan(records(0), _dupes.state['groups'], junk_dirs, settings())
        return _json(h, _cleaner.summary())
    if path == '/api/cleanup/items':
        return _json(h, {'items': _cleaner.items(body.get('key', ''))})
    if path == '/api/cleanup/remove':
        if body.get('emptyBin'):
            return _json(h, _cleaner.empty_bin())
        if not pro():
            card_of = getattr(_cleaner, 'card_of', {})
            if any(card_of.get(os.path.normcase(p)) not in FREE_CARDS for p in body.get('paths', [])):
                return _json(h, {'error': 'pro', 'feature': 'clean-all'}, 402)
        return _json(h, _cleaner.remove(body.get('paths', [])))
    if path == '/api/license/activate':
        import license
        return _json(h, license.activate(str(body.get('key', ''))))
    if path == '/api/license/deactivate':
        import license
        return _json(h, license.deactivate())
    if path == '/api/license/buy':
        import license, webbrowser
        webbrowser.open(license.BUY_URL)
        return _json(h, {'ok': True})
    if path == '/api/autostart':
        return _json(h, {'on': autostart(bool(body.get('on')))})
    if path == '/api/settings':
        try:
            save_settings(body.get('settings') or {})
        except (ValueError, TypeError) as e:
            return _json(h, {'error': str(e)}, 400)
        if 'autostart' in body:
            try:
                autostart(bool(body['autostart']))
            except OSError:
                pass
        if body.get('roots'):
            roots = [r for r in body['roots'] if isinstance(r, str) and os.path.isdir(r)]
            if roots and _ctx.get('reindex'):
                _ctx['reindex'](roots)
        if body.get('clearHashes'):
            _dupes.cache.clear()
        return _json(h, settings_view())
    if path == '/api/visual/install':
        pack_install()
        return _json(h, {'pack': dict(_pack)})
    if path == '/api/voice/install':
        pack_install('voice')
        return _json(h, {'pack': dict(_packs['voice'])})
    if path == '/api/update/open':
        u = check_update()
        if u.get('url', '') and u['url'].startswith('https://github.com/'):
            import webbrowser
            webbrowser.open(u['url'])
        return _json(h, {'ok': True})
    if path == '/api/insights':
        d = insights(bool(body.get('all')), body.get('root') or None)
        rows = history()
        return _json(h, {**d, 'history': rows if pro() else [], 'historyLocked': not pro(), 'historyDays': len(rows)})
    if path == '/api/autoclean':
        if not pro():
            return _json(h, {'error': 'pro', 'feature': 'auto'}, 402)
        if 'on' in body:
            save_settings({'autoClean': bool(body['on'])})
        if body.get('now'):
            freed = auto_clean(force=True)
            return _json(h, {**_auto_state(), 'freedNow': freed or 0})
        return _json(h, _auto_state())
    if path == '/api/spacemap':
        import cleanup
        root = body.get('root') or 'C:\\'
        return _json(h, cleanup.space_map(records(0), root, depth=int(body.get('depth', 3))))
    return False

# -- insights (analytics) ------------------------------------------------------------
# Every number comes from the index in one numpy pass: no file is opened.
FILE_CATS = ['document', 'image', 'video', 'audio', 'code', 'archive', 'other']
_insights = {}

def _locations(dirs_lower, home):
    """Name the place each directory belongs to: Desktop, Downloads, D:\\ ..."""
    named = [(os.path.join(home, d).lower(), d) for d in
             ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music')]
    onedrive = os.path.join(home, 'onedrive').lower()
    home_l = home.lower()
    out = []
    for d in dirs_lower:
        label = None
        for p, n in named:
            if d == p or d.startswith(p + '\\'):
                label = n
                break
        if label is None:
            if d.startswith(onedrive):
                label = 'OneDrive'
            elif d == home_l or d.startswith(home_l + '\\'):
                label = 'Rest of your user folder'
            elif d[1:3] == ':\\' and d[:1] != 'c':
                label = d[:3].upper()
            else:
                label = 'Elsewhere on C:\\'
        out.append(label)
    return out

def insights(show_all=False, root=None):
    eng = _ctx['engine']
    cats = _ctx['cats']
    ix = eng.ix
    key = (ix.gen, id(ix), bool(show_all), root)
    if _insights.get('key') == key:
        return _insights['data']
    t0 = time.perf_counter()
    filt = {} if show_all else {'hideSystem': True, 'hideHidden': True}
    with eng.lock:
        ix = eng.ix
        everything = eng._raw_hits('', 'substring')
        ids = eng._apply(everything, {**filt, 'kind': 'files'})
        nfolders = int(eng._apply(everything, {**filt, 'kind': 'folders'}).size)
        size = ix.size[ids].astype(np.int64)
        mtime = ix.mtime[ids]
        cat = ix.cat[ids]
        extv = ix.extv[ids]
        par = ix.parent[ids]
        dirs = ix.dirs
        exts = list(ix.exts)
        ndirs = len(dirs)
    now = time.time()
    cat_names = [cats[i] for i in range(len(cats))]
    file_slot = {c: i for i, c in enumerate(FILE_CATS)}
    cat_slot = np.array([file_slot.get(c, 6) for c in cat_names], dtype=np.int64)[cat]   # folder never occurs here

    # totals
    week = mtime >= now - 7 * 86400
    big = np.argsort(-size)[:20] if size.size else np.zeros(0, dtype=np.int64)
    def path_of(k):
        d = dirs[int(par[k])]
        return d + ('' if d.endswith('\\') else '\\') + ix.name(int(ids[k]))
    data = {
        'all': bool(show_all),
        'totals': {'files': int(ids.size), 'folders': nfolders, 'bytes': int(size.sum()),
                   'weekFiles': int(week.sum()), 'weekBytes': int(size[week].sum())},
        'biggest': [{'path': path_of(k), 'size': int(size[k]), 'mtime': int(mtime[k]),
                     'cat': FILE_CATS[int(cat_slot[k])]} for k in big.tolist()],
    }
    # by type
    tf = np.bincount(cat_slot, minlength=7)
    tb = np.bincount(cat_slot, weights=size, minlength=7)
    data['types'] = [{'cat': c, 'files': int(tf[i]), 'bytes': int(tb[i])} for i, c in enumerate(FILE_CATS)]
    # by extension (top 48 by space)
    ef = np.bincount(extv, minlength=len(exts))
    eb = np.bincount(extv, weights=size, minlength=len(exts))
    ec = np.zeros(len(exts), dtype=np.int64)
    if ids.size:
        ec[extv] = cat_slot                       # any file of that extension tells its type
    top = [int(i) for i in np.argsort(-eb)[:48] if eb[i] > 0]
    data['exts'] = [{'ext': exts[i] or '(none)', 'cat': FILE_CATS[int(ec[i])], 'files': int(ef[i]),
                     'bytes': int(eb[i])} for i in top]
    # by location (per-directory labels cached until the folder list changes)
    loc = _insights.get('loc')
    if not loc or loc[0] is not dirs or len(loc[1]) != ndirs:
        labels = _locations([d.lower() for d in dirs[:ndirs]], os.path.expanduser('~'))
        names = sorted(set(labels))
        idx = {n: i for i, n in enumerate(names)}
        loc = (dirs, np.array([idx[l] for l in labels], dtype=np.int64), names)
        _insights['loc'] = loc
    lid = loc[1][par] if ids.size else np.zeros(0, dtype=np.int64)
    lf = np.bincount(lid, minlength=len(loc[2]))
    lb = np.bincount(lid, weights=size, minlength=len(loc[2]))
    home_dir = os.path.expanduser('~')
    def loc_path(n):
        if n in ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music', 'OneDrive'):
            return os.path.join(home_dir, n)
        if n == 'Rest of your user folder':
            return home_dir
        return n if n.endswith(':\\') and n != 'Elsewhere on C:\\' else ''
    data['locations'] = sorted(({'label': n, 'path': loc_path(n), 'files': int(lf[i]), 'bytes': int(lb[i])}
                                for i, n in enumerate(loc[2]) if lf[i]), key=lambda x: -x['bytes'])
    # age of data (by last change)
    age = (now - mtime) / 86400
    edges = [(7, 'This week'), (30, 'This month'), (182, 'Last 6 months'), (365, 'This year'),
             (730, '1 to 2 years'), (1e9, 'Older than 2 years')]
    lo, buckets = -1e9, []
    for hi, label in edges:
        m = (age > lo) & (age <= hi)
        buckets.append({'label': label, 'files': int(m.sum()), 'bytes': int(size[m].sum())})
        lo = hi
    data['age'] = buckets
    # activity: files changed per day, last 53 weeks
    today = int(now // 86400)
    days = today - (mtime // 86400)
    recent = days[(days >= 0) & (days < 371)]
    data['activity'] = {'today': today, 'counts': np.bincount(370 - recent, minlength=371).tolist()}
    # busiest folders (files directly inside)
    pf = np.bincount(par, minlength=ndirs) if ids.size else np.zeros(0)
    pb = np.bincount(par, weights=size, minlength=ndirs) if ids.size else np.zeros(0)
    data['folders'] = [{'path': dirs[int(i)], 'files': int(pf[i]), 'bytes': int(pb[i])}
                       for i in np.argsort(-pf)[:15].tolist() if pf[i]]
    data['findings'] = _findings(eng, ix, ids, size, mtime, extv, par, dirs, ndirs, exts, cat_slot, now)
    # folder map: two levels under root, coloured by each block's main type
    home = os.path.expanduser('~')
    root = root or (home if not show_all else (ix.roots[0] if ix.roots else 'C:\\'))
    data['map'] = _folder_map(dirs, ndirs, par, size, cat_slot, root)
    data['ms'] = round((time.perf_counter() - t0) * 1000)
    _insights.update(key=key, data=data)
    return data

def _findings(eng, ix, ids, size, mtime, extv, par, dirs, ndirs, exts, cat_slot, now):
    """Plain, checkable things worth a look, biggest first. Each has the files behind it."""
    if not ids.size:
        return []
    home = os.path.expanduser('~')
    dl = os.path.join(home, 'Downloads').lower()
    dlow = [d.lower() for d in dirs[:ndirs]]
    in_dl = np.array([d == dl or d.startswith(dl + '\\') for d in dlow], dtype=bool)[par]
    ext_ids = lambda names: np.array([i for i, e in enumerate(exts) if e in names], dtype=np.int64)
    out = []

    def add(key, title, why, mask, action, order=None):
        k = np.flatnonzero(mask)
        if not k.size:
            return
        b = int(size[k].sum())
        if b < 50 << 20:                            # under 50 MB is not worth anyone's time
            return
        top = k[np.argsort(-size[k])[:6]]
        out.append({'key': key, 'title': title, 'why': why, 'bytes': b, 'files': int(k.size), 'action': action,
                    'items': [{'path': dirs[int(par[j])].rstrip('\\') + '\\' + ix.name(int(ids[j])),
                               'size': int(size[j]), 'mtime': int(mtime[j])} for j in top.tolist()]})

    # 1. archives you already unzipped: a folder with the same name sits right next to the file
    arc = np.flatnonzero(np.isin(extv, ext_ids({'zip', 'rar', '7z'})))
    have = set(dlow)
    unz = np.zeros(ids.size, dtype=bool)
    for j in arc.tolist():
        stem = os.path.splitext(ix.name(int(ids[j])))[0].lower()
        if dlow[int(par[j])].rstrip('\\') + '\\' + stem in have:
            unz[j] = True
    add('unzipped', 'Zip files you already unzipped',
        'A folder with the same name sits right next to each one, so the zip is a spare copy.',
        unz, {'exts': ['zip', 'rar', '7z'], 'sort': 'size'})
    # 2. downloads nobody touched in 3 months
    add('olddl', 'Old downloads', 'Files in Downloads that haven’t changed in 3 months.',
        in_dl & (mtime < now - 90 * 86400), {'scope': os.path.join(home, 'Downloads'), 'timeMax': int(now - 90 * 86400)})
    # 3. installers that already did their job
    add('installers', 'Old installers', 'Setup files in Downloads older than 2 weeks. The apps they installed stay.',
        in_dl & np.isin(extv, ext_ids({'exe', 'msi', 'msix', 'appx'})) & (mtime < now - 14 * 86400),
        {'scope': os.path.join(home, 'Downloads'), 'exts': ['exe', 'msi', 'msix', 'appx']})
    # 4. screenshots piling up
    shot = np.isin(ids, eng._ids_for(b'screenshot')) | np.isin(ids, eng._ids_for(b'screen shot'))
    add('shots', 'Screenshots', 'Screenshots saved on this PC.',
        shot & (cat_slot == FILE_CATS.index('image')), {'q': 'screenshot', 'cats': ['image']})
    # 5. big videos untouched for a year
    add('oldvideo', 'Big videos you haven’t touched in a year', 'Videos over 500 MB, unchanged for 12 months.',
        (cat_slot == FILE_CATS.index('video')) & (size > 500 << 20) & (mtime < now - 365 * 86400),
        {'cats': ['video'], 'timeMax': int(now - 365 * 86400), 'sizeMin': 500 << 20})
    out.sort(key=lambda f: -f['bytes'])
    return out

def _folder_map(dirs, ndirs, par, size, cat_slot, root):
    rl = root.lower().rstrip('\\') + '\\'
    n = len(rl)
    # bytes per (directory, type)
    per = np.bincount(par * 7 + cat_slot, weights=size, minlength=ndirs * 7).reshape(ndirs, 7) if par.size \
        else np.zeros((ndirs, 7))
    used = np.flatnonzero(per.sum(axis=1) > 0)
    tree = {}
    for i in used.tolist():
        d = dirs[i]
        dl = d.lower() + '\\'
        if not dl.startswith(rl) and d.lower().rstrip('\\') + '\\' != rl:
            continue
        parts = d[n:].split('\\') if len(d) > n else []
        k1 = parts[0] if parts and parts[0] else '(files here)'
        k2 = parts[1] if len(parts) > 1 else '(files here)'
        a = tree.setdefault(k1, {'v': np.zeros(7), 'kids': {}})
        a['v'] += per[i]
        b = a['kids'].setdefault(k2, np.zeros(7))
        b += per[i]
    def node(name, v, path):
        return {'name': name, 'path': path, 'bytes': int(v.sum()), 'cat': FILE_CATS[int(np.argmax(v))]}
    kids = []
    for k1, a in sorted(tree.items(), key=lambda kv: -kv[1]['v'].sum())[:16]:
        p1 = root.rstrip('\\') + '\\' + k1 if k1 != '(files here)' else root
        sub = sorted(a['kids'].items(), key=lambda kv: -kv[1].sum())
        ch = [node(k2, v, p1.rstrip('\\') + '\\' + k2 if k2 != '(files here)' else p1) for k2, v in sub[:10]]
        rest = sum((v for _, v in sub[10:]), np.zeros(7))
        if rest.sum():
            ch.append({**node(f'{len(sub) - 10} more', rest, p1), 'more': True})
        kids.append({**node(k1, a['v'], p1), 'kids': ch})
    return {'root': root, 'bytes': int(sum(k['bytes'] for k in kids)), 'kids': kids}

_vlight = {}

def visual_status_light(visual_dir):
    """Visual-search status read straight from its sqlite file, without importing the
    AI stack. Status checks must never wake the model or start a folder scan."""
    import sqlite3
    db = os.path.join(visual_dir, 'lens.db')
    if not os.path.exists(db):
        return None
    stamp = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in (db, db + '-wal'))
    if _vlight.get('stamp') == stamp:
        return _vlight['data']                     # unchanged since the last check: no query
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)
        try:
            images, videos = con.execute(
                "SELECT COUNT(DISTINCT CASE WHEN kind='image' THEN path END), "
                "COUNT(DISTINCT CASE WHEN kind='video' THEN path END) FROM items").fetchone()
            counts = dict(con.execute("SELECT src, COUNT(DISTINCT path) FROM items GROUP BY src").fetchall())
            cols = [r[1] for r in con.execute("PRAGMA table_info(folders)").fetchall()]
            rows = con.execute("SELECT path, label FROM folders ORDER BY path" if 'label' in cols
                               else "SELECT path, '' FROM folders ORDER BY path").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    folders = [{'path': f, 'label': label or '', 'count': counts.get(f, 0), 'error': None,
                'kind': 'drive' if f.startswith('https://drive.google.com') else 'local'} for f, label in rows]
    home = os.path.expanduser('~')
    sugg = [p for p in (os.path.join(home, d) for d in ('Pictures', 'Downloads', 'Desktop', 'Videos'))
            if os.path.isdir(p) and p not in {x['path'] for x in folders}]
    data = {'available': True, 'pro': pro(), 'images': images, 'videos': videos, 'folders': folders,
            'index': {'running': False}, 'engine': 'asleep (loads on next search)', 'verify': True,
            'google_key': False, 'suggested': sugg, 'asleep': True}
    _vlight.update(stamp=stamp, data=data)
    return data

# -- optional packs: visual search, voice search -------------------------------------
# The installer ships without the heavy libraries. Each pack is one zip attached to a
# GitHub release (site/ libraries + models/), fetched only when someone wants it.
#   visual: OpenCV, OpenVINO, Pillow, DINOv2 (~120 MB)
#   voice:  sherpa-onnx, sounddevice, a streaming English model (~75 MB)
PACKS = {'visual': 'riffle-visual-pack.zip', 'voice': 'riffle-voice-pack.zip'}
PACK_NAME = PACKS['visual']
PACK_DLL_DIRS = {'visual': ('openvino\\libs', 'openvino', 'cv2', 'PIL'),
                 'voice': ('sherpa_onnx\\lib', '_sounddevice_data\\portaudio-binaries')}
_packs = {n: {'state': 'idle', 'got': 0, 'total': 0, 'error': ''} for n in PACKS}
_pack = _packs['visual']

def pack_dir(name='visual'):
    return os.path.join(_ctx['data_dir'], 'addons', name)

def pack_needed(name='visual'):
    """True when this build has no libraries for `name` and its pack isn't installed."""
    import sys
    if not getattr(sys, 'frozen', False):
        return False                      # running from source: use the normal site-packages
    return not os.path.exists(os.path.join(pack_dir(name), 'ok'))

def pack_activate(name='visual'):
    """Put the pack on the import path. Returns its models folder, or None."""
    import sys
    d = pack_dir(name)
    if not os.path.exists(os.path.join(d, 'ok')):
        return None
    site = os.path.join(d, 'site')
    if site not in sys.path:
        sys.path.insert(0, site)
        # the pack's DLLs need the C++ runtime the app ships in _internal; a PC without the
        # Visual C++ redistributable only finds it if that folder is on the DLL search path
        if getattr(sys, '_MEIPASS', None):
            os.add_dll_directory(sys._MEIPASS)
        for sub in PACK_DLL_DIRS[name]:
            p = os.path.join(site, sub)
            if os.path.isdir(p):
                os.add_dll_directory(p)
    return os.path.join(d, 'models')

def _pack_assets(name='visual'):
    """(zip url, size, sha256 url) from the newest release that carries the pack. A release
    published without it (an app-only update) must not break the feature for new users."""
    url = f"https://api.github.com/repos/{_ctx['repo']}/releases?per_page=15"
    req = urllib.request.Request(url, headers={'User-Agent': 'Riffle', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        releases = json.load(r)
    for rel in releases:
        if rel.get('draft') or rel.get('prerelease'):
            continue
        assets = {a['name']: a for a in rel.get('assets', [])}
        z, sha = assets.get(PACKS[name]), assets.get(PACKS[name] + '.sha256')
        if z and sha:
            return z['browser_download_url'], z.get('size', 0), sha['browser_download_url']
    raise RuntimeError(f'the {name} pack is not on any recent Riffle release')

def pack_install(name='visual'):
    st = _packs[name]
    if st['state'] in ('downloading', 'unpacking'):
        return
    def run():
        import hashlib
        import shutil
        import zipfile
        st.update(state='downloading', got=0, total=0, error='')
        base = os.path.join(_ctx['data_dir'], 'addons')
        os.makedirs(base, exist_ok=True)
        zpath = os.path.join(base, PACKS[name] + '.part')
        try:
            url, size, sum_url = _pack_assets(name)
            with urllib.request.urlopen(urllib.request.Request(sum_url, headers={'User-Agent': 'Riffle'}), timeout=15) as r:
                want = r.read().decode().split()[0].lower()
            st['total'] = size
            h = hashlib.sha256()
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Riffle'}), timeout=30) as r, \
                    open(zpath, 'wb') as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    st['got'] += len(chunk)
            if h.hexdigest() != want:
                raise RuntimeError('download was corrupted (checksum mismatch), please try again')
            st['state'] = 'unpacking'
            tmp = pack_dir(name) + '.new'
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                import sys
                want_py = f'Python {sys.version_info.major}.{sys.version_info.minor}'
                got_py = z.read('PACK.txt').decode('utf-8', 'replace') if 'PACK.txt' in z.namelist() else ''
                if want_py not in got_py:
                    raise RuntimeError(f'this {name} pack is for a different Riffle build ({got_py.strip()}); '
                                       'update Riffle and try again')
                for m in z.namelist():                 # refuse paths escaping the folder
                    if m.startswith(('/', '\\')) or '..' in m.replace('\\', '/').split('/'):
                        raise RuntimeError('bad file in pack')
                z.extractall(tmp)
            open(os.path.join(tmp, 'ok'), 'w').close()
            shutil.rmtree(pack_dir(name), ignore_errors=True)
            os.replace(tmp, pack_dir(name))
            st['state'] = 'done'
            _ctx.get('reset_' + name, lambda: None)()
        except Exception as e:
            st.update(state='error', error=str(e))
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
    threading.Thread(target=run, daemon=True, name=name + '-pack').start()

def voice_models():
    """Folder holding the voice model, or None when voice search isn't installed."""
    import sys
    if getattr(sys, 'frozen', False):
        m = pack_activate('voice')
        return os.path.join(m, 'voice') if m else None
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models', 'voice')
    return here if os.path.exists(os.path.join(here, 'tokens.txt')) else None

# -- Riffle Pro --------------------------------------------------------------------
# Free: search, quick bar, preview, Insights, every scan, removing duplicates one set
# at a time, clearing temp files, caches and the Recycle Bin.
# Pro (or the 14-day trial): visual search, bulk duplicate removal, every clean-up
# card, monthly automatic clean-up, disk growth history in Insights.
FREE_CARDS = {'temp', 'caches', 'bin'}
PRO_FEATURES = [
    ('visual', 'Visual search', 'Find the original of any photo or video, even after crops and filters.'),
    ('dupes', 'One-click duplicate removal', 'Recycle every extra copy at once, across all sets.'),
    ('clean', 'Clean all', 'Clear old installers, downloads, big files and project folders in one go.'),
    ('auto', 'Monthly automatic clean-up', 'Temp files and caches cleared for you once a month.'),
    ('history', 'Disk growth history', 'See how your space changes week by week in Insights.'),
]

def pro():
    import license
    return license.is_pro()

def license_view():
    import license
    s = license.status()
    return {**s, 'features': [{'key': k, 'title': t, 'desc': d} for k, t, d in PRO_FEATURES],
            'prices': {'inr': 699, 'inrLaunch': 499, 'usd': 19, 'usdLaunch': 12},
            'autoClean': _auto_state()}

# -- daily upkeep: licence recheck, Insights history snapshot, automatic clean-up ---
def _history_path():
    return os.path.join(_ctx['data_dir'], 'history.json')

def history():
    try:
        with open(_history_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return []

def _snapshot():
    """One small record per day (totals by type), kept for 3 years. Collected for everyone,
    so the history is already there the day someone gets Pro."""
    day = time.strftime('%Y-%m-%d')
    rows = history()
    if rows and rows[-1].get('day') == day:
        return
    eng = _ctx['engine']
    if eng.state != 'ready' or eng.ix.n == 0:
        return
    d = insights(False)
    rows.append({'day': day, 'bytes': d['totals']['bytes'], 'files': d['totals']['files'],
                 'types': {t['cat']: t['bytes'] for t in d['types']}})
    rows = rows[-1100:]
    tmp = _history_path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rows, f)
    os.replace(tmp, _history_path())

def _auto_state():
    s = settings()
    return {'on': bool(s.get('autoClean')), 'last': s.get('autoCleanLast', 0), 'freed': s.get('autoCleanFreed', 0)}

def auto_clean(force=False):
    """Clear temp files (older than 2 days) and app/browser caches. Monthly, Pro, opt-in."""
    import cleanup
    s = settings()
    if not force and (not s.get('autoClean') or not pro() or time.time() - s.get('autoCleanLast', 0) < 30 * 86400):
        return None
    now = time.time()
    paths = [p for p, _, m in cleanup._walk_files(cleanup.TEMP) if now - m > 2 * 86400]
    for root in list(cleanup.browser_cache_dirs()) + cleanup.APP_CACHE_DIRS:
        if os.path.isdir(root):
            paths += [p for p, _, _ in cleanup._walk_files(root)]
    paths = [p for p in paths if not cleanup.protected(p)]     # never Windows, Program Files or Riffle's own data
    freed = cleanup.delete(paths)
    save_settings({'autoCleanLast': int(now), 'autoCleanFreed': int(freed)})
    return freed

def _upkeep():
    """Runs a few minutes after start, then every 6 hours. Cheap when there's nothing to do."""
    import license
    from dupes import background_priority
    time.sleep(240)
    while True:
        background_priority(True)
        # check_update runs here too, so installs that only use the quick bar still count
        for job in (license.recheck_if_due, check_update, _snapshot, auto_clean):
            try:
                job()
            except Exception as e:
                print(f'[riffle] upkeep {getattr(job, "__name__", job)}: {e}')
        background_priority(False)
        time.sleep(6 * 3600)

# -- updates -------------------------------------------------------------------------
def _vtuple(v):
    return tuple(int(x) for x in v.lstrip('v').split('.') if x.isdigit())

def _update_from_riffle_server():
    """riffle.bliink.in answers with the latest version and counts this install once a day
    (random install id, version and tier only). Returns True when it answered."""
    import license
    from urllib.parse import urlencode
    params = {'v': _ctx.get('version', ''), 'tier': license.status()['tier'], 'iid': license.install_id(),
              'src': license.install_source()}
    first = license.first_ping_pending()
    if first:
        params['first'] = 1                      # counts as a new install, once
    try:
        req = urllib.request.Request(f'{license.SERVER}/api/update.php?{urlencode(params)}', headers={'User-Agent': 'Riffle'})
        with urllib.request.urlopen(req, timeout=8) as r:
            u = json.load(r)
    except Exception:
        return False
    if first:
        license.mark_first_ping()
    if not u.get('latest'):
        return False
    _update.update(latest=str(u['latest']).lstrip('v'), url=u.get('url'), notes=(u.get('notes') or '')[:1200])
    return True

def check_update(force=False):
    """Latest version, asked at most once a day: riffle.bliink.in first, GitHub as a fallback."""
    repo = _ctx.get('repo')
    if not settings().get('updates', True):
        return {'current': _ctx.get('version'), 'available': False}
    if force or time.time() - _update['checked'] > 86400:
        _update['checked'] = time.time()
        if not _update_from_riffle_server() and repo:
          try:
            req = urllib.request.Request(UPDATE_URL.format(repo=repo),
                                         headers={'User-Agent': 'Riffle', 'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(req, timeout=8) as r:
                rel = json.load(r)
            setup = next((a['browser_download_url'] for a in rel.get('assets', [])
                          if a['name'].lower().endswith('.exe')), rel.get('html_url'))
            _update.update(latest=rel.get('tag_name', '').lstrip('v'), url=setup,
                           notes=(rel.get('body') or '')[:1200])
          except Exception:
            pass
    latest = _update['latest']
    newer = bool(latest) and _vtuple(latest) > _vtuple(_ctx.get('version', '0'))
    return {'current': _ctx.get('version'), 'latest': latest, 'available': newer,
            'url': _update['url'] if newer else None, 'notes': _update['notes'] if newer else ''}
