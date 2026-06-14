#!/usr/bin/env bash
# ============================================================================
#  pixelmon — style demo gallery
# ----------------------------------------------------------------------------
#  Renders the SAME set of generic subjects in EVERY style, so you can see at a
#  glance what each --style does (and compare them side by side). Nothing here
#  is game-specific — it's a showcase of the styles themselves.
#
#  Output:  ./styles/<style>/<subject>_*_sprite_*.png   (the true-size assets)
#  Then:    ./build-style-montages.sh  ->  ./styles/<style>.png  (contact sheets)
#
#  Fan it across your GPUs with the render farm (see README-RENDER-FARM.md):
#    PIXELMON_FARM=rtx,titan,local,mac ./generate-style-demos.sh
#  Or just run it locally (default):
#    ./generate-style-demos.sh
#
#  Knobs (all overridable via env):
#    PIXELMON_FARM   server pool, comma list   (default: local)
#    SEED            fixed seed => same subject is comparable across styles (7)
#    SIZE            sprite size                (128)
#    NUM             images per subject/style   (1)
#    OUT             output dir                 (./styles)
# ============================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PIXELMON="${PIXELMON:-pixelmon}"
command -v "$PIXELMON" >/dev/null 2>&1 || PIXELMON="$HOME/pixelmon/bin/pixelmon"

FARM="${PIXELMON_FARM:-local}"     # e.g. PIXELMON_FARM=rtx,titan,local,mac
SEED="${SEED:-7}"                  # fixed => composition is comparable across styles
SIZE="${SIZE:-128}"
NUM="${NUM:-1}"                    # images per subject per style
OUT="${OUT:-$(pwd)/styles}"

# The same subjects in every style => a clean comparison grid (12 = a 4x3 sheet).
SUBJECTS="castle,knight,dragon,spaceship,starbase,alien,cowboy,bandit,banker,thug,treasure,halloween"

# Styles to demo, as  name[:lora[:palette]]
#   lora    omitted -> default pixel-art-xl.safetensors (must exist on every farm box)
#   palette omitted -> none (model's own colors)
# Hardware-palette looks (Game Boy/EGA/NES) CANNOT be done by prompt alone — SDXL
# only nudges hue, it can't count colors. So those styles lock to a real palette in
# post (--palette); the prompt just sets the vibe. Prompt-driven styles stay 'none'.
STYLES=(
  clean detailed minimal
  8bit::NES 16bit gameboy::GAMEBOY
  geometric outline cute dark horror
  hyperlight deadcells blasphemous owlboy stardew
  dosrpg darkest undertale mario zelda
  hollowknight metroid finalfantasy pokemon
  ega::EGA wasteland::EGA
  dosega:dosegafx.safetensors:EGA
  r3tr0:retro-game-art.safetensors
  pixelartredmond:pixelartredmond.safetensors
)

echo "gallery -> $OUT"
echo "farm    -> $FARM    seed=$SEED size=$SIZE n=$NUM    styles=${#STYLES[@]}    subjects=$(tr ',' ' ' <<<"$SUBJECTS" | wc -w)"
echo

failed=()
for entry in "${STYLES[@]}"; do
  IFS=: read -r style lora pal <<<"$entry"
  [ -z "${lora:-}" ] && lora="pixel-art-xl.safetensors"
  [ -z "${pal:-}" ]  && pal="none"
  echo "=== $style  (lora: $lora, palette: $pal) ==="
  if "$PIXELMON" \
        --batch "$SUBJECTS" \
        --style "$style" \
        --lora "$lora" \
        --palette "$pal" \
        --size "$SIZE" \
        --seed "$SEED" \
        -n "$NUM" \
        --server "$FARM" \
        --output-to "$OUT/$style" \
        --no-subdirs --create-dirs --no-open; then
    :
  else
    echo "  !! $style failed — continuing"
    failed+=("$style")
  fi
done

echo
echo "✅ gallery done -> $OUT/<style>/"
[ ${#failed[@]} -gt 0 ] && echo "⚠️  failed styles: ${failed[*]}"
echo "   build contact sheets:  ./build-style-montages.sh"
