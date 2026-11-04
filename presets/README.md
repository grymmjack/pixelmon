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
- **GAME TEXTURES/** — 64 px game textures, art renders shrunk: walls, floors and ceilings made with
  `--surface --tile` (the card is the 3x3 repeat), and doors, paintings, a poster, a banner, a stained
  glass window, a crate side and a console seen from the front.
- **GAME TEXTURES HD/** — 512 px tiling materials with height / normal / roughness / AO maps (`--surface
  --tile --maps`).
- **GAME ANIMATION/** — 🎞 Animate presets (▶ runs the whole set; the card is a sample GIF): side-view
  heroes and enemies with their moves, 4-way top-down RPG walkers, an 8-way turnaround, fire, green
  magic fire, an explosion, smoke and sparkles, and blink / talk / smoke portrait loops.

`gui/make_game_presets.py` builds those three (it renders each sample on the server). SDXL can't
hold a regular grid, so there's no checkerboard, diamond plate or office ceiling grid among them.
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
