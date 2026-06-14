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

# pixelmon can render on a REMOTE ComfyUI (e.g. a faster box on the LAN). Choose a
# target with `--server NAME` (an alias from servers.json) or `--server host[:port]`/URL,
# or the PIXELMON_SERVER env var. Default is local. When the target is remote, results
# are fetched back over HTTP (/view) — no shared filesystem needed. Actual resolution
# happens in main(); these module-level values are the local default.
SERVER = "http://127.0.0.1:8188"
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
try:
    with open(os.path.join(_SCRIPT_DIR, "servers.json"), encoding="utf-8") as _svf:
        SERVERS = {k: v for k, v in json.load(_svf).items() if not k.startswith("_")}
except Exception:
    SERVERS = {}
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
        ex('pixelmon "a spider" --style geometric', "sharp, angular style guide"),
        ex('pixelmon "a goblin" -n 8 --palette random', "8 variations, random palettes"),
        ex('pixelmon "a knight" --transparent --preview', "transparent + zoomed preview"),
        ex('pixelmon --batch "bat,skeleton,spider" -n 128', "128 of each → own folders"),
        ex('pixelmon "a bandit" --animate "smoke from cigar"', "looping animated GIF"),
        "",
        f"{c['b']}{c['cyan']}OPTIONS{c['rst']}",
        opt("prompt", "what to draw (in quotes)"),
        opt("-n, --number N", "how many to make, each a different seed", "1"),
        opt('--batch "a,b,c"', "round-robin subjects → a folder each (N of each)"),
        opt("--size N|WxH", "square N, or non-square WxH e.g. 32x48", "128"),
        opt("--palette NAME", "none / random / a name (--list-palettes)", "none"),
        opt("--style NAMES", "append proven style guide(s) — see --list-styles"),
        opt("--transparent", "cut out background -> transparent PNG"),
        opt("--dither", "Floyd-Steinberg dithering (faked shading)"),
        opt("--snap-pixels", "snap to a perfect grid (pixel-snapper) — extra crisp"),
        opt("--fast", "LCM mode: ~5x faster, slightly softer"),
        opt("--seed N", "lock / repeat a result (re-run a favorite)", "random"),
        opt("--steps N", "refinement steps (more = slower)", "25"),
        opt("--cfg N", "prompt adherence (higher = stricter)", "7"),
        opt("--lora-strength N", "how strongly to pixelate", "1.0"),
        opt("--bg-tolerance N", "bg color match for --transparent", "16"),
        opt('--custom-hex "..."', "colors for --palette Custom"),
        opt("--list-palettes", "show every palette name"),
        opt("--list-styles", "show every style guide"),
        opt("-h, --help", "show this help"),
        "",
        f"{c['b']}{c['cyan']}ADVANCED{c['rst']}",
        opt("--server NAMES", "render on a remote ComfyUI (alias/host/URL); comma-list = render farm across GPUs", "local"),
        opt("--smooth MODE", "pre-downscale flatten: mode / median / none", "mode"),
        opt("--filter MODE", "downscale: nearest (crisp) / box (soft)", "nearest"),
        opt("--preview", "also save an enlarged zoomed-in PNG"),
        opt("--view-scale N", "enlarge factor for --preview", "8"),
        opt('--negative "..."', "negative prompt (what to avoid)"),
        opt("--name NAME", "output filename base", "from prompt"),
        opt("--res N", "SDXL generation resolution", "1024"),
        opt("--sampler NAME", "ksampler sampler", "euler / lcm"),
        opt("--base FILE", "SDXL checkpoint", "sd_xl_base_1.0"),
        opt("--lora FILE", "pixel-art LoRA", "pixel-art-xl"),
        opt("--lcm-lora FILE", "LCM LoRA (used with --fast)", "lcm-lora-sdxl"),
        opt("--no-lora", "base model only (skip pixel LoRA)"),
        opt("--steer DIR", "steer toward a folder of reference images (IPAdapter)"),
        opt("--steer-strength N", "how strongly the refs influence the result", "0.7"),
        opt("--no-open", "don't auto-open the result"),
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


