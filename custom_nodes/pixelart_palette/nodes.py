"""PixelArtPalette — turn a 512px SD render into a true, palette-locked sprite.

Pipeline:  downscale (real pixels) -> quantize to palette -> upscale for viewing.

Outputs two images:
  * "pixels"  — the true small sprite (e.g. 64x64). SaveImage this for real assets.
  * "preview" — the same image upscaled with nearest-neighbour, just so you can
                actually see it. PreviewImage / SaveImage this to eyeball results.
"""
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


class PixelArtPalette:
    @classmethod
    def INPUT_TYPES(cls):
        palette_names = ["none"] + list(ALL_PALETTES.keys()) + ["Custom"]
        return {
            "required": {
                "image": ("IMAGE",),
                "downscale_to": ("INT", {"default": 64, "min": 8, "max": 256, "step": 1}),
                "palette": (palette_names,),
                "dithering": (["none", "floyd-steinberg"],),
                "downscale_filter": (list(_RESAMPLE.keys()), {"default": "box (area average)"}),
                "view_scale": ("INT", {"default": 8, "min": 1, "max": 32, "step": 1}),
            },
            "optional": {
                "smooth": (["mode", "median", "none"], {"default": "mode"}),
                "transparent_bg": ("BOOLEAN", {"default": False}),
                "bg_tolerance": ("INT", {"default": 16, "min": 0, "max": 128, "step": 1}),
                "custom_hex": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE")
    RETURN_NAMES = ("pixels", "preview")
    FUNCTION = "process"
    CATEGORY = "image/pixel art"

    def process(self, image, downscale_to, palette, dithering,
                downscale_filter, view_scale, smooth="mode", custom_hex="",
                transparent_bg=False, bg_tolerance=16):
        palette_rgb = None if palette == "none" else parse_palette(palette, custom_hex)

        pil = _tensor_to_pil(image)
        w, h = pil.size

        # Flatten soft gradients/noise into solid regions BEFORE downscaling, so
        # near-flat backgrounds don't shatter into speckle when quantized. The
        # window is ~one output-pixel block, so each block collapses to its
        # dominant ('mode') or middle ('median') color while edges survive.
        if smooth != "none":
            block = max(3, int(round(max(w, h) / max(1, downscale_to))) | 1)  # odd >=3
            f = ImageFilter.ModeFilter if smooth == "mode" else ImageFilter.MedianFilter
            pil = pil.filter(f(size=block))

        scale = downscale_to / max(w, h)
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))

        small = pil.resize((tw, th), resample=_RESAMPLE[downscale_filter])

        if palette == "none":
            pixels = small.convert("RGB")          # keep the model's own colors
        elif dithering == "floyd-steinberg":
            pixels = _quantize_dither(small, palette_rgb)
        else:
            pixels = _quantize_flat(small, palette_rgb)

        if transparent_bg:
            pixels = _make_transparent(pixels, bg_tolerance)

        preview = pixels.resize((tw * view_scale, th * view_scale), Image.NEAREST)
        return (_pil_to_tensor(pixels), _pil_to_tensor(preview))


NODE_CLASS_MAPPINGS = {"PixelArtPalette": PixelArtPalette}
NODE_DISPLAY_NAME_MAPPINGS = {"PixelArtPalette": "Pixel Art + Palette"}
