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


def _read_psd(data):
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    layers = []
    for i, (name, bbox) in enumerate(getattr(im, "layers", []) or []):
        im.seek(i + 1)
        full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        full.paste(im.convert("RGBA"), bbox[:2])
        layers.append(Layer(name or f"layer {i + 1}", full, True, i))   # PSD lists bottom first
    if not layers:
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
