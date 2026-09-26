"""Generate starter presets for pixelmon-gui: render one sample per preset on rtx with the
GUI's own build_argv(), then write <name>.json + <name>.png into the presets folder.

    ~/ComfyUI/.venv/bin/python gui/make_starter_presets.py                     # all of them (renders on the GUI's render server)
    ~/ComfyUI/.venv/bin/python gui/make_starter_presets.py ega-night-scene     # just the named ones
    ~/ComfyUI/.venv/bin/python gui/make_starter_presets.py --try 4 style-ega   # 4 candidate seeds -> ~/pixelmon-gallery/preset-candidates/
(the ComfyUI venv python has PIL, which the GUI module needs.) Presets you saved yourself are untouched."""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "gui"))
import server  # noqa: E402  (build_argv, load_presets)

PRESETS = os.path.expanduser("~/pixelmon-gallery/gui-presets")
WORK = tempfile.mkdtemp(prefix="pixelmon-presetgen-")
DEFAULT_NEG = server.pixelmon_constant("PIXEL_NEGATIVE", "")
ART_NEG = server.pixelmon_constant("ART_NEGATIVE", "")
LORA_PRESETS = server.load_presets()

BASE_FORM = {"subject": "", "lora": "ega-art-v2.safetensors", "lora_strength": 0.8, "kind": "scene background",
             "genre": "adventure game", "era": "1980s DOS game", "negative": DEFAULT_NEG, "outW": 320, "outH": 200,
             "snap_pixels": False, "transparent": False, "art": False, "no_sprite_suffix": False, "palette": "EGA",
             "dither": "none", "dither_amount": 0.5, "despeckle": 2,
             # every newer setting explicitly — a preset must never inherit what was set before it was loaded
             "pixel_size": "1x1", "thin_lines": "0", "pixel_angles": 0, "angle_grid": "pixel", "seed": "", "seedLock": False, "n": 1,
             "name": "", "steps": "", "cfg": "", "fast": False, "showFull": False, "styles": []}
STEER_OFF = {"use": False, "strength": 0.7, "weight": "style transfer", "combine": "concat", "sel": [], "start": 0, "end": 1}
EVO_DEFAULT = {"parent": None, "init": True, "denoise": 0.45, "steerOn": True, "steer": 0.6,
               "weight": "style and composition", "n": 4,
               "start": 0, "end": 1, "perChild": False}

SCENE_LOOK = {"thin_lines": "4", "pixel_angles": 1.5, "angle_grid": "pixel"}   # 320x200+ scenes: 1-px outlines + clean angles
SMALL_LOOK = {"thin_lines": "0", "pixel_angles": 0}                           # sprites/icons: every stroke is a feature

