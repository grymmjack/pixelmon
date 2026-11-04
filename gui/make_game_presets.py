"""Game-asset presets for pixelmon-gui: textures (walls, floors, ceilings, doors, paintings, decor) and
animation (characters, effects, portrait loops). Renders one sample per preset on the render server with
the GUI's own build_argv(), then writes <name>.json + its picture into these preset folders:

    GAME TEXTURES      retro VGA pixel art, 64 px, tiling where it should (walls / floors / ceilings)
    GAME TEXTURES HD   512 px material swatches: --surface, tiling, + height / normal / rough / AO maps
    GAME ANIMATION     🎞 Animate tab presets: characters, top-down RPG sets, a turnaround, effects,
                       portrait loops (the card shows a sample GIF)

    ~/ComfyUI/.venv/bin/python gui/make_game_presets.py                    # every missing one
    ~/ComfyUI/.venv/bin/python gui/make_game_presets.py WALL-STONE-BRICK   # just the named ones
    ~/ComfyUI/.venv/bin/python gui/make_game_presets.py --folder "GAME ANIMATION"
    ~/ComfyUI/.venv/bin/python gui/make_game_presets.py --force NAME       # render again, overwrite

A preset that's already there is left alone unless --force. Then `python3 gui/presets_sync.py export
"GAME TEXTURES" "GAME TEXTURES HD" "GAME ANIMATION"` copies them into the repo's presets/.
"""
import glob, json, os, shutil, subprocess, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "gui"))
import server  # noqa: E402  (build_argv, load_presets)

ROOT = os.environ.get('GP_ROOT') or os.path.expanduser('~/pixelmon-gallery/gui-presets')
WORK = tempfile.mkdtemp(prefix="pixelmon-gamepresets-")
LORAS = server.load_presets()
PIXEL_NEG = ("(3d render, realistic, photograph:1.3), (blurry:1.2), smooth gradient, (antialiased:1.1), "
             "jpeg artifacts, (text, watermark, signature:1.3)")
TEX_NEG = PIXEL_NEG + ", (perspective, vanishing point, room, corridor:1.3), (vignette, dark corners, shadow:1.2), people, characters"
ITEM_NEG = "(perspective, angled view:1.2), room, wall around it, people, characters, (text, watermark, signature:1.3), blurry"

BASE_FORM = {"subject": "", "lora": "dosart-vga.safetensors", "lora_strength": 0.8, "kind": "", "genre": "",
             "era": "1990s DOS game", "negative": TEX_NEG, "outW": 64, "outH": 64, "snap_pixels": False,
             "snap_method": "unfake", "transparent": False, "fit_subject": False, "color_match": "oklab",
             "art": False, "raw": False, "no_sprite_suffix": True, "tile": False, "tile_axes": "both",
             "palette": "none", "palette_strength": 0.6, "dither": "none", "dither_amount": 0.5, "despeckle": 1,
             "pixel_size": "1x1", "thin_lines": "0", "pixel_angles": 0, "angle_grid": "pixel", "seed": "",
             "seedLock": False, "n": 1, "name": "", "steps": "", "cfg": "", "fast": False, "showFull": False,
             "exact": False, "exactPos": "", "exactNeg": "", "exactDirty": False, "styles": []}
STEER_OFF = {"use": False, "strength": 0.7, "weight": "style transfer", "combine": "concat", "sel": [], "start": 0, "end": 1}
EVO_DEFAULT = {"parent": None, "init": True, "denoise": 0.45, "steerOn": True, "steer": 0.6,
               "weight": "style and composition", "n": 4, "start": 0, "end": 1, "perChild": False}
AN_BASE = {"mode": "moves", "char": "", "moves": ["walk"], "dirs": "1", "facing": "right", "view": "side", "size": 64,
           "effect": "fire", "fxlook": "", "fxsize": 48, "gesture": "", "region": "", "seed": "", "name": "",
           "fine": {"moves": {}, "effect": {}, "portrait": {}}}

TILE = "(seamless tileable texture:1.3), flat, seen straight on, even lighting, fills the frame"
FRONT = "(front view:1.2), flat, seen straight on, even lighting, fills the frame"

