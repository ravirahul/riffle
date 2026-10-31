# PyInstaller spec for the light Riffle build (search, preview, duplicates, cleanup).
# Visual search libraries are left out and installed later as the visual pack.
# Build with build.bat.
import os

HERE = os.path.abspath(SPECPATH)
SRC = os.path.join(os.path.dirname(HERE), 'src')

# stdlib modules the visual pack needs (written by pack_needs.py)
with open(os.path.join(HERE, 'pack_stdlib.txt')) as f:
    pack_stdlib = [l.strip() for l in f if l.strip()]

# The visual pack's OpenCV is built against Python's stable ABI and the C++ runtime.
# Nothing in the light app needs them, so name them or PyInstaller leaves them out.
import sys
SYS32 = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'System32')
pack_dlls = [(os.path.join(os.path.dirname(sys.executable), 'python3.dll'), '.')] + [
    (os.path.join(SYS32, d), '.') for d in ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'concrt140.dll')
    if os.path.exists(os.path.join(SYS32, d))]

a = Analysis(
    [os.path.join(SRC, 'riffle.py')],
    pathex=[SRC],
    binaries=pack_dlls,
    datas=[
        (os.path.join(SRC, 'ui.html'), '.'),
        (os.path.join(SRC, 'ui-extra.css'), '.'),
        (os.path.join(SRC, 'ui-extra.js'), '.'),
        (os.path.join(SRC, 'ui-settings.css'), '.'),
        (os.path.join(SRC, 'ui-settings.js'), '.'),
        (os.path.join(SRC, 'ui-insights.css'), '.'),
        (os.path.join(SRC, 'ui-insights.js'), '.'),
        (os.path.join(SRC, 'ui-pro.css'), '.'),
        (os.path.join(SRC, 'ui-pro.js'), '.'),
        (os.path.join(SRC, 'riffle.ico'), '.'),
        (os.path.join(SRC, 'fonts'), 'fonts'),
    ],
    hiddenimports=['features', 'dupes', 'cleanup', 'shellthumb', 'visual', 'quickbar', 'license', 'numpy.core', 'numpy.core.multiarray',
                   'tkinter', 'tkinter.filedialog', 'winreg'] + pack_stdlib,
    excludes=['openvino', 'openvino_telemetry', 'cv2', 'PIL', 'pillow_heif', 'torch',
              'matplotlib', 'scipy', 'pandas', 'IPython', 'pytest', 'setuptools',
              'yaml', 'charset_normalizer', 'pydoc', 'lib2to3', 'unittest.mock'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Riffle',
    icon=os.path.join(SRC, 'riffle.ico'),
    console=False,
    version=os.path.join(HERE, 'version.txt'),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Riffle')
