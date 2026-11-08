"""Riffle quick bar: a Spotlight-style search popup in plain tkinter + ctypes.

    bar = QuickBar(search=, open_path=, reveal=, open_app=, icon_png=, theme=,
                   content=, picked=, preview=, preview_on=)   # last four optional
    bar.start()            # own daemon thread runs the Tk mainloop, window hidden
    bar.toggle()           # from any thread
    bar.show('query')      # from any thread

Calls from other threads go through root.after(), which tkinter marshals onto
the Tcl thread (Tcl is threaded in the Windows build). Nothing polls: while
hidden the Tk thread sleeps in Tcl_DoOneEvent and the worker threads in
queue.get(), so idle CPU is zero.
"""
import ast
import base64
import ctypes
import math
import operator
import queue
import re
import struct
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import zlib
from collections import OrderedDict, deque
from ctypes import wintypes as W

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
try:
    shcore = ctypes.windll.shcore
except OSError:
    shcore = None

THEMES = {
    'light': dict(paper='#F5F2EA', card='#FFFFFF', ink='#15161A', ink2='#3C3D44',
                  rule='#E0DACD', muted='#6B6457', mark='#FFE24D', mark_ink='#15161A',
                  sel='#FFF3B0'),
    'dark': dict(paper='#14151A', card='#1B1D24', ink='#EDEEF2', ink2='#B4B7C2',
                 rule='#282B34', muted='#9AA0AE', mark='#FFD426', mark_ink='#14151A',
                 sel='#3A3420'),
}
# Glass: the window's background colour is made fully see-through (colour key) and Windows
# blurs what is behind it (accent acrylic). `card` doubles as that key, `tint` is the blur's
# colour as 0xAABBGGRR; text antialiases against the key, which matches the tint, so no halo.
GLASS = {
    'light': dict(paper='#F3F3F6', card='#F3F3F6', ink='#15161A', ink2='#2E3038', rule='#C9CBD3',
                  muted='#4F525C', mark='#FFE24D', mark_ink='#15161A', sel='#DCDDE5', tint=0xC8F6F3F3),
    'dark': dict(paper='#1E1E20', card='#1E1E20', ink='#F4F5F8', ink2='#D5D7DE', rule='#4A4D58',
                 muted='#B4B8C4', mark='#FFD426', mark_ink='#14151A', sel='#3A3D4C', tint=0xB8201E1E),
}
CAT_DOT = {'document': '#3B6FD8', 'image': '#2E9E5B', 'video': '#8A4FD8', 'audio': '#D8487A',
           'code': '#1E9AA0', 'archive': '#D8892E', 'folder': '#E0B400', 'app': '#5A5E6B',
           'other': '#9A9488'}
PER_FILE_ICON = {'exe', 'lnk', 'ico', 'url', 'appref-ms', 'msc', 'cur', 'ani'}
MODES = ('search', 'image', 'voice')
MODE_HINTS = {
    'image': [('↵', 'choose a picture'), ('Ctrl+V', 'paste one'), ('← →', 'switch'), ('Esc', 'back')],
    'voice': [('↵', 'search now'), ('Esc', 'stop'), ('← →', 'switch')],
}
HINTS = [('?', 'help'), ('← →', 'image · voice'), ('↵', 'open'), ('Ctrl+↵', 'show in folder'), ('Ctrl+C', 'copy path'),
         ('Tab', 'open in Riffle'), ('Esc', 'close')]
MAX_ROWS = 11                 # grouped: top hit + groups + inside files + web row

# logical (96-dpi) sizes, multiplied by the monitor scale
WIDTH, HEAD, ROW, FOOT, PAD, LABEL = 680, 60, 44, 34, 6, 24
PREVIEW, PV_ROWS = 300, 6          # preview pane width; the list is at least this many rows tall beside it


# ── calculator: "12*(3+4)" answers in the bar ─────────────────────────────
_CALC_CHARS = re.compile(r'^[\d\s.,+\-*/x×÷^()%]+$')
_CALC_OP = re.compile(r'[\d)]\s*[-+*/x×÷^%]\s*[\d(.]')
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}

def calc(q):
    """(answer to copy, answer to show) for a plain arithmetic query, else None. Never eval()s."""
    s = (q or '').strip()
    m = re.match(r'^([\d.,]+)\s*%\s*of\s*([\d.,]+)$', s, re.I)      # "18% of 4,500"
    if m:
        s = f'{m.group(1)}*{m.group(2)}/100'
    if not _CALC_CHARS.match(s) or not _CALC_OP.search(s):
        return None
    e = s.replace(',', '').replace('×', '*').replace('x', '*').replace('÷', '/').replace('^', '**')

    def ev(n):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 64 or abs(a) > 1e6):
                raise ValueError('too big')
            return _OPS[type(n.op)](a, b)
        raise ValueError('not arithmetic')
    try:
        v = ev(ast.parse(e, mode='eval').body)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        if v.is_integer() and abs(v) < 1e15:
            v = int(v)
    if isinstance(v, int):
        return str(v), f'{v:,}'
    t = f'{v:.10g}'
    return t, t if 'e' in t else f'{v:,.10g}'

def _size(n):
    for u in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return f'{n:.0f} {u}' if u == 'bytes' else f'{n:.1f} {u}'
        n /= 1024


# ── tiny PNG resampler (icons come as 32 px, rows want 20 px * scale) ──────
def _png_chunks(png):
    i = 8
    while i < len(png):
        n, t = struct.unpack('>I4s', png[i:i + 8])
        yield t, png[i + 8:i + 8 + n]
        i += 12 + n

def _png_encode(w, h, rgba):
    raw = b''.join(b'\x00' + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))

def scale_png(png, size):
    """Area-average an 8-bit RGBA PNG to size x size. Anything else passes through."""
    try:
        chunks = list(_png_chunks(png))
        w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', chunks[0][1])
        if (w, h) == (size, size) or depth != 8 or ctype != 6 or inter:
            return png
        data = zlib.decompress(b''.join(d for t, d in chunks if t == b'IDAT'))
        stride, px = w * 4, bytearray(w * h * 4)
        prev = bytearray(stride)
        for y in range(h):
            f, line = data[y * (stride + 1)], bytearray(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                b, c = prev[x], (prev[x - 4] if x >= 4 else 0)
                if f == 1: line[x] = (line[x] + a) & 255
                elif f == 2: line[x] = (line[x] + b) & 255
                elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
                elif f == 4:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            px[y * stride:(y + 1) * stride] = line
            prev = line

        def weights(src):
            r, out = src / size, []
            for t in range(size):
                lo, hi, ws = t * r, (t + 1) * r, []
                for s in range(int(lo), min(src, math.ceil(hi))):
                    ws.append((s, min(hi, s + 1) - max(lo, s)))
                out.append(ws)
            return out
        wx, wy = weights(w), weights(h)
        out = bytearray(size * size * 4)
        for ty, ys in enumerate(wy):
            for tx, xs in enumerate(wx):
                r = g = b = a = tot = 0.0
                for sy, fy in ys:
                    for sx, fx in xs:
                        k, f = (sy * w + sx) * 4, fy * fx
                        al = px[k + 3] * f          # premultiplied, so edges stay clean
                        r += px[k] * al; g += px[k + 1] * al; b += px[k + 2] * al
                        a += al; tot += f
                o = (ty * size + tx) * 4
                if a:
                    out[o:o + 4] = bytes((round(r / a), round(g / a), round(b / a), round(a / tot)))
        return _png_encode(size, size, out)
    except Exception:
        return png


# ── Win32 helpers ──────────────────────────────────────────────────────────
class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT), ('rcWork', W.RECT), ('dwFlags', W.DWORD)]

