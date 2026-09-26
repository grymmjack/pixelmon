#!/usr/bin/env python3
"""pixelmon-gui — a local web front end for the pixelmon CLI.

Serves a single-page UI, queues render jobs, and runs them one at a time by
shelling out to bin/pixelmon (always --server rtx --no-open). Each job renders
into its own folder under the gallery dir with a job.json recording the exact
settings and command, so the gallery can reload / re-run anything.

Standard library only. usage: pixelmon-gui [--port 8190] [--lan] [--gallery DIR]
"""
import argparse
import ast
import glob
import http.server
import importlib.util
import json
import os
import queue
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
PIXELMON = os.path.join(REPO, "bin", "pixelmon")
RENDER_SERVER = "rtx"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DONE_LINE = re.compile(r"✅.*?seed=(\d+)\s+->\s+(\S.*)$")
PROMPT_LINE = re.compile(r"(📝 positive|🚫 negative|🧩 lora):\s*(.*)$")

# ---------------------------------------------------------------------------
# Metadata: palettes, styles, dither methods, LoRAs, presets
# ---------------------------------------------------------------------------
def load_palettes():
    sys.path.insert(0, os.path.join(REPO, "custom_nodes", "pixelart_palette"))
    try:
        import palettes
        return {k: list(v) for k, v in palettes.ALL_PALETTES.items()}
    except Exception:
        return {}


