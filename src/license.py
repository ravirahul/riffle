"""Riffle Pro: licence keys, activation and the 14-day trial.

Keys are issued by riffle.bliink.in and look like
    RIFFLE1.<b64url(payload json)>.<b64url(r||s)>
signed with ECDSA P-256 / SHA-256 over "RIFFLE1.<payload b64url>". They are verified
here with Windows CNG (bcrypt.dll), so no crypto library is bundled.

Privacy: activation sends the key, a hash of this PC's id and the PC name; the
14-day recheck sends the key and the activation id. Nothing about files, ever.
Staying offline never takes Pro away: only the server saying "revoked" does.
"""
import base64
import ctypes
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.request
from ctypes import wintypes as W

SERVER = os.environ.get('RIFFLE_LICENSE_SERVER', 'https://riffle.bliink.in')
BUY_URL = SERVER + '/buy'
TRIAL_DAYS = 14
RECHECK_DAYS = 14
PREFIX = 'RIFFLE1'
# Public key of the signing key on riffle.bliink.in: uncompressed point 04||X||Y, hex.
# Filled in from riffle-license\PUBLIC-KEY.txt. Tests may override with RIFFLE_PUBKEY.
PUBLIC_KEY_HEX = '04f9437f0f9858e0ef0caf89dadf83ca7849f0746ce4583e4b0a506ac3bbacbbec0e4f624de0ccc32a9a69c355d7ff5b8b1d92d2257ef3056856311c9995e17746'

_lock = threading.Lock()
_state = {}
_path = None

# ---- signature check (Windows CNG) ------------------------------------------------
def _b64u(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))

def _cng_verify(point_hex, message, sig):
    """ECDSA P-256 / SHA-256 verify of `sig` (raw r||s, 64 bytes) with Windows CNG."""
    point = bytes.fromhex(point_hex)
    if len(point) != 65 or point[0] != 4 or len(sig) != 64:
        return False
    bc = ctypes.WinDLL('bcrypt')
    alg, key = W.HANDLE(), W.HANDLE()
    if bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'ECDSA_P256', None, 0) != 0:
        return False
    try:
        blob = (0x31534345).to_bytes(4, 'little') + (32).to_bytes(4, 'little') + point[1:]  # ECS1
        if bc.BCryptImportKeyPair(alg, None, 'ECCPUBLICBLOB', ctypes.byref(key), blob, len(blob), 0) != 0:
            return False
        try:
            digest = hashlib.sha256(message).digest()
            return bc.BCryptVerifySignature(key, None, digest, len(digest), sig, len(sig), 0) == 0
        finally:
            bc.BCryptDestroyKey(key)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)

def parse_key(key):
    """Payload dict for a genuine Riffle key, else None. Offline, constant time per key."""
    key = ''.join((key or '').split())
    parts = key.split('.')
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    pub = os.environ.get('RIFFLE_PUBKEY') or PUBLIC_KEY_HEX
    try:
        sig = _b64u(parts[2])
        if not pub or not _cng_verify(pub, f'{PREFIX}.{parts[1]}'.encode(), sig):
            return None
        p = json.loads(_b64u(parts[1]))
    except (ValueError, OSError):
        return None
    return p if isinstance(p, dict) and p.get('v') == 1 and isinstance(p.get('lid'), str) else None

# ---- local state -------------------------------------------------------------------
def init(data_dir):
    """Load (or start) the licence state. The trial clock starts on first launch."""
    global _path
    _path = os.path.join(data_dir, 'license.json')
    with _lock:
        try:
            with open(_path, encoding='utf-8') as f:
                _state.update(json.load(f))
        except (OSError, ValueError):
            pass
        changed = False
        if not _state.get('trialStart'):
            _state['trialStart'] = int(time.time()); changed = True
        if not _state.get('iid'):
            _state['iid'] = secrets.token_urlsafe(12); changed = True
        if changed:
            _save()

def _save():
    tmp = _path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_state, f)
    os.replace(tmp, _path)

def install_id():
    return _state.get('iid', '')

def install_source():
    """Where this install came from ("producthunt", "store"...), recorded by the installer
    from its download file name. "direct" when unknown or run from source."""
    import features
    if features.packaged():
        return 'store'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Bliink\Riffle') as k:
            src = str(winreg.QueryValueEx(k, 'Source')[0])
    except OSError:
        return 'direct'
    src = ''.join(c for c in src.lower() if c.isalnum() or c == '-')[:24]
    return src or 'direct'

def first_ping_pending():
    return not _state.get('firstPing')

