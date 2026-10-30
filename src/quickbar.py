"""Riffle quick bar: a Spotlight-style search popup in plain tkinter + ctypes.

    bar = QuickBar(search=, open_path=, reveal=, open_app=, icon_png=, theme=)
    bar.start()            # own daemon thread runs the Tk mainloop, window hidden
    bar.toggle()           # from any thread
    bar.show('query')      # from any thread

Calls from other threads go through root.after(), which tkinter marshals onto
the Tcl thread (Tcl is threaded in the Windows build). Nothing polls: while
hidden the Tk thread sleeps in Tcl_DoOneEvent and the worker threads in
queue.get(), so idle CPU is zero.
"""
import base64
import ctypes
import math
import queue
import struct
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import zlib
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
CAT_DOT = {'document': '#3B6FD8', 'image': '#2E9E5B', 'video': '#8A4FD8', 'audio': '#D8487A',
           'code': '#1E9AA0', 'archive': '#D8892E', 'folder': '#E0B400', 'app': '#5A5E6B',
           'other': '#9A9488'}
PER_FILE_ICON = {'exe', 'lnk', 'ico', 'url', 'appref-ms', 'msc', 'cur', 'ani'}
HINTS = [('↵', 'open'), ('Ctrl+↵', 'show in folder'), ('Ctrl+C', 'copy path'),
         ('Tab', 'open in Riffle'), ('Esc', 'close')]
MAX_ROWS = 8

# logical (96-dpi) sizes, multiplied by the monitor scale
WIDTH, HEAD, ROW, FOOT, PAD, LABEL = 680, 60, 44, 34, 6, 24


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

def _border(hwnd, color):
    c = int(color[1:], 16)
    ref = ctypes.c_uint((c & 0xFF) << 16 | (c & 0xFF00) | (c >> 16))  # COLORREF is BGR
    dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ref), 4)


