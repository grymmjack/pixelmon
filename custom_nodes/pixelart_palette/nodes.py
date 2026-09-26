"""PixelArtPalette — turn a 512px SD render into a true, palette-locked sprite.

Pipeline:  downscale (real pixels) -> quantize to palette -> upscale for viewing.

Outputs two images:
  * "pixels"  — the true small sprite (e.g. 64x64). SaveImage this for real assets.
  * "preview" — the same image upscaled with nearest-neighbour, just so you can
                actually see it. PreviewImage / SaveImage this to eyeball results.
"""
import os
import subprocess
import tempfile

import numpy as np
import torch
from PIL import Image, ImageFilter

from .palettes import ALL_PALETTES, parse_palette

_RESAMPLE = {"nearest": Image.NEAREST, "box (area average)": Image.BOX}


# ---------------------------------------------------------------------------
# Image <-> ComfyUI tensor helpers. ComfyUI IMAGE = float32 [B,H,W,C] in [0,1].
# ---------------------------------------------------------------------------
def _tensor_to_pil(img):
    arr = (img[0].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _pil_to_tensor(pil):
    if pil.mode not in ("RGB", "RGBA"):     # keep alpha if present; else RGB
        pil = pil.convert("RGB")
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None, ]


# ---------------------------------------------------------------------------
# >>> THE COLOR-MAPPING DECISION lives here <<<
#
# Given every pixel of the downscaled image and the palette, decide which
# palette color each pixel becomes. This is the heart of "locking" an image to
# a palette, and there are real trade-offs in HOW you measure "closest color":
#
#   - Plain RGB Euclidean (implemented below): fast, simple, but RGB distance
#     doesn't match how the eye perceives color, so it can pick a technically-
#     close-but-visually-off swatch (e.g. a muddy brown over a cleaner one).
#   - Perceptual weighting: the human eye is most sensitive to green, least to
#     blue. Weighting the channels (or using the cheap "redmean" approximation)
#     usually picks colors that *look* more right.
#
# `pixels` is an (N, 3) int array of RGB values; `palette` is (P, 3) int.
# Return an (N,) int array of indices into `palette` — one per pixel.
# ---------------------------------------------------------------------------
def nearest_indices(pixels, palette):
    # Perceptual "redmean" distance — a cheap approximation of human color
    # vision. Green is weighted most (the eye is most sensitive to it), blue
    # least, and red's weight shifts depending on how red the colors already
    # are. Picks visually-closer palette swatches than plain RGB distance.
    px = pixels.astype(np.float64)                              # (N,3)
    pal = palette.astype(np.float64)                            # (P,3)
    rmean = (px[:, None, 0] + pal[None, :, 0]) * 0.5            # (N,P)
    dr = px[:, None, 0] - pal[None, :, 0]
    dg = px[:, None, 1] - pal[None, :, 1]
    db = px[:, None, 2] - pal[None, :, 2]
    dist = (2 + rmean / 256.0) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256.0) * db * db
    return dist.argmin(axis=1)


def _quantize_flat(small_rgb, palette_rgb):
    """Map each pixel to its nearest palette color (no dithering)."""
    h, w, _ = np.asarray(small_rgb).shape
    flat = np.asarray(small_rgb).reshape(-1, 3)
    pal = np.array(palette_rgb, dtype=np.int32)
    idx = nearest_indices(flat, pal)
    out = pal[idx].reshape(h, w, 3).astype(np.uint8)
    return Image.fromarray(out, "RGB")