# ---- GAME TEXTURES: name, note, subject, w, h, tiles?
PIXEL = [
    ("WALL-STONE-BRICK", "Dungeon wall: grey stone bricks. Tiles both ways.", "large grey stone bricks, only five rows of bricks, thick dark mortar lines", 64, 64, True),
    ("WALL-CASTLE-BLOCKS", "Big rough castle blocks.", "large rough grey castle stone blocks, moss in the cracks", 64, 64, True),
    ("WALL-RED-BRICK", "Red brick wall, town or warehouse.", "red brick wall, light grey mortar", 64, 64, True),
    ("WALL-WOOD-PLANKS", "Cabin / tavern wall: upright planks.", "old wooden plank wall, six wide vertical boards, each board a different shade of brown, dark vertical gaps between boards, two nails at the top of each board", 64, 64, True),
    ("WALL-SCIFI-PANEL", "Sci-fi base wall: metal panels, rivets, a light strip.", "square grey metal sci-fi wall panels in a grid, straight horizontal and vertical seams, rivets at the corners, a thin glowing cyan light strip", 64, 64, True),
    ("WALL-NEON-TECH", "Cyberpunk wall: dark plating with magenta neon.", "dark blue tech wall plating with glowing magenta neon lines, vents", 64, 64, True),
    ("WALL-CAVE-ROCK", "Cave or mine wall.", "rough brown cave rock wall, cracks", 64, 64, True),
    ("WALL-WALLPAPER", "Haunted mansion wallpaper.", "faded green damask wallpaper with gold pattern, old and stained", 64, 64, True),
    ("WALL-BOOKSHELF", "A wall of books.", "bookshelf full of old leather books, wooden shelves", 64, 64, True),
    ("FLOOR-FLAGSTONE", "Dungeon floor: flagstones.", "grey flagstone floor, irregular stones, dark gaps", 64, 64, True),
    ("FLOOR-WOODEN", "Wooden floorboards.", "wooden floor, six wide planks running horizontally, staggered plank ends, dark straight gaps between planks", 64, 64, True),
    ("FLOOR-COBBLESTONE", "Town street cobblestones.", "rounded grey cobblestones packed together, dark gaps, worn smooth", 64, 64, True),
    ("FLOOR-RUSTY-METAL", "Rusty industrial metal floor.", "rusty corroded steel floor sheet, orange rust patches, scratches", 64, 64, True),
    ("FLOOR-GRASS", "Grass ground.", "green grass ground, small flowers", 64, 64, True),
    ("FLOOR-DIRT", "Dirt path with pebbles.", "brown dirt ground with pebbles", 64, 64, True),
    ("FLOOR-CARPET", "Red patterned carpet (palace, hotel).", "red patterned carpet with gold border pattern", 64, 64, True),
    ("FLOOR-LAVA", "Glowing lava.", "glowing orange lava with dark cooled crust", 64, 64, True),
    ("FLOOR-WATER", "Deep water.", "deep blue water with light ripples", 64, 64, True),
    ("CEIL-WOOD-BEAMS", "Wooden beams ceiling.", "dark wooden ceiling planks crossed by thick beams", 64, 64, True),
    ("CEIL-SCIFI-LIGHT", "Sci-fi ceiling panel with a recessed light.", "grey metal sci-fi ceiling panel with a square recessed white light", 64, 64, True),
    ("CEIL-PLASTER", "Old cracked plaster ceiling.", "old off-white plaster ceiling, fine cracks, water stains", 64, 64, True),
    ("CEIL-CAVE", "Dark cave ceiling.", "dark rough cave ceiling rock, small stalactite tips", 64, 64, True),
    ("DOOR-DUNGEON", "Wooden dungeon door, iron bands, ring handle.", "a heavy wooden dungeon door with iron bands and a ring handle", 64, 64, False),
    ("DOOR-SCIFI", "Sci-fi blast door with hazard stripes.", "a sci-fi sliding blast door, yellow and black hazard stripes, metal", 64, 64, False),
    ("DOOR-CASTLE-GATE", "Studded double gate.", "a wooden castle double gate with iron studs and hinges", 64, 64, False),
    ("DOOR-HOUSE", "A house front door.", "a red wooden house front door with a brass knob and a small window", 64, 64, False),
    ("PAINTING-PORTRAIT", "A framed portrait (48 x 64).", "an oil painting portrait of a stern nobleman in an ornate gold frame", 48, 64, False),
    ("PAINTING-LANDSCAPE", "A framed landscape (64 x 48).", "a landscape oil painting of mountains and a lake in an ornate gold frame", 64, 48, False),
    ("PAINTING-CREEPY", "A haunted-house portrait (48 x 64).", "a creepy oil painting of a pale woman with dark hollow eyes in a black frame", 48, 64, False),
    ("POSTER-SCIFI", "A retro sci-fi poster (48 x 64).", "a retro sci-fi propaganda poster with a rocket and a planet", 48, 64, False),
    ("BANNER-HERALDIC", "A hanging heraldic banner (32 x 64).", "a red heraldic banner with a gold lion, hanging", 32, 64, False),
    ("WINDOW-STAINED-GLASS", "A gothic stained glass window (48 x 64).", "a gothic arched stained glass window, colourful glass, lead lines", 48, 64, False),
    ("CRATE-SIDE", "A wooden crate's side.", "the side of a wooden crate, planks and a cross brace", 64, 64, False),
    ("CONSOLE-SCREEN", "A computer console panel.", "a sci-fi computer console panel with a green screen, buttons and dials", 64, 64, False),
]
# a sample seed picked from candidates (else 3000 + 13 x its place in the list); SDXL can't hold a regular grid
# (checkerboard, diamond plate, office tiles came out as diagonal scraps at every wording), so none of those
SEED_OF = {"WALL-WOOD-PLANKS": 5101, "FLOOR-WOODEN": 5100, "SKELETON-ENEMY": 6100, "ZOMBIE-ENEMY": 6200}
# ---- GAME TEXTURES HD: name, note, surface words (--surface adds "seen straight on, even light…")
HD = [
    ("HD-STONE-BRICK", "worn grey stone brick wall, dark mortar lines"),
    ("HD-RED-BRICK", "dark red clay brick wall, every brick red, thin grey mortar lines, some chipped bricks"),
    ("HD-WOOD-PLANKS", "weathered vertical wooden boards side by side, dark gaps between the boards, nail heads"),
    ("HD-FLOORBOARDS", "polished oak floorboards running left to right"),
    ("HD-FLAGSTONE", "grey flagstone floor, irregular stones"),
    ("HD-METAL-PANEL", "square grey sci-fi metal wall panels in a grid, straight seams, rivets at the corners"),
    ("HD-COBBLESTONE", "rounded grey cobblestones packed together, dark gaps, worn smooth"),
    ("HD-RUSTY-METAL", "rusty corroded steel sheet with peeling paint"),
    ("HD-CONCRETE", "stained grey concrete wall with small holes"),
    ("HD-MARBLE", "white marble with grey veins, polished"),
    ("HD-GRASS", "short green grass seen from above"),
    ("HD-DIRT", "brown dirt with pebbles seen from above"),
    ("HD-SAND", "fine desert sand with small ripples seen from above"),
    ("HD-PLASTER", "old off-white plaster wall, fine cracks, water stains"),
]
# ---- GAME ANIMATION characters: name, note, character, moves, dirs, view, size, sample (moves, dirs)
CHARS = [
    ("PLATFORMER-KNIGHT", "Side-scroller hero: walk, run, jump, attack, hurt, death.", "a knight in blue armour with a red cape",
     ["walk", "run", "jump", "attack", "hurt", "death"], "1", "side", 64, (["walk"], "1")),
    ("PLATFORMER-NINJA", "Fast hero: idle, run, jump, attack.", "a ninja in black with a red scarf and a katana",
     ["idle", "run", "jump", "attack"], "1", "side", 64, (["run"], "1")),
    ("WIZARD", "Spell caster: idle, walk, attack, hurt, death.", "an old wizard in a purple robe with a long white beard and a wooden staff",
     ["idle", "walk", "attack", "hurt", "death"], "1", "side", 64, (["walk"], "1")),
    ("SKELETON-ENEMY", "Enemy: walk, attack, hurt, death.", "a skeleton warrior, bare white skull with no hair, bony ribs, a rusty sword and a round wooden shield",
     ["walk", "attack", "hurt", "death"], "1", "side", 64, (["walk"], "1")),
    ("ZOMBIE-ENEMY", "Slow enemy: walk, attack, death.", "a shambling green zombie in torn grey clothes",
     ["walk", "attack", "death"], "1", "side", 64, (["walk"], "1")),
    ("SPACE-MARINE", "Run-and-gun: idle, run, attack, hurt, death.", "a space marine in bulky green power armour with a big rifle",
     ["idle", "run", "attack", "hurt", "death"], "1", "side", 64, (["run"], "1")),
    ("BRAWLER", "Beat-'em-up fighter: idle, walk, attack, hurt.", "a street brawler with a white tank top, jeans and red boxing tape on his fists",
     ["idle", "walk", "attack", "hurt"], "1", "side", 64, (["idle"], "1")),
    ("RPG-HERO-4WAY", "Top-down RPG hero, 4 directions: walk, idle. Sample: the turnaround.", "a young adventurer in a green tunic with a short sword",
     ["walk", "idle"], "4", "topdown", 48, (["stand"], "4")),
    ("RPG-VILLAGER-4WAY", "Top-down RPG townsperson, 4 directions: walk. Sample: the turnaround.", "a village woman in a brown dress and a white apron",
     ["walk"], "4", "topdown", 48, (["stand"], "4")),
    ("TURNAROUND-8WAY", "An 8-way turnaround of one character (stand in 8 directions).", "a red-haired rogue in a brown leather hood and cloak",
     ["stand"], "8", "side", 64, (["stand"], "8")),
]
# ---- effects: name, note, effect, look, size
FX = [
    ("CAMPFIRE", "A looping fire.", "fire", "", 48),
    ("MAGIC-FIRE-GREEN", "Green magic fire, looping.", "fire", "green magic fire", 48),
    ("EXPLOSION", "A one-shot explosion: flash, fireball, smoke, gone.", "explosion", "", 64),
    ("SMOKE-PUFF", "A rising puff of smoke, looping.", "smoke", "", 48),
    ("MAGIC-SPARKLES", "Golden sparkles, looping.", "sparkle", "golden magic sparkles", 48),
    ("ELECTRIC-SPARKS", "Blue electric sparks, looping.", "sparkle", "blue electric sparks", 48),
]
# ---- portrait loops: name, note, subject, gesture, region
PORTRAITS = [
    ("PORTRAIT-BLINK", "A portrait that blinks now and then.", "a grizzled space captain with a scar and a grey beard", "blink", ""),
    ("PORTRAIT-TALK", "A talking NPC portrait.", "a cheerful innkeeper with a bushy moustache", "talk", ""),
    ("PORTRAIT-CIGAR-SMOKE", "A noir detective, smoke rising from his cigar.", "a noir detective in a fedora with a cigar in his mouth", "smoke", "the cigar"),
]


