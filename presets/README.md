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

`install.sh` runs `python3 gui/presets_sync.py install`, which puts every preset and image
where pixelmon looks — only what's missing, never overwriting.

To update this folder from your own tuning (the release skill does this before every release):

```bash
python3 gui/presets_sync.py export                  # every folder except FACTORY
python3 gui/presets_sync.py export "TUNED FACTORY"  # just one
```

The **FACTORY** folder isn't stored here — `gui/make_starter_presets.py` rebuilds it.
