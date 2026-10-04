"""Build a GAME-ASSET preset set (UNFAKE folder): seamless textures, doors, props, items, monsters, characters
and parallax backgrounds — the things a game actually needs — in a spread of pixel-art styles, all finished
with unfake grid snapping.

usage: make_asset_presets.py [names…]   (no names = all)
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_starter_presets as msp  # noqa: E402

DST = os.path.expanduser("~/pixelmon-gallery/gui-presets/UNFAKE")
WORK = os.path.expanduser("~/pixelmon-gallery/preset-work-assets")

NEG = msp.PIXEL_NEG_W
SPRITE_NEG = NEG + ", (multiple, sprite sheet, collage:1.3), cropped, (background scenery:1.2), (tiny, small in frame, distant:1.2)"

BASE = dict(msp.BASE_FORM, lora="pixel-art-xl.safetensors", lora_strength=1.0, kind="", genre="", era="",
            negative=NEG, palette="none", snap_pixels=True, snap_method="unfake", despeckle=0,
            thin_lines="0", pixel_angles=0, tile=False, tile_axes="both")
TEX = dict(BASE, outW=64, outH=64, tile=True)                                     # seamless 64px texture
SPRITE = dict(BASE, outW=64, outH=64, transparent=True, despeckle=1, negative=SPRITE_NEG, styles=["solo"])
ICON = dict(SPRITE, outW=32, outH=32, styles=["item", "solo"], despeckle=0)   # solo: fills the frame; despeckle 0 keeps thin shapes
BIG = dict(SPRITE, outW=96, outH=96)
DOOR = dict(BASE, outW=64, outH=96, no_sprite_suffix=True, despeckle=1)
BG = dict(BASE, outW=256, outH=144, tile=True, tile_axes="x", no_sprite_suffix=True)   # parallax: wraps sideways (1024x576 / 4)

# name, note, base, overrides
SPECS = [
    # ---- seamless textures (64x64, tile both ways) ----
    ("tex-dungeon-stone-wall", "Seamless dungeon wall: rough grey stone blocks.", TEX,
     {"subject": "rough grey stone dungeon wall, large irregular stone blocks, dark mortar lines"}),
    ("tex-red-brick-wall", "Seamless red brick wall.", TEX, {"subject": "red brick wall, staggered bricks, light mortar"}),
    ("tex-mossy-castle-wall", "Seamless mossy castle wall, SNES look.", TEX,
     {"subject": "old castle stone wall with green moss and cracks", "styles": ["16bit"]}),
    ("tex-wood-plank-floor", "Seamless wooden floor planks.", TEX,
     {"subject": "wooden plank floor, warm brown boards, wood grain, nail heads"}),
    ("tex-cobblestone-floor", "Seamless cobblestone street / courtyard floor.", TEX,
     {"subject": "grey cobblestone floor, rounded stones, dark gaps"}),
    ("tex-dungeon-floor-tiles", "Seamless cracked stone floor tiles.", TEX,
     {"subject": "square stone dungeon floor tiles, cracked, worn, dark grout", "styles": ["dark"]}),
    ("tex-cave-rock-ceiling", "Seamless cave rock (ceilings, cave walls).", TEX,
     {"subject": "dark rough cave rock surface, craggy stone, stalactite bumps"}),
    ("tex-wood-beam-ceiling", "Seamless wooden ceiling with beams.", TEX,
     {"subject": "wooden ceiling boards crossed by thick dark wooden beams, seen from below"}),
    ("tex-grass-ground", "Seamless grass ground for top-down maps.", TEX,
     {"subject": "lush green grass ground seen from above, tufts, tiny flowers", "styles": ["stardew"]}),
    ("tex-dirt-path", "Seamless dirt path / soil.", TEX, {"subject": "brown dirt path seen from above, pebbles, packed soil"}),
    ("tex-desert-sand", "Seamless desert sand.", TEX, {"subject": "golden desert sand seen from above, small dunes and ripples"}),
    ("tex-water", "Seamless water.", TEX, {"subject": "rippling blue water surface seen from above, light glints"}),
    ("tex-lava", "Seamless glowing lava.", TEX, {"subject": "glowing molten lava, orange and yellow cracks in black cooling crust"}),
    ("tex-snow-ice", "Seamless snow and ice.", TEX, {"subject": "white snow ground with patches of pale blue ice, seen from above"}),
    ("tex-scifi-metal-floor", "Seamless sci-fi metal floor plates.", TEX,
     {"subject": "sci-fi metal floor plates, rivets, grooves, hazard stripes", "styles": ["metroid"]}),
    ("tex-scifi-wall-panels", "Seamless sci-fi wall panels with lights.", TEX,
     {"subject": "sci-fi spaceship wall panels, vents, small glowing lights, cables", "styles": ["hyperlight"]}),
    ("tex-roof-shingles", "Seamless clay roof shingles.", TEX, {"subject": "red clay roof shingles, overlapping rows"}),
    ("tex-royal-carpet", "Seamless ornate royal carpet.", TEX,
     {"subject": "ornate royal red carpet pattern with gold trim motifs"}),
    ("tex-nes-brick-32", "Seamless 32px NES brick block, NES palette.", TEX,
     {"subject": "brick block wall", "outW": 32, "outH": 32, "palette": "NES", "styles": ["8bit"]}),
    ("tex-gameboy-ground-32", "Seamless 32px Game Boy ground, 4 greens.", TEX,
     {"subject": "grass and dirt ground seen from above", "outW": 32, "outH": 32, "palette": "GAMEBOY", "styles": ["gameboy"]}),

    # ---- doors and gates ----
    ("door-wooden-dungeon", "Dungeon door: heavy wood + iron bands in a stone arch.", DOOR,
     {"subject": "front view of a heavy wooden dungeon door with iron bands and a ring handle, set in a stone archway"}),
    ("door-iron-portcullis", "Castle portcullis gate.", DOOR,
     {"subject": "front view of an iron portcullis gate in a castle stone archway, dark passage behind"}),
    ("door-scifi-bulkhead", "Sci-fi sliding bulkhead door.", DOOR,
     {"subject": "front view of a sci-fi sliding bulkhead door, hazard stripes, glowing control panel", "styles": ["metroid"]}),
    ("door-castle-gate", "Big double castle gate.", dict(DOOR, outW=96, outH=96),
     {"subject": "front view of a large wooden double castle gate with iron studs, stone towers either side"}),

    # ---- props ----
    ("prop-treasure-chest", "Closed treasure chest.", SPRITE,
     {"subject": "a closed wooden treasure chest with gold trim and a lock, three-quarter view"}),
    ("prop-treasure-chest-open", "Open chest full of gold.", SPRITE,
     {"subject": "an open treasure chest overflowing with gold coins and gems, three-quarter view"}),
    ("prop-barrel", "Wooden barrel.", SPRITE, {"subject": "a wooden barrel with iron hoops"}),
    ("prop-crate", "Wooden crate.", SPRITE, {"subject": "a wooden crate, three-quarter view"}),
    ("prop-wall-torch", "Burning wall torch.", SPRITE, {"subject": "a burning torch in an iron wall sconce, bright flame"}),
    ("prop-bookshelf", "Tall bookshelf.", dict(SPRITE, outW=64, outH=96),
     {"subject": "a tall wooden bookshelf full of colorful books, front view"}),
    ("prop-altar", "Stone altar with candles.", SPRITE,
     {"subject": "a stone altar with a glowing rune and candles, front view", "styles": ["solo", "dark"]}),
    ("prop-cauldron", "Bubbling witch's cauldron.", SPRITE, {"subject": "a black iron cauldron bubbling with green potion"}),
    ("prop-skull-pile", "Pile of skulls and bones.", SPRITE, {"subject": "a pile of skulls and bones", "styles": ["solo", "darkest"]}),
    ("prop-lever", "Wall lever / switch.", dict(SPRITE, outW=32, outH=32), {"subject": "a wooden lever switch on a stone wall plate"}),

    # ---- item icons (32x32) ----
    ("item-health-potion", "Icon: red health potion.", ICON, {"subject": "a red health potion in a round glass bottle with a cork"}),
    ("item-iron-sword", "Icon: iron sword.", ICON, {"subject": "an iron sword, diagonal, blade pointing up-right"}),
    ("item-wooden-shield", "Icon: round wooden shield.", ICON, {"subject": "a round wooden shield with a metal boss"}),
    ("item-gold-key", "Icon: gold key.", ICON, {"subject": "an ornate gold key"}),
    ("item-gold-coins", "Icon: stack of gold coins.", ICON, {"subject": "a small stack of shiny gold coins"}),
    ("item-magic-scroll", "Icon: magic scroll.", ICON, {"subject": "a rolled parchment magic scroll with a red wax seal"}),
    ("item-gem", "Icon: cut gemstone.", ICON, {"subject": "a sparkling blue cut gemstone"}),
    ("item-heart", "Icon: heart / health pickup.", ICON, {"subject": "a red heart health pickup", "styles": ["item", "8bit"]}),

    # ---- monsters ----
    ("mon-slime", "Monster: green slime.", SPRITE, {"subject": "a green slime monster blob with big eyes", "styles": ["solo", "cute"]}),
    ("mon-skeleton-warrior", "Monster: skeleton warrior.", SPRITE,
     {"subject": "a skeleton warrior with a rusty sword and shield, full body, side view", "styles": ["solo", "darkest"]}),
    ("mon-goblin", "Monster: goblin with a dagger.", SPRITE,
     {"subject": "a green goblin with a dagger, full body, side view", "styles": ["solo", "16bit"]}),
    ("mon-orc-brute", "Monster: big orc brute (96px).", BIG,
     {"subject": "a huge orc brute with a spiked club, full body, side view", "styles": ["solo", "16bit"]}),
    ("mon-giant-spider", "Monster: giant spider.", SPRITE, {"subject": "a giant black spider with red markings, side view", "styles": ["solo", "dark"]}),
    ("mon-vampire-bat", "Monster: vampire bat (colored, not a silhouette).", SPRITE,
     {"subject": "a dark purple vampire bat with pink wing membranes and red eyes, wings spread, flying"}),
    ("mon-ghost", "Monster: ghost.", SPRITE, {"subject": "a pale blue floating ghost with glowing eyes, wispy tail"}),
    ("mon-giant-rat", "Monster: giant rat.", SPRITE, {"subject": "a giant brown sewer rat, side view", "styles": ["solo", "8bit"]}),
    ("mon-mimic", "Monster: treasure-chest mimic.", SPRITE,
     {"subject": "a treasure chest mimic monster with sharp teeth and a long tongue"}),
    ("mon-floating-eye", "Monster: floating eyeball.", SPRITE,
     {"subject": "a floating eyeball monster with small tentacles", "styles": ["solo", "pokemon"]}),
    ("mon-fire-demon", "Monster: fire demon (96px).", BIG,
     {"subject": "a horned fire demon with flaming wings, full body, front view", "styles": ["solo", "deadcells"]}),
    ("mon-red-dragon", "Boss: red dragon (128px).", dict(BIG, outW=128, outH=128),
     {"subject": "a red dragon, wings spread, full body, side view", "styles": ["solo", "detailed"]}),
    ("mon-zombie", "Monster: shambling zombie.", SPRITE, {"subject": "a shambling green-skinned zombie in rags, full body, side view"}),
    ("mon-mushroom", "Monster: walking mushroom.", SPRITE,
     {"subject": "a walking red-capped mushroom creature", "styles": ["solo", "stardew"]}),

    # ---- characters ----
    ("char-knight", "Hero: armored knight.", SPRITE, {"subject": "an armored knight hero with a sword and blue cape, full body, side view", "styles": ["solo", "16bit"]}),
    ("char-wizard", "Hero: wizard.", SPRITE, {"subject": "a wizard in a purple robe and pointed hat holding a glowing staff, full body"}),
    ("char-rogue", "Hero: hooded rogue.", SPRITE, {"subject": "a hooded rogue with twin daggers, full body, side view", "styles": ["solo", "dark"]}),
    ("char-cleric", "Hero: cleric.", SPRITE, {"subject": "a cleric in white robes with a mace and holy symbol, full body"}),
    ("char-villager", "NPC: villager.", SPRITE, {"subject": "a friendly villager farmer in simple clothes, full body", "styles": ["solo", "stardew"]}),
    ("char-shopkeeper", "NPC: shopkeeper.", SPRITE, {"subject": "a bearded shopkeeper with an apron, waist up", "styles": ["solo", "outline"]}),
    ("char-topdown-hero-32", "Top-down hero, 32px.", dict(SPRITE, outW=32, outH=32),
     {"subject": "a young hero in a green tunic, top-down view, facing down", "styles": ["solo", "zelda"]}),

    # ---- parallax backgrounds (wrap sideways) ----
    ("bg-parallax-mountains", "Parallax layer: mountains (wraps sideways).", BG,
     {"subject": "distant purple mountain range under a sunset sky, layered silhouettes"}),
    ("bg-parallax-forest", "Parallax layer: forest (wraps sideways).", BG,
     {"subject": "dense pine forest silhouettes against a dusk sky", "styles": ["owlboy"]}),
    ("bg-night-city", "Parallax layer: night city skyline (wraps sideways).", BG,
     {"subject": "night city skyline with lit windows and a starry sky", "styles": ["hyperlight"]}),
]


# second pass: what the first render showed (subjects too small, silhouettes from dark styles, noise) — prompt
# fixes and new seeds. "seed" overrides the default 3000 + index * 7.
BIG_SUBJECT = ", (large, filling the whole frame:1.3)"
FIX = {
    "tex-snow-ice": {"subject": "fresh white snow ground with soft blue shadows and a few small ice patches, seen from above", "seed": 4101},
    "tex-wood-beam-ceiling": {"subject": "wooden ceiling planks running left to right, crossed by thick dark wooden beams, seen from below", "seed": 4103},
    "tex-royal-carpet": {"subject": "ornate royal red carpet, repeating gold diamond pattern, clean symmetric motifs", "seed": 4105},
    "tex-nes-brick-32": {"subject": "orange brick block wall, regular rows of bricks, dark mortar lines", "seed": 4107},
    "tex-water": {"subject": "calm blue water surface seen from above, gentle wave highlights", "seed": 4109},
    "door-wooden-dungeon": {"subject": "front view of a heavy wooden dungeon door with iron bands and a ring handle, set in a dark grey stone wall archway, the door fills the frame", "seed": 4111},
    "door-iron-portcullis": {"subject": "front view of an iron portcullis gate in a dark grey castle stone archway, black passage behind, the gate fills the frame", "seed": 4113},
    "door-scifi-bulkhead": {"subject": "front view of a closed sci-fi sliding metal bulkhead door with yellow hazard stripes in a dark metal wall, the door fills the frame", "seed": 4115},
    "door-castle-gate": {"subject": "front view of a large closed wooden double castle gate with iron studs between grey stone towers, fills the frame", "seed": 4117},
    "prop-treasure-chest": {"subject": "a closed wooden treasure chest with gold trim and a lock, three-quarter view" + BIG_SUBJECT, "seed": 4119},
    "prop-skull-pile": {"subject": "one small heap of three skulls and some bones on the ground" + BIG_SUBJECT, "styles": ["solo"], "seed": 4121},
    "prop-lever": {"subject": "a single wooden pull lever on an iron wall plate" + BIG_SUBJECT, "outW": 48, "outH": 48, "seed": 4123},
    "mon-goblin": {"subject": "one single green goblin with a dagger, full body, side view" + BIG_SUBJECT, "seed": 4125},
    "mon-giant-spider": {"subject": "a giant purple spider with orange markings and glowing red eyes, side view" + BIG_SUBJECT, "styles": ["solo"], "seed": 4127},
    "mon-mimic": {"subject": "a treasure chest mimic monster with sharp teeth and a long tongue, mouth open" + BIG_SUBJECT, "seed": 4129},
    "mon-mushroom": {"subject": "a walking red-capped mushroom creature with little legs and a face" + BIG_SUBJECT, "seed": 4131},
    "mon-zombie": {"subject": "a shambling green-skinned zombie in torn brown rags, arms forward, full body, side view" + BIG_SUBJECT, "seed": 4133},
    "char-rogue": {"subject": "a rogue in a dark green hooded cloak and brown leather armor with twin daggers, full body, side view" + BIG_SUBJECT, "styles": ["solo"], "seed": 4135},
    "char-cleric": {"subject": "a cleric in white and gold robes with a mace and holy symbol, full body" + BIG_SUBJECT, "seed": 4137},
    "char-topdown-hero-32": {"subject": "a young hero in a green tunic, whole body, top-down view, facing down" + BIG_SUBJECT, "seed": 4139},
    **{n: {"subject": subj + BIG_SUBJECT, "seed": 4141 + 2 * i} for i, (n, subj) in enumerate([
        ("item-health-potion", "a red health potion in a round glass bottle with a cork"),
        ("item-iron-sword", "an iron sword with a brown leather grip, diagonal"),
        ("item-gold-key", "an ornate gold key"),
        ("item-gold-coins", "a small stack of shiny gold coins"),
        ("item-magic-scroll", "a rolled parchment magic scroll with a red wax seal"),
        ("item-heart", "a red heart health pickup")])},
}
# third pass: still too thin / turned into a pattern
FIX.update({
    "tex-royal-carpet": {"subject": "red velvet carpet with a simple repeating small gold fleur-de-lis pattern", "seed": 5101},
    "tex-nes-brick-32": {"subject": "large orange bricks in neat rows like a classic platformer brick block", "palette": "none", "seed": 5103},
    "prop-skull-pile": {"subject": "a single human skull resting on two crossed bones, centered, plain background", "seed": 5105},
    "prop-lever": {"subject": "a medieval wall switch, a wooden lever handle sticking out of a square iron plate, centered, plain background",
                   "outW": 64, "outH": 64, "seed": 5107},
    "mon-mushroom": {"subject": "one cute red-capped mushroom monster with eyes and little feet, standing, centered",
                     "styles": ["solo", "cute"], "seed": 5109},
    "item-iron-sword": {"subject": "a big iron sword icon with a brown grip and gold crossguard, diagonal, bold thick shapes", "seed": 5111},
    "item-gold-key": {"subject": "a big gold key icon with a round bow, bold thick shapes", "seed": 5113},
    "item-gold-coins": {"subject": "three shiny gold coins stacked, icon, centered, plain background", "seed": 5115},
    "item-heart": {"subject": "one big red heart icon, simple bold shape, white shine highlight, centered", "seed": 5117},
})
SPECS = [(n, note, base, dict(over, **FIX.get(n, {}))) for n, note, base, over in SPECS]


def params(form):
    p = msp.params_of(form)
    p.update(snap_method="unfake", tile=form["tile_axes"] if form.get("tile") else "")
    return p


def main(only):
    os.makedirs(DST, exist_ok=True)
    made = []
    for i, (name, note, base, over) in enumerate(SPECS):
        if only and name not in only:
            continue
        over = dict(over)
        seed = over.pop("seed", 3000 + i * 7)
        form = dict(base, **over)
        out = os.path.join(WORK, name)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        p = params(form)
        p["seed"] = seed
        argv = msp.server.build_argv(p) + ["--output-to", out]
        t = time.time()
        r = msp.subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1"))
        imgs = sorted(f for f in msp.glob.glob(os.path.join(out, "*.png")) if "_tiled_" not in f)
        if r.returncode or not imgs:
            print(f"FAILED {name}: {r.stdout[-300:]} {r.stderr[-300:]}", flush=True)
            continue
        new = "UF-" + name
        shutil.copy2(imgs[0], os.path.join(DST, new + ".png"))
        rec = {"name": new, "saved": time.time(), "note": f"{note} unfake grid snap.  (sample: seed {seed})",
               "snapshot": {"form": form, "steer": dict(msp.STEER_OFF), "evo": dict(msp.EVO_DEFAULT), "adv": {}},
               "image": new + ".png", "sample_command": msp.server.shlex.join(["pixelmon"] + argv[1:-2])}
        with open(os.path.join(DST, new + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        made.append(new)
        print(f"ok  {new:34} {time.time() - t:5.1f}s", flush=True)
    op = os.path.join(DST, ".order.json")
    prev = json.load(open(op)) if os.path.isfile(op) else []
    json.dump(prev + [n for n in made if n not in prev], open(op, "w"), indent=1)


if __name__ == "__main__":
    main(set(sys.argv[1:]))
