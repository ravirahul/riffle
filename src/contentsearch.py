"""Text inside files, read from the Windows Search index.

Riffle keeps no content index of its own: Windows already reads inside
documents in the user folders, so we just ask it (OLE DB over ADO, via
comtypes). One thread owns the COM connection; callers wait with a timeout.

    contentsearch.search('quarterly report', limit=20)  -> [path, ...]

The first query after boot can take ~10 s while Windows Search wakes up,
so warm() is called once at start and callers never block the UI on it.
"""
import queue
import threading

DSN = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
_jobs = queue.Queue()
_state = {'thread': None, 'ok': None}


def available():
    if _state['ok'] is None:
        try:
            import comtypes.client  # noqa: F401
            _state['ok'] = True
        except Exception:
            _state['ok'] = False
    return _state['ok']


def sql(q, limit):
    """The words, in order, as a phrase in the file text; the last word also matches as a prefix."""
    words = [w.replace('"', '').replace("'", "''").strip('*') for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return None
    cond = '"' + ' '.join(words) + '*"'
    return (f"SELECT TOP {int(limit)} System.ItemPathDisplay FROM SystemIndex "
            f"WHERE CONTAINS(System.Search.Contents, '{cond}') "
            f"AND System.FileAttributes <> ALL BITWISE 16 "        # no folders
            f"ORDER BY System.Search.Rank DESC")


def _worker():
    import comtypes
    import comtypes.client as cc
    comtypes.CoInitialize()
    conn = None
    while True:
        job = _jobs.get()
        while not _jobs.empty():              # only the newest query matters
            job[3].set()
            job = _jobs.get_nowait()
        q, limit, box, done = job
        try:
            s = sql(q, limit)
            if s:
                if conn is None:
                    conn = cc.CreateObject('ADODB.Connection', dynamic=True)
                    conn.Open(DSN)
                rs = conn.Execute(s)
                while not rs.EOF:
                    box.append(rs.Fields.Item(0).Value)
                    rs.MoveNext()
                rs.Close()
        except Exception:
            conn = None                       # reconnect next time (service restarted, etc.)
        done.set()


def search(q, limit=20, timeout=4.0):
    if not available() or not (q or '').strip():
        return []
    if _state['thread'] is None:
        _state['thread'] = threading.Thread(target=_worker, daemon=True, name='contentsearch')
        _state['thread'].start()
    box, done = [], threading.Event()
    _jobs.put((q, limit, box, done))
    done.wait(timeout)
    return list(box)


def warm():
    threading.Thread(target=search, args=('riffle', 1, 30), daemon=True).start()


if __name__ == '__main__':
    assert sql('  ', 5) is None
    assert "'\"it''s q*\"'" in sql("it's q*", 5)        # quote doubled, phrase kept, prefix star
    import sys
    if len(sys.argv) > 1:
        print(search(' '.join(sys.argv[1:]), 10, timeout=30))
    print('contentsearch ok')
