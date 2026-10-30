"""Cleanup recommendations. Runs only on request.

Each rule returns a card: what it found, how much space, why it's safe, and how
it is removed ('recycle' = Recycle Bin, undoable; 'delete' = permanent, used
only for caches, temp files and rebuildable dev folders).
Deletion only accepts paths that appeared in the latest report (see Cleaner).
"""
import ctypes
import os
import shutil
import stat
import threading
import time
from collections import defaultdict
from ctypes import wintypes as W

DAY = 86400
HOME = os.path.expanduser('~')
LOCAL = os.environ.get('LOCALAPPDATA', os.path.join(HOME, 'AppData', 'Local'))
TEMP = os.environ.get('TEMP', os.path.join(LOCAL, 'Temp'))
DOWNLOADS = os.path.join(HOME, 'Downloads')
CLOUD = 0x00400000 | 0x00040000 | 0x00001000

def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip('\\') + '\\'

PROTECT = [_norm(p) for p in (
    os.environ.get('WINDIR', r'C:\Windows'),
    os.environ.get('ProgramFiles', r'C:\Program Files'),
    os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    os.environ.get('ProgramData', r'C:\ProgramData'),
    os.path.join(LOCAL, 'Riffle'),
    os.path.join(LOCAL, 'Programs', 'Riffle'),
)]

def protected(path):
    p = _norm(path)
    return any(p.startswith(r) for r in PROTECT) or '\\.git\\' in p or p.count('\\') < 3

