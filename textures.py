"""Textures: the steps a material picture gets after the render (--surface, --tile in --art, --flatten, --maps).

- wrap_resize: resize a seamless picture without breaking its seam (the edges are padded with the opposite
  side first, so the filter sees what's really next to them).
- flatten: take out the big light/dark sweep a render puts across a surface (a lit corner, a vignette) so
  the picture repeats and maps onto a model without a visible patch of light. Fine detail stays.
- tile_preview: the picture 3x3, to check the seam by eye.
- maps: height, normal, roughness and ambient occlusion pictures made from the colour picture.
"""
import numpy as np
from PIL import Image


def _pad_wrap(im, px, tile):
    """The picture with px of the opposite side added round it (only on the axes that tile)."""
    a = np.asarray(im)
    pw = ((px if tile in ("both", "y") else 0,) * 2, (px if tile in ("both", "x") else 0,) * 2)
    pw = pw + ((0, 0),) * (a.ndim - 2)
    mode = "wrap"
    return np.pad(a, pw, mode=mode), pw


def wrap_resize(im, size, tile="both"):
    """Resize im (PIL) to size; with tile set the seam survives (padded with the opposite side, cut after)."""
    if im.size == tuple(size):
        return im
    if not tile:
        return im.resize(size, Image.LANCZOS)
    w, h = im.size
    W, H = size
    px = max(8, int(max(w / W, h / H) * 4))
    a, pw = _pad_wrap(im, px, tile)
    big = Image.fromarray(a)
    kx, ky = W / w, H / h
    out = big.resize((round(big.width * kx), round(big.height * ky)), Image.LANCZOS)
    x0, y0 = round(pw[1][0] * kx), round(pw[0][0] * ky)
    return out.crop((x0, y0, x0 + W, y0 + H))


