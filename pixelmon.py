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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVER = "http://127.0.0.1:8188"
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
    PALETTES = ["PICO-8", "Sweetie-16", "NES", "CGA-16", "CGA-4", "Game Boy DMG"]


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
        return f"  {c['yel']}{cmd:<45}{c['rst']}{c['dim']}{note}{c['rst']}"

    print("\n".join([
        f"{c['b']}{c['mag']}pixelmon{c['rst']} — generate pixel-art sprites from a text prompt",
        "",
        f"{c['b']}{c['cyan']}USAGE{c['rst']}",
        f"  {c['mag']}pixelmon{c['rst']} {c['yel']}\"a prompt\"{c['rst']} [options]",
        "",
        f"{c['b']}{c['cyan']}EXAMPLES{c['rst']}",
        ex('pixelmon "a fierce dragon"', "best quality (the default)"),
        ex('pixelmon "a cute slime" --palette PICO-8', "lock to a palette"),
        ex('pixelmon "a goblin" -n 8 --fast', "8 quick variations"),
        ex('pixelmon "a knight" --size 32 --transparent', "tiny + transparent bg"),
        "",
        f"{c['b']}{c['cyan']}OPTIONS{c['rst']}",
        opt("prompt", "what to draw (in quotes)"),
        opt("-n, --number N", "how many to make, each a different seed", "1"),
        opt("--size N", "sprite size in px: 16 / 32 / 64 / 128", "128"),
        opt("--palette NAME", "none = model's own colors, or a named palette", "none"),
        opt("--transparent", "cut out background -> transparent PNG"),
        opt("--dither", "Floyd-Steinberg dithering (faked shading)"),
        opt("--fast", "LCM mode: ~5x faster, slightly softer"),
        opt("--seed N", "lock / repeat a result (re-run a favorite)", "random"),
        opt("--steps N", "refinement steps (more = slower)", "25"),
        opt("--cfg N", "prompt adherence (higher = stricter)", "7"),
        opt("--lora-strength N", "how strongly to pixelate", "1.0"),
        opt("--bg-tolerance N", "bg color match for --transparent", "16"),
        opt('--custom-hex "..."', "colors for --palette Custom"),
        opt("--list-palettes", "show every palette name"),
        opt("-h, --help", "show this help"),
        "",
        f"{c['b']}{c['cyan']}ADVANCED{c['rst']}",
        opt("--smooth MODE", "pre-downscale flatten: mode / median / none", "mode"),
        opt("--filter MODE", "downscale: nearest (crisp) / box (soft)", "nearest"),
        opt("--view-scale N", "how much to enlarge the preview", "8"),
        opt('--negative "..."', "negative prompt (what to avoid)"),
        opt("--name NAME", "output filename base", "from prompt"),
        opt("--res N", "SDXL generation resolution", "1024"),
        opt("--sampler NAME", "ksampler sampler", "euler / lcm"),
        opt("--base FILE", "SDXL checkpoint", "sd_xl_base_1.0"),
        opt("--lora FILE", "pixel-art LoRA", "pixel-art-xl"),
        opt("--lcm-lora FILE", "LCM LoRA (used with --fast)", "lcm-lora-sdxl"),
        opt("--no-lora", "base model only (skip pixel LoRA)"),
        opt("--no-open", "don't auto-open the preview"),
        "",
        f"{c['b']}{c['cyan']}OUTPUT{c['rst']}",
        f"  {c['dim']}{OUTPUT}/pixelmon/{c['rst']}",
        f"  {c['dim']}true-size _sprite_ PNG  +  enlarged _preview_ PNG{c['rst']}",
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


def slug(text):
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out[:40] or "monster"


def build_graph(a, seed):
    # The Pixel Art XL LoRA does the real work of making the image pixel-shaped;
    # the prompt just needs the subject plus a light nudge. Keep it simple — the
    # over-stuffed prompts that a generic model needed actually hurt here.
    prompt = f"pixel, a {a.prompt}, simple flat colors, solid background"
    name = a.name or slug(a.prompt)
    # seed in the filename so each variation is identifiable and re-runnable.
    prefix = f"pixelmon/{name}_{a.size}_{a.palette}_s{seed}"
    res = a.res

    g = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": a.base}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": res, "height": res, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": None, "text": prompt}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": None, "text": a.negative}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": a.steps, "cfg": a.cfg,
                         "sampler_name": a.sampler, "scheduler": a.scheduler, "denoise": 1.0,
                         "model": None, "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "10": {"class_type": "PixelArtPalette",
               "inputs": {"image": ["8", 0], "downscale_to": a.size, "palette": a.palette,
                          "dithering": "floyd-steinberg" if a.dither else "none",
                          "downscale_filter": a.filter, "smooth": a.smooth,
                          "view_scale": a.view_scale, "custom_hex": a.custom_hex,
                          "transparent_bg": a.transparent, "bg_tolerance": a.bg_tolerance}},
        "11": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": prefix + "_sprite", "images": ["10", 0]}},
        "12": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": prefix + "_preview", "images": ["10", 1]}},
    }

    # Chain LoRAs onto the base: SDXL -> [Pixel Art XL] -> [LCM if --fast].
    # Each LoraLoader patches both the model and the text encoder (clip), so we
    # thread the "current" source through and wire the sampler/prompts to the end.
    model_src, clip_src = ["4", 0], ["4", 1]
    if not a.no_lora:
        g["15"] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": clip_src, "lora_name": a.lora,
                              "strength_model": a.lora_strength, "strength_clip": a.lora_strength}}
        model_src, clip_src = ["15", 0], ["15", 1]
    if a.fast:
        g["16"] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": clip_src, "lora_name": a.lcm_lora,
                              "strength_model": 1.0, "strength_clip": 1.0}}
        model_src, clip_src = ["16", 0], ["16", 1]

    g["6"]["inputs"]["clip"] = clip_src
    g["7"]["inputs"]["clip"] = clip_src
    g["3"]["inputs"]["model"] = model_src
    return g