def build_graph(a, seed, palette=None, subject=None, server=None):
    palette = palette or a.palette
    subject = subject if subject is not None else a.prompt
    # The Pixel Art XL LoRA does the heavy lifting; the base prompt stays simple
    # and --style snippets (a.style_add) do the steering. "game sprite" keeps it
    # clean. Style negatives (a.style_neg) push away unwanted shapes/looks.
    parts = [f"pixel, a {subject}"]
    if a.style_add:
        parts.append(a.style_add)
    parts.append("game sprite, simple flat colors, solid background")
    prompt = ", ".join(parts)
    negative = a.negative + ((", " + a.style_neg) if a.style_neg else "")

    name = slug(subject) if a.batch else (a.name or slug(subject))
    # seed in the filename so each variation is identifiable and re-runnable.
    prefix = f"pixelmon/{name}_{a.sw}x{a.sh}_{palette}_s{seed}"

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
        "10": {"class_type": "PixelArtPalette",
               "inputs": {"image": ["8", 0], "downscale_to": max(a.sw, a.sh), "palette": palette,
                          "dithering": "floyd-steinberg" if a.dither else "none",
                          "downscale_filter": a.filter, "smooth": a.smooth,
                          "view_scale": a.view_scale, "custom_hex": a.custom_hex,
                          "transparent_bg": a.transparent, "bg_tolerance": a.bg_tolerance,
                          "snap_pixels": a.snap_pixels, "snap_colors": a.snap_colors,
                          "out_width": a.sw, "out_height": a.sh}},
        "11": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": prefix + "_sprite", "images": ["10", 0]}},
    }
    if a.preview:  # enlarged zoomed-in copy — opt-in; default saves only the true sprite
        g["12"] = {"class_type": "SaveImage",
                   "inputs": {"filename_prefix": prefix + "_preview", "images": ["10", 1]}}

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
                              "combine_embeds": a.steer_combine, "start_at": 0.0,
                              "end_at": 1.0, "embeds_scaling": "V only"}}
        model_src = ["22", 0]

    g["6"]["inputs"]["clip"] = clip_src
    g["7"]["inputs"]["clip"] = clip_src
    g["3"]["inputs"]["model"] = model_src
    return g


