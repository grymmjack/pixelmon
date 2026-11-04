"""Contact sheets: several finished pictures on one labelled page, to pick a take at a glance.

--sheet saves NAME_sheet.png beside a batch's pictures (seed under each). Pixel art is enlarged by a whole
number (crisp), paintings shrunk smoothly; transparent pictures sit on a checkerboard.
"""
import math
import os
import re

from PIL import Image, ImageDraw, ImageFont

SEED_RE = re.compile(r"_s(\d+)_")
BG, INK, DIM = (24, 24, 28), (236, 236, 240), (150, 150, 160)


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1: one small bitmap size
        return ImageFont.load_default()


def checker(w, h, sq=8):
    im = Image.new("RGB", (w, h), (62, 62, 68))
    d = ImageDraw.Draw(im)
    for y in range(0, h, sq):
        for x in range((y // sq) % 2 * sq, w, sq * 2):
            d.rectangle((x, y, x + sq - 1, y + sq - 1), fill=(84, 84, 92))
    return im


def fit(im, cell):
    """The picture sized into a cell x cell box: small pixel art by a whole number, others smoothly."""
    w, h = im.size
    k = cell / max(w, h)
    if k >= 2:
        n = int(k)
        return im.resize((w * n, h * n), Image.NEAREST)
    return im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)


def label_of(path):
    m = SEED_RE.search(os.path.basename(path))
    return f"seed {m.group(1)}" if m else os.path.splitext(os.path.basename(path))[0][-28:]


def contact_sheet(items, out, title="", cell=256, cols=None):
    """items: [path] or [(path, label)]. Writes out (PNG) and returns it."""
    items = [(it, label_of(it)) if isinstance(it, str) else it for it in items]
    items = [(p, lab) for p, lab in items if p and os.path.isfile(p)]
    if not items:
        return None
    n = len(items)
    if not cols:                            # fewest empty cells, then a little wider than tall
        c0 = math.ceil(math.sqrt(n))
        cols = min(range(c0, min(n, c0 + 2) + 1), key=lambda c: (-n % c, abs(c / math.ceil(n / c) - 1.4)))
    rows = math.ceil(n / cols)
    pad, lab_h = 10, 22
    head = 34 if title else 0
    W = cols * (cell + pad) + pad
    H = head + rows * (cell + lab_h + pad) + pad
    sheet = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(sheet)
    if title:
        d.text((pad, 9), title[:int(W / 8)], fill=INK, font=font(16))
    f = font(13)
    for i, (p, lab) in enumerate(items):
        x = pad + (i % cols) * (cell + pad)
        y = head + pad + (i // cols) * (cell + lab_h + pad)
        try:
            im = Image.open(p)
            im.load()
        except OSError:
            d.text((x + 4, y + cell // 2), "unreadable", fill=DIM, font=f)
            continue
        im = fit(im.convert("RGBA"), cell)
        bx, by = x + (cell - im.width) // 2, y + (cell - im.height) // 2
        if im.getextrema()[3][0] < 255:     # see-through somewhere: show it on a checkerboard
            sheet.paste(checker(im.width, im.height), (bx, by))
        sheet.paste(im, (bx, by), im)
        tw = d.textlength(lab, font=f)
        d.text((x + (cell - tw) / 2, y + cell + 4), lab, fill=INK, font=f)
    sheet.save(out)
    return out


def free_name(path):
    """path, or path with _2, _3 ... before the extension if it's taken."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    k = 2
    while os.path.exists(f"{stem}_{k}{ext}"):
        k += 1
    return f"{stem}_{k}{ext}"
