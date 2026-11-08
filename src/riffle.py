#!/usr/bin/env python3
"""
RIFFLE — instant filename search for Windows, with filters that actually stack.

    pip install numpy
    python riffle.py

Opens its own window. Ctrl+Shift+Space summons it from anywhere.

How it goes fast
----------------
Every filename is lowercased and packed into one long string separated by NULs,
with a numpy offset array beside it. A substring query is then a single
str.find() sweep over that blob -- C-speed memory scan, ~20 MB for a million
files. Metadata (size, mtime, extension, category, parent dir) lives in parallel
numpy arrays, so every filter is a vectorised boolean mask instead of a loop.

Indexing happens in two phases so the tool is usable immediately:
  phase 1  NTFS USN journal enumeration (Windows, elevated) -- the whole name
           tree in a few seconds, no metadata
  phase 2  threaded os.scandir walk -- fills in size and modified time
If phase 1 is unavailable (not Windows, not elevated, not NTFS) phase 2 runs
alone and does the whole job; it is slower to start but complete on its own.

Once built, the index stays live: a watcher streams filesystem changes in and
they are applied in batches. Names carry a floating-point sort rank so a new
file can slot in between its neighbours instead of forcing a re-sort, and
deletions become tombstones that later compaction sweeps away.
"""

import ctypes
import io
import json
import mimetypes
import os
import pickle
import re
import subprocess
import sys
import threading
import time
import webbrowser
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import cloud
import contentsearch
import features
import picks
import sources

VERSION = '1.2.0'
GITHUB_REPO = os.environ.get('RIFFLE_REPO', 'ravirahul/riffle')  # update checks
HERE = os.path.dirname(os.path.abspath(__file__))
# Frozen builds (PyInstaller) unpack bundled resources to sys._MEIPASS;
# running from source they sit next to this file.
RES = getattr(sys, '_MEIPASS', HERE)

ICON = next((f for f in (os.path.join(RES, 'riffle.ico'), os.path.join(HERE, 'packaging', 'riffle.ico'))
             if os.path.exists(f)), None)
APP_ID = 'Bliink.Riffle'
DATA_DIR = os.environ.get('RIFFLE_DATA') or os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'Riffle')
os.makedirs(DATA_DIR, exist_ok=True)
PORT = int(os.environ.get('RIFFLE_PORT', '8731'))
CACHE = os.path.join(DATA_DIR, 'riffle-index.bin')
if not os.path.exists(CACHE) and os.path.exists(os.path.join(HERE, 'riffle-index.bin')):
    try:    # older builds kept the index beside the script
        os.replace(os.path.join(HERE, 'riffle-index.bin'), CACHE)
    except OSError:
        pass
IS_WIN = os.name == 'nt'
MAX_HITS = 400000
SEP = '\x00'

# File categories, as stored in Index.cat (one uint8 per record).
CATS = ['other', 'document', 'image', 'video', 'audio', 'code', 'archive', 'folder']
CAT_OTHER, CAT_DOC, CAT_IMG, CAT_VID, CAT_AUD, CAT_CODE, CAT_ARC, CAT_DIR = range(8)
_EXT_CAT = {}
for _c, _exts in (
    (CAT_DOC, 'pdf doc docx odt rtf txt md rst tex pages epub mobi xls xlsx ods csv tsv '
              'ppt pptx odp key numbers pub one msg eml'),
    (CAT_IMG, 'jpg jpeg png gif bmp webp tif tiff svg ico heic heif raw cr2 nef arw dng psd ai eps avif'),
    (CAT_VID, 'mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp ts vob ogv prproj'),
    (CAT_AUD, 'mp3 wav flac aac ogg wma m4a opus aiff mid midi aup als flp'),
    (CAT_CODE, 'py js ts jsx tsx html htm css scss sass less php rb go rs c h cpp hpp cc java kt swift '
               'cs vb sh bash zsh ps1 bat cmd sql json yaml yml toml ini cfg conf xml liquid twig hbs '
               'vue svelte lua pl r m ipynb gradle make cmake dockerfile env lock'),
    (CAT_ARC, 'zip rar 7z tar gz bz2 xz iso dmg cab msi apk jar war deb rpm zst lz4'),
):
    for _e in _exts.split():
        _EXT_CAT[_e] = _c


# -- index ------------------------------------------------------------------
# One record per file or folder; record ids are positions in the arrays.