# Places a user keeps their own files (for rules that only make sense there)
USER_DIRS = [_norm(os.path.join(HOME, d)) for d in
             ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music')]
SYSTEM_FILES = {'pagefile.sys', 'hiberfil.sys', 'swapfile.sys', 'dumpstack.log.tmp'}
KEEP_EXT_BIG = {'.vhdx', '.vhd', '.vmdk', '.pst', '.ost', '.qcow2', '.ext4'}
INSTALLER_EXT = {'.exe', '.msi', '.msix', '.msixbundle', '.appx', '.iso'}
EMPTY_OK = {'__init__.py', '.gitkeep', '.keep', '.nojekyll', 'py.typed', '.npmignore',
            'desktop.ini', '.placeholder'}

# Rebuildable dev folders: name -> how to get it back
DEV_JUNK = {
    'node_modules': 'npm install', '.venv': 'recreate the venv', 'venv': 'recreate the venv',
    '__pycache__': 'Python rebuilds it', '.next': 'next build', '.nuxt': 'nuxt build',
    '.parcel-cache': 'rebuilt on build', '.pytest_cache': 'rebuilt by pytest',
    '.mypy_cache': 'rebuilt by mypy', '.ruff_cache': 'rebuilt by ruff', '.turbo': 'rebuilt on build',
    '.svelte-kit': 'rebuilt on build', '.angular': 'rebuilt on build',
}
BROWSER_CACHE_PARTS = ('\\cache\\cache_data\\', '\\code cache\\', '\\gpucache\\',
                       '\\service worker\\cachestorage\\', '\\shadercache\\', '\\grshadercache\\')
BROWSER_ROOTS = [os.path.join(LOCAL, *p) for p in (
    ('Google', 'Chrome', 'User Data'), ('Microsoft', 'Edge', 'User Data'),
    ('BraveSoftware', 'Brave-Browser', 'User Data'), ('Vivaldi', 'User Data'),
    ('Chromium', 'User Data'))]
APP_CACHE_DIRS = [os.path.join(os.environ.get('APPDATA', ''), *p) for p in (
    ('Code', 'Cache'), ('Code', 'CachedData'), ('Code', 'Service Worker', 'CacheStorage'),
    ('discord', 'Cache'), ('Slack', 'Cache'), ('Microsoft', 'Teams', 'Cache'))] + [
    os.path.join(LOCAL, 'npm-cache', '_cacache'), os.path.join(LOCAL, 'pip', 'cache'),
    os.path.join(LOCAL, 'Microsoft', 'Windows', 'INetCache'),
    os.path.join(LOCAL, 'CrashDumps'), os.path.join(LOCAL, 'D3DSCache')]

def _walk_files(root):
    """(path, size, mtime) for every file under root, skipping links."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        stack.append(e.path)
                    else:
                        s = e.stat()
                        yield e.path, s.st_size, s.st_mtime
                except OSError:
                    continue

def _card(key, title, why, action, items, level='safe', note=''):
    items.sort(key=lambda x: -x[1])
    return {'key': key, 'title': title, 'why': why, 'action': action, 'level': level,
            'note': note, 'count': len(items), 'size': sum(i[1] for i in items),
            'items': [{'path': p, 'size': s, 'mtime': m} for p, s, m in items]}

def recycle_bin_size():
    class Q(ctypes.Structure):
        _fields_ = [('cbSize', W.DWORD), ('i64Size', ctypes.c_int64), ('i64NumItems', ctypes.c_int64)]
    q = Q(ctypes.sizeof(Q))
    try:
        ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(q))
        return q.i64Size, q.i64NumItems
    except Exception:
        return 0, 0

def _is_cloud(path):
    try:
        return bool(os.stat(path).st_file_attributes & CLOUD)
    except OSError:
        return True

# --- the report ----------------------------------------------------------------
def build_report(records, dupe_groups=(), progress=lambda s: None):
    """records() yields (path, size, mtime) for indexed files."""
    now = time.time()
    dl = _norm(DOWNLOADS)
    shots = _norm(os.path.join(HOME, 'Pictures', 'Screenshots'))
    old_dl, installers, big_old, zero, screenshots = [], [], [], [], []
    junk_roots = defaultdict(lambda: [0, 0])     # folder -> [bytes, files]
    junk_parent_latest = {}

    progress('Reading the index')
    for path, size, mtime in records():
        lp = path.lower()
        # rebuildable dev folders (find the outermost junk folder in the path)
        if '\\' in lp:
            parts = lp.split('\\')
            for i, part in enumerate(parts[:-1]):
                if part in DEV_JUNK:
                    root = '\\'.join(path.split('\\')[:i + 1])
                    r = junk_roots[root]
                    r[0] += size
                    r[1] += 1
                    break
        if protected(path):
            continue
        name = os.path.basename(lp)
        ext = os.path.splitext(name)[1]
        age = now - mtime
        np = lp if lp.endswith('\\') else lp  # paths in index are files
        in_user = any(np.startswith(u) for u in USER_DIRS)
        if np.startswith(dl):
            if ext in INSTALLER_EXT and age > 14 * DAY:
                installers.append((path, size, mtime))
            elif age > 90 * DAY:
                old_dl.append((path, size, mtime))
        elif np.startswith(shots) and age > 180 * DAY:
            screenshots.append((path, size, mtime))
        if size >= 500 * 2**20 and age > 365 * DAY and name not in SYSTEM_FILES \
                and ext not in KEEP_EXT_BIG and '\\appdata\\' not in lp:
            big_old.append((path, size, mtime))
        if size == 0 and in_user and name not in EMPTY_OK and not name.endswith('.lock') \
                and '\\node_modules\\' not in lp:
            zero.append((path, size, mtime))

    # project last-touched time for each junk folder's parent
    progress('Checking project folders')
    parents = {os.path.dirname(r).lower(): r for r in junk_roots}
    if parents:
        for path, size, mtime in records():
            lp = path.lower()
            if any(f'\\{j}\\' in lp for j in DEV_JUNK):
                continue
            d = os.path.dirname(lp)
            for _ in range(12):
                if d in parents:
                    if mtime > junk_parent_latest.get(d, 0):
                        junk_parent_latest[d] = mtime
                    break
                nd = os.path.dirname(d)
                if nd == d:
                    break
                d = nd
    dev = []
    for root, (size, n) in junk_roots.items():
        parent = os.path.dirname(root).lower()
        if protected(root) or now - junk_parent_latest.get(parent, now) < 90 * DAY:
            continue
        dev.append((root, size, junk_parent_latest.get(parent, 0)))

    progress('Measuring temp files and caches')
    temp = [(p, s, m) for p, s, m in _walk_files(TEMP) if now - m > 2 * DAY]
    caches = []
    for root in BROWSER_ROOTS:
        if os.path.isdir(root):
            caches += [(p, s, m) for p, s, m in _walk_files(root)
                       if any(c in p.lower() for c in BROWSER_CACHE_PARTS)]
    for root in APP_CACHE_DIRS:
        if os.path.isdir(root):
            caches += list(_walk_files(root))

    progress('Checking big files')
    big_old = [b for b in big_old if not _is_cloud(b[0])]
    dupes = [(f['path'], g['size'], f['mtime']) for g in dupe_groups
             for f in g['files'] if not f['keep']]
    bin_size, bin_items = recycle_bin_size()

    cards = [
        _card('dupes', 'Duplicate copies',
              'Extra copies found by the duplicate scan. One copy of each file is always kept.',
              'recycle', dupes, note='' if dupe_groups else 'Run a duplicate scan to fill this in.'),
        _card('temp', 'Temporary files', 'Files in your Temp folder older than 2 days. Apps make '
              'these while running and leave them behind.', 'delete', temp,
              note='Deleted permanently; files in use are skipped.'),
        _card('caches', 'Browser and app caches', 'Saved copies of web pages and app data. They '
              'rebuild themselves as you browse.', 'delete', caches,
              note='Deleted permanently. Close your browser first to clear the most.'),
        _card('installers', 'Old installers', 'Setup files in Downloads older than 2 weeks. '
              'Once installed, the app no longer needs them.', 'recycle', installers),
        _card('dev', 'Rebuildable project folders', 'node_modules, venv, caches and build '
              'output in projects untouched for 3 months.', 'delete', dev,
              note='Deleted permanently. Get them back with npm install, pip install or a rebuild.'),
        _card('downloads', 'Old downloads', 'Files in Downloads you haven\'t changed in 3 months.',
              'recycle', old_dl, level='review'),
        _card('big', 'Big, old files', 'Files over 500 MB not changed in a year.',
              'recycle', big_old, level='review'),
        _card('screenshots', 'Old screenshots', 'Screenshots older than 6 months.',
              'recycle', screenshots, level='review'),
        _card('empty', 'Empty files', 'Zero-byte files in your own folders.', 'recycle', zero,
              level='review'),
    ]
    cards.append({'key': 'bin', 'title': 'Recycle Bin', 'why': 'Everything you deleted earlier '
                  'still takes space until the bin is emptied.', 'action': 'empty_bin',
                  'level': 'safe', 'note': '', 'count': bin_items, 'size': bin_size, 'items': []})
    return {'time': now, 'cards': [c for c in cards if c['count'] or c['note']]}

# --- space map ------------------------------------------------------------------
def space_map(records, root, depth=3, top=14):
    """Nested folder sizes under root for a treemap."""
    root = _norm(root)
    n = len(root)
    tree = {'name': root.rstrip('\\'), 'size': 0, 'kids': {}}
    for path, size, mtime in records():
        lp = os.path.normcase(path)
        if not lp.startswith(root):
            continue
        tree['size'] += size
        node = tree
        parts = path[n:].split('\\')[:-1][:depth]
        for part in parts:
            node = node['kids'].setdefault(part, {'name': part, 'size': 0, 'kids': {}})
            node['size'] += size
        if len(parts) < depth:
            f = node['kids'].setdefault('\0files', {'name': '(files)', 'size': 0, 'kids': {}})
            f['size'] += size

    def trim(node):
        kids = sorted(node['kids'].values(), key=lambda k: -k['size'])
        rest = sum(k['size'] for k in kids[top:])
        out = [trim(k) for k in kids[:top]]
        if rest:
            out.append({'name': f'{len(kids) - top} more', 'size': rest, 'kids': []})
        return {'name': node['name'], 'size': node['size'], 'kids': out}
    return trim(tree)

# --- removal ---------------------------------------------------------------------
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [('hwnd', W.HWND), ('wFunc', W.UINT), ('pFrom', W.LPCWSTR), ('pTo', W.LPCWSTR),
                ('fFlags', ctypes.c_uint16), ('fAnyOperationsAborted', W.BOOL),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', W.LPCWSTR)]

def recycle(paths):
    """Send paths to the Recycle Bin in one shell call. Returns True on success."""
    if not paths:
        return True
    op = SHFILEOPSTRUCTW(None, 3, '\0'.join(paths) + '\0\0', None,
                         0x0040 | 0x0010 | 0x0004 | 0x0400)  # ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0

def _force_remove(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass

def delete(paths):
    freed = 0
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                size = sum(s for _, s, _ in _walk_files(p))
                shutil.rmtree(p, onexc=_force_remove)
                freed += size if not os.path.exists(p) else 0
            else:
                size = os.path.getsize(p)
                os.remove(p)
                freed += size
        except OSError:
            continue           # in use or gone: skip quietly
    return freed

class Cleaner:
    """Holds the latest report and only lets those paths be removed."""

    def __init__(self):
        self.report = None
        self.allowed = {}           # normcased path -> action
        self.running = False
        self.phase = ''
        self.lock = threading.Lock()

    def scan(self, records, dupe_groups=()):
        with self.lock:
            if self.running:
                return False
            self.running = True
        def run():
            from dupes import background_priority
            background_priority(True)
            try:
                rep = build_report(records, dupe_groups, progress=lambda s: setattr(self, 'phase', s))
                self.allowed = {os.path.normcase(i['path']): c['action']
                                for c in rep['cards'] for i in c['items']}
                self.report = rep
                self.phase = 'Done'
            except Exception as e:
                self.phase = f'Error: {e}'
            finally:
                self.running = False
                background_priority(False)
        threading.Thread(target=run, daemon=True, name='cleanup').start()
        return True

    def summary(self):
        """Report without the per-file lists (those load per card)."""
        if not self.report:
            return {'running': self.running, 'phase': self.phase, 'cards': []}
        return {'running': self.running, 'phase': self.phase, 'time': self.report['time'],
                'cards': [{k: v for k, v in c.items() if k != 'items'} for c in self.report['cards']]}

    def items(self, key, limit=2000):
        for c in (self.report or {}).get('cards', []):
            if c['key'] == key:
                return c['items'][:limit]
        return []

    def remove(self, paths):
        """Remove paths from the report. Anything not in it is refused."""
        rec, dele, refused = [], [], []
        for p in paths:
            act = self.allowed.get(os.path.normcase(p))
            if not act or protected(p) or not os.path.exists(p):
                refused.append(p)
            elif act == 'recycle':
                rec.append(p)
            else:
                dele.append(p)
        before = sum(_size_of(p) for p in rec)
        ok = recycle(rec)
        freed = (before if ok else 0) + delete(dele)
        for p in rec + dele:
            if not os.path.exists(p):
                self.allowed.pop(os.path.normcase(p), None)
        return {'freed': freed, 'removed': len(rec) + len(dele), 'refused': len(refused)}

    def empty_bin(self):
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        size, _ = recycle_bin_size()
        r = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
        return {'freed': size if r in (0, -2147418113) else 0}

def _size_of(p):
    try:
        return os.path.getsize(p) if os.path.isfile(p) else sum(s for _, s, _ in _walk_files(p))
    except OSError:
        return 0
