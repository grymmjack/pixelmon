#!/usr/bin/env python3
"""pixelmon-gui — a local web front end for the pixelmon CLI.

Serves a single-page UI, queues render jobs, and runs them one at a time by
shelling out to bin/pixelmon (--server <render server> --no-open). Each job renders
into its own folder under the gallery dir with a job.json recording the exact
settings and command, so the gallery can reload / re-run anything.

The render server is a servers.json alias, host[:port] or URL: --server NAME, else $PIXELMON_SERVER,
else the one picked in the Setup tab, else servers.json "_default", else "local" (ComfyUI on this machine, port 8188).

Standard library only. usage: pixelmon-gui [--port 8190] [--lan] [--server NAME] [--gallery DIR]
"""
import argparse
import ast
import base64
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
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request
import zipfile
from struct import error as struct_error
from zlib import error as zlib_error

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
REPO = os.path.dirname(HERE)
sys.path.insert(1, REPO)                        # the repo's shared modules (colors.py, manifest.py)
import sets as setsmod                          # noqa: E402  (gui/sets.py: list → takes → pick → export)
import similar                                  # noqa: E402  (gui/similar.py: 🔎 find similar pictures)
from colors import image_colors, picture_palette, gpl_text   # noqa: E402  (one palette readout for the CLI and the GUI)
PIXELMON = os.path.join(REPO, "bin", "pixelmon")
SERVER_ARG = ""          # --server / $PIXELMON_SERVER: overrides the Setup tab's choice
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DONE_LINE = re.compile(r"✅.*?seed=(\d+)\s+->\s+(\S.*)$")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
STEER_WEIGHT_TYPES = ["style transfer", "strong style transfer", "style transfer precise", "composition",
                      "composition precise", "style and composition", "linear", "ease in", "ease out"]
STEER_COMBINE = ["concat", "average", "norm average", "add", "subtract"]
REFS = os.path.expanduser("~/pixelmon-refs")      # steering library: one folder per collection
PRESETS = os.path.expanduser("~/pixelmon-gallery/gui-presets")   # saved lab snapshots (*.json)
BOARDS = os.path.expanduser("~/pixelmon-gallery/corkboards")      # corkboards: one folder per board
LAB = os.path.expanduser("~/pixelmon-gallery/lab-inputs")         # LAB: images to restyle
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


_CLI_SPEC = {"t": 0, "v": None}


