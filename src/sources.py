"""Quick bar results that are not files: the web, Windows settings, apps, browser history.

Each finder returns rows the quick bar draws like file rows, plus:
    target   what Enter opens (URL, ms-settings: page, shell:AppsFolder\\<id>, or a command list)
    group    'Apps' | 'Settings' | 'Websites' | 'Search the web'
    score    comparable across sources, so one row can win "Top hit"

Everything is read on this PC. Nothing is sent anywhere until the user opens a web row.
"""
import glob
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from urllib.parse import quote_plus, urlsplit

LOCAL = os.environ.get('LOCALAPPDATA', '')
ROAMING = os.environ.get('APPDATA', '')
SETTINGS_APP = r'shell:AppsFolder\windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel'

# ── web ──────────────────────────────────────────────────────────────────
ENGINES = {'google': ('Google', 'https://www.google.com/search?q={}'),
           'bing': ('Bing', 'https://www.bing.com/search?q={}'),
           'duckduckgo': ('DuckDuckGo', 'https://duckduckgo.com/?q={}')}
SHORTCUTS = {'yt': ('YouTube', 'https://www.youtube.com/results?search_query={}'),
             'maps': ('Google Maps', 'https://www.google.com/maps/search/{}'),
             'wiki': ('Wikipedia', 'https://en.wikipedia.org/w/index.php?search={}')}
# endings that are web addresses, not file extensions people search for (no .md, .ai, .py, .rs ...)
TLDS = set('com net org io in co dev app gov edu info biz xyz me tv gg ly so site online store tech blog '
           'uk us de fr jp au ca nl es it ch se no fi br mx sg ae news live shop page cloud'.split())
_HOST = re.compile(r'^(?:[a-z0-9-]+\.)+([a-z]{2,})(?::\d+)?(?:[/?#]\S*)?$', re.I)


def as_url(q):
    """'github.com/x' -> 'https://github.com/x'; None when q isn't a web address."""
    s = q.strip()
    if not s or ' ' in s:
        return None
    if re.match(r'^https?://\S+$', s, re.I):
        return s
    m = _HOST.match(s[4:] if s.lower().startswith('www.') else s)
    if m and m.group(1).lower() in TLDS:
        return 'https://' + s
    return None


def web_rows(q, engine='google'):
    """(top-hit row or None, last row) for the web: an address, a shortcut, or a plain search."""
    q = q.strip()
    if not q:
        return None, None
    top = None
    url = as_url(q)
    if url:
        host = urlsplit(url).netloc
        top = {'name': 'Open ' + q, 'dir': 'in your browser', 'target': url, 'group': 'Websites',
               'kind': 'web', 'score': 30, 'hl': [5, 5 + len(q)], 'host': host}
    word, _, rest = q.partition(' ')
    sc = SHORTCUTS.get(word.lower()) or (ENGINES['google'] if word.lower() == 'g' else None)
    if sc and rest.strip():
        top = {'name': f'Search {sc[0]} for “{rest.strip()}”', 'dir': urlsplit(sc[1]).netloc,
               'target': sc[1].format(quote_plus(rest.strip())), 'group': 'Search the web',
               'kind': 'web', 'score': 30}
    name, tpl = ENGINES.get(engine, ENGINES['google'])
    last = {'name': f'Search {name} for “{q}”', 'dir': 'opens your browser', 'target': tpl.format(quote_plus(q)),
            'group': 'Search the web', 'kind': 'web', 'score': -1,
            'hl': [len(f'Search {name} for “'), len(f'Search {name} for “') + len(q)]}
    return top, last


# ── matching shared by the small lists ──────────────────────────────────
def _match(q, name, words=''):
    """Score a name (and extra keywords) against q: 10 starts, 8 word start, 5 contains, 4 keyword."""
    n, q = name.lower(), q.lower()
    squash = lambda x: re.sub(r'[\s\-‑_]', '', x)
    if n.startswith(q) or squash(n).startswith(squash(q)):       # "wifi" finds "Wi-Fi"
        return 10
    if re.search(r'(^|[\s\-_&(])' + re.escape(q), n):
        return 8
    if q in n:
        return 5
    if words and re.search(r'(^|\s)' + re.escape(q), words):
        return 7
    return 0


def _hl(name, q):
    i = name.lower().find(q.lower())
    return [i, i + len(q)] if i >= 0 else None