class Index:
    """Packed, searchable snapshot of the filesystem."""

    def __init__(self):
        self.dirs = []                    # parent directory paths
        self.dir_id = {}                  # path -> index into dirs
        self.blob = b''                   # lowercased names, NUL separated
        self.disp = b''                   # display names, NUL separated
        self.arr = np.zeros(0, dtype=np.uint8)
        self.off = np.zeros(1, dtype=np.int64)
        self.doff = np.zeros(1, dtype=np.int64)
        self.parent = np.zeros(0, dtype=np.int32)
        self.size = np.zeros(0, dtype=np.int64)
        self.mtime = np.zeros(0, dtype=np.int64)
        self.cat = np.zeros(0, dtype=np.uint8)
        self.rank = np.zeros(0, dtype=np.float64)
        self.order = np.zeros(0, dtype=np.int64)
        self.dirrank = np.zeros(0, dtype=np.int32)
        self.dead = np.zeros(0, dtype=bool)
        self.ndead = 0
        self.gen = 0                      # bumped on every change
        self.exts = []
        self.ext_id = {}
        self.extv = np.zeros(0, dtype=np.int32)
        self.n = 0
        self.enriched = False
        self.built_at = 0.0
        self.roots = []
        self._scope_cache = {}

    # -- interning: each distinct dir / extension stored once ----------------
    def dir_index(self, path: str) -> int:
        i = self.dir_id.get(path)
        if i is None:
            i = len(self.dirs)
            self.dirs.append(path)
            self.dir_id[path] = i
        return i

    def ext_index(self, e: str) -> int:
        i = self.ext_id.get(e)
        if i is None:
            i = len(self.exts)
            self.exts.append(e)
            self.ext_id[e] = i
        return i

    def build(self, records):
        """records: iterable of (name, parent_dir_path, is_dir, size, mtime)."""
        lo_parts, di_parts = [], []
        off, doff, par, siz, mt, cat, ext = [], [], [], [], [], [], []
        lpos = dpos = 0
        for name, pdir, is_dir, size, mtime in records:
            lb = name.lower().encode('utf-8', 'surrogatepass')
            db = name.encode('utf-8', 'surrogatepass')
            off.append(lpos); doff.append(dpos)
            lo_parts.append(lb); di_parts.append(db)
            lpos += len(lb) + 1
            dpos += len(db) + 1
            par.append(self.dir_index(pdir))
            siz.append(size); mt.append(mtime)
            if is_dir:
                cat.append(CAT_DIR); ext.append(self.ext_index(''))
            else:
                d = name.rfind('.')
                e = name[d + 1:].lower() if 0 < d < len(name) - 1 and len(name) - d <= 12 else ''
                ext.append(self.ext_index(e))
                cat.append(_EXT_CAT.get(e, CAT_OTHER))
        off.append(lpos); doff.append(dpos)

        self.blob = b'\x00'.join(lo_parts) + (b'\x00' if lo_parts else b'')
        self.disp = b'\x00'.join(di_parts) + (b'\x00' if di_parts else b'')
        self.off = np.array(off, dtype=np.int64)
        self.doff = np.array(doff, dtype=np.int64)
        self.parent = np.array(par, dtype=np.int32)
        self.size = np.array(siz, dtype=np.int64)
        self.mtime = np.array(mt, dtype=np.int64)
        self.cat = np.array(cat, dtype=np.uint8)
        self.extv = np.array(ext, dtype=np.int32)
        self.n = len(par)
        self.built_at = time.time()
        self.finalize()

    def finalize(self):
        """Derive the uint8 view, the alphabetical rank, and the sorted order.

        Sorting a subset by name would otherwise mean pulling out N Python
        strings and comparing them, which dominated every query. Instead each
        name's position in the full ordering is computed once, so sorting any
        subset later is a plain argsort in C.

        The rank is a float, not an integer, so a file appearing later can be
        given a rank halfway between its two neighbours instead of forcing a
        re-sort of everything. `order` keeps the ids in name order so that
        insertion point can be found by binary search."""
        self.arr = np.frombuffer(self.blob, dtype=np.uint8)
        n = self.n
        if n == 0:
            self.rank = np.zeros(0, dtype=np.float64)
            self.order = np.zeros(0, dtype=np.int64)
            self.dead = np.zeros(0, dtype=bool)
            self.dirrank = np.zeros(len(self.dirs), dtype=np.int32)
            return
        if self.dead.size != n:
            self.dead = np.zeros(n, dtype=bool)
            self.ndead = 0
        if self.rank.size != n or self.order.size != n:
            parts = self.blob.split(b'\x00')[:n]
            if len(parts) < n:
                parts += [b''] * (n - len(parts))
            order = np.argsort(np.array(parts, dtype='S96'), kind='stable')
            rank = np.empty(n, dtype=np.float64)
            rank[order] = np.arange(n, dtype=np.float64)
            self.rank, self.order = rank, order.astype(np.int64)
        if self.dirrank.size != len(self.dirs):
            self._rebuild_dirrank()
        self._scope_cache = {}

    def dirrank_now(self):
        """Folder ranks for sorting by path; rebuilt only when that sort is used."""
        if self.dirrank.size != len(self.dirs):
            self._rebuild_dirrank()
        return self.dirrank

    def _rebuild_dirrank(self):
        if not self.dirs:
            self.dirrank = np.zeros(0, dtype=np.int32)
            return
        keys = np.array([d.lower().encode('utf-8', 'surrogatepass') for d in self.dirs],
                        dtype='S260')
        dorder = np.argsort(keys, kind='stable')
        dr = np.empty(len(self.dirs), dtype=np.int32)
        dr[dorder] = np.arange(len(self.dirs), dtype=np.int32)
        self.dirrank = dr

    # -- live updates --------------------------------------------------------
    def name_bytes(self, i):
        return self.blob[self.off[i]:self.off[i + 1] - 1]

    def find_id(self, lname: bytes, parent_id: int):
        """Locate a live record by exact lowercased name + parent. Binary search
        over `order` narrows to the names that match, then parent decides."""
        if self.n == 0:
            return -1
        lo, hi = 0, self.order.size
        while lo < hi:
            mid = (lo + hi) // 2
            if self.name_bytes(self.order[mid]) < lname:
                lo = mid + 1
            else:
                hi = mid
        while lo < self.order.size and self.name_bytes(self.order[lo]) == lname:
            j = int(self.order[lo])
            if self.parent[j] == parent_id and not self.dead[j]:
                return j
            lo += 1
        return -1

    def _insert_pos(self, lname: bytes):
        """Index in `order` where lname belongs alphabetically."""
        lo, hi = 0, self.order.size
        while lo < hi:
            mid = (lo + hi) // 2
            if self.name_bytes(self.order[mid]) < lname:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _ranks_for_batch(self, lnames):
        """Ranks for a batch of new names, computed in one pass.

        Ranks must come out strictly increasing in alphabetical order, including
        among the new names themselves. Computing each independently against the
        old order collides whenever two new names fall in the same gap, which
        silently corrupts the binary search that everything else depends on. So
        the batch is sorted, grouped by insertion point, and each group spread
        evenly across the gap it lands in.

        Returns (ranks aligned to lnames, whether a full renormalize is needed).
        """
        n = len(lnames)
        if n == 0:
            return [], False
        if self.order.size == 0:
            idx = sorted(range(n), key=lambda i: lnames[i])
            out = [0.0] * n
            for r, i in enumerate(idx):
                out[i] = float(r)
            return out, False

        by_name = sorted(range(n), key=lambda i: lnames[i])
        pos = [self._insert_pos(lnames[i]) for i in by_name]
        out = [0.0] * n
        need_renorm = False

        i = 0
        while i < len(by_name):
            j = i
            while j + 1 < len(by_name) and pos[j + 1] == pos[i]:
                j += 1
            p, m = pos[i], j - i + 1
            if p == 0:
                hi = float(self.rank[self.order[0]]); lo = hi - 1.0
            elif p >= self.order.size:
                lo = float(self.rank[self.order[-1]]); hi = lo + 1.0
            else:
                lo = float(self.rank[self.order[p - 1]])
                hi = float(self.rank[self.order[p]])
            step = (hi - lo) / (m + 1)
            for k in range(m):
                r = lo + step * (k + 1)
                if not lo < r < hi:
                    need_renorm = True   # float precision ran out in this gap
                    r = lo
                out[by_name[i + k]] = r
            i = j + 1
        return out, need_renorm

    def add_records(self, recs):
        """Append (name, parent_dir, is_dir, size, mtime) rows to a live index."""
        recs = [r for r in recs if r[0]]
        if not recs:
            return 0
        lnames = [r[0].lower().encode('utf-8', 'surrogatepass') for r in recs]
        rank_add, need_renorm = self._ranks_for_batch(lnames)

        lo_add, di_add = [], []
        off_add, doff_add, par_add, siz_add, mt_add, cat_add, ext_add = [], [], [], [], [], [], []
        lpos, dpos = int(self.off[-1]), int(self.doff[-1])
        for (name, pdir, is_dir, size, mtime), lb in zip(recs, lnames):
            db = name.encode('utf-8', 'surrogatepass')
            lo_add.append(lb); di_add.append(db)
            off_add.append(lpos); doff_add.append(dpos)
            lpos += len(lb) + 1; dpos += len(db) + 1
            par_add.append(self.dir_index(pdir))
            siz_add.append(size); mt_add.append(mtime)
            if is_dir:
                cat_add.append(CAT_DIR); ext_add.append(self.ext_index(''))
            else:
                d = name.rfind('.')
                e = name[d + 1:].lower() if 0 < d < len(name) - 1 and len(name) - d <= 12 else ''
                ext_add.append(self.ext_index(e))
                cat_add.append(_EXT_CAT.get(e, CAT_OTHER))

        k = len(recs)
        base = self.n
        self.blob = self.blob + b'\x00'.join(lo_add) + b'\x00'
        self.disp = self.disp + b'\x00'.join(di_add) + b'\x00'
        self.arr = np.frombuffer(self.blob, dtype=np.uint8)
        self.off = np.concatenate([self.off[:-1], np.array(off_add, dtype=np.int64),
                                   np.array([lpos], dtype=np.int64)])
        self.doff = np.concatenate([self.doff[:-1], np.array(doff_add, dtype=np.int64),
                                    np.array([dpos], dtype=np.int64)])
        self.parent = np.concatenate([self.parent, np.array(par_add, dtype=np.int32)])
        self.size = np.concatenate([self.size, np.array(siz_add, dtype=np.int64)])
        self.mtime = np.concatenate([self.mtime, np.array(mt_add, dtype=np.int64)])
        self.cat = np.concatenate([self.cat, np.array(cat_add, dtype=np.uint8)])
        self.extv = np.concatenate([self.extv, np.array(ext_add, dtype=np.int32)])
        self.rank = np.concatenate([self.rank, np.array(rank_add, dtype=np.float64)])
        self.dead = np.concatenate([self.dead, np.zeros(k, dtype=bool)])
        self.n = base + k
        new_ids = np.arange(base, base + k, dtype=np.int64)
        # `order` is already sorted by rank: slot the new ids in (O(n) copy, no full sort)
        new_ids = new_ids[np.argsort(self.rank[new_ids], kind='stable')]
        at = np.searchsorted(self.rank[self.order], self.rank[new_ids], side='right')
        self.order = np.insert(self.order, at, new_ids)
        if self.dirrank.size != len(self.dirs):
            self._scope_cache = {}          # dirrank itself is rebuilt lazily, see dirrank_now()
        if need_renorm:
            self.renormalize_full()
        self.gen += 1
        return k

    def remove_ids(self, ids):
        if not len(ids):
            return 0
        ids = np.asarray(ids, dtype=np.int64)
        fresh = ids[~self.dead[ids]]
        if not fresh.size:
            return 0
        self.dead[fresh] = True
        self.ndead += int(fresh.size)
        self.gen += 1
        return int(fresh.size)

    def touch(self, i, size, mtime):
        self.size[i] = size
        self.mtime[i] = mtime
        self.gen += 1

    def renormalize_full(self):
        """Rebuild ranks straight from the names. Used when float precision runs
        out in some gap -- respacing alone would not repair it, since the order
        array itself is then untrustworthy."""
        n = self.n
        if n == 0:
            return
        parts = self.blob.split(b'\x00')[:n]
        if len(parts) < n:
            parts += [b''] * (n - len(parts))
        order = np.argsort(np.array(parts, dtype='S96'), kind='stable')
        rank = np.empty(n, dtype=np.float64)
        rank[order] = np.arange(n, dtype=np.float64)
        self.rank, self.order = rank, order.astype(np.int64)

    def compact(self):
        """Physically drop tombstoned records. Cheap enough to run occasionally
        and keeps the blob from growing without bound in a long session."""
        if self.ndead == 0:
            return
        keep = np.flatnonzero(~self.dead)
        recs = []
        for i in keep.tolist():
            recs.append((self.name(i), self.dirs[self.parent[i]],
                         self.cat[i] == CAT_DIR, int(self.size[i]), int(self.mtime[i])))
        roots, enriched = self.roots, self.enriched
        self.dirs = []; self.dir_id = {}
        self.exts = []; self.ext_id = {}
        self.rank = np.zeros(0, dtype=np.float64)
        self.order = np.zeros(0, dtype=np.int64)
        self.dead = np.zeros(0, dtype=bool); self.ndead = 0
        self.build(recs)
        self.roots, self.enriched = roots, enriched
        self.gen += 1

    def flag_mask(self, kind):
        """Per-directory bool mask: 'system' (Windows, Program Files, AppData...) or
        'hidden' (a dot-folder like .git or .cache anywhere in the path). Cached and
        extended in place, since live updates only ever append directories."""
        cache = self.__dict__.setdefault('_flag_cache', {})
        arr, src = cache.get(kind, (None, None))
        n = len(self.dirs)
        if arr is None or src is not self.dirs or len(arr) > n:
            arr, start = np.zeros(0, dtype=bool), 0
        else:
            start = len(arr)
        if start < n:
            test = {'system': _is_system_dir, 'hidden': _is_hidden_dir, 'root': lambda d: len(d) <= 3,
                    'startmenu': lambda d: '\\start menu\\' in d}[kind]
            new = np.fromiter((test(d.lower()) for d in self.dirs[start:n]), dtype=bool, count=n - start)
            arr = np.concatenate([arr, new])
            cache[kind] = (arr, self.dirs)
        return arr

    def dir_lengths(self):
        """Length of each directory path (shallower folders rank higher). Cached, extended in place."""
        arr, src = self.__dict__.get('_dirlen', (None, None))
        n = len(self.dirs)
        if arr is None or src is not self.dirs or len(arr) > n:
            arr = np.zeros(0, dtype=np.int32)
        if len(arr) < n:
            arr = np.concatenate([arr, np.fromiter((len(d) for d in self.dirs[len(arr):n]), dtype=np.int32)])
            self._dirlen = (arr, self.dirs)
        return arr

    def scope_mask(self, prefix, contains=False):
        """Cached per-directory boolean mask for path scoping."""
        key = (prefix, contains)
        m = self._scope_cache.get(key)
        if m is None:
            if contains:
                m = np.array([prefix in d.lower() for d in self.dirs], dtype=bool)
            else:
                m = np.array([d.lower().startswith(prefix) for d in self.dirs], dtype=bool)
            if len(self._scope_cache) > 64:
                self._scope_cache.clear()
            self._scope_cache[key] = m
        return m

    # -- accessors -----------------------------------------------------------
    def name(self, i: int) -> str:
        return self.disp[self.doff[i]:self.doff[i + 1] - 1].decode('utf-8', 'replace')

    def path(self, i: int) -> str:
        d = self.dirs[self.parent[i]]
        return d + ('' if d.endswith(('\\', '/')) else os.sep) + self.name(i)

    # -- persistence ---------------------------------------------------------
    def save(self, path):
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump({
                'v': 3, 'dirs': self.dirs, 'blob': self.blob, 'disp': self.disp,
                'off': self.off, 'doff': self.doff, 'parent': self.parent,
                'size': self.size, 'mtime': self.mtime, 'cat': self.cat,
                'exts': self.exts, 'extv': self.extv, 'n': self.n,
                'enriched': self.enriched, 'built_at': self.built_at, 'roots': self.roots,
                'rank': self.rank, 'dirrank': self.dirrank, 'order': self.order,
                'dead': self.dead,
            }, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @staticmethod
    def load(path):
        with open(path, 'rb') as f:
            d = pickle.load(f)
        if d.get('v') != 3:
            raise ValueError('stale index format')
        ix = Index()
        ix.dirs = d['dirs']; ix.dir_id = {p: i for i, p in enumerate(ix.dirs)}
        ix.blob = d['blob']; ix.disp = d['disp']
        ix.off = d['off']; ix.doff = d['doff']; ix.parent = d['parent']
        ix.size = d['size']; ix.mtime = d['mtime']; ix.cat = d['cat']
        ix.exts = d['exts']; ix.ext_id = {e: i for i, e in enumerate(ix.exts)}
        ix.extv = d['extv']; ix.n = d['n']; ix.enriched = d['enriched']
        ix.built_at = d['built_at']; ix.roots = d.get('roots', [])
        r = d.get('rank'); dr = d.get('dirrank'); od = d.get('order')
        ix.rank = np.asarray(r, dtype=np.float64) if r is not None else np.zeros(0, dtype=np.float64)
        ix.order = np.asarray(od, dtype=np.int64) if od is not None else np.zeros(0, dtype=np.int64)
        ix.dirrank = dr if dr is not None else np.zeros(0, dtype=np.int32)
        ix.arr = np.frombuffer(ix.blob, dtype=np.uint8)
        ix.finalize()
        dead = d.get('dead')
        if dead is not None and len(dead) == ix.n:
            ix.dead = np.asarray(dead, dtype=bool).copy()
            ix.ndead = int(ix.dead.sum())
        return ix


# Folders hidden from results unless "Show system files" is on. Matched on the
# part after the drive letter, so they apply to every drive.
SYSTEM_TOP = ('\\windows', '\\program files', '\\program files (x86)', '\\programdata',
              '\\$recycle.bin', '\\system volume information', '\\$windows.~bt', '\\$windows.~ws',
              '\\$winreagent', '\\recovery', '\\perflogs', '\\msocache', '\\config.msi',
              '\\intel', '\\amd', '\\nvidia', '\\drivers', '\\onedrivetemp', '\\$sysreset',
              '\\windows.old', '\\esd')
SYSTEM_ROOT_FILES = {'pagefile.sys', 'hiberfil.sys', 'swapfile.sys', 'dumpstack.log', 'dumpstack.log.tmp',
                     'bootmgr', 'bootnxt', 'bootsect.bak', 'desktop.ini', 'thumbs.db', 'ntuser.dat'}


def _is_system_dir(d):
    if '\\start menu\\' in d:
        return False                 # app shortcuts: the quick bar's best results
    rest = d[2:] if len(d) > 1 and d[1] == ':' else d
    for t in SYSTEM_TOP:
        if rest == t or rest.startswith(t + '\\'):
            return True
    return '\\appdata\\' in d or d.endswith('\\appdata') or '\\$recycle.bin' in d


def _is_hidden_dir(d):
    return '\\.' in d


# -- scanning ---------------------------------------------------------------
# Paths containing any of these fragments are never indexed.

POLL_SECONDS = 8
SKIP_FRAGMENTS = ('\\$recycle.bin', '\\system volume information', '\\appdata\\local\\temp',
                  '\\windows\\prefetch', '\\windows\\winsxs', '\\.git\\', '\\visual-index\\',
                  DATA_DIR.lower() + '\\')
SKIP_DIRS = {'$windows.~bt', 'system volume information', '.git', 'node_modules_cache',
             '$recycle.bin', '$windows.~ws',
             'visual-index',
             # browser/app caches: thousands of writes a minute, nothing anyone searches for
             'cache_data', 'code cache', 'gpucache', 'grshadercache', 'shadercache',
             'dawncache', 'dawngraphitecache', 'dawnwebgpucache', 'crashpad',
             # dependency and system stores: the folder is listed, its contents are not.
             # Together they would triple the index (node_modules alone can be 400k files).
             'node_modules', 'winsxs', '__pycache__', 'site-packages'}
NOISE = tuple('\\' + d + '\\' for d in ('cache_data', 'code cache', 'gpucache', 'grshadercache',
                                         'shadercache', 'dawncache', 'dawngraphitecache',
                                         'dawnwebgpucache', 'crashpad', 'node_modules', 'winsxs',
                                         '__pycache__', 'site-packages'))

# Riffle's own data dir is skipped by path: its cache churns constantly.
DATA_LOWER = DATA_DIR.lower()


def _background_thread():
    """Lower the calling thread's CPU and disk priority (Windows)."""
    if IS_WIN:
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), 0x00010000)   # THREAD_MODE_BACKGROUND_BEGIN