def composed(f):
    p = LORAS.get(f["lora"]) or {}
    parts = [f["subject"].strip()] + ([p["trigger"]] if p.get("trigger") else [])
    if p.get("kinds"):
        parts += [f[k] for k in ("kind", "genre", "era") if f[k]]
    return ", ".join(x for x in parts if x)


def form_params(f, adv=None):
    """The GUI's params() for a form (what the ▶ RENDER button sends)."""
    return {"prompt": composed(f), "negative": f["negative"], "lora": f["lora"], "lora_strength": f["lora_strength"],
            "styles": f["styles"], "out": f"{f['outW']}x{f['outH']}", "snap_pixels": f["snap_pixels"],
            "fit_subject": f["fit_subject"], "color_match": f["color_match"], "transparent": f["transparent"],
            "art": f["art"], "raw": False, "no_sprite_suffix": f["no_sprite_suffix"],
            "tile": f["tile_axes"] if f["tile"] else "", "palette": f["palette"], "palette_strength": f["palette_strength"],
            "dither": f["dither"], "dither_amount": f["dither_amount"], "despeckle": f["despeckle"],
            "pixel_angles": f["pixel_angles"], "angle_grid": f["angle_grid"], "pixel_size": f["pixel_size"],
            "thin_lines": f["thin_lines"], "seed": -1, "n": 1, "name": "", "steps": f["steps"], "cfg": f["cfg"],
            "fast": f["fast"], "adv": dict(adv or {})}