# ── Windows settings ─────────────────────────────────────────────────────
# (name, target, extra words people type). ms-settings: pages exist on Windows 10 and 11.
SETTINGS = [
    ('Display', 'ms-settings:display', 'screen resolution brightness scale monitor'),
    ('Night light', 'ms-settings:nightlight', 'blue light warm eyes'),
    ('Sound', 'ms-settings:sound', 'audio speaker microphone volume output input'),
    ('Volume mixer', 'ms-settings:apps-volume', 'app volume sound'),
    ('Notifications', 'ms-settings:notifications', 'alerts banners'),
    ('Focus', 'ms-settings:quiethours', 'do not disturb focus assist'),
    ('Power & battery', 'ms-settings:powersleep', 'sleep battery power mode screen off'),
    ('Battery saver', 'ms-settings:batterysaver', 'energy saver'),
    ('Storage', 'ms-settings:storagesense', 'disk space storage sense cleanup'),
    ('Multitasking', 'ms-settings:multitasking', 'snap windows desktops alt tab'),
    ('Clipboard', 'ms-settings:clipboard', 'clipboard history copy paste'),
    ('Remote Desktop', 'ms-settings:remotedesktop', 'rdp remote'),
    ('About this PC', 'ms-settings:about', 'pc name rename specs windows version system info'),
    ('Bluetooth & devices', 'ms-settings:bluetooth', 'bluetooth pair headphones wireless devices'),
    ('Printers & scanners', 'ms-settings:printers', 'printer scanner print'),
    ('Mouse', 'ms-settings:mousetouchpad', 'mouse pointer speed scroll'),
    ('Touchpad', 'ms-settings:devices-touchpad', 'trackpad gestures'),
    ('Typing', 'ms-settings:typing', 'keyboard autocorrect spelling'),
    ('AutoPlay', 'ms-settings:autoplay', 'usb drive autoplay'),
    ('Wi-Fi', 'ms-settings:network-wifi', 'wifi wireless network internet'),
    ('Ethernet', 'ms-settings:network-ethernet', 'lan cable network'),
    ('VPN', 'ms-settings:network-vpn', 'vpn'),
    ('Mobile hotspot', 'ms-settings:network-mobilehotspot', 'hotspot tethering share internet'),
    ('Airplane mode', 'ms-settings:network-airplanemode', 'flight mode'),
    ('Proxy', 'ms-settings:network-proxy', 'proxy'),
    ('Network & internet', 'ms-settings:network-status', 'network internet connection'),
    ('Background', 'ms-settings:personalization-background', 'wallpaper desktop picture'),
    ('Colors', 'ms-settings:colors', 'dark mode light mode accent theme'),
    ('Themes', 'ms-settings:themes', 'theme desktop icons'),
    ('Lock screen', 'ms-settings:lockscreen', 'lock screen picture'),
    ('Start menu', 'ms-settings:personalization-start', 'start'),
    ('Taskbar', 'ms-settings:taskbar', 'taskbar tray system icons'),
    ('Fonts', 'ms-settings:fonts', 'font typeface'),
    ('Installed apps', 'ms-settings:appsfeatures', 'uninstall remove programs apps'),
    ('Default apps', 'ms-settings:defaultapps', 'default browser open with'),
    ('Startup apps', 'ms-settings:startupapps', 'startup boot launch'),
    ('Optional features', 'ms-settings:optionalfeatures', 'features add'),
    ('Your info', 'ms-settings:yourinfo', 'account profile picture microsoft account'),
    ('Sign-in options', 'ms-settings:signinoptions', 'password pin windows hello fingerprint face'),
    ('Email & accounts', 'ms-settings:emailandaccounts', 'email accounts'),
    ('Date & time', 'ms-settings:dateandtime', 'clock time zone date'),
    ('Language & region', 'ms-settings:regionlanguage', 'language region keyboard layout'),
    ('Speech', 'ms-settings:speech', 'voice microphone dictation'),
    ('Game Mode', 'ms-settings:gaming-gamemode', 'gaming games'),
    ('Captures', 'ms-settings:gaming-gamedvr', 'screen recording game bar'),
    ('Text size', 'ms-settings:easeofaccess-display', 'bigger text font size accessibility'),
    ('Magnifier', 'ms-settings:easeofaccess-magnifier', 'zoom accessibility'),
    ('Narrator', 'ms-settings:easeofaccess-narrator', 'screen reader accessibility'),
    ('Color filters', 'ms-settings:easeofaccess-colorfilter', 'colour blind grayscale accessibility'),
    ('Mouse pointer and touch', 'ms-settings:easeofaccess-mousepointer', 'cursor size accessibility'),
    ('Contrast themes', 'ms-settings:easeofaccess-highcontrast', 'high contrast accessibility'),
    ('Privacy & security', 'ms-settings:privacy', 'privacy permissions'),
    ('Location', 'ms-settings:privacy-location', 'gps location privacy'),
    ('Camera privacy', 'ms-settings:privacy-webcam', 'webcam camera access'),
    ('Microphone privacy', 'ms-settings:privacy-microphone', 'mic access'),
    ('Windows Security', 'ms-settings:windowsdefender', 'antivirus defender virus firewall'),
    ('Find my device', 'ms-settings:findmydevice', 'lost'),
    ('For developers', 'ms-settings:developers', 'developer mode'),
    ('Windows Update', 'ms-settings:windowsupdate', 'update upgrade patches'),
    ('Update history', 'ms-settings:windowsupdate-history', 'updates installed'),
    ('Recovery', 'ms-settings:recovery', 'reset this pc restore advanced startup'),
    ('Activation', 'ms-settings:activation', 'product key license'),
    ('Troubleshoot', 'ms-settings:troubleshoot', 'fix problems troubleshooter'),
    ('Graphics', 'ms-settings:display-advancedgraphics', 'gpu graphics card performance'),
    ('Phone Link', 'ms-settings:mobile-devices', 'phone mobile android iphone'),
    ('Control Panel', 'control', 'classic control panel'),
    ('Device Manager', 'devmgmt.msc', 'drivers hardware devices'),
    ('Disk Management', 'diskmgmt.msc', 'partition format drive'),
    ('Task Manager', 'taskmgr', 'processes kill end task performance'),
    ('Services', 'services.msc', 'windows services'),
    ('Network connections', 'ncpa.cpl', 'adapter network ip'),
    ('System properties', 'sysdm.cpl', 'computer name advanced system'),
    ('Environment variables', ['rundll32.exe', 'sysdm.cpl,EditEnvironmentVariables'], 'path env variables'),
    ('Registry Editor', 'regedit', 'regedit registry'),
    ('Event Viewer', 'eventvwr.msc', 'logs errors events'),
]


