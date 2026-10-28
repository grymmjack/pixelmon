"""pixelmon --mesh / --turnaround: from one picture to a 3D model and a set of matching rotation sprites.

  --mesh IMG          the picture -> a 3D model (.glb) with Hunyuan3D 2 on the render server, plus a
                      4-view preview rendered by Blender. --lowpoly N also writes a faceted low-poly copy.
  --texture           also a clean, textured model: quads, a UV map, the picture baked into a texture.
  --shape SPEC        build the model from the picture's outline instead (shapes.py: extrude / lathe):
                      already low-poly and textured, no render server.
  --turnaround SRC    SRC (a picture, or a model: .glb/.gltf/.obj/.stl/.ply/.fbx) -> a model (if needed)
                      -> Blender renders it from --angles directions (exact depth maps) -> pixelmon paints
                      each one: depth lock on that angle's depth, steering from the picture (its colours and
                      style), cut out, one seed for all. Every other pixelmon flag applies to the sprites.

Blender runs on this machine: --blender CMD, else $PIXELMON_BLENDER, else `blender` on the PATH, else the
org.blender.Blender flatpak. pixelmon.py calls run(P, a, parser) with itself as P (its server helpers)."""
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER_SCRIPT = os.path.join(HERE, "blender", "turnaround.py")
TEXTURE_SCRIPT = os.path.join(HERE, "blender", "texture.py")
OPEN_SCRIPT = os.path.join(HERE, "blender", "open_model.py")
MODEL_EXT = (".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx")
# flags of the parent run that the per-angle renders must not inherit (they get their own)
OWN = {"--mesh", "--turnaround", "--angles", "--elev", "--lowpoly", "--blender", "--mesh-model", "--mesh-steps",
       "--mesh-cfg", "--mesh-octree", "--name", "--seed", "-n", "--number", "--texture", "--texture-size", "--texture-elev",
       "--shape", "--shape-grid"}


def find_blender(spec=None):
    """The command that runs Blender here, as a list, or None: spec / $PIXELMON_BLENDER, `blender` on the PATH,
    the org.blender.Blender flatpak, then the usual install folders (newest version first)."""
    spec = spec or os.environ.get("PIXELMON_BLENDER")
    if spec:
        spec = os.path.expanduser(spec)
        return [spec] if os.path.isfile(spec) else shlex.split(spec, posix=os.name != "nt")
    if shutil.which("blender"):
        return [shutil.which("blender")]
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", "org.blender.Blender"],
                                                  capture_output=True).returncode == 0:
        return ["flatpak", "run", "org.blender.Blender"]
    return next(([f] for f in blender_installs() if os.path.isfile(f)), None)


def blender_installs():
    """Where Blender is usually unpacked or installed: Linux tarballs in ~/ and /opt, the Snap, Steam,
    macOS /Applications, Windows Program Files. Newest version first where a folder holds several."""
    natural = lambda f: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", f.lower())]

    def versions(d, exe):
        try:
            names = [n for n in os.listdir(d) if n.lower().startswith("blender")]
        except OSError:
            return []
        return [os.path.join(d, n, exe) for n in sorted(names, key=natural, reverse=True)]
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return ["/Applications/Blender.app/Contents/MacOS/Blender",
                os.path.join(home, "Applications/Blender.app/Contents/MacOS/Blender")]
    if os.name == "nt":
        out = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            pf = os.environ.get(var)
            if pf:
                out += versions(os.path.join(pf, "Blender Foundation"), "blender.exe")
                out.append(os.path.join(pf, "Steam", "steamapps", "common", "Blender", "blender.exe"))
        return out
    return (["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender"] + versions("/opt", "blender")
            + versions(home, "blender") + versions(os.path.join(home, "Applications"), "blender")
            + versions(os.path.join(home, "apps"), "blender")
            + [os.path.join(home, ".steam/steam/steamapps/common/Blender/blender"),
               os.path.join(home, ".local/share/Steam/steamapps/common/Blender/blender")])