def mark_first_ping():
    with _lock:
        if not _state.get('firstPing'):
            _state['firstPing'] = int(time.time())
            _save()

def _pc_id():
    """Stable per-PC id (hash of Windows' MachineGuid), so reinstalls reuse the same seat."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography', 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            guid = winreg.QueryValueEx(k, 'MachineGuid')[0]
    except OSError:
        guid = _state.get('iid', '')
    return base64.urlsafe_b64encode(hashlib.sha256(('riffle:' + guid).encode()).digest()[:24]).decode().rstrip('=')

def status():
    """{'tier': 'pro'|'trial'|'free', 'trialDaysLeft', 'email', 'plan', ...}"""
    with _lock:
        s = dict(_state)
    now = time.time()
    left = max(0, TRIAL_DAYS - int((now - s.get('trialStart', now)) // 86400))
    out = {'tier': 'free', 'trialDaysLeft': left, 'buyUrl': BUY_URL, 'email': '', 'plan': '',
           'activatedAt': s.get('activatedAt', 0), 'used': s.get('used', {})}
    p = parse_key(s.get('key', '')) if s.get('key') and s.get('instanceId') and not s.get('revoked') else None
    if p:
        out.update(tier='pro', email=s.get('email') or p.get('email', ''), plan=p.get('plan', 'lifetime'),
                   name=p.get('name', ''))
    elif left > 0:
        out['tier'] = 'trial'
    return out

def bump(key, n=1):
    """Count what Pro did ('freed' bytes, 'visual' searches) for the end-of-trial summary."""
    if not n:
        return
    with _lock:
        used = _state.setdefault('used', {})
        used[key] = used.get(key, 0) + int(n)
        _save()

def is_pro():
    """Pro features unlocked: a licence, or the trial."""
    return status()['tier'] in ('pro', 'trial')

# ---- server calls ------------------------------------------------------------------
def _post(path, body, timeout=15):
    req = urllib.request.Request(SERVER + path, json.dumps(body).encode(),
                                 {'Content-Type': 'application/json', 'User-Agent': 'Riffle'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

ERRORS = {
    'invalid': "That key isn't valid. Copy the whole line from the email and try again.",
    'revoked': 'This key has been turned off. Reply to your purchase email and we will sort it out.',
    'device_limit': 'This key is already used on 3 PCs. Deactivate one of them (Settings > Riffle Pro) first.',
}

def activate(key):
    key = ''.join((key or '').split())
    p = parse_key(key)
    if not p:
        return {'ok': False, 'error': ERRORS['invalid']}
    try:
        r = _post('/api/activate.php', {'key': key, 'instance': _pc_id(), 'label': socket.gethostname()[:60],
                                        'iid': install_id(), 'src': install_source()})   # ties the sale to its source
    except OSError:
        return {'ok': False, 'error': "Couldn't reach riffle.bliink.in. Check the internet connection and try again."}
    if not r.get('ok'):
        return {'ok': False, 'error': ERRORS.get(r.get('error'), 'Activation failed. Please try again.')}
    with _lock:
        _state.update(key=key, instanceId=r['instanceId'], email=(r.get('license') or {}).get('email', ''),
                      activatedAt=int(time.time()), lastCheck=int(time.time()), revoked=False)
        _save()
    return {'ok': True, **status()}

def deactivate():
    with _lock:
        key, inst = _state.get('key'), _state.get('instanceId')
    if key and inst:
        try:
            _post('/api/deactivate.php', {'key': key, 'instanceId': inst})
        except OSError:
            pass                     # frees the seat when it can; locally it's gone either way
    with _lock:
        for k in ('key', 'instanceId', 'email', 'activatedAt', 'lastCheck', 'revoked'):
            _state.pop(k, None)
        _save()
    return status()

def recheck_if_due():
    """Every RECHECK_DAYS, ask the server whether the key is still good. Offline is fine."""
    with _lock:
        key, inst, last = _state.get('key'), _state.get('instanceId'), _state.get('lastCheck', 0)
    if not key or not inst or time.time() - last < RECHECK_DAYS * 86400:
        return
    try:
        r = _post('/api/validate.php', {'key': key, 'instanceId': inst})
    except OSError:
        return
    with _lock:
        _state['lastCheck'] = int(time.time())
        if r.get('status') == 'revoked':
            _state['revoked'] = True
        elif r.get('status') == 'unknown_instance':
            # seat freed from another PC: try to take one again quietly next time
            _state.pop('instanceId', None)
        _save()
    if r.get('status') == 'unknown_instance':
        activate(key)
