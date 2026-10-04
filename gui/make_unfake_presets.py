"""Build the UNFAKE preset folder: every pixel-art FACTORY preset, re-done with unfake grid snapping.

Same prompt, LoRA, palette, dither and canvas as the original, and the SAME sample seed, so each UNFAKE
preset compares 1:1 with its FACTORY twin. Only the finishing changes:
  snap pixels on, snap method unfake  (find the grid the model really drew, then reduce it to the canvas)
  despeckle 0 for scenes / 1 for small sprites  (at the true grid, details are 1-2 px islands)
Art-mode presets are skipped (no pixel step to snap).

usage: make_unfake_presets.py [names…]   (no names = all)
"""
import glob
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_starter_presets as msp  # noqa: E402

ROOT = os.path.expanduser("~/pixelmon-gallery/gui-presets")
SRC, DST = os.path.join(ROOT, "FACTORY"), os.path.join(ROOT, "UNFAKE")
WORK = os.path.expanduser("~/pixelmon-gallery/preset-work-unfake")


def main(only):
    os.makedirs(DST, exist_ok=True)
    order = json.load(open(os.path.join(SRC, ".order.json"))) if os.path.isfile(os.path.join(SRC, ".order.json")) else []
    names = order + sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(SRC, "*.json"))
                           if os.path.splitext(os.path.basename(f))[0] not in order)
    made = []
    for name in names:
        path = os.path.join(SRC, name + ".json")
        if not os.path.isfile(path):
            continue
        rec = json.load(open(path))
        form = dict(rec["snapshot"]["form"])
        if form.get("art") or (only and name not in only):
            continue
        small = max(int(form.get("outW") or 0), int(form.get("outH") or 0)) < 160
        form.update(snap_pixels=True, snap_method="unfake", despeckle=1 if small else 0)
        m = re.search(r"seed (\d+)", rec.get("note", ""))
        seed = int(m.group(1)) if m else 1000
        adv = rec["snapshot"].get("adv") or None
        t = time.time()
        try:
            img, cmd = msp.render(dict(msp.BASE_FORM, **form), seed, os.path.join(WORK, name), adv)
        except RuntimeError as e:
            print(f"FAILED {name}: {e}", flush=True)
            continue
        new = "UF-" + name
        shutil.copy2(img, os.path.join(DST, new + ".png"))
        note = re.sub(r"\s*\(sample: seed \d+\)\s*$", "", rec.get("note", ""))
        out = {"name": new, "saved": time.time(),
               "note": f"{note} — unfake grid snap (true model grid, reduced to the canvas), despeckle "
                       f"{form['despeckle']}. Twin of FACTORY/{name}.  (sample: seed {seed})",
               "snapshot": dict(rec["snapshot"], form=form), "image": new + ".png",
               "sample_command": msp.server.shlex.join(["pixelmon"] + cmd[1:-2])}
        with open(os.path.join(DST, new + ".json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
        made.append(new)
        print(f"ok  {new:34} {time.time() - t:5.1f}s  s{seed}", flush=True)
    op = os.path.join(DST, ".order.json")
    prev = json.load(open(op)) if os.path.isfile(op) else []
    json.dump(prev + [n for n in made if n not in prev], open(op, "w"), indent=1)


if __name__ == "__main__":
    main(set(sys.argv[1:]))
