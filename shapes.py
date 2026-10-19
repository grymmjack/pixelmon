"""pixelmon --shape: a clean, low-poly 3D model built straight from a picture's outline, no AI guessing.

  --mesh crate.png --shape extrude:0.8           its outline pushed out to a depth (boxes, guns, cards)
  --mesh desk.png --shape extrude:0.55,0.42:0.18  ... the top 42 % of it only 0.18 deep (a monitor on a desk)
  --mesh barrel.png --shape lathe                 its outline spun round its middle (barrels, tanks, jars)

A model from one picture (--mesh) has to guess the depth and the back it can't see: props come out lumpy and
leaning, thin parts split off. For a prop seen face-on the outline already holds the shape, and these two
builders make one solid, connected, straight-edged model that matches the picture exactly from the front:

  extrude DEPTH[,SHARE:DEPTH ...]   the outline as a slab DEPTH x its width deep (front and back are the
                                    picture, each side the picture's colours just inside that edge). Each
                                    SHARE:DEPTH makes the part above SHARE of its height (from the top) that
                                    deep instead, with a step down to it.
  lathe[:SEGMENTS]                  each row's half-width spun round the outline's middle (16 segments), the
                                    profile cut to the rings that matter: steps where the width jumps (a lid,
                                    a rim), tapers elsewhere, a plain cylinder one band; capped top and bottom.
                                    The picture wraps round from the front (mirrored behind).

The picture is the texture (nothing is baked), its pixels crisp: the .glb asks for NEAREST filtering, and the
outline grid's cells sit on whole art pixels. Typically 50..700 triangles, flat shaded.
Depths that work: crate 0.8, vending machine 0.7, locker 0.55, desk 0.55 with 0.42:0.18, lab bench 0.5 with
0.45:0.14, screens 0.12, guns 0.08..0.1, keycard 0.06, map 0.04.
Not for organic, irregular things with no clean face-on outline (a plant, a chair): those suit --mesh alone.

Axes are glTF's: x to the picture's right, y up, z out of the picture towards you (the model faces +z)."""
import io
import json
import math
import os
import struct

import numpy as np
from PIL import Image

ALPHA = 110          # a grid cell is solid where its (box-filtered) alpha is above this
INSET = 3 / 64       # a side's colour is taken this far inside its edge, as a share of the grid (the outline is dark)


def parse(spec):
    """'extrude:0.8,0.42:0.18' / 'lathe' / 'lathe:24' -> ("extrude", depth, [(share, depth), ...]) or
    ("lathe", segments). Raises ValueError with a plain message."""
    kind, _, rest = (spec or "").strip().lower().partition(":")
    if kind == "extrude":
        parts = [p for p in rest.split(",") if p.strip()] if rest else []
        depth = float(parts[0]) if parts else 0.5
        if not 0.005 <= depth <= 4:
            raise ValueError("--shape extrude: the depth is 0.005..4 (times the outline's width)")
        bands = []
        for p in parts[1:]:
            share, colon, d = p.partition(":")
            if not colon:
                raise ValueError(f"--shape extrude: a band is SHARE:DEPTH (like 0.42:0.18), not {p!r}")
            share, d = float(share), float(d)
            if not 0 < share < 1 or not 0.005 <= d <= 4:
                raise ValueError("--shape extrude: a band's share of the height is 0..1, its depth 0.005..4")
            bands.append((share, d))
        return "extrude", depth, bands
    if kind == "lathe":
        seg = int(rest) if rest.strip() else 16
        if not 4 <= seg <= 128:
            raise ValueError("--shape lathe: 4..128 segments round")
        return "lathe", seg
    raise ValueError(f"--shape is extrude[:DEPTH[,SHARE:DEPTH...]] or lathe[:SEGMENTS], not {spec!r}")