def load_styles():
    try:
        with open(os.path.join(REPO, "styles.json"), encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except Exception:
        return {}


def pixelmon_constant(name, default):
    """Read a literal constant straight out of pixelmon.py so the two never drift."""
    try:
        tree = ast.parse(open(os.path.join(REPO, "pixelmon.py"), encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
    except Exception:
        pass
    return default


def load_presets():
    try:
        with open(os.path.join(HERE, "presets.json"), encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except Exception:
        return {}


def full_prompt(p):
    """The exact prompts pixelmon will send for these form params, computed by
    pixelmon.py's own final_prompts()/resolve_styles() (reloaded each call, so
    edits to pixelmon.py or styles.json show up without restarting the GUI)."""
    spec = importlib.util.spec_from_file_location("pixelmon_cli", os.path.join(REPO, "pixelmon.py"))
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    art = bool(p.get("art"))
    style_add, style_neg = pm.resolve_styles(",".join(p.get("styles") or []))
    a = types.SimpleNamespace(art=art, no_sprite_suffix=bool(p.get("no_sprite_suffix")),
                              style_add=style_add, style_neg=style_neg,
                              negative=str(p.get("negative") or "").strip()
                              or (pm.ART_NEGATIVE if art else pm.PIXEL_NEGATIVE))
    pos, neg = pm.final_prompts(a, str(p.get("prompt") or "").strip())
    lora = str(p.get("lora") or "")
    if art or lora == "(none)":
        lora = "none"
    elif lora:
        lora = f"{lora} @ {float(p.get('lora_strength') or 1):g}"
    if p.get("fast"):
        lora += " + lcm-lora-sdxl.safetensors"
    return {"positive": pos, "negative": neg, "lora": lora}


def server_url():
    try:
        with open(os.path.join(REPO, "servers.json"), encoding="utf-8") as f:
            return json.load(f).get(RENDER_SERVER)
    except Exception:
        return None


def load_loras():
    """LoRA filenames available on the render server (falls back to local ComfyUI)."""
    url = server_url()
    if url:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models/loras", timeout=4) as r:
                return sorted(json.load(r)), True
        except Exception:
            pass
    local = os.path.expanduser("~/ComfyUI/models/loras")
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(local, "*.safetensors"))), False


def server_up():
    url = server_url()
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/system_stats", timeout=3):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def build_argv(p):
    """Turn validated form params into a pixelmon argv (no shell involved)."""
    def num(key, typ, lo, hi, default=None):
        v = p.get(key, default)
        if v in (None, ""):
            return None
        v = typ(v)
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return v

    def dims(key):
        v = str(p.get(key) or "").lower().strip()
        if not v:
            return None
        if not re.fullmatch(r"\d{1,4}(x\d{1,4})?", v):
            raise ValueError(f"bad {key} {v!r}; use N or WxH")
        return v

    prompt = str(p.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is empty")
    argv = [PIXELMON, prompt, "--server", RENDER_SERVER, "--no-open", "--show-prompt"]
    art = bool(p.get("art"))
    if art:
        argv.append("--art")
    lora = str(p.get("lora") or "")
    if lora == "(none)":
        argv.append("--no-lora")
    elif lora:
        argv += ["--lora", lora]
        s = num("lora_strength", float, 0.0, 2.0)
        if s is not None:
            argv += ["--lora-strength", f"{s:g}"]
    if p.get("negative"):
        argv += ["--negative", str(p["negative"])]
    styles = [s for s in (p.get("styles") or []) if re.fullmatch(r"[\w-]+", s)]
    if styles:
        argv += ["--style", ",".join(styles)]
    out, size = dims("out"), dims("size")
    if out:
        argv += ["--out", out]
    if size:
        argv += ["--size", size]
    if not art:
        pal = str(p.get("palette") or "none")
        argv += ["--palette", pal]
        dither = str(p.get("dither") or "none")
        if dither != "none":
            argv += ["--dither", dither]
            a = num("dither_amount", float, 0.0, 1.0)
            if a is not None:
                argv += ["--dither-amount", f"{a:g}"]
        d = num("despeckle", int, 0, 64)
        if d is not None:
            argv += ["--despeckle", str(d)]
        if p.get("snap_pixels"):
            argv.append("--snap-pixels")
        if p.get("transparent"):
            argv.append("--transparent")
        if p.get("no_sprite_suffix"):
            argv.append("--no-sprite-suffix")
    if p.get("fast"):
        argv.append("--fast")
    for key, flag, typ, lo, hi in (("steps", "--steps", int, 1, 150), ("cfg", "--cfg", float, 0.0, 30.0)):
        v = num(key, typ, lo, hi)
        if v is not None:
            argv += [flag, f"{v:g}" if typ is float else str(v)]
    seed = num("seed", int, -1, 2**31 - 1, -1)
    if seed is not None and seed >= 0:
        argv += ["--seed", str(seed)]
    n = num("n", int, 1, 32, 1)
    if n and n > 1:
        argv += ["-n", str(n)]
    name = re.sub(r"[^\w-]+", "-", str(p.get("name") or "")).strip("-")[:48]
    if name:
        argv += ["--name", name]
    return argv


class JobQueue:
    def __init__(self, gallery):
        self.gallery = gallery
        self.jobs = {}          # id -> job dict (this session)
        self.order = []
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.proc = None
        self.current = None
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, params, group=None, label=None):
        argv = build_argv(params)   # validate before queueing
        jid = time.strftime("%Y%m%d-%H%M%S-") + f"{random.randrange(16**4):04x}"
        job = {"id": jid, "params": params, "argv": argv, "command": shlex.join(["pixelmon"] + argv[1:]),
               "status": "queued", "log": [], "outputs": [], "full_prompt": {}, "group": group, "label": label,
               "total": int(params.get("n") or 1),
               "created": time.time(), "started": None, "finished": None, "dir": jid}
        with self.lock:
            self.jobs[jid] = job
            self.order.append(jid)
        self.q.put(jid)
        return job

    def cancel(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return False
            if job["status"] == "queued":
                job["status"] = "cancelled"
            elif job["status"] == "running" and self.current == jid and self.proc:
                job["status"] = "cancelled"
                self.proc.terminate()
            return True

    def snapshot(self):
        with self.lock:
            return [dict(self.jobs[j], log=self.jobs[j]["log"][-40:]) for j in self.order[-60:]]

    def _save(self, job):
        d = os.path.join(self.gallery, job["dir"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in job.items() if k != "log"}, f, indent=1)

    def _worker(self):
        while True:
            jid = self.q.get()
            with self.lock:
                job = self.jobs[jid]
                if job["status"] == "cancelled":
                    continue
                job["status"], job["started"] = "running", time.time()
                self.current = jid
            d = os.path.join(self.gallery, job["dir"])
            os.makedirs(d, exist_ok=True)
            self._save(job)
            argv = job["argv"] + ["--output-to", d]
            try:
                self.proc = subprocess.Popen(argv, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             text=True, bufsize=1,
                                             env=dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1"))
                for line in self.proc.stdout:
                    line = ANSI.sub("", line.rstrip())
                    if not line.strip():
                        continue
                    with self.lock:
                        job["log"].append(line)
                        pm = PROMPT_LINE.search(line)
                        if pm:
                            key = pm.group(1).split()[-1]          # positive / negative / lora
                            job["full_prompt"][key] = pm.group(2).strip()
                        m = DONE_LINE.search(line)
                        if m:
                            job["outputs"].append({"seed": int(m.group(1)), "file": os.path.basename(m.group(2).strip())})
                rc = self.proc.wait()
            except Exception as e:
                rc = -1
                job["log"].append(f"GUI error: {e}")
            with self.lock:
                # trust the folder over log parsing (covers moved/renamed files)
                found = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.gif")))
                known = {o["file"] for o in job["outputs"]}
                for f in found:
                    if f not in known:
                        m = re.search(r"_s(\d+)_", f)
                        job["outputs"].append({"seed": int(m.group(1)) if m else None, "file": f})
                job["outputs"] = [o for o in job["outputs"] if o["file"] in found]
                if job["status"] != "cancelled":
                    job["status"] = "done" if rc == 0 and job["outputs"] else "failed"
                job["finished"] = time.time()
                self.current, self.proc = None, None
            self._save(job)


def gallery_items(gallery, limit=300):
    items = []
    for jf in sorted(glob.glob(os.path.join(gallery, "*", "job.json")), reverse=True)[:limit]:
        try:
            with open(jf, encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue
        if job.get("outputs"):
            items.append(job)
    return items


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    jobs = None
    gallery = None

    def log_message(self, fmt, *args):   # keep the terminal quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        if u.path == "/api/meta":
            loras, live = load_loras()
            return self._json({"palettes": load_palettes(), "styles": load_styles(),
                               "dithers": pixelmon_constant("DITHER_METHODS", ["none", "floyd-steinberg"]),
                               "default_negative": pixelmon_constant("PIXEL_NEGATIVE", ""),
                               "presets": load_presets(),
                               "loras": loras, "loras_live": live, "server": RENDER_SERVER,
                               "server_up": server_up()})
        if u.path == "/api/jobs":
            return self._json({"jobs": self.jobs.snapshot()})
        if u.path == "/api/gallery":
            return self._json({"items": gallery_items(self.gallery)})
        if u.path.startswith("/files/"):
            rel = urllib.parse.unquote(u.path[len("/files/"):])
            full = os.path.realpath(os.path.join(self.gallery, rel))
            if not full.startswith(os.path.realpath(self.gallery) + os.sep):
                return self._json({"error": "forbidden"}, 403)
            ctype = "image/gif" if full.endswith(".gif") else "image/png"
            return self._file(full, ctype)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        try:
            if u.path == "/api/prompt":
                return self._json(full_prompt(body))
            if u.path == "/api/render":
                return self._json({"jobs": [self.jobs.add(body)["id"]]})
            if u.path == "/api/sweep":
                # same settings + one fixed seed, varying a single field across values
                base, field, values = body.get("params") or {}, body.get("field"), body.get("values") or []
                if field not in ("lora_strength", "dither", "dither_amount", "palette", "styles", "lora",
                                 "steps", "cfg", "despeckle", "out"):
                    raise ValueError(f"can't sweep {field!r}")
                if not values:
                    raise ValueError("no sweep values")
                base = dict(base)
                if int(base.get("seed", -1) if base.get("seed") not in (None, "") else -1) < 0:
                    base["seed"] = random.randint(0, 2**31 - 1)
                base["n"] = 1
                group = f"sweep-{int(time.time())}"
                for v in values[:24]:
                    p = dict(base)
                    p[field] = [s for s in str(v).split("+") if s] if field == "styles" else v
                    self.jobs.add(p, group=group, label=f"{field}={v}")
                return self._json({"group": group, "seed": base["seed"]})
            if u.path.startswith("/api/cancel/"):
                return self._json({"ok": self.jobs.cancel(u.path.rsplit("/", 1)[1])})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(prog="pixelmon-gui", description="Web GUI for pixelmon (renders on the rtx box).")
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces so other devices on the LAN can use it")
    ap.add_argument("--gallery", default=os.path.expanduser("~/pixelmon-gallery/gui"),
                    help="where renders + job.json records go (default ~/pixelmon-gallery/gui)")
    args = ap.parse_args()
    os.makedirs(args.gallery, exist_ok=True)
    Handler.gallery = os.path.realpath(args.gallery)
    Handler.jobs = JobQueue(Handler.gallery)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    srv = http.server.ThreadingHTTPServer((host, args.port), Handler)
    print(f"pixelmon-gui on http://{'<this-ip>' if args.lan else '127.0.0.1'}:{args.port}  "
          f"(renders on '{RENDER_SERVER}', gallery {args.gallery})  Ctrl-C to quit")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
