"""What you open, remembered: Riffle's version of Spotlight's "Top Hit".

    p = Picks(path_to_json)
    p.record(path, query)        # after the user opens or reveals a result
    p.boosts(query)              # {lowercased path: score bonus}

Two signals:
  frecency  every opened path: more opens and more recent opens score higher
  top hit   the exact letters typed when it was opened ("ch" -> Chrome): typing
            those letters, or more of them, puts that file first
"""
import json
import math
import os
import threading
import time

MAX_PATHS, MAX_QUERIES = 2000, 600


class Picks:
    def __init__(self, file):
        self.file = file
        self.lock = threading.Lock()
        self.version = 0
        self.paths, self.queries = {}, {}       # lower -> [path, count, last]; query -> lower
        try:
            with open(file, encoding='utf-8') as f:
                d = json.load(f)
            self.paths = {k: list(v) for k, v in (d.get('paths') or {}).items()}
            self.queries = dict(d.get('queries') or {})
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def record(self, path, query=''):
        if not path:
            return
        k, q, now = path.lower(), (query or '').strip().lower(), time.time()
        with self.lock:
            e = self.paths.get(k)
            self.paths[k] = [path, (e[1] if e else 0) + 1, now]
            if q:
                self.queries.pop(q, None)           # re-insert so it counts as newest
                self.queries[q] = k
            if len(self.paths) > MAX_PATHS:
                for old in sorted(self.paths, key=lambda x: self.paths[x][2])[:len(self.paths) - MAX_PATHS]:
                    del self.paths[old]
            while len(self.queries) > MAX_QUERIES:
                del self.queries[next(iter(self.queries))]
            self.version += 1
            data = json.dumps({'paths': self.paths, 'queries': self.queries})
        tmp = self.file + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, self.file)
        except OSError:
            pass

    def frecency(self):
        """[(path, bonus)] for every remembered path. Up to +4, fading over ~2 months."""
        now = time.time()
        with self.lock:
            return [(v[0], min(4.0, 1.5 * math.log2(1 + v[1])) * math.exp(-(now - v[2]) / 86400 / 60) + 0.5)
                    for v in self.paths.values()]

    def top_hits(self, query):
        """[(path, bonus)] for files opened after typing this query or a shorter start of it."""
        q = (query or '').strip().lower()
        out, seen = [], set()
        with self.lock:
            for n in range(len(q), 0, -1):
                k = self.queries.get(q[:n])
                if k and k not in seen and k in self.paths and (n == len(q) or n >= 2):
                    seen.add(k)
                    out.append((self.paths[k][0], 9.0 if n == len(q) else 6.0))
        return out


if __name__ == '__main__':
    import tempfile
    f = os.path.join(tempfile.mkdtemp(), 'picks.json')
    p = Picks(f)
    p.record(r'C:\Apps\Chrome.lnk', 'ch')
    p.record(r'C:\Apps\Chrome.lnk', 'ch')
    p.record(r'C:\Docs\chart.xlsx', 'cha')
    assert Picks(f).paths[r'c:\apps\chrome.lnk'][1] == 2          # survives a reload
    assert p.top_hits('ch') == [(r'C:\Apps\Chrome.lnk', 9.0)]
    assert p.top_hits('chr') == [(r'C:\Apps\Chrome.lnk', 6.0)]     # typed more than last time
    assert p.top_hits('cha')[0] == (r'C:\Docs\chart.xlsx', 9.0)
    assert p.top_hits('c') == []                                   # one letter is too vague
    fr = dict(p.frecency())
    assert fr[r'C:\Apps\Chrome.lnk'] > fr[r'C:\Docs\chart.xlsx'] > 0
    print('picks ok')