def _lowest_thread():
    """Lowest normal CPU priority; disk stays normal so walks still finish quickly."""
    if IS_WIN:
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), -2)   # THREAD_PRIORITY_LOWEST


def scan_tree(roots, progress=None, skip_names=SKIP_DIRS, low=False):
    """Threaded os.scandir walk. On Windows DirEntry.stat() is free -- the values
come back with the directory enumeration -- so this collects metadata at no
extra syscall cost."""
    out = []
    lock = threading.Lock()
    pending = list(roots)
    count = [0]

    def work(d):
        local, subs = [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                        local.append((e.name, d, is_dir, 0 if is_dir else st.st_size,
                                      int(st.st_mtime)))
                        if is_dir and e.name.lower() not in skip_names and e.path.lower() != DATA_LOWER:
                            subs.append(e.path)
                    except OSError:
                        continue
        except OSError:
            return []
        with lock:
            out.extend(local)
            count[0] += len(local)
            if progress and count[0] % 20000 < len(local):
                progress(count[0])
        return subs

    workers = 4 if low else min(16, (os.cpu_count() or 4) * 2)
    with ThreadPoolExecutor(max_workers=workers, initializer=_lowest_thread if low else None) as ex:   # breadth-first waves
        while pending:
            futs = [ex.submit(work, d) for d in pending]
            pending = []
            for f in futs:
                pending.extend(f.result())
    return out


# -- NTFS fast path -----------------------------------------------------------
def usn_enumerate(letter):
    """Enumerate every MFT record on an NTFS volume through the USN journal.
Returns {frn: (name, parent_frn, is_dir)} or None if unavailable.
Needs an elevated process; fails cleanly otherwise."""
    if not IS_WIN:
        return None
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        GENERIC_READ = 0x80000000
        FILE_SHARE = 3                      # read | write
        OPEN_EXISTING = 3
        INVALID = ctypes.c_void_p(-1).value
        FSCTL_ENUM_USN_DATA = 0x900B3
        FILE_ATTRIBUTE_DIRECTORY = 0x10

        k32.CreateFileW.restype = ctypes.c_void_p
        h = k32.CreateFileW(ctypes.c_wchar_p(f'\\\\.\\{letter}:'), GENERIC_READ,
                            FILE_SHARE, None, OPEN_EXISTING, 0, None)
        if h in (INVALID, 0, None):
            return None

        class MFT_ENUM_DATA(ctypes.Structure):
            _fields_ = [('StartFileReferenceNumber', ctypes.c_ulonglong),
                        ('LowUsn', ctypes.c_longlong),
                        ('HighUsn', ctypes.c_longlong)]

        med = MFT_ENUM_DATA(0, 0, 0x7FFFFFFFFFFFFFFF)
        buf = ctypes.create_string_buffer(1 << 20)
        ret = ctypes.c_ulong(0)
        table = {}
        try:
            while True:
                ok = k32.DeviceIoControl(ctypes.c_void_p(h), FSCTL_ENUM_USN_DATA,
                                         ctypes.byref(med), ctypes.sizeof(med),
                                         buf, ctypes.sizeof(buf),
                                         ctypes.byref(ret), None)
                if not ok or ret.value <= 8:
                    break
                data = buf.raw[:ret.value]
                med.StartFileReferenceNumber = int.from_bytes(data[:8], 'little')
                p = 8
                while p + 60 <= len(data):
                    rlen = int.from_bytes(data[p:p + 4], 'little')
                    if rlen < 60 or p + rlen > len(data):
                        break
                    major = int.from_bytes(data[p + 4:p + 6], 'little')
                    if major == 2:                  # USN_RECORD_V2
                        frn = int.from_bytes(data[p + 8:p + 16], 'little')
                        pfrn = int.from_bytes(data[p + 16:p + 24], 'little')
                        attrs = int.from_bytes(data[p + 52:p + 56], 'little')
                        nlen = int.from_bytes(data[p + 56:p + 58], 'little')
                        noff = int.from_bytes(data[p + 58:p + 60], 'little')
                        if 0 < nlen and p + noff + nlen <= len(data):
                            nm = data[p + noff:p + noff + nlen].decode('utf-16-le', 'replace')
                            table[frn] = (nm, pfrn, bool(attrs & FILE_ATTRIBUTE_DIRECTORY))
                    p += rlen
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
        return table or None
    except Exception:
        return None


def usn_records(letter):
    """Turn a USN table into (name, parent_dir, is_dir, size, mtime) records.
Sizes and times arrive later from the scandir pass."""
    table = usn_enumerate(letter)
    if not table:
        return None
    root = f'{letter}:\\'
    path_cache = {}

    def resolve(frn, depth=0):
        if depth > 96:
            return None
        p = path_cache.get(frn)
        if p is not None:
            return p
        ent = table.get(frn)
        if ent is None:
            return None
        nm, pfrn, _ = ent
        if pfrn == frn or pfrn not in table:
            p = root
        else:
            up = resolve(pfrn, depth + 1)
            p = root if up is None else up.rstrip('\\') + '\\' + nm
        path_cache[frn] = p
        return p

    recs = []
    for frn, (nm, pfrn, is_dir) in table.items():
        parent = resolve(pfrn) if pfrn in table else root
        if parent is None:
            continue
        recs.append((nm, parent, is_dir, 0, 0))
    return recs


# -- live updates -------------------------------------------------------------
# ReadDirectoryChangesW on Windows, a polling snapshot diff elsewhere.

class Watcher(threading.Thread):
    """Streams filesystem changes into a queue of (kind, path) pairs.

kind is "add", "del" or "mod". Paths are absolute. The engine drains the
queue on a timer and patches the index, so a burst of a thousand changes
costs one batched update rather than a thousand.
"""
    def __init__(self, roots, sink):
        super().__init__(daemon=True)
        self.roots = roots
        self.sink = sink
        self.stop_flag = threading.Event()
        self.alive_mode = 'none'

    def run(self):
        if IS_WIN:
            try:
                self.alive_mode = 'win32'
                self._run_win()
                return
            except Exception:
                self.alive_mode = 'poll'
        else:
            self.alive_mode = 'poll'
        self._run_poll()

    # One ReadDirectoryChangesW thread per root.
    def _run_win(self):
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE = 0x7                            # read | write | delete
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        NOTIFY = 0x1B                               # file name | dir name | size | last write
        # FILE_ACTION_*: added, removed, modified, renamed-old, renamed-new.
        # A rename is reported as a delete of the old name plus an add of
        # the new one, which is exactly how the index wants to see it.
        ACTIONS = {1: 'add', 2: 'del', 3: 'mod', 4: 'del', 5: 'add'}
        k32.CreateFileW.restype = ctypes.c_void_p
        INVALID = ctypes.c_void_p(-1).value

        handles = []
        for root in self.roots:
            h = k32.CreateFileW(ctypes.c_wchar_p(root), FILE_LIST_DIRECTORY, FILE_SHARE,
                                None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
            if h not in (INVALID, 0, None):
                handles.append((root, h))
        if not handles:
            raise OSError('no watchable roots')

        threads = []
        for root, h in handles:
            t = threading.Thread(target=self._watch_one, args=(k32, root, h, NOTIFY, ACTIONS),
                                 daemon=True, name=f'watch {root}')
            t.start()
            threads.append(t)
        while not self.stop_flag.is_set():
            time.sleep(0.4)

    def _watch_one(self, k32, root, handle, notify, actions):
        buf = ctypes.create_string_buffer(64 * 1024)
        ret = ctypes.c_ulong(0)
        while not self.stop_flag.is_set():
            ok = k32.ReadDirectoryChangesW(ctypes.c_void_p(handle), buf, ctypes.sizeof(buf),
                                           True, notify, ctypes.byref(ret), None, None)
            if not ok or ret.value == 0:
                time.sleep(0.5)
                continue
            data = buf.raw[:ret.value]
            p = 0
            while p + 12 <= len(data):
                nxt = int.from_bytes(data[p:p + 4], 'little')
                action = int.from_bytes(data[p + 4:p + 8], 'little')
                nlen = int.from_bytes(data[p + 8:p + 12], 'little')
                if p + 12 + nlen > len(data):
                    break
                rel = data[p + 12:p + 12 + nlen].decode('utf-16-le', 'replace')
                kind = actions.get(action)
                if kind and rel:
                    full = os.path.join(root, rel)
                    if not any(s in full.lower() for s in SKIP_FRAGMENTS + NOISE):
                        self.sink(kind, full)
                if nxt == 0:
                    break
                p += nxt

    # Fallback for non-Windows hosts and volumes that refuse a directory handle.
    def _run_poll(self):
        """Walks the tree and compares against a remembered snapshot. Only used
        where the native watcher is unavailable; deliberately gentle, since this
        is a full stat sweep."""
        prev = {}
        first = True
        while not self.stop_flag.is_set():
            cur = {}
            try:
                for name, pdir, is_dir, size, mtime in scan_tree(self.roots):
                    sep = '' if pdir.endswith(('\\', '/')) else os.sep
                    cur[pdir + sep + name] = (size, mtime, is_dir)
            except Exception:
                pass
            if not first:
                for p, v in cur.items():
                    old = prev.get(p)
                    if old is None:
                        self.sink('add', p)
                    elif old[:2] != v[:2]:
                        self.sink('mod', p)
                for p in prev:
                    if p not in cur:
                        self.sink('del', p)
            prev = cur
            first = False
            for _ in range(POLL_SECONDS * 2):
                if self.stop_flag.is_set():
                    return
                time.sleep(0.5)

    def stop(self):
        self.stop_flag.set()


# -- engine -------------------------------------------------------------------
# Owns the live Index, applies watcher batches, and answers queries.
class Engine:
    def __init__(self):
        self.ix = Index()
        self.state = 'empty'            # empty | scanning | enriching | ready
        self.progress = 0
        self.message = 'No index yet'
        self.lock = threading.RLock()
        self.watcher = None
        self.pending = []
        self.pend_lock = threading.Lock()
        self.live = False
        self.live_adds = 0
        self.live_dels = 0
        self.last_change = 0.0
        self.last_query = 0.0
        self.saved_gen = 0
        self.saved_at = 0.0
        self.refreshing = False
        self.picks = picks.Picks(os.path.join(DATA_DIR, 'picks.json'))
        self._pick_cache = None
        threading.Thread(target=self._drain_loop, daemon=True, name="drain").start()

    # -- live index maintenance ------------------------------------------------
    def on_change(self, kind, path):
        with self.pend_lock:
            if len(self.pending) < 60000:     # drop the flood, keep the app alive
                self.pending.append((kind, path))

    def start_watch(self, roots):
        if self.watcher:
            self.watcher.stop()
        self.watcher = Watcher(roots, self.on_change)
        self.watcher.start()
        self.live = True

    def _drain_loop(self):
        # Every batch re-sorts the whole index, so batch rarely while nobody is
        # searching. query() drains first, so results are always current.
        while True:
            time.sleep(0.8 if time.time() - self.last_query < 90 else 8)
            try:
                self._drain()
                self._maybe_save()
            except Exception:
                continue

    def _maybe_save(self):
        """Write live changes to disk once things go quiet, at most every 10 minutes."""
        ix = self.ix
        if (self.state != 'ready' or self.refreshing or ix.gen == self.saved_gen
                or time.time() - self.saved_at < 600 or time.time() - self.last_change < 20):
            return
        self.save_now()

    def save_now(self):
        _background_thread()
        try:
            with self.lock:
                ix, gen = self.ix, self.ix.gen
                ix.save(CACHE)
            self.saved_gen, self.saved_at = gen, time.time()
        except Exception as e:
            print(f'[riffle] could not save index: {e}')
        finally:
            if IS_WIN:
                k = ctypes.windll.kernel32
                k.SetThreadPriority(k.GetCurrentThread(), 0x00020000)   # THREAD_MODE_BACKGROUND_END

    def refresh(self, roots, delay=0):
        """Catch up on changes made while Riffle wasn't running: walk again quietly,
        keep serving the old index, then swap. Watcher events wait in the queue."""
        def run():
            time.sleep(delay)
            if self.state != 'ready' or self.refreshing:
                return
            self.refreshing = True
            _lowest_thread()
            t0 = time.time()
            print('[riffle] catching up on changes made while closed', flush=True)
            try:
                walked = scan_tree(roots, low=True)
                print(f'[riffle] catch-up walk: {len(walked):,} items in {time.time() - t0:.0f}s', flush=True)
                ix = Index(); ix.roots = roots
                ix.build(walked)
                ix.enriched = True
                with self.lock:
                    self.ix = ix
                self.message = f'{ix.n:,} items indexed'
            except Exception as e:
                print(f'[riffle] refresh failed: {e}')
            finally:
                self.refreshing = False
            self._drain()
            self.save_now()
        threading.Thread(target=run, daemon=True, name='refresh').start()

    def _drain(self):
        if self.refreshing:
            return                      # keep events queued until the new index is in place
        with self.pend_lock:
            if not self.pending:
                return
            batch, self.pending = self.pending, []
        if self.state not in ('ready', 'enriching'):
            return

        # Collapse to the latest event per path, then trust the filesystem: a
        # file created and deleted inside one window simply never appears.
        latest = {}
        for kind, path in batch:
            latest[os.path.normpath(path)] = kind
        # Let the visual index know which files moved under it.
        visual_touched(latest)

        adds, dels, mods = [], [], []
        for path, kind in latest.items():
            exists = os.path.lexists(path)
            if kind == 'del' or not exists:
                if not exists:
                    dels.append(path)
                continue
            (adds if kind == 'add' else mods).append(path)

        with self.lock:
            ix = self.ix
            removed = 0
            gone = []
            for path in dels:
                pdir, nm = os.path.split(path)
                pid = ix.dir_id.get(pdir)
                if pid is None:
                    continue
                j = ix.find_id(nm.lower().encode('utf-8', 'surrogatepass'), pid)
                if j >= 0:
                    gone.append(j)
            if gone:
                removed = ix.remove_ids(gone)

            fresh = []
            for path in adds + mods:
                pdir, nm = os.path.split(path)
                if not nm:
                    continue
                try:
                    st = os.stat(path, follow_symlinks=False)
                    is_dir = os.path.isdir(path)
                    size = 0 if is_dir else st.st_size
                    mtime = int(st.st_mtime)
                except OSError:
                    continue
                pid = ix.dir_id.get(pdir)      # already indexed? then update in place
                j = ix.find_id(nm.lower().encode('utf-8', 'surrogatepass'), pid) if pid is not None else -1
                if j >= 0:
                    ix.touch(j, size, mtime)
                else:
                    fresh.append((nm, pdir, is_dir, size, mtime))
            added = ix.add_records(fresh) if fresh else 0

            if removed or added:
                self.live_adds += added
                self.live_dels += removed
                self.last_change = time.time()
                if ix.ndead > 40000 and ix.ndead > ix.n * 0.12:
                    ix.compact()

    # -- search ------------------------------------------------------------------
    def _scan(self, needle: bytes):
        """Byte positions of every occurrence of `needle` in the name blob.

        Rather than looping str.find in Python, this seeds on all positions of
        the needle's rarest-looking byte and then narrows the candidate array
        one byte at a time. Every step is a numpy comparison over C memory, so a
        13 MB blob costs single-digit milliseconds regardless of hit count."""
        arr = self.ix.arr
        L = len(needle)
        if L == 0 or arr.size < L:
            return np.zeros(0, dtype=np.int64)
        limit = arr.size - L + 1
        # Seed on a consonant if there is one: vowels and separators are
        # everywhere, so they make the largest first candidate set.
        best, bi = 0, 0
        for i, b in enumerate(needle):
            score = 3 if b not in b'aeiou_- .' else 1
            if score > best:
                best, bi = score, i
        cand = np.flatnonzero(arr[bi:bi + limit] == needle[bi])
        for i in range(L):
            if cand.size == 0 or i == bi:
                continue
            cand = cand[arr[cand + i] == needle[i]]
        return cand

    def _ids_for(self, needle: bytes):
        pos = self._scan(needle)
        if pos.size == 0:
            return np.zeros(0, dtype=np.int64)
        if pos.size > MAX_HITS:
            pos = pos[:MAX_HITS]
        ids = np.searchsorted(self.ix.off, pos, side='right') - 1
        # positions are ascending, so ids are too: dedupe in one pass instead of a sort
        if ids.size > 1:
            ids = ids[np.concatenate(([True], ids[1:] != ids[:-1]))]
        ids = ids[(ids >= 0) & (ids < self.ix.n)]
        return ids[~self.ix.dead[ids]] if self.ix.ndead else ids

    WORD_SEP = np.frombuffer(b'\x00 _-.([{+,&', dtype=np.uint8)

    def _word_start_ids(self, needle: bytes):
        """Ids whose name has `needle` at the start of a word. Fast for 1-2 letters."""
        ix = self.ix
        pos = self._scan(needle)
        if pos.size == 0:
            return np.zeros(0, dtype=np.int64)
        prev = ix.arr[np.maximum(pos - 1, 0)]
        pos = pos[(pos == 0) | np.isin(prev, self.WORD_SEP)]
        ids = np.searchsorted(ix.off, pos, side='right') - 1
        if ids.size > 1:
            ids = ids[np.concatenate(([True], ids[1:] != ids[:-1]))]
        ids = ids[(ids >= 0) & (ids < ix.n)]
        return ids[~ix.dead[ids]] if ix.ndead else ids

    def _raw_hits(self, q, mode):
        """Sorted unique record ids whose name matches q."""
        ix = self.ix
        if ix.n == 0:
            return np.zeros(0, dtype=np.int64)
        if not q or not q.strip():
            ids = np.arange(ix.n, dtype=np.int64)
            return ids[~ix.dead] if ix.ndead else ids

        if mode == 'regex':
            try:
                rx = re.compile(q.encode('utf-8', 'surrogatepass'), re.IGNORECASE)
            except (re.error, UnicodeError):
                return np.zeros(0, dtype=np.int64)
            pos = [m.start() for m, _ in zip(rx.finditer(ix.blob), range(MAX_HITS))
                   if b'\x00' not in m.group(0)]
            if not pos:
                return np.zeros(0, dtype=np.int64)
            ids = np.unique(np.searchsorted(ix.off, np.asarray(pos, dtype=np.int64),
                                            side='right') - 1)
            ids = ids[(ids >= 0) & (ids < ix.n)]
            return ids[~ix.dead[ids]] if ix.ndead else ids

        if mode == 'fuzzy':
            chars = [c for c in q.lower() if not c.isspace()]
            if not chars:
                return np.arange(ix.n, dtype=np.int64)
            pat = b'[^\x00]*?'.join(re.escape(c.encode('utf-8', 'surrogatepass'))
                                    for c in chars)
            try:
                rx = re.compile(pat)
            except re.error:
                return np.zeros(0, dtype=np.int64)
            pos, p = [], 0          # one match per name: resume at the next one
            while len(pos) < MAX_HITS:
                m = rx.search(ix.blob, p)
                if not m:
                    break
                pos.append(m.start())
                nxt = ix.blob.find(b'\x00', m.start())
                p = nxt + 1 if nxt >= 0 else len(ix.blob)
            if not pos:
                return np.zeros(0, dtype=np.int64)
            ids = np.unique(np.searchsorted(ix.off, np.asarray(pos, dtype=np.int64),
                                            side='right') - 1)
            ids = ids[(ids >= 0) & (ids < ix.n)]
            return ids[~ix.dead[ids]] if ix.ndead else ids

        # Plain mode: every term must appear; longest (rarest) term first.
        terms = [t.encode('utf-8', 'surrogatepass') for t in q.lower().split() if t]
        if not terms:
            return np.arange(ix.n, dtype=np.int64)
        terms.sort(key=len, reverse=True)
        ids = self._ids_for(terms[0])
        for t in terms[1:]:
            if ids.size == 0:
                break
            ids = np.intersect1d(ids, self._ids_for(t), assume_unique=True)
        return ids

    # filters: kind, cats, exts, sizeMin/Max, timeMin/Max, scope, exclude
    def _apply(self, idx, f):
        ix = self.ix
        if idx.size == 0:
            return idx
        m = np.ones(idx.size, dtype=bool)

        kind = f.get('kind', 'all')
        if kind == 'files':
            m &= ix.cat[idx] != CAT_DIR
        elif kind == 'folders':
            m &= ix.cat[idx] == CAT_DIR

        cats = f.get('cats') or []
        if cats:
            want = np.array([CATS.index(c) for c in cats if c in CATS], dtype=np.uint8)
            if want.size:
                m &= np.isin(ix.cat[idx], want)

        exts = [e.lower().lstrip('.') for e in (f.get('exts') or []) if e.strip()]
        if exts:
            ids = np.array([ix.ext_id[e] for e in exts if e in ix.ext_id], dtype=np.int32)
            if ids.size:
                m &= np.isin(ix.extv[idx], ids)
            else:
                m[:] = False                # none of the asked-for extensions exist

        smin, smax = f.get('sizeMin'), f.get('sizeMax')
        if smin is not None:
            m &= ix.size[idx] >= int(smin)
        if smax is not None:
            m &= ix.size[idx] <= int(smax)

        tmin, tmax = f.get('timeMin'), f.get('timeMax')
        if tmin is not None:
            m &= ix.mtime[idx] >= int(tmin)
        if tmax is not None:
            m &= ix.mtime[idx] <= int(tmax)

        scope = (f.get('scope') or '').strip().lower().replace('/', os.sep)
        if scope:
            m &= ix.scope_mask(scope)[ix.parent[idx]]

        exclude = (f.get('exclude') or '').strip().lower().replace('/', os.sep)
        if exclude:
            m &= ~ix.scope_mask(exclude, contains=True)[ix.parent[idx]]

        # Settings: hide system / hidden folders, and folders the user excluded.
        if f.get('hideSystem'):
            par = ix.parent[idx]
            m &= ~ix.flag_mask('system')[par]
            # the system folders and files sitting at a drive root themselves
            roots = np.flatnonzero(m & ix.flag_mask('root')[par])
            for k in roots.tolist():
                j = int(idx[k])
                nm = ix.name(j).lower()
                if nm in SYSTEM_ROOT_FILES or _is_system_dir('\\' + nm):
                    m[k] = False
        if f.get('hideHidden'):
            m &= ~ix.flag_mask('hidden')[ix.parent[idx]]
            m &= ix.arr[ix.off[idx]] != 46          # names starting with '.' themselves
        for d in f.get('excludeDirs') or []:
            d = str(d).strip().lower().rstrip('\\/').replace('/', os.sep)
            if d:
                dm = ix.scope_mask(d)
                m &= ~dm[ix.parent[idx]]
        if f.get('cloud'):                          # 'any' or one provider's sync folders
            dm = self._cloud_dirs(f['cloud'])
            if dm is None:
                m[:] = False
            else:
                m &= dm[ix.parent[idx]]

        return idx[m]

    def _cloud_dirs(self, which='any'):
        """Per-directory mask: inside a cloud sync folder (all, or one provider). None when there are none."""
        ix = self.ix
        roots = [r for r, p in cloud.sync_roots() if which == 'any' or p == which]
        if not roots:
            return None
        dm = np.zeros(len(ix.dirs), dtype=bool)
        for r in roots:
            dm |= ix.scope_mask(r.lower().rstrip('\\'))
        return dm

    def _cloud_counts(self, base):
        """[[provider, n]] in results before the cloud filter, so each chip shows what it would give."""
        provs = list(dict.fromkeys(p for _r, p in cloud.sync_roots()))
        if not provs or not base.size:
            return [[p, 0] for p in provs]
        par = self.ix.parent[base]
        return [[p, int(self._cloud_dirs(p)[par].sum())] for p in provs]

    def _cloud_fields(self, row):
        prov = cloud.provider(row['dir'])
        if prov:
            row['cloud'] = prov
            if row['cat'] != 'folder':
                row['online'] = cloud.online_only(os.path.join(row['dir'], row['name']), prov)
        return row

    # -- relevance: what a person most likely means ------------------------------
    APP_EXT = ('lnk', 'exe', 'appref-ms', 'url')

    def _relevance(self, ids, q):
        """Score ids for query q (higher is better). Vectorised over all hits."""
        ix = self.ix
        score = np.zeros(ids.size, dtype=np.float64)
        if not ids.size:
            return score
        starts = ix.off[ids]
        L = ix.off[ids + 1] - starts - 1
        last = ix.arr.size - 1
        term = (q.strip().lower().split() or [''])[0].encode('utf-8', 'surrogatepass')
        if term:
            n = len(term)
            pref = L >= n
            for k, ch in enumerate(term):
                pref &= ix.arr[np.minimum(starts + k, last)] == ch
            after = ix.arr[np.minimum(starts + n, last)]
            exact = pref & ((L == n) | (after == 46))          # "report" == "report.pdf"
            score += 3.0 * pref + 4.0 * exact
        age_days = np.maximum(time.time() - ix.mtime[ids], 0) / 86400.0
        score += 2.0 * np.exp(-age_days / 120.0)
        score -= 0.03 * np.minimum(L, 120)
        par = ix.parent[ids]
        score -= 0.004 * np.minimum(ix.dir_lengths()[par], 200)
        app_ext = np.array([ix.ext_id[e] for e in self.APP_EXT if e in ix.ext_id], dtype=np.int32)
        if app_ext.size:
            score += 6.0 * (ix.flag_mask('startmenu')[par] & np.isin(ix.extv[ids], app_ext))
        bids, bval = self._pick_boost(q)
        if bids.size:
            k = np.minimum(np.searchsorted(bids, ids), bids.size - 1)
            score += np.where(bids[k] == ids, bval[k], 0.0)
        return score

    # -- learned picks: what you opened before ranks higher ---------------------
    def _path_ids(self, pairs):
        """{record id: summed bonus} for (path, bonus) pairs that are in the index."""
        ix, out = self.ix, {}
        for p, b in pairs:
            d, n = os.path.split(p)
            pid = ix.dir_id.get(d)
            if pid is None:
                continue
            j = ix.find_id(n.lower().encode('utf-8', 'surrogatepass'), pid)
            if j >= 0:
                out[j] = out.get(j, 0.0) + b
        return out

    def _pick_boost(self, q):
        """(sorted ids, bonus per id): frecency (cached per index state) plus this query's top hits."""
        P, ix = self.picks, self.ix
        if not P.paths:
            return np.zeros(0, dtype=np.int64), np.zeros(0)
        key = (P.version, id(ix), ix.n, ix.ndead)
        if self._pick_cache is None or self._pick_cache[0] != key:
            self._pick_cache = (key, self._path_ids(P.frecency()))
        b = dict(self._pick_cache[1])
        for j, v in self._path_ids(P.top_hits(q)).items():
            b[j] = b.get(j, 0.0) + v
        ids = np.fromiter(b.keys(), dtype=np.int64, count=len(b))
        val = np.fromiter(b.values(), dtype=np.float64, count=len(b))
        o = np.argsort(ids)
        return ids[o], val[o]

    def inside(self, q, limit=4, filters=None, skip=()):
        """Quick bar rows for files whose text contains q (Windows Search), best first."""
        paths = contentsearch.search(q, limit * 4 + len(skip))
        skip = {p.lower() for p in skip}
        with self.lock:
            ids = [j for j in self._path_ids((p, 0.0) for p in paths if p.lower() not in skip)
                   if self.ix.cat[j] != CAT_ARC]          # a zip "contains" the names of its files: noise
            ids = self._apply(np.asarray(ids, dtype=np.int64), dict(filters or {}))[:limit]
            return [dict(self._row(int(j)), inside=True) for j in ids.tolist()]

    def _best_order(self, ids, q, top=None):
        """ids ordered best first; with `top`, only the first `top` are fully sorted."""
        score = self._relevance(ids, q)
        if top and ids.size > top:
            part = np.argpartition(-score, top)[:top]
            ids, score = ids[part], score[part]
        # word-start bonus on the short list ("tax 2026" matches "Income tax 2026.pdf" well)
        term = (q.strip().lower().split() or [''])[0]
        if term and ids.size <= 2000:
            ix = self.ix
            for j, i in enumerate(ids.tolist()):
                name = ix.name(i).lower()
                at = name.find(term)
                if at > 0 and not name[at - 1].isalnum():
                    score[j] += 2.0
        return ids[np.argsort(-score, kind='stable')]

    def _row(self, j, q=''):
        ix = self.ix
        name = ix.name(j)
        d = ix.dirs[ix.parent[j]]
        cat = CATS[int(ix.cat[j])]
        ext = ix.exts[int(ix.extv[j])]
        if ext in self.APP_EXT and '\\start menu\\' in d.lower():
            cat = 'app'
        term = (q.strip().lower().split() or [''])[0]
        at = name.lower().find(term) if term else -1
        return self._cloud_fields({'name': name, 'dir': d, 'path': d + ('' if d.endswith(('\\', '/')) else os.sep) + name,
                'ext': ext, 'cat': cat, 'size': int(ix.size[j]), 'mtime': int(ix.mtime[j]),
                'hl': [at, at + len(term)] if at >= 0 else None})

    def quick(self, q, limit=8, filters=None):
        """The quick bar's search: best matches only, or recent files for an empty query."""
        self.last_query = time.time()
        if self.pending:
            try:
                self._drain()
            except Exception:
                pass
        with self.lock:
            f = dict(filters or {})
            q = (q or '').strip()
            if not q:
                ids = self._apply(self._raw_hits('', 'substring'), {**f, 'kind': 'files'})
                if ids.size > limit:
                    ids = ids[np.argpartition(-self.ix.mtime[ids], limit)[:limit]]
                ids = ids[np.argsort(-self.ix.mtime[ids], kind='stable')]
            else:
                term = q.lower().encode('utf-8', 'surrogatepass')
                if len(term) <= 2 and b' ' not in term:
                    hits = self._word_start_ids(term)      # Spotlight-style: "ch" -> Chrome
                else:
                    hits = self._raw_hits(q, 'substring')
                if not hits.size:
                    hits = self._raw_hits(q, 'fuzzy')
                ids = self._apply(hits, f)
                self.last_quick_total = int(ids.size)          # "+N more" in the quick bar
                ids = self._best_order(ids, q, top=300)[:limit]
            return [self._row(int(j), q) for j in ids.tolist()]

    # The one call the UI makes for every keystroke.
    def query(self, q, mode, filters, sort, desc, offset, limit):
        content = None
        if mode == 'content':
            if (q or '').strip():
                content = contentsearch.search(q, 500)      # outside the lock: may be slow
            else:
                mode = 'substring'
        key = (q, mode, repr(sorted((filters or {}).items())), sort, desc)
        if key != getattr(self, '_last_key', None):        # a page refresh repeats the same key
            self._last_key = key
            self.last_query = time.time()
        if self.pending:
            try:
                self._drain()
            except Exception:
                pass
        with self.lock:
            ix = self.ix
            t0 = time.perf_counter()
            if content is not None:
                hits = np.unique(np.fromiter(self._path_ids((p, 0.0) for p in content), dtype=np.int64))
            else:
                hits = self._raw_hits(q or '', mode)
            faceted = bool(filters.get('cats')) or bool(filters.get('exts'))
            if faceted:
                base = self._apply(hits, {k: v for k, v in filters.items()
                                          if k not in ('cats', 'exts')})
                sel = self._apply(hits, filters)
            else:
                base = sel = self._apply(hits, filters)

            # Category counts come from `base` (before the category/extension
            # facets), so the chips show what each choice would give.
            cat_counts = np.bincount(ix.cat[base], minlength=8).tolist() if base.size else [0] * 8

            # Extension counts: everything but the extension filter applied.
            ext_base = self._apply(hits, {k: v for k, v in filters.items() if k != 'exts'}) \
                if filters.get('exts') else sel
            ext_counts = []
            if ext_base.size:
                files = ext_base[ix.cat[ext_base] != CAT_DIR]
                if files.size:
                    bc = np.bincount(ix.extv[files], minlength=len(ix.exts))
                    top = np.argsort(bc)[::-1][:60]
                    ext_counts = [[ix.exts[int(i)], int(bc[i])] for i in top.tolist()
                                  if bc[i] > 0 and ix.exts[int(i)]]

            # Histograms for the time and size sliders.
            hist = {'time': [], 'size': [], 'tmin': 0, 'tmax': 0}
            if sel.size:
                mt = ix.mtime[sel]
                mt = mt[mt > 315619200]        # 0 / 1 Jan 1980 = no real date: the graph starts at the oldest real one
                if mt.size:
                    lo, hi = int(mt.min()), int(mt.max())
                    if hi <= lo:
                        hi = lo + 1
                    counts, edges = np.histogram(mt, bins=48, range=(lo, hi))
                    hist['time'] = counts.tolist()
                    hist['tmin'], hist['tmax'] = lo, hi
                sz = ix.size[sel]
                lg = np.log10(np.maximum(sz.astype(np.float64), 1.0))
                # the axis runs from the smallest to the biggest file in these results, not a fixed 1 B - 100 GB
                slo = float(np.floor(lg.min() * 10) / 10) if lg.size else 0.0
                shi = max(slo + 0.1, float(np.ceil(lg.max() * 10) / 10) if lg.size else 11.0)
                counts, _ = np.histogram(lg, bins=40, range=(slo, shi))
                hist['size'] = counts.tolist()
                hist['slo'], hist['shi'] = slo, shi

            # Sorting uses the precomputed ranks, never Python string compares.
            if sel.size and sort == 'best' and (q or '').strip():
                # best matches first; only the visible pages need a full sort
                best = self._best_order(sel, q, top=max(2000, offset + limit))
                if best.size < sel.size:          # the rest follow, unranked, so totals stay right
                    sel = np.concatenate([best, sel[~np.isin(sel, best)]])
                else:
                    sel = best
                if desc:
                    sel = sel[::-1]
            elif sel.size:
                if sort == 'size':
                    order = np.argsort(ix.size[sel], kind='stable')
                elif sort == 'modified':
                    order = np.argsort(ix.mtime[sel], kind='stable')
                elif sort == 'path':
                    order = np.lexsort((ix.rank[sel], ix.dirrank_now()[ix.parent[sel]]))
                else:
                    order = np.argsort(ix.rank[sel], kind='stable')
                if desc:
                    order = order[::-1]
                sel = sel[order]

            total = int(sel.size)
            page = sel[offset:offset + limit]
            rows = []
            for j in page.tolist():
                rows.append(self._cloud_fields({
                    'name': ix.name(j),
                    'dir': ix.dirs[ix.parent[j]],
                    'size': int(ix.size[j]),
                    'mtime': int(ix.mtime[j]),
                    'cat': CATS[int(ix.cat[j])],
                    'ext': ix.exts[int(ix.extv[j])],
                }))
            cloud_base = self._apply(hits, {k: v for k, v in filters.items() if k != 'cloud'}) \
                if filters.get('cloud') else sel
            return {
                'total': total,
                'rows': rows,
                'clouds': self._cloud_counts(cloud_base) if offset == 0 else [],
                'cats': {CATS[i]: int(c) for i, c in enumerate(cat_counts)},
                'exts': ext_counts,
                'hist': hist,
                'ms': round((time.perf_counter() - t0) * 1000, 2),
                'capped': bool(q and q.strip() and hits.size >= MAX_HITS),   # empty queries are never cut
            }

    # -- indexing ----------------------------------------------------------------
    def reindex(self, roots):
        threading.Thread(target=self._reindex, args=(roots,), daemon=True).start()

    def _reindex(self, roots):
        try:
            self.state = 'scanning'
            self.progress = 0
            recs = None
            # Phase 1: NTFS names in seconds. Needs elevation; quietly
            # skipped when unavailable.
            if IS_WIN:
                letters = sorted({r[0].upper() for r in roots if len(r) > 1 and r[1] == ':'})
                got = []
                for L in letters:
                    self.message = f'Reading NTFS records on {L}:'
                    part = usn_records(L)
                    if part:
                        got.extend(part)
                if got:
                    recs = got
                    ix = Index(); ix.roots = roots; ix.build(recs)
                    with self.lock:
                        self.ix = ix
                    self.state = 'enriching'
                    self.message = f'{ix.n:,} names ready — filling in size and date'

            # Phase 2: the full walk, with sizes and dates.
            self.message = self.message if recs else 'Walking the filesystem'
            walked = scan_tree(roots, progress=self._tick)
            ix = Index(); ix.roots = roots
            ix.build(walked)
            ix.enriched = True
            with self.lock:
                self.ix = ix
            self.state = 'ready'
            self.progress = ix.n
            self.message = f'{ix.n:,} items indexed'
            try:
                self.start_watch(roots)
            except Exception:
                self.live = False
            try:
                ix.save(CACHE)
                self.saved_gen, self.saved_at = ix.gen, time.time()
            except Exception as e:
                self.message += f'  (cache not written: {e})'
        except Exception as e:
            self.state = 'ready' if self.ix.n else 'empty'
            self.message = f'Indexing failed: {e}'

    def _tick(self, n):
        self.progress = n
        self.message = f'Walking the filesystem — {n:,} items'


ENGINE = Engine()

# -- visual search ------------------------------------------------------------
# Image/video lookup ("find the original of this edited picture") lives in
# visual.py. It pulls in Pillow, OpenCV and OpenVINO, so it is imported only
# when first needed: a filename-only user never pays for it, and a missing
# dependency becomes a message in the UI instead of a crash at startup.
# _visual['dirty'] is set when the watcher sees a change under a folder the
# visual index covers; _visual_loop turns that into a rescan.
VISUAL_DIR = os.path.join(DATA_DIR, 'visual-index')
_visual = {'mod': None, 'error': None, 'dirty': False}
_visual_lock = threading.Lock()


def visual():
    with _visual_lock:
        if _visual['mod'] is None and _visual['error'] is None:
            try:
                models = features.pack_activate()      # optional download in light builds
                import visual as v
                if models:
                    from pathlib import Path
                    v.MODELS = Path(models)
                v.start(VISUAL_DIR, model_cache=os.path.join(DATA_DIR, 'model-cache'))
                _visual['mod'] = v
            except Exception as e:
                _visual['error'] = (f'{e}. Visual search needs: pip install pillow opencv-python-headless openvino'
                                    if isinstance(e, ImportError) else str(e))
        return _visual['mod']


def visual_touched(paths):
    """Called with paths the watcher saw change; new or edited images in chosen folders get fingerprinted."""
    v = _visual['mod']
    if v and any(v.covers(p) for p in paths):
        _visual['dirty'] = True


def _visual_loop():
    # Start the visual index at launch only if the user has used it before,
    # and not during boot: its folder check can load the model.
    time.sleep(180)
    if os.path.exists(os.path.join(VISUAL_DIR, 'lens.db')) and features.pro():
        visual()
    last = time.time()
    while True:
        time.sleep(10)
        v = _visual['mod']
        # Rescan on watcher changes, or every half hour if some folder is not watched.
        if v and (_visual['dirty'] or (time.time() - last > 1800 and v.has_unwatched())):
            _visual['dirty'], last = False, time.time()
            v.request_scan()


# -- HTTP server --------------------------------------------------------------
# Serves ui.html and a small JSON API on 127.0.0.1:PORT.

def default_roots():
    if os.environ.get('RIFFLE_ROOTS'):          # testing / demos: index only these folders
        return os.environ['RIFFLE_ROOTS'].split(';')
    if IS_WIN:
        out = []
        for c in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
            p = f'{c}:\\'
            if os.path.isdir(p):
                out.append(p)
        return out or ['C:\\']
    return [os.path.expanduser('~')]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        """Only Riffle's own window may talk to this server. Without this, any web page could POST to
127.0.0.1 (e.g. /api/act recycle) or read files through it via DNS rebinding."""
        ok_hosts = (f'127.0.0.1:{PORT}', f'localhost:{PORT}')
        origin = self.headers.get('Origin')
        if self.headers.get('Host', '') in ok_hosts and (origin is None or origin in tuple('http://' + h for h in ok_hosts)):
            return True
        self._send(403, json.dumps({'error': 'forbidden'}))
        return False

    def do_GET(self):
        if not self._local():
            return
        path = self.path.split('?')[0]
        if path == '/api/voice/state':
            return self._send(200, json.dumps({k: VOICE[k] for k in ('text', 'final', 'listening', 'error', 'seq')}
                                              | {'levels': VOICE['levels'][-40:],
                                                 'installed': bool(features.voice_models())}))
        if path == '/api/picks':
            n = 100 if 'all=1' in self.path else 8
            return self._send(200, json.dumps({'items': recent_picks(n)}))
        if features.get(self, path):
            return
        if path == '/api/visual/status':
            if _visual['mod'] is None and not os.path.exists(os.path.join(VISUAL_DIR, 'lens.db')):
                # Never used yet: answer without loading the models.
                home = os.path.expanduser('~')
                sugg = [p for p in (os.path.join(home, d) for d in ('Pictures', 'Downloads', 'Desktop', 'Videos'))
                        if os.path.isdir(p)]
                return self._send(200, json.dumps({'available': True, 'fresh': True, 'images': 0, 'videos': 0,
                                                   'folders': [], 'index': {'running': False}, 'suggested': sugg,
                                                   'google_key': False}))
            if _visual['mod'] is None:
                # Asleep: answer from the sqlite file. Only real use (a search, adding a
                # folder, a rescan) or the deferred start in _visual_loop wakes it.
                light = features.visual_status_light(VISUAL_DIR)
                if light:
                    return self._send(200, json.dumps(light))
            v = visual()
            return self._send(200, json.dumps(v.status() if v else {'available': False, 'reason': _visual['error']}))
        if path.startswith('/visual/thumb/'):
            v = visual()
            f = os.path.join(VISUAL_DIR, 'thumbs', f"{int(path.rsplit('/', 1)[1])}.jpg")
            if not v or not os.path.exists(f):
                return self._send(404, 'no thumbnail', 'text/plain')
            with open(f, 'rb') as fh:
                return self._send(200, fh.read(), 'image/jpeg')
        if path == '/favicon.ico' and ICON:
            with open(ICON, 'rb') as fh:
                return self._send(200, fh.read(), 'image/x-icon')
        if path in ('/', '/index.html'):
            f = os.path.join(RES, 'ui.html')
            if not os.path.exists(f):
                return self._send(500, 'ui.html is missing — keep it next to riffle.py',
                                  'text/plain; charset=utf-8')
            with open(f, 'rb') as fh:
                return self._send(200, features.inject(fh.read()), 'text/html; charset=utf-8')
        if path == '/api/status':
            ix = ENGINE.ix
            return self._send(200, json.dumps({
                'state': ENGINE.state, 'message': ENGINE.message,
                'progress': ENGINE.progress, 'count': ix.n - ix.ndead,
                'gen': ix.gen, 'live': ENGINE.live,
                'watch': ENGINE.watcher.alive_mode if ENGINE.watcher else 'none',
                'liveAdds': ENGINE.live_adds, 'liveDels': ENGINE.live_dels,
                'lastChange': ENGINE.last_change,
                'enriched': ix.enriched, 'roots': ix.roots or default_roots(),
                'builtAt': ix.built_at, 'drives': default_roots(),
                'platform': 'windows' if IS_WIN else sys.platform,
            }))
        return self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        if not self._local():
            return
        n = int(self.headers.get('Content-Length', 0))
        path = self.path.split('?')[0]

        if path.startswith('/api/visual/') and path != '/api/visual/openlink' and not features.pro():
            if n:
                self.rfile.read(n)                   # drain the body so the connection stays usable
            return self._send(402, json.dumps({'error': 'pro', 'feature': 'visual'}))

        if path == '/api/visual/search':          # raw image/video bytes, not JSON
            if not n or n > 1 << 30:
                return self._send(400, json.dumps({'error': 'Send one image or video under 1 GB.'}))
            data = self.rfile.read(n)
            v = visual()
            if not v:
                return self._send(503, json.dumps({'error': _visual['error'] or 'Visual search is starting'}))
            try:
                results, qimg = v.search_bytes(data, self.headers.get('Content-Type', ''))
            except Exception as e:
                return self._send(422, json.dumps({'error': f"Couldn't read that file: {e}"}))
            q = None
            if qimg is not None:
                w, h = qimg.info.get('true_size', qimg.size)
                q = {'w': w, 'h': h}
            return self._send(200, json.dumps({'query': q, 'results': [v.public(r) for r in results]}))

        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self._send(400, json.dumps({'error': 'bad json'}))
        if path == '/api/voice/start':
            return self._send(200, json.dumps(voice_web_start()))
        if path == '/api/voice/stop':
            if VOICE['v']:
                VOICE['v'].stop()
            return self._send(200, json.dumps({'ok': True}))
        if features.post(self, path, body):
            return

        if path.startswith('/api/visual/'):
            v = visual()
            if not v:
                return self._send(503, json.dumps({'error': _visual['error'] or 'Visual search is starting'}))
            if path == '/api/visual/folder':
                targets = v.suggested_folders() if body.get('suggested') else [v.pick_folder() if body.get('pick') else body.get('path', '')]
                for t in targets:
                    try:
                        v.add_folder(t)
                    except v.SourceError as e:
                        return self._send(400, json.dumps({'error': str(e), 'need_key': e.need_key}))
            elif path == '/api/visual/key':
                try:
                    v.set_google_key(body.get('key', ''))
                except v.SourceError as e:
                    return self._send(400, json.dumps({'error': str(e)}))
            elif path == '/api/visual/openlink':
                # Only Google links: the page must not be able to open arbitrary URLs.
                url = body.get('url', '')
                if not url.startswith(('https://drive.google.com/', 'https://console.cloud.google.com/')):
                    return self._send(400, json.dumps({'error': 'not a Google Drive link'}))
                webbrowser.open(url)
            elif path == '/api/visual/folder/remove':
                v.remove_folder(body.get('path', ''))
            elif path == '/api/visual/rescan':
                v.request_scan()
            else:
                return self._send(404, json.dumps({'error': 'not found'}))
            return self._send(200, json.dumps(v.status()))

        if path == '/api/search':
            try:
                r = ENGINE.query(
                    body.get('q', ''), body.get('mode', 'substring'),
                    body.get('filters', {}) or {}, body.get('sort', 'name'),
                    bool(body.get('desc')), int(body.get('offset', 0)),
                    min(500, int(body.get('limit', 200))))
                return self._send(200, json.dumps(r))
            except Exception as e:
                return self._send(200, json.dumps({'error': str(e), 'total': 0, 'rows': []}))

        if path == '/api/reindex':
            roots = body.get('roots') or default_roots()
            ENGINE.reindex(roots)
            return self._send(200, json.dumps({'ok': True}))

        if path == '/api/act':
            if body.get('action') in ('open', 'reveal', 'openfolder'):
                for p in (body.get('paths') or [])[:3]:
                    ENGINE.picks.record(p, body.get('q', ''))
            return self._send(200, json.dumps(shell_act(body.get('action', ''),
                                                        body.get('paths') or [])))

        return self._send(404, json.dumps({'error': 'not found'}))


# -- file actions -------------------------------------------------------------
# Open, reveal, copy, rename, recycle: all run here, never in the page.

def shell_act(action, paths):
    """Run a file action. Everything the context menu offers routes through here
so the browser never needs shell privileges of its own."""
    paths = [p for p in paths if p]
    if not paths:
        return {'ok': False, 'error': 'nothing selected'}
    live = [p for p in paths if os.path.lexists(p)]
    if not live and action != 'copy':
        return {'ok': False, 'error': 'that path is no longer there'}
    try:
        if action == 'open':
            for p in live[:12]:
                if IS_WIN:
                    os.startfile(p)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', p])
                else:
                    subprocess.Popen(['xdg-open', p])

        elif action == 'reveal':
            for p in live[:8]:
                if IS_WIN:
                    subprocess.Popen(['explorer', '/select,', os.path.normpath(p)])
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', '-R', p])
                else:
                    subprocess.Popen(['xdg-open', os.path.dirname(p)])

        elif action == 'openfolder':
            seen = set()
            for p in live[:8]:
                d = p if os.path.isdir(p) else os.path.dirname(p)
                if d in seen:
                    continue
                seen.add(d)
                if IS_WIN:
                    os.startfile(d)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', d])
                else:
                    subprocess.Popen(['xdg-open', d])

        elif action == 'openwith':
            if not IS_WIN:
                return {'ok': False, 'error': 'Windows only'}
            subprocess.Popen(['rundll32.exe', 'shell32.dll,OpenAs_RunDLL',
                              os.path.normpath(live[0])])

        elif action == 'properties':
            if not IS_WIN:
                return {'ok': False, 'error': 'Windows only'}
            p = os.path.normpath(live[0])
            ps = ("$s=New-Object -ComObject Shell.Application;$f=$s.Namespace('"
                  f"{os.path.dirname(p)}');$i=$f.ParseName('"
                  f"{os.path.basename(p)}');$i.InvokeVerb('Properties')")
            subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden',
                              '-Command', ps])

        elif action == 'recycle':
            return recycle(live)

        else:       # 'copy' is done by the page itself (clipboard)
            return {'ok': False, 'error': 'unknown action'}
        return {'ok': True, 'count': len(live)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def recycle(paths):
    """Send to the Recycle Bin (never a hard delete)."""
    if IS_WIN:
        try:
            from ctypes import wintypes
            shell32 = ctypes.WinDLL('shell32', use_last_error=True)

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                            ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                            ('fFlags', ctypes.c_uint16), ('fAnyOperationsAborted', wintypes.BOOL),
                            ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]

            FO_DELETE = 3
            FOF_ALLOWUNDO = 0x40
            FOF_NOCONFIRMATION = 0x10
            FOF_SILENT = 0x4
            buf = '\x00'.join(os.path.normpath(p) for p in paths) + '\x00\x00'   # double-NUL list
            op = SHFILEOPSTRUCTW(None, FO_DELETE, buf, None,
                                 FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT, False, None, None)
            rc = shell32.SHFileOperationW(ctypes.byref(op))
            if rc != 0:
                return {'ok': False, 'error': f'shell refused (code {rc})'}
            return {'ok': True, 'count': len(paths)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    try:
        import send2trash
        for p in paths:
            send2trash.send2trash(p)
        return {'ok': True, 'count': len(paths)}
    except Exception:
        return {'ok': False, 'error': 'recycle needs Windows, or `pip install send2trash`'}


# -- app window -----------------------------------------------------------------
# A chromeless browser window, branded as Riffle on the taskbar.

APP_TITLE = 'Riffle'

def find_browser():
    """A Chromium browser lets us open a real app window with no browser chrome."""
    cands = []
    if IS_WIN:
        pf = [os.environ.get('PROGRAMFILES', 'C:\\Program Files'),
              os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)'),
              os.environ.get('LOCALAPPDATA', '')]
        for b in pf:
            if not b:
                continue
            # Edge first: it ships with Windows, so it is almost always there.
            cands += [os.path.join(b, 'Microsoft\\Edge\\Application\\msedge.exe'),
                      os.path.join(b, 'Google\\Chrome\\Application\\chrome.exe'),
                      os.path.join(b, 'BraveSoftware\\Brave-Browser\\Application\\brave.exe')]
    elif sys.platform == 'darwin':
        cands = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    else:
        from shutil import which
        cands = [which(x) for x in ('google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge')]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def open_window(url):
    """Chromeless app window if we can, ordinary tab if not."""
    b = find_browser()
    if b:
        profile = os.path.join(DATA_DIR, 'window')     # own profile: own window, own icon
        try:
            subprocess.Popen([b, f'--app={url}', f'--user-data-dir={profile}',
                              '--no-first-run', '--no-default-browser-check',
                              '--window-size=1180,760'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Thread(target=brand_window, daemon=True).start()
            return True
        except Exception:
            pass
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _find_window():
    """The Riffle app window (a Chromium window titled "Riffle..."), or None."""
    u32 = ctypes.WinDLL('user32', use_last_error=True)
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _):
        title, cls = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
        u32.GetWindowTextW(hwnd, title, 64)
        u32.GetClassNameW(hwnd, cls, 64)
        if title.value.startswith(APP_TITLE) and cls.value == 'Chrome_WidgetWin_1' and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False                # stop enumerating
        return True
    u32.EnumWindows(enum_proc, None)
    return found[0] if found else None


def brand_window():
    """Make the app window Riffle's own taskbar button.

Left alone, Windows files the window under Edge (Edge's icon, grouped with Edge, and pinning it pins Edge).
Setting the window's AppUserModel properties gives it Riffle's ID, icon and relaunch command instead."""
    if not IS_WIN:
        return
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [('b', ctypes.c_ubyte * 16)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [('fmtid', GUID), ('pid', ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [('vt', ctypes.c_ushort), ('pad', ctypes.c_ushort * 3), ('p', ctypes.c_void_p), ('p2', ctypes.c_void_p)]

    def guid(s):
        g = GUID(); ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16); return g

    if getattr(sys, 'frozen', False):
        cmd, icon = f'"{sys.executable}"', f'{sys.executable},0'
    else:
        exe = sys.executable.replace('python.exe', 'pythonw.exe')
        cmd, icon = f'"{exe}" "{os.path.abspath(__file__)}"', ICON or ''
    aum = guid('{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}')   # PKEY_AppUserModel_* format id
    # pids: 2 RelaunchCommand, 3 RelaunchIconResource, 4 RelaunchDisplayNameResource, 5 ID
    props = [(pid, ctypes.create_unicode_buffer(t)) for pid, t in ((2, cmd), (3, icon), (4, APP_TITLE), (5, APP_ID)) if t]
    iid = guid('{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}')   # IID_IPropertyStore
    ctypes.windll.ole32.CoInitialize(None)
    # The browser takes a moment to open its window; keep re-applying for ~20 s
    # since Chromium may recreate the window while it starts up.
    for _ in range(40):
        time.sleep(0.5)
        hwnd = _find_window()
        if not hwnd:
            continue
        try:
            store = ctypes.c_void_p()
            if ctypes.windll.shell32.SHGetPropertyStoreForWindow(ctypes.c_void_p(hwnd), ctypes.byref(iid), ctypes.byref(store)):
                continue
            vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                           ctypes.POINTER(PROPVARIANT))(vtbl[6])        # IPropertyStore::SetValue
            for pid, buf in props:
                pv = PROPVARIANT(vt=31, p=ctypes.cast(buf, ctypes.c_void_p))      # VT_LPWSTR
                set_value(store, ctypes.byref(PROPERTYKEY(aum, pid)), ctypes.byref(pv))
            ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])(store)     # Commit
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(store)    # Release
        except Exception as e:
            print(f'taskbar icon: {e}', file=sys.stderr)
            return


