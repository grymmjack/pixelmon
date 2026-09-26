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
import io
import json
import mimetypes
import os
import queue
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
PIXELMON = os.path.join(REPO, "bin", "pixelmon")
RENDER_SERVER = "rtx"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DONE_LINE = re.compile(r"✅.*?seed=(\d+)\s+->\s+(\S.*)$")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
STEER_WEIGHT_TYPES = ["style transfer", "strong style transfer", "style transfer precise", "composition",
                      "composition precise", "style and composition", "linear", "ease in", "ease out"]
STEER_COMBINE = ["concat", "average", "norm average", "add", "subtract"]
REFS = os.path.expanduser("~/pixelmon-refs")      # steering library: one folder per collection
PRESETS = os.path.expanduser("~/pixelmon-gallery/gui-presets")   # saved lab snapshots (*.json)
BOARDS = os.path.expanduser("~/pixelmon-gallery/corkboards")      # corkboards: one folder per board
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


def style_samples():
    """style -> {sample, montage} image paths under examples/styles (the repo's style gallery).
    The sample prefers the 'castle' render (same subject + seed across every style)."""
    base = os.path.join(REPO, "examples", "styles")
    out = {}
    for name in load_styles():
        d = os.path.join(base, name)
        imgs = sorted(f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.lower().endswith(IMG_EXT))
        pick = next((f for f in imgs if f.startswith("castle")), imgs[0] if imgs else None)
        rec = {}
        if pick:
            rec["sample"] = f"styles/{name}/{pick}"
        if os.path.isfile(os.path.join(base, name + ".png")):
            rec["montage"] = f"styles/{name}.png"
        if rec:
            out[name] = rec
    return out


HOME = os.path.realpath(os.path.expanduser("~"))


def home_path(path, must_exist=True):
    """Expand ~ and confine to the user's home dir (batch files, export dirs)."""
    full = os.path.realpath(os.path.expanduser(str(path or "")))
    if full != HOME and not full.startswith(HOME + os.sep):
        raise ValueError("path must be inside your home folder")
    if must_exist and not os.path.exists(full):
        raise ValueError(f"not found: {path}")
    return full


def export_target(p):
    """Where a batch row's result should be copied: <dir>/<path>.png, or None."""
    ex = p.get("export") or {}
    if not ex.get("dir") or not ex.get("path"):
        return None
    base = home_path(ex["dir"], must_exist=False)
    rel = re.sub(r"\.(png|gif)$", "", str(ex["path"]).strip().lstrip("/"))
    full = os.path.realpath(os.path.join(base, rel + ".png"))
    if not full.startswith(base + os.sep):
        raise ValueError(f"bad batch path {ex['path']!r}")
    return full


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
    warnings = prompt_warnings(p, pm)
    lora = str(p.get("lora") or "")
    if art or lora == "(none)":
        lora = "none"
    elif lora:
        lora = f"{lora} @ {float(p.get('lora_strength') or 1):g}"
    if p.get("fast"):
        lora += " + lcm-lora-sdxl.safetensors"
    return {"positive": pos, "negative": neg, "lora": lora, "warnings": warnings}


STOP = {"the", "and", "with", "for", "from", "into", "onto", "set", "its", "his", "her", "their", "that", "this",
        "very", "some", "over", "under", "near", "far", "top", "side", "while", "each", "one", "two", "has", "have"}
PEOPLE = r"\(?(person|people|character|figure|human|creature)s?\b"


def _words(text):
    """Plain lowercase words of a prompt fragment, ignoring (weights:1.3) syntax."""
    return set(w for w in re.findall(r"[a-z][a-z-]{2,}", re.sub(r":[\d.]+\)", ")", text.lower())) if w not in STOP)