def _dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except Exception:
        pass
    try:
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

def _cursor_monitor():
    """(left, top, width, height, scale) of the monitor under the mouse."""
    pt = W.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.MonitorFromPoint.restype = W.HMONITOR
    mon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    dx, dy = W.UINT(96), W.UINT(96)
    try:
        shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy))
    except Exception:
        pass
    r = mi.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top, dx.value / 96

def _force_foreground(hwnd):
    """SetForegroundWindow that also works when another app is in front."""
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(fg, None)
    attached = other and other != me and user32.AttachThreadInput(me, other, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, other, False)
    if user32.GetForegroundWindow() != hwnd:
        # last resort: a synthetic Alt tap counts as input for this process
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
    return user32.GetForegroundWindow() == hwnd

def _style_window(hwnd, rule):
    """Win11 rounded corners + rule-coloured border + drop shadow."""
    pref = ctypes.c_int(2)                                       # DWMWCP_ROUND
    ok = dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4) == 0
    _border(hwnd, rule)
    GCL_STYLE, CS_DROPSHADOW = -26, 0x00020000
    get = getattr(user32, 'GetClassLongPtrW', user32.GetClassLongW)
    set_ = getattr(user32, 'SetClassLongPtrW', user32.SetClassLongW)
    get.restype = set_.restype = ctypes.c_ssize_t
    get.argtypes = [W.HWND, ctypes.c_int]
    set_.argtypes = [W.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_(hwnd, GCL_STYLE, get(hwnd, GCL_STYLE) | CS_DROPSHADOW)
    return ok

class _ACCENT(ctypes.Structure):
    _fields_ = [('AccentState', ctypes.c_int), ('AccentFlags', ctypes.c_int),
                ('GradientColor', ctypes.c_uint), ('AnimationId', ctypes.c_int)]

class _WCAD(ctypes.Structure):
    _fields_ = [('Attribute', ctypes.c_int), ('Data', ctypes.c_void_p), ('SizeOfData', ctypes.c_size_t)]

def _glass(hwnd, tint):
    """Acrylic blur behind the window (tint 0xAABBGGRR), or off when tint is None. True if Windows took it."""
    try:
        a = _ACCENT(4 if tint else 0, 2, tint or 0, 0)       # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND
        d = _WCAD(19, ctypes.cast(ctypes.pointer(a), ctypes.c_void_p), ctypes.sizeof(a))
        return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(d)))
    except (AttributeError, OSError):
        return False

class _SPS(ctypes.Structure):
    _fields_ = [('ac', ctypes.c_ubyte), ('flag', ctypes.c_ubyte), ('pct', ctypes.c_ubyte),
                ('saver', ctypes.c_ubyte), ('life', ctypes.c_ulong), ('full', ctypes.c_ulong)]

