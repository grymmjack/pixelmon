#!/usr/bin/env python3
"""Keep the repo's presets/ in step with pixelmon-gui's preset folders — images included.

    python3 gui/presets_sync.py export            # gallery -> repo: every folder except FACTORY
    python3 gui/presets_sync.py export "TUNED FACTORY" USER
    python3 gui/presets_sync.py install           # repo -> gallery (new installs; never overwrites)

A folder in the repo holds each preset's <name>.json + picture, the folder's .order.json, and
_assets/ with every image its presets point at:
    _assets/steering/<collection>/<file>   steering references  (-> ~/pixelmon-refs/<collection>/)
    _assets/lab-input/<file>               LAB input images     (-> ~/pixelmon-gallery/lab-inputs/)
FACTORY isn't exported: gui/make_starter_presets.py rebuilds it. Standard library only.
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_PRESETS = os.path.join(REPO, "presets")
GALLERY_PRESETS = os.path.expanduser("~/pixelmon-gallery/gui-presets")
REFS = os.path.expanduser("~/pixelmon-refs")
LAB = os.path.expanduser("~/pixelmon-gallery/lab-inputs")
SKIP = {"FACTORY"}


def _presets(d):
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and not f.startswith("."))


def export(folders):
    folders = folders or sorted(f for f in os.listdir(GALLERY_PRESETS)
                                if os.path.isdir(os.path.join(GALLERY_PRESETS, f)) and not f.startswith(".") and f not in SKIP)
    for folder in folders:
        src = os.path.join(GALLERY_PRESETS, folder)
        if not os.path.isdir(src):
            print(f"  ✗ no preset folder “{folder}”"); continue
        dst = os.path.join(REPO_PRESETS, folder)
        # the repo copy mirrors the gallery folder exactly — except an image a preset still uses that's gone
        # from this machine (a steering ref deleted since): the previously exported copy is kept, not lost
        old = tempfile.mkdtemp(prefix="pm-presets-")
        if os.path.isdir(os.path.join(dst, "_assets")):
            shutil.copytree(os.path.join(dst, "_assets"), os.path.join(old, "_assets"))
        shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst)
        missing, kept = [], []

        def keep_old(rel):
            src_old = os.path.join(old, rel)
            if os.path.isfile(src_old):
                os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
                shutil.copy2(src_old, os.path.join(dst, rel))
                return True
            return False
        for name in _presets(src):
            with open(os.path.join(src, name + ".json"), encoding="utf-8") as fh:
                rec = json.load(fh)
            snap = rec.get("snapshot") or {}
            snap.pop("from", None)                         # a local gallery path: meaningless elsewhere
            for k in ("id", "folder"):
                rec.pop(k, None)
            if rec.get("image") and os.path.isfile(os.path.join(src, rec["image"])):
                shutil.copy2(os.path.join(src, rec["image"]), os.path.join(dst, rec["image"]))
            for ref in (snap.get("steer") or {}).get("sel") or []:
                f = os.path.join(REFS, ref)
                if os.path.isfile(f):
                    os.makedirs(os.path.join(dst, "_assets", "steering", os.path.dirname(ref)), exist_ok=True)
                    shutil.copy2(f, os.path.join(dst, "_assets", "steering", ref))
                elif keep_old(os.path.join("_assets", "steering", ref)):
                    kept.append(f"{name}: steering ref {ref}")
                else:
                    missing.append(f"{name}: steering ref {ref}")
            lab = (snap.get("lab") or {}).get("file")
            if lab:
                f = os.path.join(LAB, os.path.basename(lab))
                if os.path.isfile(f):
                    os.makedirs(os.path.join(dst, "_assets", "lab-input"), exist_ok=True)
                    shutil.copy2(f, os.path.join(dst, "_assets", "lab-input", os.path.basename(lab)))
                elif keep_old(os.path.join("_assets", "lab-input", os.path.basename(lab))):
                    kept.append(f"{name}: LAB input {lab}")
                else:
                    missing.append(f"{name}: LAB input {lab}")
            if (snap.get("evo") or {}).get("parent"):
                missing.append(f"{name}: evolve parent (not bundled; re-save the preset without it to silence this)")
            with open(os.path.join(dst, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1)
        order = os.path.join(src, ".order.json")
        if os.path.isfile(order):
            shutil.copy2(order, os.path.join(dst, ".order.json"))
        shutil.rmtree(old, ignore_errors=True)
        if not _presets(dst):                              # nothing to ship
            shutil.rmtree(dst)
            print(f"  – {folder}: empty, skipped")
            continue
        n_assets = sum(len(fs) for _, _, fs in os.walk(os.path.join(dst, "_assets")))
        print(f"  ✓ {folder}: {len(_presets(dst))} presets, {n_assets} referenced images")
        for m in kept:
            print(f"    · kept the repo's copy of {m} (gone from this machine)")
        for m in missing:
            print(f"    ! missing {m}")


def _copy_new(src, dst):
    """copy src -> dst unless dst exists; returns True if copied"""
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def install():
    if not os.path.isdir(REPO_PRESETS):
        return
    for folder in sorted(os.listdir(REPO_PRESETS)):
        src = os.path.join(REPO_PRESETS, folder)
        if not os.path.isdir(src) or folder.startswith(("_", ".")):
            continue
        dst = os.path.join(GALLERY_PRESETS, folder)
        new = 0
        for f in os.listdir(src):                          # presets, pictures, order — only what's missing
            if os.path.isfile(os.path.join(src, f)):
                new += _copy_new(os.path.join(src, f), os.path.join(dst, f))
        assets = os.path.join(src, "_assets")
        for base, target in ((os.path.join(assets, "steering"), REFS), (os.path.join(assets, "lab-input"), LAB)):
            for d, _, fs in os.walk(base):
                for f in fs:
                    rel = os.path.relpath(os.path.join(d, f), base)
                    new += _copy_new(os.path.join(d, f), os.path.join(target, rel))
        print(f"  ✓ presets/{folder}: {new} new file(s) installed")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "export":
        export(args)
    elif cmd == "install":
        install()
    else:
        sys.exit(__doc__)
