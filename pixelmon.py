#!/usr/bin/env python3
"""pixelmon — make a palette-locked pixel-art sprite from a text prompt.

This is the brains; run it through the `pixelmon` wrapper, which makes sure the
ComfyUI server is running first. It talks to ComfyUI's HTTP API (so the visual
node-graph happens behind the scenes — you just give a prompt).
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "1.12.0"

# pixelmon can render on a REMOTE ComfyUI (e.g. a faster box on the LAN). Choose a
# target with `--server NAME` (an alias from servers.json) or `--server host[:port]`/URL,
# or the PIXELMON_SERVER env var. Default is local. When the target is remote, results
# are fetched back over HTTP (/view) — no shared filesystem needed. Actual resolution
# happens in main(); these module-level values are the local default.
SERVER = "http://127.0.0.1:8188"   # until servers.json is read (below)
REMOTE = False
POOL = []   # >1 entry (--server a,b,c) turns on render-farm mode (jobs fan across GPUs)
COMFY = os.path.expanduser("~/ComfyUI")
OUTPUT = os.path.join(COMFY, "output")
PAL_DIR = os.path.join(COMFY, "custom_nodes", "pixelart_palette")

# Pull the palette names straight from the node's registry so the two never
# drift apart (and so --list-palettes reflects palettes you add yourself).
sys.path.insert(0, PAL_DIR)
try:
    import palettes as _pal
    PALETTES = list(_pal.ALL_PALETTES.keys())
except Exception:
    _pal = None
    PALETTES = ["PICO-8", "DAWNBRINGER-16", "ENDESGA-32", "NES", "GAMEBOY", "C=64"]

# Dither methods — must match DITHER_METHODS in custom_nodes/pixelart_palette/nodes.py.
DITHER_METHODS = ["none", "bayer2", "bayer4", "bayer8", "bayer16", "clustered",
                  "floyd-steinberg", "jarvis", "stucki", "burkes", "sierra", "sierra2",
                  "sierra-lite", "atkinson"]

# Style guides (prompt snippets) loaded from styles.json next to this script.
# {name: {"prompt": "...added to positive...", "negative": "...added to negative..."}}
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
try:
    with open(os.path.join(_SCRIPT_DIR, "styles.json"), encoding="utf-8") as _sf:
        STYLES = {k: v for k, v in json.load(_sf).items() if not k.startswith("_")}
except Exception:
    STYLES = {}

# Named ComfyUI targets for `--server NAME`. Your personal servers.json (gitignored)
# is loaded if present; otherwise just the built-in 'local'. Copy servers.example.json
# to servers.json and add your machines, e.g. {"titan": "http://192.168.1.50:8188"}.
# "_default": "NAME[,...]" picks the target used when no --server / $PIXELMON_SERVER is given.
try:
    with open(os.path.join(_SCRIPT_DIR, "servers.json"), encoding="utf-8") as _svf:
        _svjson = json.load(_svf)
    SERVERS = {k: v for k, v in _svjson.items() if not k.startswith("_")}
    DEFAULT_SERVER = str(_svjson.get("_default") or "").strip()
    RESTARTS = {k: v for k, v in (_svjson.get("_restart") or {}).items() if not k.startswith("_") and v}
except Exception:
    SERVERS, DEFAULT_SERVER, RESTARTS = {}, "", {}
SERVERS.setdefault("local", "http://127.0.0.1:8188")


def resolve_server(value):
    """Resolve a --server value (a servers.json alias, or host[:port]/full URL) to a URL."""
    import urllib.parse
    url = SERVERS.get(value, value)            # alias if known, else treat as host/URL
    if "://" not in url:
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.port:                        # default ComfyUI port
        url = f"{parsed.scheme}://{parsed.hostname}:8188"
    return url.rstrip("/")


def is_local(url):
    """True for a ComfyUI on this machine (its files are on this disk)."""
    import urllib.parse
    host = urllib.parse.urlparse(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


# No --server / $PIXELMON_SERVER / "_default": the "local" alias, so a servers.json that repoints "local" (at a
# box on the LAN, or another port) is the one default everywhere, not a hard-coded 127.0.0.1:8188 here and that
# alias elsewhere.
SERVER = resolve_server("local")
REMOTE = not is_local(SERVER)


def _colors():
    """ANSI color codes — auto-disabled when piped or NO_COLOR is set."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return {k: "" for k in ("b", "dim", "cyan", "grn", "yel", "mag", "rst")}
    return {"b": "\033[1m", "dim": "\033[2m", "cyan": "\033[36m", "grn": "\033[32m",
            "yel": "\033[33m", "mag": "\033[35m", "rst": "\033[0m"}


C = _colors()


def print_help():
    c = C

    def opt(flag, desc, default=""):
        tail = f"  {c['dim']}[{default}]{c['rst']}" if default else ""
        return f"  {c['grn']}{flag:<19}{c['rst']} {desc}{tail}"

    def ex(cmd, note):
        return f"  {c['yel']}{cmd:<45}{c['rst']}  {c['dim']}{note}{c['rst']}"

    print("\n".join([
        f"{c['b']}{c['mag']}pixelmon{c['rst']} — generate pixel-art sprites from a text prompt",
        "",
        f"{c['b']}{c['cyan']}USAGE{c['rst']}",
        f"  {c['mag']}pixelmon{c['rst']} {c['yel']}\"a prompt\"{c['rst']} [options]",
        "",
        f"{c['b']}{c['cyan']}EXAMPLES{c['rst']}",
        ex('pixelmon "a fierce dragon"', "best quality (the default)"),
        ex('pixelmon "a fierce dragon" --art', "full-res digital art, not pixels"),
        ex('pixelmon "a spider" --style geometric', "sharp, angular style guide"),
        ex('pixelmon "a goblin" -n 8 --palette random', "8 variations, random palettes"),
        ex('pixelmon "a knight" --transparent --preview', "transparent + zoomed preview"),
        ex('pixelmon --batch "bat,skeleton,spider" -n 128', "128 of each → own folders"),
        ex("pixelmon --batch-file sprites.txt -n 4", "a game's sprite list: 'key | prompt' rows"),
        ex('pixelmon "a slime" --palette-from old.png', "use an old sprite's colours"),
        ex('pixelmon "a bandit" --animate "smoke from cigar"', "looping animated GIF"),
        "",
        f"{c['b']}{c['cyan']}OPTIONS{c['rst']}",
        opt("prompt", "what to draw (in quotes)"),
        opt("-n, --number N", "how many to make, each a different seed", "1"),
        opt('--batch "a,b,c"', "round-robin subjects → a folder each (N of each)"),
        opt("--batch-file FILE", "a sprite list: 'key | prompt' rows (+ 'key | style | size | prompt'), files named by key"),
        opt("--skip-existing", "--batch/--batch-file/-n: only make the takes that aren't there yet (resume a list)"),
        opt("--size N|WxH", "square N, or non-square WxH e.g. 32x48", "128"),
        opt("--out N|WxH", "exact final canvas size (default: --size); also with --snap-pixels"),
        opt("--art", "DIGITAL ART (not pixels): full-res illustration, no downscale", "1024"),
        opt("--raw", "the LoRA's own full-res picture: pixel prompt + LoRA, no palette / downscale step"),
        opt("--palette-strength F", "--art + --palette: pull colors toward the palette, 1 = exact (+ --dither)", "0.6"),
        opt("--post-sweep SPEC", "render once, save every combination: 'angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1'"),
        opt("--palette NAME", "none / random / a name (--list-palettes)", "none"),
        opt("--palette-from IMG", "lock to a reference picture's own colours (sets --palette Custom)"),
        opt("--palette-max N", "--palette-from: most colours to take", "all up to 64, else 32"),
        opt("--style NAMES", "append proven style guide(s) — see --list-styles"),
        opt("--transparent", "cut out background -> transparent PNG"),
        opt("--cutout", "cut out the subject with BiRefNet on the render server (any background)"),
        opt("--cutout-key [TOL]", "--cutout: also keep what's far from the bg colour (grey-on-grey parts)", "34"),
        opt("--cutout-model FILE", "background-removal model (models/background_removal/)", "birefnet"),
        opt("--fit-to OLD.png", "fit the cut-out into an old sprite's footprint (same size + ground line)"),
        opt("--fit-scale N", "--fit-to canvas = old size x N", "4 art / 1 pixel"),
        opt("--mesh IMG", "make a 3D model (.glb) of the picture's subject (Hunyuan3D 2 + Blender preview)"),
        opt("--turnaround SRC", "rotation sprites from a picture or a model: 3D -> N views -> painted to match"),
        opt("--angles N", "--turnaround: views around the model", "8"),
        opt("--elev DEG", "--turnaround: camera height above eye level", "0"),
        opt("--shape SPEC", "--mesh/--turnaround: build it from the outline: extrude[:DEPTH[,SHARE:DEPTH]] | lathe"),
        opt("--shape-grid N", "--shape: cells across the outline", "64"),
        opt("--lowpoly FACES", "--mesh/--turnaround: faceted low-poly model (e.g. 400 faces)"),
        opt("--texture", "--mesh/--turnaround: + a clean, textured model (quads, UVs, baked picture)"),
        opt("--texture-size PX", "--texture: texture size  [1024 up to 1500 faces, else 2048]"),
        opt("--texture-elev DEG", "--texture: how high the picture was seen from  [guessed: 0/30/55]"),
        opt("--blender CMD", "Blender for 3D", "blender / org.blender.Blender flatpak"),
        opt("--mesh-model FILE", "Hunyuan3D 2 checkpoint (models/checkpoints/)", "hunyuan3d-dit-v2_fp16"),
        opt("--tile [x|y]", "seamless tileable texture (+ a 3x3 _tiled_ preview)"),
        opt("--dither [NAME]", "dither between palette colors: bayer2/4/8/16, clustered, floyd-steinberg,"),
        opt("", "  jarvis, stucki, burkes, sierra, sierra2, sierra-lite, atkinson", "floyd-steinberg"),
        opt("--dither-amount F", "dither strength 0..1", "0.75"),
        opt("--snap-pixels", "find the model's own pixel grid instead of shrinking to --out — extra crisp"),
        opt("--snap-method M", "unfake (model's own grid, reduced to --out) or snapper", "unfake"),
        opt("--color-match M", "palette lock: oklab (perceptual) or redmean (classic)", "oklab"),
        opt("--fit-subject", "crop to the subject first so it fills the frame — tiny sprites stay readable"),
        opt("--despeckle N", "remove stray color islands of <= N px (0 = off)", "2"),
        opt("--pixel-angles F", "EXPERIMENTAL: clean pixel-art edge angles (0 off, ~1.5)", "0"),
        opt("--angle-grid G", "pixel / square / diagonal / isometric / hex / triangle", "pixel"),
        opt("--pixel-size WxH", "exact art pixel size: 1x1, 2x1, 2x2, 4x1 … (fixes huge snapped pixels)", "auto"),
        opt("--thin-lines N", "1-px outlines: thin dark strokes up to N px (0 off, ~4)", "0"),
        opt("--fast", "LCM mode: ~5x faster, slightly softer"),
        opt("--seed N", "lock / repeat a result (re-run a favorite)", "random"),
        opt("--steps N", "refinement steps (more = slower)", "25"),
        opt("--cfg N", "prompt adherence (higher = stricter)", "7"),
        opt("--lora-strength N", "how strongly to pixelate", "1.0"),
        opt("--bg-tolerance N", "bg color match for --transparent", "16"),
        opt('--custom-hex "..."', "colors for --palette Custom"),
        opt("--list-palettes", "show every palette name"),
        opt("--list-styles", "show every style guide"),
        opt("--list-servers", "each render server: up?, GPU, queue, has what this command needs?"),
        opt("-h, --help", "show this help"),
        opt("--version", "print the pixelmon version"),
        "",
        f"{c['b']}{c['cyan']}ADVANCED{c['rst']}",
        opt("--server NAMES", "render on a remote ComfyUI (alias/host/URL); comma-list = render farm across GPUs", DEFAULT_SERVER or "local"),
        opt("--no-check", "don't check the server has every node / input / model first"),
        opt("--dry-run", "show the jobs, prompts and where files go; send nothing"),
        opt("--print-graph", "print the first job's ComfyUI API graph (JSON) and exit"),
        opt("--smooth MODE", "pre-downscale flatten: mode / median / none", "mode"),
        opt("--filter MODE", "downscale: nearest (crisp) / box (soft)", "nearest"),
        opt("--preview", "also save an enlarged zoomed-in PNG"),
        opt("--view-scale N", "enlarge factor for --preview", "8"),
        opt('--negative "..."', "negative prompt (what to avoid)"),
        opt("--name NAME", "output filename base", "from prompt"),
        opt("--res N", "SDXL generation resolution", "1024"),
        opt("--pixel-grid N", "art-grid long side before the resize to --size (0 = auto)", "0"),
        opt("--snap-colors N", "colour cap for --snap-pixels (0 = auto)", "0"),
        opt("--sampler NAME", "ksampler sampler", "euler / lcm"),
        opt("--scheduler NAME", "ksampler noise schedule (karras, exponential …)", "normal / sgm_uniform"),
        opt("--base FILE", "SDXL checkpoint", "sd_xl_base_1.0"),
        opt("--lora FILE", "pixel-art LoRA", "pixel-art-xl"),
        opt("--lcm-lora FILE", "LCM LoRA (used with --fast)", "lcm-lora-sdxl"),
        opt("--no-lora", "base model only (skip pixel LoRA)"),
        opt("--steer DIR", "steer toward a folder of reference images (IPAdapter)"),
        opt("--steer-max N", "most references taken from the folder (evenly sampled)", "16"),
        opt("--steer-weight-type T", "IPAdapter weight type (linear, composition …)", "style transfer"),
        opt("--steer-combine M", "blend the refs: concat / average / norm average / add / subtract", "concat"),
        opt("--steer-model FILE", "IPAdapter model (models/ipadapter/)", "ip-adapter-plus_sdxl_vit-h"),
        opt("--steer-clip FILE", "CLIP-vision model (models/clip_vision/)", "CLIP-ViT-H-14-laion2B"),
        opt("--steer-start/--steer-end F", "when (fraction of steps) the refs apply", "0 / 1"),
        opt("--depth IMG", "depth ControlNet: lock the camera angle + geometry of an image; the prompt sets the rest"),
        opt("--depth-strength F", "how strictly to follow the depth map, 0..2", "0.7"),
        opt("--depth-start/--depth-end F", "when (fraction of steps) the depth map applies", "0 / 0.65"),
        opt("--depth-raw", "the --depth image is already a depth map (skip Depth Anything V2)"),
        opt("--depth-mask-bg", "cut out the foreground: all background depth goes black (no scenery)"),
        opt("--depth-keep-bg", "keep a plain backdrop's depth (it's blacked out automatically)"),
        opt("--depth-only", "make and save the processed depth map, then stop"),
        opt("--depth-model FILE", "depth ControlNet (models/controlnet/)", "controlnet-depth-sdxl-1.0"),
        opt("--depth-bg-model FILE", "background-removal model for --depth-mask-bg", "birefnet"),
        opt("--init IMG", "img2img: start from an image (keeps its composition and, without --size, its shape)"),
        opt("--control IMG", "ControlNet: keep an image's shape, restyle it (--control-mode canny|tile)"),
        opt("--control-strength F", "how strictly to follow the --control image, 0..2", "0.8"),
        opt("--control-end F", "fraction of the steps the control applies for", "0.8"),
        opt("--control-model FILE", "ControlNet (models/controlnet/)", "controlnet-union-sdxl-promax"),
        opt("--canny-low/--canny-high F", "canny edge thresholds (lower = more edges)", "0.3 / 0.7"),
        opt("--inpaint-model FILE", "with --mask: an SDXL inpainting UNet (models/unet/) — paints new things far better"),
        opt("--mask IMG", "inpaint with --init: redraw only the mask's white area"),
        opt("--mask-mode M", "fill (draw something new) / blend (tweak what's there)", "fill"),
        opt("--mask-grow PX", "blend margin around the mask", "6"),
        opt("--no-inpaint-crop", "--mask: render the whole picture, not just the area around the mask"),
        opt("--no-keep-outside", "--mask: don't paste the result back onto the original"),
        opt("--denoise F", "with --init: how much to change, 0..1", "0.6"),
        opt("--steer-strength N", "how strongly the refs influence the result", "0.7"),
        opt("--no-open", "don't auto-open the result"),
        opt("--show-prompt", "print the exact positive/negative prompts sent to the model"),
        opt("--exact-prompt", "send the prompt + --negative word for word: no added prefix/suffix/styles/default negative"),
        opt("--no-sprite-suffix", "drop 'game sprite … solid background' (auto for 'scene background')"),
        opt("--output-to DIR", "move outputs into DIR (relative to cwd)"),
        opt("--move-to-dirs", "put a run in its own ./<prompt>/ folder"),
        opt("--create-dirs", "create output folders if missing"),
        opt("--no-subdirs", "with --batch/--output-to: dump all into one flat folder"),
        "",
        f"{c['b']}{c['cyan']}ANIMATION{c['rst']}  {c['dim']}(EXPERIMENTAL — looping portrait gestures → GIF; "
        f"best for glow/light. for crisp sprites, hand-animate a static render){c['rst']}",
        opt("--animate GESTURE", "preset (blink/talk/glow/smoke/breathe) or free text"),
        opt("--anim-region WHAT", 'what to auto-mask, e.g. "the cigar" (CLIPSeg)', "guessed"),
        opt("--anim-frames N", "2 = toggle (blink); 3-5 = motion (smoke)", "preset"),
        opt("--anim-fps N", "loop SPEED (frames/sec during the gesture)", "6"),
        opt("--anim-hold S", "seconds the resting pose lingers", "1.2"),
        opt("--anim-denoise D", "region change strength 0.3 subtle .. 0.9 strong", "0.65"),
        opt("--anim-loop MODE", "pingpong / cycle / once-return", "preset"),
        opt("--anim-box L,T,R,B", "manual mask box (fractions) if auto-mask misses"),
        opt("--anim-res N", "base/inpaint gen resolution (detail)", "768"),
        "",
        f"{c['b']}{c['cyan']}OUTPUT{c['rst']}",
        f"  {c['dim']}{OUTPUT}/pixelmon/{c['rst']}",
        f"  {c['dim']}true-size _sprite_ PNG  (add --preview for an enlarged _preview_ PNG){c['rst']}",
        "",
        f"  {c['b']}{c['yel']}TIP{c['rst']}  explore with {c['grn']}--fast{c['rst']}, then re-run the "
        f"{c['grn']}--seed{c['rst']} you liked (without --fast) for the full-quality keeper.",
        "",
    ]))