def _box(b, r, axis):
    """A running mean 2r+1 wide along axis (the ends lose r each side: the caller padded them)."""
    c = np.cumsum(b, axis=axis, dtype=np.float64)
    c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
    n = b.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, n + 1), axis=axis)
    lo = np.take(c, np.arange(0, n - 2 * r), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def _blur(a, sigma, tile):
    """A Gaussian-like blur (three box passes) of a float array H x W [x C]; it wraps round on the axes that
    tile and mirrors at the others, so a seamless picture stays seamless."""
    a = np.asarray(a, np.float32)
    r = max(1, int(round((np.sqrt(12 * sigma * sigma / 3 + 1) - 1) / 2)))     # box radius for 3 passes
    p = 3 * r
    rest = ((0, 0),) * (a.ndim - 2)
    b = np.pad(a, ((p, p), (0, 0)) + rest, mode="wrap" if tile in ("both", "y") else "reflect")
    b = np.pad(b, ((0, 0), (p, p)) + rest, mode="wrap" if tile in ("both", "x") else "reflect")
    for _ in range(3):
        b = _box(b, r, 0)
        b = _box(b, r, 1)
    return b


def flatten(im, amount=0.6, tile=None):
    """Even out a picture's lighting: each pixel's brightness divided by its neighbourhood's (a blur a sixth
    of the picture wide), mixed in by amount (0 = as it was, 1 = perfectly even). Colours keep their hue."""
    if amount <= 0:
        return im
    rgba = im.convert("RGBA")
    a = np.asarray(rgba, np.float32)
    rgb = a[..., :3] / 255.0
    lin = rgb ** 2.2
    lum = lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    sigma = max(4.0, min(im.size) / 6.0)
    low = _blur(lum, sigma, tile) + 1e-4
    gain = (lum.mean() / low) ** amount
    gain = np.clip(gain, 0.25, 4.0)
    out = np.clip(lin * gain[..., None], 0, 1) ** (1 / 2.2)
    a[..., :3] = out * 255.0
    return Image.fromarray(a.round().astype(np.uint8), "RGBA").convert(im.mode if im.mode in ("RGB", "RGBA") else "RGB")


def tile_preview(im, tile="both", repeat=3, max_side=1536):
    """The picture repeated repeat x repeat (only along the tiling axes), shrunk to max_side."""
    nx = repeat if tile in ("both", "x") else 1
    ny = repeat if tile in ("both", "y") else 1
    w, h = im.size
    out = Image.new(im.mode, (w * nx, h * ny))
    for j in range(ny):
        for i in range(nx):
            out.paste(im, (i * w, j * h))
    k = max_side / max(out.size)
    if k < 1:
        out = out.resize((round(out.width * k), round(out.height * k)), Image.LANCZOS)
    return out


# ---------------------------------------------------------------- material maps (--maps)

def _gray(im):
    a = np.asarray(im.convert("RGB"), np.float32) / 255.0
    return (a ** 2.2) @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def height_map(im, tile=None, detail=0.5, invert=False):
    """A height picture from colour: brightness with the lighting taken out, fine detail (scratches, grain)
    mixed with the broad shapes (planks, panels). detail 0..1: more = sharper, shallower relief. 0..1 out."""
    g = _gray(im)
    s = min(im.size)
    small = _blur(g, max(1.0, s / 512), tile)               # the noise off
    broad = _blur(small, max(2.0, s / 48), tile)
    sweep = _blur(small, max(4.0, s / 6), tile)              # the light across the whole picture
    fine = small - broad
    shape = broad - sweep
    h = shape * (1 - detail) + fine * (0.5 + detail)
    if invert:
        h = -h
    lo, hi = np.percentile(h, 1), np.percentile(h, 99)
    return np.clip((h - lo) / max(1e-6, hi - lo), 0, 1)


def normal_map(height, strength=4.0, tile=None):
    """An OpenGL-style normal picture (+y up: green is light where the surface faces up) from a height array.
    strength x 7.5 degrees is how far the steeper bumps lean (4 = 30)."""
    hp = np.pad(height, ((1, 1), (0, 0)), mode="wrap" if tile in ("both", "y") else "edge")
    hp = np.pad(hp, ((0, 0), (1, 1)), mode="wrap" if tile in ("both", "x") else "edge")
    # Sobel: slope each way
    dx = (hp[:-2, 2:] + 2 * hp[1:-1, 2:] + hp[2:, 2:]) - (hp[:-2, :-2] + 2 * hp[1:-1, :-2] + hp[2:, :-2])
    dy = (hp[2:, :-2] + 2 * hp[2:, 1:-1] + hp[2:, 2:]) - (hp[:-2, :-2] + 2 * hp[:-2, 1:-1] + hp[:-2, 2:])
    # scaled so the steeper bumps (90th percentile) lean strength x 7.5 degrees (4 = 30), whatever the material:
    # a fine grain (brushed steel) and broad planks both come out readable, neither a sea of stripes
    g = np.sqrt(dx * dx + dy * dy)
    k = np.tan(np.radians(min(80.0, strength * 7.5))) / max(1e-6, float(np.percentile(g, 90)))
    nx, ny, nz = -dx * k, dy * k, np.ones_like(height)
    ln = np.sqrt(nx * nx + ny * ny + nz * nz)
    n = np.stack([nx / ln, ny / ln, nz / ln], -1)
    return Image.fromarray(((n * 0.5 + 0.5) * 255).round().astype(np.uint8), "RGB")


def roughness_map(im, height, tile=None, base=0.65):
    """Roughness 0 (polished) .. 1 (matte): busy, dark, crevice areas rougher; smooth bright areas (paint,
    polished metal) shinier. base is the middle value."""
    g = _gray(im)
    s = min(im.size)
    busy = np.abs(g - _blur(g, max(1.5, s / 256), tile))
    busy = _blur(busy, max(1.5, s / 256), tile)
    busy = busy / max(1e-6, np.percentile(busy, 98))
    dark = 1 - g / max(1e-6, np.percentile(g, 98))
    r = base + 0.25 * (np.clip(busy, 0, 1) - 0.5) + 0.15 * (np.clip(dark, 0, 1) - 0.5) + 0.1 * (0.5 - height)
    return np.clip(r, 0, 1)


def ao_map(height, tile=None):
    """Ambient occlusion: dark where a point sits below its surroundings (crevices, seams). 0..1, 1 = open."""
    s = min(height.shape)
    occ = np.zeros_like(height)
    for sig, w in ((s / 128, 0.5), (s / 48, 0.35), (s / 16, 0.15)):
        occ += w * np.clip(_blur(height, max(1.0, sig), tile) - height, 0, None)
    occ /= max(1e-6, np.percentile(occ, 99))
    return np.clip(1 - 0.85 * occ, 0, 1)


def to_img(a):
    return Image.fromarray((np.clip(a, 0, 1) * 255).round().astype(np.uint8), "L")


def make_maps(im, tile=None, strength=4.0, detail=0.5, invert=False):
    """{'height': L, 'normal': RGB, 'rough': L, 'ao': L} for a colour picture."""
    h = height_map(im, tile, detail, invert)
    return {"height": to_img(h), "normal": normal_map(h, strength, tile),
            "rough": to_img(roughness_map(im, h, tile)), "ao": to_img(ao_map(h, tile))}