def prompt_warnings(p, pm):
    """The setup wizard: combinations that fight themselves, each with one-click fixes.
    Returns [{"text": ..., "fixes": [{"label", "action", ...}]}]; the first fix is the recommended one."""
    form = p.get("form") or {}
    subject = str(form.get("subject") or p.get("prompt") or "")
    subj = _words(subject)
    kind, art = str(form.get("kind") or ""), bool(p.get("art"))
    styles = list(p.get("styles") or [])
    lp = load_presets().get(str(p.get("lora") or "")) or {}
    dos = bool(lp.get("kinds")) and not art                      # a dos-art LoRA (trained with caption tags)
    m = re.fullmatch(r"(\d+)x(\d+)", str(p.get("out") or ""))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    out = []

    def warn(text, *fixes):
        out.append({"text": text, "fixes": list(fixes)})
    drop = lambda n: {"label": f"drop “{n}”", "action": "drop_style", "value": n}
    setf = lambda label, field, value: {"label": label, "action": "set", "field": field, "value": value}
    size = lambda wh: {"label": f"size → {wh.replace('x', '×')}", "action": "size", "value": wh}

    # 4. dos-art LoRA hygiene
    if dos and not kind:
        first = "game sprite" if subj & CHARACTER_WORDS else "scene background"
        order = [first] + [k for k in ("scene background", "character portrait", "game sprite") if k != first]
        warn("this LoRA was trained with a Kind tag in every caption — pick one",
             *[setf(f"Kind → {k}", "kind", k) for k in order])
    # 1. negatives that ban the subject (a style's, or your own)
    for n in styles:
        hit = sorted(subj & _words((pm.STYLES.get(n) or {}).get("negative", "")))
        if hit:
            warn(f"style “{n}” bans {', '.join('“' + x + '”' for x in hit)} — which is in your subject, so the model is "
                 f"told to draw it AND not draw it (the negative usually wins)", drop(n))
    user_neg = str(p.get("negative") or "")
    if user_neg and user_neg.strip() != pm.PIXEL_NEGATIVE:
        hit = sorted(subj & _words(user_neg))
        if hit:
            warn(f"your negative prompt bans {', '.join('“' + x + '”' for x in hit)} — which is in your subject",
                 {"label": f"remove {', '.join(hit)} from negative", "action": "neg_remove", "value": hit})
    # 2. environment-only styles with a character
    for n in styles:
        st = pm.STYLES.get(n) or {}
        if re.search(PEOPLE, st.get("negative", "").lower()) and (kind in ("character portrait", "game sprite")
                                                                 or "empty scene" in st.get("prompt", "").lower() and kind != "scene background"):
            warn(f"style “{n}” is for EMPTY environments — it pushes people and creatures out; drop it for characters", drop(n))
    # 3. scene style on a sprite / portrait
    if "scene" in styles and kind in ("character portrait", "game sprite"):
        warn("style “scene” asks for a full environment, but Kind is a single " + kind.split()[-1], drop("scene"))
    if dos and w and max(w, h) > 400:
        warn(f"{w}×{h} is bigger than the 320×200-era art this LoRA learned — expect coarser, less authentic pixels"
             + (" (and snap pixels picks its own coarse grid)" if p.get("snap_pixels") else ""),
             size("128x128" if kind in ("character portrait", "game sprite") else "320x200"))
    if "ega" in str(p.get("lora") or "").lower() and str(p.get("palette") or "none") == "none" and not art:
        warn("an EGA LoRA without the EGA palette lock — colors will drift off the real 16", setf("palette → EGA", "palette", "EGA"))
    # 5. canvas vs kind
    if kind == "scene background" and w and max(w, h) <= 128:
        warn(f"a whole scene squeezed into {w}×{h} — there's no room for a place", size("320x200"))
    if kind in ("character portrait", "game sprite") and w and min(w, h) >= 320 and not p.get("transparent"):
        warn(f"a single {kind.split()[-1]} on a {w}×{h} canvas ends up small with lots of empty background", size("128x128"))
    if p.get("transparent") and kind == "scene background":
        warn("transparent background on a scene cuts holes in it", setf("transparent off", "transparent", False))
    # 6. sampler
    steps = float(p.get("steps") or 0)
    if steps > 50:
        warn(f"{int(steps)} steps — euler has converged by ~25–30; this just costs time", setf("steps → 30", "steps", 30))
    cfg = float(p.get("cfg") or 0)
    if p.get("fast") and cfg > 2.5:
        warn(f"CFG {cfg:g} with --fast (LCM) — LCM breaks above ~2.5", setf("CFG → default", "cfg", ""))
    elif cfg > 10:
        warn(f"CFG {cfg:g} overcooks SDXL (harsh contrast, burnt colors) — 5–9 is the sweet spot", setf("CFG → 7", "cfg", 7))
    # 7. dither that does nothing
    if str(p.get("dither") or "none") != "none" and float(p.get("dither_amount") or 0) < 0.15:
        warn(f"dither amount {float(p.get('dither_amount') or 0):g} is too small to see", setf("amount → 0.5", "dither_amount", 0.5))
    pa = float(p.get("pixel_angles") or 0)
    if pa > 2.5:
        warn(f"pixel-art angles {pa:g} starts bending shapes — 1.25–2 straightens edges without distorting",
             setf("angles → 1.5", "pixel_angles", 1.5))
    if p.get("snap_pixels") and not p.get("pixel_size") and not art:
        warn("snap pixels on 'auto' pixel size — the snapper guesses its own grid and often makes pixels HUGE",
             setf("pixel size → 1×1", "pixel_size", "1x1"), setf("pixel size → 2×1", "pixel_size", "2x1"))
    if not pa and str(p.get("angle_grid") or "pixel") != "pixel":
        warn(f"grid “{p['angle_grid']}” only applies when pixel-art angles is on", setf("angles → 1.5", "pixel_angles", 1.5))
    if pa and str(p.get("palette") or "none") == "none" and not art:
        warn("pixel-art angles works on flat color regions — without a palette lock the model's colors are too "
             "noisy to trace", setf("palette → EGA", "palette", "EGA"))
    # 8. evolve / steering strengths
    if p.get("parent"):
        if p.get("evolve_init") and float(p.get("evolve_denoise") or 0) >= 0.9:
            warn(f"evolve change {float(p['evolve_denoise']):g} ≈ a fresh render — the parent's layout barely survives",
                 {"label": "change → 0.55", "action": "evo", "field": "denoise", "value": 0.55})
        if float(p.get("evolve_steer") or 0) > 1.3:
            warn(f"steer toward parent {float(p['evolve_steer']):g} will over-bake (harsh, burnt, repeated motifs)",
                 {"label": "strength → 1.0", "action": "evo", "field": "steer", "value": 1.0})
    if p.get("steer") and float(p.get("steer_strength") or 0) > 1.3:
        warn(f"steering strength {float(p['steer_strength']):g} will over-bake",
             {"label": "strength → 0.8", "action": "steer", "field": "strength", "value": 0.8})
    return out