# name, note, form overrides, [evo overrides]
SPECS = [
    # ---- the original twelve, now with every setting explicit ----
    ("ega-sierra-scene", "EGA adventure-game room, 320x200, 16 EGA colors, 1-px outlines + pixel-art angles. Swap the subject and go.",
     {"subject": "a wizard's cluttered study with bookshelves, a glowing crystal ball and an arched window", "styles": ["scene"],
      **SCENE_LOOK}),
    ("ega-scene-bayer-dither", "Same as the Sierra scene but gradients become real EGA Bayer cross-hatch.",
     {"subject": "a stone castle on a green hill under a sunset sky", "styles": ["scene"], "dither": "bayer4", "dither_amount": 0.5,
      "thin_lines": "3", "pixel_angles": 1.25}),
    ("ega-night-scene", "Night exteriors: deep EGA blue skies. (night:1.4) emphasis + LoRA 0.65 — at full strength ega_art paints daylight.",
     {"subject": "(night scene:1.4), a castle on a cliff under a (dark blue starry night sky:1.3), full moon, lit windows, "
                 "winding road, black mountain silhouettes",
      "styles": ["scene"], "genre": "role-playing game", "lora_strength": 0.65,
      "negative": DEFAULT_NEG + ", daylight, sun, bright day, border, frame", "dither": "bayer4", "dither_amount": 0.4,
      "thin_lines": "3", "pixel_angles": 1.25}),
    ("ega-agi-play-area-chunky", "Sierra AGI play area: 160x168 art grid of double-wide pixels (pixel size 2x1), like King's Quest I–III.",
     {"subject": "a forest path leading to a wooden bridge over a stream", "styles": ["scene"], "outW": 320, "outH": 168,
      "pixel_size": "2x1", "despeckle": 1, "lora_strength": 0.9, "thin_lines": "2", "pixel_angles": 0}),
    ("ega-goldbox-portrait", "EGA RPG character portrait, 128x128 (Gold Box / Ultima era). Outlines ≤2, no angle snap — at this size they'd eat the face.",
     {"subject": "a grizzled dwarf warrior with a braided beard and a horned helmet", "kind": "character portrait",
      "genre": "role-playing game", "outW": 128, "outH": 128, "thin_lines": "2", "pixel_angles": 0}),
    ("ega-sprite-transparent", "64x64 EGA game sprite with the background cut out, ready for a game. Post-fx off: every stroke is a feature.",
     {"subject": "a green goblin warrior with a spear, full body", "styles": ["solo"], "kind": "game sprite", "genre": "action game", "outW": 64, "outH": 64,
      "transparent": True, "despeckle": 1, **SMALL_LOOK}),
    ("vga-adventure-scene", "256-color VGA adventure-game scene (LucasArts / Sierra VGA era), model's own colors.",
     {"subject": "a moonlit harbor town with wooden docks and a tall ship", "lora": "dosart-vga.safetensors",
      "era": "1990s DOS game", "palette": "none", "styles": ["scene"], "thin_lines": "0", "pixel_angles": 0}),
    ("vga-rpg-portrait", "VGA RPG portrait (Eye of the Beholder / Ultima VII era).",
     {"subject": "an elven sorceress with silver hair and a glowing amulet", "lora": "dosart-vga.safetensors",
      "kind": "character portrait", "genre": "role-playing game", "era": "1990s DOS game", "palette": "none",
      "outW": 128, "outH": 128, **SMALL_LOOK}),
    ("dungeon-monster-darkest", "Matches qb64-dungeon's default monster art: Pixel Art XL + darkest, 128x128.",
     {"subject": "a monstrous dungeon rat with matted fur, long yellow fangs and glowing red eyes, single creature",
      "lora": "pixel-art-xl.safetensors", "lora_strength": 1.0, "kind": "", "genre": "", "era": "",
      "palette": "none", "styles": ["darkest"], "outW": 128, "outH": 128, **SMALL_LOOK}),
    ("cga-4-color-bayer", "1985 CGA look: cyan/magenta/white palette with coarse 2x2 Bayer dither.",
     {"subject": "a spaceship landing on an alien planet", "styles": ["scene"], "palette": "CGA1-HIGH",
      "negative": DEFAULT_NEG + ", text, letters, title, logo, words, border, frame",
      "dither": "bayer2", "dither_amount": 0.7, "thin_lines": "0", "pixel_angles": 0}),
    ("fast-seed-hunt", "Scout compositions fast: LCM mode x8. Lock the seed you like, then render at quality.",
     {"subject": "a haunted graveyard with a crooked iron gate", "styles": ["scene"], "fast": True, "n": 8, **SCENE_LOOK}),
    ("evolve-starter", "Breeding settings: keep composition 0.45 + steer toward parent 0.6 (style and composition), 4 children. Click 🧬 on any result.",
     {"subject": "a ruined temple in a jungle clearing", "styles": ["scene"], **SCENE_LOOK}, {}),

    # ---- new: showcase the pixel-art look controls ----
    ("ega-clean-lines-town", "The full hand-drawn look: 1-px outlines ≤4 + pixel-art angles 1.5 (DRAW's 11 ratios). Compare with angles 0.",
     {"subject": "a medieval village street with timber-framed houses, a well and a tavern sign", "styles": ["scene"],
      **SCENE_LOOK}),
    ("ega-isometric-room", "Isometric grid: edges snap to 2:1 pixel-iso lines + verticals. Best with 'isometric' in the prompt.",
     {"subject": "isometric view of a small stone dungeon room with a treasure chest, torches and a wooden door",
      "styles": ["scene"], "genre": "role-playing game", "thin_lines": "3", "pixel_angles": 1.5, "angle_grid": "isometric"}),
    ("ega-dungeon-corridor-diagonal", "First-person dungeon crawler view; diagonal grid (0/45/90°) suits corridor perspective.",
     {"subject": "first-person view down a stone dungeon corridor, doorways on both sides, torch light",
      "styles": ["scene"], "genre": "role-playing game", "thin_lines": "3", "pixel_angles": 1.5, "angle_grid": "diagonal"}),
    ("ega-city-square-grid", "Square grid: everything snaps orthogonal — blocky, architectural, great for top-down maps and cities.",
     {"subject": "top-down view of a walled city with streets, houses and a castle keep", "styles": ["scene"],
      "genre": "strategy game", "thin_lines": "2", "pixel_angles": 1.5, "angle_grid": "square"}),
    ("tandy-16-color-wide", "Tandy/PCjr 160x200 16-color mode: EGA palette, pixel size 2x1 double-wide pixels.",
     {"subject": "a pirate ship sailing past a tropical island at sunset", "styles": ["scene"],
      "pixel_size": "2x1", "thin_lines": "2", "pixel_angles": 0}),
    ("c64-multicolor-wide", "Commodore 64 multicolor: C=64 palette with 2x1 fat pixels, light Bayer dither.",
     {"subject": "a lone astronaut exploring an alien jungle with giant mushrooms", "styles": ["scene"], "era": "1980s home computer game",
      "palette": "C=64", "pixel_size": "2x1", "dither": "bayer2", "dither_amount": 0.35, "thin_lines": "0", "pixel_angles": 0}),
    ("zx-spectrum-screen", "ZX Spectrum loading-screen look: 256x192, 15 bright colors, crisp outlines.",
     {"subject": "a knight fighting a dragon in front of a castle", "styles": ["scene"], "era": "1980s home computer game",
      "palette": "ZXSPECTRUM", "outW": 256, "outH": 192, "thin_lines": "2", "pixel_angles": 1.25}),
    ("vga-palette-bayer-sky", "Locked to the standard VGA palette with Bayer 4x4 — dithered sunset skies like early VGA games.",
     {"subject": "a desert oasis with palm trees under a glowing sunset sky", "lora": "dosart-vga.safetensors",
      "era": "1990s DOS game", "palette": "VGA", "dither": "bayer4", "dither_amount": 0.5, "styles": ["scene"],
      "thin_lines": "0", "pixel_angles": 0}),
    ("ega-diffusion-cave", "Error-diffusion (Floyd–Steinberg) at low amount: soft EGA shading in caves and glows.",
     {"subject": "an underground cave with a glowing blue crystal pool and stalactites", "styles": ["scene"],
      "dither": "floyd-steinberg", "dither_amount": 0.35, "thin_lines": "3", "pixel_angles": 1.25}),
    ("ega-item-icon-32", "32x32 inventory icon on the EGA palette, transparent. Pixel Art XL draws tiny icons best; outlines/angles off.",
     {"subject": "a shiny red apple with a green leaf", "lora": "pixel-art-xl.safetensors", "lora_strength": 1.0,
      "kind": "", "genre": "", "era": "", "styles": ["item"],
      "outW": 32, "outH": 32, "transparent": True, "despeckle": 1, **SMALL_LOOK}),
    ("ega-monster-sprite-96", "96x96 transparent monster sprite with 1-px outlines ≤2 (the safe setting for sprites).",
     {"subject": "a purple slime blob monster with big googly eyes and a wide grin", "kind": "game sprite", "genre": "role-playing game",
      "outW": 96, "outH": 96, "transparent": True, "thin_lines": "2", "pixel_angles": 0}),
    ("gameboy-4-shade", "Game Boy DMG: 160x144, 4 green shades, clean 1-px lines.",
     {"subject": "a small village with a cottage, fences and trees", "styles": ["scene"], "era": "1990s handheld game",
      "palette": "GAMEBOY", "outW": 160, "outH": 144, "thin_lines": "2", "pixel_angles": 0}),
]