def _setting_icon(target):
    if isinstance(target, list):
        target = target[1].split(',')[0]
    if target.startswith('ms-settings:'):
        return SETTINGS_APP
    p = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', target)
    for cand in (p, p + '.exe'):
        if os.path.exists(cand):
            return cand
    return SETTINGS_APP


def settings_rows(q, limit=2):
    q = q.strip()
    if len(q) < 2:
        return []
    out = []
    for name, target, words in SETTINGS:
        s = _match(q, name, words)
        if s:
            out.append({'name': name, 'dir': 'Settings' if isinstance(target, str) and target.startswith('ms-settings:')
                        else 'Windows tool', 'target': target, 'group': 'Settings', 'kind': 'setting',
                        'score': s + (1 if s == 8 else 0), 'hl': _hl(name, q), 'icon_path': _setting_icon(target),
                        'about': words})
    out.sort(key=lambda r: -r['score'])
    return out[:limit]


# ── apps (Start menu + Microsoft Store), via Get-StartApps ───────────────
class Apps:
    def __init__(self, cache_file):
        self.file, self.items, self.loaded_at = cache_file, [], 0.0
        try:
            with open(cache_file, encoding='utf-8') as f:
                d = json.load(f)
            self.items, self.loaded_at = d['items'], d['at']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._busy = False

    def refresh(self, max_age=86400):
        """Re-read the app list in the background when it is older than max_age seconds."""
        if self._busy or time.time() - self.loaded_at < max_age:
            return
        self._busy = True
        threading.Thread(target=self._load, daemon=True, name='apps').start()

    def _load(self):
        try:
            out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                                  '[Console]::OutputEncoding=[Text.Encoding]::UTF8; '
                                  'Get-StartApps | ConvertTo-Json -Compress'],
                                 capture_output=True, timeout=60, creationflags=0x08000000)
            items = json.loads(out.stdout.decode('utf-8', 'replace') or '[]')
            if isinstance(items, dict):
                items = [items]
            skip = re.compile(r'uninstall|readme|help|documentation|release notes|website', re.I)
            self.items = [{'name': a['Name'], 'id': a['AppID']} for a in items
                          if a.get('Name') and a.get('AppID') and not skip.search(a['Name'])]
            self.loaded_at = time.time()
            tmp = self.file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'at': self.loaded_at, 'items': self.items}, f)
            os.replace(tmp, self.file)
        except Exception:
            pass
        finally:
            self._busy = False

    def rows(self, q, limit=2):
        q = q.strip()
        if not q:
            return []
        out = []
        for a in self.items:
            s = _match(q, a['name'])
            if s:
                t = 'shell:AppsFolder\\' + a['id']
                out.append({'name': a['name'], 'dir': 'App', 'target': t, 'icon_path': t, 'group': 'Apps',
                            'kind': 'app', 'score': s + (2 if s == 8 else 1), 'hl': _hl(a['name'], q)})
        out.sort(key=lambda r: (-r['score'], len(r['name'])))
        return out[:limit]