CHARACTER_WORDS = {"knight", "wizard", "warrior", "goblin", "orc", "dwarf", "elf", "dragon", "king", "queen", "princess",
                   "witch", "skeleton", "ghost", "monster", "rat", "spider", "robot", "soldier", "ninja", "pirate", "hero",
                   "man", "woman", "girl", "boy", "creature", "demon", "zombie", "cat", "dog", "bat", "ogre", "troll", "slime",
                   "sorceress", "paladin", "thief", "ranger", "captain", "cyborg", "alien", "beast", "golem", "mage"}


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
def shq(s):
    """Shell-quote for display/copy: bare when safe, "double" quotes for text with apostrophes
    (a wizard's study), falling back to shlex's '"'"' form only when double quotes can't be used."""
    s = str(s)
    if s and re.fullmatch(r"[\w@%+=:,./-]+", s):
        return s
    if "'" not in s:
        return "'" + s + "'"
    if not re.search(r'[$`"\\!]', s):
        return '"' + s + '"'
    return shlex.quote(s)


def shjoin(argv):
    return " ".join(shq(a) for a in argv)


def safe_name(s, maxlen=64):
    return re.sub(r"[^\w.-]+", "-", str(s)).strip("-.")[:maxlen]


def ref_path(rel):
    """'collection/file.png' -> absolute path inside REFS, or ValueError."""
    full = os.path.realpath(os.path.join(REFS, rel))
    if not full.startswith(os.path.realpath(REFS) + os.sep) or not full.lower().endswith(IMG_EXT) \
            or not os.path.isfile(full):
        raise ValueError(f"steering image not found: {rel}")
    return full


def list_refs():
    cols = {}
    if os.path.isdir(REFS):
        for c in sorted(os.listdir(REFS)):
            d = os.path.join(REFS, c)
            if os.path.isdir(d) and not c.startswith("."):
                cols[c] = sorted(f for f in os.listdir(d) if f.lower().endswith(IMG_EXT))
    return cols


