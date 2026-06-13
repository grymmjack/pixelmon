#!/usr/bin/env python3
"""
animate.py — looping portrait-gesture animation for pixelmon (à la 1988 Wasteland).

The mental model: a gesture is just (region, change-prompt, frames, timing).
We generate ONE base portrait, then re-paint only a small masked REGION across a
few frames so the rest of the portrait is frozen — exactly how the EGA artists
hand-animated a blink or a puff of smoke. All frames pass through the SAME palette
and only the masked pixels are composited back, so there is ZERO flicker outside
the region.

Pipeline:  base (txt2img) -> auto-mask region (CLIPSeg, text-prompted, works on
dogs/monsters too) -> inpaint N variant frames -> downscale + palette-quantize all
-> composite only the masked pixels onto the base -> assemble a looping GIF.

Called by pixelmon.py when --animate is passed. Talks to the same ComfyUI server.
"""
import json, os, shutil, sys, time, urllib.error, urllib.request
import numpy as np
from PIL import Image

SERVER = "http://127.0.0.1:8188"
COMFY  = os.path.expanduser("~/ComfyUI")
OUTPUT = os.path.join(COMFY, "output")
INPUT  = os.path.join(COMFY, "input")

# Gesture presets: ready-made (region, change-prompt, frames, loop). Any CLI flag
# overrides the preset, and --animate "free text" skips presets entirely.
GESTURES = {
    "blink": {"region": "the eyes", "change": "eyes closed, smooth eyelids, peaceful sleeping face",
              "frames": 2, "loop": "once-return", "denoise": 0.45},
    "talk":  {"region": "the mouth", "change": "mouth open speaking, teeth visible",
              "frames": 3, "loop": "pingpong"},
    "glow":  {"region": "the eyes", "change": "bright glowing eyes, intense inner light",
              "frames": 3, "loop": "pingpong"},
    "smoke": {"region": "the cigar or cigarette", "change": "wisp of smoke rising, curling smoke trail",
              "frames": 4, "loop": "cycle"},
    "breathe": {"region": "the chest and shoulders", "change": "shoulders raised, chest expanded, inhaling",
                "frames": 3, "loop": "pingpong"},
}

# tiny keyword->region guesser for free-text gestures when --anim-region is omitted
_REGION_HINTS = [
    (("blink", "eye", "wink"), "the eyes"),
    (("lick", "chops", "mouth", "talk", "speak", "smile", "teeth", "tongue", "jaw"), "the mouth"),
    (("smoke", "cigar", "cigarette", "pipe"), "the cigar or cigarette"),
    (("gun", "rifle", "pistol", "weapon", "rub"), "the gun in the hands"),
    (("breath", "chest", "shoulder"), "the chest and shoulders"),
]


# --------------------------------------------------------------------------- API
def _submit(graph):
    data = json.dumps({"prompt": graph}).encode()
    req = urllib.request.Request(f"{SERVER}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        sys.exit("ComfyUI rejected the animation request:\n" + e.read().decode()[:1500])
    except urllib.error.URLError:
        sys.exit("Couldn't reach ComfyUI — is the server running?")


def _wait(pid, timeout=600):
    for _ in range(timeout):
        with urllib.request.urlopen(f"{SERVER}/history/{pid}", timeout=30) as r:
            hist = json.loads(r.read())
        if pid in hist and hist[pid].get("outputs"):
            return hist[pid]["outputs"]
        time.sleep(1)
    sys.exit("Timed out waiting for ComfyUI.")


def _result_image(outputs):
    for node in outputs.values():
        for im in node.get("images", []):
            path = os.path.join(OUTPUT, im.get("subfolder", ""), im["filename"])
            img = Image.open(path).convert("RGB")
            img.load()
            return img
    sys.exit("No image returned by ComfyUI.")


def _lora_nodes(a):
    """SDXL base -> (optional) the chosen LoRA. returns (model_src, clip_src, graph)."""
    g = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": a.base}}}
    model_src, clip_src = ["4", 0], ["4", 1]
    if not getattr(a, "no_lora", False):
        g["15"] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": clip_src, "lora_name": a.lora,
                              "strength_model": a.lora_strength, "strength_clip": a.lora_strength}}
        model_src, clip_src = ["15", 0], ["15", 1]
    return model_src, clip_src, g


