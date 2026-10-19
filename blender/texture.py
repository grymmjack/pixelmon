"""Blender side of pixelmon --texture (run by pixelmon, headless): a model -> a clean, textured model.

  blender -b --factory-startup --python blender/texture.py -- MODEL COLOR.png MASK.png OUTDIR
          --box X0,Y0,X1,Y1 [--faces 6000] [--tex 2048] [--flat 0] [--voxdiv 160] [--quads 1] [--elev auto]

COLOR.png is the picture the model was made from (its colours bled past the subject's edge), MASK.png its
cut-out, --box the subject's box in it. The steps:
  1. elevation: the camera height (0/30/55° above) whose outline best matches the picture's, so a picture
     that looks down on its subject is projected from above (--elev DEG skips the search)
  2. the picture's projection from that view (the model's box onto the subject's box): the front gets the
     picture, the back the same colours seen through, the sides a stretch of the edge colours
  3. a clean copy: voxel remesh (1/--voxdiv of the model's size; thin parts need finer), then QuadriFlow to
     about --faces quads (--quads 0: decimate to triangles instead, for shapes QuadriFlow refuses)
  4. a UV map (Smart UV Project), the picture projected straight onto the clean copy and baked into it,
     its pixels kept crisp: OUTDIR/model_texture.png
  5. OUTDIR/model.glb (texture inside), OUTDIR/model.gltf + model.bin (pointing at the PNG, for painting),
     OUTDIR/view_a000.png .. view_a270.png (the textured model from 4 sides)
Prints "TEX done" when it got there. Cycles on the CPU, so it runs anywhere, display or not."""
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
model, color, mask, out = argv[:4]
opt = {argv[i][2:]: argv[i + 1] for i in range(4, len(argv) - 1, 2)}
FACES, TEX, FLAT = int(opt.get("faces", 6000)), int(opt.get("tex", 2048)), opt.get("flat", "0") == "1"
VOXDIV, QUADS = int(opt.get("voxdiv", 160)), opt.get("quads", "1") == "1"
X0, Y0, X1, Y1 = (float(v) for v in opt["box"].split(","))
os.makedirs(out, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
ext = os.path.splitext(model)[1].lower()
if ext in (".glb", ".gltf"):
    bpy.ops.import_scene.gltf(filepath=model)
elif ext == ".obj":
    bpy.ops.wm.obj_import(filepath=model)
elif ext == ".stl":
    bpy.ops.wm.stl_import(filepath=model)
elif ext == ".ply":
    bpy.ops.wm.ply_import(filepath=model)
elif ext == ".fbx":
    bpy.ops.import_scene.fbx(filepath=model)
else:
    sys.exit(f"TEX error: can't import {ext} models")
src = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not src:
    sys.exit("TEX error: no mesh in " + model)
bpy.ops.object.select_all(action="DESELECT")
for o in src:
    o.select_set(True)
bpy.context.view_layer.objects.active = src[0]
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")  # the importer's empties go; their transforms stay
for o in list(bpy.context.scene.objects):
    if o.type != "MESH":
        bpy.data.objects.remove(o, do_unlink=True)
if len(src) > 1:
    bpy.ops.object.join()
hi = bpy.context.view_layer.objects.active
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
pts = [v.co.copy() for v in hi.data.vertices]
lo3 = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi3 = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
mid, size = (lo3 + hi3) / 2, max(hi3 - lo3)

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "Standard"
cam_d = bpy.data.cameras.new("cam")
cam_d.type = "ORTHO"
cam_d.ortho_scale, cam_d.clip_end = size * 1.25, size * 10
cam = bpy.data.objects.new("cam", cam_d)
scene.collection.objects.link(cam)
scene.camera = cam
world = bpy.data.worlds.new("w")
scene.world = world
world.use_nodes = True


def aim(az, elev):
    """Camera at azimuth az (0 = the front, glTF +Z = Blender -Y) and elev degrees up, looking at the middle."""
    t, e = math.radians(az), math.radians(elev)
    d = Vector((math.sin(t) * math.cos(e), -math.cos(t) * math.cos(e), math.sin(e)))
    cam.location = mid + d * size * 3
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    return d


def alpha_of(path):
    im = bpy.data.images.load(path)
    w, h = im.size
    px = np.empty(w * h * 4, np.float32)
    im.pixels.foreach_get(px)
    bpy.data.images.remove(im)
    return px.reshape(h, w, 4)[::-1, :, 3]               # Blender stores rows bottom-up


def shape(a, n=64):
    """A mask cropped to its subject, scaled (aspect kept) into an n x n square standing on its bottom edge."""
    ys, xs = np.nonzero(a >= 0.4)
    if not len(xs):
        return np.zeros((n, n), bool)
    crop = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1] >= 0.4
    k = n / max(crop.shape)
    h, w = max(1, round(crop.shape[0] * k)), max(1, round(crop.shape[1] * k))
    small = crop[(np.arange(h) / k).astype(int).clip(0, crop.shape[0] - 1)][:, (np.arange(w) / k).astype(int).clip(0, crop.shape[1] - 1)]
    sq = np.zeros((n, n), bool)
    sq[n - h:, (n - w) // 2:(n - w) // 2 + w] = small
    return sq


# --- 1. which height the picture was seen from
if opt.get("elev", "auto") == "auto":
    target = shape(alpha_of(mask))
    scene.render.resolution_x = scene.render.resolution_y = 160
    scene.render.film_transparent = True
    scene.cycles.samples = 1
    scores = {}
    for e in (0, 30, 55):                                # straight down (80°) fits boxes too well; no sprite needs it
        aim(0, e)
        scene.render.filepath = os.path.join(out, f"sil_{e}.png")
        bpy.ops.render.render(write_still=True)
        m = shape(alpha_of(scene.render.filepath))
        os.remove(scene.render.filepath)
        scores[e] = (m & target).sum() / max((m | target).sum(), 1)
    top = max(scores, key=scores.get)
    # eye level unless a higher view is clearly better AND a good match (a poor match at every height says
    # nothing about the height: a leafy plant, a figure posed differently from the picture)
    ELEV = top if scores[top] > scores[0] + 0.05 and scores[top] >= 0.6 else 0
    print("TEX elevation", ELEV, " ".join(f"{k}°={v:.2f}" for k, v in scores.items()), flush=True)
else:
    ELEV = float(opt["elev"])

# --- 2. the picture's projection from that view: the original model's box maps onto the subject's box.
# It depends only on where a point is, so the clean copy gets it directly (step 4): no rays from one
# surface to the other, which on a faceted copy land each facet on a different patch of the picture.
UP = Vector((0.0, math.sin(math.radians(ELEV)), math.cos(math.radians(ELEV))))
img = bpy.data.images.load(color)
img.colorspace_settings.name = "sRGB"
W, H = img.size
pu = [p.dot(UP) for p in pts]
ulo, uhi = min(pu), max(pu)


def project(me):
    """A "front" UV map on mesh me: each corner -> where its point falls in the picture."""
    uvl = me.uv_layers.new(name="front")
    for li, loop in enumerate(me.loops):
        p = me.vertices[loop.vertex_index].co
        u = (X0 + (p.x - lo3.x) / max(hi3.x - lo3.x, 1e-6) * (X1 - X0)) / W
        v = 1.0 - (Y0 + (uhi - p.dot(UP)) / max(uhi - ulo, 1e-6) * (Y1 - Y0)) / H
        uvl.data[li].uv = (min(max(u, 0), 1), min(max(v, 0), 1))
    return uvl


# --- 3. the clean copy: watertight, then quads (or triangles)
bpy.ops.object.select_all(action="DESELECT")
hi.select_set(True)
bpy.context.view_layer.objects.active = hi
bpy.ops.object.duplicate()
lo = bpy.context.view_layer.objects.active
lo.data.materials.clear()
r = lo.modifiers.new("vox", "REMESH")
r.mode, r.voxel_size = "VOXEL", size / VOXDIV
bpy.ops.object.modifier_apply(modifier=r.name)
n0 = len(lo.data.polygons)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.remove_doubles(threshold=size / 2000)       # QuadriFlow wants one clean surface, normals outwards
bpy.ops.mesh.dissolve_degenerate(threshold=size / 5000)
bpy.ops.mesh.normals_make_consistent(inside=False)
bpy.ops.mesh.delete_loose()
bpy.ops.object.mode_set(mode="OBJECT")
if QUADS:
    res = bpy.ops.object.quadriflow_remesh(target_faces=FACES, use_mesh_symmetry=False, use_preserve_sharp=True,
                                           use_preserve_boundary=True, smooth_normals=False, seed=1)
    if "FINISHED" not in res:
        sys.exit("TEX error: QuadriFlow failed")
    print(f"TEX quads {n0} -> {len(lo.data.polygons)} faces", flush=True)
else:
    full, dims = lo.data.copy(), lo.dimensions.copy()
    target = FACES * 2                                   # FACES quads' worth of triangles
    for _ in range(6):
        for _ in range(4):                               # collapse stalls on awkward topology: go round again
            now = len(lo.data.polygons)
            if now <= target * 1.15:
                break
            m = lo.modifiers.new("dec", "DECIMATE")
            m.ratio, m.use_collapse_triangulate = min(1.0, target / now), True
            bpy.ops.object.modifier_apply(modifier=m.name)
        # too few faces for a thin or leafy shape and whole parts collapse away (a plant loses its pot):
        # then start again from the full mesh with twice the faces
        if all(lo.dimensions[i] >= dims[i] * 0.92 for i in range(3)) or target >= n0:
            break
        print(f"TEX {target} triangles lose part of the shape, trying {target * 2}", flush=True)
        target *= 2
        old = lo.data
        lo.data = full.copy()
        bpy.data.meshes.remove(old)
    print(f"TEX triangles {n0} -> {len(lo.data.polygons)} faces", flush=True)
for poly in lo.data.polygons:
    poly.use_smooth = not FLAT

# --- 4. UV map, then bake the projected picture onto it
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(angle_limit=math.radians(55), island_margin=0.02)
bpy.ops.object.mode_set(mode="OBJECT")
uv_main = lo.data.uv_layers.active.name
front = project(lo.data).name
lo.data.uv_layers.active = lo.data.uv_layers[uv_main]    # the bake writes through the UV map, reads through "front"
baked = bpy.data.images.new("model_texture", TEX, TEX, alpha=False)
src_mat = bpy.data.materials.new("src")                  # the bake's source: the picture, unlit, on "front"
src_mat.use_nodes = True
nt = src_mat.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)
fuv = nt.nodes.new("ShaderNodeUVMap")
fuv.uv_map = front
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = img
tex.interpolation = "Closest"                            # the picture's pixels stay crisp squares, not a blur
tex.extension = "EXTEND"
em, mo = nt.nodes.new("ShaderNodeEmission"), nt.nodes.new("ShaderNodeOutputMaterial")
nt.links.new(fuv.outputs["UV"], tex.inputs["Vector"])
nt.links.new(tex.outputs["Color"], em.inputs["Color"])
nt.links.new(em.outputs["Emission"], mo.inputs["Surface"])
target = nt.nodes.new("ShaderNodeTexImage")              # the active image node is what the bake fills
target.image = baked
nt.nodes.active = target
lo.data.materials.append(src_mat)
scene.cycles.samples = 4
bpy.ops.object.select_all(action="DESELECT")
lo.select_set(True)
bpy.context.view_layer.objects.active = lo
bpy.ops.object.bake(type="EMIT", margin=8)
baked.filepath_raw = os.path.join(out, "model_texture.png")
baked.file_format = "PNG"
baked.save()
print("TEX baked", baked.filepath_raw, flush=True)
lo.data.uv_layers.remove(lo.data.uv_layers[front])
mat = bpy.data.materials.new("model")
mat.use_nodes = True
mnt = mat.node_tree
bsdf = mnt.nodes["Principled BSDF"]
bsdf.inputs["Roughness"].default_value = 0.65
btex = mnt.nodes.new("ShaderNodeTexImage")
btex.image = baked
if FLAT:                                                 # low-poly: chunky texels, old-school Quake style
    btex.interpolation = "Closest"                       # (the .glb's sampler says NEAREST, so engines keep it)
