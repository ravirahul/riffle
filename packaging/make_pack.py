"""Build riffle-visual-pack.zip (+ .sha256) for the GitHub release.

Contents: site/ (OpenCV, OpenVINO, Pillow as installed for this Python) and
models/ (the DINOv2 model). The app downloads it on request and checks the hash.
Must be built with the same Python version as the app (3.14).
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
PKGS = ['PIL', 'pillow-12.3.0.dist-info', 'cv2', 'opencv_python_headless-5.0.0.93.dist-info',
        'openvino', 'openvino-2026.4.1.dist-info', 'openvino_telemetry',
        'openvino_telemetry-2025.2.0.dist-info']
SKIP_PARTS = ('__pycache__', os.sep + 'tests' + os.sep, os.sep + 'include' + os.sep)
# Visual search runs an OpenVINO IR model on the Intel GPU or CPU only, so the NPU
# stack (~110 MB) and the converters for other model formats are never loaded.
SKIP_NAMES = ('openvino_intel_npu', 'openvino_onnx_frontend', 'openvino_tensorflow',
              'openvino_paddle_frontend', 'openvino_pytorch_frontend', 'openvino_jax_frontend',
              'openvino_gguf_frontend', '_debug.')
SKIP_DIRS = tuple(os.sep + os.path.join('openvino', 'frontend', d) + os.sep
                  for d in ('onnx', 'tensorflow', 'paddle', 'pytorch', 'jax', 'tensorflow_lite', 'gguf'))

def files():
    for pkg in PKGS:
        base = os.path.join(SITE, pkg)
        if not os.path.exists(base):
            sys.exit(f'missing {pkg} in {SITE}: pip install the same versions first')
        for dp, dn, fn in os.walk(base):
            for f in fn:
                p = os.path.join(dp, f)
                if any(s in p + os.sep for s in SKIP_PARTS + SKIP_DIRS) or f.endswith(('.pyi', '.lib', '.h')) \
                        or any(n in f for n in SKIP_NAMES):
                    continue
                yield p, 'site/' + os.path.relpath(p, SITE).replace(os.sep, '/')
    for f in os.listdir(os.path.join(ROOT, 'models')):
        if f.startswith('dinov2'):
            yield os.path.join(ROOT, 'models', f), 'models/' + f

os.makedirs(OUT, exist_ok=True)
zpath = os.path.join(OUT, 'riffle-visual-pack.zip')
with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    z.writestr('PACK.txt', f'Riffle visual pack for Python {sys.version_info.major}.{sys.version_info.minor}\n')
    for src, arc in files():
        z.write(src, arc)
h = hashlib.sha256()
with open(zpath, 'rb') as f:
    while chunk := f.read(1 << 20):
        h.update(chunk)
with open(zpath + '.sha256', 'w') as f:
    f.write(f'{h.hexdigest()}  riffle-visual-pack.zip\n')
print(f'{zpath}: {os.path.getsize(zpath) / 2**20:.0f} MB, sha256 {h.hexdigest()[:16]}…')
