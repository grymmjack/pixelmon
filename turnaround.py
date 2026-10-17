"""pixelmon --mesh / --turnaround: from one picture to a 3D model and a set of matching rotation sprites.

  --mesh IMG          the picture -> a 3D model (.glb) with Hunyuan3D 2 on the render server, plus a
                      4-view preview rendered by Blender. --lowpoly N also writes a faceted low-poly copy.
  --turnaround SRC    SRC (a picture, or a model: .glb/.gltf/.obj/.stl/.ply/.fbx) -> a model (if needed)
                      -> Blender renders it from --angles directions (exact depth maps) -> pixelmon paints
                      each one: depth lock on that angle's depth, steering from the picture (its colours and
                      style), cut out, one seed for all. Every other pixelmon flag applies to the sprites.

Blender runs on this machine: --blender CMD, else $PIXELMON_BLENDER, else `blender` on the PATH, else the
org.blender.Blender flatpak. pixelmon.py calls run(P, a, parser) with itself as P (its server helpers)."""
import math
import os
import random
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
MODEL_EXT = (".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx")
# flags of the parent run that the per-angle renders must not inherit (they get their own)
OWN = {"--mesh", "--turnaround", "--angles", "--elev", "--lowpoly", "--blender", "--mesh-model", "--mesh-steps",
       "--mesh-cfg", "--mesh-octree", "--name", "--seed", "-n", "--number"}


def find_blender(spec=None):
    """The command that runs Blender here, as a list, or None."""
    spec = spec or os.environ.get("PIXELMON_BLENDER")
    if spec:
        return shlex.split(spec)
    if shutil.which("blender"):
        return [shutil.which("blender")]
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", "org.blender.Blender"],
                                                  capture_output=True).returncode == 0:
        return ["flatpak", "run", "org.blender.Blender"]
    return None


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


def make_model(P, a, img, dest, name, seed):
    """The picture -> a 3D model (dest/<name>.glb) with Hunyuan3D 2 on the render server. An opaque picture
    is cut out first (BiRefNet); the subject is centred on white with a margin, as Hunyuan3D wants."""
    from PIL import Image
    rgba = Image.open(img).convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] == 255:
        print("   ✂ cutting the subject out first (BiRefNet)")
        rgba.putalpha(P._cutout_mask(a, rgba.convert("RGB"), img, P.SERVER).resize(rgba.size))
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
                      (a.tile, "--tile"), (a.post_sweep, "--post-sweep"), (a.batch, "--batch")):
        if bad:
            parser.error(f"{flag} doesn't go with --mesh / --turnaround")
    dest = os.path.expanduser(a.output_to) if a.output_to else os.path.join(P.OUTPUT, "pixelmon")
    os.makedirs(dest, exist_ok=True)
    name = a.name or (P.slug(a.prompt) if a.prompt else P.slug(os.path.splitext(os.path.basename(src))[0]))
    seed = a.seed if a.seed >= 0 else random.randint(0, 2**31 - 1)
    t0 = time.time()

    model = src if is_model else make_model(P, a, src, dest, name, seed)
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
        failed += subprocess.run(cmd).returncode != 0
    print(f"   all done in {time.time() - t0:.1f}s  |  files in {dest}"
          + (f"  |  {failed} angle(s) failed" if failed else ""))
    if failed == a.angles:
        sys.exit(1)