# ── browser history + bookmarks ─────────────────────────────────────────
CHROMIUM = [('Chrome', os.path.join(LOCAL, r'Google\Chrome\User Data')),
            ('Edge', os.path.join(LOCAL, r'Microsoft\Edge\User Data')),
            ('Brave', os.path.join(LOCAL, r'BraveSoftware\Brave-Browser\User Data')),
            ('Vivaldi', os.path.join(LOCAL, r'Vivaldi\User Data')),
            ('Opera', os.path.join(ROAMING, r'Opera Software\Opera Stable'))]
FIREFOX = os.path.join(ROAMING, r'Mozilla\Firefox\Profiles')
EPOCH_1601 = 11644473600


def _profiles():
    """[(browser, kind, folder)] for every profile that has a history file."""
    out = []
    for name, root in CHROMIUM:
        if os.path.isfile(os.path.join(root, 'History')):            # Opera keeps one profile in the root
            out.append((name, 'chromium', root))
        for d in glob.glob(os.path.join(root, 'Default')) + glob.glob(os.path.join(root, 'Profile *')):
            if os.path.isfile(os.path.join(d, 'History')):
                out.append((name, 'chromium', d))
    for d in glob.glob(os.path.join(FIREFOX, '*')):
        if os.path.isfile(os.path.join(d, 'places.sqlite')):
            out.append(('Firefox', 'firefox', d))
    return out


def _copy_db(path, tmpdir):
    """Browsers keep their database open: read a copy (plus its write-ahead log, if any)."""
    dst = os.path.join(tmpdir, os.path.basename(path))
    shutil.copyfile(path, dst)
    for ext in ('-wal', '-journal'):
        if os.path.exists(path + ext):
            try:
                shutil.copyfile(path + ext, dst + ext)
            except OSError:
                pass
    return dst


def _read_profile(kind, folder, tmpdir, limit):
    """{url: [title, visits, last_unix, bookmarked]} for one profile."""
    pages = {}
    if kind == 'chromium':
        db = _copy_db(os.path.join(folder, 'History'), tmpdir)
        with closing(sqlite3.connect(db)) as c:   # plain `with` would leave the file open
            for url, title, n, t in c.execute(
                    'SELECT url, title, visit_count, last_visit_time FROM urls WHERE hidden = 0 '
                    'ORDER BY last_visit_time DESC LIMIT ?', (limit,)):
                pages[url] = [title or '', n or 0, (t or 0) / 1e6 - EPOCH_1601, False]
        os.remove(db)
        try:
            with open(os.path.join(folder, 'Bookmarks'), encoding='utf-8') as f:
                roots = json.load(f).get('roots', {})
            stack = list(roots.values())
            while stack:
                n = stack.pop()
                if not isinstance(n, dict):
                    continue
                if n.get('type') == 'url' and n.get('url'):
                    p = pages.setdefault(n['url'], [n.get('name', ''), 0, 0, True])
                    p[3] = True
                    p[0] = p[0] or n.get('name', '')
                stack.extend(n.get('children') or [])
        except (OSError, ValueError):
            pass
    else:
        db = _copy_db(os.path.join(folder, 'places.sqlite'), tmpdir)
        with closing(sqlite3.connect(db)) as c:   # plain `with` would leave the file open
            for url, title, n, t, bm in c.execute(
                    'SELECT p.url, p.title, p.visit_count, p.last_visit_date, '
                    '(SELECT 1 FROM moz_bookmarks b WHERE b.fk = p.id LIMIT 1) FROM moz_places p '
                    'WHERE p.hidden = 0 ORDER BY p.last_visit_date DESC LIMIT ?', (limit,)):
                pages[url] = [title or '', n or 0, (t or 0) / 1e6, bool(bm)]
        for p in glob.glob(os.path.join(tmpdir, '*')):
            os.remove(p)
    return pages