def print_palettes():
    c = C
    counts = getattr(_pal, "ALL_PALETTES", {}) or {}
    print(f"{c['b']}{c['cyan']}Palettes{c['rst']}  {c['dim']}(use with --palette NAME){c['rst']}\n")
    print(f"  {c['grn']}{'none':<14}{c['rst']} {c['dim']}keep the model's own colors{c['rst']}")
    for name in PALETTES:
        n = len(counts.get(name, []))
        ncol = f"{c['dim']}{n} colors{c['rst']}" if n else ""
        print(f"  {c['grn']}{name:<14}{c['rst']} {ncol}")
    print(f"  {c['grn']}{'Custom':<14}{c['rst']} "
          f"{c['dim']}your own: --custom-hex \"#000000 #ffffff ...\"{c['rst']}")


def print_styles():
    c = C
    print(f"{c['b']}{c['cyan']}Style guides{c['rst']}  "
          f"{c['dim']}(append with --style NAME[,NAME2] — combine freely){c['rst']}\n")
    if not STYLES:
        print("  (none — styles.json not found)")
        return
    for name, spec in STYLES.items():
        prm = spec.get("prompt", "")
        prm = prm if len(prm) <= 58 else prm[:57] + "…"
        print(f"  {c['grn']}{name:<12}{c['rst']} {c['dim']}{prm}{c['rst']}")


def slug(text):
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out[:40] or "monster"


# Default negatives differ by mode. The pixel one pushes AWAY from realism/gradients
# (things that ruin a sprite); the art one keeps those but pushes away from junk
# (blur, artifacts, bad anatomy). Used only when the user doesn't pass --negative.
PIXEL_NEGATIVE = ("3d render, realistic, photograph, blurry, smooth gradient, "
                  "antialiased, jpeg artifacts, text, watermark, signature")
ART_NEGATIVE = ("lowres, blurry, jpeg artifacts, text, watermark, signature, "
                "deformed, bad anatomy, extra limbs, disfigured, ugly, out of frame")


def with_article(subject):
    """'goblin' -> 'a goblin', but leave 'a goblin' / 'the castle' / 'an ogre' alone."""
    return subject if re.match(r"(a|an|the)\s", subject.strip(), re.I) else f"a {subject}"


# --tile: what a seamless texture must NOT have (anything that marks an edge, a centre or a viewpoint)
TILE_NEGATIVE = "border, frame, vignette, perspective, horizon, single object, character, centered composition"
TILE_POSITIVE = "seamless repeating texture, flat even lighting, pattern fills the whole frame"


def art_positive(subject, style_add):
    """Build the POSITIVE prompt for --art (full-res digital art) mode.

    Unlike the pixel scaffold ("pixel, a X, game sprite, flat colors"), this leans
    INTO detail and lighting. `subject` is what to draw; `style_add` is any joined
    --style snippet(s). Returns the finished comma-joined positive prompt string.

    This is the taste-driven knob of art mode — tune the quality boosters below to
    steer the house look (painterly vs. photoreal vs. concept-art, etc.).
    """
    parts = [with_article(subject)]
    if style_add:
        parts.append(style_add)
    parts.append("highly detailed digital painting, intricate detail, "
                 "dramatic lighting, sharp focus, professional illustration")
    return ", ".join(parts)


def resolve_styles(style):
    """'dark,outline' -> (joined style prompts, joined style negatives)."""
    names = [s.strip() for s in (style or "").replace(",", " ").split() if s.strip()]
    adds, negs = [], []
    for nm in names:
        if nm not in STYLES:
            raise ValueError(f"unknown style {nm!r}. See --list-styles.")
        adds.append(STYLES[nm].get("prompt", ""))
        if STYLES[nm].get("negative"):
            negs.append(STYLES[nm]["negative"])
    return ", ".join(x for x in adds if x), ", ".join(negs)


def is_scene(a, subject):
    """Scenes skip the sprite suffix: --no-sprite-suffix, or the 'scene background'
    caption tag the dos-art LoRAs were trained with."""
    return a.no_sprite_suffix or "scene background" in subject.lower()


def final_prompts(a, subject):
    """The exact (positive, negative) prompt pair sent to the sampler."""
    if getattr(a, "exact_prompt", False):   # --exact-prompt: nothing added, nothing changed
        return subject, a.negative
    # The Pixel Art XL LoRA does the heavy lifting; the base prompt stays simple
    # and --style snippets (a.style_add) do the steering. "game sprite ... solid
    # background" keeps sprites clean, but fights full scenes, so scenes drop it.
    # Style negatives (a.style_neg) push away unwanted shapes/looks.
    if a.art:
        prompt = art_positive(subject, a.style_add)
        if a.tile:
            prompt += ", " + TILE_POSITIVE
    elif a.tile:
        # a texture is a surface, not a thing: no "a ...", no sprite suffix, no solid background
        prompt = ", ".join(x for x in ("pixel art texture", subject, a.style_add, TILE_POSITIVE) if x)
    else:
        parts = [f"pixel, {with_article(subject)}"]
        if a.style_add:
            parts.append(a.style_add)
        parts.append("simple flat colors" if is_scene(a, subject)
                     else "game sprite, simple flat colors, solid background")
        prompt = ", ".join(parts)
    negative = a.negative + ((", " + a.style_neg) if a.style_neg else "")
    if a.tile:
        negative += ", " + TILE_NEGATIVE
    return prompt, negative


# --post-sweep: settings the palette node applies after the model is done, so any number of them can be
# tried on ONE render. name on the command line -> the node's input name
POST_FIELDS = {"angle_grid": str, "dither": str, "dither_amount": float, "pixel_angles": float,
               "thin_lines": int, "despeckle": int, "palette": str}
POST_NODE_INPUT = {"dither": "dithering"}


def post_tag(combo):
    """a short filename tag for one combination, e.g. g-hex_d-bayer4-0.5"""
    short = {"angle_grid": "g", "dither": "d", "pixel_angles": "a", "thin_lines": "t", "despeckle": "s", "palette": "p"}
    parts = []
    for f, v in combo.items():
        if f == "dither_amount":
            continue
        tag = f"{short.get(f, f)}-{v}"
        if f == "dither" and v != "none" and "dither_amount" in combo:
            tag += f"-{combo['dither_amount']:g}"
        parts.append(re.sub(r"[^A-Za-z0-9.=+-]", "", tag))
    if "dither_amount" in combo and "dither" not in combo:
        parts.append(f"amt-{combo['dither_amount']:g}")
    return "_".join(parts)[:80]


def post_label(combo):
    bits = []
    for f, v in combo.items():
        if f == "dither_amount" and combo.get("dither") not in (None, "none"):
            continue
        if f == "dither" and v != "none" and "dither_amount" in combo:
            bits.append(f"dither {v} @ {combo['dither_amount']:g}")
        elif f == "dither_amount" and "dither" in combo:
            continue
        else:
            bits.append(f"{f.replace('angle_grid', 'grid').replace('_', ' ')} {v:g}" if isinstance(v, float) else
                        f"{f.replace('angle_grid', 'grid').replace('_', ' ')} {v}")
    return " · ".join(bits)


def parse_post_sweep(spec, p):
    """'angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1' -> every combination (dither 'none' once:
    its amount changes nothing)"""
    import itertools
    fields = []
    for part in [x for x in str(spec).split(";") if x.strip()]:
        if "=" not in part:
            p.error(f"--post-sweep: expected field=a,b,c, got {part!r}")
        f, vals = part.split("=", 1)
        f = f.strip()
        if f not in POST_FIELDS:
            p.error(f"--post-sweep: {f!r} isn't post-processing; use {', '.join(POST_FIELDS)}")
        try:
            vs = [POST_FIELDS[f](v.strip()) for v in vals.split(",") if v.strip()]
        except ValueError:
            p.error(f"--post-sweep: bad value in {part!r}")
        if not vs:
            p.error(f"--post-sweep: no values for {f}")
        fields.append((f, vs))
    combos, seen = [], set()
    for vals in itertools.product(*[vs for _, vs in fields]):
        c = dict(zip([f for f, _ in fields], vals))
        key = tuple(sorted((k, v) for k, v in c.items() if not (k == "dither_amount" and c.get("dither") == "none")))
        if key not in seen:
            seen.add(key)
            combos.append(c)
    if len(combos) > 2000:
        p.error(f"--post-sweep: {len(combos)} combinations is too many (max 2000)")
    return combos


def build_graph(a, seed, palette=None, subject=None, server=None):
    palette = palette or a.palette
    subject = subject if subject is not None else a.prompt
    prompt, negative = final_prompts(a, subject)

    name = slug(subject) if a.batch else (a.name or slug(subject))
    # seed in the filename so each variation is identifiable and re-runnable.
    tag = "art" if a.art else "raw" if a.raw else palette
    ow, oh = (a.sw, a.sh) if a.art else (a.gen_w, a.gen_h) if a.raw else (a.ow * a.enlarge, a.oh * a.enlarge)
    prefix = f"pixelmon/{name}_{ow}x{oh}_{tag}_s{seed}"

    g = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": a.base}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": a.gen_w, "height": a.gen_h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": None, "text": prompt}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": None, "text": negative}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": a.steps, "cfg": a.cfg,
                         "sampler_name": a.sampler, "scheduler": a.scheduler, "denoise": 1.0,
                         "model": None, "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    }
    if a.tile:   # seamless: the decoder wraps at the edges too (the UNet is patched further down)
        g["8"] = {"class_type": "CircularVAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2], "tiling": a.tile}}
    if a.art:
        # Digital-art mode: no pixelation, no palette lock. SDXL draws at its own resolution; a smaller
        # output is a smooth (lanczos) resize of that picture, never a tiny generation.
        art_img = ["8", 0]
        if (a.gen_w, a.gen_h) != (a.sw, a.sh):
            g["17"] = {"class_type": "ImageScale",
                       "inputs": {"image": ["8", 0], "upscale_method": "lanczos",
                                  "width": a.sw, "height": a.sh, "crop": "center"}}
            art_img = ["17", 0]
        g["11"] = {"class_type": "SaveImage",
                   "inputs": {"filename_prefix": prefix + "_art", "images": art_img}}
    elif a.raw:
        # Raw mode: the pixel render's model picture, saved as SDXL drew it (what the LoRA alone looks like)
        g["11"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix + "_raw", "images": ["8", 0]}}
    else:
        g["10"] = {"class_type": "PixelArtPalette",
                   "inputs": {"image": ["8", 0], "downscale_to": max(a.sw, a.sh), "palette": palette,
                              "pixel_grid": (a.pixel_grid if getattr(a, "pixel_grid", 0) > 0
                                             else (min(512, max(a.sw, a.sh)) if max(a.sw, a.sh) > 128 else 128)),
                              "dithering": a.dither, "dither_amount": a.dither_amount,
                              "downscale_filter": a.filter, "smooth": a.smooth,
                              "view_scale": a.view_scale, "custom_hex": a.custom_hex,
                              "transparent_bg": a.transparent, "bg_tolerance": a.bg_tolerance,
                              "snap_pixels": a.snap_pixels, "snap_colors": a.snap_colors, "snap_method": a.snap_method, "fit_subject": a.fit_subject, "color_match": a.color_match,
                              "out_width": a.ow, "out_height": a.oh, "despeckle": a.despeckle,
                              "pixel_angles": a.pixel_angles, "angle_grid": a.angle_grid,
                              "pixel_w": a.px_w, "pixel_h": a.px_h, "thin_lines": a.thin_lines,
                              **({"tile": a.tile} if a.tile else {})}}
        g["11"] = {"class_type": "SaveImage",
                   "inputs": {"filename_prefix": prefix + "_sprite", "images": ["10", 0]}}
        if a.preview:  # enlarged zoomed-in copy — opt-in; default saves only the true sprite
            g["12"] = {"class_type": "SaveImage",
                       "inputs": {"filename_prefix": prefix + "_preview", "images": ["10", 1]}}
        if getattr(a, "post_combos", None):
            # --post-sweep: the model renders ONCE; every combination is just another palette step on that
            # same picture (grid / dither / amount are post-processing), each saved under its own name
            base = g.pop("10")
            g.pop("11", None); g.pop("12", None)
            for k, combo in enumerate(a.post_combos):
                node = {"class_type": "PixelArtPalette", "inputs": {**base["inputs"], **{POST_NODE_INPUT.get(f, f): v for f, v in combo.items()}}}
                g[f"p{k}"] = node
                g[f"s{k}"] = {"class_type": "SaveImage",
                              "inputs": {"filename_prefix": f"{prefix}_v{k:03d}-{post_tag(combo)}_sprite", "images": [f"p{k}", 0]}}

    # Chain LoRAs onto the base: SDXL -> [Pixel Art XL] -> [LCM if --fast].
    # Each LoraLoader patches both the model and the text encoder (clip), so we
    # thread the "current" source through and wire the sampler/prompts to the end.
    model_src, clip_src = ["4", 0], ["4", 1]
    inpaint_model = bool(getattr(a, "inpaint_model", None) and getattr(a, "mask", None) and getattr(a, "init", None))
    if inpaint_model:
        # a dedicated SDXL inpainting UNet (trained to paint new things into a hole); the checkpoint still
        # supplies the text encoders and VAE, and LoRAs patch the inpainting UNet as usual
        g["60"] = {"class_type": "UNETLoader", "inputs": {"unet_name": a.inpaint_model, "weight_dtype": "default"}}
        model_src = ["60", 0]
    if not a.no_lora and not a.art:  # art mode never applies the pixel-art LoRA
        g["15"] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": clip_src, "lora_name": a.lora,
                              "strength_model": a.lora_strength, "strength_clip": a.lora_strength}}
        model_src, clip_src = ["15", 0], ["15", 1]
    if a.fast:
        g["16"] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": clip_src, "lora_name": a.lcm_lora,
                              "strength_model": 1.0, "strength_clip": 1.0}}
        model_src, clip_src = ["16", 0], ["16", 1]

    # --- Steering (IPAdapter): blend a folder of reference images into the model ---
    # Sits between the LoRA-loaded model and the sampler, alongside the text prompt.
    # The refs are CLIP-vision encoded and injected as a soft "image prompt", so the
    # output borrows their look without copying any one of them.
    if getattr(a, "steer", None):
        names = steer_files(a, server or SERVER)          # uploaded to THIS server, cached
        g["20"] = {"class_type": "IPAdapterModelLoader",
                   "inputs": {"ipadapter_file": a.steer_model}}
        g["21"] = {"class_type": "CLIPVisionLoader",
                   "inputs": {"clip_name": a.steer_clip}}
        # Load each reference and chain ImageBatch to feed them all in as one batch.
        batch_src = None
        for i, nm in enumerate(names):
            lid = str(30 + i)
            g[lid] = {"class_type": "LoadImage", "inputs": {"image": nm}}
            if batch_src is None:
                batch_src = [lid, 0]
            else:
                bid = str(60 + i)
                g[bid] = {"class_type": "ImageBatch",
                          "inputs": {"image1": batch_src, "image2": [lid, 0]}}
                batch_src = [bid, 0]
        g["22"] = {"class_type": "IPAdapterAdvanced",
                   "inputs": {"model": model_src, "ipadapter": ["20", 0],
                              "image": batch_src, "clip_vision": ["21", 0],
                              "weight": a.steer_strength, "weight_type": a.steer_weight_type,
                              "combine_embeds": a.steer_combine, "start_at": a.steer_start,
                              "end_at": a.steer_end, "embeds_scaling": "V only"}}
        model_src = ["22", 0]

    # --- Init image (img2img): start sampling from an existing picture instead of
    # pure noise, so the result keeps its composition; --denoise sets how much
    # changes (0 = identical, 1 = ignore it). The image is scaled nearest-neighbour
    # up to the generation size so small pixel art encodes as clean blocks.
    if getattr(a, "init", None):
        srv = server or SERVER
        if srv not in _INIT_UPLOADS:
            _INIT_UPLOADS[srv] = _upload_image(os.path.abspath(os.path.expanduser(a.init)), srv, "--init")
        g["40"] = {"class_type": "LoadImage", "inputs": {"image": _INIT_UPLOADS[srv]}}
        g["41"] = {"class_type": "ImageScale",
                   "inputs": {"image": ["40", 0], "upscale_method": "nearest-exact",
                              "width": a.gen_w, "height": a.gen_h, "crop": "disabled"}}
        g["42"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["41", 0], "vae": ["4", 2]}}
        g["3"]["inputs"]["latent_image"] = ["42", 0]
        g["3"]["inputs"]["denoise"] = a.denoise
        if getattr(a, "mask", None):
            # inpaint: only the white area of --mask is re-sampled; the rest of the latent is kept
            if srv not in _MASK_UPLOADS:
                _MASK_UPLOADS[srv] = _upload_image(os.path.abspath(os.path.expanduser(a.mask)), srv, "--mask")
            g["43"] = {"class_type": "LoadImage", "inputs": {"image": _MASK_UPLOADS[srv]}}
            g["44"] = {"class_type": "ImageScale",
                       "inputs": {"image": ["43", 0], "upscale_method": "nearest-exact",
                                  "width": a.gen_w, "height": a.gen_h, "crop": "disabled"}}
            g["46"] = {"class_type": "ImageToMask", "inputs": {"image": ["44", 0], "channel": "red"}}
            g["47"] = {"class_type": "GrowMask", "inputs": {"mask": ["46", 0], "expand": a.mask_grow, "tapered_corners": True}}
            if a.mask_mode == "fill":
                # fill: the masked area starts neutral, so the model draws something genuinely new there
                # (change / add / erase). blend: it starts from what's there (subtle tweaks).
                g["48"] = {"class_type": "VAEEncodeForInpaint",
                           "inputs": {"pixels": ["41", 0], "vae": ["4", 2], "mask": ["46", 0], "grow_mask_by": a.mask_grow}}
            else:
                g["48"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["42", 0], "mask": ["47", 0]}}
            g["3"]["inputs"]["latent_image"] = ["48", 0]

    # --- ControlNet (--control): keep the SHAPE of a reference image while the prompt / LoRA / palette
    # restyle everything else. canny = follow its edges; tile = keep its layout + colors. Uses the SDXL
    # "union" ControlNet (one model, many modes). Released at --control-end so the last steps are free.
    if getattr(a, "control", None):
        srv = server or SERVER
        if srv not in _CONTROL_UPLOADS:
            _CONTROL_UPLOADS[srv] = _upload_image(os.path.abspath(os.path.expanduser(a.control)), srv, "--control")
        g["50"] = {"class_type": "LoadImage", "inputs": {"image": _CONTROL_UPLOADS[srv]}}
        g["51"] = {"class_type": "ImageScale",
                   "inputs": {"image": ["50", 0], "upscale_method": "lanczos",
                              "width": a.gen_w, "height": a.gen_h, "crop": "center"}}
        hint = ["51", 0]
        if a.control_mode == "canny":
            g["52"] = {"class_type": "Canny", "inputs": {"image": ["51", 0], "low_threshold": a.canny_low, "high_threshold": a.canny_high}}
            hint = ["52", 0]
        g["53"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": a.control_model}}
        g["54"] = {"class_type": "SetUnionControlNetType",
                   "inputs": {"control_net": ["53", 0],
                              "type": "canny/lineart/anime_lineart/mlsd" if a.control_mode == "canny" else "tile"}}
        g["55"] = {"class_type": "ControlNetApplyAdvanced",
                   "inputs": {"positive": ["6", 0], "negative": ["7", 0], "control_net": ["54", 0], "image": hint,
                              "strength": a.control_strength, "start_percent": 0.0, "end_percent": a.control_end}}
        g["3"]["inputs"]["positive"] = ["55", 0]
        g["3"]["inputs"]["negative"] = ["55", 1]

    # --- Depth ControlNet (--depth): lock the camera angle + geometry of a reference while the prompt sets
    # subject, backdrop and style. The map was made once up front (prepare_depth) at the render size, so
    # every job — on any server — gets exactly the same picture. Chained after --control, so both can apply.
    if getattr(a, "depth", None):
        srv = server or SERVER
        if srv not in _DEPTH_UPLOADS:
            _DEPTH_UPLOADS[srv] = _upload_image(a._depth_map, srv, "--depth")
        g["d1"] = {"class_type": "LoadImage", "inputs": {"image": _DEPTH_UPLOADS[srv]}}
        g["d2"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": a.depth_model}}
        g["d3"] = {"class_type": "ControlNetApplyAdvanced",
                   "inputs": {"positive": g["3"]["inputs"]["positive"], "negative": g["3"]["inputs"]["negative"],
                              "control_net": ["d2", 0], "image": ["d1", 0], "strength": a.depth_strength,
                              "start_percent": a.depth_start, "end_percent": a.depth_end}}
        g["3"]["inputs"]["positive"] = ["d3", 0]
        g["3"]["inputs"]["negative"] = ["d3", 1]

    if inpaint_model:
        # the inpainting UNet needs the picture + mask as conditioning; its latent replaces the masked one
        g["61"] = {"class_type": "InpaintModelConditioning",
                   "inputs": {"positive": g["3"]["inputs"]["positive"], "negative": g["3"]["inputs"]["negative"],
                              "vae": ["4", 2], "pixels": ["41", 0], "mask": ["47", 0], "noise_mask": True}}
        g["3"]["inputs"]["positive"], g["3"]["inputs"]["negative"] = ["61", 0], ["61", 1]
        g["3"]["inputs"]["latent_image"] = ["61", 2]
    if a.tile:
        # circular padding in the UNet, applied last so it wraps the LoRA / IPAdapter-patched model
        g["70"] = {"class_type": "SeamlessTile", "inputs": {"model": model_src, "tiling": a.tile}}
        model_src = ["70", 0]
        # a 3x3 repeat of the result, to check the seams by eye
        if "11" in g:
            g["71"] = {"class_type": "TilePreview", "inputs": {"image": g["11"]["inputs"]["images"], "repeat": 3, "tiling": a.tile}}
            g["72"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix + "_tiled", "images": ["71", 0]}}
    if getattr(a, "cutout", False) and ("10" in g or "11" in g) and not (a.fit_subject and "10" in g):
        # (--fit-subject crops before pixelating, so its mask wouldn't line up: that one is cut out after)
        # --cutout: BiRefNet finds the subject in the full-size picture (before any pixelation, where it's
        # clearest); cutout_output() shrinks this mask onto the finished file the same way the picture was
        src = g["10"]["inputs"]["image"] if "10" in g else g["11"]["inputs"]["images"]
        g["c1"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": a.cutout_model}}
        g["c2"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["c1", 0], "image": src}}
        g["c3"] = {"class_type": "MaskToImage", "inputs": {"mask": ["c2", 0]}}
        g["c4"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix + "_cutmask", "images": ["c3", 0]}}
    g["6"]["inputs"]["clip"] = clip_src
    g["7"]["inputs"]["clip"] = clip_src
    g["3"]["inputs"]["model"] = model_src
    return g