def submit(graph, server=None):
    server = server or SERVER
    data = json.dumps({"prompt": graph}).encode()
    req = urllib.request.Request(f"{server}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        sys.exit("ComfyUI rejected the request:\n" + e.read().decode()[:1200])
    except urllib.error.URLError:
        sys.exit("Couldn't reach ComfyUI at " + server + " — is the server running?")


_STEER_UPLOADS = {}   # server -> [uploaded reference filenames] (uploaded once per server)


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


def _upload_image(path, server):
    """POST one image to ComfyUI's /upload/image (so LoadImage can reference it). Returns the server-side name."""
    import mimetypes, hashlib
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
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return resp.get("name", name)
    except urllib.error.HTTPError as e:
        sys.exit("--steer upload failed: " + e.read().decode()[:500])
    except urllib.error.URLError:
        sys.exit("--steer: couldn't reach " + server + " to upload references.")


def steer_files(a, server):
    """Reference filenames available on `server` (uploads once per server, then caches)."""
    paths = getattr(a, "_steer_paths", None)
    if paths is None:
        paths = a._steer_paths = _gather_steer_paths(a.steer, a.steer_max)
    if server not in _STEER_UPLOADS:
        _STEER_UPLOADS[server] = [_upload_image(p, server) for p in paths]
        print(f"   \U0001f9ed steer: {len(_STEER_UPLOADS[server])} reference(s) -> {_short(server)}")
    return _STEER_UPLOADS[server]


def wait(pid, server=None, timeout=600):
    server = server or SERVER
    for _ in range(timeout):
        with urllib.request.urlopen(f"{server}/history/{pid}", timeout=30) as r:
            hist = json.loads(r.read())
        if pid in hist and hist[pid].get("outputs"):
            return hist[pid]["outputs"]
        time.sleep(1)
    sys.exit("Timed out waiting for the image.")


def poll(pid, server):
    """One non-blocking /history check; returns the outputs dict, or None if not ready."""
    with urllib.request.urlopen(f"{server}/history/{pid}", timeout=30) as r:
        hist = json.loads(r.read())
    if pid in hist and hist[pid].get("outputs"):
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


def run_farm(a, work):
    """Render farm: distribute jobs across POOL with dynamic dispatch (feed the free GPU).
    Faster boxes naturally pull more jobs; results are fetched back from whichever GPU made them."""
    live = [s for s in POOL if server_up(s)]
    down = [s for s in POOL if s not in live]
    if down:
        print(f"   ⚠ skipping unreachable: {', '.join(_short(s) for s in down)}")
    if not live:
        sys.exit("render farm: no reachable servers in the pool.")
    print(f"   \U0001f69c render farm: {len(live)} GPU(s) — {', '.join(_short(s) for s in live)}")
    pending = list(work)        # (subject, seed, palette, dest)
    inflight = {}               # server -> (subject, seed, palette, dest, pid)
    total = len(work)
    done = 0

    def launch(srv):
        """Submit the next pending job to srv. False = server unusable (drop it)."""
        while pending:
            subj, seed, pal, d = pending.pop(0)
            try:
                pid = submit(build_graph(a, seed, pal, subject=subj, server=srv), srv)
            except SystemExit:
                pending.insert(0, (subj, seed, pal, d))   # couldn't submit; keep the job
                return False
            inflight[srv] = (subj, seed, pal, d, pid)
            return True
        return True             # nothing left to do

    for srv in list(live):
        if launch(srv) is False:
            live.remove(srv)

    while inflight:
        advanced = False
        for srv, (subj, seed, pal, d, pid) in list(inflight.items()):
            try:
                outs = poll(pid, srv)
            except Exception:
                print(f"   ⚠ {_short(srv)} unreachable — requeueing its job")
                pending.append((subj, seed, pal, d))
                del inflight[srv]
                advanced = True
                continue
            if outs is None:
                continue
            advanced = True
            imgs = [im for node in outs.values() for im in node.get("images", [])]
            dest_dir = d or os.path.join(OUTPUT, "pixelmon")
            files = [fetch_image(im, dest_dir, srv) for im in imgs]
            sprite = next((f for f in files if "_sprite_" in f), None)
            done += 1
            sj = f"{subj}  " if a.batch else ""
            print(f"   ✅ [{done}/{total}] {_short(srv):<20} {sj}seed={seed}  ->  {sprite}")
            del inflight[srv]
            launch(srv)          # feed the now-free GPU its next job
        if not advanced:
            time.sleep(1)

    if pending:
        print(f"   ⚠ {len(pending)} job(s) left undone (all GPUs dropped).")


def main():
    # add_help=False so we can render our own friendly, colorized help instead
    # of argparse's plain default (shown via print_help on -h or no args).
    p = argparse.ArgumentParser(prog="pixelmon", add_help=False)
    p.add_argument("-h", "--help", action="store_true", dest="show_help")
    p.add_argument("prompt", nargs="?", help='what to draw, e.g. "a goblin warrior"')
    p.add_argument("--size", default="128",
                   help="sprite size: N (square) or WxH, e.g. 32x48. default 128")
    p.add_argument("-n", "--number", type=int, default=1,
                   help="how many to generate, each with a different seed. default 1 "
                        "(with --batch: how many of EACH subject)")
    p.add_argument("--batch", default=None, metavar="SUBJECTS",
                   help='comma-separated subjects to round-robin, one of each per pass '
                        '(e.g. --batch "bat,skeleton,spider"); each goes to its own folder')
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
    p.add_argument("--dither", action="store_true", help="Floyd-Steinberg dithering")
    p.add_argument("--snap-pixels", dest="snap_pixels", action="store_true",
                   help="snap to a perfect pixel grid via the pixel-snapper (auto-detects size; "
                        "extra crisp). overrides --size.")
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
    p.add_argument("--negative", default="3d render, realistic, photograph, blurry, "
                   "smooth gradient, antialiased, jpeg artifacts, text, watermark, signature")
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
                        "GPUs (e.g. 'rtx,titan,local'). default: local (also honors $PIXELMON_SERVER)")
    p.add_argument("--base", default="sd_xl_base_1.0.safetensors", help="SDXL base checkpoint")
    p.add_argument("--lora", default="pixel-art-xl.safetensors", help="pixel-art LoRA")
    # --- steering: nudge output toward a folder of reference images (IPAdapter) ---
    p.add_argument("--steer", default=None, metavar="DIR|IMG",
                   help="steer output toward reference image(s) via IPAdapter (a folder, or one image)")
    p.add_argument("--steer-strength", dest="steer_strength", type=float, default=0.7,
                   help="IPAdapter weight: 0=off .. ~1 strong (default 0.7)")
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
    p.add_argument("--sampler", default=None,
                   help="ksampler sampler_name (default euler, or lcm with --fast)")
    p.add_argument("--no-lora", dest="no_lora", action="store_true",
                   help="generate from the base model only (no pixel-art LoRA)")
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
    a = p.parse_args()

    # Resolve the render target: --server (alias/URL) > $PIXELMON_SERVER > local default.
    # Sets the module globals used by submit() / wait() / fetch_image().
    global SERVER, REMOTE, POOL
    _target = a.server or os.environ.get("PIXELMON_SERVER")
    if _target:
        POOL = [resolve_server(s.strip()) for s in _target.split(",") if s.strip()]
        SERVER = POOL[0]                       # first entry is the single-server default
        REMOTE = not any(h in SERVER for h in ("127.0.0.1", "localhost", "[::1]"))

    # Friendly help on -h/--help, or when run with no prompt at all.
    if a.show_help or (not a.prompt and not a.batch and not a.list_palettes and not a.list_styles):
        print_help()
        return
    if a.list_palettes:
        print_palettes()
        return
    if a.list_styles:
        print_styles()
        return
    if a.palette not in ("none", "random", "Custom") and a.palette not in PALETTES:
        p.error(f"unknown palette {a.palette!r}. See --list-palettes.")

    # Resolve --style guide(s) into prompt/negative additions (used by build_graph).
    a.style_add, a.style_neg = "", ""
    if a.style:
        names = [s.strip() for s in a.style.replace(",", " ").split() if s.strip()]
        adds, negs = [], []
        for nm in names:
            if nm not in STYLES:
                p.error(f"unknown style {nm!r}. See --list-styles.")
            adds.append(STYLES[nm].get("prompt", ""))
            if STYLES[nm].get("negative"):
                negs.append(STYLES[nm]["negative"])
        a.style_add = ", ".join(x for x in adds if x)
        a.style_neg = ", ".join(negs)

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

    def _r64(v):
        return max(64, int(round(v / 64.0)) * 64)
    a.gen_w = a.res if a.sw >= a.sh else _r64(a.res * a.sw / a.sh)
    a.gen_h = a.res if a.sh >= a.sw else _r64(a.res * a.sh / a.sw)

    # Animation mode is its own pipeline (base -> mask -> inpaint frames -> GIF).
    if a.animate:
        sys.path.insert(0, _SCRIPT_DIR)
        import animate
        animate.run(a, pal_map=(_pal.ALL_PALETTES if _pal else None), slug=slug)
        return

    n = max(1, a.number)
    # Subjects: one (the prompt) or many (--batch round-robins one of each per pass).
    subjects = [s.strip() for s in a.batch.split(",") if s.strip()] if a.batch else [a.prompt]

    # Where finished files go — relative to the directory you ran pixelmon FROM:
    #   --batch        -> one folder per subject  (./<subject>/)
    #   --move-to-dirs -> one folder named after the prompt  (./<prompt>/)
    #   --output-to D  -> that folder D (flat); else left in ComfyUI's output.
    base = os.path.abspath(os.path.expanduser(a.output_to)) if a.output_to else os.getcwd()

    def dest_for(subject):
        if a.no_subdirs:
            # flatten: everything into the one base folder (files are uniquely named,
            # so you can browse them all in a single dir)
            d = base if (a.output_to or a.batch or a.move_to_dirs) else None
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
        if not os.path.isdir(d):
            if a.create_dirs or a.move_to_dirs or a.batch:
                os.makedirs(d, exist_ok=True)
            else:
                p.error(f"output dir does not exist: {d}\n  (add --create-dirs to make it)")
        return d

    dests = {subj: dest_for(subj) for subj in subjects}

    total = n * len(subjects)
    per = 20 if a.fast else 100  # rough seconds/image for the ETA
    pal_label = "random" if a.palette == "random" else a.palette
    style_label = f"  |  style: {a.style}" if a.style else ""
    subj_label = f"{len(subjects)} subjects: {', '.join(subjects)}" if a.batch else repr(a.prompt)
    count_label = f"{n} each = {total} total" if a.batch else f"{n} image(s)"
    size_label = f"{a.sw}x{a.sh}" if a.sw != a.sh else f"{a.sw}px"
    if len(POOL) > 1:
        print(f"🚜 render farm: {len(POOL)} servers — {', '.join(_short(s) for s in POOL)}")
    elif REMOTE:
        print(f"🌐 rendering on remote server {SERVER} (results fetched back here)")
    print(f"🎨 {subj_label}  |  {size_label}  |  {pal_label}"
          f"{' |  transparent' if a.transparent else ''}{style_label}  |  "
          f"{'FAST/LCM' if a.fast else 'quality'} {a.steps}st  |  {count_label}")
    if total > 1:
        eta = total * per
        tip = "" if a.fast else "  (tip: add --fast for quick variations)"
        print(f"   ~{eta // 60}m{eta % 60:02d}s estimated{tip}")

    # Build the work list. --batch round-robins (one of each subject per pass) so
    # every folder fills evenly instead of finishing one subject at a time.
    work = []  # (subject, seed, palette, dest)
    k = 0
    for _ in range(n):
        for subj in subjects:
            seed = (a.seed + k) if a.seed >= 0 else random.randint(0, 2**31 - 1)
            pal = random.choice(PALETTES) if a.palette == "random" else a.palette
            work.append((subj, seed, pal, dests[subj]))
            k += 1

    t0 = time.time()
    first_open = None
    if len(POOL) > 1:
        # Render farm: fan the whole work list out across all the GPUs in the pool.
        run_farm(a, work)
    else:
        # Single server: queue everything up front; ComfyUI runs them one at a time.
        jobs = [(subj, seed, pal, d, submit(build_graph(a, seed, pal, subject=subj, server=SERVER)))
                for (subj, seed, pal, d) in work]
        if total > 1:
            print(f"   queued {total} jobs; generating...")
        for i, (subj, seed, pal, d, pid) in enumerate(jobs, 1):
            outs = wait(pid)
            imgs = [im for node in outs.values() for im in node.get("images", [])]
            if REMOTE:
                # Files live on the remote server's disk — download them here over HTTP.
                dest_dir = d or os.path.join(OUTPUT, "pixelmon")
                files = [fetch_image(im, dest_dir) for im in imgs]
            else:
                files = [os.path.join(OUTPUT, im.get("subfolder", ""), im["filename"]) for im in imgs]
                if d:  # move finished files out of ComfyUI's output into the target folder
                    moved = []
                    for f in files:
                        if os.path.exists(f):
                            tgt = os.path.join(d, os.path.basename(f))
                            shutil.move(f, tgt)
                            moved.append(tgt)
                    files = moved
            sprite = next((f for f in files if "_sprite_" in f), None)
            preview = next((f for f in files if "_preview_" in f), None)
            first_open = first_open or preview or sprite   # open preview if saved, else the sprite
            tag = f"[{i}/{total}] " if total > 1 else ""
            subj_note = f"{subj}  " if a.batch else ""
            pal_note = f"pal={pal}  " if a.palette == "random" else ""
            print(f"   ✅ {tag}{subj_note}{pal_note}seed={seed}  ->  {sprite}")

    where = ", ".join(sorted({str(x) for x in dests.values() if x})) or f"{OUTPUT}/pixelmon/"
    print(f"   all done in {time.time() - t0:.1f}s  |  files in {where}")
    if first_open and not a.no_open and shutil.which("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", first_open],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    main()