def anim_params(f, a):
    """The 🎞 Animate tab's job (anGo) for a form + tab settings."""
    trig = (LORAS.get(f["lora"]) or {}).get("trigger") or ""
    if a["mode"] == "effect":
        raw, sprite = a["fxlook"], {"kind": "effect", "effect": a["effect"], "size": a["fxsize"]}
    else:
        raw, sprite = a["char"], {"kind": "moves", "moves": a["moves"], "directions": a["dirs"], "facing": a["facing"],
                                  "view": a["view"], "size": a["size"]}
    sprite.update(a["fine"][a["mode"]])
    prompt = f"{raw}, {trig}" if raw and trig else raw
    return {"prompt": prompt, "lora": f["lora"], "lora_strength": f["lora_strength"], "palette": f["palette"],
            "seed": -1, "n": 1, "name": "", "sprite": sprite}


def run(p, seed, out):
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    p = dict(p, seed=seed)
    argv = server.build_argv(p) + ["--output-to", out]
    r = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1"))
    files = sorted(glob.glob(os.path.join(out, "**", "*.*"), recursive=True))
    if r.returncode or not files:
        raise RuntimeError(f"{r.stdout[-500:]} {r.stderr[-500:]}")
    return files, argv


def card(files, prefer):
    """The picture a preset card shows: an animation's big GIF, a texture's 3x3 tiling check, else the picture."""
    imgs = [f for f in files if f.lower().endswith((".png", ".gif"))]
    for pick in prefer:
        hit = [f for f in imgs if pick(os.path.basename(f))]
        if hit:
            return hit[0]
    return imgs[0]