def open_in_blender(cmd, model):
    """The argv that opens `model` in Blender (cmd = find_blender()): a .blend as is, anything else imported
    (Blender only opens .blend files), framed, in Material Preview."""
    if model.lower().endswith((".blend", ".blend1")):
        return cmd + [model]
    with open(OPEN_SCRIPT, encoding="utf-8") as fh:
        return cmd + ["--python-expr", fh.read(), "--", model]


def view_words(az, elev):
    """How the prompt describes the view at azimuth az (0 = front)."""
    a = az % 360
    side = min(a, 360 - a)                       # 0 front .. 180 back, either way round
    words = ("front view, facing the viewer" if side < 22.5 else "three-quarter front view" if side < 67.5
             else "side view, in profile" if side < 112.5 else "three-quarter back view" if side < 157.5
             else "back view, seen from behind")
    if elev >= 20:
        words += ", seen from above"
    elif elev >= 8:
        words += ", seen from slightly above"
    return words


def blender(a, model, outdir, angles, size, elev=0.0, lowpoly=0, export=None, shade_only=False):
    """Run blender/turnaround.py on model; its PNGs land in outdir. Works from a scratch folder in the home
    directory (a flatpak Blender can't see /tmp), copying files in and out."""
    cmd = find_blender(a.blender)
    if not cmd:
        sys.exit("3D needs Blender on this machine: install it (blender on the PATH, or the org.blender.Blender "
                 "flatpak), or point --blender / $PIXELMON_BLENDER at it.")
    base = os.path.join(os.path.expanduser("~"), ".cache", "pixelmon")
    os.makedirs(base, exist_ok=True)
    work = tempfile.mkdtemp(prefix="turn-", dir=base)
    try:
        src = os.path.join(work, "model" + os.path.splitext(model)[1].lower())
        shutil.copy2(model, src)
        exp = os.path.join(work, "export.glb") if export else None
        args = cmd + ["-b", "--factory-startup", "--python", BLENDER_SCRIPT, "--", src, os.path.join(work, "out"),
                      "--angles", str(angles), "--size", str(size), "--elev", f"{elev:g}"]
        if lowpoly:
            args += ["--lowpoly", str(lowpoly)]
        if exp:
            args += ["--export", exp]
        if shade_only:
            args += ["--shade-only", "1"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        log = r.stdout + r.stderr
        if "TURN done" not in log:
            sys.exit("Blender didn't finish:\n" + "\n".join(log.strip().splitlines()[-15:]))
        for line in log.splitlines():
            if line.startswith("TURN lowpoly"):
                print("   🔺 " + line[5:])
        os.makedirs(outdir, exist_ok=True)
        for f in os.listdir(os.path.join(work, "out")):
            shutil.move(os.path.join(work, "out", f), os.path.join(outdir, f))
        if exp and os.path.isfile(exp):
            shutil.move(exp, export)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cut_picture(P, a, img, dest):
    """The picture with its subject cut out (BiRefNet, if it isn't already), kept as dest/input-cut.png so
    --mesh and --texture cut it once."""
    from PIL import Image, PngImagePlugin
    kept = os.path.join(dest, "input-cut.png")
    st = os.stat(img)
    src = f"{os.path.realpath(img)}|{st.st_size}|{int(st.st_mtime)}"   # only this picture's cut is reused: several
    if os.path.isfile(kept):                                           # models made into one folder share the name
        old = Image.open(kept)
        if old.info.get("pixelmon-src") == src:
            return old.convert("RGBA")
    rgba = Image.open(img).convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] == 255:
        print("   ✂ cutting the subject out first (BiRefNet)")
        rgba.putalpha(P._cutout_mask(a, rgba.convert("RGB"), img, P.SERVER).resize(rgba.size))
    meta = PngImagePlugin.PngInfo()
    meta.add_text("pixelmon-src", src)
    rgba.save(kept, pnginfo=meta)
    return rgba


