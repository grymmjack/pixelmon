"""Pixel-art angle snapping: redraw every color region's outline so its edges follow the
clean integer-ratio angles pixel artists use (the same set as DRAW's pixel-art angle snap):

    0:1, 5:1, 4:1, 3:1, 2:1, 1:1, 1:2, 1:3, 1:4, 1:5, 1:0   (run:rise, all four quadrants)

A line whose slope is a ratio p:q rasterizes with a period of q pixels, so every edge steps in
even, regular runs (2:1 = two across, one down, forever) instead of the wandering, uneven
stair-steps an AI render leaves after downscaling.

How: at the art's native resolution (after the palette lock), each color's connected regions are
traced, simplified to polygons (Douglas-Peucker), and every polygon edge is snapped to the nearest
allowed angle; corners move to where consecutive snapped edges meet. Regions are then repainted
largest-first (holes included), and small or thin details — windows, highlights, 1px lines, dither —
are kept exactly as they were and painted back on top. Finally, any change that isn't a thin sliver
along an edge (i.e. the snap altered a shape rather than straightening an edge) is reverted.
"""
import math

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - reported to the user by the node
    cv2 = None

# Allowed edge directions per GRID type, as (run, rise) for the first quadrant, mirrored into the
# other three. "pixel" is DRAW's pixel-art angle snap set; the rest restrict edges to one grid's geometry
# (the grid names match DRAW's grid modes, plus triangle).
ANGLE_SETS = {
    "pixel":     [(1, 0), (5, 1), (4, 1), (3, 1), (2, 1), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (0, 1)],
    "square":    [(1, 0), (0, 1)],                  # orthogonal only
    "diagonal":  [(1, 0), (1, 1), (0, 1)],          # 8 directions
    "isometric": [(2, 1), (0, 1)],                  # 2:1 pixel iso (26.57°) + verticals
    "hex":       [(1, 0), (1, 1)],                  # DRAW's flat-top hex: flat tops, 45° sides
    "triangle":  [(1, 0), (1, 2)],                  # triangular lattice: flats + the pixel ≈60° (1:2)
}
RATIOS = ANGLE_SETS["pixel"]
_DIRS_CACHE = {}


def _dirs(angle_set):
    if angle_set not in _DIRS_CACHE:
        out = []
        for run, rise in ANGLE_SETS.get(angle_set, RATIOS):
            for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
                v = (sx * run, sy * rise)
                n = math.hypot(*v)
                d = (v[0] / n, v[1] / n)
                if d not in out:
                    out.append(d)
        _DIRS_CACHE[angle_set] = np.array(out)
    return _DIRS_CACHE[angle_set]


def _snap_dir(dx, dy, dirs):
    """Unit vector of the allowed angle closest to (dx, dy) — sign-agnostic (an edge is a line)."""
    n = math.hypot(dx, dy) or 1.0
    u = np.array([dx / n, dy / n])
    return dirs[int(np.argmax(np.abs(dirs @ u)))]


def _intersect(p1, d1, p2, d2):
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < 1e-6:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / cross
    return (p1[0] + t * d1[0], p1[1] + t * d1[1])


def _snap_polygon(pts, max_shift, dirs):
    """pts: (N, 2) float polygon. Snap each edge's angle; corners = intersections of neighbours."""
    n = len(pts)
    if n < 3:
        return pts
    lines = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        lines.append(((a + b) / 2.0, _snap_dir(*(b - a), dirs)))
    out = []
    for i in range(n):
        (m0, d0), (m1, d1) = lines[i - 1], lines[i]
        q = _intersect(m0, d0, m1, d1)
        v = pts[i]
        if q is None:                       # same snapped angle twice: the corner just melts away
            continue
        if math.hypot(q[0] - v[0], q[1] - v[1]) > max_shift:
            out.append(v)                   # a far-away intersection would distort the shape: keep it
        else:
            out.append(np.array(q))
    return np.array(out) if len(out) >= 3 else pts


