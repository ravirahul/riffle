"""Build riffle-voice-pack.zip (+ .sha256) for the GitHub release.

Contents: site/ (sherpa-onnx, sounddevice + PortAudio, cffi as installed for this
Python) and models/voice/ (streaming English zipformer, int8). The app downloads it
from Settings and checks the hash. Must be built with the same Python as the app (3.14).
"""
import hashlib
import os
import sys
import sysconfig
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, 'dist')
SITE = sysconfig.get_paths()['purelib']
PKGS = ['sherpa_onnx', 'sounddevice.py', '_sounddevice.py', '_sounddevice_data', 'cffi', 'pycparser',
        '_cffi_backend.cp314-win_amd64.pyd']
SKIP = ('__pycache__', '32bit', 'arm64')        # 64-bit Windows only
MODEL = os.path.join(ROOT, 'models', 'voice')
MODEL_FILES = ('encoder.int8.onnx', 'decoder.int8.onnx', 'joiner.int8.onnx', 'tokens.txt')

def files():
    for pkg in PKGS:
        base = os.path.join(SITE, pkg)
        if not os.path.exists(base):
            sys.exit(f'missing {pkg} in {SITE}: pip install sherpa-onnx sounddevice first')
        if os.path.isfile(base):
            yield base, 'site/' + pkg
            continue
        for dp, dn, fn in os.walk(base):
            for f in fn:
                p = os.path.join(dp, f)
                if not any(s in p for s in SKIP):
                    yield p, 'site/' + os.path.relpath(p, SITE).replace(os.sep, '/')
    for f in MODEL_FILES:
        p = os.path.join(MODEL, f)
        if not os.path.exists(p):
            sys.exit(f'missing model file {p}')
        yield p, 'models/voice/' + f

os.makedirs(OUT, exist_ok=True)
zpath = os.path.join(OUT, 'riffle-voice-pack.zip')
with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    z.writestr('PACK.txt', f'Riffle voice pack for Python {sys.version_info.major}.{sys.version_info.minor}\n')
    for src, arc in files():
        z.write(src, arc)
h = hashlib.sha256()
with open(zpath, 'rb') as f:
    while chunk := f.read(1 << 20):
        h.update(chunk)
with open(zpath + '.sha256', 'w') as f:
    f.write(f'{h.hexdigest()}  riffle-voice-pack.zip\n')
print(f'{zpath}: {os.path.getsize(zpath) / 2**20:.0f} MB, sha256 {h.hexdigest()[:16]}')
