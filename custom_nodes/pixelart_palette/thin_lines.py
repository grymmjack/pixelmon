"""1-pixel outlines: thin the thick strokes AI renders leave (2–3 px black outlines after downscaling)
down to single-pixel lines, the way a pixel artist draws them.

At the art's native grid (after the palette lock):
  1. the line color is the darkest color in the image (EGA black, usually) — or one you pass;
  2. its pixels are split into solid FILLS (regions thicker than `max_width`: shadows, doorways, night sky)
     and STROKES (everything thinner) — only strokes are touched;
  3. strokes are skeletonized to a connected 1-px center line (Zhang–Suen thinning);
  4. the freed pixels take the most common neighbouring non-line color, filled in from the edges.
"""
import numpy as np


def _zhang_suen(mask):
    """Zhang–Suen thinning of a boolean mask -> 1-px, 8-connected skeleton (pure numpy)."""
    img = np.pad(mask.astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            p2, p3, p4 = img[:-2, 1:-1], img[:-2, 2:], img[1:-1, 2:]
            p5, p6, p7 = img[2:, 2:], img[2:, 1:-1], img[2:, :-2]
            p8, p9 = img[1:-1, :-2], img[:-2, :-2]
            c = img[1:-1, 1:-1]
            nb = [p2, p3, p4, p5, p6, p7, p8, p9]
            b = sum(n.astype(np.int32) for n in nb)
            a = sum(((nb[i] == 0) & (nb[(i + 1) % 8] == 1)).astype(np.int32) for i in range(8))
            if step == 0:
                cond = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                cond = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            rm = (c == 1) & (b >= 2) & (b <= 6) & (a == 1) & cond
            if rm.any():
                img[1:-1, 1:-1][rm] = 0
                changed = True
        if not changed:
            return img[1:-1, 1:-1].astype(bool)


def _opening(mask, k):
    """Binary opening with a k x k square (pure numpy): keeps only regions a k x k block fits inside."""
    if k <= 1:
        return mask.copy()
    h, w = mask.shape
    pad = np.pad(mask, k, constant_values=False)
    er = np.ones((h + 2 * k, w + 2 * k), dtype=bool)
    for dy in range(k):
        for dx in range(k):
            er &= np.roll(np.roll(pad, -dy, 0), -dx, 1)
    di = np.zeros_like(er)
    for dy in range(k):
        for dx in range(k):
            di |= np.roll(np.roll(er, dy, 0), dx, 1)
    return di[k:-k, k:-k]


def thin_lines(pil_rgb, max_width=4, line_color=None):
    """Return pil_rgb with strokes of the line color thinned to 1 px. max_width: anything thicker is a
    solid fill and is left alone."""
    from PIL import Image
    arr = np.asarray(pil_rgb.convert("RGB")).astype(np.int32)
    h, w, _ = arr.shape
    ids = (arr[..., 0] << 16) | (arr[..., 1] << 8) | arr[..., 2]
    if line_color is None:
        cols = np.unique(ids)
        lum = ((cols >> 16) & 255) * 299 + ((cols >> 8) & 255) * 587 + (cols & 255) * 114
        line_id = int(cols[lum.argmin()])
        if lum.min() > 70000:                     # nothing dark enough to be an outline color
            return pil_rgb.copy()
    else:
        r, g, b = line_color
        line_id = (r << 16) | (g << 8) | b
    line = ids == line_id
    fills = _opening(line, max_width + 1)         # solid dark areas: keep
    strokes = line & ~fills
    keep = _zhang_suen(strokes) | fills
    free = line & ~keep                           # pixels that become background
    out = ids.copy()
    out[free] = -1
    # fill freed pixels from the outside in with the most common non-line neighbour color
    for _ in range(max_width * 2 + 2):
        todo = np.argwhere(out == -1)
        if not len(todo):
            break
        new = {}
        for y, x in todo:
            nb = out[max(0, y - 1):y + 2, max(0, x - 1):x + 2].ravel()
            nb = nb[(nb != -1) & (nb != line_id)]
            if len(nb):
                v, c = np.unique(nb, return_counts=True)
                new[(y, x)] = int(v[c.argmax()])
        if not new:
            break
        for (y, x), v in new.items():
            out[y, x] = v
    out[out == -1] = line_id                      # nothing to borrow from: leave it as line
    rgb = np.stack([(out >> 16) & 255, (out >> 8) & 255, out & 255], -1).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")