def build_argv(p, steer_dir=None, steer_count=0, init=None):
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
        ps = str(p.get("pixel_size") or "").lower()
        if ps:
            if not re.fullmatch(r"\d{1,2}x\d{1,2}", ps):
                raise ValueError(f"bad pixel size {ps!r}; use WxH like 2x1")
            argv += ["--pixel-size", ps]
        pa = num("pixel_angles", float, 0.0, 3.0)
        if pa:
            argv += ["--pixel-angles", f"{pa:g}"]
            grid = str(p.get("angle_grid") or "pixel")
            if grid not in ("pixel", "square", "diagonal", "isometric", "hex", "triangle"):
                raise ValueError(f"unknown angle grid {grid!r}")
            if grid != "pixel":
                argv += ["--angle-grid", grid]
    if p.get("fast"):
        argv.append("--fast")
    for key, flag, typ, lo, hi in (("steps", "--steps", int, 1, 150), ("cfg", "--cfg", float, 0.0, 30.0)):
        v = num(key, typ, lo, hi)
        if v is not None:
            argv += [flag, f"{v:g}" if typ is float else str(v)]
    if steer_dir and steer_count:
        argv += ["--steer", steer_dir, "--steer-max", str(steer_count)]
        s = num("steer_strength", float, 0.0, 2.0)
        if s is not None:
            argv += ["--steer-strength", f"{s:g}"]
        wt = str(p.get("steer_weight_type") or "style transfer")
        if wt not in STEER_WEIGHT_TYPES:
            raise ValueError(f"unknown steer weight type {wt!r}")
        cb = str(p.get("steer_combine") or "concat")
        if cb not in STEER_COMBINE:
            raise ValueError(f"unknown steer combine {cb!r}")
        argv += ["--steer-weight-type", wt, "--steer-combine", cb]
        st, en = num("steer_start", float, 0.0, 1.0, 0.0), num("steer_end", float, 0.0, 1.0, 1.0)
        st, en = (0.0 if st is None else st), (1.0 if en is None else en)
        if not st < en:
            raise ValueError("steering start must be before its end")
        if (st, en) != (0.0, 1.0):
            argv += ["--steer-start", f"{st:g}", "--steer-end", f"{en:g}"]
    if init:
        d = num("evolve_denoise", float, 0.01, 1.0, 0.45)
        argv += ["--init", init, "--denoise", f"{d:g}"]
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
        jid = time.strftime("%Y%m%d-%H%M%S-") + f"{random.randrange(16**4):04x}"
        sent, params = params, dict(params)          # job records what was sent; overrides stay internal
        steer = [str(r) for r in (params.get("steer") or [])]
        if len(steer) > 32:
            raise ValueError("pick at most 32 steering images")
        srcs = [ref_path(r) for r in steer]          # validate before touching disk
        # Evolve: a parent render (from the gallery) seeds img2img and/or steering.
        parent = params.get("parent") or None
        parent_src = self.gallery_path(parent) if parent else None
        evo_steer = float(params.get("evolve_steer") or 0) if parent_src else 0.0
        evo_init = bool(params.get("evolve_init")) and parent_src is not None
        if parent_src and not (evo_steer > 0 or evo_init):
            raise ValueError("evolving needs 'keep composition' and/or 'steer toward parent' switched on")
        jdir = os.path.join(self.gallery, jid)
        init = None
        if parent_src:
            os.makedirs(jdir, exist_ok=True)
            kept = os.path.join(jdir, "parent" + os.path.splitext(parent_src)[1])
            shutil.copy2(parent_src, kept)           # the job keeps its own copy: reproducible later
            init = kept if evo_init else None
            if evo_steer > 0:
                srcs = [kept] + srcs
                params["steer_strength"] = evo_steer
                params["steer_weight_type"] = params.get("evolve_weight_type") or "style and composition"
                params["steer_start"] = params.get("evolve_steer_start", 0.0)
                params["steer_end"] = params.get("evolve_steer_end", 1.0)
        steer_dir = None
        if srcs:
            steer_dir = os.path.join(jdir, "steer")
            os.makedirs(steer_dir, exist_ok=True)
            for i, src in enumerate(srcs):           # numbered so same-named files can't collide
                os.symlink(src, os.path.join(steer_dir, f"{i:02d}_{os.path.basename(src)}"))
        argv = build_argv(params, steer_dir, len(srcs), init)   # validate before queueing
        export = export_target(params)
        job = {"id": jid, "params": sent, "argv": argv, "command": shjoin(["pixelmon"] + argv[1:]),
               "status": "queued", "log": [], "outputs": [], "full_prompt": {}, "group": group, "label": label,
               "total": int(params.get("n") or 1), "export": export, "exported": None,
               "created": time.time(), "started": None, "finished": None, "dir": jid}
        with self.lock:
            self.jobs[jid] = job
            self.order.append(jid)
        self.q.put(jid)
        return job

    def gallery_path(self, ref):
        """{'dir':..., 'file':...} of an earlier render -> its absolute path, or ValueError."""
        full = os.path.realpath(os.path.join(self.gallery, str(ref.get("dir", "")), str(ref.get("file", ""))))
        if not full.startswith(os.path.realpath(self.gallery) + os.sep) or not os.path.isfile(full):
            raise ValueError("parent image not found")
        return full

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
                found = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.gif"))
                               if not os.path.basename(f).startswith("parent."))   # an evolve job's kept parent isn't output
                known = {o["file"] for o in job["outputs"]}
                for f in found:
                    if f not in known:
                        m = re.search(r"_s(\d+)_", f)
                        job["outputs"].append({"seed": int(m.group(1)) if m else None, "file": f})
                job["outputs"] = [o for o in job["outputs"] if o["file"] in found]
                if job["status"] != "cancelled":
                    job["status"] = "done" if rc == 0 and job["outputs"] else "failed"
                if job["status"] == "done" and job.get("export"):
                    try:                             # batch rows: drop a clean <path>.png where the game loads it
                        os.makedirs(os.path.dirname(job["export"]), exist_ok=True)
                        shutil.copy2(os.path.join(d, job["outputs"][0]["file"]), job["export"])
                        job["exported"] = job["export"]
                    except OSError as e:
                        job["log"].append(f"export failed: {e}")
                job["finished"] = time.time()
                self.current, self.proc = None, None
            self._save(job)