# ---------------------------------------------------------------------------
# Dithering against the palette. Two families:
#   ordered  — a fixed threshold matrix nudges each pixel before the nearest-
#              color pick, giving the regular cross-hatch patterns of EGA/VGA-era
#              art (Bayer) or print-style dots (clustered).
#   error diffusion — each pixel's quantization error is pushed onto unvisited
#              neighbours by a kernel; scanned serpentine (alternate row
#              direction) so the error doesn't drag into diagonal "worms".
# `amount` (0..1) scales the effect: the ordered threshold spread, or the share
# of error diffused. Kernels are the canonical published ones (dx, dy, weight).
# ---------------------------------------------------------------------------
_DIFFUSION = {
    "floyd-steinberg": (16, [(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)]),
    "jarvis": (48, [(1, 0, 7), (2, 0, 5), (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
                    (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1)]),
    "stucki": (42, [(1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
                    (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1)]),
    "burkes": (32, [(1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2)]),
    "sierra": (32, [(1, 0, 5), (2, 0, 3), (-2, 1, 2), (-1, 1, 4), (0, 1, 5), (1, 1, 4), (2, 1, 2),
                    (-1, 2, 2), (0, 2, 3), (1, 2, 2)]),
    "sierra2": (16, [(1, 0, 4), (2, 0, 3), (-2, 1, 1), (-1, 1, 2), (0, 1, 3), (1, 1, 2), (2, 1, 1)]),
    "sierra-lite": (4, [(1, 0, 2), (-1, 1, 1), (0, 1, 1)]),
    "atkinson": (8, [(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)]),  # diffuses 6/8
}


def _bayer(n):
    m = np.array([[0]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m


_ORDERED = {
    "bayer2": _bayer(2), "bayer4": _bayer(4), "bayer8": _bayer(8), "bayer16": _bayer(16),
    "clustered": np.array([[12, 5, 6, 13], [4, 0, 1, 7], [11, 3, 2, 8], [15, 10, 9, 14]]),
}

DITHER_METHODS = ["none"] + list(_ORDERED) + list(_DIFFUSION)


def _palette_lut(pal):
    """Nearest-palette-index lookup table over a 64^3 RGB cube (same redmean
    metric as nearest_indices), so per-pixel error diffusion stays fast."""
    lv = np.arange(64) * 4 + 2
    cube = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    return nearest_indices(cube, pal).reshape(64, 64, 64)


def _quantize_dither(small_rgb, palette_rgb, method="floyd-steinberg", amount=1.0):
    """Dither `small_rgb` down to `palette_rgb` with an ordered or error-diffusion method."""
    pal = np.array(palette_rgb, dtype=np.int32)
    src = np.asarray(small_rgb.convert("RGB"), dtype=np.float64)
    h, w, _ = src.shape
    amount = float(max(0.0, min(1.0, amount)))

    if method in _ORDERED:
        m = _ORDERED[method]
        n = m.size
        thr = (m + 0.5) / n - 0.5                                   # centred, in -0.5..0.5
        tile = np.tile(thr, (h // m.shape[0] + 1, w // m.shape[1] + 1))[:h, :w]
        spread = 255.0 / max(1.0, len(pal) ** (1 / 3)) * amount     # ~ one palette step
        nudged = np.clip(src + tile[..., None] * spread, 0, 255)
        idx = nearest_indices(nudged.reshape(-1, 3).astype(np.int32), pal)
        return Image.fromarray(pal[idx].reshape(h, w, 3).astype(np.uint8), "RGB")

    div, kernel = _DIFFUSION.get(method, _DIFFUSION["floyd-steinberg"])
    lut = _palette_lut(pal)
    buf = src.copy()
    out = np.zeros((h, w), dtype=np.int32)
    for y in range(h):
        rev = y % 2 == 1                                            # serpentine
        xs = range(w - 1, -1, -1) if rev else range(w)
        for x in xs:
            old = np.clip(buf[y, x], 0, 255)
            i = lut[int(old[0]) >> 2, int(old[1]) >> 2, int(old[2]) >> 2]
            out[y, x] = i
            err = (old - pal[i]) * (amount / div)
            for dx, dy, wt in kernel:
                nx, ny = (x - dx if rev else x + dx), y + dy
                if 0 <= nx < w and ny < h:
                    buf[ny, nx] += err * wt
    return Image.fromarray(pal[out].astype(np.uint8), "RGB")


def _despeckle(pixels_rgb, max_size, passes=2):
    """Remove stray noise: every same-color island of <= max_size pixels
    (8-connected, so 1px diagonal lines survive as one component) is recolored
    to the most common color touching it. Runs after palette quantization, where
    anti-aliased SDXL edges leave lone pixels of whichever palette color was
    nearest."""
    from collections import Counter, deque

    arr = np.asarray(pixels_rgb.convert("RGB"), dtype=np.int32)
    h, w, _ = arr.shape
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for _ in range(passes):
        ids = (arr[..., 0] << 16) | (arr[..., 1] << 8) | arr[..., 2]
        seen = np.zeros((h, w), dtype=bool)
        out = ids.copy()
        changed = False
        for y0 in range(h):
            for x0 in range(w):
                if seen[y0, x0]:
                    continue
                c = ids[y0, x0]
                comp, border = [], Counter()
                q = deque([(y0, x0)])
                seen[y0, x0] = True
                while q:
                    y, x = q.popleft()
                    comp.append((y, x))
                    for dy, dx in nbrs:
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w:
                            if ids[ny, nx] == c:
                                if not seen[ny, nx]:
                                    seen[ny, nx] = True
                                    q.append((ny, nx))
                            else:
                                border[ids[ny, nx]] += 1
                if len(comp) <= max_size and border:
                    fill = border.most_common(1)[0][0]
                    for y, x in comp:
                        out[y, x] = fill
                    changed = True
        arr = np.stack([(out >> 16) & 255, (out >> 8) & 255, out & 255], axis=-1)
        if not changed:
            break
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _make_transparent(pixels_rgb, tolerance):
    """Flood-fill from the borders to cut out a solid background -> RGBA.

    Only background-colored pixels CONNECTED to the image edge are cleared, so
    same-colored pixels *inside* the subject are kept. Produces hard (1-bit)
    alpha — what pixel-art sprites want, no soft matte fringe.
    """
    from collections import deque

    arr = np.asarray(pixels_rgb.convert("RGB"), dtype=np.int16)
    h, w, _ = arr.shape

    # Background color = the most common color along the four borders.
    border = np.concatenate([arr[0, :], arr[-1, :], arr[:, 0], arr[:, -1]]).reshape(-1, 3)
    colors, counts = np.unique(border, axis=0, return_counts=True)
    bg = colors[counts.argmax()]
    is_bg = np.abs(arr - bg).sum(axis=2) <= tolerance

    # BFS inward from every border pixel that matches the background.
    visited = np.zeros((h, w), dtype=bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if is_bg[y, x] and not visited[y, x]:
                visited[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg[y, x] and not visited[y, x]:
                visited[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and is_bg[ny, nx]:
                visited[ny, nx] = True
                dq.append((ny, nx))

    alpha = np.where(visited, 0, 255).astype(np.uint8)
    rgba = np.dstack([arr.astype(np.uint8), alpha])
    return Image.fromarray(rgba, "RGBA")


def _snapper_bin():
    """Locate the spritefusion-pixel-snapper binary (env override or repo build)."""
    env = os.environ.get("PIXELMON_SNAPPER")
    if env and os.path.exists(env):
        return env
    # realpath: this file is reached via a symlink (~/ComfyUI/...), resolve to the real repo
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    cand = os.path.join(root, "tools", "pixel-snapper", "target", "release",
                        "spritefusion-pixel-snapper")
    return cand if os.path.exists(cand) else None


def _snap_pixels(pil_img, k_colors, pixel_size=None):
    """Run Hugo-Dz/spritefusion-pixel-snapper on a messy AI image -> clean,
    grid-snapped RGB. Auto-detects the pixel grid; k_colors caps the palette."""
    binp = _snapper_bin()
    if not binp:
        raise RuntimeError(
            "pixel-snapper not built. Build it once with:\n"
            "  cd ~/pixelmon/tools/pixel-snapper && cargo build --release\n"
            "(or run ~/pixelmon/install.sh). Needs the Rust toolchain.")
    with tempfile.TemporaryDirectory() as td:
        ip, op = os.path.join(td, "in.png"), os.path.join(td, "out.png")
        pil_img.convert("RGB").save(ip)
        cmd = [binp, ip, op, str(int(k_colors))]
        if pixel_size:
            cmd += ["--pixel-size", str(pixel_size)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return Image.open(op).convert("RGB").copy()


class PixelArtPalette:
    @classmethod
    def INPUT_TYPES(cls):
        palette_names = ["none"] + list(ALL_PALETTES.keys()) + ["Custom"]
        return {
            "required": {
                "image": ("IMAGE",),
                "downscale_to": ("INT", {"default": 128, "min": 8, "max": 1024, "step": 1}),
                "palette": (palette_names,),
                "dithering": (DITHER_METHODS,),
                "downscale_filter": (list(_RESAMPLE.keys()), {"default": "nearest"}),
                "view_scale": ("INT", {"default": 8, "min": 1, "max": 32, "step": 1}),
            },
            "optional": {
                "smooth": (["mode", "median", "none"], {"default": "mode"}),
                "pixel_grid": ("INT", {"default": 128, "min": 32, "max": 1024, "step": 8}),
                "transparent_bg": ("BOOLEAN", {"default": False}),
                "bg_tolerance": ("INT", {"default": 16, "min": 0, "max": 128, "step": 1}),
                "snap_pixels": ("BOOLEAN", {"default": False}),
                "snap_colors": ("INT", {"default": 0, "min": 0, "max": 256, "step": 1}),
                "out_width": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1}),
                "out_height": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1}),
                "despeckle": ("INT", {"default": 2, "min": 0, "max": 64, "step": 1}),
                "dither_amount": ("FLOAT", {"default": 0.75, "min": 0.0, "max": 1.0, "step": 0.05}),
                "custom_hex": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE")
    RETURN_NAMES = ("pixels", "preview")
    FUNCTION = "process"
    CATEGORY = "image/pixel art"

    def process(self, image, downscale_to, palette, dithering,
                downscale_filter, view_scale, smooth="mode", pixel_grid=128,
                custom_hex="", transparent_bg=False, bg_tolerance=16,
                snap_pixels=False, snap_colors=0, out_width=0, out_height=0, despeckle=2,
                dither_amount=0.75):
        palette_rgb = None if palette == "none" else parse_palette(palette, custom_hex)

        pil = _tensor_to_pil(image)

        # --- Grid-aware, crisp downscale ---------------------------------------
        # Pixel Art XL paints ~8px blocks at 1024 — i.e. a ~128px LOGICAL image.
        # To stay sharp we FIRST recover that native grid (flatten each block to
        # its dominant color, then shrink), THEN integer-reduce to the requested
        # size with nearest. A single big reduction instead samples mid-block
        # noise, which is what made small sprites look fuzzy/speckled.
        def flatten_shrink(src, target_long, resample):
            sw, sh = src.size
            if smooth != "none" and max(sw, sh) > target_long:
                block = max(3, int(round(max(sw, sh) / target_long)) | 1)   # odd >= 3
                fil = ImageFilter.ModeFilter if smooth == "mode" else ImageFilter.MedianFilter
                src = src.filter(fil(size=block))
            scl = target_long / max(sw, sh)
            return src.resize((max(1, round(sw * scl)), max(1, round(sh * scl))), resample=resample)

        if snap_pixels:
            # Hand the raw render to the pixel-snapper: it auto-detects the true
            # grid and outputs a perfect, grid-aligned sprite — REPLACING the
            # downscale (the snapper decides the native res; out_width/out_height
            # below still force the final canvas if given).
            # A palette will re-quantize after, so keep colors generous here.
            k = snap_colors or (64 if palette != "none" else 24)
            small = _snap_pixels(pil, k)
        elif downscale_to < pixel_grid:
            grid_img = flatten_shrink(pil, pixel_grid, _RESAMPLE[downscale_filter])  # -> ~128, clean
            gw, gh = grid_img.size
            scl = downscale_to / pixel_grid
            small = grid_img.resize((max(1, round(gw * scl)), max(1, round(gh * scl))),
                                    resample=Image.NEAREST)                          # integer reduce
        else:
            small = flatten_shrink(pil, min(downscale_to, pixel_grid), _RESAMPLE[downscale_filter])

        # Palette, dither, despeckle and cutout all run at the art's NATIVE pixel
        # grid (the snapper's res, or the grid-reduced size), so dither patterns
        # and speckle islands are measured in real art pixels. The exact output
        # canvas is applied last.
        if palette == "none":
            pixels = small.convert("RGB")          # keep the model's own colors
        elif dithering != "none":
            pixels = _quantize_dither(small, palette_rgb, dithering, dither_amount)
        else:
            pixels = _quantize_flat(small, palette_rgb)

        if despeckle > 0 and dithering == "none":   # dithering is deliberate "noise"
            pixels = _despeckle(pixels, despeckle)

        if transparent_bg:
            pixels = _make_transparent(pixels, bg_tolerance)

        # Force exact W x H (e.g. 320x200) — the grid reduce preserves aspect and
        # lands within a pixel, and the snapper picks its own res; nearest keeps
        # every art pixel (and its dither) a solid block.
        if out_width > 0 and out_height > 0 and pixels.size != (out_width, out_height):
            pixels = pixels.resize((out_width, out_height), Image.NEAREST)

        pw, ph = pixels.size
        preview = pixels.resize((pw * view_scale, ph * view_scale), Image.NEAREST)
        return (_pil_to_tensor(pixels), _pil_to_tensor(preview))


NODE_CLASS_MAPPINGS = {"PixelArtPalette": PixelArtPalette}
NODE_DISPLAY_NAME_MAPPINGS = {"PixelArtPalette": "Pixel Art + Palette"}