def focus_window():
    """Raise an existing Riffle window. Returns True if one was found."""
    if not IS_WIN:
        return False
    try:
        h = _find_window()
        if not h:
            return False
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        u32.ShowWindow(ctypes.c_void_p(h), 9)          # SW_RESTORE
        u32.SetForegroundWindow(ctypes.c_void_p(h))
        return True
    except Exception:
        return False


HOTKEY = {'combo': '', 'ok': False, 'tid': 0}
QUICK = {'bar': None}


def _system_theme():
    """'light' or 'dark' from the Riffle setting, or from Windows when set to follow it."""
    t = features.settings().get('theme', 'system')
    if t in ('light', 'dark'):
        return t
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return 'light' if winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] else 'dark'
    except OSError:
        return 'light'


def _profile_threads(interval=5.0):
    """RIFFLE_PROFILE=1: every few seconds print CPU ms per Python thread and the
    busiest thread's current stack. Diagnostic only, off unless asked for."""
    import traceback
    k32 = ctypes.windll.kernel32
    FT = ctypes.c_ulonglong

    def cpu_ms(tid):
        h = k32.OpenThread(0x0800, False, tid)          # THREAD_QUERY_LIMITED_INFORMATION
        if not h:
            return 0.0
        c, e, kt, ut = FT(), FT(), FT(), FT()
        k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
        k32.CloseHandle(h)
        return (kt.value + ut.value) / 10000.0
    last = {}
    while True:
        time.sleep(interval)
        rows = []
        frames = sys._current_frames()
        for t in threading.enumerate():
            if not t.native_id:
                continue
            now = cpu_ms(t.native_id)
            d = now - last.get(t.native_id, now)
            last[t.native_id] = now
            rows.append((d, t))
        rows.sort(key=lambda r: -r[0])
        print('[profile] ' + ', '.join(f'{t.name}={d:.0f}ms' for d, t in rows[:5]), flush=True)
        if rows and rows[0][0] > 20 and rows[0][1].ident in frames:
            st = traceback.extract_stack(frames[rows[0][1].ident])[-4:]
            print('[profile]   ' + ' <- '.join(f'{os.path.basename(f.filename)}:{f.lineno} {f.name}'
                                              for f in reversed(st)), flush=True)


