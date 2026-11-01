"""Riffle visual search - drop an edited image or video, get back the original file.

Ported from Lens (RAVI\\lens). riffle.py imports this lazily; nothing here runs until visual search is used.

    pip install pillow opencv-python-headless openvino
    python visual.py        -> self-check on a synthetic set

Each image in the chosen folders gets a DINOv2 fingerprint (whole image + 5 regions, one model pass, run by
OpenVINO on the Intel GPU when there is one). A search compares against all of them, then SIFT feature matching
proves which candidates are the same picture, and the biggest proven copy is called the original.

Env:  LENS_ENGINE = auto | gpu | cpu | torch   (auto: OpenVINO on Intel GPU, then OpenVINO CPU, then PyTorch)
      LENS_VIEWS  = pool | crops                (how crop-robust fingerprints are made; see describe())
"""
import gc, hashlib, io, json, os, re, shutil, sqlite3, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

CV2_ERROR = ""
try:
    import cv2  # optional: geometric verification (crop/transform proof) and video decoding
except ImportError as e:
    cv2 = None
    CV2_ERROR = str(e)
try:
    import pillow_heif; pillow_heif.register_heif_opener()  # optional: HEIC/HEIF
except ImportError:
    pass

ROOT = Path(__file__).parent
DATA = ROOT / "visual-index"
THUMBS = DATA / "thumbs"
MODELS = Path(getattr(sys, "_MEIPASS", ROOT)) / "models"
MODEL_CACHE = MODELS / "cache"
IDLE_UNLOAD = 120                # seconds unused before the model is freed (reload ~1-2 s)
ENGINE = os.environ.get("LENS_ENGINE", "auto")
VIEWS = os.environ.get("LENS_VIEWS", "pool")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
# Higher = more likely to be a master file when two copies are otherwise equal.
EXT_RANK = {".psd": 4, ".tif": 4, ".tiff": 4, ".png": 3, ".heic": 2, ".heif": 2}
FRAME_EVERY = 2.0   # seconds between sampled video frames
MAX_FRAMES = 600
VERIFY_TOP = 40     # candidates that get SIFT verification
MIN_INLIERS = 15
BATCH = 16          # images fingerprinted per model call
THUMB = 640         # stored preview size; also what SIFT verifies against
MIN_IMAGE_BYTES = 8000  # smaller files are icons and UI bits, not photos

# ---------------------------------------------------------------- storage

def db():
    con = sqlite3.connect(DATA / "lens.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def index_tag():
    return f"v2-{VIEWS}"  # vectors from different VIEWS modes can't be compared

def init_db():
    THUMBS.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY, path TEXT, ts REAL DEFAULT 0, kind TEXT,
            mtime REAL, size INTEGER, w INTEGER, h INTEGER, sha TEXT, dhash INTEGER,
            UNIQUE(path, ts));
        CREATE TABLE IF NOT EXISTS vecs(item_id INTEGER, v BLOB);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE INDEX IF NOT EXISTS vecs_item ON vecs(item_id);
        CREATE INDEX IF NOT EXISTS items_sha ON items(sha);
        """)
        # columns added for cloud sources (Google Drive): which source a file came from and how to show/open it
        have = {r[1] for r in con.execute("PRAGMA table_info(items)")}
        for col in ("src", "name", "folder", "link"):
            if col not in have:
                con.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        if "label" not in {r[1] for r in con.execute("PRAGMA table_info(folders)")}:
            con.execute("ALTER TABLE folders ADD COLUMN label TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS items_src ON items(src)")
        row = con.execute("SELECT v FROM meta WHERE k='tag'").fetchone()
        if not row or row[0] != index_tag():
            if row:
                print(f"Fingerprint format changed ({row[0]} -> {index_tag()}): re-indexing everything.")
            con.execute("DELETE FROM vecs"); con.execute("DELETE FROM items")
            shutil.rmtree(THUMBS, ignore_errors=True); THUMBS.mkdir(parents=True)
            con.execute("INSERT OR REPLACE INTO meta VALUES('tag', ?)", (index_tag(),))

def config():
    try:
        return json.loads((DATA / "config.json").read_text())
    except (OSError, ValueError):
        return {}

def save_config(**kv):
    (DATA / "config.json").write_text(json.dumps({**config(), **kv}, indent=1))

# ---------------------------------------------------------------- google drive (shared folder links)

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_FIELDS = ("nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,thumbnailLink,resourceKey,"
                "imageMediaMetadata(width,height,rotation),videoMediaMetadata(width,height))")

class SourceError(Exception):
    """A problem the user can fix (not shared publicly, bad API key...). The message is shown as-is."""
    def __init__(self, msg, need_key=False):
        super().__init__(msg); self.need_key = need_key

def http_get(url, params=None, headers=None, as_json=True):
    """GET with retries on rate limits and server errors. Raises SourceError with a readable message."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Riffle/1", **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return json.loads(data) if as_json else data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) or "rateLimit" in body:
                time.sleep(min(30, 2 ** attempt)); continue
            raise drive_error(e.code, body) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 5:
                raise SourceError(f"Couldn't reach Google ({getattr(e, 'reason', e)}). Check your internet connection.") from None
            time.sleep(min(30, 2 ** attempt))
    raise SourceError("Google kept asking Riffle to slow down. It will retry on the next scan.")