def snap_angles(pil_rgb, strength=1.0, keep_area=10, angle_set="pixel"):
    """Return a copy of pil_rgb (native-resolution, few colors) with region outlines snapped to
    pixel-art angles. strength ~ how much wobble gets straightened (the polygon tolerance, px)."""
    if cv2 is None:
        raise RuntimeError("pixel-art angle snap needs OpenCV:  pip install opencv-python-headless  "
                           "(into ComfyUI's venv on the render server)")
    from PIL import Image
    arr = np.asarray(pil_rgb.convert("RGB"))
    h, w, _ = arr.shape
    flat = arr.reshape(-1, 3)
    colors, inv = np.unique(flat, axis=0, return_inverse=True)
    idx = inv.reshape(h, w)
    eps = max(0.5, float(strength))
    dirs = _dirs(angle_set)
    # a restrictive grid (square / iso / hex …) changes shapes on purpose, so allow bigger corner moves
    free = angle_set == "pixel"

    big, keep = [], []                      # (area, color index, component mask)
    for c in range(len(colors)):
        mask = (idx == c).astype(np.uint8)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for k in range(1, n):
            area = int(stats[k, cv2.CC_STAT_AREA])
            comp = (lab == k)
            bw, bh = stats[k, cv2.CC_STAT_WIDTH], stats[k, cv2.CC_STAT_HEIGHT]
            # thin: mostly 1–2 px wide (lines, outlines, dither) — polygons would mangle those
            thin = area < 2.2 * max(bw, bh) or min(bw, bh) <= 2
            (keep if area < keep_area or thin else big).append((area, c, comp))

    out = np.zeros((h, w), dtype=np.int32)
    out[:] = np.bincount(idx.ravel()).argmax()          # background = most common color
    for area, c, comp in sorted(big, key=lambda t: -t[0]):
        m = comp.astype(np.uint8)
        contours, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        layer = np.zeros((h, w), dtype=np.uint8)
        polys = []
        for ci, cnt in enumerate(contours):
            if len(cnt) < 3:
                continue
            if cv2.contourArea(cnt) < 48:     # small windows/holes: keep their exact outline
                snapped = cnt.reshape(-1, 2).astype(np.float64)
            else:
                approx = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2).astype(np.float64)
                snapped = (_snap_polygon(approx, max_shift=(eps * 2 + 1.5) if free else (eps * 4 + 3), dirs=dirs)
                           if len(approx) >= 4 else cnt.reshape(-1, 2).astype(np.float64))
            is_hole = hier is not None and hier[0][ci][3] >= 0
            polys.append((is_hole, np.round(snapped * 16).astype(np.int32)))   # 4-bit subpixel (shift=4)
        for is_hole, poly in polys:
            if not is_hole:
                cv2.fillPoly(layer, [poly], 1, lineType=cv2.LINE_8, shift=4)
        for is_hole, poly in polys:
            if is_hole:
                cv2.fillPoly(layer, [poly], 0, lineType=cv2.LINE_8, shift=4)
        out[layer.astype(bool)] = c
    for area, c, comp in sorted(keep, key=lambda t: -t[0]):   # details back on top, untouched
        out[comp] = c
    # Keep the snap only where it STRAIGHTENS an edge (thin slivers of change along a boundary);
    # where it CHANGED A SHAPE (a solid blob of changed pixels — a lost timber line, a bent roof),
    # put the original pixels back.
    diff = (out != idx).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(diff, connectivity=8)
    for k in range(1, n):
        area = int(stats[k, cv2.CC_STAT_AREA])
        span = max(stats[k, cv2.CC_STAT_WIDTH], stats[k, cv2.CC_STAT_HEIGHT])
        if area >= (4 if free else 24) and area / span > (1.6 if free else 3.0):   # thick, not a sliver
            m = lab == k
            out[m] = idx[m]
    return Image.fromarray(colors[out].astype(np.uint8), "RGB")
