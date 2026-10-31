"""Check license.parse_key against keys signed with a throwaway CNG key pair.

Run: python -I tools/test_license.py   (Windows only)
"""
import base64, ctypes, hashlib, json, os, sys, tempfile
from ctypes import wintypes as W

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

bc = ctypes.WinDLL('bcrypt')
alg, key = W.HANDLE(), W.HANDLE()
assert bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'ECDSA_P256', None, 0) == 0
assert bc.BCryptGenerateKeyPair(alg, ctypes.byref(key), 256, 0) == 0
assert bc.BCryptFinalizeKeyPair(key, 0) == 0
cb = W.ULONG()
bc.BCryptExportKey(key, None, 'ECCPUBLICBLOB', None, 0, ctypes.byref(cb), 0)
blob = ctypes.create_string_buffer(cb.value)
assert bc.BCryptExportKey(key, None, 'ECCPUBLICBLOB', blob, cb, ctypes.byref(cb), 0) == 0
point = b'\x04' + blob.raw[8:8 + 64]
os.environ['RIFFLE_PUBKEY'] = point.hex()

def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip('=')
def sign(payload, prefix='RIFFLE1'):
    body = f'{prefix}.{b64u(json.dumps(payload).encode())}'
    h = hashlib.sha256(body.encode()).digest()
    sig = ctypes.create_string_buffer(64); n = W.ULONG()
    assert bc.BCryptSignHash(key, None, h, 32, sig, 64, ctypes.byref(n), 0) == 0
    return body + '.' + b64u(sig.raw[:n.value])

import license as L
good = {'v': 1, 'lid': 'abc123', 'email': 'a@b.c', 'name': 'Test', 'plan': 'lifetime', 'iat': 1, 'maxDevices': 3}
k = sign(good)
checks = {
    'valid key verifies': L.parse_key(k) == good,
    'whitespace and line breaks tolerated': L.parse_key(k[:40] + '\n ' + k[40:]) == good,
    'tampered payload rejected': L.parse_key(k.replace(k.split('.')[1], b64u(json.dumps({**good, 'plan': 'business'}).encode()))) is None,
    'tampered signature rejected': L.parse_key(k[:-3] + ('AAA' if not k.endswith('AAA') else 'BBB')) is None,
    'wrong prefix rejected': L.parse_key(sign(good, 'AMBI1')) is None,
    'v != 1 rejected': L.parse_key(sign({**good, 'v': 2})) is None,
    'garbage rejected': L.parse_key('hello') is None and L.parse_key('') is None,
}
d = tempfile.mkdtemp()
L.init(d)
s = L.status()
checks['new install is on trial'] = s['tier'] == 'trial' and s['trialDaysLeft'] == 14
L._state['trialStart'] -= 15 * 86400; L._save()
checks['trial ends after 14 days'] = L.status()['tier'] == 'free'
L._state.update(key=k, instanceId='inst1'); L._save()
checks['activated key is pro'] = L.status()['tier'] == 'pro' and L.is_pro()
L._state['revoked'] = True
checks['revoked key falls back'] = L.status()['tier'] == 'free'
for name, ok in checks.items():
    print(('PASS ' if ok else 'FAIL ') + name)
sys.exit(0 if all(checks.values()) else 1)
