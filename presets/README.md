# Curated presets

Preset folders that ship with pixelmon — complete, with every image they use — in the
layout pixelmon-gui uses in `~/pixelmon-gallery/gui-presets/`:

```
presets/<FOLDER>/
  <name>.json          every setting of the preset
  <name>.png           its sample picture
  .order.json          the folder's order
  _assets/steering/<collection>/<file>   steering references it uses   (-> ~/pixelmon-refs/)
  _assets/lab-input/<file>               LAB input images it uses      (-> ~/pixelmon-gallery/lab-inputs/)
```

- **TUNED FACTORY/** — hand-tuned presets.
- **UPSCALE AND REPAIR/** — 🔍 presets (JPEG → clean pixel art, a 4x upscale, and a big JPEG cleaned
  and enlarged 2x): each sets the upscale / JPEG-repair controls, not the prompt; the first two load an
  example picture into the Lab, JPEG-to-JPEG-CLEAN-LARGE keeps yours. The examples are
  original: a sprite sheet drawn for it, saved as a rough JPEG, and a pixelmon render.

`install.sh` runs `python3 gui/presets_sync.py install`, which puts every preset and image
where pixelmon looks — only what's missing, never overwriting.

To update this folder from your own tuning (the release skill does this before every release):

```bash
python3 gui/presets_sync.py export                  # every folder except FACTORY
python3 gui/presets_sync.py export "TUNED FACTORY"  # just one
```

The **FACTORY** folder isn't stored here — `gui/make_starter_presets.py` rebuilds it.