def _blur_on():
    """False when Windows has see-through effects off: the Transparency setting, or Energy saver.
    The blur then shows as a flat tint (same as the Start menu), so Glass draws solid instead."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            if not winreg.QueryValueEx(k, 'EnableTransparency')[0]:
                return False
    except OSError:
        pass
    s = _SPS()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.saver == 1:
        return False
    return True

def _glass_supported():
    import sys
    return sys.getwindowsversion().build >= 17134               # Windows 10 1803+

def _border(hwnd, color):
    c = int(color[1:], 16)
    ref = ctypes.c_uint((c & 0xFF) << 16 | (c & 0xFF00) | (c >> 16))  # COLORREF is BGR
    dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ref), 4)


class QuickBar:
    def __init__(self, search, open_path, reveal, open_app, icon_png, theme,
                 content=None, picked=None, preview=None, preview_on=None, open_target=None, look=None,
                 visual=None, clip_image=None, voice_start=None, voice_stop=None):
        self._search, self._open_path, self._reveal = search, open_path, reveal
        self._open_app, self._icon_png, self._theme = open_app, icon_png, theme
        self._content, self._picked = content, picked            # text-inside search, "you opened this"
        self._open_target = open_target                           # URLs, settings pages, apps
        self._look = look or (lambda: 'paper')                    # 'paper' | 'glass'
        self._visual, self._clip_image = visual, clip_image       # image mode: rows for a picture
        self._voice_start, self._voice_stop = voice_start, voice_stop
        self._mode, self._modal = 'search', False
        self._listening, self._lvl, self._phase = False, 0.0, 0
        self._lvls = deque([0.0] * 36, maxlen=36)
        self._mode_hits, self._more_hits = [], []
        self._preview, self._preview_on = preview, preview_on or (lambda: True)
        self._pv_show = False
        self._pv_jobs, self._cjobs = queue.Queue(), queue.Queue()
        self._pv, self._pv_pending = OrderedDict(), set()
        self.root = None
        self.hwnd = 0
        self.rounded = None             # True if DWM accepted the corner preference
        self.shown = False
        self.last_show_ms = None        # show() call -> window mapped and painted
        self._ready = threading.Event()
        self._rows, self._sel, self._q_shown = [], 0, None
        self._seq = 0
        self._debounce = None
        self._jobs = queue.Queue()      # latest (seq, q) for the search worker
        self._icon_jobs = queue.Queue()
        self._icons, self._icon_pending = {}, set()
        self._scale, self._tname = None, None
        self._flash = None

    # ── public, any thread ───────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, name='quickbar-tk', daemon=True).start()
        threading.Thread(target=self._search_worker, name='quickbar-search', daemon=True).start()
        threading.Thread(target=self._icon_worker, name='quickbar-icons', daemon=True).start()
        threading.Thread(target=self._content_worker, name='quickbar-content', daemon=True).start()
        threading.Thread(target=self._preview_worker, name='quickbar-preview', daemon=True).start()
        self._ready.wait(5)

    def toggle(self):
        self._call(self._toggle)

    def show(self, q=''):
        self._call(self._show, q, time.perf_counter())

    def hide(self):
        self._call(self._hide)

    def _call(self, fn, *a):
        if self.root is not None and self._ready.is_set():
            self.root.after(0, fn, *a)

    # ── Tk thread ────────────────────────────────────────────────────────
    def _run(self):
        _dpi_aware()
        self.root = root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        fams = set(tkfont.families(root))
        pick = lambda *names: next((n for n in names if n in fams), names[-1])
        self._fam_ui = pick('Segoe UI Variable Text', 'Segoe UI')
        self._fam_ui_b = pick('Segoe UI Variable Text Semibold', 'Segoe UI Semibold')
        self._fam_mono = pick('Cascadia Mono', 'Consolas')
        self._fonts = {}
        self.cv = cv = tk.Canvas(root, highlightthickness=0, bd=0)
        cv.pack(fill='both', expand=True)
        self.var = tk.StringVar(master=root)
        self.entry = tk.Entry(root, textvariable=self.var, relief='flat', bd=0,
                              highlightthickness=0, exportselection=False)
        self._apply_scale(1.0)
        self._apply_theme()
        self.var.trace_add('write', lambda *_: self._typed())
        e = self.entry
        e.bind('<Down>', lambda ev: self._move(1))
        e.bind('<Up>', lambda ev: self._move(-1))
        e.bind('<Return>', lambda ev: self._act(self._open_path))
        e.bind('<Control-Return>', lambda ev: self._act(self._reveal))
        e.bind('<Alt-Return>', lambda ev: self._to_app())
        e.bind('<Tab>', lambda ev: self._to_app())
        e.bind('<Escape>', lambda ev: self._esc())
        e.bind('<F1>', lambda ev: self._help())
        e.bind('<Left>', lambda ev: self._arrow(-1))
        e.bind('<Right>', lambda ev: self._arrow(1))
        e.bind('<Control-v>', self._paste)
        e.bind('<Control-V>', self._paste)
        e.bind('<Control-c>', self._copy)
        e.bind('<Control-C>', self._copy)
        cv.bind('<Motion>', self._hover)
        cv.bind('<Button-1>', self._click)
        root.bind('<FocusOut>', lambda ev: root.after(60, self._check_focus))
        root.after(0, self._ready.set)
        root.mainloop()

    def _font(self, key, fam, px):
        f = self._fonts.get(key)
        size = -max(1, round(px * self._scale))
        if f is None:
            f = self._fonts[key] = tkfont.Font(self.root, family=fam, size=size)
        else:
            f.configure(family=fam, size=size)
        return f

    def _apply_scale(self, s):
        if s == self._scale:
            return
        self._scale = s
        self.f_q = self._font('q', self._fam_ui, 19)
        self.f_name = self._font('name', self._fam_ui, 14)
        self.f_path = self._font('path', self._fam_mono, 11.5)
        self.f_small = self._font('small', self._fam_ui, 11.5)
        self.f_key = self._font('key', self._fam_ui_b, 11.5)
        self.f_tag = self._font('tag', self._fam_ui_b, 11)
        self.entry.configure(font=self.f_q)
        self._icon_px = round(20 * s)
        self._icons.clear()             # re-rasterise at the new size
        self._icon_pending.clear()
        if hasattr(self, '_pv'):
            self._pv.clear()
            self._pv_pending.clear()

    def _apply_theme(self):
        name = 'dark' if (self._safe(self._theme) or 'light') == 'dark' else 'light'
        look = 'glass' if self._safe(self._look) == 'glass' and _glass_supported() else 'paper'
        if look == 'glass' and not _blur_on():
            look = 'glass-solid'                       # same colours, no see-through (Windows has it off)
        if (name, look) == self._tname:
            return
        self._tname = (name, look)
        self.c = (GLASS[name] if look == 'glass' else dict(GLASS[name], tint=None) if look == 'glass-solid'
                  else THEMES[name])
        c = self.c
        try:
            self.root.attributes('-transparentcolor', c['card'] if look == 'glass' else '')
        except tk.TclError:
            pass
        if self.hwnd and not _glass(self.hwnd, c.get('tint')) and look == 'glass':
            self.root.attributes('-transparentcolor', '')          # Windows said no: stay Paper
            self._tname, self.c = (name, 'paper'), THEMES[name]
            c = self.c
        self.root.configure(bg=c['card'])
        self.cv.configure(bg=c['card'])
        self.entry.configure(bg=c['card'], fg=c['ink'], insertbackground=c['ink'],
                             selectbackground=c['mark'], selectforeground=c['mark_ink'],
                             insertwidth=max(1, round(2 * self._scale)))
        if self.hwnd:
            _border(self.hwnd, c['rule'])

    @staticmethod
    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    def _toggle(self):
        if self.shown and user32.GetForegroundWindow() == self.hwnd:
            self._hide()
        else:
            self._show('', time.perf_counter())

    def _show(self, q, t0):
        x0, y0, mw, mh, s = _cursor_monitor()
        self._apply_scale(s)
        self._apply_theme()
        self._pv_show = bool(self._preview) and self._safe(self._preview_on) is not False
        self._mon = (x0, y0, mw, mh)
        if self._mode != 'search':
            self._set_mode('search', quiet=True)
        if q:
            self.var.set(q)             # traces schedule the search
        else:
            self._schedule(0)           # refresh results / recent list
        self._layout()
        if not self.shown:
            self.root.deiconify()
        self.shown = True
        self.root.lift()
        self.root.update_idletasks()
        if not self.hwnd:
            self.hwnd = int(self.root.wm_frame(), 16)
            self.rounded = _style_window(self.hwnd, self.c['rule'])
            if self.c.get('tint') and not _glass(self.hwnd, self.c['tint']):
                self._tname = None                                   # retry as Paper
                self._look = lambda: 'paper'
                self._apply_theme()
                self._paint()
        _force_foreground(self.hwnd)
        self.entry.focus_force()
        self.entry.select_range(0, 'end') if not q else self.entry.selection_clear()
        self.entry.icursor('end')
        self.last_show_ms = (time.perf_counter() - t0) * 1000

    def _hide(self):
        if self._listening:
            self._safe(self._voice_stop)
            self._listening = False
        if self.shown:
            self.shown = False
            self.root.withdraw()

    def _check_focus(self):
        if self.shown and not self._modal and user32.GetForegroundWindow() != self.hwnd:
            self._hide()

    # ── modes: search, image, voice ──────────────────────────────────────
    def _typed(self):
        if self._mode == 'image' and self.var.get().strip():
            self._mode = 'search'                    # typing in image mode means a normal search
        self._schedule()

    def _arrow(self, d):
        """← → switch modes when the box is empty (or in image/voice); otherwise move the cursor."""
        if self.var.get() and self._mode == 'search':
            return None
        self._set_mode(MODES[(MODES.index(self._mode) + d) % len(MODES)])
        return 'break'

    def _set_mode(self, mode, quiet=False):
        if self._listening and mode != 'voice':
            self._safe(self._voice_stop)
            self._listening = False
        self._mode = mode
        if quiet:
            return
        if mode == 'search':
            self._schedule(0)
        elif mode == 'image':
            self.var.set('')
            self._mode = 'image'
            self._set_rows(self._image_intro())
        else:
            self._voice_go()
        self.entry.focus_set()

    def _set_rows(self, rows, q='\x00'):
        self._seq += 1                              # results still on their way are now stale
        self._rows, self._sel, self._q_shown = rows, 0, q
        self._layout()
        self._want_preview()

    @staticmethod
    def _note(name, sub, glyph='i', group=None, **kw):
        return dict(kind='help', name=name, dir=sub, title=name, about=kw.pop('about', sub), glyph=glyph,
                    group=group, fill=None, path='', **kw)

    # image mode
    def _image_intro(self):
        g = 'Find the original of a picture'
        return [self._note('Choose a picture or video…', 'From this PC: finds the original, other copies and look-alikes',
                           '+', g, action='pick', about='Riffle finds where the original lives, even after crops, '
                           'filters or resizing, plus other copies and pictures that look alike.'),
                self._note('Paste from the clipboard', 'Ctrl+V: a copied picture, screenshot or file', '⎘', g,
                           action='paste', about='Copy a picture in any app (or press Win+Shift+S for a '
                           'screenshot), then press Ctrl+V here.')]

    def _pick_image(self):
        from tkinter import filedialog
        self._modal = True
        try:
            path = filedialog.askopenfilename(parent=self.root, title='Find the original of…', filetypes=[
                ('Pictures and videos', '*.jpg *.jpeg *.png *.webp *.heic *.gif *.bmp *.tif *.tiff *.avif '
                                        '*.mp4 *.mov *.mkv *.avi *.webm *.m4v'), ('All files', '*.*')])
        finally:
            self._modal = False
            if self.hwnd:
                _force_foreground(self.hwnd)
                self.entry.focus_force()
        if path:
            self._image_search(path)

    def _paste(self, ev=None):
        if self._mode != 'image' and not (self._mode == 'search' and not self.var.get()):
            return None                              # ordinary text paste
        src = self._safe(self._clip_image) if self._clip_image else None
        if not src:
            if self._mode == 'image':
                self._flash = 'No picture copied'
                self._paint()
                self.root.after(1200, self._unflash)
                return 'break'
            return None
        self._mode = 'image'
        self._image_search(src)
        return 'break'

    def _image_search(self, src):
        self._set_rows([self._note('Looking for the original…', 'The first search loads the picture model, '
                                   'so it takes a few seconds', '…', 'Find the original of a picture')], '\x00')
        seq = self._seq

        def run():
            try:
                rows, err = self._visual(src) if self._visual else [], None
            except Exception as e:
                rows, err = [], str(e)
            self._call(self._image_done, seq, rows, err)
        threading.Thread(target=run, daemon=True, name='quickbar-image').start()

    def _image_done(self, seq, rows, err):
        if seq != self._seq or self._mode != 'image':
            return
        if err:
            rows = [self._note(err.split('. ')[0] + '.', 'Enter opens Riffle', '!', 'Find the original of a picture',
                               app=True, about=err)]
        elif not rows:
            rows = [self._note('No match on this PC', 'Nothing in your visual-search folders looks like it', '?',
                               'Find the original of a picture', about='Add the folder that holds the original '
                               'in Riffle › Settings › Visual search, then try again.', app=True)]
        self._set_rows(rows, '\x00')

    # voice mode
    def _voice_go(self):
        self.var.set('')
        self._mode = 'voice'
        g = 'Voice search'
        if not self._voice_start:
            return self._set_rows([self._note('Voice search is not set up', 'Enter opens Riffle', '!', g, app=True)])
        err = self._safe(self._voice_start, self._on_voice_text, self._on_voice_level, self._on_voice_end)
        if err:
            return self._set_rows([self._note(err.split('. ')[0] + '.', 'Enter opens Riffle to add it', '!', g,
                                              app=True, about=err)])
        self._listening = True
        self._lvls.extend([0.0] * self._lvls.maxlen)
        self._set_rows([self._note('Speak now', 'Words appear as you talk. Pause when you are done.', '●', g,
                                   about='Speech is turned into text on this PC. Nothing is recorded or sent.')])
        self._anim()

    def _on_voice_level(self, level):                # audio thread: just store it
        self._lvl = level

    def _on_voice_text(self, text, final):
        self._call(self._voice_text, text, final)

    def _on_voice_end(self, err):
        self._call(self._voice_end, err)

    def _voice_text(self, text, final):
        if self._mode != 'voice':
            return
        if text and text != self.var.get():
            self.var.set(text)                       # the search runs as you speak
            self.entry.icursor('end')
        if final:
            self._listening = False
            if text:
                self._mode = 'search'
            else:
                self._set_rows([self._note("Didn't catch that", 'Press Enter to try again', '↻', 'Voice search',
                                           action='voice')])
            self._paint()

    def _voice_end(self, err):
        self._listening = False
        if err and self._mode == 'voice':
            self._set_rows([self._note('The microphone did not start', err[:90], '!', 'Voice search',
                                       about='Check that a microphone is connected and that Windows › Settings › '
                                       'Privacy & security › Microphone allows desktop apps.')])
        if self.shown and self.hwnd:
            _border(self.hwnd, self.c['rule'])
            self._paint()

    def _anim(self):
        """About 30 frames a second, only while listening: the level graph and the listening border."""
        if not self._listening or not self.shown:
            return
        prev = self._lvls[-1]
        self._lvls.append(prev * 0.45 + self._lvl * 0.55)
        self._phase += 1
        self._draw_voice()
        self.root.after(33, self._anim)

    @staticmethod
    def _mix(a, b, t):
        a, b = int(a[1:], 16), int(b[1:], 16)
        ch = [round(((a >> k) & 255) * (1 - t) + ((b >> k) & 255) * t) for k in (16, 8, 0)]
        return '#%02X%02X%02X' % tuple(ch)

    def _draw_voice(self):
        cv, c, p = self.cv, self.c, self._px
        cv.delete('voice')
        if self._mode != 'voice' or not self._listening:
            return
        w, h, hy = cv.winfo_width(), cv.winfo_height(), p(HEAD) // 2
        lvl = self._lvls[-1]
        # level graph: newest on the right, mirrored around the middle line, older bars fade
        x1 = self._viz[1]
        n, bw, gap = len(self._lvls), p(3), p(3)
        for i, v in enumerate(self._lvls):
            wob = 0.08 * (1 + math.sin(self._phase * 0.22 + i * 0.55))      # breathing while quiet
            hh = max(p(2), (max(v, wob) ** 0.8) * p(19))
            x = x1 - (n - 1 - i) * (bw + gap)
            col = self._mix(c['rule'], c['mark'], 0.25 + 0.75 * (i / (n - 1)))
            cv.create_line(x, hy - hh, x, hy + hh, fill=col, width=bw, capstyle='round', tags='voice')
        # mic glyph with a ring that grows with your voice
        gx, gy = p(22), hy
        ring = p(9) + lvl * p(7)
        cv.create_oval(gx - ring, gy - ring, gx + ring, gy + ring, outline=self._mix(c['card'], c['mark'], 0.4 + 0.6 * lvl),
                       width=p(2), tags='voice')
        # listening border: a soft pulse, brighter when you talk
        t = 0.5 + 0.5 * math.sin(self._phase * 0.16)
        edge = self._mix(c['rule'], c['mark'], min(1.0, 0.35 + 0.35 * t + 0.6 * lvl))
        for k, wgt in ((1, 1.0), (3, 0.45)):
            cv.create_rectangle(p(k), p(k), w - p(k) - 1, h - p(k) - 1, outline=self._mix(c['card'], edge, wgt),
                                width=p(2), tags='voice')
        if self._phase % 3 == 0 and self.hwnd:
            _border(self.hwnd, edge)

    # ── search ───────────────────────────────────────────────────────────
    def _schedule(self, delay=25):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(delay, self._fire)

    def _fire(self):
        self._debounce = None
        if self._mode != 'search' and not (self._mode == 'voice' and self.var.get().strip()):
            return                                   # image/voice show their own rows
        self._seq += 1
        q = self.var.get().strip()
        self._jobs.put((self._seq, q))

    def _search_worker(self):
        while True:
            job = self._jobs.get()
            while not self._jobs.empty():          # skip stale queries
                job = self._jobs.get_nowait()
            seq, q = job
            rows = self._safe(self._search, q, MAX_ROWS) or []
            c = calc(q)
            if c:
                rows = [{'calc': True, 'value': c[0], 'name': c[1], 'path': '', 'group': 'Answer',
                         'dir': q + '   ·   Enter copies the answer'}] + rows
            rows = rows[:MAX_ROWS]
            self._call(self._results, seq, q, rows)
            if self._content and len(q) >= 3 and not c and not (rows and rows[0].get('kind') == 'help'):
                self._cjobs.put((seq, q, rows))

    def _content_worker(self):
        """Files whose text contains the query, added under the name matches once Windows answers."""
        while True:
            job = self._cjobs.get()
            while not self._cjobs.empty():
                job = self._cjobs.get_nowait()
            seq, q, rows = job
            if seq != self._seq:
                continue
            inside = self._safe(self._content, q, 2, [r['path'] for r in rows if r.get('path')]) or []
            if inside and seq == self._seq:
                web = [r for r in rows if r.get('group') == 'Search the web']
                rest = [r for r in rows if r.get('group') != 'Search the web']
                inside = [dict(r, group='Inside files') for r in inside][:max(1, MAX_ROWS - len(rows))]
                self._call(self._results, seq, q, rest + inside + web, True)

    def _results(self, seq, q, rows, keep=False):
        if seq != self._seq:
            return
        cur = self._current() if keep else None
        self._rows, self._q_shown, self._sel = rows, q, 0
        if cur in rows:
            self._sel = rows.index(cur)
        self._layout()
        self._want_preview()

    # ── preview pane ─────────────────────────────────────────────────────
    def _pv_box(self):
        p = self._px
        return p(PREVIEW) - p(32)       # square image box, inside the pane padding

    @staticmethod
    def _key(r):
        return r.get('path') or str(r.get('target') or r.get('name') or '')

    def _want_preview(self):
        r = self._current()
        if not self._pv_show or not r or r.get('calc') or r.get('kind') == 'help':
            return
        key = self._key(r)
        if key not in self._pv and key not in self._pv_pending:
            self._pv_pending.add(key)
            self._pv_jobs.put((key, r, self._pv_box()))

    def _preview_worker(self):
        while True:
            job = self._pv_jobs.get()
            skipped = []
            while not self._pv_jobs.empty():             # arrowing fast: only the last one
                skipped.append(job[0])
                job = self._pv_jobs.get_nowait()
            key, r, px = job
            data = self._safe(self._preview, r, px) or {}
            self._call(self._pv_ready, key, data, px, skipped)

    def _pv_ready(self, key, data, px, skipped=()):
        for k in skipped:
            self._pv_pending.discard(k)
        self._pv_pending.discard(key)
        if px != self._pv_box():
            return
        img = None
        if data.get('png'):
            try:
                img = tk.PhotoImage(master=self.root, data=base64.b64encode(data['png']))
                box = self._px(44) if data.get('site') else px
                f = math.ceil(max(img.width() / box, img.height() / box, 1))
                if f > 1:
                    img = img.subsample(f)
                elif data.get('site') and img.width() * 2 <= box:
                    img = img.zoom(box // img.width())
            except tk.TclError:
                img = None
        self._pv[key] = dict(data, img=img, png=None)
        while len(self._pv) > 40:
            self._pv.popitem(last=False)
        r = self._current()
        if self.shown and r and self._key(r) == key:
            self._paint()
        elif self.shown:
            self._want_preview()

    # ── icons ────────────────────────────────────────────────────────────
    def _icon_key(self, r):
        kind = r.get('kind')
        if kind in ('app', 'setting'):
            return r.get('icon_path') or '<settings>'
        if kind in ('web', 'site'):
            return '<web>'
        ext = (r.get('ext') or '').lower()
        if r.get('cat') == 'folder':
            return '<folder>'
        return r['path'] if ext in PER_FILE_ICON or not ext else ext

    def _icon(self, r):
        if r.get('calc') or r.get('kind') == 'help':
            return None
        key = self._icon_key(r)
        img = self._icons.get(key)
        if img is None and key not in self._icon_pending:
            self._icon_pending.add(key)
            path = key if r.get('kind') in ('app', 'setting', 'web', 'site') else r['path']
            self._icon_jobs.put((key, path, r.get('ext') or '', self._icon_px))
        return img or None

    def _icon_worker(self):
        while True:
            key, path, ext, px = self._icon_jobs.get()
            png = self._safe(self._icon_png, path, ext)
            if png:
                png = scale_png(png, px)
            self._call(self._icon_ready, key, png, px)

    def _icon_ready(self, key, png, px):
        if px != self._icon_px:
            return
        img = False
        if png:
            try:
                img = tk.PhotoImage(master=self.root, data=base64.b64encode(png))
            except tk.TclError:
                pass
        self._icons[key] = img
        if self.shown and any(self._icon_key(r) == key for r in self._rows):
            self._paint()

    # ── drawing ──────────────────────────────────────────────────────────
    def _px(self, v):
        return round(v * self._scale)

    def _row_tops(self):
        """(y of each row, [(y, label)] for each group heading, y below the last row)."""
        p = self._px
        y, tops, labels, prev = p(HEAD) + 1 + p(PAD), [], [], None
        for r in self._rows:
            g = r.get('group') or (None if self._q_shown else 'Recent')
            if g and g != prev:
                labels.append((y, g.upper(), r.get('more') or 0))
                y += p(LABEL)
            prev = g
            tops.append(y)
            y += p(ROW)
        return tops, labels, y

    def _layout(self):
        p = self._px
        n = len(self._rows)
        body = 0
        if n:
            body = self._row_tops()[2] - (p(HEAD) + 1) + p(PAD)
            if self._pv_show:
                body = max(body, p(PAD) * 2 + PV_ROWS * p(ROW))
        elif self._q_shown:
            body = p(ROW)
        w = p(WIDTH) + (p(PREVIEW) if self._pv_show else 0)
        h = p(HEAD) + 1 + body + 1 + p(FOOT)
        x0, y0, mw, mh = getattr(self, '_mon', None) or _cursor_monitor()[:4]
        self._paint(w, h)
        top = max(0, min(round(mh * 0.22), mh - h - p(20)))      # tall results move up, never off screen
        self.root.geometry(f'{w}x{h}+{x0 + (mw - w) // 2}+{y0 + top}')

    def _paint(self, w=None, h=None):
        cv, c, p = self.cv, self.c, self._px
        w = w or cv.winfo_width()
        h = h or cv.winfo_height()
        keep = getattr(self, '_ewin', None)
        cv.delete(*[i for i in cv.find_all() if i != keep])
        # header: glyph, entry, tag
        hy = p(HEAD) // 2
        gx, gr = p(22), p(6.5)
        if self._mode == 'voice':
            self._glyph_mic(gx, hy, c['ink'] if self._listening else c['muted'])
        elif self._mode == 'image':
            self._glyph_image(gx, hy, c['muted'])
        else:
            cv.create_oval(gx - gr, hy - gr - p(2), gx + gr, hy + gr - p(2), outline=c['muted'], width=p(1.8))
            cv.create_line(gx + gr * .72, hy + gr * .72 - p(2), gx + p(11), hy + p(9) - p(2) + p(2),
                           fill=c['muted'], width=p(2), capstyle='round')
        self._draw_tag(w - p(18), hy)
        mx = self._draw_modes(w - p(118), hy)          # search · image · voice, left of the tag
        right = mx - p(10)
        if self._mode == 'voice' and self._listening:
            self._viz = (right - p(214), right)
            right -= p(224)
        if keep is None:   # created once: re-creating it would steal focus from the entry
            self._ewin = cv.create_window(0, 0, window=self.entry, anchor='w')
        cv.coords(self._ewin, p(42), hy)
        hint = self._mode != 'search' and not self.var.get()       # Tk entries have no placeholder:
        cv.itemconfigure(self._ewin, width=p(10) if hint else max(p(80), right - p(42)), height=p(34))
        if hint:                                                    # shrink it, write the hint beside the caret
            ph = 'Listening…' if self._listening else 'Voice search' if self._mode == 'voice' \
                else 'Find the original of a picture'
            cv.create_text(p(56), hy, text=ph, anchor='w', fill=c['muted'], font=self.f_q)
        cv.create_line(0, p(HEAD), w, p(HEAD), fill=c['rule'])
        # body
        tops, labels, _ = self._row_tops()
        lw0 = p(WIDTH) if self._pv_show else w
        self._more_hits = []
        for ly, text, more in labels:
            cv.create_text(p(20), ly + p(LABEL) // 2 + p(2), text=text, anchor='w',
                           fill=c['muted'], font=self.f_tag)
            if more:                                # "+45 more": the rest is one Tab away
                t = cv.create_text(lw0 - p(20), ly + p(LABEL) // 2 + p(2), text=f'+{more:,} more  ›', anchor='e',
                                   fill=c['ink2'], font=self.f_tag)
                b = cv.bbox(t)
                self._more_hits.append((b[0] - p(6), ly, b[2] + p(6), ly + p(LABEL)))
        if not self._rows and self._q_shown:
            cv.create_text(p(20), p(HEAD) + 1 + p(ROW) // 2, text='No files named like that',
                           anchor='w', fill=c['muted'], font=self.f_name)
        lw = p(WIDTH) if self._pv_show else w
        for i, r in enumerate(self._rows):
            self._draw_row(i, r, tops[i], lw)
        if self._pv_show and self._rows:
            self._draw_preview(lw, p(HEAD) + 1, w, h - p(FOOT))
        # footer
        fy = h - p(FOOT)
        cv.create_line(0, fy, w, fy, fill=c['rule'])
        cv.create_rectangle(0, fy + 1, w, h, fill=c['paper'], outline='')
        x, my = p(18), fy + p(FOOT) // 2
        for k, label in MODE_HINTS.get(self._mode, HINTS):
            t = cv.create_text(x, my, text=k, anchor='w', fill=c['ink2'], font=self.f_key)
            x = cv.bbox(t)[2] + p(5)
            t = cv.create_text(x, my, text=label, anchor='w', fill=c['muted'], font=self.f_small)
            x = cv.bbox(t)[2] + p(16)
        if self._flash:
            tw = self.f_key.measure(self._flash)
            cv.create_rectangle(w - p(18) - tw - p(14), my - p(10), w - p(18), my + p(10),
                                fill=c['mark'], outline='')
            cv.create_text(w - p(18) - p(7), my, text=self._flash, anchor='e',
                           fill=c['mark_ink'], font=self.f_key)
        self._draw_voice()

    def _draw_modes(self, right, cy):
        """Search · Image · Voice buttons, current one highlighted. Returns their left edge."""
        cv, c, p = self.cv, self.c, self._px
        size, gap = p(30), p(4)
        x = right - 3 * size - 2 * gap
        left = x
        self._mode_hits = []
        for m in MODES:
            on = m == self._mode
            if on:
                self._round_rect(x, cy - size // 2, x + size, cy + size // 2, p(8), c['sel'])
            col = c['ink'] if on else c['muted']
            mx, my = x + size // 2, cy
            if m == 'search':
                r = p(5)
                cv.create_oval(mx - r - p(1), my - r - p(1), mx + r - p(1), my + r - p(1), outline=col, width=p(1.6))
                cv.create_line(mx + r * .6, my + r * .6, mx + p(6), my + p(6), fill=col, width=p(1.8), capstyle='round')
            elif m == 'image':
                self._glyph_image(mx, my, col, small=True)
            else:
                self._glyph_mic(mx, my, col, small=True)
            self._mode_hits.append((x, cy - size // 2, x + size, cy + size // 2, m))
            x += size + gap
        return left

    def _glyph_image(self, cx, cy, col, small=False):
        cv, p = self.cv, self._px
        k = 0.85 if small else 1.0                      # a framed picture: sun and a mountain
        x0, y0, x1, y1 = cx - p(8 * k), cy - p(6.5 * k), cx + p(8 * k), cy + p(6.5 * k)
        cv.create_rectangle(x0, y0, x1, y1, outline=col, width=p(1.6))
        r = p(1.8 * k)
        sx, sy = cx + p(3.5 * k), cy - p(2.5 * k)
        cv.create_oval(sx - r, sy - r, sx + r, sy + r, fill=col, outline='')
        cv.create_polygon(x0 + p(1.5), y1 - p(1), cx - p(2.5 * k), cy - p(1 * k), cx + p(1 * k), cy + p(3 * k),
                          cx + p(3 * k), cy + p(1.5 * k), x1 - p(1.5), y1 - p(1), fill=col, outline='')

    def _glyph_mic(self, cx, cy, col, small=False):
        cv, p = self.cv, self._px
        k = 0.8 if small else 1.0
        bw, bh = p(6 * k), p(10 * k)
        self._round_rect(cx - bw / 2, cy - bh / 2 - p(3 * k), cx + bw / 2, cy + bh / 2 - p(3 * k), bw / 2, col)
        cv.create_arc(cx - p(7 * k), cy - p(9 * k), cx + p(7 * k), cy + p(5 * k), start=200, extent=140,
                      style='arc', outline=col, width=p(1.6))
        cv.create_line(cx, cy + p(5 * k), cx, cy + p(8 * k), fill=col, width=p(1.6))

    def _draw_tag(self, right, cy):
        """The yellow RIFFLE logo: spaced caps on a slightly tilted highlighter."""
        cv, c, p = self.cv, self.c, self._px
        text = ' '.join('RIFFLE')                  # hair spaces ~ letter-spacing .14em
        bw, bh = self.f_tag.measure(text) + p(16), p(22)
        cx = right - bw / 2
        a = math.radians(1.2)        # Tk angles are counter-clockwise, like CSS rotate(-1.2deg)
        rot = lambda dx, dy: (cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a))
        pts = [rot(-bw / 2, -bh / 2), rot(bw / 2, -bh / 2), rot(bw / 2, bh / 2), rot(-bw / 2, bh / 2)]
        cv.create_polygon([v for pt in pts for v in pt], fill=c['mark'], outline='')
        # text stays straight: rotated text with hair spaces kerns unevenly ("RIFF LE")
        cv.create_text(cx, cy, text=text, fill=c['mark_ink'], font=self.f_tag)

    def _draw_row(self, i, r, y, w):
        cv, c, p = self.cv, self.c, self._px
        rh = p(ROW)
        if i == self._sel:
            x1, x2 = p(8), w - p(8)
            self._round_rect(x1, y + p(1), x2, y + rh - p(1), p(6), c['sel'])
            cv.create_rectangle(x1, y + p(10), x1 + p(2), y + rh - p(10), fill=c['ink'], outline='')
        ix, iy = p(30), y + rh // 2
        img = self._icon(r)
        if r.get('calc'):
            cv.create_text(ix, iy, text='=', fill=c['ink'], font=self.f_q)
        elif r.get('kind') == 'help':
            cv.create_text(ix, iy, text=r.get('glyph', '?'), fill=c['ink2'], font=self.f_key)
        elif img:
            cv.create_image(ix, iy, image=img)
        else:
            d = p(4.5)
            cv.create_oval(ix - d, iy - d, ix + d, iy + d, outline='',
                           fill=CAT_DOT.get(r.get('cat'), CAT_DOT['other']))
        tx, avail = p(52), w - p(52) - p(20)
        name, hl = self._clip_end(r.get('name') or '', avail, self.f_name), r.get('hl')
        ny, py = y + p(14), y + p(31)
        if hl and 0 <= hl[0] < hl[1]:
            a, b = hl[0], min(hl[1], len(name.rstrip('…')) if name.endswith('…') else len(name))
            if a < b:
                hx1 = tx + self.f_name.measure(name[:a])
                hx2 = tx + self.f_name.measure(name[:b])
                cv.create_rectangle(hx1 - p(1), ny - p(9), hx2 + p(1), ny + p(9),
                                    fill=c['mark'], outline='')
                cv.create_text(tx, ny, text=name[:a], anchor='w', fill=c['ink'], font=self.f_name)
                cv.create_text(hx1, ny, text=name[a:b], anchor='w', fill=c['mark_ink'], font=self.f_name)
                cv.create_text(hx2, ny, text=name[b:], anchor='w', fill=c['ink'], font=self.f_name)
                hl = True
        if hl is not True:
            cv.create_text(tx, ny, text=name, anchor='w', fill=c['ink'], font=self.f_name)
        sub = r.get('dir') or ''
        if r.get('inside'):
            tag = f'“{self._q_shown}” inside'
            tw = self.f_small.measure(tag) + p(10)
            cv.create_text(tx, py, text=tag, anchor='w', fill=c['ink2'], font=self.f_small)
            cv.create_text(tx + tw, py, text=self._clip_mid(sub, avail - tw, self.f_path),
                           anchor='w', fill=c['muted'], font=self.f_path)
        else:
            cv.create_text(tx, py, text=self._clip_mid(sub, avail, self.f_path),
                           anchor='w', fill=c['muted'], font=self.f_path)

    def _draw_preview(self, x0, y0, x1, y1):
        """Right pane: thumbnail (or first lines of text), name, size and date of the selected row."""
        cv, c, p = self.cv, self.c, self._px
        cv.create_rectangle(x0, y0, x1, y1, fill=c['paper'], outline='')
        cv.create_line(x0, y0, x0, y1, fill=c['rule'])
        r = self._current()
        if not r:
            return
        px, pad = self._pv_box(), p(16)
        bx, by = x0 + pad, y0 + pad
        inner = x1 - x0 - 2 * pad
        if r.get('kind') == 'help':
            cv.create_text(bx, by + p(6), text=r.get('title', r['name']), anchor='nw', fill=c['ink'],
                           font=self.f_name, width=inner)
            cv.create_text(bx, by + p(36), text=r.get('about', ''), anchor='nw', fill=c['ink2'],
                           font=self.f_small, width=inner)
            if r.get('fill') is not None:
                cv.create_text(bx, y1 - p(16), text='Enter tries it', anchor='sw', fill=c['muted'],
                               font=self.f_small)
            return
        if r.get('calc'):
            cv.create_text(bx, by + p(10), text=r['dir'].split('   ·')[0], anchor='nw', fill=c['muted'],
                           font=self.f_path, width=inner)
            cv.create_text(bx, by + p(40), text='= ' + r['name'], anchor='nw', fill=c['ink'], font=self.f_q,
                           width=inner)
            return
        d = self._pv.get(self._key(r))
        if r.get('kind') in ('web', 'site'):
            return self._draw_site(r, d, bx, by, inner, y1)
        box_h = min(px, (y1 - y0) - p(96))
        if d is None:
            cv.create_text(bx + inner // 2, by + box_h // 2, text='…', fill=c['muted'], font=self.f_name)
        elif d.get('text') is not None:
            self._round_rect(bx, by, bx + inner, by + box_h, p(6), c['card'])
            lh = self.f_path.metrics('linespace')
            ty = by + p(10)
            for line in d['text'].expandtabs(4).splitlines()[:max(1, (box_h - p(20)) // lh)]:
                cv.create_text(bx + p(10), ty, text=self._clip_end(line, inner - p(20), self.f_path),
                               anchor='nw', fill=c['ink2'], font=self.f_path)
                ty += lh
        elif d.get('img'):
            cv.create_image(bx + inner // 2, by + box_h // 2, image=self._fit(d, inner, box_h))
        else:
            img = self._icon(r)
            if img:
                cv.create_image(bx + inner // 2, by + box_h // 2, image=img)
        ny = by + box_h + p(14)
        tx = bx
        icon = self._icon(r)
        if icon:                                        # file-type icon beside the name
            cv.create_image(bx + self._icon_px // 2, ny + self.f_name.metrics('linespace') // 2, image=icon)
            tx = bx + self._icon_px + p(8)
        cv.create_text(tx, ny, text=self._clip_end(r.get('name') or '', inner - (tx - bx), self.f_name),
                       anchor='nw', fill=c['ink'], font=self.f_name)
        meta = []
        if d and d.get('meta'):
            meta.append(d['meta'])
        elif r.get('cat') != 'folder' and r.get('size') is not None:
            meta.append(_size(int(r['size'])))
        if r.get('mtime'):
            meta.append(time.strftime('%d %b %Y, %H:%M', time.localtime(int(r['mtime']))))
        if r.get('online'):
            meta.append('online only')
        cv.create_text(bx, ny + p(24), text=self._clip_end('  ·  '.join(meta), inner, self.f_small),
                       anchor='nw', fill=c['muted'], font=self.f_small)

    def _draw_site(self, r, d, bx, by, inner, y1):
        """A website: its icon, title, address and how often you went there."""
        cv, c, p = self.cv, self.c, self._px
        tile = p(64)
        self._round_rect(bx, by, bx + tile, by + tile, p(14), '#FFFFFF')   # most site icons are drawn for white
        img = (d or {}).get('img') or self._icon(r)
        if img:
            cv.create_image(bx + tile // 2, by + tile // 2, image=img)
        y = by + tile + p(16)
        t = cv.create_text(bx, y, text=r.get('title') or r.get('name') or '', anchor='nw', fill=c['ink'],
                           font=self.f_name, width=inner)
        y = cv.bbox(t)[3] + p(4)
        url = str(r.get('url') or r.get('target') or '')
        host = r.get('host') or (url.split('/')[2] if '//' in url else url)
        t = cv.create_text(bx, y, text=host, anchor='nw', fill=c['ink2'], font=self.f_key)
        y = cv.bbox(t)[3] + p(10)
        if len(url) > 140:
            url = url[:137] + '…'
        t = cv.create_text(bx, y, text=url, anchor='nw', fill=c['muted'], font=self.f_small, width=inner)
        y = cv.bbox(t)[3] + p(14)
        facts = []
        if r.get('visits'):
            facts.append(f"Visited {r['visits']} time{'' if r['visits'] == 1 else 's'}")
        if r.get('bookmarked'):
            facts.append('In your bookmarks')
        if r.get('mtime'):
            facts.append('Last visit ' + time.strftime('%d %b %Y', time.localtime(int(r['mtime']))))
        for f in facts:
            t = cv.create_text(bx, y, text=f, anchor='nw', fill=c['ink2'], font=self.f_small)
            y = cv.bbox(t)[3] + p(4)
        hint = (d or {}).get('meta') or 'Enter opens it in your browser'
        cv.create_text(bx, y1 - p(16), text=hint, anchor='sw', fill=c['muted'], font=self.f_small)

    def _fit(self, d, w, h):
        """The preview picture shrunk to fit w x h (cached per size), so it never covers the text."""
        img = d['img']
        if img.width() <= w and img.height() <= h:
            return img
        key = ('fit', w, h)
        if key not in d:
            f = math.ceil(max(img.width() / w, img.height() / h))
            d[key] = img.subsample(f)
        return d[key]

    def _round_rect(self, x1, y1, x2, y2, rad, fill):
        pts = [x1 + rad, y1, x2 - rad, y1, x2, y1, x2, y1 + rad, x2, y2 - rad, x2, y2,
               x2 - rad, y2, x1 + rad, y2, x1, y2, x1, y2 - rad, x1, y1 + rad, x1, y1]
        self.cv.create_polygon(pts, smooth=True, fill=fill, outline='')

    @staticmethod
    def _clip_end(s, avail, f):
        if f.measure(s) <= avail:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            m = (lo + hi + 1) // 2
            if f.measure(s[:m] + '…') <= avail: lo = m
            else: hi = m - 1
        return s[:lo] + '…'

    @staticmethod
    def _clip_mid(s, avail, f):
        """Ellipsize in the middle, keeping more of the end (the nearest folders)."""
        if f.measure(s) <= avail:
            return s
        cut = lambda k: s[:k - (2 * k) // 3] + '…' + s[len(s) - (2 * k) // 3:]
        lo, hi = 0, len(s)
        while lo < hi:
            m = (lo + hi + 1) // 2
            if f.measure(cut(m)) <= avail: lo = m
            else: hi = m - 1
        return cut(lo)

    # ── input ────────────────────────────────────────────────────────────
    def _move(self, d):
        if self._rows:
            self._sel = (self._sel + d) % len(self._rows)
            self._paint()
            self._want_preview()
        return 'break'

    def _row_at(self, y, x=0):
        if self._pv_show and x >= self._px(WIDTH):
            return None
        for i, t in enumerate(self._row_tops()[0]):
            if t <= y < t + self._px(ROW):
                return i
        return None

    def _hover(self, ev):
        i = self._row_at(ev.y, ev.x)
        if i is not None and i != self._sel:
            self._sel = i
            self._paint()
            self._want_preview()

    def _click(self, ev):
        for x1, y1, x2, y2, m in self._mode_hits:
            if x1 <= ev.x <= x2 and y1 <= ev.y <= y2:
                self._set_mode(m)
                return
        for x1, y1, x2, y2 in self._more_hits:
            if x1 <= ev.x <= x2 and y1 <= ev.y <= y2:
                self._to_app()
                return
        i = self._row_at(ev.y, ev.x)
        if i is not None:
            self._sel = i
            self._act(self._open_path)

    def _current(self):
        return self._rows[self._sel] if self._rows and self._sel < len(self._rows) else None

    def _help(self):
        self.var.set('?')
        self.entry.icursor('end')
        return 'break'

    def _act(self, fn):
        r = self._current()
        if self._mode == 'voice' and self._listening:
            self._safe(self._voice_stop)                  # Enter while talking: search what was heard
            return 'break'
        if self._mode == 'image' and not self._rows:
            self._pick_image()
            return 'break'
        if r and r.get('kind') == 'help':
            act = r.get('action')
            if r.get('app'):
                return self._to_app()
            if act == 'pick':
                self._pick_image()
            elif act == 'paste':
                self._paste()
            elif act == 'voice':
                self._voice_go()
            elif r.get('fill') is not None:                 # an example: put it in the box and search it
                self.var.set(r['fill'])
                self.entry.icursor('end')
        elif r and r.get('calc'):
            self._copy_text(r['value'])
        elif r:
            q = self.var.get().strip()
            target = r.get('target')

            def run():
                if target is not None and self._open_target:
                    self._safe(self._open_target, target)
                else:
                    self._safe(fn, r['path'])
                if self._picked:
                    self._safe(self._picked, self._key(r), q)
            threading.Thread(target=run, daemon=True).start()
            self._hide()
        return 'break'

    def _to_app(self):
        q = self.var.get().strip()
        threading.Thread(target=self._safe, args=(self._open_app, q), daemon=True).start()
        self._hide()
        return 'break'

    def _esc(self):
        if self._mode == 'voice' and self._listening:
            self._safe(self._voice_stop)
            return 'break'
        if self._mode != 'search':
            self._set_mode('search')
            return 'break'
        if self.var.get():
            self.var.set('')
        else:
            self._hide()
        return 'break'

    def _copy(self, ev):
        if self.entry.selection_present():
            return None                  # normal text copy
        r = self._current()
        if r:
            self._copy_text(r['value'] if r.get('calc') else r.get('path') or r.get('url')
                            or (r['target'] if isinstance(r.get('target'), str) else r['name']))
        return 'break'

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._flash = 'Copied'
        self._paint()
        self.root.after(900, self._unflash)

    def _unflash(self):
        self._flash = None
        self._paint()


if __name__ == '__main__':
    assert calc('12*(3+4)') == ('84', '84')
    assert calc('1,000 + 250') == ('1250', '1,250')
    assert calc('10/4') == ('2.5', '2.5')
    assert calc('2^10') == ('1024', '1,024')
    assert calc('7 x 6') == ('42', '42')
    assert calc('-3 + 1') == ('-2', '-2')
    assert calc('18% of 4,500') == ('810', '810')
    for no in ('2026', 'report 2', '1/0', '9^999', '__import__("os")', '', 'abc+1', '(1+'):
        assert calc(no) is None, no
    assert _size(1536) == '1.5 KB' and _size(12) == '12 bytes'
    print('quickbar ok')