# -- voice: one offline engine shared by the quick bar and the window -------------------
VOICE = {'v': None, 'text': '', 'final': False, 'listening': False, 'error': None, 'levels': [], 'seq': 0}


def voice_engine():
    models = features.voice_models()
    if not models:
        return None
    if VOICE['v'] is None:
        import voice as voice_mod
        VOICE['v'] = voice_mod.Voice(models)
    return VOICE['v']


def voice_web_start():
    """The window's voice mode: listen, and let the page poll /api/voice/state for words and levels."""
    v = voice_engine()
    if not v:
        return {'installed': False, 'error': 'Voice search is not installed yet. Settings › Quick bar › Voice search adds it.'}
    if v.busy:
        v.stop()
        for _ in range(30):
            if not v.busy:
                break
            time.sleep(0.05)
    VOICE.update(text='', final=False, listening=True, error=None, levels=[], seq=VOICE['seq'] + 1)
    seq = VOICE['seq']

    def on_text(text, final):
        if seq == VOICE['seq']:
            VOICE['text'] = text
            VOICE['final'] = VOICE['final'] or final

    def on_level(level):
        if seq == VOICE['seq']:
            VOICE['levels'] = (VOICE['levels'] + [round(level, 3)])[-48:]

    def on_end(err):
        if seq == VOICE['seq']:
            VOICE.update(listening=False, error=err)
    v.start(on_text, on_level, on_end)
    return {'installed': True, 'seq': seq}


