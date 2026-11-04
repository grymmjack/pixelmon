"""Sprite animation: walk / run / idle / attack / hurt / death / jump cycles, a character turned 4 or 8 ways,
and looping effects (fire, explosion, sparkle, smoke) - as game-ready sprite strips and GIFs.

How: every frame of a move is drawn into ONE render, side by side, so the model paints the same character in
each cell. A pose ControlNet (OpenPose skeletons from poses.py) sets each cell's pose; effects use depth
shapes instead. Each cell also gets its own prompt (its facing, its moment). The pixel step runs once over the
whole sheet, so every frame shares one palette and one pixel grid. Then the sheet is cut into frames and
saved as a strip PNG, a GIF and a bigger preview GIF.

Later sheets of the same character (another move, another facing) are painted with the first sheet's first
frame in their first cell, and only the other cells drawn new (inpainting): the model copies the character it
can see beside them, so the run, the attack and the 8 directions stay one character.

Called by pixelmon.py for --moves / --effect, with pixelmon itself as P (its graph, submit, wait, collect).
"""
import copy
import math
import os
import random
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import poses

P = None

# --------------------------------------------------------------------------- layout

def layout(n, fw, fh, mp=1.5e6, extra=0):
    """Grid for n frames (+ extra reference cells) of fw x fh: (cols, rows, cell_w, cell_h) at about mp pixels,
    cells multiples of 64 and the sheet near SDXL's 3:2."""
    total = n + extra
    best = None
    for cols in range(1, total + 1):
        rows = math.ceil(total / cols)
        if rows > cols:
            continue
        cw = math.sqrt(mp / total * fw / fh)
        ch = cw * fh / fw
        cw64, ch64 = max(256, int(round(cw / 64)) * 64), max(256, int(round(ch / 64)) * 64)
        W, H = cols * cw64, rows * ch64
        if W > 2560 or H > 2560:
            continue
        score = abs(math.log((W / H) / 1.5)) + 0.15 * (cols * rows - total) + abs(math.log(W * H / mp))
        if best is None or score < best[0]:
            best = (score, cols, rows, cw64, ch64)
    return best[1:]


