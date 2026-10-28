"""The colours a picture uses, shared by the CLI (--palette-from) and pixelmon-gui (Use this palette, .gpl export).

One copy on purpose: the GUI's "use this picture's palette" and the CLI's --palette-from must pick the same colours
from the same file, or a sprite made in one won't match a sprite made in the other. Pillow only; no ComfyUI.
"""
import os

_COLOR_CACHE = {}   # (path, mtime) -> (n, ['#rrggbb', ...])


def image_colors(path):
    """Colors used by an image, most-used first: (n, ['#rrggbb', …]) — or ('>255', []) when there are more.
    Fully transparent pixels don't count. Cached per file + mtime."""
    key = (path, os.path.getmtime(path))
    if key not in _COLOR_CACHE:
        try:
            from PIL import Image
        except ImportError:
            return None, []
        im = Image.open(path).convert("RGBA")
        cols = im.getcolors(maxcolors=4096)
        if cols is not None:
            cols = [(c, rgba) for c, rgba in cols if rgba[3] > 0]
        if cols is None or len(cols) > 255:
            _COLOR_CACHE[key] = (">255", [])
        else:
            cols.sort(key=lambda t: -t[0])
            _COLOR_CACHE[key] = (len(cols), ["#%02x%02x%02x" % rgba[:3] for _, rgba in cols])
    return _COLOR_CACHE[key]


def picture_palette(path, cap=32, exact=64):
    """The colors a picture uses: all of them when it has at most `exact` (pixel art), else its `cap` main colors
    (median cut)."""
    n, colors = image_colors(path)
    if n != ">255" and colors and len(colors) <= exact:
        return colors
    from PIL import Image
    q = Image.open(path).convert("RGB").quantize(colors=cap, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[: cap * 3]
    used = sorted({c for _, c in (q.getcolors(cap) or [])})
    return ["#%02x%02x%02x" % tuple(pal[i * 3: i * 3 + 3]) for i in used]


def gpl_text(name, colors):
    """GIMP palette (.gpl) — readable by GIMP, Aseprite, Krita, Inkscape, …"""
    lines = ["GIMP Palette", f"Name: {name}", "Columns: 16", "#"]
    for hx in colors:
        r, g, b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
        lines.append(f"{r:3d} {g:3d} {b:3d}\t{hx.upper()}")
    return "\n".join(lines) + "\n"