# ---- style demos: one preset per style, each with the LoRA / palette / size that suits it best ----
PXL = {"lora": "pixel-art-xl.safetensors", "lora_strength": 1.0, "kind": "", "genre": "", "era": "", "palette": "none",
       "thin_lines": "0", "pixel_angles": 0, "despeckle": 2}                  # Pixel Art XL: no caption tags
EGA2 = {"lora": "ega-art-v2.safetensors", "lora_strength": 0.8, "palette": "EGA"}
VGA = {"lora": "dosart-vga.safetensors", "lora_strength": 0.8, "era": "1990s DOS game", "palette": "none",
       "thin_lines": "0", "pixel_angles": 0}
ART = {"art": True, "lora": "pixel-art-xl.safetensors", "kind": "", "genre": "", "era": "", "negative": ART_NEG,
       "palette": "none", "thin_lines": "0", "pixel_angles": 0}               # full-res illustration (no pixel LoRA)
SPRITE = {"transparent": True, "despeckle": 1}

STYLE_SPECS = [
    ("style-clean", "clean — crisp modern pixel art, flat shading. ENDESGA-32 palette, 160x160, 1-px outlines ≤2.",
     {**PXL, "subject": "a red-roofed windmill on a grassy hill with a winding dirt path, blue sky, puffy clouds",
      "styles": ["clean"], "palette": "ENDESGA-32", "outW": 160, "outH": 160, "thin_lines": "2"}),
    ("style-detailed", "detailed — intricate shading, rich color. Model's own colors, 256x192.",
     {**PXL, "subject": "an alchemist's laboratory with bubbling glass flasks, brass instruments, candles and old books",
      "styles": ["detailed", "scene"], "outW": 256, "outH": 192}),
    ("style-minimal", "minimal — few colors, lots of empty space. PICO-8 palette, 128x128.",
     {**PXL, "subject": "a lone lighthouse on a small rocky island at dusk, calm sea",
      "styles": ["minimal"], "palette": "PICO-8", "outW": 128, "outH": 128}),
    ("style-8bit", "8bit — NES look: the NES palette at the NES's own 256x240.",
     {**PXL, "subject": "a knight with a sword and shield standing before a castle gate",
      "styles": ["8bit", "scene"], "palette": "NES", "outW": 256, "outH": 240, "despeckle": 1}),
    ("style-16bit", "16bit — vibrant SNES-era art at the SNES's 256x224.",
     {**PXL, "subject": "a young hero on a cliff overlooking a fantasy kingdom with a castle and a river valley",
      "styles": ["16bit", "scene"], "outW": 256, "outH": 224}),
    ("style-gameboy", "gameboy — Game Boy DMG: 4 green shades at 160x144.",
     {**PXL, "subject": "a small hero with a sword in front of a cave entrance, trees and rocks",
      "styles": ["gameboy", "scene"], "palette": "GAMEBOY", "outW": 160, "outH": 144, "despeckle": 1}),
    ("style-geometric", "geometric — sharp faceted forms; ENDESGA-16 + pixel-art angles 1.5 straighten every edge.",
     {**PXL, "subject": "a crystal golem made of jagged amethyst shards",
      "styles": ["geometric", "solo"], "palette": "ENDESGA-16", "outW": 192, "outH": 192,
      "thin_lines": "2", "pixel_angles": 1.5}),
    ("style-outline", "outline — bold outline, readable silhouette. 128x128 on a plain background (a cut-out would eat the outline).",
     {**PXL, "subject": "a fox adventurer with a backpack and a walking stick, full body",
      "styles": ["outline", "solo"], "outW": 128, "outH": 128}),
    ("style-cute", "cute — chibi mascot. 96x96 transparent sprite.",
     {**PXL, **SPRITE, "subject": "a tiny witch riding a broom with a black cat, full body",
      "styles": ["cute", "solo"], "outW": 96, "outH": 96}),
    ("style-dark", "dark — gritty, muted, ominous. Wide 320x180 scene in the model's own colors.",
     {**PXL, "subject": "an abandoned village street at dusk with a broken cart and boarded-up houses",
      "styles": ["dark", "scene"], "outW": 320, "outH": 180}),
    ("style-horror", "horror — creepy and grotesque, locked to the 9-color BLOODMOON21 palette. 160x160.",
     {**PXL, "subject": "a rotting scarecrow with glowing eyes in a cornfield at night",
      "styles": ["horror"], "palette": "BLOODMOON21", "outW": 160, "outH": 160}),
    ("style-hyperlight", "hyperlight — Hyper Light Drifter neon. Wide 384x216.",
     {**PXL, "subject": "a lone cloaked swordsman standing before giant ancient ruins under a pink and teal sky",
      "styles": ["hyperlight", "scene"], "outW": 384, "outH": 216}),
    ("style-deadcells", "deadcells — fluid sprite, glowing rim light. 160x160.",
     {**PXL, "subject": "a hooded warrior with a flaming sword leaping through a dark prison",
      "styles": ["deadcells"], "outW": 160, "outH": 160}),
    ("style-blasphemous", "blasphemous — gothic, ornate, dark. Tall 192x256.",
     {**PXL, "subject": "a penitent knight with a thorned helmet kneeling in a candlelit cathedral",
      "styles": ["blasphemous"], "outW": 192, "outH": 256}),
    ("style-owlboy", "owlboy — polished hi-bit, colorful. Wide 320x180.",
     {**PXL, "subject": "a floating sky island village with waterfalls, wooden houses and windmills",
      "styles": ["owlboy", "scene"], "outW": 320, "outH": 180}),
    ("style-stardew", "stardew — cozy farm RPG, top-down. 256x192.",
     {**PXL, "subject": "top-down view of a cozy farm with a red barn, crop fields, a chicken coop and a pond",
      "styles": ["stardew", "scene"], "outW": 256, "outH": 192}),
    ("style-dosrpg", "dosrpg — MS-DOS CRPG portrait: DOS VGA LoRA, 144x144 in its own 256 colors.",
     {**VGA, "subject": "an old bearded wizard with a pointed hat and a glowing staff", "kind": "character portrait",
      "genre": "role-playing game", "styles": ["dosrpg"], "outW": 144, "outH": 144}),
    ("style-darkest", "darkest — Darkest Dungeon ink gothic. 192x192.",
     {**PXL, "subject": "a plague doctor holding a lantern in a crypt",
      "styles": ["darkest"], "outW": 192, "outH": 192}),
    ("style-undertale", "undertale — simple white-outline monster, locked to pure 1-bit black and white. 128x128.",
     {**PXL, "subject": "a friendly skeleton monster with a scarf", "styles": ["undertale", "solo"],
      "palette": "1BIT", "outW": 128, "outH": 128, "despeckle": 1}),
    ("style-mario", "mario — bright platformer on the NES palette at 256x224.",
     {**PXL, "subject": "side view of a platformer level, a small hero jumping over a green pipe, floating brick blocks, "
                         "rolling green hills and white clouds in a blue sky",
      "negative": DEFAULT_NEG + ", sprite sheet, tileset, collage, many separate objects, white background",
      "styles": ["mario"], "palette": "NES", "outW": 256, "outH": 224, "despeckle": 1}),
    ("style-zelda", "zelda — SNES top-down action RPG, 256x224.",
     {**PXL, "subject": "top-down view of a hero in a green tunic in a forest clearing with bushes, rocks and a stone shrine",
      "styles": ["zelda", "scene"], "outW": 256, "outH": 224}),
    ("style-hollowknight", "hollowknight — inky monochrome gothic. Wide 320x180.",
     {**PXL, "subject": "a small masked bug knight with a needle sword in a dark cavern with glowing blue lanterns",
      "styles": ["hollowknight", "scene"], "outW": 320, "outH": 180}),
    ("style-metroid", "metroid — moody armored sci-fi, 256x224.",
     {**PXL, "subject": "an armored bounty hunter in an alien cavern full of glowing pods and strange plants",
      "styles": ["metroid", "scene"], "outW": 256, "outH": 224}),
    ("style-finalfantasy", "finalfantasy — 16-bit JRPG character sprite, 128x128 transparent.",
     {**PXL, **SPRITE, "subject": "a young mage in a red cloak and a wide hat casting a fire spell, full body",
      "styles": ["finalfantasy", "solo"], "outW": 128, "outH": 128}),
    ("style-pokemon", "pokemon — cute creature, bold outline. 96x96 transparent sprite.",
     {**PXL, **SPRITE, "subject": "a small electric fox creature with lightning-bolt tail",
      "styles": ["pokemon", "solo"], "outW": 96, "outH": 96}),
    ("style-ega", "ega — vivid 16-color EGA with hard dithering: Pixel Art XL + EGA palette + 1-px outlines + pixel-art angles, 320x200.",
     {**PXL, "subject": "a tall sorcerer's tower on a rocky hill beside a lake, mountains, blue sky", "styles": ["ega", "scene"],
      "palette": "EGA", "outW": 320, "outH": 200, "thin_lines": "3", "pixel_angles": 1.25}),
    ("style-wasteland", "wasteland — 1988 Wasteland CRPG portrait: EGA palette with Bayer dither, 128x128.",
     {**EGA2, "subject": "a post-apocalyptic desert ranger with a rifle and goggles", "kind": "character portrait",
      "genre": "role-playing game", "styles": ["wasteland", "portrait"], "outW": 128, "outH": 128,
      "dither": "bayer4", "dither_amount": 0.4, "thin_lines": "2", "pixel_angles": 0}),
    ("style-portrait", "portrait — head-and-shoulders bust: DOS VGA LoRA, 128x160.",
     {**VGA, "subject": "a weathered sea captain with a white beard and a pipe", "kind": "character portrait",
      "genre": "adventure game", "styles": ["portrait"], "outW": 128, "outH": 160}),
    ("style-dosega", "dosega — the dosegafx EGA LoRA (its trigger comes from the style) + EGA palette, 128x128.",
     {**PXL, "lora": "dosegafx.safetensors", "lora_strength": 0.9, "subject": "a desert raider with a spiked shoulder pad",
      "styles": ["dosega", "portrait"], "palette": "EGA", "outW": 128, "outH": 128, "thin_lines": "2"}),
    ("style-item", "item — one isolated object icon. 48x48 transparent.",
     {**PXL, **SPRITE, "subject": "a glowing blue health potion in a round glass flask with a cork",
      "styles": ["item"], "outW": 48, "outH": 48}),
    ("style-solo", "solo — exactly one centered subject on a plain background. PICO-8, 96x96 transparent.",
     {**PXL, **SPRITE, "subject": "a wooden treasure chest overflowing with gold coins and jewels",
      "styles": ["solo"], "palette": "PICO-8", "outW": 96, "outH": 96}),
    ("style-scene", "scene — full environment with depth: DOS VGA LoRA, 320x200.",
     {**VGA, "subject": "a bustling medieval market square with stalls, banners and a stone fountain", "kind": "scene background",
      "genre": "adventure game", "styles": ["scene"], "outW": 320, "outH": 200}),
    ("style-r3tr0", "r3tr0 — the retro-game-art LoRA (trigger from the style), 256x224.",
     {**PXL, "lora": "retro-game-art.safetensors", "lora_strength": 0.9,
      "subject": "a spaceship battle above a ringed planet, lasers and explosions",
      "styles": ["r3tr0", "scene"], "outW": 256, "outH": 224}),
    ("style-pixelartredmond", "pixelartredmond — the PixelArtRedmond LoRA (trigger from the style), 256x256.",
     {**PXL, "lora": "pixelartredmond.safetensors", "lora_strength": 0.9,
      "subject": "a cozy log cabin in a snowy forest at night with warm glowing windows",
      "styles": ["pixelartredmond", "scene"], "outW": 256, "outH": 256}),
    ("style-bourassa", "bourassa — gouache + heavy ink, as pixel art. 192x192.",
     {**PXL, "subject": "a battle-scarred crusader in dented armor holding a torch", "styles": ["bourassa", "portrait"],
      "outW": 192, "outH": 192}),
    ("style-mignola", "mignola — flat spot blacks, stark chiaroscuro, as pixel art. Tall 192x256.",
     {**PXL, "subject": "a stone gargoyle perched on a cathedral ledge under a full moon",
      "styles": ["mignola"], "outW": 192, "outH": 256}),
    ("style-painterly", "painterly — ART mode (not pixels): full-res gouache character illustration, 1024x1024.",
     {**ART, "subject": "a grim highwayman in a tricorn hat holding a flintlock pistol", "styles": ["painterly", "portrait"],
      "outW": 1024, "outH": 1024}),
    ("style-painterlyscene", "painterlyscene — ART mode: full-res painted environment, 1024x576 (16:9). No figures.",
     {**ART, "subject": "a ruined gothic abbey on a stormy hill, a broken spire against lightning",
      "styles": ["painterlyscene"], "kind": "scene background", "outW": 1024, "outH": 576}),
]
SPECS += STYLE_SPECS