def make_model(P, a, img, dest, name, seed):
    """The picture -> a 3D model (dest/<name>.glb) with Hunyuan3D 2 on the render server. An opaque picture
    is cut out first (BiRefNet); the subject is centred on white with a margin, as Hunyuan3D wants."""
    from PIL import Image
    rgba = cut_picture(P, a, img, dest)
    box = rgba.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box:
        sys.exit(f"--mesh: found no subject in {img}")
    piece = rgba.crop(box)
    side = int(max(piece.size) / 0.85)
    sheet = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    sheet.alpha_composite(piece, ((side - piece.width) // 2, (side - piece.height) // 2))
    inp = os.path.join(dest, "input-mesh.png")
    sheet.convert("RGB").resize((512, 512), Image.LANCZOS).save(inp)
    up = P._upload_image(inp, P.SERVER, "--mesh")
    # ComfyUI's own Hunyuan3D 2 template (3d_hunyuan3d_image_to_model.json), cfg inside its 4..8 advice
    g = {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": a.mesh_model}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 1.0}},
        "3": {"class_type": "LoadImage", "inputs": {"image": up}},
        "4": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["1", 1], "image": ["3", 0], "crop": "none"}},
        "5": {"class_type": "Hunyuan3Dv2Conditioning", "inputs": {"clip_vision_output": ["4", 0]}},
        "6": {"class_type": "EmptyLatentHunyuan3Dv2", "inputs": {"resolution": 3072, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "positive": ["5", 0], "negative": ["5", 1],
                                                   "latent_image": ["6", 0], "seed": seed, "steps": a.mesh_steps,
                                                   "cfg": a.mesh_cfg, "sampler_name": "euler", "scheduler": "normal",
                                                   "denoise": 1.0}},
        "8": {"class_type": "VAEDecodeHunyuan3D", "inputs": {"samples": ["7", 0], "vae": ["1", 2],
                                                            "num_chunks": 8000, "octree_resolution": a.mesh_octree}},
        "9": {"class_type": "VoxelToMesh", "inputs": {"voxel": ["8", 0], "algorithm": "surface net", "threshold": 0.6}},
        # a fresh name every run: an identical prompt answered from cache would report no file
        "10": {"class_type": "SaveGLB", "inputs": {"mesh": ["9", 0], "filename_prefix": f"pixelmon/mesh/{uuid.uuid4().hex[:8]}"}},
    }
    t0 = time.time()
    print(f"   🧊 3D model: Hunyuan3D 2 on {P._short(P.SERVER)} (seed {seed})…", flush=True)
    outs = P.wait(P.submit(g), timeout=900, resubmit=lambda fresh: P.submit(g))
    f = next((x for node in outs.values() for v in node.values() if isinstance(v, list) for x in v
              if isinstance(x, dict) and str(x.get("filename", "")).lower().endswith(".glb")), None)
    if not f:
        sys.exit("Hunyuan3D finished but returned no model.")
    q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
    out = os.path.join(dest, f"{name}.glb")
    with urllib.request.urlopen(f"{P.SERVER}/view?{q}", timeout=300) as r, open(out, "wb") as o:
        shutil.copyfileobj(r, o)
    print(f"   🧊 3D model -> {out}  ({os.path.getsize(out) // 1024} KB, {time.time() - t0:.0f}s)", flush=True)
    return out


