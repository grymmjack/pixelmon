"""Read layered art files for the LAB: a layer named "mask" becomes the edit mask, a layer named "art"
(or, without one, every other visible layer flattened) becomes the input picture.

Supported:
  .draw  DRAW's native file (a PNG with a "drAw" chunk holding the layers; format in DRAW's TOOLS/DRW.BM)
  .ora   OpenRaster (GIMP: File > Export As… .ora · Krita · MyPaint)
  .psd   Photoshop (GIMP, Krita, Photoshop, Photopea…)

Layer names match case-insensitively; "mask" / "edit" / "inpaint" count as the mask, "art" / "flat" / "base" as the art.
"""
import io
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

from PIL import Image

MASK_NAMES = ("mask", "edit", "inpaint")
ART_NAMES = ("art", "flat", "base")


class Layer:
    def __init__(self, name, img, visible=True, z=0, is_group=False):
        self.name, self.img, self.visible, self.z, self.is_group = name, img, visible, z, is_group


def _read_draw(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a .draw file (it should start like a PNG)")
    i, payload = 8, None
    while i + 8 <= len(data):
        n, t = struct.unpack(">I4s", data[i:i + 8])
        if t == b"drAw":
            payload = data[i + 8:i + 8 + n]
        i += 12 + n
    if payload is None:
        raise ValueError("this .draw file has no layers (no drAw chunk) — it's a flat picture; save it from DRAW")
    raw = zlib.decompress(payload[6:])                       # [2] chunk version, [4] size, then deflate
    p = 0

    def take(fmt):
        nonlocal p
        v = struct.unpack_from("<" + fmt, raw, p)
        p += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]
    if raw[:4] != b"DRW1":
        raise ValueError("unknown .draw layout")
    p = 4
    ver = take("h")
    w, h = take("i"), take("i")
    pal = take("h")
    p += 4 * max(0, pal) + 4                                   # colors + fg/bg index
    count = take("h")
    take("h")                                                   # current layer
    layers = []
    for _ in range(max(0, count)):
        name = raw[p:p + (64 if ver >= 28 else 16)].split(b"\0")[0].decode("latin-1").strip()
        p += 64 if ver >= 28 else 16
        visible, _opacity, z = take("h"), take("h"), take("h")
        if ver >= 2:
            p += 4                                              # blend mode, opacity lock
        if ver >= 7:
            p += 4                                              # history id
        ltype = 0
        if ver >= 24:
            ltype = take("h")
            p += 6                                              # parent group, collapsed, pass-through
        if ver >= 25:
            p += 4 + 2 + 4 + 4 + 2 + 2                          # symbol parent, dirty, scale x/y, offset x/y
        if ver >= 27:
            p += 2                                              # symbol rotation
        if ltype == 2:                                          # group: no pixels
            layers.append(Layer(name, None, bool(visible), z, True))
            continue
        n = w * h * 4
        img = Image.frombuffer("RGBA", (w, h), raw[p:p + n], "raw", "BGRA", 0, 1).copy()   # QB64 &HAARRGGBB, little-endian
        p += n
        layers.append(Layer(name, img, bool(visible), z))
    return (w, h), layers


def _read_ora(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("stack.xml"))
    img = root if root.tag == "image" else root.find("image")
    w, h = int(img.get("w")), int(img.get("h"))
    layers, zi = [], 0

    def walk(stack, parent_visible=True):
        nonlocal zi
        for el in reversed(list(stack)):                        # stack.xml lists top first
            vis = parent_visible and el.get("visibility", "visible") != "hidden"
            if el.tag == "stack":
                walk(el, vis)
            elif el.tag == "layer" and el.get("src"):
                src = Image.open(io.BytesIO(z.read(el.get("src")))).convert("RGBA")
                full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                full.paste(src, (int(float(el.get("x", 0))), int(float(el.get("y", 0)))))
                layers.append(Layer(el.get("name", ""), full, vis, zi))
                zi += 1
    walk(img)
    return (w, h), layers


def _unpackbits(src, n):
    out, i = bytearray(), 0
    while len(out) < n and i < len(src):
        c = src[i]; i += 1
        if c < 128:
            out += src[i:i + c + 1]; i += c + 1
        elif c > 128:
            out += bytes([src[i]]) * (257 - c); i += 1
    return bytes(out[:n])


