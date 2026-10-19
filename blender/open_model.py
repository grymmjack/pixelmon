# Blender start-up script: open a model for editing. Blender itself only opens .blend files, so this clears
# the default scene's cube, imports the model named after "--", frames it and switches to Material Preview.
# pixelmon-gui's 🧊 Blender button runs it (blender --python-expr <this> -- MODEL).
import bpy, os, sys

p = sys.argv[sys.argv.index("--") + 1]
ext = os.path.splitext(p)[1].lower()
cube = bpy.data.objects.get("Cube")
if cube:
    bpy.data.objects.remove(cube)
imp = {".glb": lambda: bpy.ops.import_scene.gltf(filepath=p), ".gltf": lambda: bpy.ops.import_scene.gltf(filepath=p),
       ".obj": lambda: bpy.ops.wm.obj_import(filepath=p), ".stl": lambda: bpy.ops.wm.stl_import(filepath=p),
       ".ply": lambda: bpy.ops.wm.ply_import(filepath=p), ".fbx": lambda: bpy.ops.import_scene.fbx(filepath=p)}
try:
    imp[ext]()
    print("pixelmon: imported", p)
except Exception as e:
    print("pixelmon: couldn't import", p, e)


def frame():                                    # zoom every 3D view to the model, in Material Preview
    for w in bpy.context.window_manager.windows:
        for a in w.screen.areas:
            if a.type == "VIEW_3D":
                a.spaces.active.shading.type = "MATERIAL"
                r = next(r for r in a.regions if r.type == "WINDOW")
                with bpy.context.temp_override(window=w, area=a, region=r):
                    bpy.ops.view3d.view_all()


if not bpy.app.background:
    bpy.app.timers.register(frame, first_interval=0.5)
