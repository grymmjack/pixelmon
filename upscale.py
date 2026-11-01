"""pixelmon --upscale: repair JPEG damage in a picture and/or enlarge it with an upscale model on the render server.

  --upscale IMG|DIR     the picture(s) to work on (a folder = every picture in it); repeat for more
  --upscale-model M     the model(s) in the server's models/upscale_models/, run in order: a 1x model only
                        repairs (JPEG blocks, ringing, noise), a 2x / 4x one also enlarges. M,N = a chain.
                        A name may drop its extension and its case (4x-realesrgan-x4plus). none = no enlarging
  --repair [M]          run a JPEG-repair model first (default 1x-DeJPG-realplksr-otf)
  --upscale-to SIZE     the finished size: 2x (of the original), a long side (2048), or WxH to fit inside.
                        default: what the models make. 1x with --repair = repair only, same size
  --pixel-art           the picture is pixel art: back to flat palette colours at its own size (JPEG damage
                        gone), through 2x-AnimeSharpV4-Fast-RCAN-PU when the server has it. The palette is found
                        (fitted to a bundled one like WEB-SAFE when it matches) or --palette / --palette-from.
                        a bigger --upscale-to enlarges it blocky
  --fix-jpeg IMG|DIR    same size: --upscale IMG --upscale-to 1x, with --repair unless --pixel-art
  --list-upscalers      the upscale models a render server has

Models come from OpenModelDB (https://openmodeldb.info/?t=jpeg): ./download-models.sh --upscale fetches a
tested set, ./download-models.sh --openmodeldb ID any other. ComfyUI's own UpscaleModelLoader /
ImageUpscaleWithModel run them (spandrel: ESRGAN, Compact, SPAN, SwinIR, HAT, DAT, RGT, RealPLKSR, RCAN, OmniSR …),
in tiles, so big pictures fit in the card. A see-through picture keeps its alpha (enlarged smoothly here).
pixelmon.py calls run(P, a, parser) with itself as P (its server helpers)."""
import json
import os
import re
import sys
import tempfile
import time

DEFAULT_UPSCALER = "4x-realesrgan-x4plus.pth"
DEFAULT_REPAIR = "1x-DeJPG-realplksr-otf.safetensors"
PIXEL_MODEL = "2x-AnimeSharpV4-Fast-RCAN-PU.safetensors"   # --pixel-art: crisp edges, no ringing, then box back to 1x
PICTURES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
MAX_SIDE = 16384          # a model output bigger than this is refused before it's made (a 4x of a 6000px photo)
WORK_SIDE = 8192          # with --upscale-to, a bigger model output is avoided by sending the picture smaller


def server_models(P, server):
    """The upscale models `server` has (its UpscaleModelLoader's list), [] when it has none, None if it didn't answer."""
    try:
        info = P._object_info(server, "UpscaleModelLoader")
    except Exception:
        return None
    if not info:
        return []
    spec = info.get("input") or {}
    opts = P._combo_options(dict(spec.get("required") or {}, **(spec.get("optional") or {})).get("model_name"))
    return [o for o in (opts or []) if not str(o).startswith("put_")]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", os.path.splitext(s)[0].lower())


