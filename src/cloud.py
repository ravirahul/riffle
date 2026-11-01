"""Cloud storage on this PC.

Sync apps (OneDrive, Dropbox, iCloud, Box, Google Drive for desktop...) keep their files in normal folders
or a drive letter, so the main walk already indexes them. This module only answers: which cloud is a path
in, and is the file online-only (a placeholder that Windows downloads when something reads it)?

Folders come from where Windows records them (the Cloud Files sync-root registry, used by every modern
sync app), plus a few fallbacks. Nothing here reads file contents or talks to the network.
"""
import json
import os
import threading
import time

IS_WIN = os.name == 'nt'
# RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE: reading the file would download it.
ONLINE_ATTRS = 0x00400000 | 0x00040000 | 0x00001000

NAMES = {'onedrive': 'OneDrive', 'dropbox': 'Dropbox', 'icloud': 'iCloud Drive', 'iclouddrive': 'iCloud Drive',
         'box': 'Box', 'googledrive': 'Google Drive', 'drivefs': 'Google Drive', 'pcloud': 'pCloud',
         'mega': 'MEGA', 'nextcloud': 'Nextcloud', 'sync': 'Sync.com', 'proton': 'Proton Drive'}

_cache = {'at': 0.0, 'roots': []}
_lock = threading.Lock()


def _pretty(provider_id):
    key = provider_id.lower().replace(' ', '').replace('_', '')
    for k, v in NAMES.items():
        if key.startswith(k):
            return v
    return provider_id


def _registry_roots():
    """HKLM\\...\\SyncRootManager\\<Provider!SID!Account>\\UserSyncRoots: {SID: folder}."""
    import winreg
    out = []
    base = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager'
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return out
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub + r'\UserSyncRoots') as k:
                    j = 0
                    while True:
                        try:
                            _sid, path, _t = winreg.EnumValue(k, j)
                        except OSError:
                            break
                        j += 1
                        if isinstance(path, str) and path:
                            out.append((path, _pretty(sub.split('!')[0])))
            except OSError:
                continue
    return out


def _drive_label_roots():
    """Google Drive for desktop mounts its own drive letter labelled "Google Drive"."""
    import ctypes
    out = []
    buf = ctypes.create_unicode_buffer(261)
    for c in 'DEFGHIJKLMNOPQRSTUVWXYZ':
        p = f'{c}:\\'
        if ctypes.windll.kernel32.GetDriveTypeW(p) < 2:       # unknown / no root
            continue
        if ctypes.windll.kernel32.GetVolumeInformationW(p, buf, 261, None, None, None, None, 0):
            label = buf.value.lower()
            if 'google drive' in label:
                out.append((p, 'Google Drive'))
    return out


def _fallback_roots():
    out = []
    for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(var):
            out.append((os.environ[var], 'OneDrive'))
    try:
        with open(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Dropbox', 'info.json'), encoding='utf-8') as f:
            for acct in json.load(f).values():
                if isinstance(acct, dict) and acct.get('path'):
                    out.append((acct['path'], 'Dropbox'))
    except (OSError, ValueError):
        pass
    home = os.path.expanduser('~')
    for name, prov in (('iCloudDrive', 'iCloud Drive'), ('Box', 'Box'), ('My Drive', 'Google Drive')):
        out.append((os.path.join(home, name), prov))
    return out


def sync_roots():
    """[(folder, provider)] for cloud folders that exist, longest path first. Cached for a minute."""
    with _lock:
        if time.time() - _cache['at'] < 60:
            return _cache['roots']
        found = {}
        if IS_WIN:
            for get in (_registry_roots, _drive_label_roots, _fallback_roots):
                try:
                    for path, prov in get():
                        path = os.path.normpath(path)
                        if path.lower() not in found and os.path.isdir(path):
                            found[path.lower()] = (path, prov)
                except Exception:
                    continue
        _cache['roots'] = sorted(found.values(), key=lambda r: -len(r[0]))
        _cache['at'] = time.time()
        return _cache['roots']


def provider(path):
    """Cloud name for a path inside a sync folder, else None."""
    low = path.lower()
    for root, prov in sync_roots():
        r = root.lower()
        if low == r or low.startswith(r.rstrip('\\/') + os.sep):
            return prov
    return None


def online_only(path, prov=None):
    """True when reading the file would download it. Google Drive's drive letter doesn't report this,
    so its files always count as online."""
    prov = prov or provider(path)
    if not prov:
        return False
    if prov == 'Google Drive' and len(path) > 2 and path[1] == ':' and any(
            r.lower() == path[:3].lower() for r, p in sync_roots() if p == 'Google Drive'):
        return True       # ponytail: Drive's own drive letter hides cache state; ask Drive's API if this gets annoying
    try:
        return bool(getattr(os.stat(path), 'st_file_attributes', 0) & ONLINE_ATTRS)
    except OSError:
        return False


if __name__ == '__main__':
    # Self-check on a fake root (no registry needed).
    _cache.update(at=time.time(), roots=[(r'C:\Users\x\Dropbox', 'Dropbox'), (r'C:\Users\x\OneDrive', 'OneDrive')])
    assert provider(r'C:\Users\x\Dropbox\a\b.txt') == 'Dropbox'
    assert provider(r'C:\Users\x\dropbox') == 'Dropbox'
    assert provider(r'C:\Users\x\Dropbox2\b.txt') is None
    assert provider(r'C:\Users\x\OneDrive\b.txt') == 'OneDrive'
    assert online_only(r'C:\nowhere\b.txt') is False
    assert _pretty('OneDrive') == 'OneDrive' and _pretty('Dropbox') == 'Dropbox' and _pretty('iCloudDrive') == 'iCloud Drive'
    _cache['at'] = 0
    print('ok', sync_roots())