def save_card(pic, stem, cap=768):
    """The card picture: as made, except a big one (an HD texture's 3x3 repeat is 1536 px, ~1.5 MB as PNG)
    goes in as a cap-px JPEG, so the repo's presets/ stays small. Returns the file name."""
    from PIL import Image
    ext = os.path.splitext(pic)[1].lower()
    if ext == ".png":
        with Image.open(pic) as im:
            if max(im.size) > cap:
                k = cap / max(im.size)
                im.convert("RGB").resize((round(im.width * k), round(im.height * k)), Image.LANCZOS).save(
                    stem + ".jpg", quality=88, optimize=True)
                return os.path.basename(stem) + ".jpg"
    shutil.copy2(pic, stem + ext)
    return os.path.basename(stem) + ext


MAPS = ("_height.png", "_normal.png", "_rough.png", "_ao.png")
plain = lambda n: n.endswith(".png") and "_tiled_" not in n and not n.endswith(MAPS) and "_cutmask" not in n


def specs():
    """(folder, name, note, form, adv, anim or None, sample request, card preference)"""
    out = []
    for name, note, subj, w, h, tile in PIXEL:
        # The art route shrunk to 64 px: pixel mode with a LoRA turned a busy repeat into coloured noise at this size
        # (it pixelates a 1024 render 16x down), and drew doors as sprites on white. --surface keeps walls / floors /
        # ceilings square on and evenly lit; a door or painting is an art render seen from the front.
        f = dict(BASE_FORM, subject=subj if tile else f"{subj}, {FRONT}", lora="(none)", lora_strength=1.0, era="",
                 negative="" if tile else ITEM_NEG, outW=w, outH=h, art=True, tile=tile, despeckle=0)
        adv = {"surface": True} if tile else {}
        out.append(("GAME TEXTURES", name, note, f, adv, None, form_params(f, adv), [lambda n: "_tiled_" in n, plain]))
    for name, subj in HD:
        f = dict(BASE_FORM, subject=subj, lora="(none)", lora_strength=1.0, era="", negative="", outW=512, outH=512,
                 art=True, tile=True, despeckle=0)
        adv = {"surface": True, "maps": True}
        note = f"512 px material, tiles, + height / normal / rough / AO maps: {subj}."
        out.append(("GAME TEXTURES HD", name, note, f, adv, None, form_params(f, adv), [lambda n: "_tiled_" in n, plain]))
    sprite_form = dict(BASE_FORM, lora="pixel-art-xl.safetensors", lora_strength=1.0, kind="game sprite", era="",
                       negative=PIXEL_NEG, despeckle=0)
    for name, note, char, moves, dirs, view, size, (smoves, sdirs) in CHARS:
        f = dict(sprite_form, subject=char, outW=size, outH=size)
        a = dict(json.loads(json.dumps(AN_BASE)), mode="moves", char=char, moves=moves, dirs=dirs, view=view, size=size)
        sample = dict(a, moves=smoves, dirs=sdirs)
        want = (lambda n: n.endswith("_turnaround_x4.gif")) if smoves == ["stand"] and sdirs != "1" else (lambda n: n.endswith("_x4.gif"))
        out.append(("GAME ANIMATION", name, note, f, {}, dict(a, active=True), anim_params(f, sample), [want, plain]))
    for name, note, eff, look, size in FX:
        f = dict(sprite_form, subject=look or eff, outW=size, outH=size)
        a = dict(json.loads(json.dumps(AN_BASE)), mode="effect", effect=eff, fxlook=look, fxsize=size)
        out.append(("GAME ANIMATION", name, note, f, {}, dict(a, active=True), anim_params(f, a), [lambda n: n.endswith("_x4.gif"), plain]))
    for name, note, subj, gesture, region in PORTRAITS:
        f = dict(BASE_FORM, subject=subj, kind="character portrait", negative=PIXEL_NEG, outW=128, outH=128, despeckle=1)
        a = dict(json.loads(json.dumps(AN_BASE)), mode="portrait", gesture=gesture, region=region)
        adv = {"animate": gesture, **({"anim_region": region} if region else {})}
        out.append(("GAME ANIMATION", name, note, f, {}, dict(a, active=True), form_params(f, adv), [lambda n: n.endswith(".gif"), plain]))
    return out