def resolve(want, have):
    """`want` as one of the server's model files: exact, without its extension or case, ignoring -_ and spaces,
    or the one file whose name holds it. None when nothing (or more than one) matches."""
    if want in have:
        return want
    for test in (lambda h: os.path.splitext(h)[0].lower() == os.path.splitext(want)[0].lower(),
                 lambda h: _norm(h) == _norm(want),
                 lambda h: _norm(want) in _norm(h)):
        hits = [h for h in have if test(h)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    return None


def model_chain(a):
    """[model names asked for], in the order they run."""
    chain = [a.repair] if a.repair else []
    ups = a.upscale_model
    if ups is None and getattr(a, "pixel_art", False):
        ups = PIXEL_MODEL                      # (left out when the server hasn't got it: see run)
    if ups is None:                            # repair and keep the size: no enlarging model
        ups = "none" if a.repair and a.upscale_to and a.upscale_to.lower() in ("1x", "1") else DEFAULT_UPSCALER
    if ups.lower() != "none":
        chain += [m.strip() for m in ups.split(",") if m.strip()]
    return chain


def gather(specs, parser):
    """--upscale values (pictures, folders) -> the pictures, in order, each once."""
    out = []
    for spec in specs:
        path = os.path.abspath(os.path.expanduser(spec))
        if os.path.isdir(path):
            found = sorted(os.path.join(path, n) for n in os.listdir(path)
                           if n.lower().endswith(PICTURES) and os.path.isfile(os.path.join(path, n)))
            if not found:
                parser.error(f"--upscale: no pictures in {spec}")
            out += found
        elif os.path.isfile(path):
            if not path.lower().endswith(PICTURES):
                parser.error(f"--upscale: {spec} isn't a picture (png / jpg / webp / bmp / gif / tif)")
            out.append(path)
        else:
            parser.error(f"--upscale: no such file or folder: {spec}")
    return list(dict.fromkeys(out))


def parse_to(spec, parser):
    """--upscale-to -> ('x', factor) | ('side', px) | ('box', w, h) | None."""
    if not spec:
        return None
    s = spec.lower().strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x", s)
    if m and 0 < float(m.group(1)) <= 16:
        return ("x", float(m.group(1)))
    m = re.fullmatch(r"(\d{2,5})", s)
    if m and 16 <= int(s) <= MAX_SIDE:
        return ("side", int(s))
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", s)
    if m and all(16 <= int(v) <= MAX_SIDE for v in m.groups()):
        return ("box", int(m.group(1)), int(m.group(2)))
    parser.error(f"--upscale-to {spec!r}: use 2x (times the original, up to 16x), a long side like 2048, or WxH")


def final_size(to, src, made):
    """The finished (w, h) for a source of size src whose model output is `made`."""
    w, h = src
    if to is None:
        return made
    if to[0] == "x":
        return max(1, round(w * to[1])), max(1, round(h * to[1]))
    if to[0] == "side":
        k = to[1] / max(w, h)
    else:
        k = min(to[1] / w, to[2] / h)
    return max(1, round(w * k)), max(1, round(h * k))


def name_scale(name):
    """A model's scale from its name (4x-…, …_x2…, RealESRGAN_x4plus), 4 when the name doesn't say."""
    b = os.path.basename(name).lower()
    m = re.match(r"(\d+)x", b) or re.search(r"(?:^|[^a-z0-9])x(\d+)", b)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 16 else 4


def out_name(dest, base, tag):
    path = os.path.join(dest, f"{base}_{tag}.png")
    n = 2
    while os.path.exists(path):
        path = os.path.join(dest, f"{base}_{tag}-{n}.png")
        n += 1
    return path


def graph(name, chain, prefix):
    g = {"1": {"class_type": "LoadImage", "inputs": {"image": name}}}
    last = ["1", 0]
    for i, m in enumerate(chain):
        load, run = str(10 + 2 * i), str(11 + 2 * i)
        g[load] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": m}}
        g[run] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": [load, 0], "image": last}}
        last = [run, 0]
    g["9"] = {"class_type": "SaveImage", "inputs": {"images": last, "filename_prefix": prefix}}
    return g


# --- --pixel-art: flat colours back from a JPEG of pixel art --------------------------------------------------
# JPEG smears pixel art: ringing round every edge, colour bleeding (chroma is stored at half size), 70 000
# colours where there were 40. The models clean the edges; this puts every pixel back on the picture's own
# palette. The palette comes from the flat insides of shapes (JPEG leaves those nearly alone), fitted to a
# bundled palette when one explains them (WEB-SAFE, C64, EGA, PICO-8 …), so the colours come back exact. Each
# pixel then takes the nearest colour THAT IS REALLY THERE within 2 pixels: an edge between yellow and black
# becomes yellow or black, never the brown JPEG made between them.
FLAT_STD = 7          # a 3x3 patch this even (per channel) is the inside of a shape
SURE = 22             # a flat pixel this close (max channel difference) to a palette colour is that colour
FIT = 16              # a bundled palette fits when the picture's colours are this close to its entries


def _boxsum(m, r):
    import numpy as np
    p = np.pad(m, r + 1).cumsum(0).cumsum(1)
    h, w = m.shape
    k = 2 * r + 1
    return p[k:k + h, k:k + w] - p[0:h, k:k + w] - p[k:k + h, 0:w] + p[0:h, 0:w]


def _nearest(px, pal):
    """Index of the nearest palette colour (weighted RGB: green counts most) and the max channel difference."""
    import numpy as np
    wgt = np.array([2.0, 4.0, 3.0])
    idx = np.empty(len(px), np.int64)
    cheb = np.empty(len(px), np.int64)
    for i in range(0, len(px), 32768):
        q = px[i:i + 32768].astype(np.float64)
        d = (((q[:, None, :] - pal[None]) ** 2) * wgt).sum(-1)
        idx[i:i + 32768] = d.argmin(1)
        cheb[i:i + 32768] = np.abs(q - pal[idx[i:i + 32768]]).max(1)
    return idx, cheb


def pixel_palette(a, flat, known):
    """The picture's own colours (from its flat areas), as a bundled palette's entries when one fits.
    -> (rgb array, description)."""
    import numpy as np
    f = a[flat].astype(np.int64)
    if not len(f):
        f = a.reshape(-1, 3).astype(np.int64)
    key = ((f[:, 0] >> 3) << 10) | ((f[:, 1] >> 3) << 5) | (f[:, 2] >> 3)
    keys, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    sums = np.zeros((len(keys), 3))
    np.add.at(sums, inv, f)
    cen = sums / cnt[:, None]
    floor = max(6, len(f) // 50000)
    pick = []
    for i in np.argsort(-cnt):
        if cnt[i] < floor or len(pick) >= 256:
            break
        if not pick or np.abs(cen[pick] - cen[i]).max(1).min() > 20:
            pick.append(i)
    cen, w = cen[pick], cnt[pick]
    best = None
    for name, cols in known.items():
        pal = np.array(cols, np.float64)
        if len(pal) < 2:
            continue
        idx, cheb = _nearest(cen, pal)
        cover = w[cheb <= FIT].sum() / w.sum()
        if cover >= 0.97 and (best is None or (cover, -len(pal)) > (best[0], -best[2])):
            best = (cover, name, len(pal), np.unique(idx[cheb <= FIT]), pal)
    if best:
        return best[4][best[3]], f"{best[1]} palette ({len(best[3])} of its {best[2]} colours)"
    idx, _ = _nearest(f, cen)                   # its own colours: the median of each one's pixels
    own = np.array([np.median(f[idx == k], 0) if (idx == k).any() else cen[k] for k in range(len(cen))])
    return np.round(own), f"its own {len(own)} colours"


def _linesum(m, r, axis):
    """sum over a 1x(2r+1) window along axis (edge-clamped)"""
    import numpy as np
    pad = [(0, 0)] * m.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(m, pad, mode="edge"), axis=axis)
    n = m.shape[axis]
    return np.take(c, range(2 * r + 1, 2 * r + 1 + n), axis) - np.take(c, range(n), axis)


def pixel_clean(im, pal=None, known=None, r=2):
    """A picture of pixel art with JPEG damage -> (the same picture in flat palette colours, note)."""
    import numpy as np
    from PIL import Image
    a = np.asarray(im.convert("RGB")).astype(np.int64)
    h, w, _ = a.shape
    n = 9
    flat = np.ones((h, w), bool)
    line = np.ones((2, h, w), bool)
    for ch in range(3):
        c = a[..., ch].astype(np.float64)
        flat &= _boxsum(c * c, 1) / n - (_boxsum(c, 1) / n) ** 2 < FLAT_STD ** 2
        for ax in (0, 1):                         # or on a thin flat line (1-pixel stripes, arms)
            line[ax] &= _linesum(c * c, 2, ax) / 5 - (_linesum(c, 2, ax) / 5) ** 2 < FLAT_STD ** 2
    if pal is None:                               # the palette from flat areas only: a line is too JPEG-smeared
        pal, note = pixel_palette(a, flat, known or {})
        if len(pal) < 3:                          # …unless that's all there is (small, busy sprites)
            pal, note = pixel_palette(a, flat | line[0] | line[1], known or {})
    else:
        pal, note = np.asarray(pal, np.float64), f"the {len(pal)}-colour palette asked for"
    pal = np.asarray(pal, np.float64)
    px = a.reshape(-1, 3)
    near, cheb = _nearest(px, pal)
    near, cheb = near.reshape(h, w), cheb.reshape(h, w)
    sure = (flat | line[0] | line[1]) & (cheb <= SURE)   # a colour is surely here: flat (or a flat line) and close
    used = [k for k in range(len(pal)) if (sure & (near == k)).sum() >= 3]
    if not used:
        used = list(range(len(pal)))
    wgt = np.array([2.0, 4.0, 3.0])
    best = np.full((h, w), np.inf)
    lab = near.copy()
    for k in used:                                # nearest colour among the ones really there within r pixels
        here = _boxsum((sure & (near == k)).astype(np.int32), r) > 0
        d = (((a - pal[k]) ** 2) * wgt).sum(-1)
        d[~here] = np.inf
        better = d < best
        best[better], lab[better] = d[better], k
    lost = ~np.isfinite(best)                    # nothing sure nearby: plain nearest
    lab[lost] = near[lost]
    for _ in range(2):                            # a lone pixel with no same-coloured neighbour takes its 3x3 majority
        ks = np.unique(lab)
        cnts = np.stack([_boxsum((lab == k).astype(np.int32), 1) for k in ks], -1)
        pos = np.searchsorted(ks, lab)
        own = np.take_along_axis(cnts, pos[..., None], -1)[..., 0] - 1
        lab = np.where(own == 0, ks[cnts.argmax(-1)], lab)
    out = np.clip(np.round(pal), 0, 255).astype(np.uint8)[lab]
    return Image.fromarray(out), f"{len(np.unique(lab))} colours, {note}"


def mode_down(im, k):
    """k x k blocks -> one pixel: the colour most of the block has (ties: its top-left pixel's)."""
    import numpy as np
    from PIL import Image
    x = np.asarray(im.convert("RGB"))
    h, w = x.shape[0] // k, x.shape[1] // k
    key = (x[:h * k, :w * k].astype(np.int32) << np.array([16, 8, 0], np.int32)).sum(-1)
    corner = key[::k, ::k][:h, :w]
    best, bestn = corner.copy(), np.zeros((h, w), np.int32)
    for c in np.unique(key):
        n = (key == c).reshape(h, k, w, k).sum((1, 3))
        upd = (n > bestn) | ((n == bestn) & (corner == c))
        best[upd], bestn[upd] = c, n[upd]
    return Image.fromarray(np.stack([(best >> 16) & 255, (best >> 8) & 255, best & 255], -1).astype(np.uint8))


def _key(x):
    import numpy as np
    x = np.asarray(x).astype(np.int64)
    return (x[..., 0] << 16) | (x[..., 1] << 8) | x[..., 2]


def _rgb(k):
    import numpy as np
    return np.array([(k >> 16) & 255, (k >> 8) & 255, k & 255], np.float64)


def _grid_lines(p, o, n):
    """the lines of a grid with period p (fractional for a sprite scaled 4.5x) and offset o, over 0..n"""
    import numpy as np
    return np.unique(np.floor(o + p * np.arange(-1, n / p + 2) + 0.5).astype(int))


def _period(prof, most=8, off=0.15):
    """(period, offset): the coarsest grid, in quarter pixels, that nearly all the edges in `prof` sit on"""
    import numpy as np
    pad = np.concatenate([[0], prof, [0]])
    # an edge's blur isn't another edge: drop a step next to one twice its size (a 1-pixel gap's two edges stay)
    prof = np.where((2 * prof >= pad[:-2]) & (2 * prof >= pad[2:]), prof, 0)
    tot, n = prof.sum(), len(prof)
    passing = []
    for p in np.arange(2, most + 0.01, 0.25):
        if tot <= 0 or n < 3 * p:
            break
        res = []
        for o in np.arange(0, p, 0.25):
            ln = _grid_lines(p, o, n)
            ln = ln[(ln >= 0) & (ln < n)]
            res.append((1 - prof[ln].sum() / tot, o, ln))
        r, o, ln = min(res, key=lambda t: t[0])
        if r <= off and (prof[ln] > 0.1 * prof.max()).sum() >= 3:
            passing.append((p, o, r))
    if not passing:
        return 1.0, 0.0
    top = max(q[0] for q in passing)
    p, o, _ = min((q for q in passing if q[0] >= top - 0.5), key=lambda q: q[2])
    return float(p), float(o)


def grid_snap(lab_rgb, soft, sharp=None, k=1):
    """Each sprite (a patch apart from the background) on its own art-pixel grid: the grid its edges sit on in
    `soft` (the model's result at 1x), found per sprite since sheets put them at any offset and scale (4x2,
    4.5x2.25 …). Every cell of it takes one colour, the one most of its pixels in `sharp` (the model's own k x
    result) are nearest, from the sprite's own colours (ones that fill most of a cell somewhere; a speck or an
    edge's blend never does). Without a grid, only pixels of colours outside its own move.
    -> (picture, how many sprites were on a grid, how many sprites)"""
    import numpy as np
    from scipy import ndimage as ndi
    out = np.array(lab_rgb)
    key = _key(out)
    vals, cnt = np.unique(key, return_counts=True)
    bg = int(vals[cnt.argmax()])
    fg = key != bg
    wt = np.array([2.0, 4.0, 3.0])

    def patches(region, grow):
        """the sprites in `region` (a mask): parts closer than `grow` pixels count as one sprite"""
        lab, _ = ndi.label(ndi.binary_dilation(fg & region, iterations=grow) & region if grow else fg & region,
                           structure=np.ones((3, 3)))
        return [(lab == i + 1, sl, grow) for i, sl in enumerate(ndi.find_objects(lab))]

    todo = patches(np.ones(key.shape, bool), 2)
    gridded = sprites = 0
    while todo:
        whole, sl, grow = todo.pop()
        y0, x0 = max(0, sl[0].start - 1), max(0, sl[1].start - 1)
        y1, x1 = min(out.shape[0], sl[0].stop + 1), min(out.shape[1], sl[1].stop + 1)
        s, kb, m = soft[y0:y1, x0:x1], key[y0:y1, x0:x1], whole[y0:y1, x0:x1]
        h, w = y1 - y0, x1 - x0
        gx, ox = _period(np.concatenate([[0], np.abs(np.diff(s, axis=1)).sum((0, 2))]))
        gy, oy = _period(np.concatenate([[0], np.abs(np.diff(s, axis=0)).sum((1, 2))]))
        if gx * gy == 1 and grow:                 # sprites close together with different grids: apart, try again
            parts = patches(whole, grow - 1)
            if len(parts) > 1:
                todo += parts
                continue
        sprites += 1
        ly, lx = _grid_lines(gy, oy, h), _grid_lines(gx, ox, w)
        rows = [(max(a, 0), min(b, h)) for a, b in zip(ly[:-1], ly[1:]) if min(b, h) > max(a, 0)]
        cols = [(max(a, 0), min(b, w)) for a, b in zip(lx[:-1], lx[1:]) if min(b, w) > max(a, 0)]
        own = {bg}
        if gx * gy > 1:
            for a, b in rows:
                for c, d in cols:
                    if b - a >= int(gy) and d - c >= int(gx):
                        u, n = np.unique(kb[a:b, c:d], return_counts=True)
                        area = (b - a) * (d - c)
                        if n.max() >= (area if area < 4 else 2 * area / 3):
                            own.add(int(u[n.argmax()]))
        else:
            u, n = np.unique(kb[m], return_counts=True)
            own |= {int(x) for x, c in zip(u, n) if c >= max(4, 0.02 * m.sum())}
        own = sorted(own)
        cand = np.array([_rgb(u) for u in own])
        if gx * gy == 1:                          # no grid: only colours that aren't its own move, pixel by pixel
            odd = m & ~np.isin(kb, own)
            if odd.any():
                d2 = (((s[odd][:, None, :] - cand[None]) ** 2) * wt).sum(-1)
                out[y0:y1, x0:x1][odd] = cand[d2.argmin(1)]
            _merge_rare(out[y0:y1, x0:x1], m, bg)
            continue
        gridded += 1
        for a, b in rows:
            for c, d in cols:
                if not m[a:b, c:d].any():
                    continue
                v = s[a:b, c:d] if sharp is None else sharp[(y0 + a) * k:(y0 + b) * k, (x0 + c) * k:(x0 + d) * k]
                vh, vw = v.shape[:2]
                vote = np.ones((vh, vw))
                if vh > 2 and vw > 2:
                    vote[1:-1, 1:-1] = 2              # a cell's middle counts double: its rim carries blur
                d2 = (((v.reshape(-1, 1, 3) - cand[None]) ** 2) * wt).sum(-1)
                out[y0 + a:y0 + b, x0 + c:x0 + d] = cand[np.bincount(d2.argmin(1), vote.ravel(), len(cand)).argmax()]
        _merge_rare(out[y0:y1, x0:x1], m, bg)
    return out, gridded, sprites


def _merge_rare(sub, m, bg, share=0.05, near=64):
    """The sprite's colour histogram finds its outliers: a colour it hardly uses (under `share` of it). Each patch
    of one takes the colour it touches most, when that one is close and used 4x as much: a pale rim overshot on
    yellow, a dark fringe blurred by black. Purple shorts on a yellow man touch only far colours, so they stay."""
    import numpy as np
    from scipy import ndimage as ndi
    k = _key(sub)
    u, n = np.unique(k[m], return_counts=True)
    count = dict(zip(u.tolist(), n.tolist()))
    body = sum(v for c, v in count.items() if c != bg)
    for c in sorted(count, key=count.get):
        if c == bg or count[c] > share * body:
            continue
        lab, nb = ndi.label(m & (k == c))
        for j in range(1, nb + 1):
            blob = lab == j
            ring = ndi.binary_dilation(blob) & ~blob
            t, tn = np.unique(k[ring], return_counts=True)
            for d in t[np.argsort(-tn)].tolist():
                if d != c and count.get(d, 0) >= 4 * count[c] and np.abs(_rgb(d) - _rgb(c)).max() <= near:
                    sub[blob] = _rgb(d)
                    k[blob] = d
                    break


def despeck(rgb, most=2, near=64, blend=40):
    """A blob of at most `most` pixels inside one colour takes that colour when the two are close (a lighter dot in
    grey; a black eye in a face stays). One between colours: a mix of its two main neighbours (an edge's blur)
    takes the nearer of them, else the one it touches most that is close to it."""
    import numpy as np
    from scipy import ndimage as ndi
    key, out = _key(rgb), np.array(rgb)
    h, w = key.shape
    plus = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    for c in np.unique(key):
        lab, _ = ndi.label(key == c)
        sizes = np.bincount(lab.ravel())
        small = set((np.nonzero(sizes[1:] <= most)[0] + 1).tolist())
        if not small:
            continue
        me = _rgb(int(c))
        for j, sl in enumerate(ndi.find_objects(lab)):
            if j + 1 not in small:
                continue
            y0, y1, x0, x1 = max(0, sl[0].start - 1), min(h, sl[0].stop + 1), max(0, sl[1].start - 1), min(w, sl[1].stop + 1)
            blob = lab[y0:y1, x0:x1] == j + 1
            ring = ndi.binary_dilation(blob, structure=plus) & ~blob
            nb, n = np.unique(key[y0:y1, x0:x1][ring], return_counts=True)
            if len(nb) == 1:
                if np.abs(_rgb(int(nb[0])) - me).max() <= near:
                    out[y0:y1, x0:x1][blob] = _rgb(int(nb[0]))
                continue
            o = np.argsort(-n)
            A, B = _rgb(int(nb[o[0]])), _rgb(int(nb[o[1]]))
            t = float(((me - A) @ (B - A)) / max((B - A) @ (B - A), 1))
            if 0 < t < 1 and np.abs(A + t * (B - A) - me).max() <= blend:
                out[y0:y1, x0:x1][blob] = A if t < 0.5 else B
                continue
            for j2 in o:                              # else the colour it touches most that is close to it
                if np.abs(_rgb(int(nb[j2])) - me).max() <= near:
                    out[y0:y1, x0:x1][blob] = _rgb(int(nb[j2]))
                    break
    return out


def clean_pixels(res, size, pal, known):
    """A model's k-times result (or the picture itself) of pixel art at `size` -> (flat-coloured picture at size,
    note): the colours are snapped at the model's size (where its edges are sharp) and each k x k block then takes
    its main colour, so a 1-pixel detail the JPEG smeared comes back as 1 pixel. Then each sprite goes onto its
    own art-pixel grid (one colour per cell), and the specks left are cleared (despeck)."""
    from PIL import Image
    k = max(1, round(res.width / size[0]))
    if res.size != (size[0] * k, size[1] * k):
        res = res.resize((size[0] * k, size[1] * k), Image.BOX if res.width > size[0] * k else Image.NEAREST)
    out, note = pixel_clean(res, pal=pal, known=known, r=2 * k)
    out = mode_down(out, k) if k > 1 else out
    try:
        import numpy as np
        import scipy.ndimage  # noqa: F401
    except ImportError:
        return out, note + " (no scipy here: sprites not put on their pixel grid)"
    sharp = np.asarray(res, np.float64)
    soft = np.asarray(res.resize(size, Image.BOX), np.float64) if k > 1 else sharp
    grid, on, n = grid_snap(np.asarray(out), soft, sharp if k > 1 else None, k)
    return Image.fromarray(despeck(grid).astype(np.uint8)), note + f", {on} of {n} sprites on their pixel grid"


def known_palettes(P):
    try:
        return {k: [tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for h in v]
                for k, v in P._pal.ALL_PALETTES.items()}
    except Exception:
        return {}


def asked_palette(P, a, parser):
    """--palette NAME / Custom (--custom-hex) / --palette-from IMG with --pixel-art -> [(r, g, b)], or None = find it."""
    hexes = None
    if a.palette_from:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from colors import picture_palette
        hexes = picture_palette(os.path.expanduser(a.palette_from), cap=a.palette_max or 64, exact=a.palette_max or 64)
    elif a.palette == "Custom":
        hexes = a.custom_hex.replace(",", " ").split()
    elif a.palette not in ("none", "auto", "random"):
        pals = getattr(P._pal, "ALL_PALETTES", {}) if P._pal else {}
        if a.palette not in pals:
            parser.error(f"unknown palette {a.palette!r}. See --list-palettes.")
        hexes = pals[a.palette]
    if hexes is None:
        return None
    try:
        return [tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for h in hexes]
    except ValueError:
        parser.error("--palette: bad colour list")


def list_upscalers(P, servers):
    c = P.C
    for server in servers:
        have = server_models(P, server)
        head = f"{c['b']}{P._short(server)}{c['rst']}"
        if have is None:
            print(f"{head}  {c['yel']}didn't answer{c['rst']}")
            continue
        print(f"{head}  {len(have)} upscale model{'s' if len(have) != 1 else ''} (models/upscale_models/)")
        for m in have:
            tags = [t for t, test in (("default", m == DEFAULT_UPSCALER), ("default --repair", m == DEFAULT_REPAIR)) if test]
            print(f"   {c['grn']}{m}{c['rst']}" + (f"  {c['dim']}{', '.join(tags)}{c['rst']}" if tags else ""))
        if not have:
            print(f"   {c['dim']}none: on that box run ./download-models.sh --upscale{c['rst']}")
    print(f"{c['dim']}more: https://openmodeldb.info/?t=jpeg  ->  ./download-models.sh --openmodeldb ID{c['rst']}")


def find_server(P, chain):
    """(server, model file names there) for the first server of the pool that has every model, or (None, notes)."""
    pool = P.POOL or [P.SERVER]
    notes = []
    for server in pool:
        have = server_models(P, server)
        if have is None:
            notes.append(f"{P._short(server)} didn't answer")
            continue
        names = [resolve(m, have) for m in chain]
        missing = [m for m, n in zip(chain, names) if n is None]
        if not missing:
            return server, names
        notes.append(f"{P._short(server)} has no {', '.join(missing)}"
                     + (f" (it has: {', '.join(have[:12])}{' …' if len(have) > 12 else ''})" if have else
                        " (no upscale models at all)"))
    return None, notes


def pick_server(P, chain):
    """find_server, or exit saying what each server lacks and how to get it."""
    server, names = find_server(P, chain)
    if server:
        return server, names
    pool, notes = P.POOL or [P.SERVER], names
    sys.exit(f"{', '.join(P._short(x) for x in pool)} can't render this:\n" + "\n".join(f"   • {n}" for n in notes)
             + "\n   on the render box:  ./download-models.sh --upscale   (a tested set)"
             + "\n                       ./download-models.sh --openmodeldb ID   (any model on openmodeldb.info)"
             + "\n   pixelmon --list-upscalers   shows what each box has")


def run(P, a, parser):
    if a.list_upscalers:
        return list_upscalers(P, P.POOL or [P.SERVER])
    for bad, flag in ((a.batch, "--batch"), (a.batch_file, "--batch-file"), (a.animate, "--animate"),
                      (a.init, "--init"), (a.mask, "--mask"), (a.control, "--control"), (a.depth, "--depth"),
                      (a.steer, "--steer"), (a.post_sweep, "--post-sweep"), (a.tile, "--tile"), (a.cutout, "--cutout"),
                      (a.transparent, "--transparent"), (a.print_graph, "--print-graph")):
        if bad:
            parser.error(f"{flag} doesn't go with --upscale (it works on finished pictures)")
    if a.number != 1:
        parser.error("-n doesn't go with --upscale: a model gives the same picture every time")
    pixel = bool(getattr(a, "pixel_art", False))
    files = gather(a.upscale, parser)
    chain = model_chain(a)
    if not chain and not pixel:
        parser.error("--upscale-model none with no --repair: nothing to do")
    to = parse_to(a.upscale_to or ("1x" if pixel else None), parser)
    pal = asked_palette(P, a, parser) if pixel else None
    known = known_palettes(P) if pixel and pal is None else None
    if a.name and len(files) > 1:
        parser.error("--name names one picture: drop it for several (each is named after its source)")
    dest = os.path.expanduser(a.output_to) if a.output_to else os.path.join(P.OUTPUT, "pixelmon")
    if a.output_to and not os.path.isdir(dest):
        if not a.create_dirs:
            parser.error(f"--output-to: no such folder: {a.output_to} (add --create-dirs)")
    steps = chain + (["pixel-art clean (" + (f"{len(pal)} colours asked for" if pal else "palette found") + ")"]
                     if pixel else [])
    if a.dry_run:
        print(f"   🔍 {len(files)} picture(s): {' → '.join(steps)}"
              + (f" → {a.upscale_to}" if a.upscale_to else "") + f"  |  into {dest}")
        for f in files:
            print(f"      {f}")
        return
    os.makedirs(dest, exist_ok=True)
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None              # scans and 4x results are big on purpose
    server, names = None, []
    if chain and P.CHECKS_OFF:
        server, names = P.SERVER, chain
    elif chain and pixel and a.upscale_model is None and chain[-1] == PIXEL_MODEL:
        server, names = find_server(P, chain)    # the pixel model helps; without it the clean runs here alone
        if not server:
            print(f"   note: no render server has {PIXEL_MODEL} (./download-models.sh --upscale) — cleaning "
                  f"without it, a little rounder", flush=True)
            chain = chain[:-1]
            server, names = pick_server(P, chain) if chain else (None, [])
    elif chain:
        server, names = pick_server(P, chain)
    print(f"   🔍 {' → '.join(names + (['pixel-art clean'] if pixel else []))}"
          + (f" on {P._short(server)}" if server else " (here, no server)"), flush=True)
    t0, made, failed = time.time(), 0, 0
    for i, src in enumerate(files):
        label = f"[{i + 1}/{len(files)}] " if len(files) > 1 else ""
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode == "P" and "transparency" in im.info:
                    im = im.convert("RGBA")
                size = im.size
                alpha = im.getchannel("A") if "A" in im.getbands() and im.getchannel("A").getextrema()[0] < 255 else None
        except Exception as e:
            print(f"   ✗ {label}{os.path.basename(src)}: can't read it ({e})")
            failed += 1
            continue
        k_est = 1
        for n in names:
            k_est *= name_scale(n)
        target = final_size(to, size, None) if to else None
        if target and max(target) > MAX_SIDE:
            print(f"   ✗ {label}{os.path.basename(src)}: {target[0]}x{target[1]} is past {MAX_SIDE}px — a smaller --upscale-to")
            failed += 1
            continue
        use = names
        send = size
        cap = min(MAX_SIDE, max(WORK_SIDE, max(target))) if target else MAX_SIDE
        if pixel and max(size) * k_est > WORK_SIDE:
            use = []                               # pixel art can't go in smaller: clean it as it is
            print(f"   note: {label}{size[0]}x{size[1]} x{k_est} is too big for the model — cleaning it without",
                  flush=True)
        elif max(size) * k_est > cap:
            # a model that would make something huge gets the picture smaller (it rebuilds the detail): a 6758x8525
            # scan with a 4x model and --upscale-to 1x goes in at 1690x2131 and comes back at full size, restored
            if not target:
                print(f"   ✗ {label}{os.path.basename(src)}: {size[0]}x{size[1]} x{k_est} would be {size[0] * k_est}x"
                      f"{size[1] * k_est}, past {MAX_SIDE}px — add --upscale-to (1x = restored at its own size, "
                      f"or a long side like 8192)")
                failed += 1
                continue
            f = cap / k_est / max(size)
            send = (max(1, round(size[0] * f)), max(1, round(size[1] * f)))
        base = P.slug(a.name) if a.name else P.slug(os.path.splitext(os.path.basename(src))[0]) or "picture"
        print(f"   ⏳ {label}{os.path.basename(src)} ({size[0]}x{size[1]}"
              + (f", sent at {send[0]}x{send[1]} for the x{k_est} model" if send != size else "") + ")…", flush=True)
        t1 = time.time()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if use:
                    up = src
                    if send != size or not src.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        up = os.path.join(tmp, base + ".png")       # shrunk, or a BMP / GIF / TIFF: sent as a PNG
                        with Image.open(src) as im:
                            im = ImageOps.exif_transpose(im).convert("RGB")
                            (im.resize(send, Image.LANCZOS) if send != size else im).save(up, compress_level=1)
                    g = graph(P._upload_image(up, server, "--upscale"), use, f"pixelmon/upscale/{base}")
                    outs = P.wait(P.submit(g, server), server, timeout=1800,
                                  resubmit=lambda fresh: P.submit(P.fresh_copy(g) if fresh else g, server))
                    im = next(im for node in outs.values() for im in node.get("images", []))
                    got = P.fetch_image(im, tmp, server)
                    with Image.open(got) as res:   # the server's copy is ComfyUI's; ours is the one that's kept
                        res.load()
                        res = res.convert("RGB")
                else:
                    with Image.open(src) as im:
                        res = ImageOps.exif_transpose(im).convert("RGB")
                k = res.width / send[0]
                note = ""
                if pixel:
                    res, note = clean_pixels(res, size, pal, known)
                want = final_size(to, size, res.size)
                if max(want) > MAX_SIDE:
                    raise RuntimeError(f"{want[0]}x{want[1]} is too big: use --upscale-to (e.g. 4096)")
                if want != res.size:
                    res = res.resize(want, Image.NEAREST if pixel else Image.LANCZOS)
                if alpha is not None:
                    res.putalpha(alpha.resize(res.size, Image.NEAREST if pixel else Image.LANCZOS))
                k_final = res.width / size[0]
                same = abs(k_final - 1) < 0.01
                scale = f"x{round(k_final)}" if abs(k_final - round(k_final)) < 0.01 else f"{res.width}w"
                first = "clean" if pixel else "repaired" if a.repair else "restored" if same else ""
                out = out_name(dest, base, "_".join(t for t in (first, "" if same else scale) if t))
                res.save(out)
                rec = {"tool": "pixelmon", "version": P.__version__, "upscale": use, "to": a.upscale_to or "",
                       "source": os.path.basename(src), "server": P._short(server) if use else "",
                       "argv": sys.argv[1:]}
                if pixel:
                    rec["pixel_art"] = note
                P.png_add_chunks(out, [P.png_itxt("pixelmon", json.dumps(rec, ensure_ascii=False))])
        except P.RenderFailed as e:
            print(f"   ✗ {label}{os.path.basename(src)}: {e}")
            failed += 1
            continue
        except (RuntimeError, OSError, StopIteration) as e:
            print(f"   ✗ {label}{os.path.basename(src)}: {e or 'the server returned no picture'}")
            failed += 1
            continue
        made += 1
        how = (f"models x{k:g}" if use else "") + (", " if use and note else "") + note
        print(f"   ✅ {label}{size[0]}x{size[1]} → {res.width}x{res.height} ({how}) in "
              f"{time.time() - t1:.1f}s -> {out}", flush=True)
        if not a.no_open and len(files) == 1:
            P.open_file(out)
    print(f"   all done in {time.time() - t0:.1f}s  |  files in {dest}"
          + (f"  |  {failed} of {len(files)} picture(s) failed" if failed else ""))
    if failed:
        sys.exit(1)