class History:
    """Pages from every browser profile, kept in memory and re-read when a file changes."""

    def __init__(self, limit=30000):
        self.limit, self.entries, self.stamp, self.checked = limit, [], None, 0.0
        self.lock = threading.Lock()
        self._busy = False

    def browsers(self):
        return sorted({b for b, _, _ in _profiles()})

    def refresh(self, every=60):
        if self._busy or time.time() - self.checked < every:
            return
        self.checked = time.time()
        self._busy = True
        threading.Thread(target=self._load, daemon=True, name='history').start()

    def _load(self):
        try:
            profs = _profiles()
            stamp = tuple(os.path.getmtime(os.path.join(d, 'History' if k == 'chromium' else 'places.sqlite'))
                          for _, k, d in profs)
            if stamp == self.stamp:
                return
            pages = {}
            with tempfile.TemporaryDirectory() as tmp:
                for _, kind, folder in profs:
                    try:
                        got = _read_profile(kind, folder, tmp, self.limit)
                    except (OSError, sqlite3.Error):
                        continue
                    for url, (title, n, t, bm) in got.items():
                        p = pages.get(url)
                        if p:
                            p[0] = p[0] or title
                            p[1] += n
                            p[2] = max(p[2], t)
                            p[3] = p[3] or bm
                        else:
                            pages[url] = [title, n, t, bm]
            entries = []
            for url, (title, n, t, bm) in pages.items():
                if not url.startswith(('http://', 'https://')):
                    continue
                host = urlsplit(url).netloc.lower()
                host = host[4:] if host.startswith('www.') else host
                entries.append((f'{title}\x00{host}\x00{url}'.lower(), title, url, host, n, t, bm))
            with self.lock:
                self.entries, self.stamp = entries, stamp
        except Exception:
            pass
        finally:
            self._busy = False

    def rows(self, q, limit=2):
        q = q.strip().lower()
        if len(q) < 2:
            return []
        now, scored = time.time(), []
        with self.lock:
            entries = self.entries
        for hay, title, url, host, n, t, bm in entries:
            if q not in hay:
                continue
            if host.startswith(q):
                s = 8.0
            elif re.search(r'(^|[\s\-|:·])' + re.escape(q), title.lower()):
                s = 6.0
            else:
                s = 3.0
            s += min(2.0, math.log2(1 + n) / 3) + (1.5 if bm else 0) + math.exp(-(now - t) / 86400 / 30)
            scored.append((s, title, url, host, n, t, bm))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for s, title, url, host, n, t, bm in scored:
            name = re.sub(r'^\(\d+\)\s*', '', title) or host     # "(184) YouTube" -> "YouTube"
            key, key2 = url.rstrip('/').split('#')[0], (host, name.lower())
            if key in seen or key2 in seen:
                continue
            seen.update((key, key2))
            visits = 'bookmark' if bm and not n else f'visited {n} time{"" if n == 1 else "s"}' if n else 'bookmark'
            out.append({'name': name, 'dir': f'{host} · {visits}', 'target': url, 'group': 'Websites',
                        'kind': 'site', 'score': min(s, 9.5), 'hl': _hl(name, q), 'mtime': int(t) if t > 0 else None,
                        'url': url})
            if len(out) >= limit:
                break
        return out


def compose(groups, top_extra=None, last=None, boosts=None, max_rows=9):
    """Quick bar rows: one Top hit (best score of any kind, learned picks added), then each group
    in order, then the web row. Groups shrink from the biggest until it fits in max_rows."""
    boosts = boosts or {}
    groups = [(label, [dict(r) for r in rows]) for label, rows in groups]
    for _, rows in groups:
        for r in rows:
            r['score'] = r.get('score', 0) + boosts.get(str(r.get('target') or r.get('path') or '').lower(), 0)
    pool = [r for _, rows in groups for r in rows] + ([top_extra] if top_extra else [])
    top = max(pool, key=lambda r: r.get('score', 0)) if pool else None
    groups = [(label, [r for r in rows if r is not top]) for label, rows in groups]
    room = max_rows - (1 if top else 0) - (1 if last else 0)
    while sum(len(rows) for _, rows in groups) > room:
        max(groups, key=lambda g: len(g[1]))[1].pop()
    out = [dict(top, group='Top hit')] if top else []
    for label, rows in groups:
        out += [dict(r, group=label) for r in rows]
    if last:
        out.append(last)
    return out