def board_dir(name):
    name = safe_name(name or "")
    if not name:
        raise ValueError("board needs a name")
    return os.path.join(BOARDS, name)


def list_boards():
    os.makedirs(os.path.join(BOARDS, "favorites"), exist_ok=True)
    out = []
    for b in sorted(os.listdir(BOARDS)):
        d = os.path.join(BOARDS, b)
        if os.path.isdir(d) and not b.startswith("."):
            files = sorted((f for f in os.listdir(d) if f.lower().endswith(IMG_EXT)),
                           key=lambda f: os.path.getmtime(os.path.join(d, f)))
            out.append({"name": b, "items": files})
    return out


def list_presets():
    out = []
    for f in sorted(glob.glob(os.path.join(PRESETS, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception:
            continue
    return out


_COLOR_CACHE = {}


def image_colors(path):
    """Colors used by an image, most-used first: (n, ['#rrggbb', …]) — or ('>255', []) when there are more.
    Fully transparent pixels don't count. Cached per file + mtime."""
    key = (path, os.path.getmtime(path))
    if key not in _COLOR_CACHE:
        try:
            from PIL import Image
        except ImportError:
            return None, []
        im = Image.open(path).convert("RGBA")
        cols = im.getcolors(maxcolors=4096)
        if cols is not None:
            cols = [(c, rgba) for c, rgba in cols if rgba[3] > 0]
        if cols is None or len(cols) > 255:
            _COLOR_CACHE[key] = (">255", [])
        else:
            cols.sort(key=lambda t: -t[0])
            _COLOR_CACHE[key] = (len(cols), ["#%02x%02x%02x" % rgba[:3] for _, rgba in cols])
    return _COLOR_CACHE[key]


def gpl_text(name, colors):
    """GIMP palette (.gpl) — readable by GIMP, Aseprite, Krita, Inkscape, …"""
    lines = ["GIMP Palette", f"Name: {name}", "Columns: 16", "#"]
    for hx in colors:
        r, g, b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
        lines.append(f"{r:3d} {g:3d} {b:3d}\t{hx.upper()}")
    return "\n".join(lines) + "\n"


def gallery_items(gallery, limit=300):
    items = []
    for jf in sorted(glob.glob(os.path.join(gallery, "*", "job.json")), reverse=True)[:limit]:
        try:
            with open(jf, encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue
        job["outputs"] = [o for o in job.get("outputs") or [] if not o["file"].startswith("parent.")]
        if job["outputs"]:
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
                               "steer_weight_types": STEER_WEIGHT_TYPES, "steer_combine": STEER_COMBINE,
                               "style_samples": style_samples(),
                               "presets": load_presets(),
                               "loras": loras, "loras_live": live, "server": RENDER_SERVER,
                               "server_up": server_up()})
        if u.path == "/api/jobs":
            return self._json({"jobs": self.jobs.snapshot()})
        if u.path in ("/api/colors", "/api/colors.gpl"):
            q = urllib.parse.parse_qs(u.query)
            try:
                path = self.jobs.gallery_path({"dir": q.get("dir", [""])[0], "file": q.get("file", [""])[0]})
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            n, colors = image_colors(path)
            if n is None:
                return self._json({"error": "palette readout needs Pillow — run pixelmon-gui with ComfyUI's venv"}, 501)
            if u.path == "/api/colors":
                return self._json({"n": n, "colors": colors})
            if n == ">255":
                return self._json({"error": "more than 255 colors — no palette to export"}, 400)
            stem = os.path.splitext(os.path.basename(path))[0]
            body = gpl_text(stem, colors).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name(stem, 80)}.gpl"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/refs":
            return self._json({"root": REFS, "collections": list_refs()})
        if u.path.startswith("/refs/"):
            try:
                full = ref_path(urllib.parse.unquote(u.path[len("/refs/"):]))
            except ValueError:
                return self._json({"error": "not found"}, 404)
            return self._file(full, mimetypes.guess_type(full)[0] or "application/octet-stream")
        if u.path == "/api/presets":
            return self._json({"presets": list_presets()})
        if u.path == "/api/boards":
            return self._json({"root": BOARDS, "boards": list_boards()})
        if u.path.startswith("/boards/"):
            rel = urllib.parse.unquote(u.path[len("/boards/"):])
            full = os.path.realpath(os.path.join(BOARDS, rel))
            if not full.startswith(os.path.realpath(BOARDS) + os.sep) or not full.lower().endswith(IMG_EXT):
                return self._json({"error": "forbidden"}, 403)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png")
        if u.path == "/api/boards/zip":
            try:
                d = board_dir(urllib.parse.parse_qs(u.query).get("board", [""])[0])
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:     # PNGs are already compressed
                for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                    if f.lower().endswith(IMG_EXT):
                        z.write(os.path.join(d, f), f"{os.path.basename(d)}/{f}")
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(d)}.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path.startswith("/examples/") or u.path.startswith("/preset-img/"):
            root = os.path.join(REPO, "examples") if u.path.startswith("/examples/") else PRESETS
            rel = urllib.parse.unquote(u.path.split("/", 2)[2])
            full = os.path.realpath(os.path.join(root, rel))
            if not full.startswith(os.path.realpath(root) + os.sep) or not full.lower().endswith(IMG_EXT):
                return self._json({"error": "forbidden"}, 403)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png")
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
        if u.path == "/api/refs/upload":            # raw image bytes; ?collection=&filename=
            return self._upload(urllib.parse.parse_qs(u.query))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        try:
            if u.path == "/api/prompt":
                return self._json(full_prompt(body))
            if u.path == "/api/render":
                group, label = body.pop("group", None), body.pop("label", None)
                return self._json({"jobs": [self.jobs.add(body, group=group, label=label)["id"]]})
            if u.path == "/api/sweep":
                # same settings + one fixed seed, varying a single field across values
                base, field, values = body.get("params") or {}, body.get("field"), body.get("values") or []
                if field not in ("lora_strength", "dither", "dither_amount", "palette", "styles", "lora",
                                 "steps", "cfg", "despeckle", "out", "steer_strength",
                                 "steer_start", "steer_end", "pixel_angles", "angle_grid", "pixel_size"):
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
            if u.path == "/api/batch/read":
                f = home_path(body.get("path"))
                if not os.path.isfile(f) or os.path.getsize(f) > 4 * 1024 * 1024:
                    raise ValueError("pick a text file under 4 MB")
                with open(f, encoding="utf-8", errors="replace") as fh:
                    return self._json({"path": f, "text": fh.read()})
            if u.path == "/api/batch/exists":
                base = home_path(body.get("dir"), must_exist=False)
                found = [r for r in body.get("paths") or []
                         if os.path.isfile(export_target({"export": {"dir": base, "path": r}}) or "")]
                return self._json({"exists": found})
            if u.path == "/api/reveal":
                # open a folder in the desktop file manager (on the machine running this server)
                kind, arg = body.get("kind"), body.get("arg") or ""
                if kind == "job":
                    target = os.path.join(self.gallery, safe_name(arg, 64))
                elif kind == "gallery":
                    target = self.gallery
                elif kind == "refs":
                    target = os.path.join(REFS, safe_name(arg)) if arg else REFS
                elif kind == "presets":
                    target = PRESETS
                elif kind == "board":
                    target = board_dir(arg)
                elif kind == "dir":
                    target = home_path(arg)
                else:
                    raise ValueError("unknown folder")
                if not os.path.isdir(target):
                    raise ValueError(f"folder doesn't exist yet: {target}")
                subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return self._json({"opened": target})
            if u.path == "/api/boards/new":
                d = board_dir(body.get("name"))
                os.makedirs(d, exist_ok=True)
                return self._json({"name": os.path.basename(d)})
            if u.path == "/api/boards/add":
                # copy a render ({dir,file} in the gallery) or a steering ref ('col/file') onto a board
                d = board_dir(body.get("board"))
                os.makedirs(d, exist_ok=True)
                if body.get("ref"):
                    src = ref_path(body["ref"])
                    name = safe_name(os.path.basename(src), 120)
                elif body.get("src"):
                    src = self.jobs.gallery_path(body["src"])
                    name = safe_name(f"{body['src'].get('dir')}__{os.path.basename(src)}", 160)  # keeps provenance
                else:
                    raise ValueError("nothing to add")
                if os.path.exists(os.path.join(d, name)):
                    return self._json({"file": name, "exists": True})
                shutil.copy2(src, os.path.join(d, name))
                return self._json({"file": name})
            if u.path == "/api/boards/remove":
                d = board_dir(body.get("board"))
                f = os.path.realpath(os.path.join(d, str(body.get("file") or "")))
                if not f.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(f):
                    raise ValueError("not on this board")
                os.remove(f)                             # only the board's copy
                return self._json({"ok": True})
            if u.path == "/api/boards/clear":
                d = board_dir(body.get("board"))
                n = 0
                for f in os.listdir(d) if os.path.isdir(d) else []:
                    if f.lower().endswith(IMG_EXT) and os.path.isfile(os.path.join(d, f)):
                        os.remove(os.path.join(d, f))   # the board's copies only
                        n += 1
                return self._json({"removed": n})
            if u.path == "/api/boards/to-steering":
                # mirror the board into the steering library as collection 'cork-<board>'
                d = board_dir(body.get("board"))
                col = "cork-" + os.path.basename(d)
                cd = os.path.join(REFS, col)
                os.makedirs(cd, exist_ok=True)
                want = {f for f in os.listdir(d) if f.lower().endswith(IMG_EXT)} if os.path.isdir(d) else set()
                for f in os.listdir(cd):                 # this collection is a mirror: drop what left the board
                    if f.lower().endswith(IMG_EXT) and f not in want:
                        os.remove(os.path.join(cd, f))
                for f in want:
                    if not os.path.exists(os.path.join(cd, f)):
                        shutil.copy2(os.path.join(d, f), os.path.join(cd, f))
                return self._json({"collection": col, "refs": sorted(f"{col}/{f}" for f in want)})
            if u.path == "/api/presets":
                name = safe_name(body.get("name") or "")
                if not name or not isinstance(body.get("snapshot"), dict):
                    raise ValueError("preset needs a name and a snapshot")
                os.makedirs(PRESETS, exist_ok=True)
                rec = {"name": name, "saved": time.time(), "note": str(body.get("note") or "")[:500],
                       "snapshot": body["snapshot"], "image": None}
                img = body.get("image")                 # {dir, file} of a render to keep as the preset's picture
                if img:
                    src = self.jobs.gallery_path(img)
                    ext = os.path.splitext(src)[1].lower()
                    for e in IMG_EXT:                       # replace the previous picture of this preset
                        if os.path.isfile(os.path.join(PRESETS, name + e)):
                            os.remove(os.path.join(PRESETS, name + e))
                    shutil.copy2(src, os.path.join(PRESETS, name + ext))
                    rec["image"] = name + ext
                with open(os.path.join(PRESETS, name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, indent=1)
                return self._json({"name": name})
            if u.path == "/api/presets/delete":
                name = safe_name(body.get("name") or "")
                for e in (".json",) + IMG_EXT if name else ():   # exact names only
                    if os.path.isfile(os.path.join(PRESETS, name + e)):
                        os.remove(os.path.join(PRESETS, name + e))
                return self._json({"ok": True})
            if u.path == "/api/refs/collection":
                name = safe_name(body.get("name") or "")
                if not name:
                    raise ValueError("collection needs a name")
                os.makedirs(os.path.join(REFS, name), exist_ok=True)
                return self._json({"name": name})
            if u.path.startswith("/api/cancel/"):
                return self._json({"ok": self.jobs.cancel(u.path.rsplit("/", 1)[1])})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)


