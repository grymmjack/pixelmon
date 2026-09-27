# Curated presets

Preset folders that ship with pixelmon, in the same layout pixelmon-gui uses in
`~/pixelmon-gallery/gui-presets/`: each preset is `<name>.json` (every setting) plus
`<name>.png` (its sample picture); `.order.json` keeps the folder's order.

- **TUNED FACTORY/** — hand-tuned presets.

`install.sh` copies these folders into `~/pixelmon-gallery/gui-presets/` when they aren't
there yet (it never overwrites). To add them by hand:

```bash
cp -rn presets/* ~/pixelmon-gallery/gui-presets/
```

The **FACTORY** folder isn't stored here — it's rebuilt by `gui/make_starter_presets.py`.

Some presets point at images that aren't in the repo: steering references (in
`~/pixelmon-refs/`) and LAB inputs (in `~/pixelmon-gallery/lab-inputs/`). They load fine
without them; add your own references or input, or deselect steering, before rendering.
To move a preset *with* every image it uses, export it from the Presets tab as a `.zip`.