# the sample image's seed, picked from candidates (--try); anything not listed uses 1000 + 7 * its index
SEEDS = {
    "cga-4-color-bayer": 1164,
    "ega-dungeon-corridor-diagonal": 1401,
    "zx-spectrum-screen": 1328,
    "ega-sprite-transparent": 1237,
    "gameboy-4-shade": 1363,
    "style-clean": 1471,
    "style-detailed": 1377,
    "style-minimal": 1485,
    "style-8bit": 1391,
    "style-16bit": 1297,
    "style-gameboy": 1203,
    "style-geometric": 1412,
    "ega-night-scene": 1216,
    "ega-item-icon-32": 1349,
    "ega-monster-sprite-96": 1457,
    "style-cute": 1426,
    "style-dark": 1433,
    "style-horror": 1339,
    "style-hyperlight": 1447,
    "style-deadcells": 1454,
    "style-blasphemous": 1259,
    "style-owlboy": 1569,
    "style-stardew": 1475,
    "style-dosrpg": 1280,
    "style-darkest": 1489,
    "style-undertale": 1496,
    "style-zelda": 1510,
    "style-hollowknight": 1315,
    "style-metroid": 1423,
    "style-finalfantasy": 1329,
    "style-pokemon": 1538,
    "style-wasteland": 1552,
    "style-portrait": 1660,
    "style-dosega": 1465,
    "style-item": 1371,
    "style-solo": 1479,
    "style-scene": 1587,
    "style-r3tr0": 1594,
    "style-pixelartredmond": 1500,
    "style-bourassa": 1406,
    "style-mignola": 1514,
    "style-painterly": 1521,
    "style-mario": 1503,
    "style-outline": 1217,
    "style-painterlyscene": 1427,
    "style-ega": 1444,
}