def cli_spec():
    """Every pixelmon flag's default / choices / help, read from the add_argument calls in pixelmon.py —
    the GUI's flag tooltips (and Setup's model checks) come from the CLI itself, so they never drift."""
    path = os.path.join(REPO, "pixelmon.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CLI_SPEC["v"] is not None and _CLI_SPEC["t"] == mtime:
        return _CLI_SPEC["v"]
    out = {}
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not flags or not flags[0].startswith("-"):
            continue
        rec = {"flags": flags}
        for kw in node.keywords:
            if kw.arg in ("default", "choices", "help", "action", "metavar", "dest"):
                try:
                    rec[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    if kw.arg == "help":            # f-strings / concatenations: keep the literal parts
                        parts = [n.value for n in ast.walk(kw.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                        if parts:
                            rec["help"] = "".join(parts)
            elif kw.arg == "type":
                rec["type"] = getattr(kw.value, "id", None)
        if "default" in rec and not isinstance(rec["default"], (str, int, float, bool, type(None))):
            del rec["default"]
        out[max(flags, key=len)] = rec              # keyed by the long form (--steer-strength, not -s)
    _CLI_SPEC.update(t=mtime, v=out)
    return out


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
LAN = False              # --lan: the page is open to other devices; browsing / opening paths stays on this machine


def home_path(path, must_exist=True):
    """Expand ~ and confine to the user's home dir (batch files, export dirs)."""
    full = os.path.realpath(os.path.expanduser(str(path or "")))
    if full != HOME and not full.startswith(HOME + os.sep):
        raise ValueError("path must be inside your home folder")
    if must_exist and not os.path.exists(full):
        raise ValueError(f"not found: {path}")
    return full


def browse(path, kind="any", exts=None, hidden=False, gallery=None, sets=None):
    """One folder of the user's home for the Browse dialog: sub-folders, then files (filtered by
    extension), breadcrumbs back to home, and shortcuts. Home-confined like every other path here."""
    full = home_path(path or "~")
    if os.path.isfile(full):
        full = os.path.dirname(full)
    dirs, files = [], []
    try:
        with os.scandir(full) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        dirs.append(e.name)
                    elif kind != "dir" and e.is_file() and (not exts or e.name.lower().endswith(exts)):
                        st = e.stat()
                        files.append({"name": e.name, "size": st.st_size, "mtime": int(st.st_mtime)})
                except OSError:
                    continue
    except OSError as e:
        raise ValueError(f"can't read {full}: {e.strerror or e}")
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    crumbs, p = [], full
    while True:
        crumbs.append({"name": "~" if p == HOME else os.path.basename(p), "path": p})
        if p == HOME or os.path.dirname(p) == p:
            break
        p = os.path.dirname(p)
    places = [{"name": "Home", "path": HOME}]
    for label, d in (("Gallery", gallery), ("Steering refs", REFS), ("LAB inputs", LAB), ("Corkboards", BOARDS),
                     ("Presets", PRESETS), ("Pictures", "~/Pictures"), ("Downloads", "~/Downloads"),
                     ("Desktop", "~/Desktop"), ("Documents", "~/Documents"), ("git", "~/git")):
        if d and os.path.isdir(os.path.expanduser(d)):
            places.append({"name": label, "path": os.path.realpath(os.path.expanduser(d))})
    folders = []
    if sets is not None:                     # every set's game folder (and old-sprites folder)
        for s in sets.list()[:12]:
            try:
                t = sets.load(s["id"])["theme"]
            except (ValueError, KeyError):
                continue
            folders += [(s["name"], t.get("game_folder")), (s["name"] + " (old)", (t.get("fit") or {}).get("dir"))]
    for name, f in folders:
        try:
            d = home_path(f) if f else None
        except ValueError:
            continue
        if d and os.path.isdir(d) and all(x["path"] != d for x in places):
            places.append({"name": name, "path": d})
    cap = 3000
    return {"path": full, "home": HOME, "parent": None if full == HOME else os.path.dirname(full), "crumbs": crumbs[::-1],
            "dirs": dirs[:cap], "files": files[:cap], "more": max(0, len(dirs) + len(files) - 2 * cap), "places": places,
            "sep": os.sep}


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


def fit_target(p):
    """A batch row's old picture to fit into (<dir>/<path>.png), or None when it doesn't exist (yet)."""
    ft = p.get("fit") or {}
    if not ft.get("dir") or not ft.get("path"):
        return None
    base = home_path(ft["dir"], must_exist=False)
    rel = re.sub(r"\.(png|gif)$", "", str(ft["path"]).strip().lstrip("/"))
    full = os.path.realpath(os.path.join(base, rel + ".png"))
    if not full.startswith(base + os.sep):
        raise ValueError(f"bad batch path {ft['path']!r}")
    return full if os.path.isfile(full) else None


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
    if p.get("exact"):     # exact prompt: sent word for word, so the preview is just the text itself
        pos, neg = str(p.get("prompt") or "").strip(), str(p.get("negative") or "").strip()
        style_add = style_neg = ""
    else:
        style_add, style_neg = pm.resolve_styles(",".join(p.get("styles") or []))
        a = types.SimpleNamespace(art=art, no_sprite_suffix=bool(p.get("no_sprite_suffix")),
                                  tile=p.get("tile") if p.get("tile") in ("both", "x", "y") else None,
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


# "no thick lines", "without a border", "not blurry" … up to the next comma/period
NEGATION = re.compile(r"\b(?:no|not|without|never|avoid|don'?t)\b[^,.;]*", re.I)
STOP = {"the", "and", "with", "for", "from", "into", "onto", "set", "its", "his", "her", "their", "that", "this",
        "very", "some", "over", "under", "near", "far", "top", "side", "while", "each", "one", "two", "has", "have"}
PEOPLE = r"(?<!full )\(?(person|people|character|figure|human|creature)s?\b"   # "full figure" = framing, not a person


def _words(text):
    """Plain lowercase words of a prompt fragment, ignoring (weights:1.3) syntax."""
    return set(w for w in re.findall(r"[a-z][a-z-]{2,}", re.sub(r":[\d.]+\)", ")", text.lower())) if w not in STOP)


def prompt_warnings(p, pm):
    """The setup wizard: combinations that fight themselves, each with one-click fixes.
    Returns [{"text": ..., "fixes": [{"label", "action", ...}]}]; the first fix is the recommended one."""
    form = p.get("form") or {}
    subject = str(form.get("subject") or p.get("prompt") or "")
    negs = [m.group(0).strip() for m in NEGATION.finditer(subject)]
    subj = _words(NEGATION.sub(" ", subject))       # words that are only there inside "no …" don't count
    kind, art = str(form.get("kind") or ""), bool(p.get("art"))
    styles = list(p.get("styles") or [])
    lp = load_presets().get(str(p.get("lora") or "")) or {}
    dos = bool(lp.get("kinds")) and not art                      # a dos-art LoRA (trained with caption tags)
    m = re.fullmatch(r"(\d+)x(\d+)", str(p.get("out") or ""))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    out = []

    def warn(text, *fixes):
        out.append({"text": text, "fixes": list(fixes)})
    if negs:
        warn("the model doesn't understand " + ", ".join("“" + n + "”" for n in negs[:3]) + " — it reads the words and tends "
             "to ADD them. Say what you want in the prompt; put what you don't want in the negative",
             {"label": "move to negative", "action": "neg_move", "value": negs})
    if re.search(r"\b(1|one|single)[ -]?(px|pixel)\b.*\blines?\b|\bthin (out)?lines?\b|\bthick (out)?lines?\b", subject.lower() + " "
                 + str(p.get("negative") or "").lower()) and not p.get("thin_lines") and not p.get("art"):
        warn("prompts can't make lines exactly 1 px — the model draws 6–10 px outlines at full size. "
             "“1-px outlines” thins them after rendering", {"label": "1-px outlines on", "action": "set", "field": "thin_lines", "value": 4})
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
    tl = int(p.get("thin_lines") or 0)
    if w and max(w, h) <= 160 and not art:
        # on small sprites/portraits almost every feature (eyes, nose, beard shading) IS a thin stroke
        if tl > 2:
            warn(f"1-px outlines thinning lines up to {tl} px on a {w}×{h} image thins away the features themselves "
                 "(eyes, shading) — use 2 or off for small sprites/portraits",
                 setf("outlines → up to 2 px", "thin_lines", "2"), setf("outlines off", "thin_lines", "0"))
        if pa:
            warn(f"pixel-art angles on a {w}×{h} image reshapes the features — it's meant for 320×200+ scenes",
                 setf("angles off", "pixel_angles", 0))
    if pa > 2.5:
        warn(f"pixel-art angles {pa:g} starts bending shapes — 1.25–2 straightens edges without distorting",
             setf("angles → 1.5", "pixel_angles", 1.5))
    snapper = str(p.get("snap_method") or "unfake") == "snapper"
    if p.get("snap_pixels") and snapper and not p.get("pixel_size") and not art:
        warn("snap pixels on 'auto' pixel size — the snapper guesses its own grid and often makes pixels HUGE",
             setf("pixel size → 1×1", "pixel_size", "1x1"), setf("pixel size → 2×1", "pixel_size", "2x1"))
    if 0 < max(w, h) <= 32 and not art and not p.get("fit_subject") and not p.get("tile") and kind != "scene background":
        warn(f"a {w}×{h} sprite: the model leaves space around its subject, so it ends up only a few pixels wide — "
             "fill frame crops to the subject first",
             setf("fill frame on", "fit_subject", True))
    if p.get("snap_pixels") and not snapper and not art and int(p.get("despeckle") or 0) >= 2:
        warn("unfake keeps the model's real grid, where small details (book spines, eyes) are 1–2 pixel islands — "
             "despeckle 2+ wipes them out",
             setf("despeckle → 0", "despeckle", 0), setf("despeckle → 1", "despeckle", 1))
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


def _servers_json():
    try:
        with open(os.path.join(REPO, "servers.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def known_servers():
    """servers.json aliases (keys starting with _ are comments) plus the built-in 'local'."""
    out = {k: v for k, v in _servers_json().items() if not k.startswith("_") and isinstance(v, str)}
    out.setdefault("local", "http://127.0.0.1:8188")
    return out


def default_server():
    """servers.json "_default" (what pixelmon uses without --server), else 'local'."""
    return re.sub(r"\s+", "", str(_servers_json().get("_default") or "")) or "local"


def render_server():
    """Where renders go: --server / $PIXELMON_SERVER, else the Setup tab's pick, else servers.json's default."""
    return SERVER_ARG or load_setup().get("server") or default_server()


def server_url(name=None):
    """The URL of a render server name, resolved the way pixelmon does (a farm list -> its first server)."""
    name = (name or render_server()).split(",")[0].strip()
    url = known_servers().get(name, name)
    if "://" not in url:
        url = "http://" + url
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.hostname}:{p.port or 8188}" if p.hostname else None


def load_loras():
    """LoRA filenames available on the render server (falls back to local ComfyUI)."""
    url = server_url()
    if url and server_is_up():
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models/loras", timeout=4) as r:
                return sorted(json.load(r)), True
        except Exception:
            pass
    local = os.path.expanduser("~/ComfyUI/models/loras")
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(local, "*.safetensors"))), False


_ADV_CACHE = {"t": 0, "v": None}
FALLBACK_SAMPLERS = ["euler", "euler_ancestral", "heun", "dpm_2", "dpm_2_ancestral", "lms", "dpmpp_2s_ancestral", "dpmpp_sde",
                     "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "ddim", "uni_pc", "lcm"]
FALLBACK_SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"]


def _combo(info, node, field):
    """A COMBO input's options from ComfyUI /object_info (old list form or new {"options": [...]} form)."""
    try:
        spec = info[node]["input"]["required"][field]
    except (KeyError, TypeError):
        return []
    if isinstance(spec[0], list):
        return list(spec[0])
    if len(spec) > 1 and isinstance(spec[1], dict):
        return list(spec[1].get("options") or [])
    return []


def load_adv_lists():
    """Choices for the Advanced tab, read live from the render server's ComfyUI (cached a minute)."""
    if _ADV_CACHE["v"] is not None and time.time() - _ADV_CACHE["t"] < 60:
        return _ADV_CACHE["v"]
    info, url = {}, server_url()
    if url and server_is_up():
        for node in ("KSampler", "CheckpointLoaderSimple", "ControlNetLoader", "IPAdapterModelLoader", "CLIPVisionLoader", "UNETLoader"):
            try:
                with urllib.request.urlopen(f"{url.rstrip('/')}/object_info/{node}", timeout=4) as r:
                    info.update(json.load(r))
            except Exception:
                pass
    v = {"samplers": _combo(info, "KSampler", "sampler_name") or FALLBACK_SAMPLERS,
         "schedulers": _combo(info, "KSampler", "scheduler") or FALLBACK_SCHEDULERS,
         "checkpoints": [c for c in _combo(info, "CheckpointLoaderSimple", "ckpt_name")
                         if not re.search(r"audio|ace_step|music|sfx", c, re.I)],
         "controlnets": _combo(info, "ControlNetLoader", "control_net_name"),
         "ipadapters": _combo(info, "IPAdapterModelLoader", "ipadapter_file"),
         "clip_visions": _combo(info, "CLIPVisionLoader", "clip_name"),
         "inpaint_models": [u for u in _combo(info, "UNETLoader", "unet_name") if "inpaint" in u.lower()],
         "live": bool(info)}
    if info:                                     # don't remember the fallbacks from a server that didn't answer
        _ADV_CACHE.update(t=time.time(), v=v)
    return v


# What each pixelmon feature needs on a render server: (feature, ComfyUI node, its model input, pixelmon flag whose
# default names the file). A None field only checks that the node (a custom node pack) is installed.
NEEDS = [("render", "CheckpointLoaderSimple", "ckpt_name", "--base"),
         ("pixel LoRA", "LoraLoader", "lora_name", "--lora"),
         ("palette", "PixelArtPalette", None, None),
         ("fast (LCM)", "LoraLoader", "lora_name", "--lcm-lora"),
         ("LAB / edit", "ControlNetLoader", "control_net_name", "--control-model"),
         ("depth lock", "ControlNetLoader", "control_net_name", "--depth-model"),
         ("depth map", "DepthAnythingV2Preprocessor", None, None),
         ("steering", "IPAdapterModelLoader", "ipadapter_file", "--steer-model"),
         ("steering vision", "CLIPVisionLoader", "clip_name", "--steer-clip"),
         ("cut-out", "LoadBackgroundRemovalModel", "bg_removal_name", "--cutout-model"),
         ("3D model", "CheckpointLoaderSimple", "ckpt_name", "--mesh-model")]
_MODELS = {}


def server_models(name):
    """[{feature, ok, need, why}] for one render server (its /object_info, cached a minute), or None if it didn't answer."""
    hit = _MODELS.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    url, info = server_url(name), {}
    if not url:
        return None
    for node in sorted({n for _, n, _, _ in NEEDS}):
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/object_info/{node}", timeout=4) as r:
                info.update(json.load(r))
        except Exception:
            pass
    if not info:
        return None
    spec, out = cli_spec(), []
    for feat, node, field, flag in NEEDS:
        want = str((spec.get(flag) or {}).get("default") or "") if flag else ""
        if node not in info:
            out.append({"feature": feat, "ok": False, "need": want or node, "why": f"node {node} isn't installed"})
            continue
        if not field:
            out.append({"feature": feat, "ok": True, "need": node, "why": ""})
            continue
        have = _combo(info, node, field)
        ok = (want in have) if want else bool(have)
        out.append({"feature": feat, "ok": ok, "need": want or field,
                    "why": "" if ok else f"{want or 'a model'} isn't in {node} ({len(have)} other{'s' if len(have) != 1 else ''})"})
    _MODELS[name] = (time.time(), out)
    return out


def servers_report():
    """Every known render server: up/down, its URL, the default / current pick, auto-restart, and the
    models each pixelmon feature needs (all checked in parallel)."""
    names = sorted(known_servers())
    cur = [s.strip() for s in render_server().split(",")]
    for s in cur:                                # a typed host:port in Setup counts too
        if s and s not in names:
            names.append(s)
    out = {n: {"name": n, "url": server_url(n), "up": False, "models": None} for n in names}

    def check(n):
        out[n]["up"] = server_up(n)
        if out[n]["up"]:
            out[n]["models"] = server_models(n)
    ts = [threading.Thread(target=check, args=(n,), daemon=True) for n in names]
    for t in ts:
        t.start()
    for t in ts:
        t.join(12)
    for n in names:
        out[n].update(default=n == default_server(), selected=n in cur, restart=bool(restart_command(n)))
    return {"servers": [out[n] for n in names], "render": render_server(), "locked": SERVER_ARG}


def servers_for(p):
    """Where this render goes: the form's own pick (p['servers'], the chips under RENDER), else render_server()."""
    pick = p.get("servers")
    if not pick:
        return render_server()
    if isinstance(pick, str):
        pick = pick.split(",")
    known, out = known_servers(), []
    for s in list(pick)[:16]:
        s = re.sub(r"\s+", "", str(s or ""))
        if not s:
            continue
        if s not in known and not re.fullmatch(r"(https?://)?[\w.-]+(:\d{1,5})?/?", s):
            raise ValueError(f"unknown render server {s!r}")
        if s not in out:
            out.append(s)
    return ",".join(out) or render_server()


def adv_argv(adv, art, has_control, has_mask, mask_mode="fill", rough=False):
    """The Advanced tab: extra pixelmon flags. Blank / missing = pixelmon's own default. Validated like the rest."""
    adv = adv if isinstance(adv, dict) else {}
    out = []

    def val(key):
        v = adv.get(key)
        return None if v in (None, "", False) else v

    def num(key, typ, lo, hi):
        v = val(key)
        if v is None:
            return None
        v = typ(v)
        if not lo <= v <= hi:
            raise ValueError(f"advanced: {key.replace('_', ' ')} must be between {lo:g} and {hi:g}")
        return v

    def fname(key):
        v = val(key)
        if v is None:
            return None
        v = str(v)
        if not re.fullmatch(r"[\w.+\- ()\[\]/]{1,200}", v) or ".." in v:
            raise ValueError(f"advanced: bad file name for {key.replace('_', ' ')}")
        return v

    def choice(key, options):
        v = val(key)
        if v is None:
            return None
        if v not in options:
            raise ValueError(f"advanced: unknown {key.replace('_', ' ')} {v!r}")
        return v

    lists = load_adv_lists()
    for key, flag in (("sampler", "--sampler"), ("scheduler", "--scheduler")):
        v = choice(key, lists[key + "s"] + (FALLBACK_SAMPLERS if key == "sampler" else FALLBACK_SCHEDULERS))
        if v:
            out += [flag, v]
    for key, flag in (("base", "--base"), ("lcm_lora", "--lcm-lora")):
        v = fname(key)
        if v:
            out += [flag, v]
    r = num("res", int, 512, 2048)
    if r is not None:
        if r % 64:
            raise ValueError("advanced: generation resolution must be a multiple of 64")
        out += ["--res", str(r)]
    sz = val("size")
    if sz is not None:
        if not re.fullmatch(r"\d{1,4}(x\d{1,4})?", str(sz).lower()):
            raise ValueError("advanced: sampling size must be N or WxH")
        out += ["--size", str(sz).lower()]
    if not art:
        v = choice("smooth", ["mode", "median", "none"])
        if v:
            out += ["--smooth", v]
        v = choice("filter", ["nearest", "box"])
        if v:
            out += ["--filter", "box (area average)" if v == "box" else v]
        for key, flag, lo, hi in (("pixel_grid", "--pixel-grid", 32, 1024), ("snap_colors", "--snap-colors", 1, 256),
                                  ("bg_tolerance", "--bg-tolerance", 0, 128)):
            v = num(key, int, lo, hi)
            if v is not None:
                out += [flag, str(v)]
        hx = val("custom_hex")
        if hx is not None:
            cols = re.findall(r"#?[0-9a-fA-F]{6}\b|#?[0-9a-fA-F]{3}\b", str(hx))
            if not cols or len(cols) > 256:
                raise ValueError("advanced: custom colors must be 1..256 hex codes like #0000aa")
            out += ["--custom-hex", " ".join(c if c.startswith("#") else "#" + c for c in cols)]
        if val("preview"):
            out.append("--preview")
            v = num("view_scale", int, 1, 32)
            if v is not None:
                out += ["--view-scale", str(v)]
    if has_control:
        v = num("control_end", float, 0.05, 1.0)
        if v is not None:
            out += ["--control-end", f"{v:g}"]
        v = fname("control_model")
        if v:
            out += ["--control-model", v]
        lo, hi = num("canny_low", float, 0.01, 0.99), num("canny_high", float, 0.01, 0.99)
        if lo is not None or hi is not None:
            lo, hi = (0.3 if lo is None else lo), (0.7 if hi is None else hi)
            if not lo < hi:
                raise ValueError("advanced: canny low threshold must be below the high one")
            out += ["--canny-low", f"{lo:g}", "--canny-high", f"{hi:g}"]
    if has_mask:
        # the SDXL inpainting model: "auto" = fill edits (painting something new) when the server has one;
        # picked by name = every LAB edit, blend ones too; "off" = the checkpoint like any other render
        choice_im = str(adv.get("inpaint_model") or "auto")
        # a painted-in edit never uses it: the inpainting model is shown the masked area blanked to grey, so it can't
        # see the painted shape and draws what "should" be there (a masked eye came back as an eye, or a patch with a hole)
        if not rough and choice_im != "off" and (choice_im != "auto" or (adv.get("mask_mode") or mask_mode) == "fill"):
            have = lists.get("inpaint_models") or []
            pick = choice_im if choice_im in have else (have[0] if have else None)
            if pick:
                out += ["--inpaint-model", pick]
        v = num("mask_grow", int, 0, 64)
        if v is not None:
            out += ["--mask-grow", str(v)]
        if adv.get("inpaint_crop") is False:
            out.append("--no-inpaint-crop")
        if adv.get("keep_outside") is False:
            out.append("--no-keep-outside")
    for key, flag in (("steer_model", "--steer-model"), ("steer_clip", "--steer-clip")):
        v = fname(key)
        if v:
            out += [flag, v]
    g = val("animate")
    if g is not None:
        g = str(g).strip()
        if not 0 < len(g) <= 120:
            raise ValueError("advanced: animation gesture must be 1..120 characters")
        out += ["--animate", g]
        v = val("anim_region")
        if v is not None:
            out += ["--anim-region", str(v)[:80]]
        for key, flag, typ, lo, hi in (("anim_frames", "--anim-frames", int, 2, 8), ("anim_fps", "--anim-fps", float, 1, 30),
                                       ("anim_hold", "--anim-hold", float, 0, 10), ("anim_denoise", "--anim-denoise", float, 0.1, 1),
                                       ("anim_res", "--anim-res", int, 512, 1024)):
            v = num(key, typ, lo, hi)
            if v is not None:
                out += [flag, f"{v:g}" if typ is float else str(v)]
        v = choice("anim_loop", ["pingpong", "cycle", "once-return"])
        if v:
            out += ["--anim-loop", v]
        v = val("anim_box")
        if v is not None:
            try:
                box = [float(x) for x in str(v).split(",")]
            except ValueError:
                box = []
            if len(box) != 4 or not all(0 <= x <= 1 for x in box) or not (box[0] < box[2] and box[1] < box[3]):
                raise ValueError("advanced: animation box must be left,top,right,bottom fractions 0..1, e.g. 0.25,0.36,0.67,0.49")
            out += ["--anim-box", ",".join(f"{x:g}" for x in box)]
    return out


SERVER_STATE = {"server": None, "url": None, "up": None, "stuck": None, "restart": None}
STUCK_AFTER = 20      # seconds the same prompt may wait at the head of an idle ComfyUI queue
DOWN_AFTER = 30       # seconds of no answer before a render server with a restart command is restarted
RESTART_EVERY = 180   # at most one automatic restart per server this often (it needs time to come up)
# errors that leave ComfyUI failing every prompt until it's restarted (a model it freed is still on its
# loaded list; seen after Stable Audio jobs swap Qwen / T5Gemma / SA3 through an 8GB card)
POISONED = ("object has no attribute 'model_size'", "object has no attribute 'is_dynamic'")


def restart_command(name):
    """servers.json "_restart": {"<alias>": "<shell command>"} — how to restart that server's ComfyUI."""
    cmds = _servers_json().get("_restart")
    return cmds.get(name) if isinstance(cmds, dict) and isinstance(cmds.get(name), str) else None


def restart_server(name, why):
    """Run the server's restart command in the background; SERVER_STATE["restart"] reports how it went."""
    cmd = restart_command(name)
    state = {"server": name, "why": why, "at": time.time(), "status": "running", "output": ""}
    SERVER_STATE["restart"] = state
    print(f"[pixelmon-gui] render server {name} {why}: restarting it ({cmd})", flush=True)

    def run():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
            state.update(status="done" if r.returncode == 0 else "failed",
                         output=(r.stdout + r.stderr).strip()[-400:])
        except Exception as e:
            state.update(status="failed", output=str(e))
        print(f"[pixelmon-gui] restart of {name}: {state['status']} {state['output']}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def watch_server():
    """Background check of the render server every few seconds, so pages and polls never wait on a dead one.
    Also catches a ComfyUI that answers but no longer runs anything (its prompt worker died): prompts
    waiting, nothing running, and the same one stuck at the head of the queue for STUCK_AFTER seconds."""
    head = None                                     # (server, first waiting prompt id, first seen)
    down_since, last_restart = None, {}
    while True:
        name = render_server()
        up = server_up(name)
        if up and SERVER_STATE["up"] is False:
            _ADV_CACHE["v"] = None                  # it came back: read its model lists again
        down_since = None if up else (down_since or time.time())
        stuck = None
        q = server_queue(name) if up else None
        if q and not q["queue_running"] and q["queue_pending"]:
            first = min(q["queue_pending"], key=lambda p: p[0])[1]
            if not head or head[:2] != (name, first):
                head = (name, first, time.time())
            elif time.time() - head[2] >= STUCK_AFTER:
                stuck = {"pending": len(q["queue_pending"]), "since": head[2]}
        else:
            head = None
        poisoned = server_poisoned(name) if up and not stuck else None
        SERVER_STATE.update(server=name, url=server_url(name), up=up, stuck=stuck or poisoned)
        why = ("is stuck: prompts waiting, nothing running" if stuck
               else f"fails every prompt ({poisoned['error']})" if poisoned
               else f"hasn't answered for {DOWN_AFTER}s" if down_since and time.time() - down_since >= DOWN_AFTER else None)
        gpu_work = Handler.jobs is not None and Handler.jobs.busy()   # nothing to render: don't restart anyone's ComfyUI
        if why and gpu_work and restart_command(name) and time.time() - last_restart.get(name, 0) >= RESTART_EVERY:
            last_restart[name] = time.time()           # a restart empties its history, so this can't loop
            head = None
            restart_server(name, why)
        time.sleep(4)


def server_queue(name=None):
    """The render server's ComfyUI /queue ({queue_running, queue_pending}), or None."""
    url = server_url(name)
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/queue", timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def server_poisoned(name=None):
    """{'prompt': id, 'error': msg, 'pending': n} when the newest prompt the server finished died of one of
    the POISONED errors (it will fail every prompt from now on), else None."""
    url = server_url(name)
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/history?max_items=1", timeout=3) as r:
            hist = json.load(r)
    except Exception:
        return None
    for pid, h in hist.items():
        for kind, msg in (h.get("status") or {}).get("messages") or []:
            err = str((msg or {}).get("exception_message") or "") if kind == "execution_error" else ""
            if any(p in err for p in POISONED):
                return {"prompt": pid, "error": err.strip()[:120], "pending": 0}
    return None


def server_stuck():
    """{'pending': n, 'since': t} while the current render server's queue isn't moving, else None."""
    return SERVER_STATE["stuck"] if SERVER_STATE["server"] == render_server() else None


def server_restart():
    """The last automatic restart (any server) while it's recent, so the page can say so."""
    r = SERVER_STATE["restart"]
    return dict(r, auto=bool(restart_command(render_server()))) if r and time.time() - r["at"] < 600 else None


def server_is_up():
    """The watcher's last answer for the current render server (asks directly if it hasn't looked yet)."""
    if SERVER_STATE["server"] == render_server() and SERVER_STATE["up"] is not None:
        return SERVER_STATE["up"]
    return server_up()


def server_up(name=None):
    url = server_url(name)
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


def combo_count(ps):
    """how many pictures a combo sweep makes (dither 'none' once per grid: its amount changes nothing)"""
    if not isinstance(ps, dict):
        return 0
    g = len(ps.get("angle_grid") or []) or 1
    d = ps.get("dither") or []
    a = len(ps.get("dither_amount") or []) or 1
    return g * ((1 if "none" in d else 0) + len([x for x in d if x != "none"]) * a) if d else g * a


def depth_argv(d, src):
    """The --depth flags for the Steering tab's depth slot ({ref, strength, start, end, raw, mask_bg})."""
    st, en, sg = float(d.get("start", 0.0)), float(d.get("end", 0.65)), float(d.get("strength", 0.7))
    if not (0 <= st < en <= 1 and 0 <= sg <= 2):
        raise ValueError("depth: strength must be 0..2, and start before end within 0..1")
    out = ["--depth", src, "--depth-strength", f"{sg:g}"]
    if (st, en) != (0.0, 0.65):
        out += ["--depth-start", f"{st:g}", "--depth-end", f"{en:g}"]
    if d.get("raw"):
        out.append("--depth-raw")
    elif d.get("mask_bg"):
        out.append("--depth-mask-bg")
    return out


def build_argv(p, steer_dir=None, steer_count=0, init=None, control=None, mask=None, depth=None, turn=None):
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
    argv = [PIXELMON, prompt, "--server", servers_for(p), "--no-open", "--show-prompt"]
    art = bool(p.get("art"))
    if art:
        argv.append("--art")
    elif p.get("raw") and not (mask and init) and not p.get("post_sweep"):   # inpaint / sweeps are pixel-art only
        argv.append("--raw")
    tile = str(p.get("tile") or "")
    if tile in ("both", "x", "y"):
        argv += ["--tile"] + ([] if tile == "both" else [tile])
    lora = str(p.get("lora") or "")
    if lora == "(none)":
        argv.append("--no-lora")
    elif lora:
        argv += ["--lora", lora]
        s = num("lora_strength", float, 0.0, 2.0)
        if s is not None:
            argv += ["--lora-strength", f"{s:g}"]
    exact = bool(p.get("exact"))
    if exact:              # the edited full prompt: sent word for word (an empty negative stays empty)
        argv += ["--exact-prompt", "--negative", str(p.get("negative") or "")]
    elif p.get("negative"):
        argv += ["--negative", str(p["negative"])]
    styles = [] if exact else [s for s in (p.get("styles") or []) if re.fullmatch(r"[\w-]+", s)]
    if styles:
        argv += ["--style", ",".join(styles)]
    out, size = dims("out"), dims("size")
    if out:
        argv += ["--out", out]
    if size:
        argv += ["--size", size]
    if art:                                      # art mode: the palette tints the painting (no pixelation)
        pal = str(p.get("palette") or "none")
        ps = num("palette_strength", float, 0.0, 1.0)
        if pal != "none" and ps != 0:
            argv += ["--palette", pal]
            if ps is not None:
                argv += ["--palette-strength", f"{ps:g}"]
            dither = str(p.get("dither") or "none")
            if dither != "none":
                argv += ["--dither", dither]
                da = num("dither_amount", float, 0.0, 1.0)
                if da is not None:
                    argv += ["--dither-amount", f"{da:g}"]
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
            sm = str(p.get("snap_method") or "unfake")
            if sm not in ("unfake", "snapper"):
                raise ValueError(f"unknown snap method {sm!r}")
            argv += ["--snap-pixels", "--snap-method", sm]
        if p.get("transparent"):
            argv.append("--transparent")
        if p.get("fit_subject"):
            argv.append("--fit-subject")
        if str(p.get("color_match") or "oklab") == "redmean":
            argv += ["--color-match", "redmean"]
        if p.get("no_sprite_suffix"):
            argv.append("--no-sprite-suffix")
        ps = str(p.get("pixel_size") or "").lower()
        if ps:
            if not re.fullmatch(r"\d{1,2}x\d{1,2}", ps):
                raise ValueError(f"bad pixel size {ps!r}; use WxH like 2x1")
            argv += ["--pixel-size", ps]
        tl = num("thin_lines", int, 0, 12)
        if tl:
            argv += ["--thin-lines", str(tl)]
        pa = num("pixel_angles", float, 0.0, 3.0)
        if pa:
            argv += ["--pixel-angles", f"{pa:g}"]
            grid = str(p.get("angle_grid") or "pixel")
            if grid not in ("pixel", "square", "diagonal", "isometric", "hex", "triangle"):
                raise ValueError(f"unknown angle grid {grid!r}")
            if grid != "pixel":
                argv += ["--angle-grid", grid]
    if turn:                                     # 🧊 3D: a model of the picture, or rotation sprites from it
        t = p.get("turnaround") or {}
        if t.get("mode") == "mesh":
            argv += ["--mesh", turn]
        else:
            n, e = int(t.get("angles") or 8), float(t.get("elev") or 0)
            if not 2 <= n <= 64 or not -10 <= e <= 80:
                raise ValueError("turnaround: 2..64 angles, -10..80° elevation")
            argv += ["--turnaround", turn, "--angles", str(n), "--elev", f"{e:g}"]
        if t.get("ref_path") and t.get("mode") != "mesh":
            argv += ["--steer", t["ref_path"]]
        shape = str(t.get("shape") or "").strip().lower()
        if shape and not turn.lower().endswith((".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx")):   # 📐 built from the outline: already low-poly + textured
            if not re.fullmatch(r"(extrude(:[0-9.]+(,[0-9.]+:[0-9.]+)*)?|lathe(:\d+)?)", shape):
                raise ValueError("shape: extrude[:DEPTH[,SHARE:DEPTH…]] or lathe[:SEGMENTS], like extrude:0.8")
            argv += ["--shape", shape]
            t = {**t, "lowpoly": 0, "texture": ""}
        lp = int(t.get("lowpoly") or 0)
        if lp:
            if not 50 <= lp <= 100000:
                raise ValueError("low-poly: 50..100000 faces")
            argv += ["--lowpoly", str(lp)]
        blender = load_setup().get("editor3d") or ""
        ed = editor3d({"editor3d": blender}) if blender else None
        if ed and ed[2]:                         # the Blender set in 🛠 Setup › 3D editor renders the views too
            argv += ["--blender", blender]
        tx = str(t.get("texture") or "")
        if tx:                                   # 🎨 a clean, textured copy of the model
            argv.append("--texture")
            if tx != "auto":
                if not 0 <= float(tx) <= 80:
                    raise ValueError("texture: the picture's height is 0..80°")
                argv += ["--texture-elev", f"{float(tx):g}"]
    fit = fit_target(p)
    if p.get("cutout") or fit:                   # ✂ BiRefNet cut-out (+ fit into an old sprite's footprint)
        if p.get("transparent"):
            raise ValueError("✂ cut out replaces transparent bg: pick one")
        if p.get("tile"):
            raise ValueError("✂ cut out doesn't go with seamless tiles")
        argv.append("--cutout")
        if p.get("cutout_key"):
            argv.append("--cutout-key")
        if fit:
            sc = int((p.get("fit") or {}).get("scale") or 0)
            if not 0 <= sc <= 16:
                raise ValueError("fit scale must be 1..16")
            argv += ["--fit-to", fit] + (["--fit-scale", str(sc)] if sc else [])
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
    if depth:
        argv += depth_argv(p.get("depth") or {}, depth)
    if init:
        d = num("evolve_denoise", float, 0.01, 1.0, 0.45)
        argv += ["--init", init, "--denoise", f"{d:g}"]
    if mask and init:
        mm = str((p.get("adv") or {}).get("mask_mode") or (p.get("inpaint") or {}).get("mode") or "fill")
        if mm not in ("fill", "blend"):
            raise ValueError(f"unknown mask mode {mm!r}")
        argv += ["--mask", mask, "--mask-mode", mm]
    if control:
        c = p.get("control") or {}
        mode = str(c.get("mode") or "canny")
        if mode not in ("canny", "tile"):
            raise ValueError(f"unknown control mode {mode!r}")
        adv = p.get("adv") if isinstance(p.get("adv"), dict) else {}
        st, en = float(c.get("strength", 0.8)), float(adv.get("control_end") or c.get("end", 0.8))
        if not (0 <= st <= 2 and 0 < en <= 1):
            raise ValueError("control strength must be 0..2 and end in (0, 1]")
        argv += ["--control", control, "--control-mode", mode, "--control-strength", f"{st:g}", "--control-end", f"{en:g}"]
    extra = adv_argv(p.get("adv"), art, bool(control), bool(mask and init), str((p.get("inpaint") or {}).get("mode") or "fill"),
                     rough=bool((p.get("inpaint") or {}).get("rough") or (p.get("inpaint") or {}).get("sketch")))
    extra = [x for i, x in enumerate(extra) if not (x == "--control-end" or (i and extra[i - 1] == "--control-end"))]
    if "--custom-hex" in extra and "--palette" in argv:              # custom colors = the Custom palette
        argv[argv.index("--palette") + 1] = "Custom"
    argv += extra
    seed = num("seed", int, -1, 2**31 - 1, -1)
    if seed is not None and seed >= 0:
        argv += ["--seed", str(seed)]
    ps = p.get("post_sweep")
    if ps:                                       # combo sweep: one render, every combination of post-processing
        if not isinstance(ps, dict):
            raise ValueError("bad combo sweep")
        parts = []
        for f in ("angle_grid", "dither", "dither_amount"):
            vals = [str(v).strip() for v in (ps.get(f) or []) if re.fullmatch(r"[\w.+-]{1,32}", str(v).strip())]
            if vals:
                parts.append(f"{f}={','.join(vals)}")
        if not parts:
            raise ValueError("tick at least one grid, dither or amount for the combo sweep")
        argv += ["--post-sweep", ";".join(parts)]
        p = {**p, "n": 1}
    n = num("n", int, 1, 32, 1)
    if n and n > 1:
        argv += ["-n", str(n)]
    name = re.sub(r"[^\w-]+", "-", str(p.get("name") or "")).strip("-")[:48]
    if name:
        argv += ["--name", name]
    return argv


def depth_preview(params, gallery):
    """Run pixelmon --depth-only with the current settings: the exact depth map the sampler would get."""
    d = params.get("depth") or {}
    src = ref_path(str(d.get("ref") or ""))
    root = os.path.join(gallery, "_preview")
    rel = time.strftime("%Y%m%d-%H%M%S-") + f"{random.randrange(16**4):04x}-depth"
    out = os.path.join(root, rel)
    os.makedirs(out, exist_ok=True)
    p = {k: v for k, v in params.items() if k not in ("steer", "parent", "control", "inpaint", "lab_init", "post_sweep")}
    argv = build_argv({**p, "n": 1}, depth=src) + ["--depth-only", "--output-to", out]
    r = subprocess.run(argv, cwd=out, capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1"))
    maps = sorted(glob.glob(os.path.join(out, "depth", "*_depth.png")))
    for old in sorted(x for x in os.listdir(root) if x.endswith("-depth") and x != rel)[:-8]:
        shutil.rmtree(os.path.join(root, old), ignore_errors=True)        # keep the last few previews
    if r.returncode or not maps:
        tail = [ANSI.sub("", ln) for ln in (r.stdout + r.stderr).strip().splitlines()[-4:]]
        raise ValueError("depth preview failed: " + (" / ".join(tail) or f"exit {r.returncode}"))
    f = os.path.relpath(maps[-1], gallery)
    return {"url": "/files/" + urllib.parse.quote(f), "size": image_size(maps[-1]),
            "command": shjoin(["pixelmon"] + argv[1:])}


def command_preview(p, jobs):
    """The pixelmon command RENDER would run for these form params (Copy CLI), built by build_argv itself so
    it can't drift from what a job runs. The files a job copies into its folder are shown where they are now."""
    p = dict(p)
    for k in ("group", "label", "form", "set", "preview"):
        p.pop(k, None)
    srcs = [ref_path(str(r)) for r in (p.get("steer") or [])][:32]
    init = control = mask = depth = turn = None
    parent = p.get("parent") or None
    if parent:
        psrc = jobs.gallery_path(parent)
        if p.get("evolve_init"):
            init = psrc
        if float(p.get("evolve_steer") or 0) > 0:
            srcs = [psrc] + srcs
            p.update(steer_strength=p["evolve_steer"], steer_weight_type=p.get("evolve_weight_type") or "style and composition",
                     steer_start=p.get("evolve_steer_start", 0.0), steer_end=p.get("evolve_steer_end", 1.0))
    lab = (p.get("control") or {}).get("file") or p.get("lab_init") or (p.get("inpaint") or {}).get("file")
    if lab:
        src = lab_path(lab)
        control = src if p.get("control") else None
        if p.get("lab_init") or p.get("inpaint"):
            init = src
            p["evolve_denoise"] = p.get("lab_denoise", 0.7)
        if p.get("inpaint"):
            mask = "mask.png"
    if (p.get("depth") or {}).get("ref"):
        depth = ref_path(p["depth"]["ref"])
    if (p.get("turnaround") or {}).get("src"):
        turn = jobs.gallery_path(p["turnaround"]["src"])
    steer_dir = None
    if len(srcs) == 1:
        steer_dir = srcs[0]                              # one image: --steer takes a file too
    elif srcs:
        folders = {os.path.dirname(s) for s in srcs}
        whole = len(folders) == 1 and len([f for f in os.listdir(next(iter(folders))) if f.lower().endswith(IMG_EXT)]) == len(srcs)
        steer_dir = next(iter(folders)) if whole else "<a-folder-with-the-selected-refs>"
    argv = build_argv(p, steer_dir, len(srcs), init, control, mask, depth, turn)
    return shjoin(["pixelmon"] + argv[1:])


def scan_outputs(d, job):
    """Fill job outputs / models / depth_map / tiled from what is really in its folder (the log may miss some)."""
    found = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.gif"))
                   if not os.path.basename(f).startswith(("parent.", "input", "mask."))
                   and "_preview_" not in os.path.basename(f) and "_tiled_" not in os.path.basename(f)
                   and "_cutmask_" not in os.path.basename(f)
                   and not f.endswith("_texture.png"))   # kept parent / LAB input / seam checks / a model's texture aren't output
    job["outputs"] = list(job.get("outputs") or [])
    known = {o["file"] for o in job["outputs"]}
    for f in found:
        if f not in known:
            m = re.search(r"_s(\d+)_", f)
            job["outputs"].append({"seed": int(m.group(1)) if m else None, "file": f})
    job["outputs"] = [o for o in job["outputs"] if o["file"] in found]
    models = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.glb"))
                    if not os.path.basename(f).startswith("input"))   # not the job's copy of its source
    if models:                                   # 🧊 3D models the job made
        job["models"] = models
    dm = sorted(glob.glob(os.path.join(d, "depth", "*_depth.png")))
    if dm:                                       # --depth: the map the sampler actually got
        job["depth_map"] = "depth/" + os.path.basename(dm[-1])
    for o in job["outputs"]:                     # --tile: the 3x3 seam check saved beside each texture
        t = re.sub(r"_(sprite|art)_(\d+_\.png)$", r"_tiled_\2", o["file"])
        if t != o["file"] and os.path.isfile(os.path.join(d, t)):
            o["tiled"] = t
    return job


def recover_interrupted(gallery):
    """Jobs a killed GUI left saying queued / running: mark them interrupted and keep whatever they finished,
    so those pictures (and Sets takes) show up. Returns how many were fixed."""
    n = 0
    for jf in glob.glob(os.path.join(gallery, "*", "job.json")):
        job = _read_json(jf, None)
        if not isinstance(job, dict) or job.get("status") not in ("queued", "running"):
            continue
        scan_outputs(os.path.dirname(jf), job)
        job["status"] = "interrupted"
        job["error"] = job.get("error") or "pixelmon-gui stopped while this was rendering"
        job["finished"] = job.get("finished") or os.path.getmtime(jf)
        _write_json(jf, job)
        n += 1
    return n


CANCEL_GRACE = 8         # seconds pixelmon gets after SIGTERM to clear its prompts off ComfyUI before it's killed
# lines that say why a render failed, most telling first (pixelmon's own messages, then ComfyUI's, then Python's)
ERROR_LINES = [re.compile(x) for x in (
    r"Render failed on the server|render failed —|ComfyUI rejected|rejected the request|returned no images|restarted and lost",
    r"[Cc]ouldn't reach|unreachable|Timed out|upload failed|no reachable servers",
    r"pixelmon(\.py)?: error:|^\s*--[\w-]+: |not found|no such file",
    r"^\w*(Error|Exception)\b.*:")]


def job_error(log, rc):
    """Why a job failed, in one line: the CLI's (or the render node's) own message, plus the last line when
    that adds something — instead of a bare exit code."""
    lines = [ln.strip() for ln in log if ln.strip() and not ln.startswith("⛔")]
    last = lines[-1] if lines else f"pixelmon exited with code {rc}"
    for i, ln in enumerate(lines):               # the capability check: "<box> can't render this:" + • problems + the fix
        if "can't render this" in ln:
            rest = lines[i + 1:i + 12]
            probs = [x.lstrip("• ").strip() for x in rest if x.startswith("•")]
            fix = next((x for x in rest if not x.startswith("•")), "")
            return (ln.rstrip(":") + ": " + "; ".join(probs[:3]) + (f" (+{len(probs) - 3} more)" if len(probs) > 3 else "")
                    + (f"  —  {fix}" if fix else ""))[:600]
    why = ""
    for rx in ERROR_LINES:
        why = next((ln for ln in lines if rx.search(ln)), "")
        if why:
            break
    msg = why + ("  —  " + last if why and last not in why and why not in last else "") if why else last
    return msg[:600]


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
        """Queue a render. Everything is validated before the job counts: a bad parameter (or a missing file)
        leaves nothing behind, not even the job folder the copies were going into."""
        jid = time.strftime("%Y%m%d-%H%M%S-") + f"{random.randrange(16**4):04x}"
        try:
            return self._add(params, group, label, jid)
        except Exception:
            for d in (os.path.join(self.gallery, jid), os.path.join(self.gallery, "_preview", jid)):
                if os.path.isdir(d) and not os.path.exists(os.path.join(d, "job.json")):
                    shutil.rmtree(d, ignore_errors=True)
            raise

    def _add(self, params, group, label, jid):
        ref = params.get("set")
        if ref is not None:                          # a Sets render: which set and sprite the takes belong to
            if not (isinstance(ref, dict) and re.fullmatch(r"[\w-]{1,64}", str(ref.get("id") or ""))
                    and setsmod.key_ok(ref.get("key"))):
                raise ValueError("bad set reference")
            params = dict(params, set={"id": ref["id"], "key": ref["key"]})
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
        # LAB live previews live in gallery/_preview/ (not listed in the gallery; only the newest are kept)
        dirname = f"_preview/{jid}" if params.get("preview") else jid
        jdir = os.path.join(self.gallery, dirname)
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
        control = mask = None
        if params.get("inpaint"):
            params["lab_init"] = params.get("lab_init") or (params["inpaint"].get("file"))
        lab = (params.get("control") or {}).get("file") or params.get("lab_init")
        if lab:                                      # LAB: the job keeps its own copy of the input image
            src = lab_path(lab)
            os.makedirs(jdir, exist_ok=True)
            adj = params.get("lab_adjust") or {}
            stroke_adj = None
            if params.get("lab_adjust_scope") == "strokes" and (params.get("inpaint") or {}).get("sketch"):
                # tune only the 🎨 strokes: the picture keeps just the crop, the tones go to the strokes
                stroke_adj = {k: v for k, v in adj.items() if k != "crop"}
                adj = {"crop": adj.get("crop")} if adj.get("crop") else {}
            if adjust_is_default(adj):
                kept = os.path.join(jdir, "input" + os.path.splitext(src)[1].lower())
                shutil.copy2(src, kept)
            else:                                    # tuned source: the job keeps both, uses the tuned one
                shutil.copy2(src, os.path.join(jdir, "input-original" + os.path.splitext(src)[1].lower()))
                kept = os.path.join(jdir, "input.png")
                adjust_image(src, adj).save(kept)
            if params.get("control"):
                control = kept
            if params.get("lab_init"):
                init = kept
                params["evolve_denoise"] = params.get("lab_denoise", 0.7)
            if (params.get("inpaint") or {}).get("keep_colors"):
                # lock the edit to the picture's own colors (outside the mask is pasted back anyway)
                params["adv"] = dict(params.get("adv") or {}, custom_hex=" ".join(picture_palette(kept)))
            if params.get("inpaint"):                # the painted mask, as sent (white = redraw)
                from PIL import Image, ImageChops

                def png(data, what):
                    raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
                    if raw[:8] != b"\x89PNG\r\n\x1a\n":
                        raise ValueError(f"{what} must be a PNG")
                    return Image.open(io.BytesIO(raw))
                data, sk = str(params["inpaint"].get("mask") or ""), str(params["inpaint"].get("sketch") or "")
                if not data and not sk:
                    raise ValueError("paint a mask (or 🎨 colors) first")
                size = Image.open(init).size if init else None
                m = png(data, "mask").convert("L") if data else Image.new("L", size, 0)
                if size and m.size != size:
                    m = m.resize(size, Image.NEAREST)
                sketch = None
                if sk:                                   # 🎨 color strokes: they count as mask too
                    sketch = png(sk, "color sketch").convert("RGBA")
                    if size and sketch.size != size:
                        sketch = sketch.resize(size, Image.NEAREST)
                    if stroke_adj and not adjust_is_default(stroke_adj):
                        sketch = adjust_strokes(sketch, stroke_adj)
                    m = ImageChops.lighter(m, sketch.getchannel("A").point(lambda v: 255 if v > 16 else 0))
                mask = os.path.join(jdir, "mask.png")
                m.save(mask)
                if sketch is not None and init:          # paint the strokes into the picture: the rough version of the new thing
                    base_img = Image.open(init).convert("RGBA")
                    params["inpaint"]["rough"] = ""      # your colors are the paint-in; no automatic color on top
                    init = os.path.join(jdir, "input-roughed.png")
                    Image.alpha_composite(base_img, sketch).convert("RGB").save(init)
                    if control == kept:
                        control = init
                rough = str(params["inpaint"].get("rough") or "")
                if re.fullmatch(r"#[0-9a-fA-F]{6}", rough) and init:
                    # "paint it in first": a flat blob of the new thing's color where the mask is, so the edit reshapes
                    # it instead of redrawing what was there (a masked eye otherwise comes back as an eye)
                    from PIL import Image
                    base_img = Image.open(init).convert("RGB")
                    m = Image.open(mask).convert("L").resize(base_img.size, Image.NEAREST).point(lambda v: 255 if v > 127 else 0)
                    rgb = tuple(int(rough[i:i + 2], 16) for i in (1, 3, 5))
                    init = os.path.join(jdir, "input-roughed.png")
                    Image.composite(Image.new("RGB", base_img.size, rgb), base_img, m).save(init)
                    if control == kept:              # the outline guide follows the painted-in shape, not the old eye
                        control = init
        steer_dir = None
        if srcs:
            steer_dir = os.path.join(jdir, "steer")
            os.makedirs(steer_dir, exist_ok=True)
            for i, src in enumerate(srcs):           # numbered so same-named files can't collide
                os.symlink(src, os.path.join(steer_dir, f"{i:02d}_{os.path.basename(src)}"))
        depth = None
        if (params.get("depth") or {}).get("ref"):   # depth lock: the run keeps its own copy, next to steer/
            dsrc = ref_path(params["depth"]["ref"])
            os.makedirs(os.path.join(jdir, "depth"), exist_ok=True)
            depth = os.path.join(jdir, "depth", "source" + os.path.splitext(dsrc)[1].lower())
            shutil.copy2(dsrc, depth)
        turn = None
        if (params.get("turnaround") or {}).get("src"):    # 🧊 3D: the job keeps its own copy of the picture / model
            tsrc = self.gallery_path(params["turnaround"]["src"])
            os.makedirs(jdir, exist_ok=True)
            turn = os.path.join(jdir, "input-turn" + os.path.splitext(tsrc)[1].lower())
            shutil.copy2(tsrc, turn)
            if params["turnaround"].get("ref"):     # a model's turnaround steers from the picture it was made from
                rsrc = self.gallery_path(params["turnaround"]["ref"])
                ref = os.path.join(jdir, "input-ref" + os.path.splitext(rsrc)[1].lower())
                shutil.copy2(rsrc, ref)
                params["turnaround"]["ref_path"] = ref
        argv = build_argv(params, steer_dir, len(srcs), init, control, mask, depth, turn)   # validate before queueing
        export = export_target(params)
        job = {"id": jid, "params": sent, "argv": argv, "command": shjoin(["pixelmon"] + argv[1:]),
               "status": "queued", "log": [], "outputs": [], "full_prompt": {}, "group": group, "label": label,
               "total": combo_count(params.get("post_sweep")) or (
                   (1 if params["turnaround"].get("mode") == "mesh" else int(params["turnaround"].get("angles") or 8))
                   if turn else int(params.get("n") or 1)), "export": export, "exported": None,
               "created": time.time(), "started": None, "finished": None, "dir": dirname}
        with self.lock:
            self.jobs[jid] = job
            self.order.append(jid)
        self.q.put(jid)
        if params.get("preview"):
            self._prune_previews()
        return job

    def _prune_previews(self, keep=40):
        """Delete all but the newest `keep` preview folders (never one that's queued or running)."""
        root = os.path.join(self.gallery, "_preview")
        with self.lock:
            busy = {j["dir"].split("/", 1)[-1] for j in self.jobs.values() if j["status"] in ("queued", "running")}
        dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) if os.path.isdir(root) else []
        for d in dirs[:-keep]:
            if d not in busy:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

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
                self._save_if_started(job)
            elif job["status"] == "running" and self.current == jid and self.proc:
                job["status"] = "cancelled"
                job["log"].append("⛔ cancelled — pixelmon is taking its prompts off the render server's queue")
                self._stop(self.proc)
            return True

    def _save_if_started(self, job):
        """A queued job whose folder already holds copies (steer refs, a LAB input) records that it was
        cancelled, so the folder isn't a mystery; one that never touched disk leaves nothing."""
        if os.path.isdir(os.path.join(self.gallery, job["dir"])):
            job["finished"] = time.time()
            self._save(job)

    def all(self):
        """Every job this session (Sets' pending counts need all of them, not the last 60 the page gets)."""
        with self.lock:
            return [dict(self.jobs[j], log=[]) for j in self.order]

    def busy(self):
        """Is there GPU work queued or running?"""
        with self.lock:
            return any(j["status"] in ("queued", "running") for j in self.jobs.values())

    @staticmethod
    def _stop(proc, grace=CANCEL_GRACE):
        """SIGTERM pixelmon: it removes its own prompts from ComfyUI's queue and interrupts only the one it's
        running, then exits. Give it `grace` seconds for that before it's killed (never blocks the caller)."""
        proc.terminate()

        def reap():
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
        threading.Thread(target=reap, daemon=True).start()

    def clear(self, stop_running=False):
        """Clear the Queue tab: cancel waiting jobs, forget finished ones (their files stay in the
        gallery). The running job keeps going unless stop_running."""
        cancelled = removed = 0
        with self.lock:
            for jid in list(self.order):
                job = self.jobs[jid]
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                    self._save_if_started(job)
                    cancelled += 1
                elif job["status"] == "running":
                    if stop_running and self.current == jid and self.proc:
                        job["status"] = "cancelled"
                        self._stop(self.proc)
                        cancelled += 1
                    continue                     # it drops off the list once it has stopped
                self.order.remove(jid)
                del self.jobs[jid]
                removed += 1
        return {"cancelled": cancelled, "removed": removed}

    def abort(self, clear_server=False):
        """⛔ CANCEL AND ABORT: cancel every waiting job and stop the running one. pixelmon, sent SIGTERM, takes its
        OWN prompts off the render server's queue and interrupts only the one it's running — other tools' prompts
        on a shared ComfyUI are left alone. clear_server=True (an explicit opt-in) also empties every render
        server's whole queue and interrupts it, for a box that's stuck on someone else's work."""
        servers = set(render_server().split(","))
        with self.lock:
            proc, stopped = None, None
            cancelled = 0
            for jid in self.order:
                job = self.jobs[jid]
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                    self._save_if_started(job)
                    cancelled += 1
                elif job["status"] == "running":
                    argv = job["argv"]
                    if "--server" in argv:               # the server this job was sent to, even if Setup changed since
                        servers.update(argv[argv.index("--server") + 1].split(","))
                    if self.current == jid and self.proc:
                        job["status"] = "cancelled"
                        job["log"].append("⛔ aborted — pixelmon is taking its prompts off the render server's queue")
                        proc, stopped = self.proc, job
                        proc.terminate()
                        cancelled += 1
        said, killed = "", False
        if proc:                                         # let pixelmon clean up and exit, so it can't send one more prompt
            try:
                proc.wait(timeout=CANCEL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                killed = True
            time.sleep(0.2)                              # the worker reads pixelmon's last lines
            with self.lock:
                said = next((ln for ln in reversed(stopped["log"]) if ln.strip() and "aborted —" not in ln), "")
        comfy = {}
        if clear_server:
            for name in sorted(s.strip() for s in servers if s.strip()):
                url = server_url(name)
                if not url or url in comfy.values():
                    continue
                try:
                    for path, body in (("/queue", {"clear": True}), ("/interrupt", {})):
                        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                                     headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(req, timeout=4).close()
                    comfy[name] = url
                except Exception as e:
                    comfy[name] = f"unreachable ({getattr(e, 'reason', e)})"
        return {"cancelled": cancelled, "servers": comfy, "cleared": bool(clear_server),
                "stopped": {"id": stopped["id"], "said": said[:300], "killed": killed,
                            "servers": sorted(s for s in servers if s)} if stopped else None}

    def snapshot(self):
        with self.lock:
            return [dict(self.jobs[j], log=self.jobs[j]["log"][-40:]) for j in self.order[-60:]]

    def _save(self, job):
        d = os.path.join(self.gallery, job["dir"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in job.items() if k != "log"}, f, indent=1)
        if job["log"]:                               # the whole run's output, kept beside the pictures
            with open(os.path.join(d, "log.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(job["log"]) + "\n")

    def _worker(self):
        while True:
            jid = self.q.get()
            with self.lock:
                job = self.jobs.get(jid)
                if not job or job["status"] == "cancelled":    # cleared from the queue, or cancelled
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
                            o = {"seed": int(m.group(1)), "file": os.path.basename(m.group(2).strip())}
                            lab = re.search(r"✅\s*\[\d+/\d+\]\s+(\S.*?)\s+seed=", line)   # a combo sweep names each picture
                            if lab and job["params"].get("post_sweep"):
                                o["label"] = lab.group(1).strip()
                            ang = re.search(r"_a(\d{3})_", o["file"]) if job["params"].get("turnaround") else None
                            if ang:                  # 🧊 turnaround: each picture is an angle
                                o["label"] = f"{int(ang.group(1))}°"
                            job["outputs"].append(o)
                rc = self.proc.wait()
            except Exception as e:
                rc = -1
                job["log"].append(f"GUI error: {e}")
            with self.lock:
                scan_outputs(d, job)                 # trust the folder over log parsing (covers moved/renamed files)
                if job["status"] != "cancelled":
                    made = bool(job["outputs"] or job.get("models"))
                    job["status"] = "done" if made else "failed"
                    if rc != 0:                      # pixelmon exits 1 when some renders of the job failed: keep what it made
                        job["error"] = job_error(job["log"], rc)
                        m = next((re.search(r"(\d+) of (\d+) (?:render|job)\(s\)", ln) for ln in reversed(job["log"])
                                  if re.search(r"(\d+) of (\d+) (?:render|job)\(s\)", ln)), None)
                        if made:
                            job["failed"] = int(m.group(1)) if m else max(1, int(job.get("total") or 1) - len(job["outputs"]))
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


# ---------------- Analyze tab: images sent for comparison (copies, so the originals can come and go) ----------------
def analysis_dir():
    return os.path.join(GALLERY_HOME, "analysis")


def analysis_path(name, exports=False):
    base = os.path.join(analysis_dir(), "exports") if exports else analysis_dir()
    full = os.path.realpath(os.path.join(base, os.path.basename(str(name or ""))))
    if not full.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(full):
        raise ValueError("that analysis image is gone")
    return full


def analysis_items():
    items = _read_json(os.path.join(analysis_dir(), "items.json"), [])
    return [it for it in items if isinstance(it, dict) and os.path.isfile(os.path.join(analysis_dir(), it.get("file", "")))]


def _analysis_save(items):
    os.makedirs(analysis_dir(), exist_ok=True)
    _write_json(os.path.join(analysis_dir(), "items.json"), items)


def analysis_add(src, label, origin):
    ext = os.path.splitext(src)[1].lower() if src.lower().endswith(IMG_EXT) else ".png"
    stem = safe_name(os.path.splitext(os.path.basename(src))[0], 60)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}__{stem}{ext}"
    os.makedirs(analysis_dir(), exist_ok=True)
    shutil.copy2(src, os.path.join(analysis_dir(), name))
    w, h = image_size(os.path.join(analysis_dir(), name)) or (0, 0)
    items = analysis_items()
    it = {"file": name, "label": str(label or stem)[:120], "from": origin, "added": time.time(), "w": w, "h": h}
    items.append(it)
    _analysis_save(items)
    return it


def analysis_remove(files):
    files = set(files or [])
    keep, n = [], 0
    for it in analysis_items():
        if it["file"] in files:
            try:
                os.remove(os.path.join(analysis_dir(), it["file"]))          # our own copy; the original is untouched
            except OSError:
                pass
            n += 1
        else:
            keep.append(it)
    _analysis_save(keep)
    return n


def _diff_mask(a, b, tol):
    """1 where any channel (RGBA) of a and b differs by more than tol — the same rule as Kaleidotron's compare."""
    from PIL import ImageChops
    m = None
    for ca, cb in zip(a.split(), b.split()):
        d = ImageChops.difference(ca, cb).point(lambda v: 255 if v > tol else 0)
        m = d if m is None else ImageChops.lighter(m, d)
    return m


def analysis_export(files, fmt, diff=True, tol=0, color="#ff00ff", opacity=0.6):
    """The ticked analysis images as layers of one PSD / XCF / ORA / DRAW file (+ a difference layer per image)."""
    import layered
    from PIL import Image
    items = {it["file"]: it for it in analysis_items()}
    files = [f for f in (files or []) if f in items]
    if not files:
        raise ValueError("tick at least one image to export")
    imgs = [Image.open(analysis_path(f)).convert("RGBA") for f in files]
    size = imgs[0].size
    imgs = [im if im.size == size else im.resize(size, Image.NEAREST) for im in imgs]      # everything on the reference's grid
    short = lambda i: f"{i + 1}" + (" ref" if i == 0 else "")
    layers = [{"name": f"{short(i)} {items[f]['label']}"[:60], "img": im} for i, (f, im) in enumerate(zip(files, imgs))]
    if diff and len(imgs) > 1:
        c = str(color or "#ff00ff").lstrip("#")
        rgb = tuple(int(c[k:k + 2], 16) for k in (0, 2, 4)) if re.fullmatch(r"[0-9a-fA-F]{6}", c) else (255, 0, 255)
        for i, im in enumerate(imgs[1:], 1):
            m = _diff_mask(imgs[0], im, max(0, min(255, int(tol or 0))))
            d = Image.new("RGBA", size, rgb + (0,))
            d.putalpha(m)
            layers.append({"name": f"diff 1-{i + 1}", "img": d, "opacity": max(0.0, min(1.0, float(opacity)))})
    out_dir = os.path.join(analysis_dir(), "exports")
    os.makedirs(out_dir, exist_ok=True)
    name = f"analysis-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.{fmt}"
    with open(os.path.join(out_dir, name), "wb") as fh:
        fh.write(layered.write_layered(fmt, layers))
    return {"file": name, "path": os.path.join(out_dir, name), "layers": [l["name"] for l in layers]}


def lab_path(name):
    full = os.path.realpath(os.path.join(LAB, os.path.basename(str(name or ""))))
    if not full.startswith(os.path.realpath(LAB) + os.sep) or not os.path.isfile(full):
        raise ValueError("LAB input image not found")
    return full


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


ADJ_DEFAULTS = {"brightness": 0, "contrast": 0, "gamma": 1.0, "saturation": 100, "hue": 0, "clean": 0,
                "sharpen": 0, "posterize": 0, "gray": False, "invert": False, "crop": None}


def adjust_is_default(adj):
    if not adj:
        return True
    if adj.get("crop"):
        return False
    return all(adj.get(k, v) == v or (isinstance(v, (int, float)) and float(adj.get(k, v)) == float(v))
               for k, v in ADJ_DEFAULTS.items() if k != "crop")


def region_mask(size, region):
    """ "apply to: the selection": the selection's outline ([[x, y], …] as fractions of the whole picture) as an L mask,
    with a hair of feathering so the tuned part doesn't end in a jagged seam."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = size
    m = Image.new("L", size, 0)
    if region and len(region) >= 3:
        ImageDraw.Draw(m).polygon([(float(x) * W, float(y) * H) for x, y in region], fill=255)
        m = m.filter(ImageFilter.GaussianBlur(0.7))
    return m


def adjust_image(path, adj, max_side=None):
    """Source-image tuning for the LAB (PIL only): clean → brightness/contrast → gamma → saturation → hue →
    sharpen → posterize → grayscale → invert. With a `region` (the selection's outline) only the inside is tuned
    (an empty region = nothing selected yet = nothing tuned). Returns an RGB PIL image."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    im = Image.open(path).convert("RGB")
    region = (adj or {}).get("region")
    mask = region_mask(im.size, region) if region is not None else None
    crop = (adj or {}).get("crop")
    if crop:                                    # [x, y, w, h] as fractions of the image — applied first
        x, y, w, h = (min(1.0, max(0.0, float(v))) for v in crop)
        W, H = im.size
        box = (int(x * W), int(y * H), max(int(x * W) + 1, int((x + w) * W)), max(int(y * H) + 1, int((y + h) * H)))
        im = im.crop(box)
        mask = mask.crop(box) if mask is not None else None
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        mask = mask.resize(im.size, Image.LANCZOS) if mask is not None else None
    if mask is None:
        return adjust_tone(im, adj)
    if not mask.getbbox():
        return im
    return Image.composite(adjust_tone(im, adj), im, mask)


def _stroke_color_field(sk):
    """The strokes' colors as a full RGB picture: painted colors where the paint is solid, and — where it's faint
    (soft edges, whose color is unreliable) or empty — the nearby painted colors, so filters never see a fake edge."""
    from PIL import Image, ImageFilter
    try:
        import numpy as np
    except ImportError:
        flat = Image.new("RGB", sk.size, (128, 128, 128))
        flat.paste(sk.convert("RGB"), (0, 0), sk.getchannel("A").point(lambda v: 255 if v >= 64 else 0))
        return flat
    A = np.asarray(sk.getchannel("A"), dtype=np.float32)
    C = np.asarray(sk.convert("RGB"), dtype=np.float32)
    solid = (A >= 64).astype(np.float32)[..., None]
    pm = Image.fromarray((C * solid).clip(0, 255).astype(np.uint8))
    ms = Image.fromarray((solid[..., 0] * 255).astype(np.uint8))
    pmb = np.asarray(pm.filter(ImageFilter.GaussianBlur(6)), dtype=np.float32)
    msb = np.asarray(ms.filter(ImageFilter.GaussianBlur(6)), dtype=np.float32)[..., None] / 255
    fill = np.where(msb > 1e-3, pmb / np.maximum(msb, 1e-3), 128)
    return Image.fromarray(np.where(solid > 0, C, fill).clip(0, 255).astype(np.uint8), "RGB")


def adjust_strokes(sk, adj):
    """The LAB's tuning on the 🎨 strokes layer (RGBA), every slider on everything painted:
    colors — the same steps as the picture (clean → brightness → contrast → gamma → saturation → hue → sharpen →
    posterize → grayscale → invert), with contrast pivoting on mid-grey like the live preview (an almost-empty
    layer has no meaningful average); edges — clean smooths ragged outlines and drops specks, sharpen hardens soft
    edges, posterize steps the soft falloff into bands."""
    from PIL import ImageFilter
    a = dict(ADJ_DEFAULTS, **(adj or {}))
    alpha = sk.getchannel("A")
    rgb = _stroke_color_field(sk)
    c, sh, bits = int(a["clean"]), float(a["sharpen"]), int(a["posterize"])
    if c > 0:                                   # colors: smooth blotchy / blocky paint
        rgb = rgb.filter(ImageFilter.MedianFilter(3 if c < 3 else 5))
        if c >= 2:
            rgb = rgb.filter(ImageFilter.GaussianBlur(0.4 * (c - 1)))
    rgb = adjust_tone(rgb, {"brightness": a["brightness"]})
    ct = float(a["contrast"])
    if ct:                                      # contrast around mid-grey
        f = 1 + ct / 100
        rgb = rgb.point([max(0, min(255, int(round(128 + (i - 128) * f)))) for i in range(256)] * 3)
    rgb = adjust_tone(rgb, {k: a[k] for k in ("gamma", "saturation", "hue", "sharpen", "posterize", "gray", "invert")})
    if c > 0:                                   # edges: smooth the outline, lose stray specks
        alpha = alpha.filter(ImageFilter.MedianFilter(3 if c < 3 else 5))
        if c >= 2:
            alpha = alpha.filter(ImageFilter.GaussianBlur(0.5 * (c - 1)))
    if sh > 0:                                  # edges: harden soft falloff
        k = 1 + 1.5 * sh
        alpha = alpha.point(lambda v: max(0, min(255, int((v - 128) * k + 128))) if v else 0)
    if bits > 0:                                # edges: the soft falloff in as few steps as the colors
        n = 2 ** max(1, min(8, bits)) - 1
        alpha = alpha.point(lambda v: int(round(round(v / 255 * n) / n * 255)))
    rgb.putalpha(alpha)
    return rgb


def adjust_tone(im, adj):
    """The tonal part of the LAB's source tuning (no crop) on an RGB PIL image — used on the picture, or only on
    the 🎨 color strokes when "apply to: only my strokes" is picked."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    a = dict(ADJ_DEFAULTS, **(adj or {}))
    c = int(a["clean"])
    if c > 0:                                   # JPEG clean: median removes blocky noise, then a light blur
        im = im.filter(ImageFilter.MedianFilter(3 if c < 3 else 5))
        if c >= 2:
            im = im.filter(ImageFilter.GaussianBlur(0.4 * (c - 1)))
    if float(a["brightness"]):
        im = ImageEnhance.Brightness(im).enhance(1 + float(a["brightness"]) / 100)
    if float(a["contrast"]):
        im = ImageEnhance.Contrast(im).enhance(1 + float(a["contrast"]) / 100)
    g = float(a["gamma"])
    if abs(g - 1) > 1e-3:
        lut = [min(255, int(255 * (i / 255) ** (1 / g) + 0.5)) for i in range(256)]
        im = im.point(lut * 3)
    if float(a["saturation"]) != 100:
        im = ImageEnhance.Color(im).enhance(float(a["saturation"]) / 100)
    if int(a["hue"]):
        h, s_, v = im.convert("HSV").split()
        shift = int(round(int(a["hue"]) / 360 * 256)) % 256
        h = h.point(lambda x: (x + shift) % 256)
        im = Image.merge("HSV", (h, s_, v)).convert("RGB")
    if float(a["sharpen"]):
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=int(60 * float(a["sharpen"])), threshold=2))
    if int(a["posterize"]):
        im = ImageOps.posterize(im, max(1, min(8, int(a["posterize"]))))
    if a["gray"]:
        im = ImageOps.grayscale(im).convert("RGB")
    if a["invert"]:
        im = ImageOps.invert(im)
    return im


def edge_view(im):
    """Approximation of the Canny edge map the ControlNet 'shape' mode follows (white edges on black)."""
    from PIL import ImageFilter, ImageOps
    e = ImageOps.grayscale(im).filter(ImageFilter.GaussianBlur(1)).filter(ImageFilter.FIND_EDGES)
    return e.point(lambda x: 255 if x > 24 else 0).convert("RGB")


def list_lab():
    os.makedirs(LAB, exist_ok=True)
    fs = sorted((f for f in os.listdir(LAB) if f.lower().endswith(IMG_EXT)),
                key=lambda f: -os.path.getmtime(os.path.join(LAB, f)))
    return [{"file": f, "size": image_size(os.path.join(LAB, f))} for f in fs[:60]]


def board_dir(name):
    name = safe_name(name or "")
    if not name:
        raise ValueError("board needs a name")
    return os.path.join(BOARDS, name)


GALLERY_HOME = os.path.dirname(PRESETS)                           # ~/pixelmon-gallery
BACKUPS = os.path.join(GALLERY_HOME, "backups")
BACKUP_SKIP = {"backups", "preset-candidates"}                    # old backups and seed-picking scratch


def make_backup(keyword, gallery):
    """Zip everything in ~/pixelmon-gallery (renders, presets, corkboards, LAB inputs…) into
    backups/pixelmon-gallery-<keyword>-<YYYY-MM-DD>.zip. Returns (path, files, bytes)."""
    kw = re.sub(r"[^\w-]+", "-", str(keyword or "").strip()).strip("-")[:40] or "backup"
    os.makedirs(BACKUPS, exist_ok=True)
    base = f"pixelmon-gallery-{kw}-{time.strftime('%Y-%m-%d')}"
    path, i = os.path.join(BACKUPS, base + ".zip"), 2
    while os.path.exists(path):
        path, i = os.path.join(BACKUPS, f"{base}-{i}.zip"), i + 1
    roots = [GALLERY_HOME] + ([gallery] if not os.path.realpath(gallery).startswith(os.path.realpath(GALLERY_HOME) + os.sep) else [])
    n = skipped = 0
    try:
        with zipfile.ZipFile(path + ".part", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for root in roots:
                top = os.path.basename(os.path.normpath(root))
                for d, dirs, files in os.walk(root):
                    rel = os.path.relpath(d, root)
                    if rel == ".":
                        dirs[:] = [x for x in dirs if x not in BACKUP_SKIP]
                    for f in files:
                        if f.endswith(".part"):
                            continue
                        full = os.path.join(d, f)
                        if not os.path.isfile(full):          # a broken link (e.g. a steering ref since deleted)
                            skipped += 1
                            continue
                        try:
                            z.write(full, os.path.normpath(os.path.join(top, rel, f)),
                                    compress_type=zipfile.ZIP_STORED if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip"))
                                    else zipfile.ZIP_DEFLATED)
                            n += 1
                        except OSError:                        # unreadable: skip it rather than lose the backup
                            skipped += 1
        os.replace(path + ".part", path)
    except BaseException:
        if os.path.exists(path + ".part"):
            os.remove(path + ".part")
        raise
    return path, n, os.path.getsize(path), skipped


BOARD_LAYOUT = ".layout.json"          # per board: {"order": [file, …], "sizes": {file: [cols, rows]}}
MAX_SPAN = 8


def board_layout(d, files):
    """The board's saved mood-board layout applied to its current files: items in the saved order (new
    ones appended oldest-first), and a [cols, rows] span per item that isn't the default 1×1."""
    try:
        with open(os.path.join(d, BOARD_LAYOUT), encoding="utf-8") as fh:
            lay = json.load(fh)
    except (OSError, ValueError):
        lay = {}
    have = set(files)
    order = [f for f in lay.get("order", []) if f in have]
    order += [f for f in files if f not in set(order)]
    sizes = {}
    for f, wh in (lay.get("sizes") or {}).items():
        if f in have and isinstance(wh, list) and len(wh) == 2:
            w, h = (max(1, min(MAX_SPAN, int(v))) for v in wh)
            if (w, h) != (1, 1):
                sizes[f] = [w, h]
    return order, sizes


def save_board_layout(board, order, sizes):
    d = board_dir(board)
    if not os.path.isdir(d):
        raise ValueError("no such board")
    files = [f for f in os.listdir(d) if f.lower().endswith(IMG_EXT)]
    have = set(files)
    order = [str(f) for f in (order or []) if str(f) in have]
    clean = {}
    for f, wh in (sizes or {}).items():
        if f in have and isinstance(wh, (list, tuple)) and len(wh) == 2:
            w, h = (max(1, min(MAX_SPAN, int(v))) for v in wh)
            if (w, h) != (1, 1):
                clean[f] = [w, h]
    tmp = os.path.join(d, BOARD_LAYOUT + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"order": order, "sizes": clean}, fh, indent=1)
    os.replace(tmp, os.path.join(d, BOARD_LAYOUT))


# ---------------------------------------------------------------------------
# Setup tab: DRAW / Kaleidotron paths, "open with" programs for images and folders
# ---------------------------------------------------------------------------
SETUP_FILE = os.path.expanduser("~/pixelmon-gallery/gui-setup.json")
IS_WIN, IS_MAC = sys.platform.startswith("win"), sys.platform == "darwin"


def load_setup():
    s = _read_json(SETUP_FILE, {}) if os.path.exists(SETUP_FILE) else {}
    s.setdefault("draw", "")
    s.setdefault("kaleidotron", "")
    s.setdefault("editor3d", "")          # 3D models open in this; "" = Blender, found automatically
    s.setdefault("image_apps", [])        # [{name, exec, args}] — args: "{}" = the file, else appended
    s.setdefault("folder_apps", [])
    s.setdefault("similar_folders", [])  # 🔎 Find: the folders its index covers ([] = Pictures, the gallery, the refs)
    s.setdefault("folder_default", "os")  # what a 📂 click opens: "os" | "kaleidotron" | "dir:<i>"
    s.setdefault("draw_palette", True)     # Open in DRAW also loads the image's colors as a .gpl (--palette)
    s.setdefault("server", "")            # render server (servers.json alias / host / URL); "" = servers.json "_default"
    return s


def _clean_app(a):
    return {"name": str(a.get("name") or "")[:60], "exec": str(a.get("exec") or "")[:500], "args": str(a.get("args") or "{}")[:300]}


def save_setup(body):
    s = load_setup()
    for k in ("draw", "kaleidotron", "editor3d"):
        if k in body:
            s[k] = str(body[k] or "").strip()[:500]
    for k in ("image_apps", "folder_apps"):
        if isinstance(body.get(k), list):
            s[k] = [_clean_app(a) for a in body[k][:40] if isinstance(a, dict) and (a.get("exec") or "").strip()]
    if "draw_palette" in body:
        s["draw_palette"] = bool(body["draw_palette"])
    if "folder_default" in body:
        s["folder_default"] = str(body["folder_default"] or "os")[:20]
    if isinstance(body.get("similar_folders"), list):
        s["similar_folders"] = [str(f).strip()[:500] for f in body["similar_folders"][:40] if str(f).strip()]
    if "server" in body:
        s["server"] = re.sub(r"\s+", "", str(body["server"] or ""))[:300]
        _ADV_CACHE["v"] = None                 # model lists come from the render server
    os.makedirs(os.path.dirname(SETUP_FILE), exist_ok=True)
    _write_json(SETUP_FILE, s)
    return s


def _desktop_entries():
    """Linux: every installed app's .desktop entry (name, exec, mime types, categories)."""
    dirs = [os.path.expanduser("~/.local/share/applications"), "/usr/local/share/applications", "/usr/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/flatpak/exports/share/applications", "/var/lib/snapd/desktop/applications"]
    seen, out = set(), []
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.desktop"))):
            base = os.path.basename(f)
            if base in seen:
                continue
            seen.add(base)
            e, sect = {}, None
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("["):
                            sect = line
                        elif sect == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            e.setdefault(k.strip(), v.strip())
            except OSError:
                continue
            if e.get("Type") != "Application" or e.get("NoDisplay", "").lower() == "true" or e.get("Hidden", "").lower() == "true" \
                    or not e.get("Exec"):
                continue
            out.append(e)
    return out


def _desktop_app(e):
    """A .desktop Exec line -> {name, exec, args} with the file placeholder as '{}'."""
    try:
        parts = shlex.split(e["Exec"])
    except ValueError:
        return None
    parts = [p for p in parts if p not in ("%i", "%c", "%k")]
    args = ["{}" if p in ("%f", "%F", "%u", "%U") else p for p in parts[1:]]
    if "{}" not in args:
        args.append("{}")
    return {"name": e.get("Name", parts[0]), "exec": parts[0], "args": shlex.join(args).replace("'{}'", "{}")}


def _mac_app(name, bundle):
    for root in ("/Applications", os.path.expanduser("~/Applications"), "/System/Applications"):
        for app in glob.glob(os.path.join(root, bundle)):
            return {"name": name, "exec": "open", "args": f"-a {shlex.quote(os.path.splitext(os.path.basename(app))[0])} {{}}"}
    return None


def _win_app(name, *patterns):
    roots = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432")]
    for pat in patterns:
        if not any(ch in pat for ch in "\\/*"):
            w = shutil.which(pat)
            if w:
                return {"name": name, "exec": w, "args": "{}"}
            continue
        for r in [x for x in roots if x]:
            hits = sorted(glob.glob(os.path.join(r, pat)))
            if hits:
                return {"name": name, "exec": hits[-1], "args": "{}"}
    return None


def detect_programs():
    """What's installed on this OS: image programs and file managers for the Setup tab's 'Add program'."""
    img, fold = [], []
    if IS_MAC:
        for n, b in (("Preview", "Preview.app"), ("Adobe Photoshop", "Adobe Photoshop*/Adobe Photoshop*.app"),
                     ("Adobe Illustrator", "Adobe Illustrator*/Adobe Illustrator*.app"), ("Pixelmator Pro", "Pixelmator Pro.app"),
                     ("Affinity Photo", "Affinity Photo*.app"), ("GIMP", "GIMP*.app"), ("Krita", "krita.app"),
                     ("Aseprite", "Aseprite.app"), ("LibreSprite", "LibreSprite.app"), ("Inkscape", "Inkscape.app"),
                     ("Pixelorama", "Pixelorama.app")):
            a = _mac_app(n, b)
            if a:
                img.append(a)
        fold.append({"name": "Finder", "exec": "open", "args": "{}"})
        for n, b in (("ForkLift", "ForkLift.app"), ("Path Finder", "Path Finder.app"), ("VS Code", "Visual Studio Code.app")):
            a = _mac_app(n, b)
            if a:
                fold.append(a)
    elif IS_WIN:
        for n, *pats in (("Paint", "mspaint"), ("Adobe Photoshop", r"Adobe\Adobe Photoshop*\Photoshop.exe"),
                         ("Adobe Illustrator", r"Adobe\Adobe Illustrator*\Support Files\Contents\Windows\Illustrator.exe"),
                         ("Paint.NET", r"paint.net\paintdotnet.exe"), ("GIMP", r"GIMP*\bin\gimp-*.exe"),
                         ("Krita", r"Krita (x64)\bin\krita.exe"), ("Aseprite", r"Aseprite\Aseprite.exe", r"Steam\steamapps\common\Aseprite\Aseprite.exe"),
                         ("Inkscape", r"Inkscape\bin\inkscape.exe"), ("Pixelorama", r"Pixelorama\Pixelorama.exe")):
            a = _win_app(n, *pats)
            if a:
                img.append(a)
        fold.append({"name": "Explorer", "exec": "explorer", "args": "{}"})
        for n, *pats in (("Total Commander", r"totalcmd\TOTALCMD64.EXE"), ("Directory Opus", r"GPSoftware\Directory Opus\dopus.exe"),
                         ("VS Code", "code")):
            a = _win_app(n, *pats)
            if a:
                fold.append(a)
    else:
        for e in _desktop_entries():
            mimes = [m for m in e.get("MimeType", "").split(";") if m]
            cats = e.get("Categories", "")
            a = _desktop_app(e)
            if not a:
                continue
            if any(m in ("image/png", "image/gif", "image/bmp", "image/jpeg") for m in mimes):
                img.append(a)
            if "inode/directory" in mimes or "FileManager" in cats:
                fold.append(a)
        have = {a["exec"] for a in img}
        for n, x in (("Aseprite", "aseprite"), ("LibreSprite", "libresprite"), ("Pixelorama", "pixelorama")):
            if x not in have and shutil.which(x):
                img.append({"name": n, "exec": x, "args": "{}"})
    # DRAW and Kaleidotron have their own built-in entries; don't offer them twice
    builtin = lambda a: a["name"].lower() in ("draw", "kaleidotron") or \
        os.path.basename(a["exec"]).lower() in ("draw", "draw.run", "draw.exe", "kaleidotron", "kaleidotron.exe")
    key = lambda a: a["name"].lower()
    return (sorted({a["name"]: a for a in img if not builtin(a)}.values(), key=key),
            sorted({a["name"]: a for a in fold if not builtin(a)}.values(), key=key))


def detect_draw():
    names = ["DRAW.exe"] if IS_WIN else ["DRAW.run", "DRAW"]
    for d in ("~/git/DRAW", "~/DRAW", "~/Apps/DRAW", "~/Applications/DRAW"):
        for n in names:
            f = os.path.expanduser(os.path.join(d, n))
            if os.path.isfile(f):
                return f
    return shutil.which("DRAW") or ""


def detect_kaleidotron():
    return shutil.which("kaleidotron") or next((f for f in (os.path.expanduser(p) for p in (
        "~/git/kaleidotron/target/release/kaleidotron" + (".exe" if IS_WIN else ""), "~/.cargo/bin/kaleidotron")) if os.path.isfile(f)), "")


_TURN = {}


def turnaround_mod():
    """pixelmon's turnaround.py (Blender detection, the open-a-model script), loaded once."""
    if "m" not in _TURN:
        spec = importlib.util.spec_from_file_location("pixelmon_turnaround", os.path.join(REPO, "turnaround.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _TURN["m"] = m
    return _TURN["m"]


def detect_blender():
    """How Blender runs here (a command line), found once: the PATH, the flatpak, the usual install folders."""
    if "blender" not in _TURN:
        cmd = turnaround_mod().find_blender("")
        _TURN["blender"] = shlex.join(cmd) if cmd and not IS_WIN else subprocess.list2cmdline(cmd) if cmd else ""
    return _TURN["blender"]


def editor3d(setup):
    """The 3D editor: (argv, name, is_blender) for the Setup tab's program, else Blender found here, else None."""
    spec = (setup.get("editor3d") or "").strip() or detect_blender()
    if not spec:
        return None
    full = os.path.expanduser(spec)
    argv = [full] if os.path.isfile(full) else shlex.split(spec, posix=not IS_WIN)
    words = [os.path.basename(w).lower() for w in argv[:3]]
    blender = any(w.startswith("blender") or w == "org.blender.blender" for w in words)
    name = "Blender" if blender else os.path.splitext(os.path.basename(argv[0]))[0]
    return argv, name, blender


def open_in_3d_editor(setup, path):
    """Start the 3D editor on a model, detached. Blender imports it (it only opens .blend files itself)."""
    ed = editor3d(setup)
    if not ed:
        raise ValueError("no 3D editor: Blender isn't installed here (blender.org) — or pick a program in 🛠 Setup › 3D editor")
    argv, name, blender = ed
    if argv[0] != "flatpak" and os.sep in argv[0] and not os.path.isfile(argv[0]):
        raise ValueError(f"{name}: program not found at {argv[0]} — fix it in 🛠 Setup › 3D editor")
    if argv[0] == "flatpak" and os.path.realpath(path).startswith(os.path.realpath(tempfile.gettempdir()) + os.sep):
        # a flatpak Blender can't see the host's /tmp: copy the model (and its .bin / textures) to the cache
        dest = os.path.join(os.path.expanduser("~/.cache/pixelmon/3d-editor"), os.path.splitext(os.path.basename(path))[0])
        os.makedirs(dest, exist_ok=True)
        for f in os.listdir(os.path.dirname(path)):
            src = os.path.join(os.path.dirname(path), f)
            if os.path.isfile(src) and os.path.getsize(src) < 512 << 20:
                shutil.copy2(src, dest)
        path = os.path.join(dest, os.path.basename(path))
    cmd = turnaround_mod().open_in_blender(argv, path) if blender else argv + [path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        raise ValueError(f"couldn't start {name}: {e}")
    return name


PALETTE_CACHE = os.path.expanduser("~/.cache/pixelmon/palettes")


def image_gpl(path):
    """The image's own colors as a GIMP palette in the cache, or None (> 255 colors / unreadable)."""
    try:
        n, colors = image_colors(path)
    except OSError:
        return None
    if not colors or n == ">255":
        return None
    stem = safe_name(os.path.splitext(os.path.basename(path))[0], 120) or "palette"
    os.makedirs(PALETTE_CACHE, exist_ok=True)
    out = os.path.join(PALETTE_CACHE, stem + ".gpl")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(gpl_text(stem, colors))
    return out


def os_opener(is_dir):
    if IS_MAC:
        return {"name": "Finder" if is_dir else "system default", "exec": "open", "args": "{}"}
    if IS_WIN:
        return {"name": "Explorer" if is_dir else "system default", "exec": "explorer", "args": "{}"}
    return {"name": "file manager" if is_dir else "system default", "exec": "xdg-open", "args": "{}"}


def launch(app, path, cwd=None):
    """Start app on path. args template: '{}' marks the path, else it's appended. Never waits."""
    exe = os.path.expanduser(app["exec"])
    try:
        args = shlex.split(app.get("args") or "{}", posix=not IS_WIN)
    except ValueError as e:
        raise ValueError(f"bad arguments for {app.get('name')}: {e}")
    args = [a.replace("{}", path) for a in args] if any("{}" in a for a in args) else args + [path]
    if os.sep in exe and not os.path.isfile(exe):
        raise ValueError(f"{app.get('name') or exe}: program not found at {exe} — fix it in the Setup tab")
    if os.sep not in exe and not shutil.which(exe):
        raise ValueError(f"{app.get('name') or exe}: '{exe}' isn't installed or isn't on PATH")
    subprocess.Popen([exe] + args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return exe


def list_backups():
    if not os.path.isdir(BACKUPS):
        return []
    out = []
    for f in sorted(os.listdir(BACKUPS), key=lambda f: -os.path.getmtime(os.path.join(BACKUPS, f))):
        full = os.path.join(BACKUPS, f)
        if f.endswith(".zip") and os.path.isfile(full):
            try:
                with zipfile.ZipFile(full) as z:
                    n = sum(1 for i in z.infolist() if not i.is_dir())
            except zipfile.BadZipFile:
                continue
            out.append({"name": f, "bytes": os.path.getsize(full), "mtime": os.path.getmtime(full), "files": n})
    return out


def restore_backup(name, gallery):
    """Put back everything from a backup that's missing now. Never overwrites an existing file."""
    full = os.path.realpath(os.path.join(BACKUPS, os.path.basename(str(name or ""))))
    if not full.startswith(os.path.realpath(BACKUPS) + os.sep) or not os.path.isfile(full):
        raise ValueError("no such backup")
    roots = {os.path.basename(os.path.normpath(GALLERY_HOME)): GALLERY_HOME,
             os.path.basename(os.path.normpath(gallery)): gallery}    # the gallery, if it lives outside GALLERY_HOME
    restored = kept = 0
    with zipfile.ZipFile(full) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            top, _, rest = info.filename.partition("/")
            root = roots.get(top)
            if not root or not rest:
                continue
            dest = os.path.realpath(os.path.join(root, rest))
            if not dest.startswith(os.path.realpath(root) + os.sep) or os.path.relpath(dest, root).split(os.sep)[0] == "backups":
                continue                                  # nothing outside the gallery; never into backups/
            if os.path.exists(dest):
                kept += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            restored += 1
    return {"restored": restored, "kept": kept}


def clear_gallery(gallery):
    """Delete every render in the gallery folder (job folders). Presets, corkboards, LAB inputs, steering
    images and backups live elsewhere and are untouched."""
    gallery = os.path.realpath(gallery)
    if gallery in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(GALLERY_HOME), "/"):
        raise ValueError("refusing to clear that folder")
    n = 0
    for f in os.listdir(gallery):
        p = os.path.join(gallery, f)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
            n += 1
    return n


def list_boards():
    os.makedirs(os.path.join(BOARDS, "favorites"), exist_ok=True)
    out = []
    for b in sorted(os.listdir(BOARDS)):
        d = os.path.join(BOARDS, b)
        if os.path.isdir(d) and not b.startswith("."):
            files = sorted((f for f in os.listdir(d) if f.lower().endswith(IMG_EXT)),
                           key=lambda f: os.path.getmtime(os.path.join(d, f)))
            order, sizes = board_layout(d, files)
            out.append({"name": b, "items": order, "sizes": sizes})
    return out


def export_bundle(pid, gallery):
    """A preset + everything it references, as zip bytes (see README.txt inside)."""
    _, name, pdir = preset_loc(pid)
    f = os.path.join(pdir, name + ".json")
    if not os.path.isfile(f):
        raise ValueError(f"no preset {pid!r}")
    with open(f, encoding="utf-8") as fh:
        rec = json.load(fh)
    snap = rec.get("snapshot") or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("preset.json", json.dumps(rec, indent=1))
        if rec.get("image") and os.path.isfile(os.path.join(pdir, rec["image"])):
            z.write(os.path.join(pdir, rec["image"]), "picture" + os.path.splitext(rec["image"])[1])
        lab = snap.get("lab") or {}
        if lab.get("file"):
            try:
                z.write(lab_path(lab["file"]), "lab-input/" + os.path.basename(lab["file"]))
            except ValueError:
                pass
        for ref in (snap.get("steer") or {}).get("sel") or []:
            try:
                z.write(ref_path(ref), "steering/" + ref)
            except ValueError:
                pass
        dref = (snap.get("depth") or {}).get("ref")
        if dref:
            try:
                z.write(ref_path(dref), "steering/" + dref)
            except ValueError:
                pass
        par = (snap.get("evo") or {}).get("parent")
        if par:
            try:
                z.write(os.path.realpath(os.path.join(gallery, par["dir"], par["file"])), "evolve-parent/" + par["file"])
            except OSError:
                pass
        form = snap.get("form") or {}
        styles = {n: load_styles().get(n) for n in form.get("styles") or [] if n in load_styles()}
        z.writestr("styles.json", json.dumps(styles, indent=1))
        loras = sorted({x for x in [form.get("lora")] if x and x != "(none)"}
                       | ({"controlnet-union-sdxl-promax.safetensors"} if lab.get("active") else set())
                       | ({"controlnet-depth-sdxl-1.0.safetensors"} if (snap.get("depth") or {}).get("use") else set()))
        commit = subprocess.run(["git", "-C", REPO, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()
        z.writestr("README.txt", "\n".join([
            f"pixelmon preset bundle: {rec.get('name')}",
            f"saved: {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.get('saved') or time.time()))}",
            f"note: {rec.get('note') or '-'}", f"pixelmon: {commit or 'unknown'}", "",
            "Import it in pixelmon-gui: Presets tab -> IMPORT (drop this .zip). Images are restored next to",
            "pixelmon's own (lab-inputs, pixelmon-refs, gallery) without overwriting anything.", "",
            "Contents: preset.json (every setting), picture, lab-input/, steering/, evolve-parent/,",
            "styles.json (the style guides it uses, as they were).", "",
            "Model files it needs on the render server (not included):",
            *[f"  - {x}" for x in loras], "",
            "Prompt: " + str(form.get("subject") or ""),
        ]) + "\n")
    return buf.getvalue(), safe_name(rec.get("name") or name)


def import_bundle(data, gallery, folder=""):
    """Unpack a bundle: images go back where pixelmon looks for them (never overwriting), the preset's
    references are rewritten to wherever they landed, and the preset is saved into `folder`.
    A folder zip (folder.json + <name>.zip bundles) recreates that folder. Returns the saved record(s)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    if "folder.json" in names:
        meta = json.loads(z.read("folder.json"))
        target = folder_name(meta.get("folder") or "") or "imported"
        recs = [import_bundle(z.read(m), gallery, target) for m in sorted(names) if m.endswith(".zip")]
        order = [n for n in meta.get("order") or []]
        _set_order(target, [r["name"] for n in order for r in recs if r.get("orig") == n] + [r["name"] for r in recs if r.get("orig") not in order])
        return {"folder": target, "count": len(recs), "name": recs[0]["name"] if recs else "", "id": recs[0]["id"] if recs else "",
                "snapshot": None}
    if "preset.json" not in names:
        raise ValueError("not a pixelmon preset bundle (no preset.json)")
    rec = json.loads(z.read("preset.json"))
    snap = rec.setdefault("snapshot", {})

    def put(member, dest_dir, fname):
        """copy a zip member to dest_dir/fname unless an identical file is already there; returns the name used"""
        blob = z.read(member)
        if len(blob) > 60 * 1024 * 1024 or not fname.lower().endswith(IMG_EXT):
            raise ValueError(f"refusing {member}")
        os.makedirs(dest_dir, exist_ok=True)
        fname = safe_name(fname, 120)
        target = os.path.join(dest_dir, fname)
        if os.path.exists(target):
            with open(target, "rb") as fh:
                if fh.read() == blob:
                    return fname                  # already here: reuse it
            target = unique_path(dest_dir, fname)
        with open(target, "wb") as fh:
            fh.write(blob)
        return os.path.basename(target)

    lab = snap.get("lab") or {}
    if lab.get("file") and f"lab-input/{os.path.basename(lab['file'])}" in names:
        lab["file"] = put(f"lab-input/{os.path.basename(lab['file'])}", LAB, os.path.basename(lab["file"]))
    steer = snap.get("steer") or {}
    sel = []
    for ref in steer.get("sel") or []:
        member = "steering/" + ref
        if member in names and "/" in ref:
            col = safe_name(ref.split("/", 1)[0])
            sel.append(f"{col}/{put(member, os.path.join(REFS, col), os.path.basename(ref))}")
    if steer:
        steer["sel"] = sel
    dep = snap.get("depth") or {}
    dref = str(dep.get("ref") or "")
    if "/" in dref and "steering/" + dref in names:
        col = safe_name(dref.split("/", 1)[0])
        dep["ref"] = f"{col}/{put('steering/' + dref, os.path.join(REFS, col), os.path.basename(dref))}"
    par = (snap.get("evo") or {}).get("parent")
    if par and f"evolve-parent/{par.get('file')}" in names:
        jid = "import-" + time.strftime("%Y%m%d-%H%M%S")
        d = os.path.join(gallery, jid)
        fname = put(f"evolve-parent/{par['file']}", d, par["file"])
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as fh:   # so it shows in the gallery
            json.dump({"id": jid, "dir": jid, "status": "done", "command": "(imported from a preset bundle)",
                       "params": {"prompt": (snap.get("form") or {}).get("subject", ""), "form": snap.get("form")},
                       "outputs": [{"file": fname, "seed": par.get("seed")}], "created": time.time()}, fh, indent=1)
        par.update({"dir": jid, "file": fname})
    orig = safe_name(rec.get("name") or "imported")
    pdir = preset_dir(folder)
    name, k = orig, 1
    while os.path.exists(os.path.join(pdir, name + ".json")):
        name, k = f"{orig}-{k}", k + 1
    rec["name"] = name
    for key in ("id", "folder"):
        rec.pop(key, None)
    os.makedirs(pdir, exist_ok=True)
    pic = next((m for m in names if m.startswith("picture.")), None)
    rec["image"] = None
    if pic and pic.lower().endswith(IMG_EXT):
        rec["image"] = name + os.path.splitext(pic)[1]
        with open(os.path.join(pdir, rec["image"]), "wb") as fh:
            fh.write(z.read(pic))
    with open(os.path.join(pdir, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    return dict(rec, orig=orig, id=preset_id(folder_name(folder) if folder else "", name))


# ---- preset folders: sub-folders of PRESETS; a preset's id is "FOLDER/name" ("name" = unfiled, at the top) ----
FOLDERS_ORDER = ".folders.json"          # [folder, …] display order
PRESET_ORDER = ".order.json"             # per folder: [name, …] display order


def folder_name(s):
    """A preset folder name: letters, digits, spaces, dots, dashes (e.g. 'TUNED FACTORY')."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w .-]+", "-", str(s or ""))).strip(" -.")[:48]


def preset_dir(folder):
    folder = folder_name(folder) if folder else ""
    return os.path.join(PRESETS, folder) if folder else PRESETS


def preset_loc(pid):
    """'FOLDER/name' or 'name' -> (folder, name, dir). ValueError on nonsense."""
    pid = str(pid or "")
    folder, _, name = pid.rpartition("/")
    name = safe_name(name)
    if not name:
        raise ValueError("no preset given")
    return (folder_name(folder) if folder else ""), name, preset_dir(folder)


def preset_id(folder, name):
    return f"{folder}/{name}" if folder else name


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def list_folders():
    os.makedirs(PRESETS, exist_ok=True)
    have = sorted(d for d in os.listdir(PRESETS) if os.path.isdir(os.path.join(PRESETS, d)) and not d.startswith("."))
    order = [f for f in _read_json(os.path.join(PRESETS, FOLDERS_ORDER), []) if f in have]
    return order + [f for f in have if f not in order]


def list_presets():
    """Every preset, folder by folder in display order (unfiled ones last), each with 'id' and 'folder'."""
    out = []
    for folder in list_folders() + [""]:
        d = preset_dir(folder)
        recs = {}
        for f in glob.glob(os.path.join(d, "*.json")):
            if os.path.basename(f).startswith("."):
                continue
            rec = _read_json(f, None)
            if isinstance(rec, dict):
                name = os.path.splitext(os.path.basename(f))[0]
                rec.update(name=name, folder=folder, id=preset_id(folder, name))
                recs[name] = rec
        order = [n for n in _read_json(os.path.join(d, PRESET_ORDER), []) if n in recs]
        out += [recs[n] for n in order + sorted(n for n in recs if n not in order)]
    return out


def _set_order(folder, names):
    d = preset_dir(folder)
    have = {os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".json") and not f.startswith(".")}
    names = [n for n in names if n in have]
    _write_json(os.path.join(d, PRESET_ORDER), names + sorted(have - set(names)))


def _preset_files(d, name):
    return [os.path.join(d, name + e) for e in (".json",) + IMG_EXT if os.path.isfile(os.path.join(d, name + e))]


def move_preset(pid, to_folder, before=None):
    """Move a preset into to_folder ('' = unfiled), placed before the preset named `before` (else last)."""
    folder, name, d = preset_loc(pid)
    to_folder = folder_name(to_folder) if to_folder else ""
    td = preset_dir(to_folder)
    if not os.path.isfile(os.path.join(d, name + ".json")):
        raise ValueError("no such preset")
    os.makedirs(td, exist_ok=True)
    new = name
    if td != d:
        k = 2
        while os.path.exists(os.path.join(td, new + ".json")):
            new, k = f"{name}-{k}", k + 1
        rec = _read_json(os.path.join(d, name + ".json"), {})
        for f in _preset_files(d, name):
            ext = os.path.splitext(f)[1]
            shutil.move(f, os.path.join(td, new + ext))
        if rec.get("image"):
            rec["image"] = new + os.path.splitext(rec["image"])[1]
        rec["name"] = new
        _write_json(os.path.join(td, new + ".json"), rec)
    names = [p["name"] for p in list_presets() if p["folder"] == to_folder and p["name"] != new]
    at = names.index(before) if before in names else len(names)
    names.insert(at, new)
    _set_order(to_folder, names)
    return preset_id(to_folder, new)


def rename_preset(pid, new_name):
    """Rename a preset in place (its json + picture), keeping its spot in the folder's order."""
    folder, name, d = preset_loc(pid)
    new = safe_name(new_name or "")
    if not new:
        raise ValueError("the new name is empty")
    if not os.path.isfile(os.path.join(d, name + ".json")):
        raise ValueError("no such preset")
    if new == name:
        return preset_id(folder, name)
    if os.path.exists(os.path.join(d, new + ".json")):
        raise ValueError(f"“{new}” already exists in {folder or 'unfiled'}")
    names = [p["name"] for p in list_presets() if p["folder"] == folder]
    rec = _read_json(os.path.join(d, name + ".json"), {})
    for f in _preset_files(d, name):
        os.rename(f, os.path.join(d, new + os.path.splitext(f)[1]))
    if rec.get("image"):
        rec["image"] = new + os.path.splitext(rec["image"])[1]
    rec["name"] = new
    _write_json(os.path.join(d, new + ".json"), rec)
    _set_order(folder, [new if n == name else n for n in names])
    return preset_id(folder, new)


def export_folder(folder, gallery):
    """Every preset in a folder as one zip: <name>.zip bundles + folder.json (name + order)."""
    folder = folder_name(folder) if folder else ""
    recs = [p for p in list_presets() if p["folder"] == folder]
    if not recs:
        raise ValueError("that folder has no presets")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("folder.json", json.dumps({"folder": folder or "unfiled", "order": [p["name"] for p in recs]}, indent=1))
        for p in recs:
            data, _ = export_bundle(p["id"], gallery)
            z.writestr(p["name"] + ".zip", data)
    return buf.getvalue(), safe_name(folder or "unfiled")


def gallery_items(gallery, limit=300):
    items = []
    for jf in sorted(glob.glob(os.path.join(gallery, "*", "job.json")), reverse=True)[:limit]:
        try:
            with open(jf, encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue
        job["outputs"] = [o for o in job.get("outputs") or [] if not o["file"].startswith(("parent.", "input", "mask."))]
        if job["outputs"]:
            items.append(job)
    return items


def gallery_count(gallery):
    return len(glob.glob(os.path.join(gallery, "*", "job.json")))


def rate(gallery, d, f, stars):
    """★ 0..5 for one picture of a gallery job, kept in its job.json (ratings: {file: stars})."""
    jf = os.path.join(gallery, safe_name(d, 64), "job.json")
    job = _read_json(jf, None)
    if not job:
        raise ValueError("that render isn't in the gallery")
    if f not in {o.get("file") for o in job.get("outputs") or []}:
        raise ValueError("that picture isn't in the job")
    stars = int(stars)
    if not 0 <= stars <= 5:
        raise ValueError("stars must be 0..5")
    r = job.setdefault("ratings", {})
    if stars:
        r[f] = stars
    else:
        r.pop(f, None)
    _write_json(jf, job)
    return r


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return                               # the browser left before the answer (reload, closed tab): harmless
        super().handle_error(request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    jobs = None
    gallery = None
    sets = None
    similar = None

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

    def _local(self):
        """Is the browser on this machine? Always true unless --lan opened the page to other devices."""
        ip = self.client_address[0]
        return not LAN or ip in ("::1", "::ffff:127.0.0.1") or ip.startswith("127.")

    def _want_download(self):
        return "download" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)

    def _file(self, path, ctype, download=None):
        """Serve a file in 64 KB chunks, honouring Range (big wallpapers, .glb models, zips never sit in
        memory whole); download=name adds Content-Disposition so the browser saves it."""
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            return self._json({"error": "not found"}, 404)
        with fh:
            start, end = 0, size - 1
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if rng and size and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            else:
                rng = None
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1 if size else 0))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                quoted = urllib.parse.quote(download)
                self.send_header("Content-Disposition", f"attachment; filename=\"{safe_name(download, 120) or 'file'}\"; filename*=UTF-8''{quoted}")
            self.end_headers()
            fh.seek(start)
            left = end - start + 1 if size else 0
            while left > 0:
                chunk = fh.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _resolve_image(self, t):
        """An image the GUI shows -> its absolute path (renders, corkboard items, preset pictures, LAB inputs, refs)."""
        if t.get("src"):
            return self.jobs.gallery_path(t["src"])
        if t.get("board"):
            d = board_dir(t["board"])
            f = os.path.realpath(os.path.join(d, os.path.basename(str(t.get("file") or ""))))
            if not f.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(f):
                raise ValueError("not on that board")
            return f
        if t.get("preset"):
            folder, name, pdir = preset_loc(t["preset"])
            rec = _read_json(os.path.join(pdir, name + ".json"), {})
            f = os.path.join(pdir, rec.get("image") or "")
            if not rec.get("image") or not os.path.isfile(f):
                raise ValueError("that preset has no picture")
            return f
        if t.get("lab"):
            return lab_path(t["lab"])
        if t.get("ref"):
            return ref_path(t["ref"])
        if t.get("analysis"):
            return analysis_path(t["analysis"])
        if t.get("analysis_export"):
            return analysis_path(t["analysis_export"], exports=True)
        if t.get("set_now"):                     # a set's NOW: the sprite the game has today
            sn = t["set_now"] if isinstance(t["set_now"], dict) else {}
            return self.sets.now_path(self.sets.load(sn.get("id")), sn.get("key"))
        if t.get("indexed"):                     # a 🔎 Find result: only pictures in the index
            p = str(t["indexed"])
            if not self.similar.has(p) or not os.path.isfile(p):
                raise ValueError("that picture isn't in the Find index")
            return p
        if t.get("path"):                        # any file under home (a path field, a Browse pick, an export)
            if not self._local():
                raise ValueError("opening paths only works on the computer running pixelmon-gui")
            return home_path(t["path"])
        raise ValueError("nothing to open")

    def _resolve_folder(self, kind, arg=""):
        """A folder the GUI knows about -> its absolute path."""
        if kind == "job":
            target = os.path.join(self.gallery, safe_name(arg, 64))
        elif kind == "gallery":
            target = self.gallery
        elif kind == "refs":
            target = os.path.join(REFS, safe_name(arg)) if arg else REFS
        elif kind == "presets":
            target = preset_dir(arg) if arg else PRESETS
        elif kind == "board":
            target = board_dir(arg)
        elif kind == "backups":
            target = BACKUPS
        elif kind == "lab":
            target = LAB
        elif kind == "analysis":
            target = os.path.join(analysis_dir(), "exports") if arg == "exports" else analysis_dir()
            os.makedirs(target, exist_ok=True)
        elif kind == "dir":
            if not self._local():
                raise ValueError("opening folders by path only works on the computer running pixelmon-gui")
            target = home_path(arg)
            if os.path.isfile(target):           # a file's path: its folder
                target = os.path.dirname(target)
        elif kind == "sets":
            target = os.path.dirname(self.sets.path(arg)) if arg else self.sets.root
        elif kind == "game":                     # a set's game folder
            target = self.sets.game_dir(self.sets.load(arg)) or ""
        else:
            raise ValueError("unknown folder")
        if not os.path.isdir(target):
            raise ValueError(f"folder doesn't exist yet: {target}")
        return target

    def _guard(self):
        """Refuse what a browser tab on another site could send: a Host that isn't this GUI (DNS rebinding) and,
        on POSTs, an Origin / Referer from another page (cross-site requests need no preflight for a text/plain
        body). Requests with neither header (curl, scripts) are fine. Returns an error message, or None."""
        host = (self.headers.get("Host") or "").strip().lower()
        hostname = re.sub(r":\d+$", "", host).strip("[]")
        if not LAN and host and hostname not in ("127.0.0.1", "localhost", "::1"):
            return f"this GUI only answers as 127.0.0.1 / localhost, not {host!r}"
        if self.command == "POST":
            src = self.headers.get("Origin") or self.headers.get("Referer")
            if src is not None:
                net = urllib.parse.urlparse(src).netloc.lower()
                if not host or net != host:
                    return "cross-site request refused (Origin doesn't match this GUI)"
        return None

    def _length(self, limit):
        """The request's Content-Length as an int within 0..limit, or ValueError (a clean 400)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if not 0 <= n <= limit:
            raise ValueError(f"body must be under {limit // (1024 * 1024) or 1} MB")
        return n

    def _body(self, n):
        """Exactly n bytes of body, or ValueError when the client sent fewer (a truncated upload is never written)."""
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise ValueError(f"the upload was cut short ({len(data)} of {n} bytes)")
        return data

    def do_GET(self):
        err = self._guard()
        if err:
            return self._json({"error": err}, 403)
        try:
            return self._do_GET()
        except (ValueError, UnicodeError) as e:          # a NUL byte in a path, a bad query value…
            return self._json({"error": f"bad request: {e}"}, 400)

    def do_POST(self):
        err = self._guard()
        if err:
            return self._json({"error": err}, 403)
        try:
            return self._do_POST()
        except (ValueError, UnicodeError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)

    def _do_GET(self):
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
                               "loras": loras, "loras_live": live, "server": render_server(),
                               "adv_lists": load_adv_lists(),
                               "server_up": server_is_up(),
                               "known_servers": sorted(known_servers()), "default_server": default_server(),
                               "server_locked": SERVER_ARG, "cli": cli_spec(), "home": HOME if self._local() else "",
                               "local": self._local(), "gallery": self.gallery})
        if u.path == "/api/jobs":
            return self._json({"jobs": self.jobs.snapshot(),
                               "render": {"server": render_server(), "url": server_url(), "up": server_is_up(),
                                          "stuck": server_stuck(), "restart": server_restart(),
                                          "auto_restart": bool(restart_command(render_server()))}})
        if u.path in ("/api/colors", "/api/colors.gpl"):
            q = urllib.parse.parse_qs(u.query)
            d, f = q.get("dir", [""])[0], q.get("file", [""])[0]
            if d in ("@presets", "@refs", "@lab") or d.startswith("@board:"):   # preset picture / ref / LAB input / board image
                root = {"@presets": PRESETS, "@refs": REFS, "@lab": LAB}.get(d) or os.path.join(BOARDS, safe_name(d[len("@board:"):]))
                path = os.path.realpath(os.path.join(root, f))
                if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
                    return self._json({"error": "image not found"}, 404)
            else:
                try:
                    path = self.jobs.gallery_path({"dir": d, "file": f})
                except ValueError as e:
                    return self._json({"error": str(e)}, 404)
            n, colors = image_colors(path)
            if n is None:
                return self._json({"error": "palette readout needs Pillow — run pixelmon-gui with ComfyUI's venv"}, 501)
            if u.path == "/api/colors":
                from PIL import Image
                with Image.open(path) as im:       # reads the header only
                    w, h = im.size
                return self._json({"n": n, "colors": colors, "w": w, "h": h})
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
            return self._file(full, mimetypes.guess_type(full)[0] or "application/octet-stream",
                              os.path.basename(full) if self._want_download() else None)
        if u.path == "/api/presets":
            return self._json({"presets": list_presets(), "folders": list_folders()})
        if u.path == "/api/setup":
            img, fold = detect_programs()
            setup = load_setup()
            ed = editor3d(setup)
            return self._json({"setup": setup, "detected": {"draw": detect_draw(), "kaleidotron": detect_kaleidotron(),
                                                             "editor3d": detect_blender(), "image_apps": img, "folder_apps": fold},
                               "editor3d": {"name": ed[1], "blender": ed[2]} if ed else None,
                               "os": "windows" if IS_WIN else "macos" if IS_MAC else "linux",
                               "servers": known_servers(), "server": render_server(), "server_locked": SERVER_ARG,
                               "server_default": default_server()})
        if u.path == "/api/server/check":
            name = (urllib.parse.parse_qs(u.query).get("s") or [""])[0].strip() or render_server()
            return self._json({"server": name, "url": server_url(name), "up": server_up(name)})
        if u.path.startswith("/setup-img/"):
            full = os.path.realpath(os.path.join(HERE, "img", os.path.basename(u.path)))
            if not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png")
        if u.path == "/api/presets/folder/export":
            try:
                body, fname = export_folder(urllib.parse.parse_qs(u.query).get("folder", [""])[0], self.gallery)
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="pixelmon-presets-{fname}.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/presets/export":
            q = urllib.parse.parse_qs(u.query)
            try:
                body, fname = export_bundle((q.get("id") or q.get("name") or [""])[0], self.gallery)
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="pixelmon-preset-{fname}.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/lab/preview":
            q = urllib.parse.parse_qs(u.query)
            try:
                src = lab_path(q.get("file", [""])[0])
                adj = json.loads(q.get("adj", ["{}"])[0] or "{}")
                im = adjust_image(src, adj, max_side=720)
                if q.get("edges", ["0"])[0] == "1":
                    im = edge_view(im)
            except ImportError:
                return self._json({"error": "needs Pillow — run pixelmon-gui with ComfyUI's venv"}, 501)
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/lab/inputs":
            return self._json({"root": LAB, "inputs": list_lab()})
        if u.path.startswith("/lab/"):
            try:
                full = lab_path(urllib.parse.unquote(u.path[len("/lab/"):]))
            except ValueError:
                return self._json({"error": "not found"}, 404)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png", os.path.basename(full) if self._want_download() else None)
        if u.path.startswith("/analysis/"):
            rel = urllib.parse.unquote(u.path[len("/analysis/"):])
            try:
                full = analysis_path(rel[len("exports/"):], exports=True) if rel.startswith("exports/") else analysis_path(rel)
            except ValueError:
                return self._json({"error": "not found"}, 404)
            return self._file(full, mimetypes.guess_type(full)[0] or "application/octet-stream")
        if u.path == "/api/analysis":
            return self._json({"items": analysis_items(), "dir": analysis_dir()})
        if u.path == "/api/boards":
            return self._json({"root": BOARDS, "boards": list_boards()})
        if u.path.startswith("/boards/"):
            rel = urllib.parse.unquote(u.path[len("/boards/"):])
            full = os.path.realpath(os.path.join(BOARDS, rel))
            if not full.startswith(os.path.realpath(BOARDS) + os.sep) or not full.lower().endswith(IMG_EXT):
                return self._json({"error": "forbidden"}, 403)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png")
        if u.path == "/api/backups":
            return self._json({"backups": list_backups(), "folder": BACKUPS})
        if u.path == "/api/backup/download":
            name = os.path.basename(urllib.parse.parse_qs(u.query).get("name", [""])[0])
            full = os.path.realpath(os.path.join(BACKUPS, name))
            if not name.endswith(".zip") or not full.startswith(os.path.realpath(BACKUPS) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "no such backup"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(os.path.getsize(full)))
            self.end_headers()
            with open(full, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return
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
        if u.path.startswith(("/docs/pipeline/", "/docs/3d-guide/")) and u.path.lower().endswith((".png", ".jpg")):
            # the atlas's pipeline pictures and the 3D guide's
            sub = u.path.split("/")[2]
            full = os.path.realpath(os.path.join(REPO, "docs", urllib.parse.unquote(u.path[len("/docs/"):])))
            if not full.startswith(os.path.realpath(os.path.join(REPO, "docs", sub)) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            return self._file(full, mimetypes.guess_type(full)[0] or "image/png")
        if u.path.startswith("/docs/") and u.path.endswith(".html"):
            # the settings atlas and the 3D guide (both also published as artifacts); the atlas has no page skeleton of its own
            full = os.path.realpath(os.path.join(REPO, "docs", urllib.parse.unquote(u.path[len("/docs/"):])))
            if not full.startswith(os.path.realpath(os.path.join(REPO, "docs")) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as fh:
                body = fh.read()
            if not body.lstrip().lower().startswith(b"<!doctype"):
                body = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                        b'<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>' + body + b"</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
            q = urllib.parse.parse_qs(u.query)
            try:
                limit = max(1, min(5000, int((q.get("limit") or ["300"])[0])))
            except ValueError:
                limit = 300
            return self._json({"items": gallery_items(self.gallery, limit), "limit": limit, "total": gallery_count(self.gallery)})
        if u.path == "/api/servers":
            return self._json(servers_report())
        if u.path == "/api/similar/status":
            st = self.similar.status()
            st.update(setup_folders=load_setup().get("similar_folders") or [], default_folders=self._similar_defaults())
            return self._json(st)
        if u.path == "/api/similar/file":               # a Find result's picture: only files in the index
            p = urllib.parse.parse_qs(u.query).get("path", [""])[0]
            if not self.similar.has(p) or not os.path.isfile(p):
                return self._json({"error": "not in the index"}, 404)
            return self._file(p, mimetypes.guess_type(p)[0] or "application/octet-stream",
                              os.path.basename(p) if self._want_download() else None)
        if u.path in ("/api/browse", "/api/disk/file") and not self._local():
            return self._json({"error": "browsing this computer's files only works on the computer running pixelmon-gui"}, 403)
        if u.path == "/api/browse":                    # the Browse dialog: one folder under home
            q = urllib.parse.parse_qs(u.query)
            exts = tuple(e.strip().lower() for e in (q.get("ext") or [""])[0].split(",") if e.strip()) or None
            try:
                return self._json(browse((q.get("path") or ["~"])[0], (q.get("kind") or ["any"])[0], exts,
                                         (q.get("hidden") or [""])[0] == "1", self.gallery, self.sets))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/disk/file":                 # preview a picture picked in Browse (images only, under home)
            try:
                p = home_path(urllib.parse.parse_qs(u.query).get("path", [""])[0])
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            if not os.path.isfile(p) or not p.lower().endswith(IMG_EXT):
                return self._json({"error": "only pictures can be previewed"}, 404)
            return self._file(p, mimetypes.guess_type(p)[0] or "application/octet-stream")
        if u.path == "/api/sets":
            return self._json({"sets": self.sets.list()})
        if u.path in ("/api/set", "/api/set/now"):
            q = urllib.parse.parse_qs(u.query)
            try:
                s = self.sets.load(q.get("id", [""])[0])
                if u.path == "/api/set/now":             # the sprite the game has today, for NOW
                    full = self.sets.now_path(s, q.get("key", [""])[0])
                    return self._file(full, "image/gif" if full.endswith(".gif") else "image/png",
                                      os.path.basename(full) if "download" in q or self._want_download() else None)
                return self._json({"set": s, "takes": self.sets.takes(s["id"]), "now": self.sets.now_files(s),
                                   "pending": self.sets.pending(s["id"], self.jobs.all())})
            except ValueError as e:
                return self._json({"error": str(e)}, 404 if u.path == "/api/set/now" else 400)
        if u.path.startswith("/files/"):
            rel = urllib.parse.unquote(u.path[len("/files/"):])
            full = os.path.realpath(os.path.join(self.gallery, rel))
            if not full.startswith(os.path.realpath(self.gallery) + os.sep):
                return self._json({"error": "forbidden"}, 403)
            ext = os.path.splitext(full)[1].lower()
            ctype = {".glb": "model/gltf-binary", ".txt": "text/plain; charset=utf-8", ".json": "application/json"}.get(ext) \
                or mimetypes.guess_type(full)[0] or "application/octet-stream"
            return self._file(full, ctype, os.path.basename(full) if self._want_download() else None)
        return self._json({"error": "not found"}, 404)

    def _do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/presets/import":
            n = self._length(300 * 1024 * 1024)
            if not n:
                return self._json({"error": "send a preset .zip"}, 400)
            data = self._body(n)
            folder = urllib.parse.parse_qs(u.query).get("folder", [""])[0]
            try:
                rec = import_bundle(data, self.gallery, folder)
            except (ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
                return self._json({"error": f"couldn't import: {e}"}, 400)
            return self._json({"name": rec["name"], "id": rec.get("id"), "folder": rec.get("folder"),
                               "count": rec.get("count"), "snapshot": rec.get("snapshot")})
        if u.path == "/api/similar/search-bytes":        # Find by a picture dropped from the desktop (not kept)
            n = self._length(40 * 1024 * 1024)
            if not n:
                return self._json({"error": "drop a picture"}, 400)
            try:
                fp = similar.fingerprint(io.BytesIO(self._body(n)))
            except ImportError:
                return self._json({"error": "finding similar pictures needs Pillow — run pixelmon-gui with ComfyUI's venv"}, 501)
            except (OSError, SyntaxError) as e:
                return self._json({"error": f"that isn't a picture Pillow can read ({e})"}, 400)
            q = urllib.parse.parse_qs(u.query)
            return self._json({"query": (q.get("name") or ["dropped picture"])[0], "results": self._similar_hits(fp, (q.get("within") or [""])[0])})
        if u.path == "/api/lab/layered":
            return self._upload_layered(urllib.parse.parse_qs(u.query))
        if u.path == "/api/lab/upload":
            return self._upload(urllib.parse.parse_qs(u.query), lab=True)
        if u.path == "/api/refs/upload":            # raw image bytes; ?collection=&filename=
            return self._upload(urllib.parse.parse_qs(u.query))
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._json({"error": "expected application/json"}, 415)
        try:
            body = json.loads(self._body(self._length(64 * 1024 * 1024)) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "the body must be a JSON object"}, 400)
        try:
            if u.path == "/api/prompt":
                return self._json(full_prompt(body))
            if u.path == "/api/command":                 # Copy CLI: the exact command RENDER would run
                try:                                     # a half-set form is normal here: answer, don't fail
                    return self._json({"command": command_preview(body, self.jobs)})
                except ValueError as e:
                    return self._json({"invalid": str(e)})
            if u.path == "/api/rate":
                return self._json({"ratings": rate(self.gallery, body.get("dir"), body.get("file"), body.get("stars", 0))})
            if u.path == "/api/input/from-path":         # Browse picked a file on disk: copy it in, like a drop
                return self._json(self._from_path(body))
            if u.path == "/api/similar/index":           # (re)build the Find index over these folders
                if not self._local():
                    raise ValueError("choosing folders to index only works on the computer running pixelmon-gui")
                chosen = [str(f).strip() for f in (body.get("folders") or []) if str(f).strip()][:40]
                full = []
                for f in chosen or self._similar_defaults():
                    d = home_path(f)
                    if not os.path.isdir(d):
                        raise ValueError(f"not a folder: {f}")
                    full.append(d)
                save_setup({"similar_folders": chosen})           # [] = keep following the defaults
                self.similar.start(full)
                return self._json(self.similar.status())
            if u.path == "/api/similar/search":          # Find by any picture the GUI shows (or a Browse pick)
                t = body.get("target") or {}
                src = self._resolve_image(t)
                if not src.lower().endswith(IMG_EXT):
                    raise ValueError("that isn't a picture")
                try:
                    fp = similar.fingerprint(src)
                except ImportError:
                    return self._json({"error": "finding similar pictures needs Pillow — run pixelmon-gui with ComfyUI's venv"}, 501)
                return self._json({"query": os.path.basename(src), "query_path": src if self._local() else "",
                                   "results": self._similar_hits(fp, body.get("within"), exclude=src)})
            if u.path == "/api/similar/use":             # a Find result becomes a steering ref / LAB input / pin (a copy)
                p = str(body.get("path") or "")
                if not self.similar.has(p) or not os.path.isfile(p):
                    raise ValueError("that picture isn't in the Find index")
                return self._json(self._from_path(dict(body, path=p), indexed=True))
            if u.path == "/api/render" and isinstance(body.get("batch"), list):
                # several renders at once (a Set's sprites, Batch rows): every one is checked before any is queued
                items = body["batch"][:2000]
                if not items or not all(isinstance(p, dict) for p in items):
                    raise ValueError("batch must be a list of render settings")
                for p in items:
                    try:
                        command_preview(p, self.jobs)
                        export_target(p)
                        ref = p.get("set")
                        if ref is not None and not (isinstance(ref, dict) and setsmod.key_ok(ref.get("key"))):
                            raise ValueError("bad set reference")
                    except ValueError as e:
                        raise ValueError(f"{p.get('label') or str(p.get('prompt') or '')[:40]}: {e} — nothing was queued")
                ids = []
                for p in items:
                    p = dict(p)
                    group, label = p.pop("group", None), p.pop("label", None)
                    ids.append(self.jobs.add(p, group=group, label=label)["id"])
                return self._json({"jobs": ids})
            if u.path == "/api/render":
                group, label = body.pop("group", None), body.pop("label", None)
                return self._json({"jobs": [self.jobs.add(body, group=group, label=label)["id"]]})
            if u.path.startswith("/api/sets/"):
                act = u.path.rsplit("/", 1)[-1]
                if act == "create":
                    return self._json({"set": self.sets.create(body)})
                if act == "update":
                    return self._json({"set": self.sets.update(body)})
                if act == "mark":
                    return self._json({"set": self.sets.mark(body)})
                if act == "export":
                    s = self.sets.load(body.get("id"))
                    if body.get("preview"):
                        return self._json({"plan": self.sets.export_plan(s, body.get("opts")), "folder": self.sets.game_dir(s)})
                    return self._json(self.sets.export(s, only_changed=not body.get("all"), opts=body.get("opts")))
                raise ValueError(f"unknown sets action {act!r}")
            if u.path == "/api/sweep":
                # same settings + one fixed seed, varying a single field across values
                base, field, values = body.get("params") or {}, body.get("field"), body.get("values") or []
                if field not in ("sampler", "scheduler", "base", "lora_strength", "dither", "dither_amount", "palette", "styles", "lora",
                                 "steps", "cfg", "despeckle", "out", "steer_strength",
                                 "steer_start", "steer_end", "pixel_angles", "angle_grid", "pixel_size", "thin_lines"):
                    raise ValueError(f"can't sweep {field!r}")
                if not values:
                    raise ValueError("no sweep values")
                base = dict(base)
                if int(base.get("seed", -1) if base.get("seed") not in (None, "") else -1) < 0:
                    base["seed"] = random.randint(0, 2**31 - 1)
                base["n"] = 1
                group = f"sweep-{int(time.time())}"
                for v in values[:64]:
                    p = dict(base)
                    if field in ("sampler", "scheduler", "base"):          # Advanced settings: one override per render
                        p["adv"] = {**(p.get("adv") or {}), field: v}
                        label = f"{'checkpoint' if field == 'base' else field}={str(v).replace('.safetensors', '')}"
                    else:
                        p[field] = [s for s in str(v).split("+") if s] if field == "styles" else v
                        label = f"{field}={v}"
                    self.jobs.add(p, group=group, label=label)
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
            if u.path == "/api/setup":
                s = save_setup(body)
                ed = editor3d(s)
                return self._json({"setup": s, "editor3d": {"name": ed[1], "blender": ed[2]} if ed else None})
            if u.path == "/api/open":
                # open an image or a folder in DRAW / Kaleidotron / a program saved in the Setup tab / the OS default.
                # Only programs saved in Setup can be launched (the request names one, it can't supply a command).
                setup, app, key = load_setup(), None, str(body.get("app") or "os")
                t = body.get("target") or {}
                is_dir = "folder" in t
                path = self._resolve_folder(t["folder"], t.get("arg") or "") if is_dir else self._resolve_image(t)
                if not is_dir and t.get("reveal"):          # "Show in folder": the folder the file is in
                    path, is_dir = os.path.dirname(path), True
                elif not is_dir and os.path.isdir(path):    # a {path} that names a folder
                    is_dir = True
                cwd = None
                if key == "draw":
                    if is_dir:
                        raise ValueError("DRAW opens images, not folders")
                    exe = setup["draw"] or detect_draw()
                    if not exe:
                        raise ValueError("DRAW isn't set up — add its path in the Setup tab")
                    app, cwd = {"name": "DRAW", "exec": exe, "args": "{}"}, os.path.dirname(os.path.expanduser(exe))
                    gpl = image_gpl(path) if setup.get("draw_palette", True) else None
                    if gpl:                                  # DRAW.run art.png --palette art.gpl
                        app["args"] = "{} --palette " + shlex.quote(gpl)
                        app["name"] = "DRAW (+ its palette)"
                elif key == "kaleidotron":
                    exe = setup["kaleidotron"] or detect_kaleidotron()
                    if not exe:
                        raise ValueError("Kaleidotron isn't set up — add its path in the Setup tab")
                    app = {"name": "Kaleidotron", "exec": exe, "args": "--folder {}" if is_dir else "--view {}"}
                elif key.startswith(("img:", "dir:")):
                    lst = setup["folder_apps" if key.startswith("dir:") else "image_apps"]
                    i = int(key.split(":", 1)[1]) if key.split(":", 1)[1].isdigit() else -1
                    if not 0 <= i < len(lst):
                        raise ValueError("that program isn't in the Setup tab any more")
                    app = lst[i]
                elif key == "editor3d":
                    if is_dir or not path.lower().endswith(turnaround_mod().MODEL_EXT + (".blend", ".blend1")):
                        raise ValueError("the 3D editor opens models (.glb .gltf .obj .stl .ply .fbx .blend)")
                    return self._json({"opened": path, "with": open_in_3d_editor(setup, path)})
                elif key == "os":
                    app = os_opener(is_dir)
                else:
                    raise ValueError(f"unknown program {key!r}")
                launch(app, path, cwd)
                return self._json({"opened": path, "with": app.get("name")})
            if u.path == "/api/reveal":
                # open a folder in the desktop file manager (on the machine running this server; "dir" = any
                # folder under home, which needs the browser to be on that machine too)
                target = self._resolve_folder(body.get("kind"), body.get("arg") or "")
                launch(os_opener(True), target)
                return self._json({"opened": target})
            if u.path == "/api/lab/segment":
                # "select by words": CLIPSeg (CPU, the model pixelmon's --animate uses) on the adjusted input
                src = lab_path(body.get("file"))
                text = str(body.get("text") or "").strip()
                if not text:
                    raise ValueError("type what to select, e.g. “the moon”")
                im = adjust_image(src, body.get("adj") or {}, max_side=720)
                try:
                    spec = importlib.util.spec_from_file_location("pixelmon_animate", os.path.join(REPO, "animate.py"))
                    anim = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(anim)
                    m = anim._clipseg_mask(im, text)
                except ImportError as e:
                    raise ValueError(f"select-by-words needs torch + transformers (run pixelmon-gui with ComfyUI's venv): {e}")
                buf = io.BytesIO()
                m.convert("L").save(buf, "PNG")
                return self._json({"mask": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                                   "size": m.size})
            if u.path == "/api/lab/edges":
                # "what the AI sees" with your 🎨 strokes painted in (as the edit uses them), tuned the same way
                from PIL import Image
                adj = body.get("adj") or {}
                strokes_only = body.get("scope") == "strokes"
                pic_adj = ({"crop": adj.get("crop")} if adj.get("crop") else {}) if strokes_only else adj
                im = adjust_image(lab_path(body.get("file")), pic_adj, max_side=720).convert("RGBA")
                data = str(body.get("sketch") or "")
                if data:
                    raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
                    sk = Image.open(io.BytesIO(raw)).convert("RGBA").resize(im.size, Image.NEAREST)
                    if strokes_only:
                        tone = {k: v for k, v in adj.items() if k != "crop"}
                        if not adjust_is_default(tone):
                            sk = adjust_strokes(sk, tone)
                    im = Image.alpha_composite(im, sk)
                b = io.BytesIO(); edge_view(im.convert("RGB")).save(b, "PNG")
                return self._json({"image": "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()})
            if u.path == "/api/lab/bake":
                # "apply now": bake the adjust sliders into the picture (a new LAB input; the old one stays for undo)
                # or into the 🎨 strokes. The crop isn't baked, so the mask and strokes keep lining up.
                adj = {k: v for k, v in (body.get("adj") or {}).items() if k != "crop"}
                if adjust_is_default(adj):
                    raise ValueError("nothing to apply: the sliders are at their defaults")
                from PIL import Image
                if body.get("sketch"):
                    data = str(body["sketch"])
                    raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
                    sk = Image.open(io.BytesIO(raw)).convert("RGBA")
                    b = io.BytesIO(); adjust_strokes(sk, adj).save(b, "PNG")
                    return self._json({"sketch": "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()})
                src = lab_path(body.get("file"))
                stem = re.sub(r"(__adj\d+)+$", "", os.path.splitext(os.path.basename(src))[0])
                n = 1 + max([int(m.group(1)) for f in os.listdir(LAB) for m in [re.match(re.escape(stem) + r"__adj(\d+)\.png$", f)] if m] or [0])
                out = os.path.join(LAB, f"{stem}__adj{n}.png")
                adjust_image(src, adj).save(out)             # (a `region`: only inside the selection)
                return self._json({"file": os.path.basename(out), "size": image_size(out)})
            if u.path == "/api/lab/import":
                # bring a render / steering ref / corkboard item into the LAB as an input
                if body.get("ref"):
                    src = ref_path(body["ref"])
                elif body.get("board"):
                    d = board_dir(body["board"])
                    src = os.path.realpath(os.path.join(d, os.path.basename(str(body.get("file") or ""))))
                    if not src.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(src):
                        raise ValueError("not on that board")
                elif body.get("src"):
                    src = self.jobs.gallery_path(body["src"])
                else:
                    raise ValueError("nothing to import")
                os.makedirs(LAB, exist_ok=True)
                out = unique_path(LAB, safe_name(os.path.basename(src), 120))
                shutil.copy2(src, out)
                return self._json({"file": os.path.basename(out), "size": image_size(out)})
            if u.path == "/api/refs/import":
                # copy a render / corkboard item / LAB input into a steering collection (the depth slot uses 'depth')
                col = safe_name(body.get("collection") or "gui-drops") or "gui-drops"
                if body.get("ref"):
                    return self._json({"ref": f"{os.path.relpath(ref_path(body['ref']), REFS)}"})
                if body.get("board"):
                    d = board_dir(body["board"])
                    src = os.path.realpath(os.path.join(d, os.path.basename(str(body.get("file") or ""))))
                    if not src.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(src):
                        raise ValueError("not on that board")
                elif body.get("src"):
                    src = self.jobs.gallery_path(body["src"])
                elif body.get("lab"):
                    src = lab_path(body["lab"])
                else:
                    raise ValueError("nothing to import")
                os.makedirs(os.path.join(REFS, col), exist_ok=True)
                out = unique_path(os.path.join(REFS, col), safe_name(os.path.basename(src), 120))
                shutil.copy2(src, out)
                return self._json({"ref": f"{col}/{os.path.basename(out)}"})
            if u.path == "/api/depth/preview":
                return self._json(depth_preview(body, self.gallery))
            if u.path == "/api/jobs/clear":
                return self._json(self.jobs.clear(bool(body.get("stop_running"))))
            if u.path == "/api/jobs/abort":
                return self._json(self.jobs.abort(clear_server=bool(body.get("clear_server"))))
            if u.path == "/api/backups/restore":
                r = restore_backup(body.get("name"), self.gallery)
                return self._json(r)
            if u.path == "/api/gallery/clear":
                if any(j["status"] in ("queued", "running") for j in self.jobs.snapshot()):
                    raise ValueError("renders are still queued or running — wait for them (or ✕ clear queue) first")
                backup = None
                if body.get("backup", True):
                    path, n, size, _ = make_backup(body.get("keyword") or "before-clear", self.gallery)
                    backup = os.path.basename(path)
                removed = clear_gallery(self.gallery)
                self.jobs.clear()
                return self._json({"removed": removed, "backup": backup})
            if u.path == "/api/backup":
                path, n, size, skipped = make_backup(body.get("keyword"), self.gallery)
                return self._json({"path": path, "name": os.path.basename(path), "files": n, "bytes": size, "skipped": skipped})
            if u.path == "/api/boards/move":
                # move one image from a board to another (the file moves; both layouts are kept tidy)
                src_d, dst_d = board_dir(body.get("board")), board_dir(body.get("to"))
                f = os.path.basename(str(body.get("file") or ""))
                src = os.path.join(src_d, f)
                if not f or not os.path.isfile(src):
                    raise ValueError("not on that board")
                if os.path.realpath(src_d) == os.path.realpath(dst_d):
                    return self._json({"file": f})
                os.makedirs(dst_d, exist_ok=True)
                dst = os.path.join(dst_d, f)
                if os.path.exists(dst):
                    dst = unique_path(dst_d, f)
                shutil.move(src, dst)
                for d in (src_d, dst_d):                 # drop stale layout entries; the moved image lands last
                    files = [x for x in os.listdir(d) if x.lower().endswith(IMG_EXT)]
                    order, sizes = board_layout(d, sorted(files, key=lambda x: os.path.getmtime(os.path.join(d, x))))
                    save_board_layout(os.path.basename(d), order, sizes)
                return self._json({"file": os.path.basename(dst), "board": os.path.basename(dst_d)})
            if u.path == "/api/boards/layout":
                save_board_layout(body.get("board"), body.get("order"), body.get("sizes"))
                return self._json({"ok": True})
            if u.path == "/api/analysis/add":
                src = self._resolve_image(body.get("target") or {})
                if not src.lower().endswith(IMG_EXT):
                    raise ValueError("that isn't a picture")
                it = analysis_add(src, body.get("label"), body.get("target"))
                return self._json({"item": it, "items": analysis_items()})
            if u.path == "/api/analysis/remove":
                n = analysis_remove(body.get("files") or [])
                return self._json({"removed": n, "items": analysis_items()})
            if u.path == "/api/analysis/clear":
                n = analysis_remove([it["file"] for it in analysis_items()])
                return self._json({"removed": n, "items": []})
            if u.path == "/api/analysis/export":
                fmt = str(body.get("format") or "psd").lower()
                r = analysis_export(body.get("files"), fmt, bool(body.get("diff", True)), body.get("tol", 0),
                                    body.get("color"), body.get("opacity", 0.6))
                return self._json(r)
            if u.path == "/api/lab/remove":
                # take LAB input images off the recents strip (the files are moved to lab-inputs/.removed, not deleted)
                files = body.get("files") or ([body["file"]] if body.get("file") else [])
                trash = os.path.join(LAB, ".removed")
                os.makedirs(trash, exist_ok=True)
                n = 0
                for f in files:
                    try:
                        src = lab_path(f)
                    except ValueError:
                        continue
                    shutil.move(src, unique_path(trash, os.path.basename(src)))
                    n += 1
                return self._json({"removed": n})
            if u.path == "/api/boards/rename":
                src, dst = board_dir(body.get("board")), board_dir(body.get("name"))
                if not os.path.isdir(src):
                    raise ValueError("no such board")
                if os.path.exists(dst):
                    raise ValueError(f"a board called “{os.path.basename(dst)}” already exists")
                os.rename(src, dst)
                return self._json({"name": os.path.basename(dst)})
            if u.path == "/api/boards/delete":
                d = board_dir(body.get("board"))
                if not os.path.isdir(d):
                    raise ValueError("no such board")
                if not os.path.realpath(d).startswith(os.path.realpath(BOARDS) + os.sep):
                    raise ValueError("not a board")
                shutil.rmtree(d)                         # the board's copies only; originals stay in the gallery
                return self._json({"ok": True})
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
                elif body.get("lab"):                        # a LAB input image
                    src = lab_path(body["lab"])
                    name = safe_name(os.path.basename(src), 120)
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
                folder = folder_name(body.get("folder") or "")
                pdir = preset_dir(folder)
                os.makedirs(pdir, exist_ok=True)
                rec = {"name": name, "saved": time.time(), "note": str(body.get("note") or "")[:500],
                       "snapshot": body["snapshot"], "image": None}
                img = body.get("image")                 # {dir, file} of a render to keep as the preset's picture
                if img:
                    src = self.jobs.gallery_path(img)
                    ext = os.path.splitext(src)[1].lower()
                    for e in IMG_EXT:                       # replace the previous picture of this preset
                        if os.path.isfile(os.path.join(pdir, name + e)):
                            os.remove(os.path.join(pdir, name + e))
                    shutil.copy2(src, os.path.join(pdir, name + ext))
                    rec["image"] = name + ext
                elif os.path.isfile(os.path.join(pdir, name + ".json")):      # re-saving keeps its picture
                    rec["image"] = (_read_json(os.path.join(pdir, name + ".json"), {}) or {}).get("image")
                with open(os.path.join(pdir, name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, indent=1)
                return self._json({"name": name, "id": preset_id(folder, name), "folder": folder})
            if u.path == "/api/presets/delete":
                folder, name, pdir = preset_loc(body.get("id") or body.get("name"))
                for f in _preset_files(pdir, name):     # exact names only
                    os.remove(f)
                return self._json({"ok": True})
            if u.path == "/api/presets/move":
                return self._json({"id": move_preset(body.get("id"), body.get("folder"), body.get("before"))})
            if u.path == "/api/presets/rename":
                return self._json({"id": rename_preset(body.get("id"), body.get("name"))})
            if u.path == "/api/presets/note":
                folder, name, d = preset_loc(body.get("id"))
                f = os.path.join(d, name + ".json")
                if not os.path.isfile(f):
                    raise ValueError("no such preset")
                rec = _read_json(f, {})
                rec["note"] = str(body.get("note") or "")[:500]
                _write_json(f, rec)
                return self._json({"ok": True})
            if u.path == "/api/presets/order":
                _set_order(folder_name(body.get("folder") or ""), [safe_name(n) for n in body.get("order") or []])
                return self._json({"ok": True})
            if u.path == "/api/presets/folder/new":
                name = folder_name(body.get("name"))
                if not name:
                    raise ValueError("folder needs a name")
                os.makedirs(preset_dir(name), exist_ok=True)
                return self._json({"folder": name})
            if u.path == "/api/presets/folder/rename":
                src, dst = folder_name(body.get("folder")), folder_name(body.get("name"))
                if not src or not dst or not os.path.isdir(preset_dir(src)):
                    raise ValueError("no such folder")
                if os.path.exists(preset_dir(dst)):
                    raise ValueError(f"a folder called “{dst}” already exists")
                os.rename(preset_dir(src), preset_dir(dst))
                order = _read_json(os.path.join(PRESETS, FOLDERS_ORDER), [])
                _write_json(os.path.join(PRESETS, FOLDERS_ORDER), [dst if f == src else f for f in order])
                return self._json({"folder": dst})
            if u.path == "/api/presets/folder/delete":
                name = folder_name(body.get("folder"))
                d = preset_dir(name)
                if not name or not os.path.isdir(d) or os.path.realpath(d) == os.path.realpath(PRESETS):
                    raise ValueError("no such folder")
                shutil.rmtree(d)
                return self._json({"ok": True})
            if u.path == "/api/presets/folders/order":
                _write_json(os.path.join(PRESETS, FOLDERS_ORDER), [folder_name(f) for f in body.get("order") or []])
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
        except (TypeError, AttributeError, KeyError) as e:   # a field of the wrong type ([] where {} belongs…)
            return self._json({"error": f"bad request: {type(e).__name__}: {e}"}, 400)
        except Exception as e:                            # never drop the connection: report what went wrong
            import traceback
            traceback.print_exc()
            return self._json({"error": f"server error: {type(e).__name__}: {e}"}, 500)
        return self._json({"error": "not found"}, 404)


def unique_path(d, fname):
    """d/fname, or d/fname-1, -2 … so nothing is ever overwritten."""
    stem, ext = os.path.splitext(fname)
    out, k = os.path.join(d, fname), 1
    while os.path.exists(out):
        out, k = os.path.join(d, f"{stem}-{k}{ext}"), k + 1
    return out


def _upload(self, q, lab=False):
    board = q.get("board")
    col = safe_name((q.get("collection") or ["gui-drops"])[0]) or "gui-drops"
    fname = safe_name((q.get("filename") or ["image.png"])[0], 96)
    if not fname.lower().endswith(IMG_EXT):
        return self._json({"error": f"not an image type: {fname}"}, 400)
    n = self._length(40 * 1024 * 1024)
    if not n:
        return self._json({"error": "empty upload"}, 400)
    data = self._body(n)
    magic = (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8"
             or data[:2] == b"BM" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
    if not magic:
        return self._json({"error": f"{fname} doesn't look like an image"}, 400)
    try:
        d = LAB if lab else board_dir(board[0]) if board else os.path.join(REFS, col)   # LAB / corkboard / refs
    except ValueError as e:
        return self._json({"error": str(e)}, 400)
    os.makedirs(d, exist_ok=True)
    out = unique_path(d, fname)
    with open(out, "wb") as f:
        f.write(data)
    if lab:
        return self._json({"file": os.path.basename(out), "size": image_size(out)})
    if board:
        return self._json({"board": os.path.basename(d), "file": os.path.basename(out)})
    return self._json({"ref": f"{col}/{os.path.basename(out)}"})


Handler._upload = _upload


def _from_path(self, body, indexed=False):
    """A file picked with Browse… goes where a drop would put it: a steering collection ('refs', the depth slot
    uses collection 'depth'), the LAB inputs ('lab'; .draw/.ora/.psd are read as layers), a corkboard ('board'),
    or a preset / folder bundle ('bundle'). Always a copy; the original is never touched."""
    if not indexed and not self._local():
        raise ValueError("picking files from this computer only works on the computer running pixelmon-gui")
    p = body["path"] if indexed else home_path(body.get("path"))
    if not os.path.isfile(p):
        raise ValueError("pick a file, not a folder")
    to, name = str(body.get("to") or "refs"), os.path.basename(p)
    size = os.path.getsize(p)
    if to == "bundle":
        if not name.lower().endswith(".zip") or size > 300 * 1024 * 1024:
            raise ValueError("pick a preset .zip under 300 MB")
        with open(p, "rb") as fh:
            rec = import_bundle(fh.read(), self.gallery, str(body.get("folder") or ""))
        return {"name": rec["name"], "id": rec.get("id"), "folder": rec.get("folder"), "count": rec.get("count"),
                "snapshot": rec.get("snapshot"), "from": p}
    if to == "lab" and name.lower().endswith((".draw", ".ora", ".psd")):
        import layered
        if size > 200 * 1024 * 1024:
            raise ValueError("file must be under 200 MB")
        with open(p, "rb") as fh:
            art, mask, info = layered.read_layered(name, fh.read())
        os.makedirs(LAB, exist_ok=True)
        out = unique_path(LAB, safe_name(os.path.splitext(name)[0], 90) + ".png")
        art.save(out)
        res = {"file": os.path.basename(out), "size": image_size(out), "info": info, "mask": None, "from": p}
        if mask is not None:
            buf = io.BytesIO()
            mask.save(buf, "PNG")
            res["mask"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        return res
    if not name.lower().endswith(IMG_EXT):
        raise ValueError(f"not a picture: {name} (png, jpg, webp, gif, bmp)")
    if size > 40 * 1024 * 1024:
        raise ValueError("image must be under 40 MB")
    if to == "lab":
        d = LAB
    elif to == "board":
        d = board_dir(body.get("board") or "")
    elif to == "refs":
        col = safe_name(body.get("collection") or "gui-drops") or "gui-drops"
        d = os.path.join(REFS, col)
    else:
        raise ValueError(f"can't put a file into {to!r}")
    os.makedirs(d, exist_ok=True)
    out = unique_path(d, safe_name(name, 96) or "image.png")
    shutil.copy2(p, out)
    if to == "lab":
        return {"file": os.path.basename(out), "size": image_size(out), "from": p}
    if to == "board":
        return {"board": os.path.basename(d), "file": os.path.basename(out), "from": p}
    return {"ref": f"{os.path.basename(d)}/{os.path.basename(out)}", "from": p}


Handler._from_path = _from_path


def _similar_defaults(self):
    """What 🔎 Find indexes when you haven't chosen: Pictures, the gallery, the steering refs, the corkboards."""
    short = lambda d: "~" + d[len(HOME):] if d.startswith(HOME + os.sep) else d     # noqa: E731
    return [short(os.path.realpath(d)) for d in (os.path.expanduser("~/Pictures"), self.gallery, REFS, BOARDS) if os.path.isdir(d)]


def _similar_hits(self, fp, within=None, exclude=None):
    w = None
    if within:
        if not self._local():
            raise ValueError("'only inside this folder' only works on the computer running pixelmon-gui")
        w = home_path(within)
    hits = self.similar.search(fp, 48, w, exclude)
    for h in hits:
        h["url"] = "/api/similar/file?path=" + urllib.parse.quote(h["path"])
        h["name"] = os.path.basename(h["path"])
        h["folder"] = os.path.dirname(h["path"])
    return hits


Handler._similar_defaults = _similar_defaults
Handler._similar_hits = _similar_hits


def _upload_layered(self, q):
    """A layered file (.draw / .ora / .psd): its 'art' layer becomes the LAB input, its 'mask' layer the mask."""
    import layered
    fname = safe_name((q.get("filename") or ["art.draw"])[0], 96)
    n = self._length(200 * 1024 * 1024)
    if not n:
        return self._json({"error": "empty upload"}, 400)
    data = self._body(n)
    try:
        art, mask, info = layered.read_layered(fname, data)
    except (ValueError, KeyError, OSError, struct_error, zlib_error, zipfile.BadZipFile) as e:
        return self._json({"error": f"couldn't read {fname}: {e}"}, 400)
    os.makedirs(LAB, exist_ok=True)
    out = unique_path(LAB, os.path.splitext(fname)[0] + ".png")
    art.save(out)
    res = {"file": os.path.basename(out), "size": image_size(out), "info": info, "mask": None}
    if mask is not None:
        buf = io.BytesIO(); mask.save(buf, "PNG")
        res["mask"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return self._json(res)


Handler._upload_layered = _upload_layered


def main():
    global REFS, PRESETS, BOARDS, LAB, GALLERY_HOME, BACKUPS, SETUP_FILE, SERVER_ARG, LAN
    ap = argparse.ArgumentParser(prog="pixelmon-gui", description="Web GUI for pixelmon.")
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--server", default=os.environ.get("PIXELMON_SERVER", ""),
                    help="render server: a servers.json alias, host[:port] or URL (default: $PIXELMON_SERVER, "
                         "else the Setup tab's choice, else servers.json \"_default\", else 'local')")
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces so other devices on the LAN can use it")
    ap.add_argument("--refs", default=REFS, help="steering image library, one folder per collection "
                                                  "(default ~/pixelmon-refs)")
    ap.add_argument("--lab", default=LAB, help="LAB input images (default ~/pixelmon-gallery/lab-inputs)")
    ap.add_argument("--boards", default=BOARDS, help="corkboards, one folder per board (default ~/pixelmon-gallery/corkboards)")
    ap.add_argument("--presets", default=PRESETS, help="saved lab presets (default ~/pixelmon-gallery/gui-presets)")
    ap.add_argument("--gallery", default=os.path.expanduser("~/pixelmon-gallery/gui"),
                    help="where renders + job.json records go (default ~/pixelmon-gallery/gui)")
    args = ap.parse_args()
    REFS = os.path.realpath(os.path.expanduser(args.refs))
    PRESETS = os.path.expanduser(args.presets)
    BOARDS = os.path.expanduser(args.boards)
    LAB = os.path.expanduser(args.lab)
    GALLERY_HOME = os.path.dirname(os.path.normpath(PRESETS))           # backups + setup live beside the presets
    BACKUPS = os.path.join(GALLERY_HOME, "backups")
    SETUP_FILE = os.path.join(GALLERY_HOME, "gui-setup.json")
    SERVER_ARG = re.sub(r"\s+", "", args.server or "")
    LAN = bool(args.lan)
    threading.Thread(target=watch_server, daemon=True).start()
    os.makedirs(os.path.join(REFS, "gui-drops"), exist_ok=True)
    os.makedirs(args.gallery, exist_ok=True)
    Handler.gallery = os.path.realpath(args.gallery)
    fixed = recover_interrupted(Handler.gallery)
    if fixed:
        print(f"[pixelmon-gui] {fixed} render(s) were cut off when pixelmon-gui last stopped: marked interrupted, "
              f"their finished pictures kept", flush=True)
    Handler.jobs = JobQueue(Handler.gallery)
    Handler.sets = setsmod.Sets(os.path.join(GALLERY_HOME, "sets"), Handler.gallery, home_path)
    Handler.similar = similar.Index(os.path.join(GALLERY_HOME, "similar", "index.json"))
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        srv = QuietServer((host, args.port), Handler)
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
          f"(renders on '{render_server()}' {server_url() or ''}, gallery {args.gallery})  Ctrl-C to quit")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