def recent_picks(n=8):
    """Files you opened with Riffle, newest first: the window's "Jump back in"."""
    P = ENGINE.picks
    with P.lock:
        items = sorted(P.paths.values(), key=lambda v: -v[2])
    out = []
    for path, count, last in items:
        if len(out) >= n:
            break
        if not os.path.isabs(path) or not os.path.exists(path):
            continue                                   # websites, settings pages, deleted files
        try:
            st = os.stat(path)
        except OSError:
            continue
        name, d = os.path.basename(path), os.path.dirname(path)
        isdir = os.path.isdir(path)
        ext = '' if isdir else os.path.splitext(name)[1][1:].lower()
        out.append({'path': path, 'name': name, 'dir': d, 'ext': ext, 'size': 0 if isdir else st.st_size,
                    'mtime': int(st.st_mtime), 'opened': int(last), 'count': count,
                    'cat': 'folder' if isdir else CATS[_EXT_CAT.get(ext, CAT_OTHER)]})
    return out


def start_quickbar(url):
    """Create the hidden quick bar (native Tk window) so the hotkey shows it instantly."""
    try:
        import quickbar
        import shellthumb
    except Exception as e:
        print(f'[riffle] quick bar unavailable: {e}')
        return

    def bar_filters():
        s = features.settings()
        f = {}
        if s.get('hideSystem', True):
            f['hideSystem'] = True
        if s.get('hideHidden', True):
            f['hideHidden'] = True
        if s.get('excludeDirs'):
            f['excludeDirs'] = s['excludeDirs']
        return f

    apps = sources.Apps(os.path.join(DATA_DIR, 'apps.json'))
    history = sources.History()
    QUICK['history'] = history

    def search(q, limit):
        """Everything the bar shows for q: a Top hit, then Apps, Settings, Files, Websites, then the web row."""
        s = features.settings()
        f = bar_filters()
        q = (q or '').strip()
        if q in ('?', 'help'):
            return sources.help_rows()
        if not q:
            return [dict(r, group='Recent') for r in ENGINE.quick('', 5, f)]     # 5 keeps the bar compact
        if s.get('quickApps', True):
            apps.refresh()
        if s.get('quickHistory', True):
            history.refresh()
        app_rows = apps.rows(q, 2) if s.get('quickApps', True) else []
        names = {a['name'].lower() for a in app_rows}
        files = []
        for i, r in enumerate(ENGINE.quick(q, 4, f)):
            stem = os.path.splitext(r['name'])[0].lower()
            if r.get('ext') in ('lnk', 'url', 'appref-ms') and stem in names:
                continue                                   # the app row already covers its shortcut
            n = r['name'].lower()
            score = 9 if n.startswith(q.lower()) else 7 if r.get('hl') and r['hl'][0] > 0 else 5
            files.append(dict(r, kind='file', score=score - i * 0.5))
        top, last = sources.web_rows(q, s.get('searchEngine', 'google')) if s.get('quickWeb', True) else (None, None)
        setting_rows = sources.settings_rows(q, 2) if s.get('quickSettings', True) else []
        site_rows = history.rows(q, 2) if s.get('quickHistory', True) else []
        if top or app_rows or setting_rows or site_rows:
            files = [r for r in files if r.get('hl')]       # drop close-match guesses when real matches exist
        boosts = {}
        for path, bonus in ENGINE.picks.top_hits(q):
            boosts[path.lower()] = boosts.get(path.lower(), 0) + bonus * 2
        rows = sources.compose([
            ('Apps', app_rows),
            ('Settings', setting_rows),
            ('Files', files),
            ('Websites', site_rows),
        ], top_extra=top, last=last, boosts=boosts, max_rows=min(limit, 9))
        totals = {'Apps': apps.last_total if app_rows else 0,
                  'Settings': len(sources.settings_rows(q, 999)) if setting_rows else 0,
                  'Files': getattr(ENGINE, 'last_quick_total', 0) if files else 0,
                  'Websites': history.last_total if site_rows else 0}
        for label, total in totals.items():
            shown = sum(1 for r in rows if label in (r.get('group'), r.get('origin')))
            first = next((r for r in rows if r.get('group') == label), None)
            if first and total > shown:
                first['more'] = total - shown
        return rows

    def visual_rows(src):
        """Image mode in the bar: the original, other copies and look-alikes of a picture."""
        if not features.pro():
            raise RuntimeError('Visual search is part of Riffle Pro. Enter opens Riffle, where you can try it free')
        if features.pack_needed():
            raise RuntimeError('Visual search is not installed yet. Enter opens Riffle to add it (a free download)')
        v = visual()
        if not v:
            raise RuntimeError(_visual['error'] or 'Visual search is still starting, try again in a moment')
        if isinstance(src, str):
            with open(src, 'rb') as fh:
                data = fh.read()
            ctype = mimetypes.guess_type(src)[0] or ''
        else:
            data, ctype = src
        res = [v.public(r) for r in v.search_bytes(data, ctype)[0]]
        if not res and (v.status().get('index') or {}).get('running'):
            raise RuntimeError('Riffle is still reading your pictures. Try again in a moment')
        orig = [r for r in res if r['original']]
        vers = [r for r in res if r['match'] == 'verified' and not r['original']]
        sim = [r for r in res if r['match'] == 'similar'] or ([] if orig else [r for r in res if r['match'] == 'loose'])
        rows = []
        for label, items, cap in (('Original', orig, 1), ('Other versions', vers, 3), ('Looks similar', sim, 5)):
            for i, r in enumerate(items[:cap]):
                why = ('This exact file' if r['same_file'] else 'Same picture' if r['match'] == 'verified'
                       else f"{round(r['score'] * 100)}% alike")
                row = {'kind': 'file', 'name': r['name'], 'dir': f"{why} · {r['w']} × {r['h']} · {r['dir']}",
                       'path': '' if r.get('link') else r['path'], 'ext': os.path.splitext(r['name'])[1][1:].lower(),
                       'cat': 'video' if r['kind'] == 'video' else 'image', 'size': r['size'], 'mtime': r['mtime'],
                       'group': label, 'hl': None}
                if r.get('link'):
                    row['target'] = r['link']
                if i == 0 and len(items) > cap:
                    row['more'] = len(items) - cap
                rows.append(row)
        return rows

    def clip_image():
        """A copied picture (bytes) or a copied picture file (path), or None."""
        features.pack_activate()                    # Pillow lives in the visual pack in the light build
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        got = ImageGrab.grabclipboard()
        if isinstance(got, list):
            return next((f for f in got if os.path.isfile(f)), None)
        if got is None:
            return None
        buf = io.BytesIO()
        got.convert('RGB').save(buf, 'PNG')
        return buf.getvalue(), 'image/png'

    def voice_start(on_text, on_level, on_end):
        v = voice_engine()
        if not v:
            return ('Voice search is not installed yet. Enter opens Riffle: Settings › Quick bar › '
                    'Voice search adds it (a free 65 MB download)')
        v.start(on_text, on_level, on_end)
        return None

    def voice_stop():
        if VOICE['v']:
            VOICE['v'].stop()

    def open_target(t):
        if isinstance(t, list):
            subprocess.Popen(t, creationflags=0x08000000)
        else:
            os.startfile(t)

    def icon_png(p, ext):
        if p == '<web>':
            p, ext = default_browser_exe() or '', 'exe'
        elif p.startswith('shell:') or ext == '':
            return shellthumb.icon(p, '<dir>' if os.path.isdir(p) else '', 32)
        return shellthumb.icon(p, ext, 32)

    def content(q, limit, skip):
        return ENGINE.inside(q, limit, bar_filters(), skip)

    def preview(r, px):
        """Thumbnail PNG, or the first lines of a text file, for the bar's preview pane."""
        kind = r.get('kind')
        if kind in ('web', 'site'):
            url = str(r.get('url') or r.get('target') or '')
            exe = os.path.splitext(os.path.basename(default_browser_exe() or ''))[0].lower()
            name = {'chrome': 'Chrome', 'msedge': 'Edge', 'brave': 'Brave', 'firefox': 'Firefox',
                    'opera': 'Opera', 'vivaldi': 'Vivaldi'}.get(exe, 'your browser')
            return {'png': history.favicon(url) if url.startswith('http') else None, 'site': True,
                    'meta': f'Enter opens it in {name}'}
        if kind in ('app', 'setting'):
            return {'png': shellthumb.image(r.get('icon_path') or '', px // 2, icon_only=True),
                    'meta': 'App' if kind == 'app' else r.get('dir', 'Settings')}
        p, ext, out = r['path'], (r.get('ext') or '').lower(), {}
        if r.get('cat') == 'folder':
            try:
                with os.scandir(p) as it:
                    n = sum(1 for _ in zip(it, range(10000)))
                out['meta'] = f'{n:,}{"+" if n == 10000 else ""} item{"" if n == 1 else "s"}'
            except OSError:
                pass
        elif ext in features.TEXT_EXT and not r.get('online') and 0 < (r.get('size') or 0) < 50 << 20:
            with open(p, 'rb') as fh:
                raw = fh.read(4096)
            if b'\x00' not in raw:
                out['text'] = raw.decode('utf-8', 'replace')
        if 'text' not in out:
            out['png'] = shellthumb.image(p, px, cache_only=bool(r.get('online')))
        return out

    def open_app(q):
        features._ctx['inbox'] = q                  # the window picks this up when it gets focus
        if not focus_window():
            open_window(url)

    bar = quickbar.QuickBar(
        search=search,
        open_path=lambda p: shell_act('open', [p]),
        reveal=lambda p: shell_act('reveal', [p]),
        open_app=open_app,
        icon_png=icon_png,
        open_target=open_target,
        theme=_system_theme,
        content=content if contentsearch.available() else None,
        picked=lambda p, q: ENGINE.picks.record(p, q),
        preview=preview,
        preview_on=lambda: features.settings().get('quickPreview', True),
        look=lambda: features.settings().get('quickLook', 'paper'),
        visual=visual_rows,
        clip_image=clip_image,
        voice_start=voice_start,
        voice_stop=voice_stop,
    )
    bar.start()
    if contentsearch.available():
        contentsearch.warm()
    apps.refresh()
    history.refresh(0)
    QUICK['bar'] = bar


def default_browser_exe():
    """Path of the default web browser, for the icon on web rows."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\Shell\Associations'
                            r'\UrlAssociations\https\UserChoice') as k:
            prog = winreg.QueryValueEx(k, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r'\shell\open\command') as k:
            cmd = winreg.QueryValueEx(k, '')[0]
        m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
        return m.group(1) or m.group(2)
    except (OSError, AttributeError):
        return None

VK_NAMES = {'space': 0x20, 'enter': 0x0D, 'tab': 0x09, '`': 0xC0, ';': 0xBA, '/': 0xBF, '.': 0xBE}


def parse_hotkey(combo):
    """'ctrl+shift+space' -> (modifiers, virtual key)."""
    mods, vk = 0, None
    for part in combo.lower().replace(' ', '').split('+'):
        if part in ('ctrl', 'control'):
            mods |= 0x2
        elif part == 'shift':
            mods |= 0x4
        elif part == 'alt':
            mods |= 0x1
        elif part == 'win':
            mods |= 0x8
        elif part in VK_NAMES:
            vk = VK_NAMES[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith('f') and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            vk = 0x70 + int(part[1:]) - 1
    return mods, vk


def restart_hotkey(url):
    """Stop the current hotkey thread (if any) and register the saved combo again."""
    if HOTKEY['tid']:
        ctypes.windll.user32.PostThreadMessageW(HOTKEY['tid'], 0x0012, 0, 0)   # WM_QUIT
    threading.Thread(target=hotkey_loop, args=(url,), daemon=True, name="hotkey").start()


def hotkey_loop(url):
    """Register the summon hotkey (Ctrl+Shift+Space by default) system-wide and pump the message queue.

Doing this in-process means no AutoHotkey dependency: the tool is summonable
from anywhere the moment it is running.
"""
    if not IS_WIN:
        return
    try:
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        MOD_NOREPEAT = 0x4000
        combo = features.settings().get('hotkey') or 'ctrl+shift+space'
        mods, vk = parse_hotkey(combo)
        HOTKEY.update(combo=combo, ok=False, tid=ctypes.windll.kernel32.GetCurrentThreadId())
        if vk is None or not mods or not u32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            print(f'[riffle] hotkey {combo} is taken by something else')
            return
        HOTKEY['ok'] = True
        print(f'[riffle] hotkey ready — {combo}')

        class MSG(ctypes.Structure):
            _fields_ = [('hwnd', ctypes.c_void_p), ('message', ctypes.c_uint),
                        ('wParam', ctypes.c_void_p), ('lParam', ctypes.c_void_p),
                        ('time', ctypes.c_uint),
                        ('pt_x', ctypes.c_long), ('pt_y', ctypes.c_long)]

        msg = MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:                   # WM_HOTKEY
                if QUICK['bar'] and features.settings().get('hotkeyOpens', 'bar') == 'bar':
                    QUICK['bar'].toggle()               # the light quick bar
                elif not focus_window():
                    open_window(url)
        u32.UnregisterHotKey(None, 1)                   # WM_QUIT: settings changed the combo
    except Exception:
        return


def already_running():
    """A second launch should raise the existing window, not start a rival server."""
    import socket
    s = socket.socket()
    try:
        s.connect(('127.0.0.1', PORT))
        s.close()
        return True
    except OSError:
        return False


def main():
    if already_running():
        url = f'http://127.0.0.1:{PORT}'
        if not focus_window():
            open_window(url)
        print('[riffle] already running — raised the existing window')
        return

    if os.path.exists(CACHE):
        try:
            ENGINE.ix = Index.load(CACHE)
            ENGINE.state = 'ready'
            ENGINE.message = f'{ENGINE.ix.n:,} items from cache'
            print(f'[riffle] loaded {ENGINE.ix.n:,} items from cache')
        except Exception as e:
            print(f'[riffle] cache unusable ({e}), starting fresh')

    features.init(ENGINE, CAT_DIR, DATA_DIR, VERSION, GITHUB_REPO, RES)
    features._ctx['reset_visual'] = lambda: _visual.update(error=None)   # retry after the pack installs
    features._ctx['restart_hotkey'] = lambda: restart_hotkey(f'http://127.0.0.1:{PORT}')
    features._ctx['hotkey_state'] = HOTKEY
    features._ctx['reindex'] = lambda roots: ENGINE.reindex(roots)
    features._ctx['cats'] = CATS
    # warm the filter caches so the first quick search is instant
    threading.Thread(target=lambda: (time.sleep(3), ENGINE.quick('', 1, {'hideSystem': True, 'hideHidden': True})),
                     daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    url = f'http://127.0.0.1:{PORT}'
    print(f'[riffle] {url}')

    if ENGINE.ix.n == 0:
        roots = default_roots()
        print(f"[riffle] first run — indexing {', '.join(roots)}")
        ENGINE.reindex(roots)
    else:
        try:
            ENGINE.start_watch(ENGINE.ix.roots or default_roots())
        except Exception:
            ENGINE.live = False
        ENGINE.saved_gen, ENGINE.saved_at = ENGINE.ix.gen, os.path.getmtime(CACHE)
        if time.time() - ENGINE.saved_at > 3600:
            # Files may have changed while Riffle was off. Let boot settle first.
            ENGINE.refresh(ENGINE.ix.roots or default_roots(), delay=0 if '--now' in sys.argv else 90)

    if '--no-window' not in sys.argv:
        open_window(url)
    if os.environ.get('RIFFLE_PROFILE') and IS_WIN:
        threading.Thread(target=_profile_threads, daemon=True, name='profile').start()
    if IS_WIN and '--no-quickbar' not in sys.argv:
        start_quickbar(url)                     # before the hotkey, so the first press is instant
    threading.Thread(target=hotkey_loop, args=(url,), daemon=True, name="hotkey").start()
    threading.Thread(target=_visual_loop, daemon=True, name="visual-loop").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[riffle] bye')


if __name__ == '__main__':
    main()