def composed_prompt(f):
    p = LORA_PRESETS.get(f["lora"]) or {}
    parts = [f["subject"].strip()]
    if p.get("trigger"):
        parts.append(p["trigger"])
    if p.get("kinds"):
        parts += [f[k] for k in ("kind", "genre", "era") if f[k]]
    return ", ".join(x for x in parts if x)


def params_of(f):
    return {"prompt": composed_prompt(f), "negative": f["negative"], "lora": f["lora"], "lora_strength": f["lora_strength"],
            "styles": f["styles"], "out": f"{f['outW']}x{f['outH']}", "snap_pixels": f["snap_pixels"],
            "transparent": f["transparent"], "art": f["art"], "no_sprite_suffix": f["no_sprite_suffix"],
            "palette": f["palette"], "dither": f["dither"], "dither_amount": f["dither_amount"],
            "despeckle": f["despeckle"], "seed": -1, "n": 1, "name": "", "steps": f["steps"], "cfg": f["cfg"],
            "fast": f["fast"], "pixel_size": f["pixel_size"], "thin_lines": f["thin_lines"],
            "pixel_angles": f["pixel_angles"], "angle_grid": f["angle_grid"]}


def render(form, seed, out):
    """One sample on the render server -> path of the image (or raises with pixelmon's output)."""
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    p = params_of(form)
    p["seed"] = seed                               # the SAMPLE is reproducible; the preset stays unlocked
    argv = server.build_argv(p) + ["--output-to", out]
    r = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1"))
    imgs = sorted(glob.glob(os.path.join(out, "*.png")))
    if r.returncode or not imgs:
        raise RuntimeError(f"{r.stdout[-400:]} {r.stderr[-400:]}")
    return imgs[0], argv