def _read_psd(data):
    """Layers of a Photoshop file (8-bit RGB; raw or RLE channels — what Photoshop, GIMP, Krita, Photopea write)."""
    if data[:4] != b"8BPS":
        raise ValueError("not a .psd file")
    ver, nch, h, w, depth, mode = struct.unpack(">H6xHIIHH", data[4:26])
    if ver != 1 or depth != 8 or mode != 3:
        raise ValueError("only 8-bit RGB .psd files can be read (save it as 8-bit RGB)")
    p = 26
    p += 4 + struct.unpack(">I", data[p:p + 4])[0]                              # color mode data
    p += 4 + struct.unpack(">I", data[p:p + 4])[0]                              # image resources
    lmi_len = struct.unpack(">I", data[p:p + 4])[0]; p += 4
    if not lmi_len:
        raise ValueError("no layers found in that .psd")
    li_len = struct.unpack(">I", data[p:p + 4])[0]; p += 4
    if not li_len:
        raise ValueError("no layers found in that .psd")
    count = abs(struct.unpack(">h", data[p:p + 2])[0]); p += 2
    recs = []
    for _ in range(count):
        top, left, bottom, right, nc = struct.unpack(">iiiiH", data[p:p + 18]); p += 18
        chans = [struct.unpack(">hI", data[p + 6 * k:p + 6 * k + 6]) for k in range(nc)]; p += 6 * nc
        opacity, _clip, flags = data[p + 8], data[p + 9], data[p + 10]; p += 12
        extra_len = struct.unpack(">I", data[p:p + 4])[0]; q = p + 4; p = q + extra_len
        q += 4 + struct.unpack(">I", data[q:q + 4])[0]                          # layer mask data
        q += 4 + struct.unpack(">I", data[q:q + 4])[0]                          # blending ranges
        name = data[q + 1:q + 1 + data[q]].decode("latin-1", "replace")
        uni = data.find(b"8BIMluni", q, p)                                      # the unicode name, when there is one
        if uni >= 0:
            n = struct.unpack(">I", data[uni + 12:uni + 16])[0]
            name = data[uni + 16:uni + 16 + 2 * n].decode("utf-16-be", "replace").rstrip("\0") or name
        recs.append((name, top, left, bottom, right, chans, opacity, flags))
    layers = []
    for z, (name, top, left, bottom, right, chans, opacity, flags) in enumerate(recs):
        lw, lh = right - left, bottom - top
        planes = {}
        for cid, clen in chans:
            comp = struct.unpack(">H", data[p:p + 2])[0]
            body = data[p + 2:p + clen]; p += clen
            if lw <= 0 or lh <= 0:
                continue
            if comp == 0:
                raw = body[:lw * lh]
            elif comp == 1:
                counts = struct.unpack(f">{lh}H", body[:2 * lh]); o, rows = 2 * lh, []
                for c in counts:
                    rows.append(_unpackbits(body[o:o + c], lw)); o += c
                raw = b"".join(rows)
            else:
                raise ValueError("this .psd uses ZIP-compressed layers; re-save it with RLE (the default) or uncompressed")
            planes[cid] = Image.frombytes("L", (lw, lh), raw.ljust(lw * lh, b"\0"))
        if lw <= 0 or lh <= 0 or not all(c in planes for c in (0, 1, 2)):
            layers.append(Layer(name, None, True, z, True))                       # a group marker / empty layer
            continue
        a = planes.get(-1, Image.new("L", (lw, lh), 255))
        if opacity < 255:
            a = a.point(lambda v, o=opacity: v * o // 255)
        full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        full.paste(Image.merge("RGBA", (planes[0], planes[1], planes[2], a)), (left, top))
        layers.append(Layer(name, full, not flags & 2, z))
    if not any(l.img is not None for l in layers):
        raise ValueError("no layers found in that .psd")
    return (w, h), layers


def read_layered(filename, data):
    """-> (art RGBA image, mask L image or None, info dict)"""
    ext = filename.lower().rsplit(".", 1)[-1]
    reader = {"draw": _read_draw, "ora": _read_ora, "psd": _read_psd}.get(ext)
    if not reader:
        raise ValueError("layered files: .draw (DRAW), .ora (GIMP / Krita export) or .psd")
    (w, h), layers = reader(data)
    named = lambda names: next((l for l in layers if not l.is_group and l.name.strip().lower() in names), None)
    mask_l, art_l = named(MASK_NAMES), named(ART_NAMES)
    if art_l:
        art = art_l.img
    else:                                                        # flatten everything visible except the mask
        art = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for l in sorted(layers, key=lambda l: l.z):
            if l.img is not None and l.visible and l is not mask_l:
                art = Image.alpha_composite(art, l.img)
    mask = None
    if mask_l:                                                   # painted = redraw: opaque and not black
        from PIL import ImageChops
        opaque = mask_l.img.split()[3].point(lambda v: 255 if v > 127 else 0)
        bright = mask_l.img.convert("L").point(lambda v: 255 if v > 40 else 0)
        mask = ImageChops.multiply(opaque, bright)
    info = {"layers": [l.name for l in layers if not l.is_group], "mask_layer": mask_l.name if mask_l else None,
            "art_layer": art_l.name if art_l else None, "size": [w, h]}
    return art, mask, info


# ---------------------------------------------------------------------------------------------------------
# Writers: several RGBA images as layers of one file (the Analyze tab's export).
# `layers` = [{"name", "img" (RGBA, all the same size), "opacity" 0..1, "visible"}], BOTTOM layer first.
# ---------------------------------------------------------------------------------------------------------
def _flatten(layers):
    w, h = layers[0]["img"].size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for l in layers:
        if l.get("visible", True):
            im = l["img"]
            if l.get("opacity", 1) < 1:
                im = im.copy()
                im.putalpha(im.getchannel("A").point(lambda v, o=l["opacity"]: int(v * o)))
            out = Image.alpha_composite(out, im)
    return out


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def write_ora(layers):
    """OpenRaster: GIMP, Krita and MyPaint open it with all layers."""
    from xml.sax.saxutils import quoteattr
    w, h = layers[0]["img"].size
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "image/openraster", compress_type=zipfile.ZIP_STORED)   # must be first, stored
        rows = []
        for i, l in enumerate(layers):
            z.writestr(f"data/layer{i}.png", _png(l["img"]))
            rows.append(f'<layer name={quoteattr(l["name"])} src="data/layer{i}.png" x="0" y="0" '
                        f'opacity="{l.get("opacity", 1):.3f}" visibility="{"visible" if l.get("visible", True) else "hidden"}"/>')
        z.writestr("stack.xml", f'<?xml version="1.0" encoding="UTF-8"?>\n<image version="0.0.3" w="{w}" h="{h}"><stack>'
                   + "".join(reversed(rows)) + "</stack></image>")          # stack.xml lists the top layer first
        merged = _flatten(layers)
        z.writestr("mergedimage.png", _png(merged))
        th = merged.copy()
        th.thumbnail((256, 256))
        z.writestr("Thumbnails/thumbnail.png", _png(th))
    return b.getvalue()