# Prompts this run has sent and not yet seen finish, per server. On Ctrl-C, a stop signal (the GUI's Cancel) or
# any early exit they are taken off ComfyUI's queue, and the one rendering is interrupted only if it is ours: a
# shared ComfyUI may be busy with soundmon, the GUI or another pixelmon, and those must keep going. Without this,
# `-n 128` + Ctrl-C (or one failed job ending a batch) left the rest rendering on the GPU for nobody.
SENT = {}
STOPPING = False    # set by a stop signal: code that catches SystemExit (a box refusing a job) must let it through


def _forget(server, pid):
    SENT.get(server, set()).discard(pid)


def cancel_sent(why="stopped"):
    """Remove this run's prompts from every server's queue; interrupt the running one if it is ours."""
    told = []
    for server, pids in list(SENT.items()):
        if not pids:
            continue
        ids = sorted(pids)
        try:
            req = urllib.request.Request(f"{server}/queue", data=json.dumps({"delete": ids}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).close()
            with urllib.request.urlopen(f"{server}/queue", timeout=3) as r:
                running = {e[1] for e in json.loads(r.read()).get("queue_running", []) if len(e) > 1}
            if running & pids:
                urllib.request.urlopen(urllib.request.Request(f"{server}/interrupt", data=b"{}", method="POST",
                                                              headers={"Content-Type": "application/json"}),
                                       timeout=3).close()
            told.append(f"{_short(server)} ({len(ids)} prompt{'s' if len(ids) > 1 else ''})")
        except Exception:
            told.append(f"{_short(server)} (unreachable; its queue may still hold {len(ids)})")
        pids.clear()
    if told:
        print(f"   ⛔ {why}: cleared {', '.join(told)}", flush=True)


def _on_stop(signum, frame):
    import signal
    global STOPPING
    STOPPING = True
    signal.signal(signum, signal.SIG_IGN)             # a second Ctrl-C while cleaning up shouldn't cut it short
    cancel_sent("cancelled")
    sys.exit(130 if signum == signal.SIGINT else 143)


def rejection_text(body):
    """ComfyUI's 400 answer to /prompt, as one readable line per problem ("node 10 (PixelArtPalette): ...")
    instead of a 1200-character JSON dump."""
    try:
        j = json.loads(body)
    except ValueError:
        return body.strip()[:1200]
    lines = []
    err = j.get("error")
    head = (err.get("message") if isinstance(err, dict) else err) or "rejected"
    for nid, ne in sorted((j.get("node_errors") or {}).items(), key=lambda kv: str(kv[0])):
        cls = ne.get("class_type") or ""
        for e in ne.get("errors") or []:
            msg = e.get("message") or e.get("type") or "error"
            det = (e.get("details") or "").strip()
            inp = (e.get("extra_info") or {}).get("input_name")
            what = f"node {nid}" + (f" ({cls})" if cls else "")
            lines.append(f"   {what}: {msg}" + (f" — {det}" if det and det not in msg else
                                                 f" — input {inp}" if inp and inp not in msg else ""))
    if isinstance(err, dict) and err.get("details") and not lines:
        head += f" — {err['details']}"
    return "\n".join([head] + lines[:20] + ([f"   … and {len(lines) - 20} more"] if len(lines) > 20 else []))


def submit(graph, server=None):
    server = server or SERVER
    if not CHECKS_OFF:
        probs = graph_problems(graph, server)
        if probs:
            sys.exit(f"{_short(server)} can't render this:\n" + "\n".join(f"   • {x}" for x in probs)
                     + "\n" + provision_hint(probs, server))
    data = json.dumps({"prompt": graph}).encode()
    req = urllib.request.Request(f"{server}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        pid = json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        sys.exit(f"ComfyUI on {_short(server)} rejected the request: " + rejection_text(e.read().decode(errors="replace")))
    except urllib.error.URLError:
        sys.exit("Couldn't reach ComfyUI at " + server + " — is the server running?")
    SENT.setdefault(server, set()).add(pid)
    return pid


# --- capability check: does a box have every node, input and model file a graph needs? ---------------------
# A farm box with a stale custom_nodes/pixelart_palette silently IGNORES inputs it doesn't know (ComfyUI drops
# undeclared inputs), so thin lines / pixel size / oklab just don't happen there; a box missing a LoRA or the
# ControlNet fails mid-batch. Ask each box once (/object_info/<node>, cached) before sending it work.
CHECKS_OFF = False          # --no-check
_OBJ_INFO = {}              # (server, class) -> its object_info entry, or None when the server has no such node
MODEL_INPUTS = {"ckpt_name", "lora_name", "control_net_name", "ipadapter_file", "clip_name", "unet_name",
                "bg_removal_name"}
UPLOAD_NODES = {"LoadImage", "LoadImageMask"}     # their file list is what this run uploads


def _object_info(server, cls):
    import urllib.parse
    key = (server, cls)
    if key not in _OBJ_INFO:
        with urllib.request.urlopen(f"{server}/object_info/{urllib.parse.quote(cls)}", timeout=15) as r:
            info = json.loads(r.read())
        _OBJ_INFO[key] = info.get(cls) or None
    return _OBJ_INFO[key]


def _combo_options(spec):
    """The allowed values of a combo input spec: legacy [[a, b], {...}] or the newer ["COMBO", {"options": [...]}]."""
    if isinstance(spec, (list, tuple)) and spec:
        if isinstance(spec[0], list):
            return spec[0]
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return spec[1].get("options")
    return None


def graph_problems(graph, server):
    """What `server` lacks to run `graph` as asked: node classes it doesn't have, inputs its version of a node
    doesn't know, model files that aren't installed. [] = ready, or couldn't ask (an old ComfyUI, or the server
    is unreachable — submit reports that)."""
    probs = []
    try:
        for node in graph.values():
            cls = node.get("class_type")
            info = _object_info(server, cls)
            if info is None:
                probs.append(f"no {cls} node — its custom node isn't installed (or ComfyUI wasn't restarted)")
                continue
            spec = info.get("input") or {}
            known = dict(spec.get("required") or {}, **(spec.get("optional") or {}))
            if not known:                               # nothing to compare against
                continue
            unknown = []
            for k, v in (node.get("inputs") or {}).items():
                if k not in known and k not in (spec.get("hidden") or {}):
                    unknown.append(k)
                elif k in MODEL_INPUTS and cls not in UPLOAD_NODES and isinstance(v, str):
                    opts = _combo_options(known.get(k))
                    if opts is not None and v not in opts:
                        probs.append(f"{k.replace('_name', '').replace('_file', '')} {v} isn't installed ({cls})")
            if unknown:
                probs.append(f"{cls} doesn't know input{'s' if len(unknown) > 1 else ''} {', '.join(unknown)} — "
                             f"that box runs an older {cls}")
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    return list(dict.fromkeys(probs))


def _model_groups():
    """model file -> the download-models.sh flag that fetches it ('' = the core set), read from the script itself
    so the hint can't drift from it."""
    groups, flag = {}, ""
    flags = {"WANT_LAB": "--lab", "WANT_STEER": "--steer", "WANT_DEPTH": "--depth", "WANT_3D": "--3d",
             "WANT_JUGG": "--juggernaut"}
    try:
        with open(os.path.join(_SCRIPT_DIR, "download-models.sh"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'if \[ "\$(WANT_\w+)" = 1 \]', line.strip())
                if m:
                    flag = flags.get(m.group(1), "")
                elif line.strip() == "fi":
                    flag = ""
                else:
                    m = re.match(r"\s+\w+ (\S+\.(?:safetensors|pth|ckpt|bin)) \"", line)
                    if m:
                        groups[m.group(1)] = flag
    except OSError:
        pass
    return groups


def provision_hint(probs, server=None):
    """What to run on the box to fix `probs`."""
    groups = _model_groups()
    want, other = set(), False
    for x in probs:
        m = re.match(r"\w+ (\S+) isn't installed", x)
        if m and m.group(1) in groups:
            want.add(groups[m.group(1)])
        elif m:
            other = True                                # a model download-models.sh doesn't know (your own LoRA)
    out = []
    if want:
        out.append("   provision it:  ./download-models.sh " + " ".join(sorted(f for f in want if f)))
    if other:
        out.append("   copy your own models into the same models/ folders on that box")
    if any("doesn't know input" in x or "node —" in x for x in probs):
        out.append("   update its pixelmon (custom_nodes/ + ./install.sh), then restart its ComfyUI")
    out.append("   (--no-check skips this check)")
    return "\n".join(o.rstrip() for o in out)


_STEER_UPLOADS = {}   # server -> [uploaded reference filenames] (uploaded once per server)
DRY = "dry://"        # a "server" that is never contacted: build_graph for --dry-run and the capability check


def _gather_steer_paths(spec, cap):
    """Resolve --steer (a folder or a single image) to a sorted list of image paths."""
    p = os.path.abspath(os.path.expanduser(spec))
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    if os.path.isdir(p):
        files = sorted(os.path.join(p, n) for n in os.listdir(p)
                       if n.lower().endswith(exts) and os.path.isfile(os.path.join(p, n)))
    elif os.path.isfile(p):
        files = [p]
    else:
        sys.exit(f"--steer: no such file or folder: {spec}")
    if not files:
        sys.exit(f"--steer: no image files (png/jpg/webp/bmp/gif) in {spec}")
    if len(files) > cap:
        # evenly sample across the (sorted) set so the blend spans all of them,
        # not just the first N alphabetically (which would skew to early subjects).
        step = len(files) / float(cap)
        files = [files[int(i * step)] for i in range(cap)]
        print(f"   \U0001f9ed steer: evenly sampling {cap} of the references (raise with --steer-max)")
    return files


def _upload_image(path, server, what="--steer"):
    """POST one image to ComfyUI's /upload/image (so LoadImage can reference it). Returns the server-side name.
    `what` names the flag in error messages. Retries for ~20s, so a server that's briefly restarting doesn't
    fail the job."""
    import mimetypes, hashlib
    if server == DRY:                            # --dry-run / the capability check: name it, send nothing
        return f"<upload:{os.path.basename(path)}>"
    with open(path, "rb") as f:
        content = f.read()
    name = "pmsteer_" + hashlib.md5(path.encode()).hexdigest()[:8] + "_" + os.path.basename(path)
    ct = mimetypes.guess_type(name)[0] or "image/png"
    b = "----pixelmonSteerBoundary"
    crlf = "\r\n"
    body = b"".join([
        ("--%s%s" % (b, crlf)).encode(),
        ('Content-Disposition: form-data; name="overwrite"%s%strue%s' % (crlf, crlf, crlf)).encode(),
        ("--%s%s" % (b, crlf)).encode(),
        ('Content-Disposition: form-data; name="image"; filename="%s"%s' % (name, crlf)).encode(),
        ("Content-Type: %s%s%s" % (ct, crlf, crlf)).encode(),
        content,
        ("%s--%s--%s" % (crlf, b, crlf)).encode(),
    ])
    req = urllib.request.Request(f"{server}/upload/image", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + b})
    for attempt in range(6):
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            return resp.get("name", name)
        except urllib.error.HTTPError as e:
            sys.exit(f"{what} upload failed: " + e.read().decode()[:500])
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt < 5:
                time.sleep(4)
    sys.exit(f"{what}: couldn't reach {server} to upload the image (tried for ~20s).")


_INIT_UPLOADS = {}      # server -> uploaded --init image name
_CONTROL_UPLOADS = {}   # server -> uploaded --control image name
_MASK_UPLOADS = {}      # server -> uploaded --mask image name
_DEPTH_UPLOADS = {}     # server -> uploaded processed depth map name


def plain_backdrop(path):
    """True if the picture stands on an empty backdrop: at least 3 of its 4 sides are see-through, or one
    colour (a sprite padded with black, a cut-out on white; the 4th side may be what it stands on, or a
    first-person weapon rising from the bottom). Depth Anything turns such a backdrop into a wall or a
    ceiling, and the image model then paints something there."""
    from PIL import Image
    import numpy as np
    try:
        im = Image.open(path)
        im.thumbnail((256, 256))
        a = np.asarray(im.convert("RGBA")).astype(np.int16)
    except OSError:
        return False
    sides = [a[:2].reshape(-1, 4), a[-2:].reshape(-1, 4), a[:, :2].reshape(-1, 4), a[:, -2:].reshape(-1, 4)]
    if sum((e[:, 3] < 16).mean() > 0.95 for e in sides) >= 3:
        return True
    med = np.median(np.concatenate(sides)[:, :3], axis=0)
    return sum((np.abs(e[:, :3] - med).max(axis=1) <= 12).mean() > 0.95 for e in sides) >= 3


def prepare_depth(a, dest_dir, name):
    """Turn --depth into the exact depth map the sampler gets, once, before any render: the reference is
    center-cropped to the native SDXL render size (a.gen_w x a.gen_h, not --out), run through Depth Anything
    V2 (unless --depth-raw), optionally has its background blacked out (--depth-mask-bg, BiRefNet), and is
    saved to <dest_dir>/depth/. Sets a._depth_map to that file and returns its path."""
    srv = next((s for s in (POOL or [SERVER]) if server_up(s)), SERVER)
    src = _upload_image(os.path.abspath(os.path.expanduser(a.depth)), srv, "--depth")
    gw, gh = a.gen_w, a.gen_h
    g = {"1": {"class_type": "LoadImage", "inputs": {"image": src}},
         "2": {"class_type": "ImageScale", "inputs": {"image": ["1", 0], "upscale_method": "lanczos",
                                                      "width": gw, "height": gh, "crop": "center"}}}
    depth = ["2", 0]
    if not a.depth_raw:
        g["3"] = {"class_type": "DepthAnythingV2Preprocessor",
                  "inputs": {"image": ["2", 0], "ckpt_name": "depth_anything_v2_vitl.pth",
                             "resolution": max(64, int(round(max(gw, gh) / 64.0)) * 64)}}
        g["4"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos",
                                                         "width": gw, "height": gh, "crop": "disabled"}}
        depth = ["4", 0]
    if a.depth_mask_bg:
        # foreground mask from the (cropped) picture; outside it the depth map becomes pure black = "nothing there"
        g["5"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": a.depth_bg_model}}
        g["6"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["5", 0], "image": ["2", 0]}}
        g["7"] = {"class_type": "ThresholdMask", "inputs": {"mask": ["6", 0], "value": 0.5}}
        g["8"] = {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": gw, "height": gh}}
        g["9"] = {"class_type": "MaskToImage", "inputs": {"mask": ["8", 0]}}
        g["10"] = {"class_type": "ImageCompositeMasked",
                   "inputs": {"destination": ["9", 0], "source": depth, "x": 0, "y": 0,
                              "resize_source": False, "mask": ["7", 0]}}
        depth = ["10", 0]
    g["99"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": f"pixelmon/depth/{name}_{gw}x{gh}_depth",
                                                     "images": depth}}
    print(f"   \U0001f5fa depth: {'raw map' if a.depth_raw else 'Depth Anything V2'}"
          f"{' + background masked' if a.depth_mask_bg else ''} at {gw}x{gh} on {_short(srv)}")
    outs = wait(submit(g, srv), srv, resubmit=lambda fresh: submit(fresh_copy(g) if fresh else g, srv))
    im = next(im for node in outs.values() for im in node.get("images", []))
    out_dir = os.path.join(dest_dir, "depth")
    got = fetch_image(im, out_dir, srv)
    final = os.path.join(out_dir, f"{name}_{gw}x{gh}_depth.png")
    os.replace(got, final)
    a._depth_map = final
    print(f"   \U0001f5fa depth map  ->  {final}")
    return final


def prepare_inpaint_crop(a, r64):
    """Crop init + mask to the mask's neighbourhood; retarget sizes to it. Returns stitch info or None."""
    import tempfile
    from PIL import Image
    full = Image.open(os.path.expanduser(a.init)).convert("RGB")
    fw, fh = full.size
    m = Image.open(os.path.expanduser(a.mask)).convert("L").resize(full.size, Image.NEAREST)
    bbox = m.point(lambda v: 255 if v > 127 else 0).getbbox()
    if not bbox:
        return None
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # context = 2x the mask each way (at least a quarter of the image), squarish so the render isn't a sliver
    side = max(bw, bh) * 2
    cw = min(fw, max(int(side), fw // 4, 16))
    ch = min(fh, max(int(side), fh // 4, 16))
    if cw * ch > 0.6 * fw * fh:                 # the mask already covers most of it: no point cropping
        return None
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    x0 = int(min(max(cx - cw / 2, 0), fw - cw))
    y0 = int(min(max(cy - ch / 2, 0), fh - ch))
    td = tempfile.mkdtemp(prefix="pixelmon-inpaint-")
    ci, cm = os.path.join(td, "init-crop.png"), os.path.join(td, "mask-crop.png")
    full.crop((x0, y0, x0 + cw, y0 + ch)).save(ci)
    m.crop((x0, y0, x0 + cw, y0 + ch)).save(cm)
    if getattr(a, "control", None):              # a ControlNet guide (e.g. the old shapes) must cover the same area
        cc = os.path.join(td, "control-crop.png")
        Image.open(os.path.expanduser(a.control)).convert("RGB").resize(full.size, Image.LANCZOS) \
            .crop((x0, y0, x0 + cw, y0 + ch)).save(cc)
        a.control = cc
    info = {"init": a.init, "mask": a.mask, "box": (x0, y0, cw, ch), "src": (fw, fh), "out": (a.ow, a.oh)}
    sx, sy = a.ow / fw, a.oh / fh               # init pixels -> output pixels
    a.init, a.mask = ci, cm
    a.ow = a.sw = max(8, round(cw * sx))
    a.oh = a.sh = max(8, round(ch * sy))
    a.gen_w = a.res if a.sw >= a.sh else r64(a.res * a.sw / a.sh)
    a.gen_h = a.res if a.sh >= a.sw else r64(a.res * a.sh / a.sw)
    print(f"   ✂ inpaint: rendering the {cw}x{ch} area around the mask at full size, then stitching it back")
    return info


def stitch_inpaint(a, sprite):
    """Paste the cropped inpaint result back into the full original at the output size."""
    st = getattr(a, "_stitch", None)
    if not (st and sprite and os.path.isfile(sprite)):
        return
    from PIL import Image
    part = Image.open(sprite)
    mode = "RGBA" if part.mode in ("RGBA", "LA") or "transparency" in part.info else "RGB"
    ow, oh = st["out"]
    fw, fh = st["src"]
    x0, y0, cw, ch = st["box"]
    full = Image.open(os.path.expanduser(st["init"])).convert(mode)
    if full.size != (ow, oh):
        full = full.resize((ow, oh), Image.LANCZOS if a.art else Image.NEAREST)
    px, py = round(x0 * ow / fw), round(y0 * oh / fh)
    full.paste(part.convert(mode), (px, py))
    resave(full, sprite)


def _hex_rgb(colors):
    out = []
    for h in colors:
        h = str(h).strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if re.fullmatch(r"[0-9a-fA-F]{6}", h):
            out.append([int(h[i:i + 2], 16) for i in (0, 2, 4)])
    return out


def _palette_node():
    """The PixelArtPalette node module (for its dithering), or None."""
    try:
        sys.path.insert(0, os.path.join(COMFY, "custom_nodes"))
        from pixelart_palette import nodes
        return nodes
    except Exception:
        return None


# --- provenance: ComfyUI's SaveImage embeds the API graph (a "prompt" text chunk) in every PNG, but each PIL
# re-save below (enlarge, art palette, cut-out, inpaint stitch) dropped it. Chunks are carried over byte for byte,
# and a compact "pixelmon" chunk records how to make the picture again (version, prompt, seed, command line).
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_text_chunks(path):
    """The raw tEXt / zTXt / iTXt chunks of a PNG ([] if it has none or isn't a PNG)."""
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if data[:8] != PNG_SIG:
        return []
    out, i = [], 8
    while i + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            out.append(data[i:i + 12 + n])
        if kind == b"IEND":
            break
        i += 12 + n
    return out


def _chunk_key(chunk):
    return chunk[8:].split(b"\0", 1)[0]


def png_add_chunks(path, chunks, replace=False):
    """Splice text chunks in before IEND without touching the pixels. A keyword the file already has is kept
    (or replaced, with replace=True)."""
    if not chunks:
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return
    end = data.rfind(b"IEND") - 4
    if data[:8] != PNG_SIG or end < 8:
        return
    new = {_chunk_key(c) for c in chunks}
    if replace:
        for c in png_text_chunks(path):
            if _chunk_key(c) in new:
                data = data.replace(c, b"", 1)
        end = data.rfind(b"IEND") - 4
        add = chunks
    else:
        have = {_chunk_key(c) for c in png_text_chunks(path)}
        add = [c for c in chunks if _chunk_key(c) not in have]
    if not add and not replace:
        return
    tmp = path + ".pmtmp"
    with open(tmp, "wb") as f:
        f.write(data[:end] + b"".join(add) + data[end:])
    os.replace(tmp, path)


def png_itxt(key, text):
    """One iTXt chunk (UTF-8 text; tEXt is Latin-1 only)."""
    import struct
    import zlib
    body = b"iTXt" + key.encode("latin-1") + b"\0\0\0\0\0" + text.encode("utf-8")
    return struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def resave(im, path, **kw):
    """im.save(path) that keeps the text chunks the file had (a plain PIL save drops them)."""
    keep = png_text_chunks(path)
    im.save(path, **kw)
    png_add_chunks(path, keep)


def stamp_recipe(path, a, seed, subject, server):
    """Record how this picture was made in the PNG itself: a 'pixelmon' chunk next to ComfyUI's 'prompt'."""
    if not (path and os.path.isfile(path) and path.lower().endswith(".png")):
        return
    rec = {"tool": "pixelmon", "version": __version__, "prompt": subject, "seed": seed,
           "server": _short(server or SERVER), "argv": sys.argv[1:]}
    if getattr(a, "_row_key", None):
        rec["key"] = a._row_key
    try:
        png_add_chunks(path, [png_itxt("pixelmon", json.dumps(rec, ensure_ascii=False))], replace=True)
    except OSError:
        pass


def enlarge_output(a, sprite):
    """Pixel art past 1024 was made at 1/k size: enlarge it by exactly k, nearest neighbour (crisp square pixels)."""
    k = getattr(a, "enlarge", 1)
    if k <= 1 or getattr(a, "raw", False) or not (sprite and os.path.isfile(sprite)):
        return
    from PIL import Image
    im = Image.open(sprite)
    resave(im.resize((im.width * k, im.height * k), Image.NEAREST), sprite)


def _bbox(alpha, thr):
    """(x0, y0, x1, y1) of the pixels at or above thr in a 2-D alpha array, or None."""
    import numpy as np
    ys, xs = np.nonzero(alpha >= thr)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None


def _edge_key(rgb, tol):
    """True where a pixel is further than tol from the background colour (the median of the border)."""
    import numpy as np
    e = max(1, min(8, min(rgb.shape[:2]) // 16))
    edge = np.concatenate([rgb[:e].reshape(-1, 3), rgb[-e:].reshape(-1, 3), rgb[:, :e].reshape(-1, 3), rgb[:, -e:].reshape(-1, 3)])
    return np.sqrt(((rgb - np.median(edge, 0)) ** 2).sum(-1)) > tol


def _bleed(im, rounds=12):
    """Colour the see-through pixels from their solid neighbours, so a game that smooths the edge gets no dark halo."""
    import numpy as np
    from PIL import Image
    a = np.array(im)
    acc = a[..., :3].astype(np.float32) * (a[..., 3] > 0)[..., None]
    filled = a[..., 3] > 0
    for _ in range(rounds):
        if filled.all():
            break
        p, m = np.pad(acc, ((1, 1), (1, 1), (0, 0))), np.pad(filled, 1).astype(np.float32)
        h, w = filled.shape
        tot = sum(p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        cnt = sum(m[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        new = ~filled & (cnt > 0)
        acc[new] = tot[new] / cnt[new][:, None]
        filled |= new
    a[..., :3] = np.clip(acc, 0, 255).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def _cutout_mask(a, src, sprite, server):
    """BiRefNet's mask of a finished picture (a separate pass, for a render that didn't make its own)."""
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        big = src
        if max(src.size) < 1024:                # BiRefNet sees a small sprite at a size it was trained on
            k = max(1, 1024 // max(src.size))
            big = src.resize((src.width * k, src.height * k), Image.NEAREST)
        up = os.path.join(tmp, "cut_" + os.path.basename(sprite))
        big.save(up)
        name = _upload_image(up, server, "--cutout")
        g = {"1": {"class_type": "LoadImage", "inputs": {"image": name}},
             "2": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": a.cutout_model}},
             "3": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["2", 0], "image": ["1", 0]}},
             "4": {"class_type": "MaskToImage", "inputs": {"mask": ["3", 0]}},
             "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": "pixelmon/cutout/mask"}}}
        outs = wait(submit(g, server), server, resubmit=lambda fresh: submit(fresh_copy(g) if fresh else g, server))
        im = next(im for node in outs.values() for im in node.get("images", []))
        mask = Image.open(fetch_image(im, tmp, server)).convert("L")
        mask.load()
    return mask


def cutout_output(a, sprite, server=None, files=()):
    """--cutout: cut the subject out with BiRefNet (the background goes transparent), widened by a colour key
    with --cutout-key (parts as grey as the backdrop), then with --fit-to fitted into an old picture's
    footprint at --fit-scale: the same width-or-height, centred the same way, standing on the same line, so
    a remade sprite is the same size in the game as the one it replaces. The mask comes from the render's
    full-size picture (build_graph saves it as *_cutmask_*), shrunk onto the file the way the picture was."""
    if not (getattr(a, "cutout", False) and sprite and os.path.isfile(sprite)):
        return
    import numpy as np
    from PIL import Image
    server = server or SERVER
    smooth = a.art or a.raw                     # pixel art keeps hard 0/255 edges and whole pixels
    src = Image.open(sprite).convert("RGB")
    made = next((f for f in files if "_cutmask_" in os.path.basename(f) and os.path.isfile(f)), None)
    if made:                                    # the render's own mask, from the full-size picture
        mask = Image.open(made).convert("L")
        mask.load()
        os.remove(made)
    else:                                       # no mask in the render: cut the finished file out instead
        mask = _cutout_mask(a, src, sprite, server)
    if not mask.getbbox():
        print("      ⚠ cut-out: BiRefNet found no subject — left the picture as it was")
        return
    if smooth:
        m = np.array(mask.resize(src.size, Image.LANCZOS))
    else:                                       # a sprite pixel is solid when most of its block is subject
        m = np.where(np.array(mask.resize(src.size, Image.BOX)) >= 128, 255, 0).astype(np.uint8)
    if a.cutout_key:
        key = _edge_key(np.asarray(src, np.float32), a.cutout_key)
        try:
            from scipy import ndimage
            if smooth:                          # drop specks of noise; a pixel sprite has none to drop
                key = ndimage.binary_opening(key, iterations=2)
            key = ndimage.binary_fill_holes(key | (m > 128))
        except ImportError:
            pass
        m = np.maximum(m, (key * 255).astype(np.uint8))
    m[m < 24] = 0                               # the faint haze BiRefNet leaves round the edge
    cut = src.convert("RGBA")
    cut.putalpha(Image.fromarray(m, "L"))
    note = "cut out"
    if a.fit_to:
        old = Image.open(a.fit_to).convert("RGBA")
        oa = np.array(old)
        if oa[..., 3].min() == 255:             # an opaque old picture: its footprint is what isn't background
            ob = _bbox(_edge_key(oa[..., :3].astype(np.float32), a.cutout_key or 34).astype(np.uint8), 1)
        else:
            ob = _bbox(oa[..., 3], 128)
        nb = _bbox(m, 96)
        scale = a.fit_scale or (4 if smooth else 1)
        canvas = Image.new("RGBA", (old.width * scale, old.height * scale), (0, 0, 0, 0))
        if ob and nb:
            x0, y0, x1, y1 = [v * scale for v in ob]
            bw, bh = x1 - x0, y1 - y0
            piece = cut.crop(nb)
            k = min(bw / piece.width, bh / piece.height)
            pw, ph = max(1, round(piece.width * k)), max(1, round(piece.height * k))
            piece = piece.resize((pw, ph), Image.LANCZOS if smooth else Image.NEAREST)
            canvas.alpha_composite(piece, (max(0, round(x0 + (bw - pw) / 2)), max(0, y1 - ph)))   # standing where it stood
        cut = canvas
        note = f"cut out, fitted to {os.path.basename(a.fit_to)}'s footprint at {scale}x ({cut.width}x{cut.height})"
    resave(_bleed(cut) if smooth else cut, sprite)
    print(f"      ✂ {note}")


def art_palette(a, sprite, pal):
    """--art + --palette: move the finished picture's colors toward the palette WITHOUT pixelating it.
    Each pixel is blended toward its nearest palette color by --palette-strength (1 = exactly that color)."""
    s = getattr(a, "palette_strength", 0)
    if not (a.art and pal and pal != "none" and s > 0 and sprite and os.path.isfile(sprite)):
        return
    colors = a.custom_hex.split() if pal == "Custom" else ((getattr(_pal, "ALL_PALETTES", {}) or {}).get(pal) if _pal else None)
    rgb = _hex_rgb(colors or [])
    if not rgb:
        return
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        print("   note: --palette-strength needs numpy + Pillow; left the colors as rendered")
        return
    im = Image.open(sprite)
    alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") else None
    px = np.asarray(im.convert("RGB")).astype(np.float32)
    P = np.array(rgb, dtype=np.float32)
    exact = None
    if a.dither != "none":                                   # the palette node's own dithering, so it matches pixel mode
        node = _palette_node()
        if node:
            exact = np.asarray(node._quantize_dither(im.convert("RGB"), rgb, a.dither, a.dither_amount)).astype(np.float32)
        else:
            print("   note: couldn't load the palette node's dithering; used plain nearest colors")
    if exact is None:
        exact = np.empty_like(px)
        for y in range(0, px.shape[0], 64):                # rows in chunks: pixels x colors stays small
            c = px[y:y + 64]
            d = c[:, :, None, :] - P[None, None, :, :]
            rmean = (c[:, :, None, 0] + P[None, None, :, 0]) / 2  # "redmean": closer to how eyes judge color distance
            dist = (2 + rmean / 256) * d[..., 0] ** 2 + 4 * d[..., 1] ** 2 + (2 + (255 - rmean) / 256) * d[..., 2] ** 2
            exact[y:y + 64] = P[dist.argmin(-1)]
    out = exact if s >= 1 else px * (1 - s) + exact * s
    res = Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8), "RGB")
    if alpha is not None:
        res.putalpha(alpha)
    resave(res, sprite)


def keep_outside_mask(a, sprite):
    """--mask: paste ONLY the masked pixels of the new render onto the original (--init), at the output's
    native size, so everything outside the mask stays exactly as it was."""
    if not (getattr(a, "mask", None) and a.init and a.keep_outside and sprite and os.path.isfile(sprite)):
        return
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return
    new = Image.open(sprite)
    mode = "RGBA" if new.mode in ("RGBA", "LA") or "transparency" in new.info else "RGB"
    new = new.convert(mode)
    orig = Image.open(os.path.expanduser(a.init)).convert(mode).resize(new.size, Image.NEAREST)
    m = Image.open(os.path.expanduser(a.mask)).convert("L").resize(new.size, Image.NEAREST)
    m = m.point(lambda v: 255 if v > 127 else 0).filter(ImageFilter.MaxFilter(3))   # +1 px so no seam shows
    orig.paste(new, (0, 0), m)
    resave(orig, sprite)


def steer_files(a, server):
    """Reference filenames available on `server` (uploads once per server, then caches)."""
    paths = getattr(a, "_steer_paths", None)
    if paths is None:
        paths = a._steer_paths = _gather_steer_paths(a.steer, a.steer_max)
    if server not in _STEER_UPLOADS:
        _STEER_UPLOADS[server] = [_upload_image(p, server) for p in paths]
        if server != DRY:
            print(f"   \U0001f9ed steer: {len(_STEER_UPLOADS[server])} reference(s) -> {_short(server)}")
    return _STEER_UPLOADS[server]


def _render_error(entry):
    """The ComfyUI error message of a finished-with-error history entry, or None."""
    st = (entry or {}).get("status") or {}
    if st.get("status_str") != "error":
        return None
    for kind, m in st.get("messages", []):
        if kind == "execution_error":
            return f"{m.get('node_type')} (node {m.get('node_id')}): {m.get('exception_type')}: {m.get('exception_message', '').strip()}"
    if any(kind == "execution_interrupted" for kind, _ in st.get("messages", [])):
        return "interrupted on the server (someone pressed Cancel / Interrupt there)"
    return "the render failed on the server"


def _transient(err):
    """A server error worth one automatic retry: ComfyUI's model manager occasionally trips over its own
    loaded-model list (IndexError) or runs short of VRAM while swapping models between different jobs."""
    return bool(err) and (("Loader" in err.split(" (node")[0] and "IndexError" in err) or "out of memory" in err.lower())


# errors after which the server fails every prompt until its ComfyUI is restarted (pixelmon-gui does that
# by itself when servers.json has a "_restart" command for it)
POISONED = ("object has no attribute 'model_size'", "object has no attribute 'is_dynamic'")


RESTART_EVERY = 180   # seconds: at most one "_restart" of a server, shared with other pixelmon runs (stamp file)


def restart_server(server):
    """Run servers.json's "_restart" command for this server (by alias), as pixelmon-gui does, so a headless run
    doesn't just wait for someone else to restart a broken ComfyUI. At most once per RESTART_EVERY per server
    across runs. True if it ran."""
    alias = next((k for k, v in SERVERS.items() if resolve_server(k) == server), None)
    cmd = RESTARTS.get(alias) if alias else None
    if not cmd:
        return False
    stamp = os.path.join(os.path.expanduser("~/.cache/pixelmon"), "restart-" + re.sub(r"\W", "_", _short(server)))
    try:
        if time.time() - os.path.getmtime(stamp) < RESTART_EVERY:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(stamp), exist_ok=True)
    open(stamp, "w").close()
    print(f"   ↻ restarting {alias}'s ComfyUI (servers.json _restart): {cmd}")
    try:
        subprocess.run(cmd, shell=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print("   … the restart command is still running after 120s; waiting for the server anyway")
    return True


def _wait_restart(pid, server, limit=120):
    """After a POISONED error: wait (up to `limit` s) for the server to be restarted, which shows as the
    prompt dropping out of its history. True once it has been. Restarts it itself (servers.json "_restart")
    when nobody else has after a few seconds."""
    t, tried = time.time(), False
    while time.time() - t < limit:
        time.sleep(3)
        try:
            with urllib.request.urlopen(f"{server}/history/{pid}", timeout=10) as r:
                if pid not in json.loads(r.read()):
                    return True
        except OSError:                            # down: restarting
            tried = True
            continue
        if not tried and time.time() - t >= 6:     # the GUI didn't restart it: do it ourselves if we know how
            tried = True
            restart_server(server)
    return False


class CachedNoOutput(RuntimeError):
    """ComfyUI answered an identical, fully cached prompt with 'success' but no image list."""


def _cached_empty(entry):
    st = (entry or {}).get("status") or {}
    return st.get("status_str") == "success" and st.get("completed") and not (entry or {}).get("outputs")


def fresh_copy(graph):
    """The same graph, saved under a unique subfolder: ComfyUI re-runs only the save step (everything
    upstream stays cached, so it's instant) and reports the images again. File names don't change."""
    import copy
    import uuid
    g = copy.deepcopy(graph)
    tag = uuid.uuid4().hex[:8]
    for node in g.values():
        if node.get("class_type") == "SaveImage":
            pre = node["inputs"]["filename_prefix"]
            head, _, tail = pre.rpartition("/")
            node["inputs"]["filename_prefix"] = f"{head + '/' if head else ''}rerun-{tag}/{tail}"
    return g


def _queued(pid, server):
    """True while the prompt is still waiting in the server's queue (not started yet)."""
    try:
        with urllib.request.urlopen(f"{server}/queue", timeout=10) as r:
            q = json.loads(r.read())
        return any(item[1] == pid for item in q.get("queue_pending", []))
    except Exception:
        return False


def _known(pid, server):
    """True while the server still has the prompt waiting or running. True when unsure (no answer),
    so a slow server never looks like one that forgot the prompt."""
    try:
        with urllib.request.urlopen(f"{server}/queue", timeout=10) as r:
            q = json.loads(r.read())
        return any(item[1] == pid for item in q.get("queue_running", []) + q.get("queue_pending", []))
    except Exception:
        return True


SERVER_GRACE = 240    # seconds wait() rides out a render server that stops answering (a restart takes ~10-30s)


class RenderFailed(SystemExit):
    """This one job failed on the server (a node raised, it returned nothing). A SystemExit, so a lone render
    still ends with the message as before, but a batch can catch it, report the job and carry on."""


def wait(pid, server=None, timeout=600, resubmit=None):
    """Wait for a prompt's outputs. timeout = seconds of actual rendering; time spent queued behind
    other jobs doesn't count. A server-side error stops at once with the node's message — except a
    transient model-loading hiccup, which is resubmitted once when resubmit(fresh) is given. An identical
    re-run that ComfyUI answers from its cache with no images is resubmitted once with fresh=True.
    A server that restarts mid-wait (pixelmon-gui restarts a stuck one) is waited out, and a prompt it
    forgot (not done, waiting or running) is sent again, up to twice. A failed job raises RenderFailed;
    a server that is gone or broken for good ends the run (sys.exit)."""
    server = server or SERVER
    waited = 0
    refreshed = False
    again, relost, lost, n, down = resubmit, 2, 0, 0, None

    def swap(new):                                # a resubmit replaces the prompt we are waiting on
        _forget(server, pid)
        return new
    while waited < timeout:
        try:
            with urllib.request.urlopen(f"{server}/history/{pid}", timeout=30) as r:
                hist = json.loads(r.read())
        except OSError as e:                      # connection refused / reset / timed out: restarting?
            if down is None:
                down = time.time()
                print(f"   … render server stopped answering ({getattr(e, 'reason', e)}) — waiting for it")
            elif time.time() - down > SERVER_GRACE:
                sys.exit(f"The render server has been unreachable for {SERVER_GRACE}s ({getattr(e, 'reason', e)}).")
            time.sleep(3)
            continue
        if down is not None:
            print(f"   … render server is back after {time.time() - down:.0f}s")
            down, lost = None, 0
        n += 1
        if pid not in hist and n % 5 == 0:        # every ~5s: does the server still know this prompt?
            lost = 0 if _known(pid, server) else lost + 1
            if lost >= 3:                         # forgotten three checks running: the server restarted
                if not (again and relost):
                    sys.exit("The render server restarted and lost this render.")
                print("   ↻ the render server restarted and lost this render — sending it again")
                pid, relost, lost, waited = swap(again(False)), relost - 1, 0, 0
                continue
        if pid in hist:
            err = _render_error(hist[pid])
            if err and again and relost and any(p in err for p in POISONED):
                print("   ↻ the render server broke (it needs a restart) — waiting for it to come back, then sending this again")
                if not _wait_restart(pid, server):
                    _forget(server, pid)
                    sys.exit(f"Render failed on the server — {err}\n   Its ComfyUI needs a restart.")
                pid, relost, lost, waited = swap(again(False)), relost - 1, 0, 0
                continue
            if err and resubmit and _transient(err):
                print(f"   ↻ server hiccup ({err.split(': ', 1)[-1][:60]}) — retrying once")
                pid, resubmit, waited = swap(resubmit(False)), None, 0
                continue
            if _cached_empty(hist[pid]):
                if resubmit and not refreshed:
                    print("   ↻ identical re-run answered from the server's cache without images — re-saving")
                    pid, refreshed, waited = swap(resubmit(True)), True, 0
                    continue
                _forget(server, pid)
                raise RenderFailed("The server finished but returned no images (identical cached prompt).")
            if err:
                _forget(server, pid)
                raise RenderFailed(f"Render failed on the server — {err}")
            if hist[pid].get("outputs"):
                _forget(server, pid)
                return hist[pid]["outputs"]
        time.sleep(1)
        if waited % 10 or not _queued(pid, server):
            waited += 1
    sys.exit("Timed out waiting for the image.")


def poll(pid, server):
    """One non-blocking /history check; returns the outputs dict, or None if not ready."""
    with urllib.request.urlopen(f"{server}/history/{pid}", timeout=30) as r:
        hist = json.loads(r.read())
    if pid in hist:
        err = _render_error(hist[pid])
        if err:
            _forget(server, pid)
            raise RuntimeError(err)
        if _cached_empty(hist[pid]):
            _forget(server, pid)
            raise CachedNoOutput("identical cached prompt returned no images")
        if hist[pid].get("outputs"):
            _forget(server, pid)
            return hist[pid]["outputs"]
    return None


def server_up(server):
    try:
        urllib.request.urlopen(f"{server}/system_stats", timeout=5).read()
        return True
    except Exception:
        return False


def _short(url):
    return url.split("//", 1)[-1]


def fetch_image(im, dest_dir, server=None):
    """Download one server-side output image via /view into dest_dir; return local path.
    Used for remote servers and render-farm members (files live on the server's disk)."""
    import urllib.parse
    server = server or SERVER
    q = urllib.parse.urlencode({"filename": im["filename"],
                                "subfolder": im.get("subfolder", ""),
                                "type": im.get("type", "output")})
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, im["filename"])
    with urllib.request.urlopen(f"{server}/view?{q}", timeout=180) as r, open(out, "wb") as f:
        shutil.copyfileobj(r, f)
    return out


def on_this_disk(server):
    """True when the server writes into this machine's ~/ComfyUI/output (the ComfyUI launch-comfyui.sh starts).
    Any other ComfyUI — remote, or a second one on another local port — is read over /view."""
    import urllib.parse
    return is_local(server) and (urllib.parse.urlparse(server).port or 8188) == 8188


def collect(outs, dest, server):
    """A finished job's files, here: moved out of ComfyUI's output into dest (or left there) for the local
    server, downloaded over /view from any other."""
    imgs = [im for node in outs.values() for im in node.get("images", [])]
    here = dest or os.path.join(OUTPUT, "pixelmon")
    files = []
    for im in imgs:
        f = os.path.join(OUTPUT, im.get("subfolder", ""), im["filename"])
        if not (on_this_disk(server) and os.path.exists(f)):
            files.append(fetch_image(im, here, server))
        elif dest:
            tgt = os.path.join(dest, os.path.basename(f))
            shutil.move(f, tgt)
            files.append(tgt)
        else:
            files.append(f)
    return files


def pick_sprite(files):
    return next((f for f in files if ("_sprite_" in f or "_art_" in f or "_raw_" in f) and "_cutmask_" not in f), None)


class Job:
    """One picture to make: the subject, its seed and palette, where it goes, and the settings it's made with
    (a --batch-file row may have its own style and size)."""
    def __init__(self, idx, subj, seed, pal, dest, a, label=""):
        self.idx, self.subj, self.seed, self.pal, self.dest, self.a, self.label = idx, subj, seed, pal, dest, a, label
        self.bad = set()        # servers it already failed on (a requeued job goes to another box)

    def graph(self, server):
        return build_graph(self.a, self.seed, self.pal, subject=self.subj, server=server)


def finish(job, files, server):
    """The post-processing every render gets after the server is done; returns the sprite's path."""
    ja, sprite = job.a, pick_sprite(files)
    keep_outside_mask(ja, sprite)
    stitch_inpaint(ja, sprite)
    art_palette(ja, sprite, job.pal)
    enlarge_output(ja, sprite)
    cutout_output(ja, sprite, server, files)
    stamp_recipe(sprite, ja, job.seed, job.subj, server)
    return sprite


FARM_FAILS = 2      # a box that fails this many jobs in a row is dropped (circuit breaker)
POLL_GRACE = 20     # seconds a farm box may not answer before its job goes to another box


def check_boxes(boxes, jobs):
    """The boxes that have every node, input and model these jobs need; the others are reported with what to run
    on them. One graph per distinct job settings is checked (no uploads: build_graph against DRY)."""
    if CHECKS_OFF:
        return list(boxes)
    probes = {}
    for j in jobs:
        probes.setdefault(id(j.a), j)
    graphs = [j.graph(DRY) for j in probes.values()]
    ready = []
    for s in boxes:
        probs = list(dict.fromkeys(x for g in graphs for x in graph_problems(g, s)))
        if probs:
            print(f"   ⚠ skipping {_short(s)}: it can't render this —\n" + "\n".join(f"      • {x}" for x in probs)
                  + "\n" + "\n".join("   " + h for h in provision_hint(probs, s).splitlines()))
        else:
            ready.append(s)
    return ready


def run_farm(a, jobs):
    """Render farm: distribute jobs across POOL with dynamic dispatch (feed the free GPU).
    Faster boxes naturally pull more jobs; results are fetched back from whichever GPU made them.
    A box that fails FARM_FAILS jobs in a row is dropped (a broken box fails fast, is free first, and would
    otherwise eat the queue); a failed job is tried once more on another box; requeued work reaches boxes that
    already went idle. Returns (pictures made, jobs that produced nothing)."""
    live = [s for s in POOL if server_up(s)]
    down = [s for s in POOL if s not in live]
    if down:
        print(f"   ⚠ skipping unreachable: {', '.join(_short(s) for s in down)}")
    if not live:
        sys.exit("render farm: no reachable servers in the pool.")
    live = check_boxes(live, jobs)
    if not live:
        sys.exit("render farm: no server in the pool can render this (see above).")
    print(f"   \U0001f69c render farm: {len(live)} GPU(s) — {', '.join(_short(s) for s in live)}")
    pending = list(jobs)
    inflight = {}               # server -> (job, pid)
    total = len(jobs)
    made, failed = [], 0
    fails = {}                  # server -> failed jobs in a row
    tries = {}                  # job idx -> failed attempts
    silent = {}                 # server -> when its polls started failing
    retried = set()             # jobs already resent once (transient error / cached-empty re-save)

    def name(job):
        return f"{job.label or job.subj}  " if (a.batch or a.batch_file) else ""

    def launch(srv):
        """Submit the next pending job to srv. False = server unusable (drop it)."""
        if not pending:
            return True
        cands = [j for j in pending if srv not in j.bad]
        if not cands:
            if any(s != srv for s in live):      # leave it for a box it hasn't failed on
                return True
            cands = pending
        job = cands[0]
        pending.remove(job)
        try:
            pid = submit(job.graph(srv), srv)
        except SystemExit as e:
            if STOPPING:
                raise
            pending.insert(0, job)               # couldn't submit; keep the job
            print(f"   ⚠ dropping {_short(srv)}: {e}")
            return False
        inflight[srv] = (job, pid)
        return True

    def give_up_or_requeue(srv, job, why, breaker=True):
        nonlocal failed
        tries[job.idx] = tries.get(job.idx, 0) + 1
        job.bad.add(srv)
        if tries[job.idx] >= 2:
            print(f"   ✗ {_short(srv)}: {name(job)}seed={job.seed} failed twice — giving up on it ({why})")
            failed += 1
        else:
            print(f"   ⚠ {_short(srv)}: {name(job)}seed={job.seed} failed ({why}) — trying it once more elsewhere")
            pending.append(job)
        if breaker:
            fails[srv] = fails.get(srv, 0) + 1
            if fails[srv] >= FARM_FAILS and srv in live:
                print(f"   ⚠ dropping {_short(srv)} after {fails[srv]} failures in a row")
                live.remove(srv)

    while True:
        # feed every idle box — including ones that went idle before a job was requeued, or it is lost
        for srv in list(live):
            if pending and srv not in inflight and launch(srv) is False:
                live.remove(srv)
        if not inflight:
            break
        advanced = False
        for srv, (job, pid) in list(inflight.items()):
            try:
                outs = poll(pid, srv)
                silent.pop(srv, None)
            except CachedNoOutput:
                del inflight[srv]
                advanced = True
                if job.idx not in retried:      # re-save the cached result under a fresh folder
                    retried.add(job.idx)
                    try:
                        inflight[srv] = (job, submit(fresh_copy(job.graph(srv)), srv))
                    except SystemExit as e:
                        if STOPPING:
                            raise
                        give_up_or_requeue(srv, job, str(e))
                    continue
                give_up_or_requeue(srv, job, "the server returned no images", breaker=False)
                continue
            except RuntimeError as e:          # the render itself failed
                del inflight[srv]
                advanced = True
                if _transient(str(e)) and job.idx not in retried:
                    print(f"   ↻ {_short(srv)}: server hiccup — requeueing once")
                    retried.add(job.idx)
                    pending.append(job)
                    continue
                give_up_or_requeue(srv, job, f"render failed — {e}")
                continue
            except Exception as e:             # no answer: a blip, or the box went away
                t0 = silent.setdefault(srv, time.time())
                if time.time() - t0 < POLL_GRACE:
                    continue
                print(f"   ⚠ {_short(srv)} hasn't answered for {POLL_GRACE}s ({getattr(e, 'reason', e)}) — "
                      f"dropping it, requeueing its job")
                del inflight[srv]
                silent.pop(srv, None)
                _forget(srv, pid)              # nothing to clear there at the end: we can't reach it
                pending.append(job)
                if srv in live:
                    live.remove(srv)
                advanced = True
                continue
            if outs is None:
                continue
            advanced = True
            del inflight[srv]
            try:
                sprite = finish(job, collect(outs, job.dest, srv), srv)
            except Exception as e:             # a fetch / post-processing error is this job's, not the box's
                give_up_or_requeue(srv, job, f"couldn't fetch or finish the picture: {e}", breaker=False)
                continue
            fails[srv] = 0
            made.append(sprite)
            print(f"   ✅ [{len(made)}/{total}] {_short(srv):<20} {name(job)}seed={job.seed}  ->  {sprite}")
        if not advanced:
            time.sleep(1)

    if pending:
        print(f"   ⚠ {len(pending)} job(s) left undone (no GPU left that can take them).")
    if failed:
        print(f"   ✗ {failed} of {total} render(s) failed on the server (see above).")
    return made, failed + len(pending)


def main():
    # Flush each line even when piped (GUI, tee, logs), so progress and every
    # finished image show up as they happen instead of all at exit.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    # add_help=False so we can render our own friendly, colorized help instead
    # of argparse's plain default (shown via print_help on -h or no args).
    p = argparse.ArgumentParser(prog="pixelmon", add_help=False)
    p.add_argument("-h", "--help", action="store_true", dest="show_help")
    p.add_argument("--version", action="version", version=f"pixelmon {__version__}")
    p.add_argument("prompt", nargs="?", help='what to draw, e.g. "a goblin warrior"')
    p.add_argument("--size", default=None,
                   help="output size: N (square) or WxH, e.g. 32x48. "
                        "default 128 (pixel sprite) or 1024 (--art)")
    p.add_argument("--out", default=None, metavar="N|WxH",
                   help="exact final canvas size, e.g. 320x200 (default: --size). --size still sets "
                        "the sampling aspect/detail; when only --out is given, --size follows it. "
                        "Also forces the size with --snap-pixels")
    p.add_argument("-n", "--number", type=int, default=1,
                   help="how many to generate, each with a different seed. default 1 "
                        "(with --batch: how many of EACH subject)")
    p.add_argument("--batch", default=None, metavar="SUBJECTS",
                   help='comma-separated subjects to round-robin, one of each per pass '
                        '(e.g. --batch "bat,skeleton,spider"); each goes to its own folder')
    p.add_argument("--tile", nargs="?", const="both", default=None, choices=["both", "x", "y"],
                   help="seamless, tileable texture: the render wraps at its edges so it repeats with no "
                        "seam (both axes, or just x / y). also saves a 3x3 _tiled_ preview")
    p.add_argument("--transparent", action="store_true",
                   help="cut out the background -> transparent PNG (great for game sprites)")
    p.add_argument("--bg-tolerance", dest="bg_tolerance", type=int, default=16,
                   help="how aggressively to match the background color for --transparent. default 16")
    p.add_argument("--palette", default="none",
                   help="palette to lock to: 'none' = keep the model's own colors; 'random' = "
                        "a random bundled palette per image; or a name (see --list-palettes)")
    p.add_argument("--style", default="",
                   help="style guide(s) to append to the prompt, comma-separated "
                        "(e.g. geometric,detailed). see --list-styles")
    p.add_argument("--dither", nargs="?", const="floyd-steinberg", default="none", metavar="NAME",
                   choices=DITHER_METHODS,
                   help="dither between palette colors (needs --palette): " + ", ".join(DITHER_METHODS[1:]) +
                        ". bare --dither = floyd-steinberg")
    p.add_argument("--dither-amount", dest="dither_amount", type=float, default=0.75, metavar="F",
                   help="dither strength 0..1: ordered = threshold spread, diffusion = share of error pushed on. "
                        "default 0.75")
    p.add_argument("--snap-pixels", dest="snap_pixels", action="store_true",
                   help="find the pixel grid the model actually drew and take one color per block, instead of "
                        "shrinking to --out (extra crisp). see --snap-method")
    p.add_argument("--color-match", dest="color_match", choices=["oklab", "redmean"], default="oklab",
                   help="how the palette lock picks the nearest color: oklab = perceptual (keeps hue and lightness "
                        "as the eye sees them; default), redmean = the older RGB approximation")
    p.add_argument("--cutout", action="store_true",
                   help="cut the subject out with BiRefNet (a background-removal model on the render server): "
                        "the background goes transparent, even a busy one. see --cutout-key, --fit-to")
    p.add_argument("--cutout-key", dest="cutout_key", type=float, nargs="?", const=34.0, default=0.0, metavar="TOL",
                   help="with --cutout: also keep anything further than TOL (default 34) from the background's "
                        "colour, for parts as grey as the backdrop that BiRefNet drops")
    p.add_argument("--cutout-model", dest="cutout_model", default="birefnet.safetensors", metavar="FILE",
                   help="background-removal model for --cutout (models/background_removal/). default birefnet.safetensors")
    p.add_argument("--fit-to", dest="fit_to", default=None, metavar="OLD.png",
                   help="with --cutout: fit the cut-out into this old picture's footprint at --fit-scale: the same "
                        "width-or-height, centred the same way, standing on the same line (a remade sprite stays "
                        "the same size in the game). implies --cutout")
    p.add_argument("--fit-scale", dest="fit_scale", type=int, default=0, metavar="N",
                   help="--fit-to canvas = the old picture's size x N. default 4 with --art/--raw, 1 for pixel art")
    p.add_argument("--mesh", default=None, metavar="IMG",
                   help="make a 3D model (.glb) of the picture's subject with Hunyuan3D 2 on the render server "
                        "(+ a Blender preview). the prompt is optional (it names the files)")
    p.add_argument("--turnaround", default=None, metavar="IMG|MODEL",
                   help="rotation sprites: the picture -> a 3D model (or use a .glb/.obj/... you have) -> Blender "
                        "renders --angles views -> each painted with depth lock + steering from the picture, cut "
                        "out, one seed. the prompt says what the subject is; every other flag applies to the sprites")
    p.add_argument("--angles", type=int, default=8, help="--turnaround: how many views around the model (default 8)")
    p.add_argument("--elev", type=float, default=0.0, metavar="DEG",
                   help="--turnaround: camera height in degrees above the model's middle (default 0, eye level)")
    p.add_argument("--shape", default=None, metavar="SPEC",
                   help="--mesh/--turnaround: build the model straight from the picture's outline instead of "
                        "Hunyuan3D (a clean low-poly solid wearing the picture, crisp; no render server): "
                        "extrude[:DEPTH[,SHARE:DEPTH...]] pushes it out DEPTH x its width (boxes 0.5-0.8, guns "
                        "0.08, cards 0.04; SHARE:DEPTH makes the top SHARE of it that deep: a monitor on a desk), "
                        "lathe[:SEGMENTS] spins it round its middle (barrels, tanks, jars). writes NAME_textured.glb")
    p.add_argument("--shape-grid", dest="shape_grid", type=int, default=64, metavar="N",
                   help="--shape: how many cells across the outline is read at, 8..256 (default 64; fewer = "
                        "blockier and fewer triangles). cells always sit on whole pixels")
    p.add_argument("--lowpoly", type=int, default=0, metavar="FACES",
                   help="--mesh/--turnaround: also make a faceted low-poly copy with about FACES faces (e.g. 400); "
                        "the turnaround renders and paints that one")
    p.add_argument("--texture", action="store_true",
                   help="--mesh/--turnaround: also make a clean, textured model: quads, a UV map and the picture "
                        "baked into a texture (NAME_textured.glb, + .gltf/.bin/_texture.png to paint). about 6000 "
                        "quads, or --lowpoly FACES (flat shaded). the front is the picture, the back its colours "
                        "seen through, the sides stretched")
    p.add_argument("--texture-size", dest="texture_size", type=int, default=0, metavar="PX",
                   help="--texture: the texture's size, 256..8192 (default 1024 up to 1500 faces, else 2048)")
    p.add_argument("--texture-elev", dest="texture_elev", type=float, default=None, metavar="DEG",
                   help="--texture: how far above the subject the picture was seen from, 0..80 degrees, for projecting "
                        "it. default: guessed from the outline (0, 30 or 55)")
    p.add_argument("--blender", default=None, metavar="CMD",
                   help="how to run Blender for --mesh/--turnaround. default $PIXELMON_BLENDER, blender on the "
                        "PATH, or the org.blender.Blender flatpak")
    p.add_argument("--mesh-model", dest="mesh_model", default="hunyuan3d-dit-v2_fp16.safetensors", metavar="FILE",
                   help="Hunyuan3D 2 checkpoint (models/checkpoints/). default hunyuan3d-dit-v2_fp16.safetensors")
    p.add_argument("--mesh-steps", dest="mesh_steps", type=int, default=20, help=argparse.SUPPRESS)
    p.add_argument("--mesh-cfg", dest="mesh_cfg", type=float, default=5.5, help=argparse.SUPPRESS)
    p.add_argument("--mesh-octree", dest="mesh_octree", type=int, default=256, help=argparse.SUPPRESS)
    p.add_argument("--fit-subject", dest="fit_subject", action="store_true",
                   help="crop the full-res render to its subject (background found from the edges) so it FILLS the "
                        "canvas before reducing — a 16x16 sprite gets twice the pixels on the subject")
    p.add_argument("--snap-method", dest="snap_method", choices=["unfake", "snapper"], default="unfake",
                   help="--snap-pixels engine: unfake = find the grid the model really drew, then reduce it cleanly "
                        "to --out (sharpest); snapper = spritefusion pixel-snapper, then fitted to --out. default unfake")
    p.add_argument("--pixel-angles", dest="pixel_angles", type=float, default=0.0, metavar="F",
                   help="EXPERIMENTAL: redraw region outlines with clean pixel-art angles (integer ratios "
                        "1:1, 2:1, 3:1 … like DRAW's angle snap). F = how much wobble gets straightened, "
                        "0 = off, ~1.5 recommended, >2.5 starts bending shapes. needs opencv on the server")
    p.add_argument("--thin-lines", dest="thin_lines", type=int, default=0, metavar="N",
                   help="1-px outlines: thin strokes of the darkest color (up to N px thick) to single-pixel "
                        "lines; thicker dark areas (shadows, doorways) are kept. 0 = off, ~4 recommended")
    p.add_argument("--pixel-size", dest="pixel_size", default=None, metavar="WxH",
                   help="size of one art pixel in output pixels: 1x1, 2x1 (wide, like EGA/CGA low-res), 2x2, 4x1 … "
                        "the art grid = --out / pixel size, and with --snap-pixels the snapper uses that grid "
                        "instead of guessing (which often gives huge pixels). default: auto")
    p.add_argument("--angle-grid", dest="angle_grid", default="pixel",
                   choices=["pixel", "square", "diagonal", "isometric", "hex", "triangle"],
                   help="with --pixel-angles: which edge angles are allowed. pixel = all pixel-art ratios "
                        "(DRAW's angle snap); square = 0/90; diagonal = +45; isometric = 2:1 + vertical; "
                        "hex = flats + 45 (DRAW's flat-top hex); triangle = flats + 1:2 (~60). default pixel")
    p.add_argument("--despeckle", type=int, default=2, metavar="N",
                   help="after the palette lock, recolor stray same-color islands of <= N pixels "
                        "to their surroundings (kills random speckle noise). default 2, 0 = off. "
                        "skipped with --dither")
    p.add_argument("--snap-colors", dest="snap_colors", type=int, default=0,
                   help="color cap for --snap-pixels (0 = auto)")
    p.add_argument("--smooth", choices=["mode", "median", "none"], default="mode",
                   help="flatten noise before downscaling (mode=cleanest). default mode")
    p.add_argument("--filter", choices=["box (area average)", "nearest"],
                   default="nearest", help="downscale: nearest (crisp) / box (soft). default nearest")
    p.add_argument("--steps", type=int, default=None,
                   help="sampling steps (default 25, or 8 with --fast)")
    p.add_argument("--cfg", type=float, default=None,
                   help="prompt strength (default 7, or 1.5 with --fast)")
    p.add_argument("--seed", type=int, default=-1, help="-1 = random each run")
    p.add_argument("--negative", default=None,
                   help="negative prompt (what to avoid). default: a pixel-art-tuned list, "
                        "or an illustration-tuned list with --art")
    p.add_argument("--name", default=None, help="output filename base (default: from prompt)")
    # --- where finished files go (relative to the directory you run pixelmon FROM) ---
    p.add_argument("--output-to", dest="output_to", default=None, metavar="DIR",
                   help="move finished files into DIR (relative to your current directory)")
    p.add_argument("--move-to-dirs", dest="move_to_dirs", action="store_true",
                   help="organize each run into its own subdir named after the prompt (or --name)")
    p.add_argument("--create-dirs", dest="create_dirs", action="store_true",
                   help="create the output dir(s) if they don't exist")
    p.add_argument("--no-subdirs", dest="no_subdirs", action="store_true",
                   help="with --batch/--output-to: dump everything flat into the one folder "
                        "(no per-subject subdirs) — files are uniquely named, so all viewable together")
    p.add_argument("--custom-hex", dest="custom_hex", default="",
                   help='hex codes when --palette Custom, e.g. "#000 #fff #f00"')
    p.add_argument("--preview", action="store_true",
                   help="also save an enlarged, zoomed-in PNG (default: only the true-size sprite)")
    p.add_argument("--view-scale", dest="view_scale", type=int, default=8,
                   help="enlarge factor for --preview. default 8")
    # --- model / engine ---
    p.add_argument("--server", default=None, metavar="NAME|HOST[,...]",
                   help="render on a remote ComfyUI: a servers.json alias (e.g. 'titan') "
                        "or host[:port]/URL. comma-list = RENDER FARM, jobs fan across all "
                        "GPUs (e.g. 'rtx,titan,local'). default: $PIXELMON_SERVER, else servers.json "
                        "\"_default\", else local")
    p.add_argument("--base", default="sd_xl_base_1.0.safetensors", help="SDXL base checkpoint")
    p.add_argument("--lora", default="pixel-art-xl.safetensors", help="pixel-art LoRA")
    # --- steering: nudge output toward a folder of reference images (IPAdapter) ---
    p.add_argument("--steer", default=None, metavar="DIR|IMG",
                   help="steer output toward reference image(s) via IPAdapter (a folder, or one image)")
    # --- depth ControlNet: lock camera angle + geometry of a reference, the prompt sets subject/backdrop/style ---
    p.add_argument("--depth", default=None, metavar="IMG",
                   help="depth ControlNet: keep the camera angle and geometry of this image (made into a depth map "
                        "with Depth Anything V2) while the prompt sets subject, backdrop and style. needs "
                        "comfyui_controlnet_aux + controlnet-depth-sdxl-1.0 on the server")
    p.add_argument("--depth-strength", dest="depth_strength", type=float, default=0.7, metavar="F",
                   help="how strictly to follow the depth map, 0..2. default 0.7")
    p.add_argument("--depth-start", dest="depth_start", type=float, default=0.0, metavar="F",
                   help="fraction of the steps at which the depth map switches ON (0..1). default 0")
    p.add_argument("--depth-end", dest="depth_end", type=float, default=0.65, metavar="F",
                   help="fraction of the steps at which the depth map switches OFF; the rest are free for detail. "
                        "default 0.65")
    p.add_argument("--depth-raw", dest="depth_raw", action="store_true",
                   help="the --depth image is already a depth map (white = near): skip preprocessing")
    p.add_argument("--depth-mask-bg", dest="depth_mask_bg", action="store_true",
                   help="find the foreground subject (BiRefNet) and set all background depth to black, so the "
                        "reference's scenery doesn't come through — only the subject's shape is kept")
    p.add_argument("--depth-keep-bg", dest="depth_keep_bg", action="store_true",
                   help="keep a plain backdrop's depth: by default a --depth picture on a flat-colour or transparent "
                        "backdrop (a sprite, a cut-out) gets --depth-mask-bg, because Depth Anything reads the empty "
                        "space as a wall or ceiling and the image model fills it (a second copy of the subject)")
    p.add_argument("--depth-model", dest="depth_model", default="controlnet-depth-sdxl-1.0.safetensors",
                   help="depth ControlNet file in the server's models/controlnet/")
    p.add_argument("--depth-bg-model", dest="depth_bg_model", default="birefnet.safetensors",
                   help="background-removal model for --depth-mask-bg, in the server's models/background_removal/")
    p.add_argument("--depth-only", dest="depth_only", action="store_true",
                   help="with --depth: make and save the processed depth map (what the sampler would get), then stop")
    p.add_argument("--control", default=None, metavar="IMG",
                   help="ControlNet: keep the SHAPE of this image while the prompt/LoRA/palette restyle it "
                        "(photo of a dog -> EGA dog). needs the SDXL union ControlNet on the server")
    p.add_argument("--control-mode", dest="control_mode", default="canny", choices=["canny", "tile"],
                   help="canny = follow its edges/outlines (most restyle freedom); tile = keep its layout + colors")
    p.add_argument("--control-strength", dest="control_strength", type=float, default=0.8, metavar="F",
                   help="how strictly to follow the control image, 0..2. default 0.8")
    p.add_argument("--canny-low", dest="canny_low", type=float, default=0.3, metavar="F",
                   help="with --control-mode canny: low edge threshold 0.01..0.99 (lower = more faint edges). default 0.3")
    p.add_argument("--canny-high", dest="canny_high", type=float, default=0.7, metavar="F",
                   help="with --control-mode canny: high edge threshold 0.01..0.99 (lower = more strong edges). default 0.7")
    p.add_argument("--inpaint-model", dest="inpaint_model", default=None, metavar="FILE",
                   help="with --mask: use this SDXL inpainting UNet (in the server's models/unet/, e.g. "
                        "sdxl-inpainting-0.1.fp16.safetensors) — far better at painting NEW things into the mask")
    p.add_argument("--control-end", dest="control_end", type=float, default=0.8, metavar="F",
                   help="fraction of the steps the control applies for; the rest are free for style. default 0.8")
    p.add_argument("--control-model", dest="control_model", default="controlnet-union-sdxl-promax.safetensors",
                   help="ControlNet file in the server's models/controlnet/")
    p.add_argument("--mask", default=None, metavar="IMG",
                   help="inpaint (with --init): only the WHITE area of this mask image is redrawn; everything else "
                        "stays exactly as in --init. Use with --denoise ~0.85 (change), 0.95 (erase/add)")
    p.add_argument("--mask-mode", dest="mask_mode", default="fill", choices=["fill", "blend"],
                   help="fill = the masked area starts blank, so something new is drawn (change/add/erase; use "
                        "--denoise 1); blend = starts from what's there (subtle tweaks, --denoise 0.4-0.7). default fill")
    p.add_argument("--mask-grow", dest="mask_grow", type=int, default=6, metavar="PX",
                   help="blend margin around the mask at generation size. default 6")
    p.add_argument("--no-inpaint-crop", dest="inpaint_crop", action="store_false",
                   help="with --mask: render the whole image instead of just the area around a small mask "
                        "(crop-and-stitch is on by default: small edits get far more detail and follow the prompt)")
    p.add_argument("--no-keep-outside", dest="keep_outside", action="store_false",
                   help="with --mask: don't paste the result back onto the original (let the whole image drift)")
    p.add_argument("--init", default=None, metavar="IMG",
                   help="img2img: start from this image instead of noise (keeps its composition). without "
                        "--size / --out the result takes the image's shape")
    p.add_argument("--denoise", type=float, default=0.6, metavar="F",
                   help="with --init: how much to change, 0..1 (0 = same image, 1 = ignore it). default 0.6")
    p.add_argument("--steer-strength", dest="steer_strength", type=float, default=0.7,
                   help="IPAdapter weight: 0=off .. ~1 strong (default 0.7)")
    p.add_argument("--steer-start", dest="steer_start", type=float, default=0.0, metavar="F",
                   help="fraction of the sampling steps at which the refs switch ON (0..1). default 0")
    p.add_argument("--steer-end", dest="steer_end", type=float, default=1.0, metavar="F",
                   help="fraction of the sampling steps at which the refs switch OFF (0..1). e.g. 0.5 = refs set "
                        "palette/composition early, then the prompt + LoRA finish alone. default 1")
    p.add_argument("--steer-weight-type", dest="steer_weight_type", default="style transfer",
                   help="IPAdapter weight_type (default 'style transfer'; e.g. 'linear', 'composition')")
    p.add_argument("--steer-combine", dest="steer_combine", default="concat",
                   help="blend multiple refs: concat / average / norm average / add / subtract")
    p.add_argument("--steer-max", dest="steer_max", type=int, default=16,
                   help="max reference images to pull from a folder (default 16)")
    p.add_argument("--steer-model", dest="steer_model", default="ip-adapter-plus_sdxl_vit-h.safetensors",
                   help="IPAdapter model file (in models/ipadapter/)")
    p.add_argument("--steer-clip", dest="steer_clip", default="CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
                   help="CLIP-vision model file (in models/clip_vision/)")
    p.add_argument("--lora-strength", dest="lora_strength", type=float, default=1.0,
                   help="LoRA strength. default 1.0 (try 1.2 for stronger pixelation)")
    p.add_argument("--res", type=int, default=1024, help="SDXL generation resolution. default 1024")
    p.add_argument("--pixel-grid", dest="pixel_grid", type=int, default=0,
                   help="logical pixel resolution (long side) before upscaling to --size; "
                        "0 = auto. For clean pixel art at a big --size, set a value that divides "
                        "it evenly (e.g. 160 for 320x200 = clean 2x blocks).")
    p.add_argument("--sampler", default=None,
                   help="ksampler sampler_name (default euler, or lcm with --fast)")
    p.add_argument("--scheduler", default=None,
                   help="ksampler scheduler: normal, karras, exponential, sgm_uniform, simple, beta … "
                        "(default normal, or sgm_uniform with --fast)")
    p.add_argument("--no-lora", dest="no_lora", action="store_true",
                   help="generate from the base model only (no pixel-art LoRA)")
    p.add_argument("--art", action="store_true",
                   help="DIGITAL ART mode: skip pixelation entirely — no pixel LoRA, no pixel "
                        "downscale. Saves SDXL's full-res image (default 1024px; a smaller --out is a "
                        "smooth resize). --palette pulls its colors toward a palette (--palette-strength).")
    p.add_argument("--exact-prompt", dest="exact_prompt", action="store_true",
                   help="send the prompt and --negative exactly as written: no 'pixel, a …' prefix, no sprite / "
                        "scene suffix, no --style text, no default negative (an omitted --negative = empty)")
    p.add_argument("--raw", action="store_true",
                   help="RAW mode: the same prompt and LoRA as a pixel render, but saves the model's own "
                        "full-res picture (SDXL's VAE output) — no downscale, palette or cleanup step")
    p.add_argument("--post-sweep", dest="post_sweep", default=None, metavar="SPEC",
                   help="render once, then save every combination of post-processing settings, e.g. "
                        "'angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1' (fields: "
                        "angle_grid, dither, dither_amount, pixel_angles, thin_lines, despeckle, palette)")
    p.add_argument("--palette-strength", dest="palette_strength", type=float, default=0.6, metavar="F",
                   help="--art with a --palette: how far colors move toward the palette, 0..1 "
                        "(1 = every pixel exactly a palette color, posterized; ~0.4-0.7 keeps smooth shading). default 0.6")
    p.add_argument("--fast", action="store_true",
                   help="LCM mode: ~3-4x faster (8 steps); small quality trade-off")
    p.add_argument("--lcm-lora", dest="lcm_lora", default="lcm-lora-sdxl.safetensors",
                   help="LCM LoRA filename (used with --fast)")
    # --- animation: looping portrait gestures, à la 1988 Wasteland ---
    p.add_argument("--animate", default=None, metavar="GESTURE",
                   help='make a looping animated GIF. a preset (blink/talk/glow/smoke/breathe) '
                        'OR free text like "licks chops", "smoke rising from cigar". '
                        'generates a base portrait, then animates ONE region.')
    p.add_argument("--anim-region", dest="anim_region", default=None, metavar="WHAT",
                   help='what to auto-mask & animate (CLIPSeg text), e.g. "the eyes", "the cigar", '
                        '"the gun". works on dogs/monsters too. default: guessed from the gesture')
    p.add_argument("--anim-frames", dest="anim_frames", type=int, default=None,
                   help="distinct motion frames: 2 = toggle (blink); 3-5 = motion (smoke). default per-preset or 2")
    p.add_argument("--anim-fps", dest="anim_fps", type=float, default=6.0,
                   help="loop SPEED in frames/second during the gesture. default 6")
    p.add_argument("--anim-hold", dest="anim_hold", type=float, default=1.2,
                   help="seconds to hold the resting pose between gestures. default 1.2")
    p.add_argument("--anim-denoise", dest="anim_denoise", type=float, default=None,
                   help="how strongly the region changes per frame (0.3 subtle .. 0.9 strong). default per-preset or 0.65")
    p.add_argument("--anim-loop", dest="anim_loop", default=None,
                   choices=["pingpong", "cycle", "once-return"],
                   help="frame ordering. default per-preset or pingpong")
    p.add_argument("--anim-box", dest="anim_box", default=None, metavar="L,T,R,B",
                   help="manual mask box as fractions 0..1 (overrides auto-mask), e.g. 0.25,0.36,0.67,0.49")
    p.add_argument("--anim-res", dest="anim_res", type=int, default=768,
                   help="base/inpaint generation resolution (bigger = better region detail). default 768")
    p.add_argument("--list-palettes", action="store_true", help="list palettes and exit")
    p.add_argument("--list-styles", action="store_true", help="list style guides and exit")
    p.add_argument("--no-open", action="store_true", help="don't auto-open the result image")
    p.add_argument("--show-prompt", dest="show_prompt", action="store_true",
                   help="print the exact positive + negative prompts sent to the model "
                        "(after styles, LoRA tags and pixelmon's own additions)")
    p.add_argument("--no-sprite-suffix", dest="no_sprite_suffix", action="store_true",
                   help="don't append 'game sprite, ..., solid background' (automatic when the "
                        "prompt contains 'scene background')")
    p.add_argument("--batch-file", dest="batch_file", default=None, metavar="FILE",
                   help="a list of sprites, one per line: 'key | prompt' (also 'key | style | size | prompt', "
                        "'key | style-or-size | prompt', or a prompt on its own; '# comments', [Group] lines). "
                        "each row's files are named by its key (a key like enemies/bat goes in ./enemies/); "
                        "-n = takes of each. the same lists the GUI's Sets tab reads")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="with --batch/--batch-file/-n: don't make takes that are already there (a key with 2 of "
                        "-n 4 files gets 2 more; with --seed, a seed whose file exists is skipped) — re-run a "
                        "stopped list and it carries on")
    p.add_argument("--palette-from", dest="palette_from", default=None, metavar="IMG",
                   help="lock to the colours of a reference picture (sets --palette Custom): all of them when it "
                        "has few (pixel art), else its main colours (--palette-max). with --art, a colour pull at "
                        "--palette-strength")
    p.add_argument("--palette-max", dest="palette_max", type=int, default=None, metavar="N",
                   help="--palette-from: most colours to take from the picture (2..256). default: all of them "
                        "up to 64 (pixel art), else its 32 main colours — what the GUI picks")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="show what would be made (jobs, prompts, where the files go) without contacting a server")
    p.add_argument("--print-graph", dest="print_graph", action="store_true",
                   help="print the first job's ComfyUI API graph as JSON and exit (no server; uploads are "
                        "shown as <upload:NAME>)")
    p.add_argument("--list-servers", dest="list_servers", action="store_true",
                   help="every render server in servers.json (or --server's list): up/down, GPU, queue, and "
                        "whether it has what this command needs (add your usual flags)")
    p.add_argument("--no-check", dest="no_check", action="store_true",
                   help="don't check that the server has every node, input and model the render needs "
                        "before sending it")
    p.add_argument("--wrapper-local-url", dest="wrapper_local_url", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()

    # Resolve the render target: --server (alias/URL) > $PIXELMON_SERVER > servers.json
    # "_default" > local. Sets the module globals used by submit() / wait() / fetch_image().
    global SERVER, REMOTE, POOL, CHECKS_OFF
    _target = a.server or os.environ.get("PIXELMON_SERVER") or DEFAULT_SERVER
    if _target:
        POOL = [resolve_server(s.strip()) for s in _target.split(",") if s.strip()]
        SERVER = POOL[0]                       # first entry is the single-server default
    REMOTE = not is_local(SERVER)
    CHECKS_OFF = a.no_check

    nothing_to_make = not (a.prompt or a.batch or a.batch_file or a.mesh or a.turnaround)
    if a.wrapper_local_url:
        # bin/pixelmon asks which ComfyUI on this machine (if any) this command renders on, and starts the
        # default one when it's down. Help, listings and dry runs need none; neither does a remote target.
        if not (a.show_help or a.list_palettes or a.list_styles or a.list_servers or a.dry_run or a.print_graph
                or nothing_to_make):
            for srv in (POOL or [SERVER]):
                if is_local(srv):
                    print(srv)
            if len(POOL) > 1:
                print("farm")                  # the other boxes can carry on if this one won't start
        return

    # Friendly help on -h/--help, or when run with no prompt at all.
    if a.show_help or (nothing_to_make and not a.list_palettes and not a.list_styles and not a.list_servers):
        print_help()
        return
    if a.list_servers and nothing_to_make:      # check the boxes for a plain render
        a.prompt = "a test sprite"
    graph_out = sys.stdout
    if a.print_graph:                    # only the JSON on stdout, so it can be piped; notes go to stderr
        sys.stdout = sys.stderr
    if a.mesh or a.turnaround:               # 3D: a model, and rotation sprites painted from it (turnaround.py)
        import turnaround
        return turnaround.run(sys.modules[__name__], a, p)
    if a.list_palettes:
        print_palettes()
        return
    if a.list_styles:
        print_styles()
        return
    if a.custom_hex:                     # #0af -> #00aaff (older palette nodes only take 6 digits)
        hexes = [t.lstrip("#") for t in a.custom_hex.replace(",", " ").split()]
        a.custom_hex = " ".join("#" + ("".join(c * 2 for c in h) if len(h) == 3 else h) for h in hexes)
    if a.palette not in ("none", "random", "Custom") and a.palette not in PALETTES:
        p.error(f"unknown palette {a.palette!r}. See --list-palettes.")
    if a.palette_from:                   # the reference's own colours become a Custom palette
        src = os.path.expanduser(a.palette_from)
        if not os.path.isfile(src):
            p.error(f"--palette-from: no such image: {a.palette_from}")
        if a.palette != "none" or a.custom_hex:
            p.error("--palette-from sets the palette itself: drop --palette / --custom-hex")
        if a.palette_max is not None and not 2 <= a.palette_max <= 256:
            p.error("--palette-max must be 2..256")
        sys.path.insert(0, _SCRIPT_DIR)
        from colors import picture_palette
        try:
            # the GUI's pick by default (exact up to 64, else 32 by median cut); --palette-max caps both
            hexes = (picture_palette(src) if a.palette_max is None else
                     picture_palette(src, cap=a.palette_max, exact=a.palette_max))
        except Exception as e:
            p.error(f"--palette-from: couldn't read {a.palette_from}: {e}")
        a.palette, a.custom_hex = "Custom", " ".join(hexes)
        print(f"   🎨 palette from {os.path.basename(src)}: {len(hexes)} colours")

    # Mode-dependent defaults (only when the user didn't override them):
    #   --art  -> full-res illustration: bigger canvas, illustration-tuned negative.
    #   pixel  -> small sprite, pixel-tuned negative.
    if a.size is None and not a.out and a.init and os.path.isfile(os.path.expanduser(a.init)):
        # --init with no size: keep the reference's shape (a 320x200 screen stays 16:10 instead of being squashed
        # into the default square), at the usual long side
        try:
            from PIL import Image
            with Image.open(os.path.expanduser(a.init)) as im:
                iw, ih = im.size
            side = 1024 if a.art else 128
            if iw != ih:
                a.size = f"{side}x{max(8, round(side * ih / iw))}" if iw > ih else f"{max(8, round(side * iw / ih))}x{side}"
                print(f"   🎯 from the reference: {a.size} (its shape; --size / --out override)")
        except Exception:
            pass
    if a.size is None:
        a.size = a.out if a.out else ("1024" if a.art else "128")
    if a.negative is None:
        a.negative = "" if a.exact_prompt else ART_NEGATIVE if a.art else PIXEL_NEGATIVE
    if a.exact_prompt and (a.style or a.animate):
        p.error("--exact-prompt sends your text as-is, so it can't be combined with --style or --animate "
                "(put the style words in the prompt yourself)")
    if a.art:
        ignored = [f for f, on in [
            ("--transparent", a.transparent), ("--dither (needs a --palette)", a.dither != "none" and a.palette == "none"),
            ("--snap-pixels", a.snap_pixels), ("--pixel-grid", a.pixel_grid > 0),
            ] if on]
        if ignored:
            print(f"   note: {', '.join(ignored)} have no effect in --art mode "
                  f"(there's no pixelation step to apply them to)")

    if a.raw and not a.art:
        print("   note: --raw saves the model's full-res picture; palette, dither and cleanup options are skipped")

    a.px_w = a.px_h = 0
    if a.pixel_size:
        try:
            a.px_w, a.px_h = (int(v) for v in str(a.pixel_size).lower().split("x", 1))
        except ValueError:
            p.error(f"bad --pixel-size {a.pixel_size!r}; use WxH like 1x1, 2x1, 4x4")
        if not (1 <= a.px_w <= 32 and 1 <= a.px_h <= 32):
            p.error("--pixel-size parts must be 1..32")
    if not 0.0 <= a.steer_start < a.steer_end <= 1.0:
        p.error("--steer-start/--steer-end must satisfy 0 <= start < end <= 1")
    if not 0.0 <= a.palette_strength <= 1.0:
        p.error("--palette-strength must be 0..1")
    a.post_combos = None
    if a.raw and (a.art or a.animate or a.mask or a.post_sweep):
        p.error("--raw can't be combined with --art, --animate, --mask or --post-sweep")
    if a.post_sweep:
        if a.art or a.animate or a.mask:
            p.error("--post-sweep works on pixel-art renders (not --art, --animate or LAB edits)")
        a.post_combos = parse_post_sweep(a.post_sweep, p)
    if a.mask and not a.init:
        p.error("--mask needs --init (the image to edit)")
    if a.mask and not os.path.isfile(os.path.expanduser(a.mask)):
        p.error(f"--mask: no such image: {a.mask}")
    if a.control and not os.path.isfile(os.path.expanduser(a.control)):
        p.error(f"--control: no such image: {a.control}")
    if not (0 < a.control_end <= 1.0 and 0 <= a.control_strength <= 2.0):
        p.error("--control-end must be in (0, 1] and --control-strength in [0, 2]")
    if a.depth:
        if not os.path.isfile(os.path.expanduser(a.depth)):
            p.error(f"--depth: no such image: {a.depth}")
        if not 0.0 <= a.depth_start < a.depth_end <= 1.0:
            p.error("--depth-start/--depth-end must satisfy 0 <= start < end <= 1")
        if not 0.0 <= a.depth_strength <= 2.0:
            p.error("--depth-strength must be 0..2")
        if a.depth_raw and a.depth_mask_bg:
            p.error("--depth-mask-bg finds the subject in a picture; with --depth-raw there's only a depth map "
                    "(black out its background yourself)")
        if a.mask or a.animate:
            p.error("--depth can't be combined with --mask or --animate")
        if a.depth_mask_bg and a.depth_keep_bg:
            p.error("--depth-mask-bg and --depth-keep-bg: pick one")
        if not (a.depth_raw or a.depth_mask_bg or a.depth_keep_bg) and plain_backdrop(os.path.expanduser(a.depth)):
            a.depth_mask_bg = True                  # an object on an empty backdrop, not a scene
            print("   \U0001f5fa depth: the picture is on a plain backdrop, so its background goes black "
                  "(--depth-keep-bg keeps it)")
    elif a.depth_raw or a.depth_mask_bg or a.depth_only or a.depth_keep_bg:
        p.error("--depth-raw / --depth-mask-bg / --depth-keep-bg / --depth-only need --depth IMG")
    if not (0 < a.canny_low < a.canny_high < 1):
        p.error("--canny-low / --canny-high must be 0..1 with low < high")
    if a.fit_to:
        a.fit_to = os.path.expanduser(a.fit_to)
        if not os.path.isfile(a.fit_to):
            p.error(f"--fit-to: no such image: {a.fit_to}")
        a.cutout = True
    if a.cutout:
        if a.transparent:
            p.error("--cutout replaces --transparent's colour match with a model cut-out: use one of them")
        if a.animate or a.tile or a.post_sweep:
            p.error("--cutout works on single pictures (not --animate, --tile or --post-sweep)")
    elif a.cutout_key or a.fit_scale:
        p.error("--cutout-key / --fit-scale need --cutout (or --fit-to)")
    if a.cutout_key < 0 or a.fit_scale < 0:
        p.error("--cutout-key and --fit-scale can't be negative")
    if a.init:
        if not os.path.isfile(os.path.expanduser(a.init)):
            p.error(f"--init: no such image: {a.init}")
        if not 0.0 <= a.denoise <= 1.0:
            p.error("--denoise must be between 0 and 1")

    # Resolve --style guide(s) into prompt/negative additions (used by build_graph).
    try:
        a.style_add, a.style_neg = resolve_styles(a.style)
    except ValueError as e:
        p.error(str(e))

    # Resolve sampler settings by mode. --fast = LCM (8 steps, low cfg, lcm
    # sampler + sgm_uniform schedule); default = full-quality 25-step euler.
    if a.fast:
        a.steps = 8 if a.steps is None else a.steps
        a.cfg = 1.5 if a.cfg is None else a.cfg
        a.sampler = "lcm" if a.sampler is None else a.sampler
        a.scheduler = "sgm_uniform" if a.scheduler is None else a.scheduler
    else:
        a.steps = 25 if a.steps is None else a.steps
        a.cfg = 7.0 if a.cfg is None else a.cfg
        a.sampler = "euler" if a.sampler is None else a.sampler
        a.scheduler = "normal" if a.scheduler is None else a.scheduler

    def _r64(v):
        return max(64, int(round(v / 64.0)) * 64)

    def apply_size(a):
        """--size / --out -> output, art-grid and generation sizes (also run for a --batch-file row with its own
        size)."""
        # Parse --size into target W x H (square if a single number), plus a matching
        # generation resolution (long side = --res, kept ~proportional so the subject
        # isn't distorted); the node then nails the exact W x H.
        try:
            if "x" in str(a.size).lower():
                a.sw, a.sh = (int(v) for v in str(a.size).lower().split("x", 1))
            else:
                a.sw = a.sh = int(a.size)
        except ValueError:
            p.error(f"bad --size {a.size!r}; use N or WxH (e.g. 32 or 32x48)")
        if a.sw < 1 or a.sh < 1:
            p.error("--size dimensions must be >= 1")
        a.ow, a.oh = a.sw, a.sh
        if a.out:
            try:
                if "x" in str(a.out).lower():
                    a.ow, a.oh = (int(v) for v in str(a.out).lower().split("x", 1))
                else:
                    a.ow = a.oh = int(a.out)
            except ValueError:
                p.error(f"bad --out {a.out!r}; use N or WxH (e.g. 320x200)")
            if not (1 <= a.ow <= 4096 and 1 <= a.oh <= 4096):
                p.error("--out dimensions must be 1..4096")
        # Pixel art past 1024 (wallpapers): the palette node works up to 1024, so the art is made at 1/k of the
        # size and enlarged by exactly k afterwards — nearest neighbour, so every pixel stays square and crisp.
        a.enlarge = 1
        if not a.art and max(a.ow, a.oh) > 1024:
            k = next((k for k in range(2, 17) if a.ow % k == 0 and a.oh % k == 0 and max(a.ow, a.oh) <= 1024 * k), None)
            if not k:
                p.error(f"--out {a.ow}x{a.oh}: past 1024, pixel art has to divide evenly by a whole number "
                        f"(1920x1080, 2560x1440, 3840x2160 … all do)")
            a.enlarge, a.ow, a.oh = k, a.ow // k, a.oh // k
            if max(a.sw, a.sh) > 1024:
                a.sw, a.sh = a.ow, a.oh

        a.gen_w = a.res if a.sw >= a.sh else _r64(a.res * a.sw / a.sh)
        a.gen_h = a.res if a.sh >= a.sw else _r64(a.res * a.sh / a.sw)

        if a.art and max(a.sw, a.sh) > a.res:
            # Art mode bigger than --res (wallpapers): SDXL paints its own ~1-megapixel size for that shape
            # (1344x768 for 16:9) and the picture is resized up to W x H. Painting straight at 1920+ repeats
            # the subject, and 4K doesn't fit an 8 GB card. Smaller sizes keep the --res render above and
            # are resized down: SDXL can't draw at 256 px (it makes abstract blobs).
            ar = a.sw / a.sh
            a.gen_w, a.gen_h = _r64(a.res * ar ** 0.5), _r64(a.res / ar ** 0.5)

    apply_size(a)

    # Inpaint crop-and-stitch: a small mask is a small patch of a 1024px render, so the model mostly
    # continues the surroundings. Cut out the mask's neighbourhood (with context), render THAT at full
    # size — the edit gets real detail and follows the prompt — and paste it back afterwards.
    a._stitch = None
    if a.mask and a.init and a.inpaint_crop:     # art mode too: it renders at full size now, so the crop works there
        a._stitch = prepare_inpaint_crop(a, _r64)

    # Animation mode is its own pipeline (base -> mask -> inpaint frames -> GIF).
    if a.animate:
        sys.path.insert(0, _SCRIPT_DIR)
        import animate
        animate.run(a, pal_map=(_pal.ALL_PALETTES if _pal else None), slug=slug, P=sys.modules[__name__])
        return

    n = max(1, a.number)
    # Subjects: one (the prompt), many (--batch round-robins one of each per pass), or the rows of a
    # --batch-file, each named by its key and maybe with its own style / size.
    rows = None
    if a.batch_file:
        if a.prompt or a.batch:
            p.error("--batch-file gives the prompts: drop the prompt / --batch")
        if a.animate:
            p.error("--batch-file makes still pictures (not --animate)")
        sys.path.insert(0, _SCRIPT_DIR)
        import manifest
        try:
            with open(os.path.expanduser(a.batch_file), encoding="utf-8") as f:
                rows = manifest.parse_manifest(f.read())
        except (OSError, ValueError) as e:
            p.error(f"--batch-file: {e}")
        subjects = [r["prompt"] for r in rows]
    else:
        subjects = [s.strip() for s in a.batch.split(",") if s.strip()] if a.batch else [a.prompt]

    # Where finished files go — relative to the directory you ran pixelmon FROM:
    #   --batch        -> one folder per subject  (./<subject>/)
    #   --batch-file   -> the folder in each key  (enemies/bat -> ./enemies/), flat for a plain key
    #   --move-to-dirs -> one folder named after the prompt  (./<prompt>/)
    #   --output-to D  -> that folder D (flat); else left in ComfyUI's output.
    base = os.path.abspath(os.path.expanduser(a.output_to)) if a.output_to else os.getcwd()

    def dest_for(subject, key=None):
        if a.no_subdirs:
            # flatten: everything into the one base folder (files are uniquely named,
            # so you can browse them all in a single dir)
            d = base if (a.output_to or a.batch or a.batch_file or a.move_to_dirs) else None
        elif key is not None:
            d = os.path.join(base, os.path.dirname(key)) if os.path.dirname(key) else base
        elif a.batch:
            d = os.path.join(base, slug(subject))
        elif a.move_to_dirs:
            d = os.path.join(base, a.name or slug(a.prompt))
        elif a.output_to:
            d = base
        else:
            d = None
        if d is None:
            return None
        if not os.path.isdir(d) and not (a.dry_run or a.print_graph or a.list_servers):   # those write nothing
            if a.create_dirs or a.move_to_dirs or a.batch or a.batch_file:
                os.makedirs(d, exist_ok=True)
            else:
                p.error(f"output dir does not exist: {d}\n  (add --create-dirs to make it)")
        return d

    def row_args(row):
        """The settings one --batch-file row is made with: its key as the file name, its style and size."""
        import copy
        ja = copy.copy(a)
        ja.name = re.sub(r"[^\w.=+-]+", "_", os.path.basename(row["key"])).strip("_") or "sprite"
        ja._row_key = row["key"]
        if row.get("style"):
            if a.exact_prompt:
                p.error(f"--batch-file row {row['key']!r} has a style, which --exact-prompt can't add")
            try:
                ja.style = row["style"]
                ja.style_add, ja.style_neg = resolve_styles(row["style"])
            except ValueError as e:
                p.error(f"--batch-file row {row['key']!r}: {e}")
        if row.get("size"):
            if a.mask or a.depth:
                p.error(f"--batch-file row {row['key']!r} has its own size, which --mask / --depth can't follow")
            ja.size, ja.out = row["size"], None
            apply_size(ja)
        return ja

    if rows:
        settings = [row_args(r) for r in rows]
        dests = [dest_for(r["prompt"], r["key"]) for r in rows]
        labels = [r["key"] for r in rows]
    else:
        settings = [a] * len(subjects)
        dests = [dest_for(subj) for subj in subjects]
        labels = list(subjects)

    # Build the work list. --batch round-robins (one of each subject per pass) so
    # every folder fills evenly instead of finishing one subject at a time.
    jobs = []
    k = 0
    for _ in range(n):
        for i, subj in enumerate(subjects):
            seed = (a.seed + k) if a.seed >= 0 else random.randint(0, 2**31 - 1)
            pal = random.choice(PALETTES) if a.palette == "random" else a.palette
            jobs.append(Job(len(jobs), subj, seed, pal, dests[i], settings[i], labels[i]))
            k += 1
    if a.depth:                          # until prepare_depth makes the real map: for graphs nobody sends
        a._depth_map = os.path.abspath(os.path.expanduser(a.depth))
    if a.skip_existing:
        jobs = skip_made(a, jobs)
        if not jobs:
            print("   ⏭ every take is already there (--skip-existing): nothing to do")
            return

    if a.list_servers:                   # which boxes can render THIS command
        list_servers(a, jobs)
        return
    if a.print_graph:                    # the JSON alone, so it can be piped
        print(json.dumps(jobs[0].graph(DRY), indent=1), file=graph_out)
        return

    total = len(jobs)
    per = 20 if a.fast else 100  # rough seconds/image for the ETA
    art_pal = f" + {a.palette} @ {a.palette_strength:g}" if a.palette != "none" and a.palette_strength > 0 else ""
    pal_label = ("ART / full-res" + art_pal) if a.art else "RAW / model output (no pixel step)" if a.raw else ("random" if a.palette == "random" else a.palette)
    style_label = f"  |  style: {a.style}" if a.style else ""
    many = bool(a.batch or a.batch_file)
    subj_label = (f"{len(subjects)} rows from {os.path.basename(a.batch_file)}" if a.batch_file else
                  f"{len(subjects)} subjects: {', '.join(subjects)}" if a.batch else repr(a.prompt))
    count_label = f"{n} each = {total} total" if many else f"{total} image(s)"
    lw, lh = (a.sw, a.sh) if a.art else (a.gen_w, a.gen_h) if a.raw else (a.ow * a.enlarge, a.oh * a.enlarge)
    size_label = f"{lw}x{lh}" if lw != lh else f"{lw}px"
    if a.dry_run:
        print("🧪 dry run: nothing is sent to a server")
    if len(POOL) > 1:
        print(f"🚜 render farm: {len(POOL)} servers — {', '.join(_short(s) for s in POOL)}")
    elif REMOTE:
        print(f"🌐 rendering on remote server {SERVER} (results fetched back here)")
    print(f"🎨 {subj_label}  |  {size_label}  |  {pal_label}"
          f"{' |  transparent' if a.transparent else ''}{style_label}  |  "
          f"{'FAST/LCM' if a.fast else 'quality'} {a.steps}st  |  {count_label}")
    if a.show_prompt or a.dry_run:
        lora = ("none" if a.no_lora or a.art else f"{a.lora} @ {a.lora_strength:g}") + \
               (f" + {a.lcm_lora}" if a.fast else "")
        shown = set()
        for job in jobs:
            if (job.subj, id(job.a)) in shown:
                continue
            shown.add((job.subj, id(job.a)))
            pos, neg = final_prompts(job.a, job.subj)
            print(f"   📝 positive: {pos}\n   🚫 negative: {neg}\n   🧩 lora:     {lora}")
    if a.dry_run:
        for i, job in enumerate(jobs, 1):
            ja = job.a
            sz = f"{ja.ow * ja.enlarge}x{ja.oh * ja.enlarge}" if not (ja.art or ja.raw) else f"{ja.sw}x{ja.sh}"
            print(f"   · [{i}/{total}] {job.label + '  ' if many else ''}seed={job.seed}  {sz}  pal={job.pal}  "
                  f"->  {job.dest or os.path.join(OUTPUT, 'pixelmon')}/{(ja.name if rows else slug(job.subj) if a.batch else a.name or slug(job.subj))}_…png")
        return
    if total > 1:
        eta = total * per
        tip = "" if a.fast else "  (tip: add --fast for quick variations)"
        print(f"   ~{eta // 60}m{eta % 60:02d}s estimated{tip}")

    if a.depth:
        prepare_depth(a, next((d for d in dests if d), None) or os.path.join(OUTPUT, "pixelmon"),
                      a.name or slug(subjects[0]))
        for ja in settings:                  # --batch-file rows were copied before the map existed
            ja._depth_map = a._depth_map
        if a.depth_only:
            return

    t0 = time.time()
    first_open = None
    failed = 0
    if len(POOL) > 1 and a.post_combos:
        print("   note: --post-sweep renders on one server — using the first in the list")
    if len(POOL) > 1 and not a.post_combos:
        # Render farm: fan the whole work list out across all the GPUs in the pool.
        made, failed = run_farm(a, jobs)
        first_open = made[0] if made else None
    else:
        # Single server: queue everything up front; ComfyUI runs them one at a time. A job that fails is
        # reported and the batch carries on (the rest were already queued and would render for nobody).
        queued = []
        for job in jobs:
            g = job.graph(SERVER)
            queued.append((job, submit(g), g))
        if total > 1:
            print(f"   queued {total} jobs; generating...")
        for i, (job, pid, g) in enumerate(queued, 1):
            tag = f"[{i}/{total}] " if total > 1 else ""
            subj_note = f"{job.label}  " if many else ""
            try:
                outs = wait(pid, timeout=600 + 5 * len(a.post_combos or []),       # a combo sweep runs many palette steps
                            resubmit=lambda fresh, g=g: submit(fresh_copy(g) if fresh else g))
                files = collect(outs, job.dest, SERVER)
            except (RenderFailed, OSError) as e:
                failed += 1
                print(f"   ✗ {tag}{subj_note}seed={job.seed}: {e}")
                continue
            if a.post_combos:                     # one render, a picture per combination
                shots = sorted(f for f in files if "_sprite_" in f)
                for j, f in enumerate(shots, 1):
                    enlarge_output(a, f)
                    stamp_recipe(f, a, job.seed, job.subj, SERVER)
                    m = re.search(r"_v(\d{3})-", os.path.basename(f))
                    k = int(m.group(1)) if m else -1
                    lab = post_label(a.post_combos[k]) if 0 <= k < len(a.post_combos) else ""
                    print(f"   ✅ [{j}/{len(shots)}] {lab}  seed={job.seed}  ->  {f}")
                first_open = first_open or (shots[0] if shots else None)
                continue
            try:
                sprite = finish(job, files, SERVER)
            except Exception as e:                # post-processing broke on this one picture
                failed += 1
                print(f"   ✗ {tag}{subj_note}seed={job.seed}: couldn't finish the picture: {e}")
                continue
            preview = next((f for f in files if "_preview_" in f), None)
            tiled = next((f for f in files if "_tiled_" in f), None)
            first_open = first_open or tiled or preview or sprite   # open the seam check / preview if saved, else the sprite
            pal_note = f"pal={job.pal}  " if a.palette == "random" else ""
            print(f"   ✅ {tag}{subj_note}{pal_note}seed={job.seed}  ->  {sprite}")
            if tiled:
                print(f"      3x3 seam check  ->  {tiled}")

    where = ", ".join(sorted({str(x) for x in dests if x})) or f"{OUTPUT}/pixelmon/"
    print(f"   all done in {time.time() - t0:.1f}s  |  files in {where}")
    if first_open and not a.no_open:
        open_file(first_open)
    if failed:
        sys.exit(f"pixelmon: {failed} of {total} job(s) produced nothing (see above).")


def open_file(path):
    """Show a finished picture with the desktop's viewer (Linux, WSL, macOS, Windows). Never waits."""
    if sys.platform == "darwin":
        cmd = ["open"]
    elif os.name == "nt":
        cmd = ["explorer"]
    elif os.environ.get("WSL_DISTRO_NAME") and shutil.which("wslview"):
        cmd = ["wslview"]
    else:
        cmd = ["xdg-open"]
    if not shutil.which(cmd[0]):
        return
    try:
        subprocess.Popen(cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


TAKE_RE = r"_\d+x\d+_.+_s(\d+)_(?:sprite|art|raw)_\d+_?\.png$"


def skip_made(a, jobs):
    """--skip-existing: drop the takes whose files are already there. With --seed, a job whose seed has a file
    is skipped; with random seeds a key that already has k files needs k fewer takes."""
    have, keep, skipped = {}, [], 0
    for job in jobs:
        d = job.dest or os.path.join(OUTPUT, "pixelmon")
        nm = job.a.name if getattr(job.a, "_row_key", None) else (slug(job.subj) if a.batch else (a.name or slug(job.subj)))
        if (d, nm) not in have:
            rx = re.compile("^" + re.escape(nm) + TAKE_RE)
            seeds = []
            if os.path.isdir(d):
                for f in os.listdir(d):
                    m = rx.match(f)
                    if m:
                        seeds.append(int(m.group(1)))
            have[(d, nm)] = seeds
        seeds = have[(d, nm)]
        if a.seed >= 0:
            if job.seed in seeds:
                skipped += 1
                continue
        elif seeds:
            seeds.pop()
            skipped += 1
            continue
        keep.append(job)
    if skipped:
        print(f"   ⏭ {skipped} take(s) already made — skipped (--skip-existing)")
    return keep


def list_servers(a, jobs):
    """--list-servers: each render server's state, and whether it has what these jobs need."""
    c = C
    targets = POOL if a.server else [resolve_server(k) for k in SERVERS]
    names = {resolve_server(k): k for k in SERVERS}
    default = DEFAULT_SERVER or "local"
    print(f"{c['b']}{c['cyan']}Render servers{c['rst']}  {c['dim']}(servers.json; default: {default}){c['rst']}\n")
    graphs = []
    seen = set()
    for j in jobs:
        if id(j.a) not in seen:
            seen.add(id(j.a))
            graphs.append(j.graph(DRY))
    for url in dict.fromkeys(targets):
        alias = names.get(url, "")
        head = f"  {alias:<12} {_short(url):<24}"
        try:
            with urllib.request.urlopen(f"{url}/system_stats", timeout=4) as r:
                st = json.loads(r.read())
        except Exception as e:
            print(f"{head} {c['yel']}✗ unreachable{c['rst']} {c['dim']}({getattr(e, 'reason', e)}){c['rst']}")
            continue
        dev = (st.get("devices") or [{}])[0]
        gpu = re.sub(r"^\w+:\d+\s+", "", dev.get("name", "?").split(" : ")[0])   # "cuda:0 NVIDIA … : cudaMallocAsync"
        if dev.get("vram_total"):
            gpu += f" {dev['vram_total'] / 2**30:.0f} GB"
        flags = [f for f in ((st.get("system") or {}).get("argv") or []) if f in ("--lowvram", "--force-fp32", "--cpu", "--highvram")]
        try:
            with urllib.request.urlopen(f"{url}/queue", timeout=4) as r:
                q = json.loads(r.read())
            queue = f"queue {len(q.get('queue_running', []))} running, {len(q.get('queue_pending', []))} waiting"
        except Exception:
            queue = "queue ?"
        probs = list(dict.fromkeys(x for g in graphs for x in graph_problems(g, url)))
        state = f"{c['grn']}✓ ready{c['rst']}" if not probs else f"{c['yel']}⚠ can't render this{c['rst']}"
        print(f"{head} {state}  {c['dim']}{gpu}{' ' + ' '.join(flags) if flags else ''} · {queue}{c['rst']}")
        for x in probs:
            print(f"      • {x}")
        if probs:
            print("\n".join("   " + h for h in provision_hint(probs, url).splitlines()))


if __name__ == "__main__":
    import atexit
    import signal
    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGTERM, _on_stop)
    # Any other way out with prompts still queued (a fatal error, an exit mid-batch) clears them too.
    atexit.register(cancel_sent, "stopped early")
    main()
