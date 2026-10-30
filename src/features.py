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
            _send_file(h, p, ctype)
            return True
        import shellthumb
        q = parse_qs(h.path.partition('?')[2])
        size = max(16, min(1024, int(q.get('s', ['256'])[0])))
        png = shellthumb.icon(p, _ext(p) if os.path.isfile(p) else '<dir>', size) if path == '/api/icon' \
            else shellthumb.image(p, size)
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
        _cleaner.scan(records(0), _dupes.state['groups'], junk_dirs)
        return _json(h, _cleaner.summary())
    if path == '/api/cleanup/items':
        return _json(h, {'items': _cleaner.items(body.get('key', ''))})
    if path == '/api/cleanup/remove':
        if body.get('emptyBin'):
            return _json(h, _cleaner.empty_bin())
        return _json(h, _cleaner.remove(body.get('paths', [])))
    if path == '/api/autostart':
        return _json(h, {'on': autostart(bool(body.get('on')))})
    if path == '/api/visual/install':
        pack_install()
        return _json(h, {'pack': dict(_pack)})
    if path == '/api/update/open':
        u = check_update()
        if u.get('url', '') and u['url'].startswith('https://github.com/'):
            import webbrowser
            webbrowser.open(u['url'])
        return _json(h, {'ok': True})
    if path == '/api/spacemap':
        import cleanup
        root = body.get('root') or 'C:\\'
        return _json(h, cleanup.space_map(records(0), root, depth=int(body.get('depth', 3))))
    return False

# -- optional visual-search pack -----------------------------------------------------
# The installer ships without OpenCV, OpenVINO, Pillow and the model (~270 MB).
# They come as one zip attached to the GitHub release, fetched only when wanted.
PACK_NAME = 'riffle-visual-pack.zip'
_pack = {'state': 'idle', 'got': 0, 'total': 0, 'error': ''}

def pack_dir():
    return os.path.join(_ctx['data_dir'], 'addons', 'visual')

def pack_needed():
    """True when this build has no visual libraries and the pack isn't installed."""
    import sys
    if not getattr(sys, 'frozen', False):
        return False                      # running from source: use the normal site-packages
    return not os.path.exists(os.path.join(pack_dir(), 'ok'))

def pack_activate():
    """Put the pack on the import path. Returns its models folder, or None."""
    import sys
    d = pack_dir()
    if not os.path.exists(os.path.join(d, 'ok')):
        return None
    site = os.path.join(d, 'site')
    if site not in sys.path:
        sys.path.insert(0, site)
        for sub in ('openvino\\libs', 'openvino', 'cv2', 'PIL'):
            p = os.path.join(site, sub)
            if os.path.isdir(p):
                os.add_dll_directory(p)
    return os.path.join(d, 'models')

def _release_asset(name):
    req = urllib.request.Request(UPDATE_URL.format(repo=_ctx['repo']),
                                 headers={'User-Agent': 'Riffle', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    for a in rel.get('assets', []):
        if a['name'] == name:
            return a['browser_download_url'], a.get('size', 0)
    raise RuntimeError(f'{name} is not attached to the latest release')

def pack_install():
    if _pack['state'] == 'downloading':
        return
    def run():
        import hashlib
        import shutil
        import zipfile
        _pack.update(state='downloading', got=0, total=0, error='')
        base = os.path.join(_ctx['data_dir'], 'addons')
        os.makedirs(base, exist_ok=True)
        zpath = os.path.join(base, PACK_NAME + '.part')
        try:
            url, size = _release_asset(PACK_NAME)
            sum_url, _ = _release_asset(PACK_NAME + '.sha256')
            with urllib.request.urlopen(urllib.request.Request(sum_url, headers={'User-Agent': 'Riffle'}), timeout=15) as r:
                want = r.read().decode().split()[0].lower()
            _pack['total'] = size
            h = hashlib.sha256()
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Riffle'}), timeout=30) as r, \
                    open(zpath, 'wb') as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    _pack['got'] += len(chunk)
            if h.hexdigest() != want:
                raise RuntimeError('download was corrupted (checksum mismatch), please try again')
            _pack['state'] = 'unpacking'
            tmp = pack_dir() + '.new'
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                for m in z.namelist():                 # refuse paths escaping the folder
                    if m.startswith(('/', '\\')) or '..' in m.replace('\\', '/').split('/'):
                        raise RuntimeError('bad file in pack')
                z.extractall(tmp)
            open(os.path.join(tmp, 'ok'), 'w').close()
            shutil.rmtree(pack_dir(), ignore_errors=True)
            os.replace(tmp, pack_dir())
            _pack['state'] = 'done'
            _ctx.get('reset_visual', lambda: None)()
        except Exception as e:
            _pack.update(state='error', error=str(e))
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
    threading.Thread(target=run, daemon=True, name='visual-pack').start()

# -- updates -------------------------------------------------------------------------
def _vtuple(v):
    return tuple(int(x) for x in v.lstrip('v').split('.') if x.isdigit())

def check_update(force=False):
    """Ask GitHub for the latest release, at most once a day."""
    repo = _ctx.get('repo')
    if not repo:
        return {'current': _ctx.get('version'), 'available': False}
    if force or time.time() - _update['checked'] > 86400:
        _update['checked'] = time.time()
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
