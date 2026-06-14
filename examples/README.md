# pixelmon style gallery

Every `--style` rendered against the **same 12 subjects**, so you can see at a glance
what each one does — and compare them side by side. Nothing here is game-specific;
it's a showcase of the styles themselves.

- **Same subjects everywhere:** `castle, knight, dragon, spaceship, starbase, alien, cowboy, bandit, banker, thug, treasure, halloween`
- **Fixed seed per subject** so the *only* variable between styles is the style — `castle` in `dark` and `castle` in `mario` share a composition.
- Rendered at 128px across the [render farm](../README-RENDER-FARM.md). Each grid below is `styles/<name>.png`; the raw sprites live in `styles/<name>/`.

## Regenerate it yourself

```bash
# 1) render the sprites (point at your GPUs; falls back to local)
PIXELMON_FARM=rtx,titan,local,mac ./generate-style-demos.sh
# 2) build the contact sheets
./build-style-montages.sh
```

> **Why some styles set `--palette` and most don't:** hardware-palette looks
> (Game Boy, EGA, NES) are *exact* color sets. A text prompt can only nudge hue —
> SDXL can't *count* colors — so "monochrome green" just tints the subject green.
> Those styles therefore **lock to a real palette in post** (`--palette`); the prompt
> only sets the vibe. Prompt-driven styles keep the model's own colors.

---

## Palette-locked (true hardware palettes)

| | |
|---|---|
| **8bit** — NES palette | **gameboy** — Game Boy DMG 4-shade green |
| ![8bit](styles/8bit.png) | ![gameboy](styles/gameboy.png) |
| **ega** — 16-color IBM EGA | **wasteland** — EGA, 1988 Wasteland CRPG |
| ![ega](styles/ega.png) | ![wasteland](styles/wasteland.png) |

## LoRA-driven

| | |
|---|---|
| **dosega** — dosegafx EGA LoRA + EGA palette | **r3tr0** — retro-game-art LoRA |
| ![dosega](styles/dosega.png) | ![r3tr0](styles/r3tr0.png) |
| **pixelartredmond** — PixelArtRedmond LoRA | |
| ![pixelartredmond](styles/pixelartredmond.png) | |

## Prompt-driven (default Pixel Art XL LoRA)

| | |
|---|---|
| **clean** — crisp, flat shading | **detailed** — intricate shading, rich color |
| ![clean](styles/clean.png) | ![detailed](styles/detailed.png) |
| **minimal** — few colors, open space | **16bit** — vibrant SNES-era sprite |
| ![minimal](styles/minimal.png) | ![16bit](styles/16bit.png) |
| **geometric** — sharp angular forms | **outline** — bold outline, strong silhouette |
| ![geometric](styles/geometric.png) | ![outline](styles/outline.png) |
| **cute** — chibi mascot | **dark** — gritty, muted, ominous |
| ![cute](styles/cute.png) | ![dark](styles/dark.png) |
| **horror** — creepy, grotesque | **hyperlight** — Hyper Light Drifter neon |
| ![horror](styles/horror.png) | ![hyperlight](styles/hyperlight.png) |
| **deadcells** — fluid, glowing rim light | **blasphemous** — gothic, ornate, dark |
| ![deadcells](styles/deadcells.png) | ![blasphemous](styles/blasphemous.png) |
| **owlboy** — polished hi-bit, colorful | **stardew** — cozy farm-RPG |
| ![owlboy](styles/owlboy.png) | ![stardew](styles/stardew.png) |
| **dosrpg** — MS-DOS CRPG portrait, VGA | **darkest** — Darkest Dungeon ink gothic |
| ![dosrpg](styles/dosrpg.png) | ![darkest](styles/darkest.png) |
| **undertale** — simple, white-outline | **mario** — bright Nintendo platformer |
| ![undertale](styles/undertale.png) | ![mario](styles/mario.png) |
| **zelda** — SNES top-down action-RPG | **hollowknight** — inky monochrome gothic |
| ![zelda](styles/zelda.png) | ![hollowknight](styles/hollowknight.png) |
| **metroid** — moody sci-fi, armored | **finalfantasy** — 16-bit JRPG sprite |
| ![metroid](styles/metroid.png) | ![finalfantasy](styles/finalfantasy.png) |
| **pokemon** — cute creature, bold outline | |
| ![pokemon](styles/pokemon.png) | |

---

*Combine styles with modifiers like `solo` (one centered subject), `item` (object icon),
or `portrait` (head-and-shoulders) — e.g. `--style "dark,solo"`. Full list: `pixelmon --list-styles`.*