def _prompts(a, subject, extra=""):
    """Portrait-framed prompt for animation. Unlike pixelmon's *sprite* prompt
    ("game sprite, ...") which tiles into a sheet at high res, this forces ONE
    centered bust and negative-prompts away grids/sheets/duplicates."""
    parts = [f"a {subject}"]
    if a.style_add:
        parts.append(a.style_add)
    if extra:
        parts.append(extra)
    parts.append("single character portrait, one face, centered, head and shoulders, solid background")
    pos = ", ".join(parts)
    neg = a.negative + ((", " + a.style_neg) if a.style_neg else "")
    neg += (", multiple characters, many faces, grid, sprite sheet, contact sheet, "
            "collage, tiled, panels, frame border, duplicate, full body")
    return pos, neg


def _gen_base(a, subject, seed, w, h):
    model_src, clip_src, g = _lora_nodes(a)
    pos, neg = _prompts(a, subject)
    g.update({
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": pos}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": neg}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": a.steps, "cfg": a.cfg,
                         "sampler_name": a.sampler, "scheduler": a.scheduler, "denoise": 1.0,
                         "model": model_src, "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pixelmon_anim/base", "images": ["8", 0]}},
    })
    return _result_image(_wait(_submit(g)))


def _inpaint(a, base_img, mask_img, subject, change, seed, denoise):
    base_img.save(os.path.join(INPUT, "pm_anim_base.png"))
    # LoadImageMask reads a channel; save mask as white-on-black RGB
    mask_img.convert("RGB").save(os.path.join(INPUT, "pm_anim_mask.png"))
    model_src, clip_src, g = _lora_nodes(a)
    pos, neg = _prompts(a, subject, extra=change)
    g.update({
        "20": {"class_type": "LoadImage", "inputs": {"image": "pm_anim_base.png"}},
        "21": {"class_type": "LoadImageMask", "inputs": {"image": "pm_anim_mask.png", "channel": "red"}},
        "22": {"class_type": "VAEEncode", "inputs": {"pixels": ["20", 0], "vae": ["4", 2]}},
        "23": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["22", 0], "mask": ["21", 0]}},
        "6":  {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": pos}},
        "7":  {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": neg}},
        "3":  {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": a.steps, "cfg": a.cfg,
                          "sampler_name": a.sampler, "scheduler": a.scheduler, "denoise": denoise,
                          "model": model_src, "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["23", 0]}},
        "8":  {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9":  {"class_type": "SaveImage", "inputs": {"filename_prefix": "pixelmon_anim/frame", "images": ["8", 0]}},
    })
    return _result_image(_wait(_submit(g)))


# ----------------------------------------------------------------- auto-masking
_CLIPSEG = {}


def _clipseg_mask(image, region_text, thresh=0.40, grow_frac=0.015):
    """Text-prompted segmentation -> binary mask (PIL 'L'), same size as image.
    Runs on CPU (small model) to avoid contending with the GPU."""
    import torch
    from transformers import CLIPSegProcessor, CLIPSegForImageSegmentation
    if "m" not in _CLIPSEG:
        name = "CIDAS/clipseg-rd64-refined"
        _CLIPSEG["p"] = CLIPSegProcessor.from_pretrained(name)
        _CLIPSEG["m"] = CLIPSegForImageSegmentation.from_pretrained(name).eval()
    proc, mdl = _CLIPSEG["p"], _CLIPSEG["m"]
    inp = proc(text=[region_text], images=[image], return_tensors="pt")
    with torch.no_grad():
        logits = mdl(**inp).logits
    pred = torch.sigmoid(logits).squeeze().cpu().numpy().astype(np.float32)
    rng = pred.max() - pred.min()
    pred = (pred - pred.min()) / (rng + 1e-6)
    m = (pred >= thresh).astype(np.uint8)
    # grow the mask a little so the inpaint has margin to blend
    grow = max(1, int(grow_frac * max(image.size)))
    try:
        from scipy.ndimage import binary_dilation
        m = binary_dilation(m, iterations=grow).astype(np.uint8)
    except Exception:
        pass
    mask = Image.fromarray(m * 255).resize(image.size, Image.NEAREST)
    return mask


def _box_mask(image, box):
    l, t, r, b = box
    W, H = image.size
    m = Image.new("L", (W, H), 0)
    from PIL import ImageDraw
    ImageDraw.Draw(m).rectangle([int(l * W), int(t * H), int(r * W), int(b * H)], fill=255)
    return m


def _mask_coverage(mask):
    a = np.asarray(mask) > 127
    return a.mean()


# --------------------------------------------------------------- palette / GIF
def _to_rgb(c):
    """Normalize a palette color to (r,g,b) — accepts (r,g,b) tuples or hex strings."""
    if isinstance(c, (tuple, list)) and len(c) >= 3:
        return (int(c[0]), int(c[1]), int(c[2]))
    if isinstance(c, str):
        s = c.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    raise ValueError(f"unrecognized palette color: {c!r}")


def _quantize(img, colors):
    """Nearest-color map to a palette (tuples or hex strings). colors None -> unchanged."""
    if not colors:
        return img.convert("RGB")
    arr = np.asarray(img.convert("RGB"), dtype=np.int32)
    pal = np.array([_to_rgb(c) for c in colors], dtype=np.int32)
    d = ((arr[:, :, None, :] - pal[None, None, :, :]) ** 2).sum(-1)
    idx = d.argmin(-1)
    return Image.fromarray(pal[idx].astype(np.uint8), "RGB")


def _sequence(n_variants, loop):
    """Order of frame indices (0 = resting base, 1..n = variants)."""
    V = list(range(1, n_variants + 1))
    if loop == "cycle":
        return [0] + V
    if loop == "once-return":
        return [0] + V + [0]
    # pingpong
    return [0] + V + V[-2::-1] if n_variants > 1 else [0] + V


# -------------------------------------------------------------------- the engine
def run(a, pal_map=None, slug=lambda s: s):
    subject = a.prompt or "character"

    # 1) resolve the gesture -> (region, change-prompt, frames, loop), CLI overrides win
    key = (a.animate or "").strip().lower()
    preset = GESTURES.get(key, {})
    change = preset.get("change") or a.animate            # free text becomes the change-prompt
    region = a.anim_region or preset.get("region")
    if not region:                                        # guess a region noun from the gesture text
        low = (a.animate or "").lower()
        region = next((reg for kws, reg in _REGION_HINTS if any(k in low for k in kws)), "the face")
    n_variants = max(1, (a.anim_frames if a.anim_frames is not None else preset.get("frames", 2)) - 1)
    loop = a.anim_loop or preset.get("loop", "pingpong")
    denoise = a.anim_denoise if a.anim_denoise is not None else preset.get("denoise", 0.65)

    # gen resolution (proportional to the target sprite, long side = --anim-res)
    sw, sh = a.sw, a.sh
    def r64(v): return max(64, int(round(v / 64.0)) * 64)
    aw = a.anim_res if sw >= sh else r64(a.anim_res * sw / sh)
    ah = a.anim_res if sh >= sw else r64(a.anim_res * sh / sw)

    base_seed = a.seed if a.seed >= 0 else __import__("random").randint(0, 2**31 - 1)
    colors = None
    if pal_map and a.palette not in ("none", "random", "Custom"):
        colors = pal_map.get(a.palette)

    print("⚗  animation is EXPERIMENTAL — great for glow/light gestures; subtle ones "
          "(blink, tiny mouths) often misread at sprite scale.")
    print("   tip: for crisp results, render a static sprite and hand-animate it.")
    print(f"🎬 animate '{a.animate}'  |  subject={subject!r}")
    print(f"   region='{region}'  frames={n_variants + 1}  loop={loop}  "
          f"fps={a.anim_fps}  hold={a.anim_hold}s  denoise={denoise}")
    print(f"   base {aw}x{ah} -> sprite {sw}x{sh}  |  palette={a.palette}  seed={base_seed}")

    # 2) base portrait
    print("   [1] generating base portrait...")
    base = _gen_base(a, subject, base_seed, aw, ah)

    # 3) region mask (manual box overrides auto-mask)
    if a.anim_box:
        box = tuple(float(x) for x in a.anim_box.split(","))
        mask = _box_mask(base, box)
        print(f"   [2] mask = manual box {box}")
    else:
        print(f"   [2] auto-masking '{region}' with CLIPSeg (first run downloads ~1.5GB)...")
        mask = _clipseg_mask(base, region)
        cov = _mask_coverage(mask)
        if cov < 0.002 or cov > 0.6:
            print(f"       ⚠ mask covers {cov*100:.1f}% of the image — CLIPSeg may have missed "
                  f"'{region}'. Try --anim-region or --anim-box L,T,R,B.")
        else:
            print(f"       mask covers {cov*100:.1f}% of the image")

    # 4) inpaint the variant frames (different seeds -> motion variety)
    variants = []
    for i in range(n_variants):
        print(f"   [3.{i+1}] inpainting frame {i+1}/{n_variants}: {change}")
        variants.append(_inpaint(a, base, mask, subject, change, base_seed + 101 + i, denoise))

    # 5) downscale + palette-quantize everything to sprite size; build a sprite-res mask
    def to_sprite(img): return _quantize(img.resize((sw, sh), Image.NEAREST), colors)
    base_q = to_sprite(base)
    var_q = [to_sprite(v) for v in variants]
    mask_s = mask.resize((sw, sh), Image.NEAREST)
    mbin = np.asarray(mask_s) > 127

    # composite: only masked pixels come from the variant -> rest is byte-identical
    frames_q = [base_q]
    for v in var_q:
        f = np.asarray(base_q).copy()
        f[mbin] = np.asarray(v)[mbin]
        frames_q.append(Image.fromarray(f, "RGB"))

    # 6) assemble the loop with the requested timing/speed
    order = _sequence(n_variants, loop)
    per = max(20, int(1000.0 / max(0.5, a.anim_fps)))     # ms per gesture frame
    hold = max(per, int(a.anim_hold * 1000))              # ms for the resting pose
    seq = [frames_q[i] for i in order]
    durations = [hold if i == 0 else per for i in order]

    # output location: honor --output-to / --create-dirs, else CWD
    name = a.name or slug(subject)
    gtag = slug(a.animate or "anim")
    out_dir = os.path.abspath(os.path.expanduser(a.output_to)) if a.output_to else os.getcwd()
    if a.output_to and not os.path.isdir(out_dir):
        if a.create_dirs:
            os.makedirs(out_dir, exist_ok=True)
        else:
            sys.exit(f"output dir does not exist: {out_dir}  (add --create-dirs)")
    stem = os.path.join(out_dir, f"{name}_{gtag}_{sw}x{sh}_s{base_seed}")

    scale = max(1, a.view_scale // 2) if a.preview else 1
    big = [f.resize((sw * scale, sh * scale), Image.NEAREST) for f in seq] if scale > 1 else seq
    gif = stem + ".gif"
    big[0].save(gif, save_all=True, append_images=big[1:], duration=durations, loop=0, disposal=1)

    # also drop the raw sprite-size frames so they can be hand-tweaked / packed
    fdir = stem + "_frames"
    os.makedirs(fdir, exist_ok=True)
    for j, f in enumerate(frames_q):
        f.save(os.path.join(fdir, f"frame_{j:02d}.png"))

    print(f"   ✅ {len(seq)}-step loop @ {a.anim_fps}fps  ->  {gif}")
    print(f"      frames (sprite-size): {fdir}/frame_*.png")
    if not a.no_open and shutil.which("xdg-open"):
        try:
            __import__("subprocess").Popen(["xdg-open", gif],
                                           stdout=__import__("subprocess").DEVNULL,
                                           stderr=__import__("subprocess").DEVNULL)
        except Exception:
            pass
    return gif