def cell_box(i, cols, cw, ch):
    return ((i % cols) * cw, (i // cols) * ch, (i % cols + 1) * cw, (i // cols + 1) * ch)


# --------------------------------------------------------------------------- the render

def _regional(g, cells, cols, cw, ch, clip_src, strength):
    """Each cell its own prompt: text -> ConditioningSetArea over the cell, all combined with the whole
    picture's prompt. Everything that read the plain prompt (node 6) reads the combination."""
    src = ["6", 0]
    for i, text in enumerate(cells):
        if not text or strength <= 0:
            continue
        x0, y0, x1, y1 = cell_box(i, cols, cw, ch)
        g[f"rt{i}"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": text}}
        g[f"ra{i}"] = {"class_type": "ConditioningSetArea",
                       "inputs": {"conditioning": [f"rt{i}", 0], "width": x1 - x0, "height": y1 - y0,
                                  "x": x0, "y": y0, "strength": strength}}
        g[f"rc{i}"] = {"class_type": "ConditioningCombine", "inputs": {"conditioning_1": src,
                                                                         "conditioning_2": [f"ra{i}", 0]}}
        src = [f"rc{i}", 0]
    if src == ["6", 0]:
        return
    mine = {k for k in g if k[:2] in ("rt", "ra", "rc")}
    for k, node in g.items():
        if k in mine:
            continue
        for name, v in node["inputs"].items():
            if v == ["6", 0]:
                node["inputs"][name] = src


def render_sheet(a, ctl_img, ctl_mode, size, sheet_px, prompt, negative, cells, cols, cw, ch, seed, name,
                 init=None, mask=None, tmp=None, cell_strength=0.0, dest=None, strength=None, end=None, cut=False,
                 keep_full=False):
    """One render: a pixelmon graph at size (the control picture's), pixelated to sheet_px, with per-cell
    prompts. init / mask: paint only the mask's white over init (reference cells kept). cut: BiRefNet's mask
    saved with it (see _cutout). -> (the picture's path, the files saved, the settings used)."""
    ja = copy.copy(a)
    ctl = os.path.join(tmp, f"{name}_{ctl_mode}.png")
    ctl_img.save(ctl)
    ja.control, ja.control_mode = ctl, ctl_mode
    ja.control_strength = a.pose_strength if strength is None else strength
    ja.control_end = a.pose_end if end is None else end
    ja.gen_w, ja.gen_h = size
    ja.sw, ja.sh = sheet_px
    ja.ow, ja.oh = sheet_px
    ja.enlarge = 1
    ja.exact_prompt, ja.prompt, ja.negative = True, prompt, negative
    ja.style_add, ja.style_neg = "", ""
    ja.name, ja.batch, ja.tile, ja.post_combos = name, None, None, None
    ja.cutout, ja.fit_subject, ja.preview, ja.depth = bool(cut), False, False, None
    ja.cutout_key, ja.fit_to, ja.fit_scale = 0, None, 0
    if cut:
        ja.transparent = False                      # BiRefNet does the cutting
    ja.init = ja.mask = None
    ja.pixel_grid = 0                               # pixelmon's own grid for this size (the sprite size itself
                                                    # broke a 64 px goblin into specks: legs gone, a blob at the hips)
    if init is not None:
        ip, mp_ = os.path.join(tmp, f"{name}_init.png"), os.path.join(tmp, f"{name}_mask.png")
        init.save(ip)
        mask.save(mp_)
        ja.init, ja.mask, ja.mask_mode, ja.denoise = ip, mp_, "fill", 1.0
        ja.mask_grow, ja.inpaint_model = 0, None
    for cache in (P._INIT_UPLOADS, P._MASK_UPLOADS, P._CONTROL_UPLOADS):
        cache.clear()                               # every render has its own pictures
    g = P.build_graph(ja, seed, server=P.SERVER)
    if ja.art:                                      # art sprites: the render resized to the sheet, no pixel step
        g["17"] = {"class_type": "ImageScale", "inputs": {"image": ["8", 0], "upscale_method": "lanczos",
                                                         "width": sheet_px[0], "height": sheet_px[1], "crop": "disabled"}}
        g["11"]["inputs"]["images"] = ["17", 0]
        if cut and "c2" not in g:
            g["c1"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": a.cutout_model}}
            g["c2"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["c1", 0], "image": ["8", 0]}}
            g["c3"] = {"class_type": "MaskToImage", "inputs": {"mask": ["c2", 0]}}
            g["c4"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": f"pixelmon/{name}_cutmask", "images": ["c3", 0]}}
    _regional(g, cells, cols, cw, ch, g["6"]["inputs"]["clip"], cell_strength)
    if keep_full or os.environ.get("PIXELMON_SPRITES_DEBUG"):      # the model's own picture, before the pixel step
        g["dbg"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": f"pixelmon/{name}_0-full", "images": ["8", 0]}}
    pid = P.submit(g)
    outs = P.wait(pid, resubmit=lambda fresh: P.submit(P.fresh_copy(g) if fresh else g))
    files = P.collect(outs, tmp, P.SERVER)
    ja._full = None
    for f in files:
        if "_0-full_" in os.path.basename(f):
            _debug_copy(f, "0")
            ja._full = f
    files = [f for f in files if "_0-full_" not in os.path.basename(f)]
    sheet = P.pick_sprite(files)
    if not sheet:
        raise RuntimeError("the render saved no picture")
    return sheet, files, ja


# --------------------------------------------------------------------------- cutting and saving

def cut(sheet_path, n, cols, fw, fh, first=0):
    """The sheet's frames (PIL RGBA), first..first+n-1, in reading order."""
    im = Image.open(sheet_path).convert("RGBA")
    return [im.crop(cell_box(i, cols, fw, fh)) for i in range(first, first + n)]


def key_out(frames, tol=40):
    """No transparency yet (art, or the background wasn't cleared): the colour round the sheet's edges goes
    see-through in every frame (flood fill from each frame's border)."""
    out = []
    for f in frames:
        a = np.asarray(f.convert("RGBA")).copy()
        if (a[..., 3] < 255).any():
            out.append(f)
            continue
        h, w = a.shape[:2]
        rgb = a[..., :3].astype(np.int32)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(border, axis=0)
        near = (np.abs(rgb - bg).sum(-1) <= tol)
        seen = np.zeros((h, w), bool)
        stack = [(y, x) for x in range(w) for y in (0, h - 1)] + [(y, x) for y in range(h) for x in (0, w - 1)]
        while stack:
            y, x = stack.pop()
            if seen[y, x] or not near[y, x]:
                continue
            seen[y, x] = True
            if y > 0: stack.append((y - 1, x))
            if y < h - 1: stack.append((y + 1, x))
            if x > 0: stack.append((y, x - 1))
            if x < w - 1: stack.append((y, x + 1))
        a[seen, 3] = 0
        out.append(Image.fromarray(a, "RGBA"))
    return out


def trim_bg(frames, bg, tol=48):
    """BiRefNet's mask takes a rim of the backdrop with it: pixels near the backdrop's colour that touch the
    see-through area go see-through too (repeatedly, so a rim two or three pixels wide goes)."""
    out = []
    bg = np.array(bg, np.int32)
    for f in frames:
        a = np.asarray(f.convert("RGBA")).copy()
        near = np.abs(a[..., :3].astype(np.int32) - bg).sum(-1) <= tol
        for _ in range(4):
            clear = a[..., 3] < 128
            pad = np.pad(clear, 1, constant_values=True)
            touch = pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
            gone = near & touch & ~clear
            if not gone.any():
                break
            a[gone, 3] = 0
        out.append(Image.fromarray(a, "RGBA"))
    return out


def tidy(frames, keep=0.04):
    """Floating specks and stray bits off each frame: only blobs (8-connected opaque pixels) of at least keep
    of the frame's biggest blob stay - the character and what it holds, not lone pixels or a far-off smudge."""
    out = []
    for f in frames:
        a = np.asarray(f.convert("RGBA")).copy()
        op = a[..., 3] >= 128
        h, w = op.shape
        lab = np.zeros((h, w), np.int32)
        sizes = [0]
        for y0 in range(h):
            for x0 in range(w):
                if op[y0, x0] and not lab[y0, x0]:
                    n = len(sizes)
                    lab[y0, x0] = n
                    stack, cnt = [(y0, x0)], 0
                    while stack:
                        y, x = stack.pop()
                        cnt += 1
                        for dy in (-1, 0, 1):
                            for dx in (-1, 0, 1):
                                yy, xx = y + dy, x + dx
                                if 0 <= yy < h and 0 <= xx < w and op[yy, xx] and not lab[yy, xx]:
                                    lab[yy, xx] = n
                                    stack.append((yy, xx))
                    sizes.append(cnt)
        if len(sizes) > 1:
            big = max(sizes)
            gone = np.array([sz < max(3, keep * big) for sz in sizes])
            gone[0] = False
            a[gone[lab], 3] = 0
        out.append(Image.fromarray(a, "RGBA"))
    return out


def glow_alpha(frames, black=28, hard=False, smoke=False):
    """Effects on black: brightness becomes see-through (a fire's dark edge fades out), colour kept.
    Below `black` counts as backdrop (the model's faint haze). `smoke`: grey counts as solid too (glowing
    alone would lose a grey puff), its colour left as painted. `hard` (pixel art): each pixel solid or
    gone, so the GIF's one-bit see-through matches the PNG."""
    out = []
    for f in frames:
        a = np.asarray(f.convert("RGBA"), np.float32)
        mx = a[..., :3].max(-1)
        if hard:
            al = (mx >= black + 22).astype(np.float32)
            rgb = a[..., :3]
        else:
            al = np.clip((mx - black) / (255.0 - black) * 1.6, 0, 1)
            rgb = np.where(al[..., None] > 0, a[..., :3] / np.maximum(mx[..., None] / 255.0, np.maximum(al[..., None], 1e-3)), 0)
            if smoke:
                key = np.clip((mx - black) / 60.0, 0, 1)
                rgb = np.where((key > al)[..., None], a[..., :3], rgb)
                al = np.maximum(al, key)
        a[..., :3] = np.clip(rgb, 0, 255)
        a[..., 3] = (al * 255) * (a[..., 3] / 255.0)
        out.append(Image.fromarray(a.round().astype(np.uint8), "RGBA"))
    return tidy(out, keep=0.02) if hard else out


def save_anim(frames, base, fps, loop=True, hold_last=0.0, preview=4):
    """frames -> base.png (a strip, true size), base.gif (true size), base_xN.gif (N x bigger, crisp)."""
    fw, fh = frames[0].size
    strip = Image.new("RGBA", (fw * len(frames), fh), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        strip.paste(f, (i * fw, 0))
    strip.save(base + ".png")
    dur = [int(1000 / fps)] * len(frames)
    if hold_last and not loop:
        dur[-1] = int(hold_last * 1000)

    def gif(frs, path):
        # one shared palette for every frame (no flicker), index 0 kept for see-through
        pal_src = Image.new("RGBA", (fw * len(frs), frs[0].height))
        for i, f in enumerate(frs):
            pal_src.paste(f, (i * f.width, 0))
        rgb = Image.new("RGB", pal_src.size, (0, 0, 0))
        rgb.paste(pal_src, (0, 0), pal_src)
        pal = rgb.quantize(colors=255, method=Image.MEDIANCUT, dither=Image.NONE)
        outs = []
        for f in frs:
            fr = Image.new("RGB", f.size, (0, 0, 0))
            fr.paste(f, (0, 0), f)
            q = fr.quantize(palette=pal, dither=Image.NONE)
            idx = np.asarray(q).astype(np.uint8) + 1
            idx[np.asarray(f)[..., 3] < 128] = 0
            o = Image.fromarray(idx, "P")
            o.putpalette([0, 0, 0] + pal.getpalette()[:255 * 3])
            outs.append(o)
        outs[0].save(path, save_all=True, append_images=outs[1:], duration=dur, loop=0 if loop else 1,
                     transparency=0, disposal=2, optimize=False)
    gif(frames, base + ".gif")
    if preview > 1:
        big = [f.resize((fw * preview, fh * preview), Image.NEAREST) for f in frames]
        gif(big, base + f"_x{preview}.gif")
    return base + ".png"


# --------------------------------------------------------------------------- characters (moves, directions)

MOVE_FPS = {"walk": 8, "run": 12, "idle": 5, "attack": 10, "hurt": 8, "death": 8, "jump": 10, "stand": 1}
EFFECT_FPS = 12


def fps_for(a, key):
    return a.anim_fps if getattr(a, "anim_fps_given", False) else MOVE_FPS.get(key, EFFECT_FPS)


SHEET_NEG = ("shadow, cast shadow, ground, floor, scenery, background objects, different characters, inconsistent character design, changing outfit, changing colors, merged "
             "figures, overlapping figures, cropped, cut off feet, extra limbs, extra arms, extra legs, "
             "text, numbers, grid lines, frame borders, panels, watermark, blurry, 3d render, photograph")


def view_words(yaw, pitch):
    w = poses.DIR_WORDS.get(int(yaw) % 360, "")
    return w + (", seen from above, top-down view" if pitch >= 20 else "")


def character_jobs(a):
    """(move, yaw, [(pose, yaw, pitch)]) for every sheet asked for: each move in each direction."""
    pitch = 30.0 if a.view == "topdown" else 0.0
    if a.directions in (4, 8):
        yaws = poses.DIRECTIONS[a.directions]
    else:
        yaws = [{"right": 90, "left": 270, "front": 0, "back": 180}[a.facing]]
    jobs = []
    for mv in a.moves:
        n = a.anim_frames or poses.MOVES[mv][0]
        qs = poses.move_poses(mv, n)
        for yaw in yaws:
            jobs.append((mv, yaw, [(poses.body(q), yaw, pitch) for q in qs]))
    return jobs


def cell_size(fw, fh, cells, mp=1.5e6):
    """Cells (multiples of 64) of the frame's shape, cells of them side by side making about mp pixels."""
    cw = math.sqrt(mp / cells * fw / fh)
    ch = cw * fh / fw
    return max(320, int(round(cw / 64)) * 64), max(320, int(round(ch / 64)) * 64)


BG_RGB = {"white": (255, 255, 255), "black": (0, 0, 0), "grey": (128, 128, 128), "gray": (128, 128, 128),
          "light grey": (200, 200, 200), "green": (0, 177, 64), "magenta": (255, 0, 255)}


def _bg_rgb(a):
    return BG_RGB.get(a.sprite_bg, (128, 128, 128))


def _words(a):
    style = (", " + a.style_add) if a.style_add else ""
    pre = "" if a.art else "pixel, "
    post = "" if a.art else ", game sprite, simple flat colors"
    return style, pre, post


def palette_of(img, cap=48):
    """The opaque colours of a sprite (the most used, up to cap) as an array K x 3."""
    a = np.asarray(img.convert("RGBA"))
    px = a[a[..., 3] >= 128][:, :3]
    if not len(px):
        return None
    cols, counts = np.unique(px, axis=0, return_counts=True)
    if len(cols) > cap:
        q = Image.fromarray(px.reshape(1, -1, 3)).quantize(colors=cap, method=Image.MEDIANCUT, dither=Image.NONE)
        cols = np.array(q.getpalette()[:cap * 3]).reshape(-1, 3)
    return cols.astype(np.int32)


def lock_palette(frames, pal):
    """Each frame's opaque pixels moved to the nearest of the reference's colours (redmean distance), so
    no frame flickers to a colour the character doesn't have."""
    out = []
    for f in frames:
        a = np.asarray(f.convert("RGBA")).copy()
        op = a[..., 3] >= 128
        px = a[op][:, :3].astype(np.int32)
        if len(px):
            rm = (px[:, None, 0] + pal[None, :, 0]) / 2
            d = ((2 + rm / 256) * (px[:, None, 0] - pal[None, :, 0]) ** 2 + 4 * (px[:, None, 1] - pal[None, :, 1]) ** 2
                 + (2 + (255 - rm) / 256) * (px[:, None, 2] - pal[None, :, 2]) ** 2)
            a[op, :3] = pal[d.argmin(1)].astype(np.uint8)
        a[..., 3] = np.where(op, 255, 0)
        out.append(Image.fromarray(a, "RGBA"))
    return out


def pixelate(full, mask, box, fw, fh, colours=64):
    """One cell of the model's full-size picture as a fw x fh sprite: each sprite pixel takes the most common
    colour of its block (after a median cut to colours), see-through where BiRefNet's mask covers under ~45 %
    of the block. pixelmon's palette node, tuned for one big sprite, wiped out thin legs and staffs here."""
    cell = full.crop(box).convert("RGB")
    q = np.asarray(cell.quantize(colors=colours, method=Image.MEDIANCUT, dither=Image.NONE).convert("RGB"))
    h, w = q.shape[:2]
    out = np.zeros((fh, fw, 4), np.uint8)
    for j in range(fh):
        for i in range(fw):
            b = q[j * h // fh:(j + 1) * h // fh, i * w // fw:(i + 1) * w // fw].reshape(-1, 3)
            c, n = np.unique(b, axis=0, return_counts=True)
            out[j, i, :3] = c[n.argmax()]
    if mask is not None:
        m = mask.convert("L").resize(full.size, Image.BILINEAR).crop(box).resize((fw, fh), Image.BOX)
        out[..., 3] = np.where(np.asarray(m) >= 115, 255, 0)
    else:
        out[..., 3] = 255
    return Image.fromarray(out, "RGBA")


def _mask_of(files):
    f = next((f for f in files if "_cutmask_" in os.path.basename(f)), None)
    if not f:
        return None
    m = Image.open(f).convert("L")
    m.load()
    return m


def _debug_copy(path, tag):
    """PIXELMON_SPRITES_DEBUG=DIR keeps every in-between picture there (before / after the cut-out...)."""
    d = os.environ.get("PIXELMON_SPRITES_DEBUG")
    if d and path and os.path.isfile(path):
        import shutil
        os.makedirs(d, exist_ok=True)
        shutil.copy2(path, os.path.join(d, f"{os.path.basename(path)[:-4]}_{tag}.png"))


def _cutout(a, ja, sheet, files):
    """BiRefNet's cut-out on a finished sheet (the mask came with the render)."""
    _debug_copy(sheet, "1-render")
    for f in files:
        if "_cutmask_" in os.path.basename(f):
            _debug_copy(f, "2-mask")
    ja.cutout, ja.cutout_key, ja.fit_to, ja.fit_scale = True, 0, None, 0
    P.cutout_output(ja, sheet, P.SERVER, files)
    _debug_copy(sheet, "3-cut")


def _reference(a, dest, tmp, name, yaw, pitch, seed, fw, fh):
    """The character once, standing (facing yaw), as a normal full-size sprite cut out by BiRefNet: every
    frame is painted beside it and copies it."""
    cw, ch = cell_size(fw, fh, 1, mp=1.0e6)
    k = poses.body({})
    pose_img = poses.sheet([(k, yaw, pitch)], 1, cw, ch, fill=a.fill)
    view = view_words(yaw, pitch)
    style, pre, post = _words(a)
    prompt = f"{pre}{a.prompt}{style}, standing, {view}, full body, one character, plain solid {a.sprite_bg} background{post}"
    print(f"   🧍 the character, standing ({view}) - every frame copies it")
    sheet, files, ja = render_sheet(a, pose_img, "openpose", (cw, ch), (fw, fh), prompt, a.negative + ", " + SHEET_NEG,
                                    [], 1, cw, ch, seed, f"{name}_ref", tmp=tmp, dest=dest,
                                    strength=min(a.pose_strength, 0.75), end=min(a.pose_end, 0.6), cut=True,
                                    keep_full=True)
    full = Image.open(ja._full).convert("RGB")
    full.load()
    _debug_copy(sheet, "1-render")
    fr = tidy(trim_bg([pixelate(full, _mask_of(files), (0, 0) + full.size, fw, fh)], _bg_rgb(a)))
    ref_png = os.path.join(dest, f"{name}_ref.png")
    if a.palette not in ("none", "random"):
        hexes = a.custom_hex.split() if a.palette == "Custom" else ((getattr(P._pal, "ALL_PALETTES", {}) or {}).get(a.palette) or [])
        if hexes:
            fr = lock_palette(fr, np.array([[int(h.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4)] for h in hexes], np.int32))
    fr[0].save(ref_png)
    if os.path.abspath(sheet) != os.path.abspath(ref_png):
        os.remove(sheet)
    return (fr[0], full), (k, yaw, pitch), ref_png


def _frame(a, dest, tmp, tag, i, ref, pose, words, view, seed, fw, fh, cw, ch):
    """One frame, painted on a two-cell canvas beside the reference (only the right cell is new)."""
    pose_img = poses.sheet([ref[1], pose], 2, cw, ch, fill=a.fill)
    init = Image.new("RGB", (2 * cw, ch), _bg_rgb(a))
    init.paste(ref[0][1].resize((cw, ch), Image.LANCZOS), (0, 0))     # the model's own full-size picture of it
    # only the room round the new pose is painted (its skeleton's box, with space for hair, a cape or a raised
    # weapon): left the whole cell, a lying body's empty space above got filled with more characters
    mask = Image.new("RGB", (2 * cw, ch), (0, 0, 0))
    sk = np.asarray(pose_img.convert("L"))[:, cw:] > 20
    ys, xs = np.nonzero(sk)
    if len(xs):
        m = int(0.16 * ch)
        box = (cw + max(0, xs.min() - m), max(0, ys.min() - m), cw + min(cw - 1, xs.max() + m), min(ch - 1, ys.max() + m // 3))
    else:
        box = (cw, 0, 2 * cw - 1, ch - 1)
    ImageDraw.Draw(mask).rectangle(box, fill=(255, 255, 255))
    style, pre, post = _words(a)
    prompt = (f"{pre}character sheet of the same {a.prompt}{style} twice, left: standing, right: {words}, {view}, "
              f"full body, the same character design, outfit and colors in both, plain solid {a.sprite_bg} "
              f"background{post}")
    neg = a.negative + ", " + SHEET_NEG
    if 135 <= int(pose[1]) % 360 <= 225:             # seen from behind: no face (it copied the front's)
        neg += ", face, eyes, nose, mouth, facing the viewer"
    sheet, files, ja = render_sheet(a, pose_img, "openpose", (2 * cw, ch), (2 * fw, fh), prompt,
                                    neg, [], 2, cw, ch, seed, f"{tag}_f{i}",
                                    init=init, mask=mask, tmp=tmp, dest=dest, cut=True, keep_full=True)
    full = Image.open(ja._full).convert("RGB")
    full.load()
    W, H = full.size
    f = pixelate(full, _mask_of(files), (W // 2, 0, W, H), fw, fh)
    _debug_copy(sheet, "1-render")
    os.remove(sheet)
    return f, full.crop((W // 2, 0, W, H))


def frame_fault(px, ref_px):
    """Why a painted frame is no good, or "": hardly any character (a direction's reference once came out as
    just the club), or cut off by the frame's bottom edge (painted bigger than its skeleton)."""
    a = np.asarray(px.convert("RGBA"))[..., 3] >= 128
    r = np.asarray(ref_px.convert("RGBA"))[..., 3] >= 128
    if a.sum() < 0.35 * max(1, r.sum()):
        return "too little character"
    tall = lambda m: int(np.ptp(np.nonzero(m.any(1))[0])) + 1 if m.any() else 0
    if a[-1].sum() > 2 and tall(a) >= 1.1 * tall(r):    # a foot on the last row is fine; legs going past it aren't
        return "cut off at the bottom"
    return ""


def _frame_ok(a, dest, tmp, tag, i, ref, pose, words, view, seed, fw, fh, cw, ch, tries=3):
    """_frame, painted again with another seed (up to tries in all) when frame_fault finds it no good."""
    best = None
    for k in range(tries):
        got = _frame(a, dest, tmp, tag, i, ref, pose, words, view, seed + 7919 * k, fw, fh, cw, ch)
        why = frame_fault(got[0], ref[0][0])
        if not why:
            return got
        print(f"        ↻ frame {i + 1}: {why} - painting it again")
        best = best or got
    return best


def direction_refs(a, dest, tmp, name, ref, yaws, pitch, seed, fw, fh, cw, ch):
    """{yaw: ref}: the character standing in each direction, each painted beside the nearest one already made
    (front, then 45 or 90 degrees at a time round both sides), so every direction's frames copy a picture of
    the character from their own side - a back view copied from the front came out with a face."""
    ry = int(ref[1][1]) % 360
    refs = {ry: ref}
    dist = lambda p, q: min((p - q) % 360, (q - p) % 360)
    todo = sorted((int(y) % 360 for y in yaws if int(y) % 360 != ry), key=lambda y: dist(y, ry))
    for k, y in enumerate(todo):
        near = min(refs, key=lambda r: dist(r, y))
        pose = (poses.body({}), y, pitch)
        print(f"   🧭 [{k + 1}/{len(todo)}] standing, {poses.DIR_NAMES[y]} ({view_words(y, pitch)})")
        px, big = _frame_ok(a, dest, tmp, f"{name}_dir{y}", 0, refs[near], pose, "standing, turned",
                            view_words(y, pitch), seed, fw, fh, cw, ch)
        refs[y] = ((px, big), pose)
    return refs


def run_characters(a, dest, tmp, slug):
    fw, fh = a.frame_w, a.frame_h
    name = a.name or slug(a.prompt)
    jobs = character_jobs(a)
    seed = a.seed if a.seed >= 0 else random.randint(0, 2**31 - 1)
    ref_img, ref_pose, ref_png = _reference(a, dest, tmp, name, jobs[0][1], jobs[0][2][0][2], seed, fw, fh)
    ref = (ref_img, ref_pose)
    pal = palette_of(ref_img[0]) if a.lock_palette else None
    if a.palette not in ("none", "random"):          # a named palette: every frame in its colours
        hexes = a.custom_hex.split() if a.palette == "Custom" else ((getattr(P._pal, "ALL_PALETTES", {}) or {}).get(a.palette) or [])
        if hexes:
            pal = np.array([[int(h.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4)] for h in hexes], np.int32)
    made = [(ref_png, "reference")]
    cw, ch = cell_size(fw, fh, 2, mp=1.0e6)
    total = sum(len(f) for mv, _, f in jobs if not (mv == "stand" and a.directions in (4, 8)))
    done = 0
    refs = {int(ref[1][1]) % 360: ref}
    if a.directions in (4, 8):
        refs = direction_refs(a, dest, tmp, name, ref, poses.DIRECTIONS[a.directions], jobs[0][2][0][2], seed,
                              fw, fh, cw, ch)
        turn = [refs[int(y) % 360][0][0] for y in poses.DIRECTIONS[a.directions]]
        turn = tidy(trim_bg(turn, _bg_rgb(a)))
        if pal is not None:
            turn = lock_palette(turn, pal)
        out = save_anim(turn, os.path.join(dest, f"{name}_turnaround"), 4, loop=True, preview=a.anim_preview)
        made.append((out, "turnaround"))
        print(f"      ✅ {out}  (+ .gif, _x{a.anim_preview}.gif)")
    for j, (mv, yaw, frames) in enumerate(jobs):
        n = len(frames)
        view = view_words(yaw, frames[0][2])
        tag = f"{name}_{mv}" + (f"_{poses.DIR_NAMES[int(yaw) % 360]}" if a.directions in (4, 8) else "")
        dref = refs.get(int(yaw) % 360, ref)
        if mv == "stand" and a.directions in (4, 8):
            continue                                     # the turnaround is the standing frames
        print(f"   🏃 [{j + 1}/{len(jobs)}] {mv}  {view}  —  {n} frame(s)")
        fr = []
        for i, pose in enumerate(frames):
            fr.append(_frame_ok(a, dest, tmp, tag, i, dref, pose, poses.frame_words(mv, i, n), view, seed, fw, fh, cw, ch)[0])
            done += 1
            print(f"      · frame {i + 1}/{n}   ({done}/{total})")
        fr = tidy(trim_bg(fr, _bg_rgb(a)))
        if pal is not None:
            fr = lock_palette(fr, pal)
        loop = poses.MOVES[mv][1]
        out = save_anim(fr, os.path.join(dest, tag), fps_for(a, mv), loop=loop,
                        hold_last=a.anim_hold if not loop else 0, preview=a.anim_preview)
        made.append((out, f"{mv} {poses.DIR_NAMES[int(yaw) % 360] if a.directions in (4, 8) else ''}".strip()))
        print(f"      ✅ {out}  (+ .gif, _x{a.anim_preview}.gif)")
    return made


# --------------------------------------------------------------------------- effects

EFFECTS = {
    "fire": (8, True, "a burning fire, flames licking upward", ["flickering flames"] * 8),
    "explosion": (8, False, "an explosion, a billowing orange fireball",
                  ["a tiny bright white flash", "a bright yellow-white burst", "a billowing orange fireball",
                   "a big fireball with orange flames and dark smoke", "a huge fiery cloud turning to smoke",
                   "dark grey smoke with embers", "thinning grey smoke", "fading wisps of smoke"]),
    "sparkle": (6, True, "magic sparkles, twinkling stars",
                ["a small twinkle", "a bright four-pointed star sparkle", "several sparkling stars",
                 "a burst of glittering stars", "sparkles fading", "a faint twinkle"]),
    "smoke": (8, True, "a puff of pale grey smoke rising", ["a soft rising puff of pale grey smoke"] * 8),
}


def _noise_loop(w, h, t, seed, scale=6):
    """Smooth noise that loops in time: two noise fields blended round a circle."""
    rng = np.random.default_rng(seed)
    g1 = rng.random((scale + 1, scale + 1, 4))
    ang = 2 * math.pi * t
    wts = np.array([max(0, math.cos(ang)), max(0, math.sin(ang)), max(0, -math.cos(ang)), max(0, -math.sin(ang))])
    grid = (g1 * wts).sum(-1) / wts.sum()
    im = Image.fromarray((grid * 255).astype(np.uint8), "L").resize((w, h), Image.BICUBIC)
    return np.asarray(im, np.float32) / 255.0


def effect_shape(kind, i, n, w, h, seed):
    """A depth picture for frame i of n of an effect (white = near: the effect's body)."""
    t = i / n
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = (xx - w / 2) / (w / 2), (h - yy) / h            # u -1..1 across, v 0 (bottom)..1 (top)
    if kind == "fire":
        # one full teardrop flame (wide at the base, a point at the top) with two side tongues, each swaying
        # and stretching on whole sine cycles (it loops); edges broken by noise that rises through the loop
        nz = _noise_loop(w, h * 2, t, seed, 7)
        off = int(t * h) % h
        nz = np.roll(nz, -off, axis=0)[:h]
        uu = u + 0.16 * (nz - 0.5) * (0.3 + v)
        d = np.zeros((h, w), np.float32)
        rng = np.random.default_rng(seed)
        tongues = [(0.0, 0.46, 0.80), (-0.36, 0.22, 0.50), (0.36, 0.22, 0.56), (-0.12, 0.16, 0.92)]
        for j, (bx0, wd, ht0) in enumerate(tongues):
            ph = rng.random()
            bx = bx0 + 0.05 * math.sin(2 * math.pi * (t + ph)) * (1 + v)
            ht = ht0 + 0.08 * math.sin(2 * math.pi * (2 * t + ph))
            sv = np.clip(v / ht, 0, 1)
            prof = wd * 2.0 * sv ** 0.35 * (1 - sv) ** 0.8 + 1e-3
            lean = 0.10 * math.sin(2 * math.pi * (t + ph)) * sv * sv
            d = np.maximum(d, np.clip(1 - np.abs(uu - bx - lean) / prof, 0, 1) ** 0.6 * (v < ht))
        d *= np.clip(v * 14, 0, 1)                          # sits on its base line, not cut off
        d = np.clip(d * 1.3, 0, 1)
    elif kind == "explosion":
        r = 0.12 + 0.85 * (1 - (1 - t) ** 2)
        nz = _noise_loop(w, h, 0.3 * t, seed, 6)
        cy = 0.42 + 0.18 * t
        dist = np.sqrt(u * u + ((v - cy) * 2) ** 2) / r
        body = np.clip(1 - dist + 0.35 * (nz - 0.5), 0, 1)
        d = body ** (0.6 + 1.6 * t) * (1 - 0.55 * t)
    elif kind == "sparkle":
        # four-pointed stars that grow and fade on their own beats
        rng = np.random.default_rng(seed)
        d = np.zeros((h, w), np.float32)
        for k in range(5):
            ph = (t + k / 5) % 1.0
            size = math.sin(math.pi * ph) ** 1.5 * (0.18 + 0.12 * rng.random())
            cx, cy = rng.uniform(-0.55, 0.55), rng.uniform(0.2, 0.8)
            if size < 0.01:
                continue
            du, dv = (u - cx), (v - cy) * h / w * 2
            arm = lambda p, q: np.exp(-(p / size) ** 2 - (q / (size * 0.12)) ** 2)
            core = np.exp(-(du * du + dv * dv) / (0.25 * size) ** 2)
            d = np.maximum(d, np.clip(arm(du, dv) + arm(dv, du) + core, 0, 1))
    else:  # smoke
        nz = _noise_loop(w, h, t, seed, 5)
        cy = 0.25 + 0.45 * t
        r = 0.25 + 0.3 * t
        dist = np.sqrt(u * u + ((v - cy) * 1.6) ** 2) / r
        d = np.clip(1 - dist + 0.4 * (nz - 0.5), 0, 1) ** 0.8 * (1 - 0.6 * t)
    im = Image.fromarray((np.clip(d, 0, 1) * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(w / 80))
    return im.convert("RGB")


def explosion_order(frames, seed):
    """A one-shot explosion must grow, peak, then go: the model often paints its biggest burst first, so the
    first half is sorted small to big, and the last three frames burn away (pixels dropped in one fixed
    random order, so what's left of each frame is a part of the one before)."""
    n = len(frames)
    area = lambda f: int((np.asarray(f)[..., 3] > 0).sum())
    half = n // 2
    out = sorted(frames[:half], key=area) + list(frames[half:])
    k = min(3, n - half)
    if k:
        w, h = out[0].size
        order = np.random.default_rng(seed).random((h, w))
        for j in range(k):
            keep = 1 - (j + 1) / (k + 1)
            a = np.asarray(out[n - k + j]).copy()
            a[order > keep, 3] = 0
            out[n - k + j] = Image.fromarray(a, "RGBA")
    return out


def run_effect(a, dest, tmp, slug):
    kind = a.effect
    n0, loop, words, phases = EFFECTS[kind]
    n = a.anim_frames or n0
    phases = [phases[min(len(phases) - 1, int(i * len(phases) / n))] for i in range(n)]
    fw, fh = a.frame_w, a.frame_h
    cols, rows, cw, ch = layout(n, fw, fh)
    seed = a.seed if a.seed >= 0 else random.randint(0, 2**31 - 1)
    shape = Image.new("RGB", (cols * cw, rows * ch))
    for i in range(n):
        x0, y0, x1, y1 = cell_box(i, cols, cw, ch)
        shape.paste(effect_shape(kind, i, n, cw, ch, seed), (x0, y0))
    subject = a.prompt or words
    style = (", " + a.style_add) if a.style_add else ""
    look = "pixel art" if not a.art else "digital art"
    prompt = (f"{look} sprite sheet of {subject}{style}, {n} animation frames of one effect, each frame "
              f"centered in its own square, glowing, on a plain pure black background")
    if not a.art:
        prompt = "pixel, " + prompt + ", game sprite effect, simple flat colors"
    cells = [f"{subject}{style}, {ph}, on black" for ph in phases]
    name = a.name or slug(a.prompt or kind)
    tag = f"{name}_{kind}"
    print(f"   💥 {kind}  —  {n} frames, {cols}x{rows} cells of {cw}x{ch}")
    sheet, files, ja = render_sheet(a, shape, "depth", (cols * cw, rows * ch), (cols * fw, rows * fh), prompt,
                                    a.negative + ", characters, people, creature, face, eyes, mouth, skull, ground, scenery, text, grid lines, frame borders",
                                    cells, cols, cw, ch, seed, tag, tmp=tmp, dest=dest, keep_full=True,
                                    cell_strength=a.cell_strength if a.cell_strength is not None else (0.6 if kind == "explosion" else 0.35))
    full = Image.open(ja._full).convert("RGB")
    full.load()
    _debug_copy(sheet, "1-render")
    os.remove(sheet)
    fr = glow_alpha([pixelate(full, None, cell_box(i, cols, cw, ch), fw, fh) for i in range(n)], hard=not a.art,
                    smoke=kind in ("smoke", "explosion"))
    if kind == "explosion":
        fr = explosion_order(fr, seed)
    out = save_anim(fr, os.path.join(dest, tag), fps_for(a, kind), loop=loop, hold_last=0, preview=a.anim_preview)
    print(f"      ✅ {out}  (+ .gif, _x{a.anim_preview}.gif)")
    return [(out, kind)]


def run(a, dest, slug, pixelmon):
    """--moves / --effect: make every sheet, return [(strip path, label)]."""
    global P
    P = pixelmon
    tmp = tempfile.mkdtemp(prefix="pixelmon-sprites-")
    if a.effect:
        return run_effect(a, dest, tmp, slug)
    return run_characters(a, dest, tmp, slug)