def outline(rgba, cells=64):
    """A cut-out picture (RGBA) -> (texture, grid): the subject cropped, padded to a square that a whole
    number of art pixels per cell divides, and which of the G x G cells over it are solid. The texture is the
    square; UVs (col / G, row / G) land on it."""
    a = np.asarray(rgba.getchannel("A"))
    ys, xs = np.nonzero(a > 24)
    if not len(xs):
        raise ValueError("found no subject in the picture (it needs a see-through background)")
    w, h = xs.max() + 1 - xs.min(), ys.max() + 1 - ys.min()
    side = max(w, h)
    cell = max(1, round(side / cells))                   # art pixels per cell: cells on whole pixels
    g = -(-(side + 2 * cell) // cell)                    # a cell of margin all round
    size = g * cell
    sq = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sq.paste(rgba.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)), ((size - w) // 2, (size - h) // 2))
    grid = np.asarray(sq.getchannel("A").resize((g, g), Image.BOX)) > ALPHA
    if not grid.any():
        raise ValueError("the subject is too faint to find its outline")
    # every see-through pixel takes its nearest solid one's colour: an outline cell is partly background, and
    # a nearest-filtered UV on a cell's edge can land on the pixel beside it
    from scipy import ndimage
    px = np.array(sq)
    _, (iy, ix) = ndimage.distance_transform_edt(px[..., 3] < 128, return_indices=True)
    px[..., :3] = px[iy, ix, :3]
    return Image.fromarray(px, "RGBA"), grid


def _quad(tris, p, uv, want):
    """Two triangles for corners p (4, in order round), each turned so its outside faces want."""
    p, uv = np.array(p, float), np.array(uv, float)
    for i in ((0, 1, 2), (0, 2, 3)):
        P, U = p[list(i)], uv[list(i)]
        if np.dot(np.cross(P[1] - P[0], P[2] - P[0]), want) < 0:
            P, U = P[[0, 2, 1]], U[[0, 2, 1]]
        tris.append((P, U))


def _runs(row):
    """[(c0, c1), ...]: the runs of True in a row, c1 exclusive."""
    out, c, n = [], 0, len(row)
    while c < n:
        if row[c]:
            c0 = c
            while c < n and row[c]:
                c += 1
            out.append((c0, c))
        else:
            c += 1
    return out


def extrude(a, depth, bands=()):
    """The outline grid a as a slab: front and back (each row's runs of solid cells; a run carrying on down
    through rows that have it, as deep, is one quad), and its edges as sides, each showing the picture's
    colour a little inside that edge, the colour bands along it running with the cells. depth is its depth
    times its width; bands, (share of its height from the top, depth), make the part above that line a
    depth of its own, with a step between."""
    G = a.shape[0]
    cols, rows = np.where(a.any(axis=0))[0], np.where(a.any(axis=1))[0]
    wide = (cols.max() - cols.min() + 1) / 2
    dz = np.full(G, depth * wide)                        # each row's half-depth
    for share, d in sorted(bands, reverse=True):         # the deepest-reaching band first, the shallower over it
        dz[:rows.min() + int(round(share * (rows.max() - rows.min() + 1)))] = d * wide
    X = lambda c: c - G / 2
    Y = lambda r: G - r
    tris = []
    inset = max(2, round(INSET * G))

    def in_r(r, c, dr):                                  # INSET cells in from a top / bottom edge (still solid)
        for _ in range(inset):
            if 0 <= r + dr < G and a[r + dr, c]:
                r += dr
        return r

    def in_c(r, c, dc):                                  # ... from a left / right edge
        for _ in range(inset):
            if 0 <= c + dc < G and a[r, c + dc]:
                c += dc
        return c

    # front and back
    open_ = {}

    def close(key, r1):
        r0 = open_.pop(key)
        c0, c1, d = key
        for z, w in ((d, 1), (-d, -1)):
            _quad(tris, [(X(c0), Y(r0), z), (X(c1), Y(r0), z), (X(c1), Y(r1), z), (X(c0), Y(r1), z)],
                  [(c0 / G, r0 / G), (c1 / G, r0 / G), (c1 / G, r1 / G), (c0 / G, r1 / G)], (0, 0, w))

    for r in range(G + 1):
        now = {(c0, c1, dz[r]) for c0, c1 in _runs(a[r])} if r < G else set()
        for key in [k for k in open_ if k not in now]:
            close(key, r)
        for key in now:
            open_.setdefault(key, r)
    # top and bottom edges: runs of cells with nothing above (below)
    empty = np.zeros(G, bool)
    for r in range(G):
        for up in (True, False):
            nb = (a[r - 1] if r > 0 else empty) if up else (a[r + 1] if r < G - 1 else empty)
            y, d = (Y(r) if up else Y(r + 1)), dz[r]
            for c0, c1 in _runs(a[r] & ~nb):
                v = (in_r(r, (c0 + c1) // 2, 1 if up else -1) + 0.5) / G
                _quad(tris, [(X(c0), y, d), (X(c1), y, d), (X(c1), y, -d), (X(c0), y, -d)],
                      [((c0 + 0.5) / G, v), ((c1 - 0.5) / G, v), ((c1 - 0.5) / G, v), ((c0 + 0.5) / G, v)],
                      (0, 1 if up else -1, 0))
            # a step, where the row above is solid but less deep: front and back
            if up and r > 0 and dz[r - 1] < d:
                for c0, c1 in _runs(a[r] & a[r - 1]):
                    v = (in_r(r, (c0 + c1) // 2, 1) + 0.5) / G
                    for s in (1, -1):
                        _quad(tris, [(X(c0), y, s * dz[r - 1]), (X(c1), y, s * dz[r - 1]), (X(c1), y, s * d), (X(c0), y, s * d)],
                              [((c0 + 0.5) / G, v), ((c1 - 0.5) / G, v), ((c1 - 0.5) / G, v), ((c0 + 0.5) / G, v)], (0, 1, 0))
    # left and right edges: runs of cells open to that side (and as deep), down a column
    for c in range(G):
        for left in (True, False):
            nb = (a[:, c - 1] if c > 0 else empty) if left else (a[:, c + 1] if c < G - 1 else empty)
            edge, x, r = a[:, c] & ~nb, (X(c) if left else X(c + 1)), 0
            while r < G:
                if not edge[r]:
                    r += 1
                    continue
                r0 = r
                while r < G and edge[r] and dz[r] == dz[r0]:
                    r += 1
                d, u = dz[r0], (in_c((r0 + r) // 2, c, 1 if left else -1) + 0.5) / G
                _quad(tris, [(x, Y(r0), d), (x, Y(r), d), (x, Y(r), -d), (x, Y(r0), -d)],
                      [(u, (r0 + 0.5) / G), (u, (r - 0.5) / G), (u, (r - 0.5) / G), (u, (r0 + 0.5) / G)],
                      (-1 if left else 1, 0, 0))
    return tris


def lathe(a, seg=16):
    """The outline grid a spun round its middle: each row's half-width a ring, the profile cut to the rings
    that matter (within half a cell), capped top and bottom. The picture wraps round from the front, as it
    is painted from in front, mirrored behind."""
    G = a.shape[0]
    cols, rows = np.where(a.any(axis=0))[0], np.where(a.any(axis=1))[0]
    cc = (cols.min() + cols.max() + 1) / 2               # the axis: the middle of the outline's columns
    rad = np.zeros(G + 1)
    for r in rows:
        cs = np.where(a[r])[0]
        rad[r] = max(cc - cs.min(), cs.max() + 1 - cc)   # the furthest solid cell either side
    prof = [(rows.min(), rad[rows.min()])]               # (row edge, radius), top down
    for r in range(rows.min(), rows.max() + 1):
        nxt = rad[r + 1] if r + 1 <= rows.max() else None
        if nxt is not None and abs(nxt - rad[r]) > 1.5:
            prof += [(r + 1, rad[r]), (r + 1, nxt)]      # a step: a lid, a rim
        else:
            prof.append((r + 1, rad[r] if nxt is None else (rad[r] + nxt) / 2))
    keep = [prof[0]]                                     # drop the rings on a straight line between their neighbours
    for i in range(1, len(prof) - 1):
        (r0, a0), (r1, a1), (r2, a2) = keep[-1], prof[i], prof[i + 1]
        if r1 in (r0, r2) or r2 == r0 or abs(a0 + (a2 - a0) * (r1 - r0) / (r2 - r0) - a1) > 0.5:
            keep.append(prof[i])
    keep.append(prof[-1])
    Y = lambda r: G - r
    ang = [2 * math.pi * k / seg for k in range(seg + 1)]

    def pt(r, rr, t):                                    # t from the front (+z), round towards the right (+x)
        return (rr * math.sin(t), Y(r), rr * math.cos(t))

    def uv(r, rr, t):                                    # 0.85: the wrap stays inside the dark outline
        return (min(max((cc + 0.85 * rr * math.sin(t)) / G, 0), 1), min(max(r / G, 0), 1))

    tris = []
    for (r0, a0), (r1, a1) in zip(keep, keep[1:]):
        if a0 < 1e-6 and a1 < 1e-6:
            continue
        for k in range(seg):
            t0, t1, tm = ang[k], ang[k + 1], (ang[k] + ang[k + 1]) / 2
            # a band faces out; a flat step faces up where it's the top of a wider part, else down
            want = (0, 1 if a1 > a0 else -1, 0) if r0 == r1 else (math.sin(tm), 0, math.cos(tm))
            _quad(tris, [pt(r0, a0, t0), pt(r0, a0, t1), pt(r1, a1, t1), pt(r1, a1, t0)],
                  [uv(r0, a0, t0), uv(r0, a0, t1), uv(r1, a1, t1), uv(r1, a1, t0)], want)
    for (r, rr), up in ((keep[0], 1), (keep[-1], -1)):  # the caps
        for k in range(seg):
            P = np.array([(0, Y(r), 0), pt(r, rr, ang[k]), pt(r, rr, ang[k + 1])], float)
            U = np.array([(cc / G, (r + 0.5 * up) / G), uv(r, rr, ang[k]), uv(r, rr, ang[k + 1])])
            if np.dot(np.cross(P[1] - P[0], P[2] - P[0]), (0, up, 0)) < 0:
                P, U = P[[0, 2, 1]], U[[0, 2, 1]]
            tris.append((P, U))
    return tris


def place(tris, height=1.0):
    """Scaled to height tall, its foot on y 0, centred on its box's middle in x and z. Sliver triangles go."""
    tris = [(P, U) for P, U in tris if np.linalg.norm(np.cross(P[1] - P[0], P[2] - P[0])) > 1e-9]
    allp = np.concatenate([p for p, _ in tris])
    lo, hi = allp.min(axis=0), allp.max(axis=0)
    s = height / (hi[1] - lo[1])
    mid = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    return [((P - mid) * s, U) for P, U in tris]


def write_gltf(tris, tex, glb, gltf=None, bin_path=None, png_path=None, name="model"):
    """The model as glb (texture inside) and, if asked, .gltf + .bin + the texture's .png (to paint): one
    mesh, flat shaded (each triangle its own corners), the texture filtered NEAREST, matte."""
    P = np.concatenate([p for p, _ in tris]).astype(np.float32)
    U = np.concatenate([u for _, u in tris]).astype(np.float32)
    N = np.repeat(np.array([np.cross(p[1] - p[0], p[2] - p[0]) for p, _ in tris], np.float32), 3, axis=0)
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
    geo = P.tobytes() + N.tobytes() + U.tobytes()
    n = len(P)
    png = io.BytesIO()
    tex.convert("RGB").save(png, "PNG")
    png = png.getvalue()

    def doc(image):
        return {
            "asset": {"version": "2.0", "generator": "pixelmon --shape"},
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
            "meshes": [{"name": name, "primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                                                      "material": 0, "mode": 4}]}],
            "materials": [{"name": name, "pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                                                 "metallicFactor": 0.0, "roughnessFactor": 1.0}}],
            "textures": [{"source": 0, "sampler": 0}],
            "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 33071, "wrapT": 33071}],   # NEAREST, clamp
            "images": [image],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": n, "type": "VEC3",
                 "min": P.min(axis=0).tolist(), "max": P.max(axis=0).tolist()},
                {"bufferView": 1, "componentType": 5126, "count": n, "type": "VEC3"},
                {"bufferView": 2, "componentType": 5126, "count": n, "type": "VEC2"}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": n * 12, "target": 34962},
                            {"buffer": 0, "byteOffset": n * 12, "byteLength": n * 12, "target": 34962},
                            {"buffer": 0, "byteOffset": n * 24, "byteLength": n * 8, "target": 34962}],
        }

    pad = lambda b, c: b + c * (-len(b) % 4)
    j = doc({"bufferView": 3, "mimeType": "image/png"})
    body = pad(geo, b"\0") + png
    j["bufferViews"].append({"buffer": 0, "byteOffset": len(pad(geo, b"\0")), "byteLength": len(png)})
    j["buffers"] = [{"byteLength": len(pad(body, b"\0"))}]
    js, body = pad(json.dumps(j, separators=(",", ":")).encode(), b" "), pad(body, b"\0")
    with open(glb, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(body)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(body), 0x004E4942) + body)
    if gltf:
        j = doc({"uri": os.path.basename(png_path)})
        j["buffers"] = [{"uri": os.path.basename(bin_path), "byteLength": len(geo)}]
        with open(gltf, "w") as f:
            json.dump(j, f)
        with open(bin_path, "wb") as f:
            f.write(geo)
        with open(png_path, "wb") as f:
            f.write(png)


def render(tris, tex, az, size=384, ss=2, elev=12):
    """A preview: the model from az degrees round (0: in front, 90: its right), a little above, the texture
    NEAREST, each face shaded by the way it faces (lit from over your left shoulder). RGBA, see-through
    round it."""
    S = size * ss
    t, e = math.radians(az), math.radians(elev)
    eye = np.array([math.sin(t) * math.cos(e), math.sin(e), math.cos(t) * math.cos(e)])   # towards you
    right = np.array([math.cos(t), 0.0, -math.sin(t)])
    up = np.cross(eye, right)
    light = eye * 0.55 + np.array([0, 0.8, 0]) - right * 0.35
    light /= np.linalg.norm(light)
    allp = np.concatenate([p for p, _ in tris])
    mid = (allp.min(axis=0) + allp.max(axis=0)) / 2
    span = np.linalg.norm(allp.max(axis=0) - allp.min(axis=0)) * 1.02
    tx = np.asarray(tex.convert("RGB"), np.float32)
    th, tw = tx.shape[:2]
    col = np.zeros((S, S, 3), np.float32)
    depth = np.full((S, S), -1e9)
    cover = np.zeros((S, S), bool)
    for P, U in tris:
        n = np.cross(P[1] - P[0], P[2] - P[0])
        if np.linalg.norm(n) < 1e-12 or np.dot(n, eye) <= 0:   # (closed shapes: a face turned away is hidden)
            continue
        n = n / np.linalg.norm(n)
        Q = P - mid
        sx, sy, d = (Q @ right / span + 0.5) * S, (0.5 - (Q @ up) / span) * S, Q @ eye
        x0, x1 = max(int(math.floor(sx.min())), 0), min(int(math.ceil(sx.max())), S - 1)
        y0, y1 = max(int(math.floor(sy.min())), 0), min(int(math.ceil(sy.max())), S - 1)
        if x0 > x1 or y0 > y1:
            continue
        den = (sy[1] - sy[2]) * (sx[0] - sx[2]) + (sx[2] - sx[1]) * (sy[0] - sy[2])
        if abs(den) < 1e-12:
            continue
        gy, gx = np.mgrid[y0:y1 + 1, x0:x1 + 1] + 0.5
        w0 = ((sy[1] - sy[2]) * (gx - sx[2]) + (sx[2] - sx[1]) * (gy - sy[2])) / den
        w1 = ((sy[2] - sy[0]) * (gx - sx[2]) + (sx[0] - sx[2]) * (gy - sy[2])) / den
        w2 = 1 - w0 - w1
        z = w0 * d[0] + w1 * d[1] + w2 * d[2]
        win = depth[y0:y1 + 1, x0:x1 + 1]
        hit = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (z > win)
        if not hit.any():
            continue
        u = w0 * U[0, 0] + w1 * U[1, 0] + w2 * U[2, 0]
        v = w0 * U[0, 1] + w1 * U[1, 1] + w2 * U[2, 1]
        texel = tx[np.clip((v * th).astype(int), 0, th - 1), np.clip((u * tw).astype(int), 0, tw - 1)]
        shade = 0.62 + 0.55 * max(0.0, float(np.dot(n, light)))
        win[hit] = z[hit]
        col[y0:y1 + 1, x0:x1 + 1][hit] = texel[hit] * shade
        cover[y0:y1 + 1, x0:x1 + 1][hit] = True
    c, k = col.reshape(size, ss, size, ss, 3), cover.reshape(size, ss, size, ss)
    cnt = k.sum(axis=(1, 3))
    out = np.zeros((size, size, 4), np.uint8)
    out[..., :3] = np.clip((c * k[..., None]).sum(axis=(1, 3)) / np.maximum(cnt, 1)[..., None], 0, 255)
    out[..., 3] = np.clip(cnt * 255 // (ss * ss), 0, 255)
    return Image.fromarray(out, "RGBA")


def build(spec, rgba, cells=64, height=1.0):
    """--shape SPEC of a cut-out picture -> (triangles, texture): the model, placed, and the picture it wears."""
    kind = parse(spec)
    tex, grid = outline(rgba, cells)
    tris = extrude(grid, kind[1], kind[2]) if kind[0] == "extrude" else lathe(grid, kind[1])
    return place(tris, height), tex