def submit(graph):
    data = json.dumps({"prompt": graph}).encode()
    req = urllib.request.Request(f"{SERVER}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        sys.exit("ComfyUI rejected the request:\n" + e.read().decode()[:1200])
    except urllib.error.URLError:
        sys.exit("Couldn't reach ComfyUI at " + SERVER + " — is the server running?")


def wait(pid, timeout=600):
    for _ in range(timeout):
        with urllib.request.urlopen(f"{SERVER}/history/{pid}", timeout=30) as r:
            hist = json.loads(r.read())
        if pid in hist and hist[pid].get("outputs"):
            return hist[pid]["outputs"]
        time.sleep(1)
    sys.exit("Timed out waiting for the image.")


def main():
    # add_help=False so we can render our own friendly, colorized help instead
    # of argparse's plain default (shown via print_help on -h or no args).
    p = argparse.ArgumentParser(prog="pixelmon", add_help=False)
    p.add_argument("-h", "--help", action="store_true", dest="show_help")
    p.add_argument("prompt", nargs="?", help='what to draw, e.g. "a goblin warrior"')
    p.add_argument("--size", type=int, default=128,
                   help="sprite size in px (16/32/64/128). default 128 (sharpest)")
    p.add_argument("-n", "--number", type=int, default=1,
                   help="how many to generate, each with a different seed. default 1")
    p.add_argument("--transparent", action="store_true",
                   help="cut out the background -> transparent PNG (great for game sprites)")
    p.add_argument("--bg-tolerance", dest="bg_tolerance", type=int, default=16,
                   help="how aggressively to match the background color for --transparent. default 16")
    p.add_argument("--palette", default="none",
                   help="palette to lock to: 'none' = keep the model's own colors; "
                        "or PICO-8 / Sweetie-16 / NES / ... (see --list-palettes)")
    p.add_argument("--dither", action="store_true", help="Floyd-Steinberg dithering")
    p.add_argument("--smooth", choices=["mode", "median", "none"], default="mode",
                   help="flatten noise before downscaling (mode=cleanest). default mode")
    p.add_argument("--filter", choices=["box (area average)", "nearest"],
                   default="nearest", help="downscale: nearest (crisp) / box (soft). default nearest")
    p.add_argument("--steps", type=int, default=None,
                   help="sampling steps (default 25, or 8 with --fast)")
    p.add_argument("--cfg", type=float, default=None,
                   help="prompt strength (default 7, or 1.5 with --fast)")
    p.add_argument("--seed", type=int, default=-1, help="-1 = random each run")
    p.add_argument("--negative", default="3d render, realistic, photograph, blurry, "
                   "smooth gradient, antialiased, jpeg artifacts, text, watermark, signature")
    p.add_argument("--name", default=None, help="output filename base (default: from prompt)")
    p.add_argument("--custom-hex", dest="custom_hex", default="",
                   help='hex codes when --palette Custom, e.g. "#000 #fff #f00"')
    p.add_argument("--view-scale", dest="view_scale", type=int, default=8,
                   help="how much to enlarge the preview. default 8")
    # --- model / engine ---
    p.add_argument("--base", default="sd_xl_base_1.0.safetensors", help="SDXL base checkpoint")
    p.add_argument("--lora", default="pixel-art-xl.safetensors", help="pixel-art LoRA")
    p.add_argument("--lora-strength", dest="lora_strength", type=float, default=1.0,
                   help="LoRA strength. default 1.0 (try 1.2 for stronger pixelation)")
    p.add_argument("--res", type=int, default=1024, help="SDXL generation resolution. default 1024")
    p.add_argument("--sampler", default=None,
                   help="ksampler sampler_name (default euler, or lcm with --fast)")
    p.add_argument("--no-lora", dest="no_lora", action="store_true",
                   help="generate from the base model only (no pixel-art LoRA)")
    p.add_argument("--fast", action="store_true",
                   help="LCM mode: ~3-4x faster (8 steps); small quality trade-off")
    p.add_argument("--lcm-lora", dest="lcm_lora", default="lcm-lora-sdxl.safetensors",
                   help="LCM LoRA filename (used with --fast)")
    p.add_argument("--list-palettes", action="store_true", help="list palettes and exit")
    p.add_argument("--no-open", action="store_true", help="don't auto-open the preview")
    a = p.parse_args()

    # Friendly help on -h/--help, or when run with no prompt at all.
    if a.show_help or (not a.prompt and not a.list_palettes):
        print_help()
        return
    if a.list_palettes:
        print_palettes()
        return
    if a.palette not in ("none", "Custom") and a.palette not in PALETTES:
        p.error(f"unknown palette {a.palette!r}. See --list-palettes.")

    # Resolve sampler settings by mode. --fast = LCM (8 steps, low cfg, lcm
    # sampler + sgm_uniform schedule); default = full-quality 25-step euler.
    if a.fast:
        a.steps = 8 if a.steps is None else a.steps
        a.cfg = 1.5 if a.cfg is None else a.cfg
        a.sampler = "lcm" if a.sampler is None else a.sampler
        a.scheduler = "sgm_uniform"
    else:
        a.steps = 25 if a.steps is None else a.steps
        a.cfg = 7.0 if a.cfg is None else a.cfg
        a.sampler = "euler" if a.sampler is None else a.sampler
        a.scheduler = "normal"

    n = max(1, a.number)
    # Distinct seed per image so they're real variations. With an explicit
    # --seed we increment from it (reproducible); otherwise pick random seeds.
    if a.seed >= 0:
        seeds = [a.seed + i for i in range(n)]
    else:
        seeds = [random.randint(0, 2**31 - 1) for _ in range(n)]

    per = 20 if a.fast else 100  # rough seconds/image for the ETA
    print(f"🎨 {a.prompt!r}  |  {a.size}px  |  {a.palette}"
          f"{' |  transparent' if a.transparent else ''}  |  "
          f"{'FAST/LCM' if a.fast else 'quality'} {a.steps}st  |  {n} image(s)")
    if n > 1:
        eta = n * per
        tip = "" if a.fast else "  (tip: add --fast for quick variations)"
        print(f"   ~{eta // 60}m{eta % 60:02d}s estimated{tip}")

    # Queue every job up front; ComfyUI runs them one at a time, we collect in order.
    jobs = [(s, submit(build_graph(a, s))) for s in seeds]
    if n > 1:
        print(f"   queued {n} jobs; generating...")

    t0 = time.time()
    first_preview = None
    for i, (s, pid) in enumerate(jobs, 1):
        outs = wait(pid)
        files = [os.path.join(OUTPUT, im.get("subfolder", ""), im["filename"])
                 for node in outs.values() for im in node.get("images", [])]
        sprite = next((f for f in files if "_sprite_" in f), None)
        preview = next((f for f in files if "_preview_" in f), None)
        first_preview = first_preview or preview
        tag = f"[{i}/{n}] " if n > 1 else ""
        print(f"   ✅ {tag}seed={s}  ->  {sprite}")

    print(f"   all done in {time.time() - t0:.1f}s  |  files in {OUTPUT}/pixelmon/")
    if first_preview and not a.no_open and shutil.which("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", first_preview],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    main()