def unique_path(d, fname):
    """d/fname, or d/fname-1, -2 … so nothing is ever overwritten."""
    stem, ext = os.path.splitext(fname)
    out, k = os.path.join(d, fname), 1
    while os.path.exists(out):
        out, k = os.path.join(d, f"{stem}-{k}{ext}"), k + 1
    return out


def _upload(self, q):
    board = q.get("board")
    col = safe_name((q.get("collection") or ["gui-drops"])[0]) or "gui-drops"
    fname = safe_name((q.get("filename") or ["image.png"])[0], 96)
    n = int(self.headers.get("Content-Length") or 0)
    if not fname.lower().endswith(IMG_EXT):
        return self._json({"error": f"not an image type: {fname}"}, 400)
    if not 0 < n <= 40 * 1024 * 1024:
        return self._json({"error": "image must be under 40 MB"}, 400)
    data = self.rfile.read(n)
    magic = (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8"
             or data[:2] == b"BM" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
    if not magic:
        return self._json({"error": f"{fname} doesn't look like an image"}, 400)
    try:
        d = board_dir(board[0]) if board else os.path.join(REFS, col)   # desktop files dropped on a corkboard
    except ValueError as e:
        return self._json({"error": str(e)}, 400)
    os.makedirs(d, exist_ok=True)
    out = unique_path(d, fname)
    with open(out, "wb") as f:
        f.write(data)
    if board:
        return self._json({"board": os.path.basename(d), "file": os.path.basename(out)})
    return self._json({"ref": f"{col}/{os.path.basename(out)}"})


Handler._upload = _upload


def main():
    global REFS, PRESETS, BOARDS
    ap = argparse.ArgumentParser(prog="pixelmon-gui", description="Web GUI for pixelmon (renders on the rtx box).")
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces so other devices on the LAN can use it")
    ap.add_argument("--refs", default=REFS, help="steering image library, one folder per collection "
                                                  "(default ~/pixelmon-refs)")
    ap.add_argument("--boards", default=BOARDS, help="corkboards, one folder per board (default ~/pixelmon-gallery/corkboards)")
    ap.add_argument("--presets", default=PRESETS, help="saved lab presets (default ~/pixelmon-gallery/gui-presets)")
    ap.add_argument("--gallery", default=os.path.expanduser("~/pixelmon-gallery/gui"),
                    help="where renders + job.json records go (default ~/pixelmon-gallery/gui)")
    args = ap.parse_args()
    REFS = os.path.realpath(os.path.expanduser(args.refs))
    PRESETS = os.path.expanduser(args.presets)
    BOARDS = os.path.expanduser(args.boards)
    os.makedirs(os.path.join(REFS, "gui-drops"), exist_ok=True)
    os.makedirs(args.gallery, exist_ok=True)
    Handler.gallery = os.path.realpath(args.gallery)
    Handler.jobs = JobQueue(Handler.gallery)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        srv = http.server.ThreadingHTTPServer((host, args.port), Handler)
    except OSError as e:
        if e.errno != 98:                        # EADDRINUSE
            raise
        try:                                     # is it us?
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/meta", timeout=2) as r:
                json.load(r)
            sys.exit(f"pixelmon-gui is already running: http://127.0.0.1:{args.port}\n"
                     f"  (stop it with:  pkill -f pixelmon/gui/server.py   or pick another --port)")
        except (OSError, ValueError):
            sys.exit(f"port {args.port} is in use by another program; try  pixelmon-gui --port {args.port + 1}")
    print(f"pixelmon-gui on http://{'<this-ip>' if args.lan else '127.0.0.1'}:{args.port}  "
          f"(renders on '{RENDER_SERVER}', gallery {args.gallery})  Ctrl-C to quit")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
