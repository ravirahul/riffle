"""Duplicate file finder. Runs only on request, at background priority.

Pipeline: group by size -> hash the first+last 16 KB -> hash the whole file.
Hashes are cached in hashes.db keyed by (path, size, mtime), so a rescan only
reads files that changed. Cloud-only placeholders (OneDrive "free up space")
are skipped because reading them would download them.
"""
import ctypes
import hashlib
import os
import re
import sqlite3
import stat
import threading
import time
from collections import defaultdict

PART = 16 * 1024
CHUNK = 1024 * 1024
# Attributes meaning "content is not on this disk"
CLOUD = 0x00400000 | 0x00040000 | 0x00001000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE

def background_priority(on=True):
    """Lower this thread's CPU and disk priority (Windows THREAD_MODE_BACKGROUND)."""
    try:
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), 0x00010000 if on else 0x00020000)
    except Exception:
        pass

class HashCache:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS h (path TEXT PRIMARY KEY, size INT, '
                        'mtime REAL, part BLOB, full BLOB)')
        self.lock = threading.Lock()

    def get(self, path, size, mtime):
        with self.lock:
            r = self.db.execute('SELECT size, mtime, part, full FROM h WHERE path=?', (path,)).fetchone()
        if r and r[0] == size and abs(r[1] - mtime) < 1e-3:
            return r[2], r[3]
        return None, None

    def put(self, path, size, mtime, part, full=None):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO h VALUES (?,?,?,?,?)', (path, size, mtime, part, full))

    def commit(self):
        with self.lock:
            self.db.commit()

    def forget_missing(self, live_paths):
        """Drop cache rows for files that no longer exist in the index."""
        with self.lock:
            rows = [p for (p,) in self.db.execute('SELECT path FROM h')]
            gone = [(p,) for p in rows if p not in live_paths]
            self.db.executemany('DELETE FROM h WHERE path=?', gone)
            self.db.commit()

def _part_hash(path, size):
    h = hashlib.blake2b(digest_size=16)
    with open(path, 'rb', buffering=0) as f:
        h.update(f.read(PART))
        if size > 2 * PART:
            f.seek(-PART, 2)
            h.update(f.read(PART))
    return h.digest()

def _full_hash(path, cancel):
    h = hashlib.blake2b(digest_size=20)
    with open(path, 'rb', buffering=0) as f:
        while chunk := f.read(CHUNK):
            if cancel.is_set():
                return None
            h.update(chunk)
    return h.digest()

# --- which copy to keep -------------------------------------------------------
JUNKY_DIRS = ('\\downloads\\', '\\temp\\', '\\tmp\\', '\\$recycle.bin\\', '\\whatsapp',
              '\\telegram desktop\\', '\\cache', '\\backup', '\\old\\', '\\copy of')
JUNKY_NAMES = (' - copy', ' copy', '(1)', '(2)', '(3)', '_copy', '-copy', ' (copy')
LOOSE_DIRS = ('\\desktop\\',)                         # files land here in passing
CAMERA_NAME = re.compile(r'^(img|dsc|dscn|pxl|vid|mov|photo|image|whatsapp image|screenshot)[ _-]?\d', re.I)

def _junk_dir(p):
    return next((d.strip('\\').title() for d in JUNKY_DIRS if d in p), '')

def keep_score(path, mtime):
    """Lower is better: prefer organised folders, given names, older files."""
    p = path.lower()
    name = os.path.basename(p)
    score = 0.0
    score += 10 * sum(d in p for d in JUNKY_DIRS)
    score += 8 * sum(n in name for n in JUNKY_NAMES)
    score += 4 * sum(d in p for d in LOOSE_DIRS)
    score += 3 if CAMERA_NAME.match(name) else 0     # "Goa beach.jpg" beats "IMG_2041.jpg"
    score += len(p) / 200          # shorter paths win ties
    score += mtime / 1e10          # older wins remaining ties
    return score

def keep_reason(path, mtime, keep_path, keep_mtime):
    """Short reason this copy is the one to remove, relative to the kept copy."""
    p, k = path.lower(), keep_path.lower()
    jp, jk = _junk_dir(p), _junk_dir(k)
    copyname = lambda x: any(n in os.path.basename(x) for n in JUNKY_NAMES)
    if jp and not jk:
        return f'in {jp}'
    if copyname(p) and not copyname(k):
        return 'name looks like a copy'
    if any(d in p for d in LOOSE_DIRS) and not any(d in k for d in LOOSE_DIRS):
        return 'loose on the Desktop'
    if CAMERA_NAME.match(os.path.basename(p)) and not CAMERA_NAME.match(os.path.basename(k)):
        return 'camera filename'
    if mtime > keep_mtime + 1:
        return 'newer copy'
    return 'longer path'