def drive_error(code, body):
    if "keyInvalid" in body or "API key not valid" in body:
        return SourceError("Google rejected the API key. Check it was copied in full.", need_key=True)
    if "accessNotConfigured" in body or "has not been used in project" in body or "SERVICE_DISABLED" in body:
        return SourceError("The Google Drive API isn't turned on for this key's project yet. Enable it, wait a minute, then try again.", need_key=True)
    if code == 404:
        return SourceError("Riffle can't see this folder. In Google Drive, open Share, set General access to "
                           "\"Anyone with the link\", then add it again.")
    if code == 403:
        return SourceError("Google refused access to this folder or file. Make sure it's shared as \"Anyone with the link\".")
    return SourceError(f"Google Drive returned an error ({code}).")

def parse_drive_link(text):
    """(folder id, resource key or None) from any shared-folder link format, else None."""
    m = re.search(r"/folders/([\w-]{10,})", text) or re.search(r"[?&]id=([\w-]{10,})", text)
    if not m or "google.com" not in text:
        return None
    rk = re.search(r"[?&]resourcekey=([\w-]+)", text)
    return m.group(1), rk.group(1) if rk else None

def drive_source(fid, rk):
    return f"https://drive.google.com/drive/folders/{fid}" + (f"?resourcekey={rk}" if rk else "")

def drive_headers(fid, rk):
    # older shared links carry a resource key that must accompany every request for that item
    return {"X-Goog-Drive-Resource-Keys": f"{fid}/{rk}"} if rk else {}

def drive_key():
    key = config().get("google_api_key")
    if not key:
        raise SourceError("Riffle needs a free Google API key to read shared Drive folders.", need_key=True)
    return key

def drive_folder_name(src):
    fid, rk = parse_drive_link(src)
    meta = http_get(f"{DRIVE_API}/{fid}", {"key": drive_key(), "fields": "name,mimeType", "supportsAllDrives": "true"}, drive_headers(fid, rk))
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        raise SourceError("That link is to a single file, not a folder. Share the folder that contains it.")
    return meta["name"]