class QuickBar:
    def __init__(self, search, open_path, reveal, open_app, icon_png, theme):
        self._search, self._open_path, self._reveal = search, open_path, reveal
        self._open_app, self._icon_png, self._theme = open_app, icon_png, theme
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
        self.var = tk.StringVar()
        self.entry = tk.Entry(root, textvariable=self.var, relief='flat', bd=0,
                              highlightthickness=0, exportselection=False)
        self._apply_scale(1.0)
        self._apply_theme()
        self.var.trace_add('write', lambda *_: self._schedule())
        e = self.entry
        e.bind('<Down>', lambda ev: self._move(1))
        e.bind('<Up>', lambda ev: self._move(-1))
        e.bind('<Return>', lambda ev: self._act(self._open_path))
        e.bind('<Control-Return>', lambda ev: self._act(self._reveal))
        e.bind('<Alt-Return>', lambda ev: self._to_app())
        e.bind('<Tab>', lambda ev: self._to_app())
        e.bind('<Escape>', lambda ev: self._esc())
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

    def _apply_theme(self):
        name = 'dark' if (self._safe(self._theme) or 'light') == 'dark' else 'light'
        if name == self._tname:
            return
        self._tname, self.c = name, THEMES[name]
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
        self._mon = (x0, y0, mw, mh)
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
        _force_foreground(self.hwnd)
        self.entry.focus_force()
        self.entry.select_range(0, 'end') if not q else self.entry.selection_clear()
        self.entry.icursor('end')
        self.last_show_ms = (time.perf_counter() - t0) * 1000

    def _hide(self):
        if self.shown:
            self.shown = False
            self.root.withdraw()

    def _check_focus(self):
        if self.shown and user32.GetForegroundWindow() != self.hwnd:
            self._hide()

    # ── search ───────────────────────────────────────────────────────────
    def _schedule(self, delay=25):
        if self._debounce:
            self.root.after_cancel(self._debounce)
        self._debounce = self.root.after(delay, self._fire)

    def _fire(self):
        self._debounce = None
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
            self._call(self._results, seq, q, rows[:MAX_ROWS])

    def _results(self, seq, q, rows):
        if seq != self._seq:
            return
        self._rows, self._q_shown, self._sel = rows, q, 0
        self._layout()

    # ── icons ────────────────────────────────────────────────────────────
    def _icon_key(self, r):
        ext = (r.get('ext') or '').lower()
        if r.get('cat') == 'folder':
            return '<folder>'
        return r['path'] if ext in PER_FILE_ICON or not ext else ext

    def _icon(self, r):
        key = self._icon_key(r)
        img = self._icons.get(key)
        if img is None and key not in self._icon_pending:
            self._icon_pending.add(key)
            self._icon_jobs.put((key, r['path'], r.get('ext') or '', self._icon_px))
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

    def _list_top(self):
        return self._px(HEAD) + 1 + self._px(PAD) + (self._px(LABEL) if self._recent() else 0)

    def _recent(self):
        return not self._q_shown and bool(self._rows)

    def _layout(self):
        p = self._px
        n = len(self._rows)
        body = 0
        if n:
            body = p(PAD) * 2 + (p(LABEL) if self._recent() else 0) + n * p(ROW)
        elif self._q_shown:
            body = p(ROW)
        w, h = p(WIDTH), p(HEAD) + 1 + body + 1 + p(FOOT)
        x0, y0, mw, mh = getattr(self, '_mon', None) or _cursor_monitor()[:4]
        self._paint(w, h)
        self.root.geometry(f'{w}x{h}+{x0 + (mw - w) // 2}+{y0 + round(mh * 0.22)}')

    def _paint(self, w=None, h=None):
        cv, c, p = self.cv, self.c, self._px
        w = w or cv.winfo_width()
        h = h or cv.winfo_height()
        keep = getattr(self, '_ewin', None)
        cv.delete(*[i for i in cv.find_all() if i != keep])
        # header: glyph, entry, tag
        hy = p(HEAD) // 2
        gx, gr = p(22), p(6.5)
        cv.create_oval(gx - gr, hy - gr - p(2), gx + gr, hy + gr - p(2), outline=c['muted'], width=p(1.8))
        cv.create_line(gx + gr * .72, hy + gr * .72 - p(2), gx + p(11), hy + p(9) - p(2) + p(2),
                       fill=c['muted'], width=p(2), capstyle='round')
        self._draw_tag(w - p(18), hy)
        if keep is None:   # created once: re-creating it would steal focus from the entry
            self._ewin = cv.create_window(0, 0, window=self.entry, anchor='w')
        cv.coords(self._ewin, p(42), hy)
        cv.itemconfigure(self._ewin, width=w - p(42) - p(104), height=p(34))
        cv.create_line(0, p(HEAD), w, p(HEAD), fill=c['rule'])
        # body
        y = p(HEAD) + 1 + p(PAD)
        if self._recent():
            cv.create_text(p(20), y + p(LABEL) // 2 + p(2), text='RECENT', anchor='w',
                           fill=c['muted'], font=self.f_tag)
            y += p(LABEL)
        if not self._rows and self._q_shown:
            cv.create_text(p(20), p(HEAD) + 1 + p(ROW) // 2, text='No files named like that',
                           anchor='w', fill=c['muted'], font=self.f_name)
        for i, r in enumerate(self._rows):
            self._draw_row(i, r, y + i * p(ROW), w)
        # footer
        fy = h - p(FOOT)
        cv.create_line(0, fy, w, fy, fill=c['rule'])
        cv.create_rectangle(0, fy + 1, w, h, fill=c['paper'], outline='')
        x, my = p(18), fy + p(FOOT) // 2
        for k, label in HINTS:
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
        if img:
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
        cv.create_text(tx, py, text=self._clip_mid(r.get('dir') or '', avail, self.f_path),
                       anchor='w', fill=c['muted'], font=self.f_path)

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
        return 'break'

    def _row_at(self, y):
        i = (y - self._list_top()) // self._px(ROW)
        return i if 0 <= i < len(self._rows) else None

    def _hover(self, ev):
        i = self._row_at(ev.y)
        if i is not None and i != self._sel:
            self._sel = i
            self._paint()

    def _click(self, ev):
        i = self._row_at(ev.y)
        if i is not None:
            self._sel = i
            self._act(self._open_path)

    def _current(self):
        return self._rows[self._sel] if self._rows and self._sel < len(self._rows) else None

    def _act(self, fn):
        r = self._current()
        if r:
            threading.Thread(target=self._safe, args=(fn, r['path']), daemon=True).start()
            self._hide()
        return 'break'

    def _to_app(self):
        q = self.var.get().strip()
        threading.Thread(target=self._safe, args=(self._open_app, q), daemon=True).start()
        self._hide()
        return 'break'

    def _esc(self):
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
            self.root.clipboard_clear()
            self.root.clipboard_append(r['path'])
            self._flash = 'Copied'
            self._paint()
            self.root.after(900, self._unflash)
        return 'break'

    def _unflash(self):
        self._flash = None
        self._paint()
