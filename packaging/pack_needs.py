"""List the standard-library modules the visual pack needs at runtime.

The light build leaves OpenCV, OpenVINO and Pillow out, but the frozen app must
still contain every stdlib module they import, or the downloaded pack fails.
This runs a real model inference, image decode and video decode, then writes
the stdlib modules that got loaded to pack_stdlib.txt (read by riffle.spec).
"""
import os
import sys
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

before = set(sys.modules)
import numpy as np
import cv2
import openvino as ov
from PIL import Image, ImageOps
import visual

core = ov.Core()
model = core.compile_model(os.path.join(ROOT, 'models', 'dinov2_vits14.xml'), 'CPU')
model(np.zeros((1, 3, 224, 224), dtype=np.float32))
ok, buf = cv2.imencode('.jpg', np.zeros((64, 64, 3), dtype=np.uint8))
cv2.imdecode(buf, cv2.IMREAD_COLOR)
import io
ImageOps.exif_transpose(Image.open(io.BytesIO(buf.tobytes())).convert('RGB'))
visual.dhash(Image.new('RGB', (32, 32)))

stdlib = sysconfig.get_paths()['stdlib'].lower()
skip = ('openvino', 'cv2', 'PIL', 'visual', 'openvino_telemetry')
need = set()
for name, mod in list(sys.modules.items()):
    if name.split('.')[0] in skip or name in before:
        continue
    f = (getattr(mod, '__file__', None) or '').lower()
    if name.split('.')[0] == 'numpy' or not f or (f.startswith(stdlib) and 'site-packages' not in f):
        need.add(name)
need = sorted(n for n in need if not n.startswith('_frozen'))
with open(os.path.join(HERE, 'pack_stdlib.txt'), 'w') as fh:
    fh.write('\n'.join(need) + '\n')
print(len(need), 'stdlib modules:', ' '.join(need))
