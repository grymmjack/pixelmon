"""Blender side of pixelmon --mesh / --turnaround (run by pixelmon, headless):

  blender -b --factory-startup --python blender/turnaround.py -- MESH OUTDIR
          [--angles 8] [--size 1024] [--elev 0] [--lowpoly FACES] [--export OUT.glb] [--shade-only 1]

Imports MESH (.glb/.gltf/.obj/.stl/.ply/.fbx), stands it on the origin, and renders it from --angles
directions around it, azimuth 0 = the side the source picture showed. Each angle gets
OUTDIR/shade_aNNN.png (a grey preview) and, unless --shade-only, OUTDIR/depth_aNNN.png (camera depth,
near = white, transparent background). One orthographic camera size for every angle, so the model is
the same size in all of them. --lowpoly FACES decimates the model to about that many faces and shades
it flat (faceted low-poly look); --export writes the model as rendered (decimated, standing on the
origin) to a .glb. Cycles on the CPU, so it runs anywhere, display or not."""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
mesh_path, out = argv[0], argv[1]
opt = {argv[i][2:]: argv[i + 1] for i in range(2, len(argv) - 1, 2)}
N, SIZE = int(opt.get("angles", 8)), int(opt.get("size", 1024))
ELEV = math.radians(float(opt.get("elev", 0)))
LOWPOLY, EXPORT, SHADE_ONLY = int(opt.get("lowpoly", 0)), opt.get("export"), opt.get("shade-only") == "1"
os.makedirs(out, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
ext = os.path.splitext(mesh_path)[1].lower()
if ext in (".glb", ".gltf"):
    bpy.ops.import_scene.gltf(filepath=mesh_path)
elif ext == ".obj":
    bpy.ops.wm.obj_import(filepath=mesh_path)
elif ext == ".stl":
    bpy.ops.wm.stl_import(filepath=mesh_path)
elif ext == ".ply":
    bpy.ops.wm.ply_import(filepath=mesh_path)
elif ext == ".fbx":
    bpy.ops.import_scene.fbx(filepath=mesh_path)
else:
    sys.exit(f"TURN error: can't import {ext} models")
objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not objs:
    sys.exit("TURN error: no mesh in " + mesh_path)

if LOWPOLY > 0:
    faces = lambda: sum(len(o.data.polygons) for o in objs)
    total = faces()
    for o in objs:                               # a clean closed surface first: collapse stalls on Hunyuan3D's ragged one
        bpy.context.view_layer.objects.active = o
        r = o.modifiers.new("remesh", "REMESH")
        r.mode = "VOXEL"
        r.voxel_size = max(o.dimensions) / max(24, int(math.sqrt(LOWPOLY) * 3))
        bpy.ops.object.modifier_apply(modifier=r.name)
    for _ in range(4):                           # collapse stalls on awkward topology: go round again
        now = faces()
        if now <= LOWPOLY * 1.15:
            break
        for o in objs:
            bpy.context.view_layer.objects.active = o
            m = o.modifiers.new("lowpoly", "DECIMATE")
            m.ratio = min(1.0, LOWPOLY / max(1, now))
            m.use_collapse_triangulate = True
            bpy.ops.object.modifier_apply(modifier=m.name)
    print(f"TURN lowpoly {total} -> {faces()} faces", flush=True)
for o in objs:                                   # facets read as facets; a smooth model stays smooth
    for poly in o.data.polygons:
        poly.use_smooth = LOWPOLY <= 0

# stand the model on the origin, centred
pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
shift = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
for o in objs:
    o.location -= shift
lo, hi = lo - shift, hi - shift
radius = max(math.hypot(x, y) for x in (lo.x, hi.x) for y in (lo.y, hi.y))
height = hi.z

if EXPORT:
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=EXPORT, export_format="GLB", use_selection=True)
    print("TURN exported", EXPORT, flush=True)

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.render.resolution_x = scene.render.resolution_y = SIZE
scene.render.film_transparent = True
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "Standard"

cam_data = bpy.data.cameras.new("cam")
cam_data.type = "ORTHO"
cam_data.ortho_scale = max(2 * radius, height) * 1.08
dist = radius * 4 + height * 2
cam_data.clip_start, cam_data.clip_end = 0.01, dist * 3
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

world = bpy.data.worlds.new("w")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]

grey = bpy.data.materials.new("grey")
grey.use_nodes = True
bsdf = grey.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.6, 1)
bsdf.inputs["Roughness"].default_value = 0.7
depth = bpy.data.materials.new("depth")          # camera depth as emission: near white .. far black
depth.use_nodes = True
nt = depth.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)
cd, mr = nt.nodes.new("ShaderNodeCameraData"), nt.nodes.new("ShaderNodeMapRange")
em, mo = nt.nodes.new("ShaderNodeEmission"), nt.nodes.new("ShaderNodeOutputMaterial")
mr.inputs["From Min"].default_value = dist - radius * 1.05
mr.inputs["From Max"].default_value = dist + radius * 1.05
mr.inputs["To Min"].default_value, mr.inputs["To Max"].default_value = 1.0, 0.0
nt.links.new(cd.outputs["View Z Depth"], mr.inputs["Value"])
nt.links.new(mr.outputs["Result"], em.inputs["Color"])
nt.links.new(em.outputs["Emission"], mo.inputs["Surface"])

sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 3.0
scene.collection.objects.link(sun)


def use(mat):
    for o in objs:
        o.data.materials.clear()
        o.data.materials.append(mat)


for k in range(N):
    az = 360.0 * k / N
    t = math.radians(az)
    # azimuth 0 faces the model's front (glTF +Z = Blender -Y); positive angles walk round to its left
    d = Vector((math.sin(t) * math.cos(ELEV), -math.cos(t) * math.cos(ELEV), math.sin(ELEV)))
    cam.location = Vector((0, 0, height / 2)) + d * dist
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    sun.rotation_euler = (-(d + Vector((0.4, 0, 0.8)))).to_track_quat("-Z", "Y").to_euler()
    tag = f"a{round(az) % 360:03d}"
    use(grey)
    bg.inputs["Strength"].default_value = 0.6
    scene.cycles.samples = 16
    scene.render.filepath = os.path.join(out, f"shade_{tag}.png")
    bpy.ops.render.render(write_still=True)
    if not SHADE_ONLY:
        use(depth)
        bg.inputs["Strength"].default_value = 0.0
        scene.cycles.samples = 4
        scene.render.filepath = os.path.join(out, f"depth_{tag}.png")
        bpy.ops.render.render(write_still=True)
    print(f"TURN angle {az:g}", flush=True)
print("TURN done", flush=True)