mnt.links.new(btex.outputs["Color"], bsdf.inputs["Base Color"])
lo.data.materials.clear()
lo.data.materials.append(mat)

# --- 5. export, then the textured model from 4 sides
bpy.data.objects.remove(hi, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
lo.select_set(True)
bpy.ops.export_scene.gltf(filepath=os.path.join(out, "model.glb"), export_format="GLB", use_selection=True)
try:                                                     # the .gltf points at model_texture.png rather than a copy
    bpy.ops.export_scene.gltf(filepath=os.path.join(out, "model.gltf"), export_format="GLTF_SEPARATE",
                              use_selection=True, export_keep_originals=True)
except TypeError:
    bpy.ops.export_scene.gltf(filepath=os.path.join(out, "model.gltf"), export_format="GLTF_SEPARATE", use_selection=True)
print("TEX exported", flush=True)

world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.9
scene.render.resolution_x = scene.render.resolution_y = 384
scene.render.film_transparent = True
scene.cycles.samples = 16
sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 2.0
scene.collection.objects.link(sun)
for az in (0, 90, 180, 270):
    d = aim(az, 10)
    sun.rotation_euler = (-(d + Vector((0.4, 0, 0.8)))).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = os.path.join(out, f"view_a{az:03d}.png")
    bpy.ops.render.render(write_still=True)
print("TEX done", flush=True)