HELP = [
    # (group, glyph, what to type, short line, title, longer text, example put in the box)
    ('Try typing', 'Aa', 'invoice', 'Files, apps, settings and websites named like it',
     'Find anything by name', 'Type part of a name. Riffle looks through every file on this PC, your apps, '
     'Windows settings and the sites in your browser history, and puts the best one first.', 'invoice'),
    ('Try typing', '⚙', 'bluetooth', 'A Windows settings page',
     'Open Windows settings', 'Words like wifi, dark mode, display, uninstall or printer open the right '
     'settings page. Tools like Task Manager and Device Manager work too.', 'bluetooth'),
    ('Try typing', '=', '18% of 4,500', 'A sum. Enter copies the answer',
     'Do a quick sum', 'Plus, minus, times (* or x), divide, powers (^) and brackets. Also "18% of 4,500". '
     'Enter copies the answer.', '18% of 4,500'),
    ('Try typing', '↗', 'github.com', 'Opens the website in your browser',
     'Open a website', 'Type an address and press Enter. Pages you visited there show up too.', 'github.com'),
    ('Try typing', 'yt', 'yt lofi', 'Search YouTube. Also g, maps, wiki',
     'Search the web', 'Start with yt, g (Google), maps or wiki, then your words. Anything else: the last row '
     'searches the web with your chosen engine.', 'yt lofi'),
    ('Try typing', '¶', 'landed cost', 'Also finds files with these words inside',
     'Find words inside documents', 'Under the names, Riffle lists documents that contain your words, '
     'using the Windows Search index (Documents, Desktop, Downloads, Pictures).', 'landed cost'),
    ('Keys', '↵', 'Enter', 'Open the selected result', 'Enter', 'Opens the file, app, setting or page.', None),
    ('Keys', '⌃', 'Ctrl + Enter', 'Show the file in its folder', 'Ctrl + Enter', 'Opens Explorer with the file selected.', None),
    ('Keys', '⌃', 'Ctrl + C', 'Copy the path, address or answer', 'Ctrl + C', 'Copies what the selected row points to.', None),
    ('Keys', '⇥', 'Tab', 'Continue in the full Riffle window', 'Tab', 'Filters, duplicates, clean-up and the space map live there.', None),
    ('Keys', '⎋', 'Esc', 'Clear the box, then close', 'Esc', 'Riffle learns what you open, so your usual picks rise to the top.', None),
]


def help_rows():
    return [{'kind': 'help', 'group': g, 'glyph': gl, 'name': name, 'dir': line, 'title': title,
             'about': about, 'fill': fill, 'path': ''} for g, gl, name, line, title, about, fill in HELP]


if __name__ == '__main__':
    assert [r['group'] for r in help_rows()].count('Keys') == 5 and help_rows()[0]['fill'] == 'invoice'
    rows = compose([('Apps', [{'name': 'Calculator', 'score': 11, 'target': 'a'}]),
                    ('Files', [{'name': f'f{i}', 'score': 9 - i, 'path': f'p{i}'} for i in range(5)]),
                    ('Websites', [{'name': 's', 'score': 9.5, 'target': 'u'}])],
                   last={'name': 'web', 'group': 'Search the web'}, boosts={'p2': 20}, max_rows=5)
    assert [r['name'] for r in rows] == ['f2', 'Calculator', 'f0', 's', 'web'], rows   # learned pick wins
    assert rows[0]['group'] == 'Top hit' and rows[-1]['group'] == 'Search the web'
    assert as_url('github.com') == 'https://github.com'
    assert as_url('https://x.ai/a') == 'https://x.ai/a'
    assert as_url('www.bbc.co.uk/news') == 'https://www.bbc.co.uk/news'
    for no in ('report.pdf', 'notes.md', 'logo.ai', 'goa beach', 'riffle.py', '12*7', 'main.rs'):
        assert as_url(no) is None, no
    top, last = web_rows('yt lofi beats')
    assert top['target'] == 'https://www.youtube.com/results?search_query=lofi+beats'
    assert last['target'].startswith('https://www.google.com/search?q=yt+lofi')
    top, _ = web_rows('github.com')
    assert top['name'] == 'Open github.com' and top['score'] == 30
    assert web_rows('goa')[0] is None
    assert settings_rows('blue')[0]['name'] == 'Bluetooth & devices'
    assert settings_rows('wifi')[0]['target'] == 'ms-settings:network-wifi'
    assert settings_rows('wi-fi')[0]['score'] == 10 and settings_rows('wifi')[0]['score'] == 10
    assert settings_rows('dark mode')[0]['name'] == 'Colors'
    assert settings_rows('uninstall')[0]['name'] == 'Installed apps'
    print('sources ok')