def main(argv):
    tries = 0
    if argv[:1] == ["--try"]:                      # --try N names…: N candidate seeds each, no presets written
        tries, argv = int(argv[1]), argv[2:]
    only = set(argv)
    cand_root = os.path.expanduser("~/pixelmon-gallery/preset-candidates")
    os.makedirs(PRESETS, exist_ok=True)
    for idx, spec in enumerate(SPECS):
        name, note, over = spec[0], spec[1], spec[2]
        if only and name not in only:
            continue
        evo = dict(EVO_DEFAULT, **(spec[3] if len(spec) > 3 else {}))
        form = dict(BASE_FORM, **over)
        base = SEEDS.get(name, 1000 + idx * 7)
        if tries:
            for k in range(tries):
                seed = 1000 + idx * 7 + k * 101
                t = time.time()
                try:
                    img, _ = render(form, seed, os.path.join(WORK, f"{name}-{seed}"))
                except RuntimeError as e:
                    print(f"FAILED {name} s{seed}: {e}")
                    continue
                os.makedirs(os.path.join(cand_root, name), exist_ok=True)
                shutil.copy2(img, os.path.join(cand_root, name, f"s{seed}.png"))
                print(f"try {name:30} s{seed:<6} {time.time() - t:5.1f}s")
            continue
        t = time.time()
        try:
            img, cmd = render(form, base, os.path.join(WORK, name))
        except RuntimeError as e:
            print(f"FAILED {name}: {e}")
            continue
        shutil.copy2(img, os.path.join(PRESETS, name + ".png"))
        rec = {"name": name, "saved": time.time(), "note": f"{note}  (sample: seed {base})",
               "snapshot": {"form": form, "steer": dict(STEER_OFF), "evo": evo}, "image": name + ".png",
               "sample_command": server.shlex.join(["pixelmon"] + cmd[1:-2])}
        with open(os.path.join(PRESETS, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        print(f"ok  {name:30} {time.time() - t:5.1f}s  s{base}")


if __name__ == "__main__":
    main(sys.argv[1:])
