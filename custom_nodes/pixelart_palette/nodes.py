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


def _quantize_dither(small_rgb, palette_rgb):
    """Floyd-Steinberg dithering against the fixed palette (via Pillow)."""
    pal_img = Image.new("P", (1, 1))
    flat = []
    for rgb in palette_rgb:
        flat.extend(rgb)
    # Pad to 256 entries by repeating the palette so no stray colors sneak in.
    while len(flat) < 256 * 3:
        flat.extend(flat[: min(len(flat), 256 * 3 - len(flat))])
    pal_img.putpalette(flat[: 256 * 3])
    q = small_rgb.convert("RGB").quantize(palette=pal_img,
                                          dither=Image.Dither.FLOYDSTEINBERG)
    return q.convert("RGB")


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
                "downscale_to": ("INT", {"default": 128, "min": 8, "max": 256, "step": 1}),
                "palette": (palette_names,),
                "dithering": (["none", "floyd-steinberg"],),
                "downscale_filter": (list(_RESAMPLE.keys()), {"default": "nearest"}),
                "view_scale": ("INT", {"default": 8, "min": 1, "max": 32, "step": 1}),
            },
            "optional": {
                "smooth": (["mode", "median", "none"], {"default": "mode"}),
                "pixel_grid": ("INT", {"default": 128, "min": 32, "max": 256, "step": 8}),
                "transparent_bg": ("BOOLEAN", {"default": False}),
                "bg_tolerance": ("INT", {"default": 16, "min": 0, "max": 128, "step": 1}),
                "snap_pixels": ("BOOLEAN", {"default": False}),
                "snap_colors": ("INT", {"default": 0, "min": 0, "max": 256, "step": 1}),
                "out_width": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1}),
                "out_height": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1}),
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
                snap_pixels=False, snap_colors=0, out_width=0, out_height=0):
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
            # downscale (so --size is ignored; the snapper decides the real res).
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

        # Force exact W x H (e.g. 32x48) — the grid reduce above preserves aspect
        # and lands within a pixel; this snaps to the precise size. (snap_pixels
        # auto-sizes, so it opts out.)
        if not snap_pixels and out_width > 0 and out_height > 0:
            small = small.resize((out_width, out_height), Image.NEAREST)

        if palette == "none":
            pixels = small.convert("RGB")          # keep the model's own colors
        elif dithering == "floyd-steinberg":
            pixels = _quantize_dither(small, palette_rgb)
        else:
            pixels = _quantize_flat(small, palette_rgb)

        if transparent_bg:
            pixels = _make_transparent(pixels, bg_tolerance)

        pw, ph = pixels.size
        preview = pixels.resize((pw * view_scale, ph * view_scale), Image.NEAREST)
        return (_pil_to_tensor(pixels), _pil_to_tensor(preview))


NODE_CLASS_MAPPINGS = {"PixelArtPalette": PixelArtPalette}
NODE_DISPLAY_NAME_MAPPINGS = {"PixelArtPalette": "Pixel Art + Palette"}
