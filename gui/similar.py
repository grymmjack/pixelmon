"""Find similar pictures: a small index of the images in chosen folders, searched by a picture.

soundmon-gui's 🔎 Find, for images. Each picture gets a cheap fingerprint (Pillow only, no GPU, no model):

  - a 64-bit difference hash (dHash) of a 9×8 grey thumbnail: the layout / shape, robust to scaling and
    small colour changes;
  - a 4×4×4 colour histogram of a 32×32 thumbnail (transparent pixels skipped): the palette and how much of
    each colour there is — what matters most for pixel art;
  - the aspect ratio.

The score is 45 % shape, 45 % colours and 10 % proportions. The index lives in one JSON file and is updated
incrementally (a file whose size and mtime haven't changed keeps its fingerprint).
"""
import json
import os
import threading
import time

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
SKIP_DIRS = {"node_modules", "__pycache__", "_preview", "backups", "_analysis", "site-packages", "venv", ".venv"}
MAX_FILES = 60000
MAX_BYTES = 60 * 1024 * 1024


def fingerprint(src):
    """{"d": dHash hex, "c": [64 colour-bin counts over 1024 pixels], "w", "h"} of a path or file object."""
    from PIL import Image
    with Image.open(src) as im:
        w, h = im.size
        try:
            im.draft("RGB", (128, 128))                 # JPEGs decode small: much faster for big photos
        except (AttributeError, ValueError, OSError):
            pass
        im.seek(0)
        im = im.convert("RGBA")
    if max(im.size) > 256:
        im.thumbnail((256, 256), Image.BOX)
    bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
    bg.alpha_composite(im)
    grey = bg.convert("L").resize((9, 8), Image.BOX)
    px, bits = list(grey.getdata()), 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] < px[y * 9 + x + 1])
    small = im.resize((32, 32), Image.BOX)
    hist, n = [0] * 64, 0
    for r, g, b, a in small.getdata():
        if a < 16:
            continue
        hist[(r >> 6) * 16 + (g >> 6) * 4 + (b >> 6)] += 1
        n += 1
    if n:                                                # normalise to 1024 so pictures with alpha compare fairly
        hist = [round(v * 1024 / n) for v in hist]
    return {"d": f"{bits:016x}", "c": hist, "w": w, "h": h}


def score(a, b):
    """0..1 similarity of two fingerprints, and which aspects matched."""
    shape = 1 - bin(int(a["d"], 16) ^ int(b["d"], 16)).count("1") / 64
    colours = sum(min(x, y) for x, y in zip(a["c"], b["c"])) / 1024
    ra, rb = a["w"] / max(1, a["h"]), b["w"] / max(1, b["h"])
    prop = min(ra, rb) / max(ra, rb)
    s = 0.45 * shape + 0.45 * colours + 0.10 * prop
    why = [n for n, v in (("shape", shape >= 0.8), ("colours", colours >= 0.7), ("proportions", prop >= 0.95)) if v]
    return s, why


def walk(folders):
    """Every picture under the folders (hidden folders and caches skipped), at most MAX_FILES."""
    seen = set()
    for top in folders:
        for root, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for f in sorted(files):
                if f.lower().endswith(IMG_EXT) and not f.startswith("."):
                    p = os.path.join(root, f)
                    if p not in seen:
                        seen.add(p)
                        yield p
                        if len(seen) >= MAX_FILES:
                            return


class Index:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.job = {"running": False, "done": 0, "total": 0, "added": 0, "error": None, "started": None, "finished": None}
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data.get("files"), dict):
            self.data = {"folders": [], "files": {}, "updated": None}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with self.lock:
            blob = json.dumps(self.data)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
        os.replace(tmp, self.path)

    def status(self):
        with self.lock:
            return {"count": len(self.data["files"]), "folders": list(self.data["folders"]), "updated": self.data.get("updated"),
                    "job": dict(self.job)}

    def start(self, folders):
        """(Re)index these folders in the background; status() reports progress."""
        with self.lock:
            if self.job["running"]:
                raise ValueError("indexing is already running")
            self.job = {"running": True, "done": 0, "total": 0, "added": 0, "error": None, "started": time.time(), "finished": None}
        threading.Thread(target=self._run, args=(list(folders),), daemon=True).start()

    def _run(self, folders):
        try:
            paths = list(walk(folders))
            with self.lock:
                self.job["total"] = len(paths)
                old = self.data["files"]
            new = {}
            for i, p in enumerate(paths):
                try:
                    st = os.stat(p)
                    prev = old.get(p)
                    if prev and prev.get("m") == int(st.st_mtime) and prev.get("s") == st.st_size:
                        new[p] = prev
                    elif st.st_size <= MAX_BYTES:
                        fp = fingerprint(p)
                        fp.update(m=int(st.st_mtime), s=st.st_size)
                        new[p] = fp
                        with self.lock:
                            self.job["added"] += 1
                except Exception:                        # unreadable / not really a picture: skip it
                    pass
                if i % 50 == 0:
                    with self.lock:
                        self.job["done"] = i
            with self.lock:
                self.data = {"folders": folders, "files": new, "updated": time.time()}
            self.save()
        except Exception as e:
            with self.lock:
                self.job["error"] = str(e)[:300]
        finally:
            with self.lock:
                self.job.update(running=False, finished=time.time(), done=self.job["total"])

    def has(self, path):
        with self.lock:
            return path in self.data["files"]

    def search(self, fp, k=48, within=None, exclude=None):
        """The k most similar indexed pictures to a fingerprint: [{path, score, why, w, h, size}]."""
        with self.lock:
            items = list(self.data["files"].items())
        within = os.path.join(within, "") if within else None
        out = []
        for p, f in items:
            if (within and not p.startswith(within)) or p == exclude:
                continue
            s, why = score(fp, f)
            out.append((s, p, why, f))
        out.sort(key=lambda t: -t[0])
        return [{"path": p, "score": round(s, 4), "why": why, "w": f["w"], "h": f["h"], "size": f.get("s")}
                for s, p, why, f in out[:k]]