def despill(rgba, img, ring=3):
    """Take the backdrop's tint out of a cut-out's outline (rgba: an HxWx4 array, alpha 0/255, changed in place).
    The cut keeps a few pixels of anti-aliased edge mixed with the backdrop (magenta, say); the texture's bleed
    would spread that tint over the model's outline. The backdrop is the picture's border colour, so a picture
    that came already cut out (no backdrop) is left alone."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    pic = Image.open(img)
    if pic.mode in ("RGBA", "LA", "PA") and pic.getchannel("A").getextrema()[0] < 255:
        return
    pic = np.asarray(pic.convert("RGB"), np.float32)
    key = np.median(np.concatenate([pic[0], pic[-1], pic[:, 0], pic[:, -1]]), 0)
    tint = key - key.mean()
    if np.linalg.norm(tint) < 24:                        # a grey/white/black backdrop has no tint to take out
        return
    tint /= np.linalg.norm(tint)
    solid = rgba[..., 3] > 0
    edge = solid & (ndimage.distance_transform_edt(solid) <= ring)
    rgb = rgba[..., :3].astype(np.float32)
    amount = (rgb - rgb.mean(-1, keepdims=True)) @ tint  # how far each pixel leans towards the backdrop's hue
    full = float((key - key.mean()) @ tint)              # ... and the backdrop itself
    mostly = edge & ((amount > 0.35 * full) | (np.sqrt(((rgb - key) ** 2).sum(-1)) < 60))
    fix = (edge & (amount > 0)) | (solid & (amount > 30))   # + backdrop showing through gaps (under an arm)
    rgb[fix] -= amount[fix][:, None] * tint              # remove that lean, keep the brightness
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[..., 3][mostly] = 0                             # mostly backdrop (untinted it'd be a grey speck): drop it


def shape_model(P, a, img, dest, name, seed):
    """--shape: the picture's outline built into a clean low-poly model, the picture its texture
    (shapes.py; no render server unless the picture needs cutting out) -> dest/<name>_textured.glb (+ .gltf,
    .bin, _texture.png to paint) and a 4-view preview."""
    import numpy as np
    from PIL import Image
    import shapes
    t0 = time.time()
    rgba = np.array(cut_picture(P, a, img, dest))
    clean = rgba.copy()
    clean[..., 3] = np.where(rgba[..., 3] >= 128, 255, 0)
    despill(clean, img)                                  # the backdrop's tint out of the edge colours
    rgba[..., :3] = clean[..., :3]
    rgba[..., 3][(clean[..., 3] == 0) & (rgba[..., 3] >= 128)] = 0
    try:
        tris, tex = shapes.build(a.shape, Image.fromarray(rgba, "RGBA"), a.shape_grid)
    except ValueError as e:
        sys.exit(f"--shape: {e}")
    stem = os.path.join(dest, f"{name}_textured")
    shapes.write_gltf(tris, tex, stem + ".glb", stem + ".gltf", stem + ".bin", stem + "_texture.png", name)
    sheet = Image.new("RGBA", (384 * 4, 384), (48, 48, 60, 255))
    for i, az in enumerate((0, 45, 90, 180)):
        sheet.alpha_composite(shapes.render(tris, tex, az, 384), (384 * i, 0))
    views = f"{stem}_s{seed}_views.png"
    sheet.convert("RGB").save(views)
    print(f"   📐 {a.shape}: {len(tris)} triangles, the picture as a {tex.width}px texture "
          f"-> {stem}.glb  ({time.time() - t0:.1f}s)", flush=True)
    print(f"   ✅ seed={seed}  ->  {views}")
    return stem + ".glb"


def texture_model(P, a, model, img, dest, name, seed):
    """model + the picture it was made from -> dest/<name>_textured.glb (+ .gltf, .bin, _texture.png) and a
    4-view preview. Blender (blender/texture.py) does the work; shapes QuadriFlow refuses get finer voxels,
    then plain triangles."""
    import json
    import numpy as np
    from PIL import Image
    cmd = find_blender(a.blender)
    if not cmd:
        sys.exit("--texture needs Blender on this machine (see --blender).")
    faces = a.lowpoly or 6000
    tex = a.texture_size or (1024 if faces <= 1500 else 2048)
    if not 256 <= tex <= 8192:
        sys.exit("--texture-size must be 256..8192")
    rgba = cut_picture(P, a, img, dest)
    al = np.array(rgba.getchannel("A"))
    ys, xs = np.nonzero(al > 24)
    if not len(xs):
        sys.exit(f"--texture: found no subject in {img}")
    box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    solid = np.array(rgba)
    solid[..., 3] = np.where(al >= 128, 255, 0)          # half-see-through edge pixels carry background colour
    despill(solid, img)
    base = os.path.join(os.path.expanduser("~"), ".cache", "pixelmon")
    os.makedirs(base, exist_ok=True)
    work = tempfile.mkdtemp(prefix="tex-", dir=base)    # a flatpak Blender can't see /tmp
    try:
        color, mask, out = (os.path.join(work, f) for f in ("color.png", "mask.png", "out"))
        P._bleed(Image.fromarray(solid, "RGBA"), rounds=96).convert("RGB").save(color)
        rgba.getchannel("A").save(mask)
        src = os.path.join(work, "model" + os.path.splitext(model)[1].lower())
        shutil.copy2(model, src)
        t0 = time.time()
        print(f"   🎨 textured model: {faces} {'flat ' if a.lowpoly else ''}faces, {tex}px texture (Blender)…", flush=True)
        ok, log = False, ""
        for voxdiv, quads in ((160, 1), (320, 1), (480, 1), (160, 0)):   # thin parts need finer voxels; then triangles
            args = cmd + ["-b", "--factory-startup", "--python", TEXTURE_SCRIPT, "--", src, color, mask, out,
                          "--box", ",".join(str(int(v)) for v in box), "--faces", str(faces), "--tex", str(tex),
                          "--flat", "1" if a.lowpoly else "0", "--voxdiv", str(voxdiv), "--quads", str(quads),
                          "--elev", "auto" if a.texture_elev is None else f"{a.texture_elev:g}"]
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=900)
                log = r.stdout + r.stderr
            except subprocess.TimeoutExpired:                # QuadriFlow can spin forever on a bad shape
                subprocess.run(["pkill", "-KILL", "-f", work])   # a flatpak's Blender outlives the flatpak command
                log = "timed out"
            if "TEX done" in log:
                ok = True
                break
            why = next((l for l in reversed(log.splitlines()) if "TEX error" in l), None) or \
                (log.strip().splitlines() or [""])[-1]
            print(f"   🎨 {'quads' if quads else 'triangles'} at 1/{voxdiv} didn't work ({why.strip()[:80]})"
                  f"{', trying again…' if quads else ''}", flush=True)
        if not ok:
            print("   🎨 couldn't texture this model:\n" + "\n".join(log.strip().splitlines()[-10:]))
            return None
        for line in log.splitlines():
            if line.startswith(("TEX elevation", "TEX quads", "TEX triangles")) or " lose part" in line:
                print("   🎨 " + line[4:])
        stem = f"{name}_textured"
        shutil.move(os.path.join(out, "model.glb"), os.path.join(dest, stem + ".glb"))
        shutil.move(os.path.join(out, "model_texture.png"), os.path.join(dest, stem + "_texture.png"))
        gl = os.path.join(out, "model.gltf")
        if os.path.isfile(gl):                           # point the .gltf at the files' new names
            with open(gl) as f:
                j = json.load(f)
            for im in j.get("images", []):
                im["uri"] = stem + "_texture.png"
                im.pop("bufferView", None)
            for b in j.get("buffers", []):
                if b.get("uri"):
                    shutil.move(os.path.join(out, b["uri"]), os.path.join(dest, stem + ".bin"))
                    b["uri"] = stem + ".bin"
            with open(os.path.join(dest, stem + ".gltf"), "w") as f:
                json.dump(j, f)
        views = [os.path.join(out, f"view_a{az:03d}.png") for az in (0, 90, 180, 270)]
        sheet = views_sheet(views, os.path.join(dest, f"{stem}_s{seed}_views.png"))
        print(f"   🎨 textured model -> {os.path.join(dest, stem + '.glb')}  ({time.time() - t0:.0f}s)")
        print(f"   ✅ seed={seed}  ->  {sheet}")
        return os.path.join(dest, stem + ".glb")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def views_sheet(shades, out, cell=384, bg=(48, 48, 60)):
    """Side-by-side preview of the grey renders."""
    from PIL import Image
    sheet = Image.new("RGBA", (cell * len(shades), cell), bg + (255,))
    for i, f in enumerate(shades):
        im = Image.open(f).convert("RGBA").resize((cell, cell), Image.LANCZOS)
        sheet.alpha_composite(im, (i * cell, 0))
    sheet.convert("RGB").save(out)
    return out


def out_aspect(a):
    """Width / height of the pictures the per-angle renders make (--out WxH or N; square by default)."""
    for v in (a.out, a.size):
        if v and "x" in str(v).lower():
            w, h = str(v).lower().split("x", 1)
            if w.isdigit() and h.isdigit() and int(w) and int(h):
                return int(w) / int(h)
    return 1.0


def stretch_depth(src, dst, aspect=1.0):
    """Blender's depth -> a depth map for the ControlNet: this angle's own near..far spread over 0.15..1
    (so the model's relief reads strongly), background black, padded (never cropped) out to the render's
    aspect so the whole model is in a wide or tall picture too."""
    import numpy as np
    from PIL import Image
    im = np.asarray(Image.open(src).convert("RGBA")).astype(np.float32)
    d, m = im[..., 0] / 255.0, im[..., 3] > 8
    if m.any():
        lo, hi = d[m].min(), d[m].max()
        d = np.where(m, 0.15 + 0.85 * (d - lo) / max(hi - lo, 1e-6), 0.0)
    pic = Image.fromarray((np.clip(d, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    w, h = pic.size
    W, H = (round(h * aspect), h) if aspect >= 1 else (w, round(w / aspect))
    if (W, H) != (w, h):
        canvas = Image.new("RGB", (W, H), (0, 0, 0))
        canvas.paste(pic, ((W - w) // 2, (H - h) // 2))
        pic = canvas
    pic.save(dst)


def passthrough(parser, argv):
    """The parent's command-line flags, minus the prompt and the flags in OWN, ready for a per-angle run."""
    acts = parser._option_string_actions
    kept, i = [], 0
    while i < len(argv):
        t = argv[i]
        if t.startswith("-") and len(t) > 1 and not t[1:].replace(".", "", 1).isdigit():
            opt = t.split("=", 1)[0]
            act = acts.get(opt)
            vals = []
            if act is not None and "=" not in t and act.nargs != 0:
                if act.nargs == "?":                 # an optional value: only if it parses as one
                    try:
                        if i + 1 < len(argv):
                            (act.type or str)(argv[i + 1])
                            vals = [] if argv[i + 1].startswith("-") else [argv[i + 1]]
                    except (TypeError, ValueError):
                        vals = []
                else:
                    vals = argv[i + 1:i + 2]
            if opt not in OWN:
                kept += [t] + vals
            i += 1 + len(vals)
        else:                                        # the prompt (positional)
            i += 1
    return kept


def run_angle(cmd):
    """Run one angle's pixelmon and return its exit code. If this run is stopped (Ctrl-C, the GUI's Cancel), the
    child gets SIGTERM and a moment to take its prompt off the server's queue — subprocess.run would SIGKILL it,
    and that prompt would render on for nobody."""
    proc = subprocess.Popen(cmd)
    try:
        return proc.wait()
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def run(P, a, parser):
    if a.mesh and a.turnaround:
        parser.error("--mesh makes a model; --turnaround makes a model and its angles: use one")
    src = os.path.expanduser(a.mesh or a.turnaround)
    if not os.path.isfile(src):
        parser.error(f"no such file: {src}")
    is_model = src.lower().endswith(MODEL_EXT)
    if a.mesh and is_model:
        parser.error("--mesh needs a picture (it makes the model)")
    if a.turnaround and not a.prompt:
        parser.error('--turnaround needs a prompt: what the subject is, e.g. "a green battle mech"')
    if not 2 <= a.angles <= 64:
        parser.error("--angles must be 2..64")
    for bad, flag in ((a.depth, "--depth"), (a.animate, "--animate"), (a.mask, "--mask"), (a.init, "--init"),
                      (a.tile, "--tile"), (a.post_sweep, "--post-sweep"), (a.batch, "--batch"),
                      (a.batch_file, "--batch-file"), (a.dry_run, "--dry-run"), (a.print_graph, "--print-graph"),
                      (a.list_servers, "--list-servers")):
        if bad:
            parser.error(f"{flag} doesn't go with --mesh / --turnaround")
    dest = os.path.expanduser(a.output_to) if a.output_to else os.path.join(P.OUTPUT, "pixelmon")
    os.makedirs(dest, exist_ok=True)
    name = a.name or (P.slug(a.prompt) if a.prompt else P.slug(os.path.splitext(os.path.basename(src))[0]))
    seed = a.seed if a.seed >= 0 else random.randint(0, 2**31 - 1)
    t0 = time.time()

    if a.shape:
        if is_model:
            parser.error("--shape builds the model from a picture; that's already a model")
        if a.lowpoly or a.texture:
            parser.error("--shape makes its own low-poly, textured model: drop --lowpoly / --texture")
        if not 8 <= a.shape_grid <= 256:
            parser.error("--shape-grid must be 8..256")
        import shapes
        try:
            shapes.parse(a.shape)
        except ValueError as e:
            parser.error(str(e))
        model = shape_model(P, a, src, dest, name, seed)
        if a.mesh:
            print(f"   all done in {time.time() - t0:.1f}s  |  files in {dest}")
            return
    else:
        model = src if is_model else make_model(P, a, src, dest, name, seed)
    if a.texture:                                   # from the full model (the bake's source), before any low-poly
        pic = src if not is_model else a.steer
        if not pic or not os.path.isfile(os.path.expanduser(pic)):
            parser.error("--texture of a model needs the picture it was made from: --steer PICTURE")
        texture_model(P, a, model, os.path.expanduser(pic), dest, name, seed)
    turn = os.path.join(dest, "turn")
    if a.lowpoly:                                    # the faceted copy is what gets rendered and painted
        low = os.path.join(dest, f"{name}_lowpoly.glb")
        blender(a, model, turn, 4, 384, a.elev, a.lowpoly, export=low, shade_only=True)
        print(f"   🔺 low-poly model -> {low}")
        model = low

    if a.mesh:                                       # a preview, so the gallery has something to show
        if find_blender(a.blender):
            blender(a, model, turn, 4, 384, a.elev, shade_only=True)
            shades = [os.path.join(turn, f"shade_a{az:03d}.png") for az in (0, 90, 180, 270)]
            sheet = views_sheet(shades, os.path.join(dest, f"{name}_model_s{seed}_views.png"))
            print(f"   ✅ seed={seed}  ->  {sheet}")
        else:
            print("   (no Blender here, so no preview picture; the model is ready)")
        print(f"   all done in {time.time() - t0:.1f}s  |  files in {dest}")
        return

    print(f"   🎥 Blender: {a.angles} angles of {os.path.basename(model)}"
          f"{f' from {a.elev:g}° up' if a.elev else ''}…", flush=True)
    blender(a, model, turn, a.angles, 1024, a.elev)
    ref = None if a.steer else (None if is_model else src)
    kept = passthrough(parser, sys.argv[1:])
    has = lambda *flags: any(t.split("=", 1)[0] in flags for t in kept)
    failed = 0
    for k in range(a.angles):
        az = round(360 * k / a.angles) % 360
        dm = os.path.join(turn, f"depthmap_a{az:03d}.png")
        stretch_depth(os.path.join(turn, f"depth_a{az:03d}.png"), dm, out_aspect(a))
        words = view_words(az, a.elev)
        cmd = [sys.executable, os.path.join(HERE, "pixelmon.py"), f"{a.prompt}, {words}"] + kept
        cmd += ["--depth", dm, "--depth-raw", "--seed", str(seed), "--name", f"{name}_a{az:03d}"]
        if not has("--depth-strength"):
            cmd += ["--depth-strength", "0.9"]
        if not has("--depth-end"):
            cmd += ["--depth-end", "0.8"]
        if ref:
            cmd += ["--steer", ref] + ([] if has("--steer-strength") else ["--steer-strength", "0.8"])
        if not has("--cutout", "--transparent", "--fit-to"):
            cmd.append("--cutout")
        if not has("--output-to"):
            cmd += ["--output-to", dest]
        if not has("--no-open"):
            cmd.append("--no-open")
        print(f"   🧭 [{k + 1}/{a.angles}] {az}° — {words}", flush=True)
        failed += run_angle(cmd) != 0
    print(f"   all done in {time.time() - t0:.1f}s  |  files in {dest}"
          + (f"  |  {failed} angle(s) failed" if failed else ""))
    if failed == a.angles:
        sys.exit(1)