def main(args):
    force = "--force" in args
    args = [x for x in args if x != "--force"]
    folder_only = None
    if "--folder" in args:
        i = args.index("--folder")
        folder_only, args = args[i + 1], args[:i] + args[i + 2:]
    only = set(args)
    all_specs = specs()
    for folder in sorted({s[0] for s in all_specs}):         # each folder's display order: as listed here
        d = os.path.join(ROOT, folder)
        os.makedirs(d, exist_ok=True)
        names = [s[1] for s in all_specs if s[0] == folder]
        server._write_json(os.path.join(d, server.PRESET_ORDER), names)
    done = failed = 0
    for idx, (folder, name, note, form, adv, anim, req, prefer) in enumerate(all_specs):
        if (only and name not in only) or (folder_only and folder != folder_only):
            continue
        d = os.path.join(ROOT, folder)
        if os.path.exists(os.path.join(d, name + ".json")) and not force:
            print(f"have {folder} / {name}")
            continue
        seed = SEED_OF.get(name, 3000 + idx * 13)
        t = time.time()
        try:
            files, argv = run(req, seed, os.path.join(WORK, name))
        except RuntimeError as e:
            print(f"FAILED {name}: {e}", flush=True)
            failed += 1
            continue
        pic = card(files, prefer)
        for old in glob.glob(os.path.join(d, name + ".*")):
            os.remove(old)
        image = save_card(pic, os.path.join(d, name))
        snap = {"form": form, "steer": dict(STEER_OFF), "evo": dict(EVO_DEFAULT), "adv": dict(adv)}
        if anim:
            snap["anim"] = anim
        rec = {"name": name, "saved": time.time(), "note": f"{note}  (sample: seed {seed})", "snapshot": snap,
               "image": image, "sample_command": server.shlex.join(["pixelmon"] + argv[1:-2])}
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        done += 1
        print(f"ok   {folder} / {name:24} {time.time() - t:6.1f}s  s{seed}", flush=True)
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"done: {done} made, {failed} failed")


if __name__ == "__main__":
    main(sys.argv[1:])
