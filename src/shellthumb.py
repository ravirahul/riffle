"""Explorer-quality thumbnails and file icons through the Windows shell.

Uses IShellItemImageFactory, the same API Explorer uses, so any file type with
a thumbnail handler works (photos, video, PDF, Office, PSD with codecs...) and
Windows' own thumbnail cache is reused. Standard library only (ctypes + zlib).
"""
import ctypes
import struct
import threading
import zlib
from collections import OrderedDict
from ctypes import wintypes as W

ole32 = ctypes.OleDLL('ole32')
shell32 = ctypes.OleDLL('shell32')
gdi32 = ctypes.WinDLL('gdi32')

class GUID(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16),
                ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]

    def __init__(self, s):
        s = s.replace('-', '')
        super().__init__(int(s[:8], 16), int(s[8:12], 16), int(s[12:16], 16),
                         (ctypes.c_ubyte * 8)(*bytes.fromhex(s[16:])))

class SIZE(ctypes.Structure):
    _fields_ = [('cx', W.LONG), ('cy', W.LONG)]

class BITMAP(ctypes.Structure):
    _fields_ = [('bmType', W.LONG), ('bmWidth', W.LONG), ('bmHeight', W.LONG),
                ('bmWidthBytes', W.LONG), ('bmPlanes', W.WORD),
                ('bmBitsPixel', W.WORD), ('bmBits', ctypes.c_void_p)]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', W.DWORD), ('biWidth', W.LONG), ('biHeight', W.LONG),
                ('biPlanes', W.WORD), ('biBitCount', W.WORD),
                ('biCompression', W.DWORD), ('biSizeImage', W.DWORD),
                ('biXPelsPerMeter', W.LONG), ('biYPelsPerMeter', W.LONG),
                ('biClrUsed', W.DWORD), ('biClrImportant', W.DWORD)]

IID_IShellItemImageFactory = GUID('bcc18b79-ba16-442f-80c4-8a59c30c463b')
GetImage = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, SIZE, ctypes.c_int,
                              ctypes.POINTER(W.HBITMAP))
Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

shell32.SHCreateItemFromParsingName.argtypes = [
    W.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
gdi32.GetObjectW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, W.UINT]
gdi32.CreateCompatibleDC.argtypes = [W.HDC]
gdi32.CreateCompatibleDC.restype = W.HDC
gdi32.DeleteDC.argtypes = [W.HDC]
gdi32.DeleteObject.argtypes = [W.HANDLE]

SIIGBF_BIGGERSIZEOK = 0x1
SIIGBF_ICONONLY = 0x4
SIIGBF_THUMBNAILONLY = 0x8
SIIGBF_INCACHEONLY = 0x10

_com = threading.local()

def _init_com():
    if not getattr(_com, 'ok', False):
        try:
            ole32.CoInitializeEx(None, 2)  # apartment threaded
        except OSError:
            pass  # already initialised in another mode: still usable
        _com.ok = True

def _png(w, h, bgra):
    """Encode top-down BGRA pixels as a PNG (RGBA)."""
    rgba = bytearray(bgra)
    rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
    if not any(rgba[3::4]):          # thumbnails often come back with alpha 0
        rgba[3::4] = b'\xff' * (w * h)
    stride = w * 4
    raw = b''.join(b'\x00' + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))

def _bitmap_png(hbm):
    bm = BITMAP()
    gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = bm.bmWidth, bm.bmHeight
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    dc = gdi32.CreateCompatibleDC(None)
    try:
        gdi32.GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.DeleteDC(dc)
    return _png(w, h, buf.raw)

def image(path, size=256, icon_only=False, thumb_only=False, cache_only=False):
    """PNG bytes for `path`, or None. Thumbnail with icon fallback by default.
    cache_only: only a thumbnail Windows already has, so an online-only cloud file isn't downloaded."""
    _init_com()
    ptr = ctypes.c_void_p()
    try:
        shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(IID_IShellItemImageFactory),
                                            ctypes.byref(ptr))
    except OSError:
        return None
    vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                       ctypes.POINTER(ctypes.c_void_p))
    flags = SIIGBF_BIGGERSIZEOK
    if icon_only:
        flags |= SIIGBF_ICONONLY
    if thumb_only:
        flags |= SIIGBF_THUMBNAILONLY
    if cache_only:
        flags |= SIIGBF_INCACHEONLY
    hbm = W.HBITMAP()
    try:
        GetImage(vtbl[3])(ptr, SIZE(size, size), flags, ctypes.byref(hbm))
        return _bitmap_png(hbm)
    except OSError:
        return None
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        Release(vtbl[2])(ptr)

# Icons are the same for every file of a type, except these.
PER_FILE_ICON = {'exe', 'lnk', 'ico', 'url', 'appref-ms', 'msc', 'cur', 'ani'}
_icons = OrderedDict()
_lock = threading.Lock()

def icon(path, ext, size=32):
    """File-type icon PNG, cached in memory by extension (max 400 entries)."""
    ext = (ext or '').lower()
    key = (path if ext in PER_FILE_ICON or not ext else ext, size)
    with _lock:
        if key in _icons:
            _icons.move_to_end(key)
            return _icons[key]
    png = image(path, size, icon_only=True)
    with _lock:
        _icons[key] = png
        while len(_icons) > 400:
            _icons.popitem(last=False)
    return png