def write_psd(layers):
    """Photoshop layers, 8-bit RGB, uncompressed (GIMP, Photoshop, Krita, Photopea, DRAW open it)."""
    w, h = layers[0]["img"].size
    recs, chans = b"", b""
    for l in layers:                                       # PSD layer records go bottom -> top
        r, g, bl, a = l["img"].convert("RGBA").split()
        data = [(-1, a), (0, r), (1, g), (2, bl)]
        rec = struct.pack(">iiiiH", 0, 0, h, w, len(data))
        for cid, _ in data:
            rec += struct.pack(">hI", cid, 2 + w * h)
        flags = 0 if l.get("visible", True) else 2         # bit 1 set = hidden
        rec += b"8BIMnorm" + struct.pack(">BBBB", int(round(l.get("opacity", 1) * 255)), 0, flags, 0)
        name = l["name"].encode("latin-1", "replace")[:255]
        pname = bytes([len(name)]) + name
        pname += b"\0" * ((4 - len(pname) % 4) % 4)
        extra = struct.pack(">II", 0, 0) + pname           # no mask, no blending ranges, then the name
        rec += struct.pack(">I", len(extra)) + extra
        recs += rec
        for _, ch in data:
            chans += struct.pack(">H", 0) + ch.tobytes()
    info = struct.pack(">h", len(layers)) + recs + chans
    if len(info) % 2:
        info += b"\0"
    layer_info = struct.pack(">I", len(info)) + info
    lmi = layer_info + struct.pack(">I", 0)                 # + empty global layer mask info
    merged = _flatten(layers).convert("RGB")
    out = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, h, w, 8, 3)
    out += struct.pack(">I", 0) + struct.pack(">I", 0)      # color mode data, image resources
    out += struct.pack(">I", len(lmi)) + lmi
    out += struct.pack(">H", 0) + b"".join(c.tobytes() for c in merged.split())
    return out