def drive_files(src, label):
    """Every image/video under a shared folder, recursively, as (file metadata, folder trail)."""
    key = drive_key()
    fid, rk = parse_drive_link(src)
    stack = [(fid, rk, [label])]
    while stack:
        folder, frk, trail = stack.pop()
        token = None
        while True:
            params = {"key": key, "q": f"'{folder}' in parents and trashed=false", "fields": DRIVE_FIELDS, "pageSize": 1000,
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            page = http_get(DRIVE_API, params, drive_headers(folder, frk))
            for f in page.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    stack.append((f["id"], f.get("resourceKey"), trail + [f["name"]]))
                elif f["mimeType"].startswith(("image/", "video/")):
                    yield f, trail
            token = page.get("nextPageToken")
            if not token:
                break

def drive_job(f, trail, src):
    meta = f.get("imageMediaMetadata") or f.get("videoMediaMetadata") or {}
    w, h = meta.get("width"), meta.get("height")
    if meta.get("rotation") in (1, 3) and w:
        w, h = h, w
    return {"path": f"gdrive/{f['id']}", "kind": "video" if f["mimeType"].startswith("video/") else "image",
            "mtime": datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")).timestamp(),
            "size": int(f.get("size") or 0), "src": src, "name": f["name"], "folder": " › ".join(["Google Drive"] + trail),
            "link": f"https://drive.google.com/file/d/{f['id']}/view", "drive": f, "w": w, "h": h}

def drive_media_url(f):
    return f"{DRIVE_API}/{f['id']}?alt=media&key={drive_key()}&supportsAllDrives=true"

def drive_image_bytes(f):
    """A ~800 px rendition: Drive's thumbnail when it has one (tiny download), else the file itself."""
    hdr = drive_headers(f["id"], f.get("resourceKey"))
    if f.get("thumbnailLink"):
        try:
            return http_get(re.sub(r"=s\d+$", "", f["thumbnailLink"]) + "=s800", headers=hdr, as_json=False)
        except SourceError:
            pass  # thumbnail links expire; fall through to the file
    return http_get(drive_media_url(f), headers=hdr, as_json=False)

# ---------------------------------------------------------------- model

_model_lock = threading.Lock()
_backend = {"run": None, "name": "loading"}
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

def torch_model():
    import torch, warnings
    warnings.filterwarnings("ignore", message="xFormers")
    torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
    local = Path(torch.hub.get_dir()) / "facebookresearch_dinov2_main"
    # after the first download, load from cache so Lens works offline
    repo, src = (str(local), "local") if local.exists() else ("facebookresearch/dinov2", "github")
    m = torch.hub.load(repo, "dinov2_vits14", source=src, verbose=False).eval()

    class Tokens(torch.nn.Module):  # CLS token + 16x16 patch tokens in one tensor: (B, 257, 384)
        def __init__(self, m):
            super().__init__(); self.m = m
        def forward(self, x):
            f = self.m.forward_features(x)
            return torch.cat([f["x_norm_clstoken"][:, None], f["x_norm_patchtokens"]], 1)
    return Tokens(m).eval()

def openvino_model(device):
    import openvino as ov
    xml = MODELS / "dinov2_vits14.xml"
    if not xml.exists():  # one-time conversion; after this PyTorch isn't needed at all
        import torch
        MODELS.mkdir(exist_ok=True)
        ovm = ov.convert_model(torch_model(), example_input=torch.zeros(1, 3, 224, 224), input=ov.PartialShape([-1, 3, 224, 224]))
        ov.save_model(ovm, str(xml))
    core = ov.Core()
    core.set_property({"CACHE_DIR": str(MODEL_CACHE)})  # GPU kernels compile once, then load fast
    cm = core.compile_model(str(xml), device)
    return lambda x: cm(x)[0]

def backend():
    with _model_lock:
        if _backend["run"] is None:
            order = {"auto": ["GPU", "CPU", "torch"], "gpu": ["GPU"], "cpu": ["CPU"], "torch": ["torch"]}[ENGINE]
            for name in order:
                try:
                    if name == "torch":
                        import torch
                        m = torch_model()
                        def run(x, m=m):
                            with torch.no_grad():
                                return m(torch.from_numpy(x)).numpy()
                    else:
                        run = openvino_model(name)
                    run(np.zeros((1, 3, 224, 224), np.float32))  # fail here, not mid-scan
                    _backend.update(run=run, name={"GPU": "Intel GPU (OpenVINO)", "CPU": "CPU (OpenVINO)", "torch": "CPU (PyTorch)"}[name])
                    break
                except Exception as e:
                    print(f"engine {name} unavailable: {e}", file=sys.stderr)
            if _backend["run"] is None:
                raise RuntimeError("No AI engine could start. Install it with: pip install openvino")
            _backend["used"] = time.time()
            if not _backend.get("watching"):
                _backend["watching"] = True
                threading.Thread(target=_unload_when_idle, daemon=True).start()
        return _backend["run"]

def _unload_when_idle():
    """Riffle runs all day; the model (~300 MB) only needs to be in memory while visual search is in use."""
    while True:
        time.sleep(60)
        with _model_lock:
            if _backend["run"] and not state["running"] and time.time() - _backend.get("used", 0) > IDLE_UNLOAD:
                _backend.update(run=None, name="asleep (loads on next search)")
                gc.collect()

def forward(imgs):
    """(N, 257, 384) tokens for a list of PIL images, in chunks so memory stays flat on 8 GB machines."""
    # ponytail: squash to 224x224 so every image batches together; aspect-true tiles if recall on panoramas suffers
    x = np.stack([(np.asarray(i.resize((224, 224), Image.BILINEAR), np.float32) / 255 - MEAN) / STD for i in imgs]).transpose(0, 3, 1, 2)
    run = backend()
    with _model_lock:  # one inference at a time: indexing and searching share the model
        _backend["used"] = time.time()
        return np.concatenate([run(np.ascontiguousarray(x[i:i + 32])) for i in range(0, len(x), 32)])

# 16x16 patch-grid boxes (y0, y1, x0, x1) for the regions an edit might have been cropped to
REGIONS = [(0, 10, 0, 10), (0, 10, 6, 16), (6, 16, 0, 10), (6, 16, 6, 16), (4, 12, 4, 12)]

def crop_views(img):
    w, h = img.size
    cw, ch = int(w * 0.6), int(h * 0.6)
    out = [img.crop((x, y, x + cw, y + ch)) for x, y in ((0, 0), (w - cw, 0), (0, h - ch), (w - cw, h - ch))]
    return out + [img.crop((w // 4, h // 4, w // 4 + w // 2, h // 4 + h // 2))]

def describe(imgs, regions):
    """Fingerprints per image: [whole] or [whole + 5 regions], each a unit 384-vector.

    pool:  one model pass; whole = mean of all patch tokens, region = mean of that region's patch tokens.
    crops: whole = CLS token; each region is cut out and run through the model again (6x the work).
    """
    if VIEWS == "crops":
        groups = [[i] + (crop_views(i) if regions else []) for i in imgs]
        cls = forward([v for g in groups for v in g])[:, 0]
        out, k = [], 0
        for g in groups:
            out.append(cls[k:k + len(g)]); k += len(g)
    else:
        grid = forward(imgs)[:, 1:].reshape(len(imgs), 16, 16, -1)
        boxes = [(0, 16, 0, 16)] + (REGIONS if regions else [])
        out = [np.stack([g[y0:y1, x0:x1].mean((0, 1)) for y0, y1, x0, x1 in boxes]) for g in grid]
    return [v / np.linalg.norm(v, axis=1, keepdims=True) for v in out]

# ---------------------------------------------------------------- fingerprints

def dhash(img):
    g = np.asarray(img.convert("L").resize((9, 8), Image.BILINEAR), np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int(sum(1 << i for i, b in enumerate(bits) if b)) - (1 << 63)  # fit signed 64-bit for sqlite

def hamming(a, b):
    return bin((a + (1 << 63)) ^ (b + (1 << 63))).count("1")

def load_image(src, side):
    """Decode at roughly `side` px (JPEGs decode 2-8x faster this way). Returns (image, true width, true height)."""
    img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    w, h = img.size
    img.draft("RGB", (side, side))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if (img.width > img.height) != (w > h):  # EXIF rotated it 90 degrees
        w, h = h, w
    if max(img.size) > side * 1.5:
        img.thumbnail((side, side))
    return img, w, h

def thumb_bytes(img):
    t = img.copy(); t.thumbnail((THUMB, THUMB))
    buf = io.BytesIO(); t.save(buf, "JPEG", quality=85)
    return buf.getvalue()

def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def video_frames(source):
    """(timestamp, PIL image) every FRAME_EVERY seconds, minus look-alike frames.
    Decoded by OpenCV's bundled FFmpeg (no separate ffmpeg, no console windows), on the GPU's video decoder when
    there is one: a 20 s 4K clip takes ~3 s instead of ~30 s."""
    if cv2 is None:
        return
    got = False
    for x in _frames(source, hw=True):
        got = True
        yield x
    if not got:  # some GPU decoders refuse a codec (HEVC on older chips): decode on the CPU instead
        yield from _frames(source, hw=False)

def _frames(source, hw):
    cap = cv2.VideoCapture(str(source), cv2.CAP_FFMPEG,
                           [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY if hw else cv2.VIDEO_ACCELERATION_NONE])
    if not cap.isOpened():
        cap = cv2.VideoCapture(str(source))
    fps, n = cap.get(cv2.CAP_PROP_FPS) or 0, cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    dur = n / fps if fps > 0 and n > 0 else 0

    def raw():
        if 0 < dur <= 120:  # short clip: decode straight through (seeking costs more than it saves)
            step, i = max(1, round(fps * FRAME_EVERY)), 0
            while cap.grab():
                if i % step == 0:
                    ok, f = cap.retrieve()
                    if ok:
                        yield i / fps, f
                i += 1
        else:  # long or unknown length: seek to each sample point
            for i in range(MAX_FRAMES * 4):
                cap.set(cv2.CAP_PROP_POS_MSEC, i * FRAME_EVERY * 1000)
                ok, f = cap.read()
                if not ok:
                    return
                yield i * FRAME_EVERY, f
    try:
        last, kept = None, 0
        for t, frame in raw():
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            img.thumbnail((640, 640))
            d = dhash(img)
            if last is None or hamming(d, last) > 8:
                last, kept = d, kept + 1
                yield t, img
                if kept >= MAX_FRAMES:
                    return
    finally:
        cap.release()

# ---------------------------------------------------------------- indexing
# A "job" is one file to index, from any source:
#   {"path": unique key, "kind": "image"|"video", "mtime", "size",
#    local files: "local": Path            Drive files: "drive": metadata, "src", "name", "folder", "link", "w", "h"}
state = {"running": False, "done": 0, "total": 0, "current": "", "errors": 0, "last": None, "again": False, "source_errors": {}}
_index_cache = {"stamp": None}

def local_job(p, st, src):
    return {"path": str(p), "kind": "video" if p.suffix.lower() in VIDEO_EXT else "image",
            "mtime": st.st_mtime, "size": st.st_size, "local": p, "src": src}

def insert(con, job, ts, w, h, sha, dh, vecs, thumb):
    iid = con.execute("INSERT INTO items(path,ts,kind,mtime,size,w,h,sha,dhash,src,name,folder,link) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job["path"], ts, job["kind"], job["mtime"], job["size"], w, h, sha, dh,
                       job.get("src"), job.get("name"), job.get("folder"), job.get("link"))).lastrowid
    con.executemany("INSERT INTO vecs VALUES(?,?)", [(iid, v.astype(np.float16).tobytes()) for v in vecs])
    (THUMBS / f"{iid}.jpg").write_bytes(thumb)

def forget(con, path):
    for (iid,) in con.execute("SELECT id FROM items WHERE path=?", (str(path),)).fetchall():
        con.execute("DELETE FROM vecs WHERE item_id=?", (iid,))
        (THUMBS / f"{iid}.jpg").unlink(missing_ok=True)
    con.execute("DELETE FROM items WHERE path=?", (str(path),))

def prepare(job, known_sha):
    """Worker-thread part of indexing an image: hash, fetch/decode small, thumbnail. Skips exact duplicates."""
    if "drive" in job:  # Drive gives an MD5 for free, so duplicates are caught before downloading anything
        md5 = job["drive"].get("md5Checksum")
        sha = f"md5:{md5}" if md5 else None
        if sha in known_sha:
            return {"job": job, "sha": sha, "dup": known_sha[sha]}
        img, w, h = load_image(drive_image_bytes(job["drive"]), THUMB)
        w, h = job["w"] or w, job["h"] or h  # the thumbnail is small; Drive knows the real size
        return {"job": job, "sha": sha, "img": img, "w": w, "h": h, "dhash": dhash(img), "thumb": thumb_bytes(img)}
    sha = sha_of(job["local"])
    if sha in known_sha:
        return {"job": job, "sha": sha, "dup": known_sha[sha]}
    img, w, h = load_image(job["local"], THUMB)
    return {"job": job, "sha": sha, "img": img, "w": w, "h": h, "dhash": dhash(img), "thumb": thumb_bytes(img)}

def index_images(con, jobs, pool, known_sha):
    """Fetch/decode a batch on worker threads, fingerprint it in one model call, write it in one transaction."""
    ready = []
    for job, fut in [(j, pool.submit(prepare, j, known_sha)) for j in jobs]:
        try:
            ready.append(fut.result())
        except Exception as e:
            state["errors"] += 1
            print(f"skip {job['path']}: {e}", file=sys.stderr)
    fresh = [r for r in ready if "img" in r]
    vecs = describe([r["img"] for r in fresh], regions=True) if fresh else []
    for r, v in zip(fresh, vecs):
        r["vecs"] = v
    for r in ready:
        job = r["job"]
        if "dup" in r:  # exact copy of a file already indexed: reuse its fingerprints and preview
            src = con.execute("SELECT * FROM items WHERE id=?", (r["dup"],)).fetchone()
            if src is None:
                continue
            # read before forget(): the "copy" may be this same path re-saved with identical bytes
            v = [np.frombuffer(b, np.float16) for (b,) in con.execute("SELECT v FROM vecs WHERE item_id=?", (src["id"],))]
            thumb = (THUMBS / f"{src['id']}.jpg").read_bytes()
            forget(con, job["path"])
            insert(con, job, 0, src["w"], src["h"], r["sha"], src["dhash"], v, thumb)
        else:
            forget(con, job["path"])
            insert(con, job, 0, r["w"], r["h"], r["sha"], r["dhash"], r["vecs"], r["thumb"])
        if r["sha"]:
            known_sha[r["sha"]] = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()

def index_video(con, job):
    if "drive" in job:
        f = job["drive"]
        sha = f"md5:{f['md5Checksum']}" if f.get("md5Checksum") else None
        # streamed by OpenCV straight from Drive (the key is in the URL; no headers needed), never saved
        frames = list(video_frames(drive_media_url(f)))
    else:
        sha = sha_of(job["local"])
        frames = list(video_frames(job["local"]))
    if not frames:
        raise ValueError("no frames decoded")
    w, h = job.get("w") or frames[0][1].width, job.get("h") or frames[0][1].height
    forget(con, job["path"])
    for (ts, img), v in zip(frames, describe([f for _, f in frames], regions=False)):
        insert(con, job, ts, w, h, sha, dhash(img), v, thumb_bytes(img))
    con.commit()

SKIP_DIRS = {"node_modules", "appdata", "windows", "program files", "program files (x86)", "programdata", "__pycache__", "site-packages"}
_scan_lock = threading.Lock()

def request_scan():
    """Start a scan, or make the running one stop and start over (folders changed mid-scan)."""
    with _scan_lock:
        if state["running"]:
            state["again"] = True
        else:
            state["running"] = True
            threading.Thread(target=scan, daemon=True).start()

def list_source(src, label, known, seen, todo):
    """Add every new/changed image and video under one source (folder, network share or Drive link) to `todo`."""
    if parse_drive_link(src):
        for f, trail in drive_files(src, label or "Shared folder"):
            if state["again"]:
                return
            job = drive_job(f, trail, src)
            if job["path"] in seen:
                continue
            seen.add(job["path"])
            if known.get(job["path"]) != (job["mtime"], job["size"]):
                todo.append(job)
            state["current"] = f"Reading {label or 'Google Drive'} · {len(seen):,} found"
        return
    if not os.path.isdir(src):  # os.walk would silently find nothing and every file would be forgotten
        raise OSError("Folder not found. Is it on a drive that's unplugged?")
    for dirpath, dirnames, files in os.walk(src):
        if state["again"]:
            return
        # skip hidden, system and recycle-bin folders ($Recycle.Bin, .git, AppData...)
        dirnames[:] = [d for d in dirnames if d[0] not in ".$" and d.lower() not in SKIP_DIRS]
        for name in files:
            p = Path(dirpath, name)
            ext = p.suffix.lower()
            if (ext not in IMAGE_EXT and ext not in VIDEO_EXT) or str(p) in seen:  # overlapping folders
                continue
            seen.add(str(p))
            try:
                st = p.stat()
            except OSError:
                continue
            if ext in IMAGE_EXT and st.st_size < MIN_IMAGE_BYTES:
                continue
            if getattr(st, "st_file_attributes", 0) & 0x00441000:  # cloud placeholder: reading would download it
                continue
            if known.get(str(p)) != (st.st_mtime, st.st_size):
                todo.append(local_job(p, st, src))
        state["current"] = f"Finding files · {len(seen):,} found"

def _low_thread():
    """Indexing threads run below normal priority: the laptop (and riffle's own search) stay responsive."""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL

def scan():
    _low_thread()
    state.update(running=True, done=0, total=0, errors=0, again=False, current="Finding files", source_errors={})
    try:
        con = db()
        sources = con.execute("SELECT path, label FROM folders").fetchall()
        rows = con.execute("SELECT path, mtime, size, src FROM items WHERE ts=0").fetchall()
        known = {r["path"]: (r["mtime"], r["size"]) for r in rows}
        seen, todo = set(), []
        for src, label in sources:
            try:
                list_source(src, label, known, seen, todo)
            except (OSError, SourceError) as e:
                # Couldn't list it (offline, sharing changed...): keep what we already have rather than deleting it.
                state["source_errors"][src] = str(e)
                seen |= {r["path"] for r in rows if r["src"] == src}
            if state["again"]:
                return
        for gone in set(known) - seen:
            forget(con, gone)
        con.commit()
        _index_cache["stamp"] = None
        images = [j for j in todo if j["kind"] == "image"]
        videos = [j for j in todo if j["kind"] == "video"]  # after images: search is useful sooner
        state["total"] = len(todo)
        if todo:
            state["current"] = "Starting the AI model"
            backend()
        known_sha = {r["sha"]: r["id"] for r in con.execute("SELECT sha, id FROM items WHERE kind='image' AND sha IS NOT NULL")}
        with ThreadPoolExecutor(8, initializer=_low_thread) as pool:  # decode is CPU, Drive fetches are network: both overlap the model
            for i in range(0, len(images), BATCH):
                if state["again"]:
                    break
                jobs = images[i:i + BATCH]
                state["current"] = jobs[-1].get("name") or Path(jobs[-1]["path"]).name
                try:
                    index_images(con, jobs, pool, known_sha)
                except Exception as e:
                    state["errors"] += len(jobs)
                    print(f"batch failed: {e}", file=sys.stderr)
                    con.rollback()
                state["done"] += len(jobs)
        for job in videos:
            if state["again"]:
                break
            state["current"] = job.get("name") or Path(job["path"]).name
            try:
                index_video(con, job)
            except Exception as e:
                state["errors"] += 1
                print(f"skip {job['path']}: {e}", file=sys.stderr)
                con.rollback()
            state["done"] += 1
        con.close()
    finally:
        _index_cache["stamp"] = None
        with _scan_lock:
            if state["again"]:
                threading.Thread(target=scan, daemon=True).start()
            else:
                state.update(running=False, current="", last=time.time())

# ---------------------------------------------------------------- search

def matrix():
    """All stored vectors as one float32 matrix + owning item id per row. Reloaded after each scan."""
    if _index_cache["stamp"] is None:
        with db() as con:
            rows = con.execute("SELECT item_id, v FROM vecs").fetchall()
        # ponytail: brute force in RAM, fine to ~1M vectors (~1.5 GB); swap for FAISS past that
        M = np.frombuffer(b"".join(r[1] for r in rows), np.float16).reshape(-1, 384).astype(np.float32) if rows else np.zeros((0, 384), np.float32)
        owner = np.array([r[0] for r in rows], np.int64)
        _index_cache.update(stamp=time.time(), M=M, owner=owner)
    return _index_cache["M"], _index_cache["owner"]

def sift_inliers(q_feats, cand_img):
    sift = cv2.SIFT_create(nfeatures=1500)
    kp2, d2 = sift.detectAndCompute(cv2.cvtColor(np.asarray(cand_img), cv2.COLOR_RGB2GRAY), None)
    best = 0
    for kp1, d1 in q_feats:
        if d1 is None or d2 is None or len(kp1) < 8 or len(kp2) < 8:
            continue
        pairs = cv2.BFMatcher().knnMatch(d1, d2, k=2)
        good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < 8:
            continue
        src = np.float32([kp1[m.queryIdx].pt for m in good])
        dst = np.float32([kp2[m.trainIdx].pt for m in good])
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
        best = max(best, int(mask.sum()) if mask is not None else 0)
    return best

_verify_pool = ThreadPoolExecutor(4)  # OpenCV releases the GIL, so candidates verify in parallel

def search_image(qimg, q_sha=None, limit=60):
    M, owner = matrix()
    if not len(M):
        return []
    q = np.concatenate(describe([qimg, ImageOps.mirror(qimg)], regions=False))
    sims = (M @ q.T).max(axis=1)
    best = np.full(owner.max() + 1, -1.0, np.float32)
    np.maximum.at(best, owner, sims)
    ids = [int(i) for i in np.argsort(-best)[: limit * 3] if best[i] > 0.25]
    if not ids:
        return []
    with db() as con:
        rows = {r["id"]: dict(r) for r in con.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)}
    qd = dhash(qimg)
    # one entry per file: best-scoring frame for videos
    results, seen = [], set()
    for i in ids:
        r = rows[i]
        if r["path"] in seen:
            continue
        seen.add(r["path"])
        r.update(score=float(best[i]), dist=hamming(qd, r["dhash"]), inliers=0, same_file=bool(q_sha and r["sha"] == q_sha))
        results.append(r)
        if len(results) >= limit:
            break
    top_inliers = 0
    if cv2 is not None:
        sift = cv2.SIFT_create(nfeatures=1500)
        qs = qimg.copy(); qs.thumbnail((800, 800))
        q_feats = [sift.detectAndCompute(cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY), None) for im in (qs, ImageOps.mirror(qs))]

        def verify(r):  # thumbnails are enough for SIFT and keep this fast (and work for cloud sources later)
            try:
                r["inliers"] = sift_inliers(q_feats, Image.open(THUMBS / f"{r['id']}.jpg").convert("RGB"))
            except Exception:
                pass
        list(_verify_pool.map(verify, results[:VERIFY_TOP]))
        top_inliers = max(r["inliers"] for r in results)
    for r in results:
        # Files built on the same template share logos and type, so they pass a fixed bar too.
        # The same picture scores close to the best match; template siblings score far below it.
        proven = r["inliers"] >= max(MIN_INLIERS, 0.35 * top_inliers)
        r["match"] = "verified" if r["same_file"] or proven or r["dist"] <= 6 else ("similar" if r["score"] > 0.45 else "loose")
    return rank(results)

def rank(results):
    verified = [r for r in results if r["match"] == "verified"]
    rest = [r for r in results if r["match"] != "verified"]
    # The original is the biggest, most "master"-like file among proven matches; ties go to the oldest.
    master = lambda r: (r["kind"] == "image", r["w"] * r["h"], EXT_RANK.get(Path(r["path"]).suffix.lower(), 1), -r["mtime"])
    verified.sort(key=master, reverse=True)
    if verified:
        verified[0]["original"] = True
    rest.sort(key=lambda r: -r["score"])
    return verified + rest

def search_bytes(data, content_type):
    if content_type.startswith("video/"):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(data)
        try:
            frames = [img for _, img in video_frames(f.name)][:12] or []
        finally:
            os.unlink(f.name)
        merged = {}
        for img in frames:
            for r in search_image(img, limit=30):
                old = merged.get(r["path"])
                if not old or (r["inliers"], r["score"]) > (old["inliers"], old["score"]):
                    merged[r["path"]] = r
        for r in merged.values():
            r.pop("original", None)
        return rank(list(merged.values())), (frames[len(frames) // 2] if frames else None)
    img, w, h = load_image(data, 1024)
    results = search_image(img, hashlib.sha256(data).hexdigest())
    img.info["true_size"] = (w, h)
    return results, img

def public(r):
    p = Path(r["path"])
    return {"id": r["id"], "path": r.get("link") or r["path"], "name": r.get("name") or p.name, "dir": r.get("folder") or str(p.parent),
            "kind": r["kind"], "ts": r["ts"], "w": r["w"], "h": r["h"], "size": r["size"], "mtime": r["mtime"],
            "score": round(r["score"], 3), "inliers": r["inliers"], "match": r["match"],
            "original": r.get("original", False), "same_file": r["same_file"], "link": r.get("link")}

# ---------------------------------------------------------------- riffle api

def start(data_dir=None, model_cache=None):
    """Open (or create) the visual index and pick up changes since last run. Cheap: no model load unless files changed."""
    global DATA, THUMBS, MODEL_CACHE
    if data_dir:
        DATA, THUMBS = Path(data_dir), Path(data_dir) / "thumbs"
    if model_cache:
        MODEL_CACHE = Path(model_cache)
    init_db()
    request_scan()

def status():
    with db() as con:
        images, videos = con.execute("SELECT COUNT(DISTINCT CASE WHEN kind='image' THEN path END), "
                                     "COUNT(DISTINCT CASE WHEN kind='video' THEN path END) FROM items").fetchone()
        folders = [{"path": f, "label": label, "kind": "drive" if parse_drive_link(f) else "local",
                    "count": con.execute("SELECT COUNT(DISTINCT path) FROM items WHERE src=?", (f,)).fetchone()[0],
                    "error": state["source_errors"].get(f)}
                   for f, label in con.execute("SELECT path, label FROM folders ORDER BY path").fetchall()]
    return {"available": True, "images": images, "videos": videos, "folders": folders,
            "index": {k: v for k, v in state.items() if k != "source_errors"},
            "engine": _backend["name"] if _backend["run"] else "asleep (loads on next search)",
            "verify": cv2 is not None, "cv2_error": CV2_ERROR, "google_key": bool(config().get("google_api_key")),
            "suggested": [f for f in suggested_folders() if f not in {x["path"] for x in folders}]}

def suggested_folders():
    """Where a designer's images usually are: the standard Windows libraries that exist on this machine."""
    home = Path.home()
    return [str(p) for p in (home / "Pictures", home / "Downloads", home / "Desktop", home / "Videos",
                             home / "OneDrive" / "Pictures") if p.is_dir()]

def is_network(p):
    """A NAS / shared folder (\\\\server\\share or a mapped drive). Riffle's file watcher can't see changes there."""
    s = str(p)
    if s.startswith(("\\\\", "//")):
        return True
    if os.name == "nt" and len(s) > 1 and s[1] == ":":
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(s[:2] + "\\") == 4  # DRIVE_REMOTE
    return False

def add_folder(path):
    """Add a local folder, a network share or a Google Drive shared-folder link. Raises SourceError with a readable message."""
    path = str(path).strip().strip('"')
    if not path:
        return
    drive = parse_drive_link(path)
    if drive:
        src = drive_source(*drive)
        label = drive_folder_name(src)
        with db() as con:
            con.execute("INSERT OR REPLACE INTO folders(path, label) VALUES(?, ?)", (src, label))
        request_scan()
        return
    if "://" in path:
        raise SourceError("Only Google Drive folder links are supported. Paste a link like drive.google.com/drive/folders/…, "
                          "or a folder path such as D:\\Shoots or \\\\NAS\\Photos.")
    p = Path(path)
    if not p.is_dir():
        raise SourceError(f"Folder not found: {path}")
    # a share root (\\NAS\Photos) resolves to itself too, but it is a folder, not a whole drive
    if p.resolve().parent == p.resolve() and not is_network(p):
        raise SourceError(f"{path} is a whole drive. Pick the folders where your images live")
    with db() as con:
        con.execute("INSERT OR IGNORE INTO folders(path) VALUES(?)", (str(p),))
    request_scan()

def set_google_key(key):
    key = (key or "").strip()
    if key and not re.fullmatch(r"[\w-]{20,}", key):
        raise SourceError("That doesn't look like a Google API key (they start with AIza and have no spaces).")
    save_config(google_api_key=key)
    if key:
        request_scan()  # retry Drive folders that failed for want of a key

def has_unwatched():
    """Drive links and network shares change without Riffle's watcher seeing it, so they need a timed rescan."""
    return any(parse_drive_link(f) or is_network(f) for f in _folder_cache())

def remove_folder(path):
    with db() as con:
        con.execute("DELETE FROM folders WHERE path=?", (path,))
        for (p,) in con.execute("SELECT DISTINCT path FROM items WHERE src=?", (path,)).fetchall():
            forget(con, p)
    _index_cache["stamp"] = None
    request_scan()  # stop a scan still walking the removed folder

def covers(path):
    """True if a changed file is an image/video inside a chosen folder (so riffle's watcher should trigger a rescan)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXT and ext not in VIDEO_EXT:
        return False
    lp = path.lower()
    return any(lp.startswith(f.lower().rstrip("\\/") + os.sep) for f in _folder_cache())

_folders = {"at": 0, "list": []}
def _folder_cache():
    if time.time() - _folders["at"] > 5:  # covers() runs for every file event: don't open the db each time
        with db() as con:
            _folders.update(at=time.time(), list=[r[0] for r in con.execute("SELECT path FROM folders")])
    return _folders["list"]

def pick_folder():
    import tkinter, tkinter.filedialog
    root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
    path = tkinter.filedialog.askdirectory(title="Add a folder to Riffle visual search")
    root.destroy()
    return path

# ---------------------------------------------------------------- self-check

def use_data_dir(path):
    global DATA, THUMBS
    DATA, THUMBS = Path(path), Path(path) / "thumbs"
    _index_cache["stamp"] = None
    init_db()

def selftest():
    """Index a synthetic folder, then search with an edited crop; the untouched master must come back as the original."""
    tmp = Path(tempfile.mkdtemp())
    use_data_dir(tmp / "data")
    lib = tmp / "lib"; lib.mkdir()
    rng = np.random.default_rng(1)
    for i in range(6):  # distinct "photos": random blobs on gradients
        a = np.zeros((900, 1200, 3), np.uint8)
        a[:] = rng.integers(0, 255, 3)
        for _ in range(40):
            x, y, r = rng.integers(0, 1200), rng.integers(0, 900), rng.integers(20, 160)
            yy, xx = np.ogrid[:900, :1200]
            a[(xx - x) ** 2 + (yy - y) ** 2 < r * r] = rng.integers(0, 255, 3)
        Image.fromarray(a).save(lib / f"shot_{i}.png")
    master = Image.open(lib / "shot_3.png")
    master.resize((600, 450)).save(lib / "shot_3_web.jpg", quality=70)  # smaller export of the same shot
    shutil.copy(lib / "shot_1.png", lib / "shot_1_copy.png")  # exact duplicate: must reuse, not re-fingerprint
    with db() as con:
        con.execute("INSERT INTO folders(path) VALUES(?)", (str(lib),))
    scan()
    assert state["errors"] == 0, state
    with db() as con:
        a, b = (con.execute("SELECT w, h, dhash FROM items WHERE path LIKE ?", (f"%{n}",)).fetchone() for n in ("shot_1.png", "shot_1_copy.png"))
        assert tuple(a) == tuple(b) == (1200, 900, a[2]), (tuple(a), tuple(b))  # true size kept, duplicate copied
    edit = ImageOps.mirror(master.crop((300, 200, 1000, 760))).point(lambda v: min(255, int(v * 1.15)))  # crop + flip + grade
    buf = io.BytesIO(); edit.save(buf, "JPEG", quality=80)
    results, _ = search_bytes(buf.getvalue(), "image/jpeg")
    top = results[0]
    print("top:", Path(top["path"]).name, top["match"], "inliers", top["inliers"], "score", round(top["score"], 3), "engine", _backend["name"])
    names = [Path(r["path"]).name for r in results[:3]]
    if cv2:
        assert names[0] == "shot_3.png" and top.get("original"), names
    else:  # without verification we can only check the right shot ranks first
        assert names[0].startswith("shot_3"), names
    try:
        add_folder("C:\\"); raise AssertionError("a whole local drive must be refused")
    except SourceError:
        pass
    selftest_drive(rng)
    print("selftest ok" + ("" if cv2 else " (no OpenCV: verification skipped)"))

def selftest_drive(rng):
    """Shared Drive folder (with a subfolder) indexed through a fake Google API, then found by search."""
    global http_get
    def blob_image(seed):
        r = np.random.default_rng(seed)
        a = np.full((900, 1200, 3), r.integers(0, 255, 3), np.uint8)
        yy, xx = np.ogrid[:900, :1200]
        for _ in range(40):
            x, y, s = r.integers(0, 1200), r.integers(0, 900), r.integers(20, 160)
            a[(xx - x) ** 2 + (yy - y) ** 2 < s * s] = r.integers(0, 255, 3)
        return Image.fromarray(a)
    pics = {"D1": blob_image(101), "D2": blob_image(202)}
    folder = "application/vnd.google-apps.folder"
    def file(fid, name):
        return {"id": fid, "name": name, "mimeType": "image/jpeg", "modifiedTime": "2026-03-01T10:00:00.000Z", "size": "4000000",
                "md5Checksum": fid * 4, "thumbnailLink": f"https://lh3.test/{fid}=s220", "imageMediaMetadata": {"width": 4800, "height": 3600}}
    listing = {"1RootFolderAbc": [{"id": "1SubFolderXyz", "name": "Edits", "mimeType": folder}, file("D1", "hero.jpg")], "1SubFolderXyz": [file("D2", "detail.jpg")]}
    calls = []
    def fake(url, params=None, headers=None, as_json=True):
        calls.append(url)
        if url.startswith("https://lh3.test/"):
            assert url.endswith("=s800"), url  # asks Drive for a bigger thumbnail than the default 220 px
            buf = io.BytesIO(); pics[url.split("/")[-1].split("=")[0]].resize((800, 600)).save(buf, "JPEG"); return buf.getvalue()
        if url == f"{DRIVE_API}/1RootFolderAbc":
            return {"name": "Shoots", "mimeType": folder}
        parent = re.search(r"'(\w+)' in parents", params["q"]).group(1)
        return {"files": listing[parent]}
    real, http_get = http_get, fake
    try:
        src = drive_source("1RootFolderAbc", None)
        try:
            drive_folder_name(src); raise AssertionError("should ask for an API key first")
        except SourceError as e:
            assert e.need_key
        save_config(google_api_key="AIzaTEST")
        add_folder("https://drive.google.com/drive/folders/1RootFolderAbc?usp=sharing")
        while state["running"]:  # add_folder started a background scan; let it finish
            time.sleep(0.1)
        scan()
        assert state["errors"] == 0 and not state["source_errors"], state
        edit = pics["D2"].crop((200, 150, 1000, 750))
        buf = io.BytesIO(); edit.save(buf, "JPEG", quality=80)
        top = public(search_bytes(buf.getvalue(), "image/jpeg")[0][0])
        print("drive top:", top["name"], top["dir"], top["w"], top["h"], top["match"])
        assert top["name"] == "detail.jpg" and top["link"].endswith("/file/d/D2/view"), top
        assert top["dir"] == "Google Drive › Shoots › Edits" and (top["w"], top["h"]) == (4800, 3600), top
        n = len(calls); scan()
        assert not any("lh3.test" in c for c in calls[n:]), "unchanged Drive files must not be fetched again"
    finally:
        http_get = real

if __name__ == "__main__":
    selftest()