# --- scanner ------------------------------------------------------------------
class DupeScan:
    """One scan at a time. `records()` yields (path, size, mtime) for files."""

    def __init__(self, cache_path):
        self.cache = HashCache(cache_path)
        self.cancel = threading.Event()
        self.thread = None
        self.state = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0,
                      'bytes': 0, 'groups': [], 'folders': [], 'finished': None}

    def status(self, full=False):
        s = dict(self.state)
        if not full:
            s['groups'] = len(s['groups'])
            s['folders'] = len(s['folders'])
        return s

    def start(self, records, min_size=1 << 20, roots=None, skip=()):
        if self.thread and self.thread.is_alive():
            return False
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, args=(records, min_size, roots, skip),
                                       daemon=True, name='dupes')
        self.thread.start()
        return True

    def stop(self):
        self.cancel.set()

    def _run(self, records, min_size, roots, skip):
        background_priority(True)
        st = self.state
        st.update(running=True, phase='Grouping by size', done=0, total=0, bytes=0,
                  groups=[], folders=[], finished=None, error=None)
        try:
            roots = tuple(r.lower().rstrip('\\') + '\\' for r in roots or ())
            skip = tuple(s.lower() for s in skip)
            by_size = defaultdict(list)
            for path, size, mtime in records():
                if size < min_size:
                    continue
                lp = path.lower()
                if roots and not lp.startswith(roots):
                    continue
                if skip and any(s in lp for s in skip):
                    continue
                by_size[size].append((path, size, mtime))
            cands = [v for v in by_size.values() if len(v) > 1]
            groups = self._narrow(cands)
            if self.cancel.is_set():
                st['phase'] = 'Stopped'
                return
            st['groups'] = groups
            st['folders'] = folder_dupes(groups)
            st['phase'] = 'Done'
        except Exception as e:      # report instead of dying silently
            st['phase'] = 'Error'
            st['error'] = str(e)
        finally:
            self.cache.commit()
            st['running'] = False
            st['finished'] = time.time()
            background_priority(False)

    def _hash_stage(self, groups, full):
        """Split each group by partial (or full) hash. Returns groups of 2+."""
        out = []
        for grp in groups:
            if self.cancel.is_set():
                break
            buckets = defaultdict(list)
            seen_inodes = set()
            for path, size, mtime in grp:
                self.state['done'] += 1
                try:
                    s = os.stat(path)
                except OSError:
                    continue
                if getattr(s, 'st_file_attributes', 0) & CLOUD:
                    continue
                ino = (s.st_dev, s.st_ino)
                if s.st_ino and ino in seen_inodes:
                    continue                 # hard link to a file already counted
                seen_inodes.add(ino)
                part, whole = self.cache.get(path, size, s.st_mtime)
                try:
                    if part is None:
                        part = _part_hash(path, size)
                        self.cache.put(path, size, s.st_mtime, part)
                    key = part
                    if full:
                        if size <= 2 * PART:
                            whole = part        # partial hash already covered the whole file
                        elif whole is None:
                            whole = _full_hash(path, self.cancel)
                            if whole is None:
                                break
                            self.cache.put(path, size, s.st_mtime, part, whole)
                            self.state['bytes'] += size
                        key = whole
                except OSError:
                    continue                 # locked or unreadable: leave it out
                buckets[key].append((path, size, s.st_mtime))
            out.extend((k, b) for k, b in buckets.items() if len(b) > 1)
        return out

    def _narrow(self, cands):
        st = self.state
        st.update(phase='Checking file starts and ends', done=0, total=sum(map(len, cands)))
        partial = self._hash_stage(cands, full=False)
        st.update(phase='Comparing full contents', done=0,
                  total=sum(len(b) for _, b in partial))
        full = self._hash_stage([b for _, b in partial], full=True)
        groups = []
        for digest, files in full:
            files.sort(key=lambda f: keep_score(f[0], f[2]))
            size = files[0][1]
            groups.append({
                'hash': digest.hex(),
                'size': size,
                'waste': size * (len(files) - 1),
                'files': [{'path': p, 'mtime': m, 'keep': i == 0,
                           'why': '' if i == 0 else keep_reason(p, m, files[0][0], files[0][2])}
                          for i, (p, _, m) in enumerate(files)],
            })
        groups.sort(key=lambda g: -g['waste'])
        return groups

def folder_dupes(groups):
    """Folders whose files are all duplicated by another folder with the same contents."""
    sig = defaultdict(list)              # folder -> [hash...]
    for g in groups:
        for f in g['files']:
            sig[os.path.dirname(f['path'])].append((g['hash'], g['size']))
    by_sig = defaultdict(list)
    for folder, items in sig.items():
        if len(items) >= 2:
            by_sig[tuple(sorted(items))].append(folder)
    out = []
    for items, folders in by_sig.items():
        if len(folders) < 2:
            continue
        # every file in the folder must be part of the matched set
        full = []
        for d in folders:
            try:
                n = sum(1 for e in os.scandir(d) if e.is_file())
            except OSError:
                continue
            if n == len(items):
                full.append(d)
        if len(full) >= 2:
            full.sort(key=lambda d: keep_score(d + '\\', 0))
            size = sum(s for _, s in items)
            out.append({'folders': full, 'files': len(items), 'size': size,
                        'waste': size * (len(full) - 1)})
    out.sort(key=lambda d: -d['waste'])
    return out