def write_xcf(layers):
    """GIMP's own format (classic 32-bit-offset XCF, uncompressed 64x64 tiles)."""
    w, h = layers[0]["img"].size
    parts = []                                              # (offset-placeholder-aware) build in two passes

    def u32(v):
        return struct.pack(">I", v)

    def xstr(s):
        e = s.encode("utf-8")[:250] + b"\0"
        return u32(len(e)) + e

    head = b"gimp xcf file\0" + u32(w) + u32(h) + u32(0)                       # RGB image
    head += u32(17) + u32(1) + b"\0" + u32(0) + u32(0)                          # PROP_COMPRESSION none, PROP_END
    n = len(layers)
    ptr_table_len = 4 * (n + 1) + 4                                             # layer ptrs + 0, channel ptrs: just 0
    pos = len(head) + ptr_table_len
    blobs, layer_ptrs = [], []
    for l in reversed(layers):                                                  # XCF lists the top layer first
        img = l["img"].convert("RGBA")
        layer_ptrs.append(pos)
        lay = u32(w) + u32(h) + u32(1) + xstr(l["name"])                        # RGBA layer
        lay += u32(6) + u32(4) + u32(int(round(l.get("opacity", 1) * 255)))     # PROP_OPACITY
        lay += u32(8) + u32(4) + u32(1 if l.get("visible", True) else 0)        # PROP_VISIBLE
        lay += u32(15) + u32(8) + struct.pack(">ii", 0, 0)                      # PROP_OFFSETS
        lay += u32(0) + u32(0)                                                  # PROP_END
        hier_pos = pos + len(lay) + 8
        lay += u32(hier_pos) + u32(0)                                           # hierarchy, no layer mask
        level_pos = hier_pos + 16 + 4
        hier = u32(w) + u32(h) + u32(4) + u32(level_pos) + u32(0)
        tiles = [(tx, ty) for ty in range(0, h, 64) for tx in range(0, w, 64)]
        tile_pos = level_pos + 8 + 4 * (len(tiles) + 1)
        level, data = u32(w) + u32(h), b""
        for tx, ty in tiles:
            level += u32(tile_pos + len(data))
            data += img.crop((tx, ty, min(tx + 64, w), min(ty + 64, h))).tobytes()
        level += u32(0)
        blob = lay + hier + level + data
        blobs.append(blob)
        pos += len(blob)
    table = b"".join(u32(p) for p in layer_ptrs) + u32(0) + u32(0)
    return head + table + b"".join(blobs)


def write_draw(layers, palette=None):
    """DRAW's .draw: a PNG of the flattened picture with a "drAw" chunk holding the layers (DRW1 version 2,
    which DRAW's loader reads and upgrades on the next save)."""
    w, h = layers[0]["img"].size
    merged = _flatten(layers)
    if palette is None:                                     # the picture's most used colors (DRAW wants a palette)
        q = merged.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()[:3 * len(q.getcolors(256) or [])] or [0, 0, 0, 255, 255, 255]
        palette = [tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)]
    raw = b"DRW1" + struct.pack("<hii", 2, w, h)
    raw += struct.pack("<h", len(palette)) + b"".join(struct.pack("<BBBB", bb, gg, rr, 255) for rr, gg, bb in palette)
    raw += struct.pack("<hh", 0, 1 if len(palette) > 1 else 0)                  # fg / bg palette index
    raw += struct.pack("<hh", len(layers), len(layers))                         # layer count, current = the top one
    for z, l in enumerate(layers, 1):                                           # bottom first; higher zIndex = on top
        name = l["name"].encode("latin-1", "replace")[:16].ljust(16, b" ")
        raw += name + struct.pack("<hhhhh", -1 if l.get("visible", True) else 0,
                                   int(round(l.get("opacity", 1) * 255)), z, 0, 0)
        r, g, bl, a = l["img"].convert("RGBA").split()
        raw += Image.merge("RGBA", (bl, g, r, a)).tobytes()                    # &HAARRGGBB little-endian = B G R A
    raw += struct.pack("<hhhhh", 0, 1, 0, 0, 8)                                 # tool (none), brush, pixel-perfect, grid, grid size
    payload = struct.pack("<hi", 1, len(raw)) + zlib.compress(raw, 6)
    chunk = struct.pack(">I", len(payload)) + b"drAw" + payload + struct.pack(">I", zlib.crc32(b"drAw" + payload) & 0xffffffff)
    png = _png(merged)
    return png[:-12] + chunk + png[-12:]                                        # before IEND


WRITERS = {"psd": write_psd, "xcf": write_xcf, "ora": write_ora, "draw": write_draw}


def write_layered(fmt, layers):
    if fmt not in WRITERS:
        raise ValueError("export as psd, xcf, ora or draw")
    size = layers[0]["img"].size
    if any(l["img"].size != size for l in layers):
        raise ValueError("all layers must be the same size")
    return WRITERS[fmt](layers)
