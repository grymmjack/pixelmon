#!/usr/bin/env bash
# ============================================================================
#  pixelmon — build a contact sheet per style
# ----------------------------------------------------------------------------
#  Turns ./styles/<style>/ (made by generate-style-demos.sh) into one labelled
#  grid image per style: ./styles/<style>.png  — the at-a-glance demo.
#
#  Pixels are upscaled with nearest-neighbor (-filter point) so the art stays
#  crisp instead of getting blurred by the montage resize.
#
#  Needs ImageMagick (`magick montage`). Knobs via env:
#    OUT   gallery dir (./styles)   CELL  cell px (192)   TILE  grid (4x3)
# ============================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

OUT="${OUT:-$(pwd)/styles}"
CELL="${CELL:-192}"
TILE="${TILE:-4x3}"
MONTAGE=( ${MONTAGE:-magick montage} )

# linuxbrew/macOS ImageMagick often has no default font -> pick one that exists.
FONT="${FONT:-}"
if [ -z "$FONT" ]; then
  for f in /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
           /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf \
           /System/Library/Fonts/Supplemental/Arial.ttf /Library/Fonts/Arial.ttf; do
    [ -f "$f" ] && { FONT="$f"; break; }
  done
fi
[ -n "$FONT" ] && FONTARG=( -font "$FONT" ) || FONTARG=()

# Keep this list in sync with generate-style-demos.sh (defines order + labels).
SUBJECTS=(castle knight dragon spaceship starbase alien cowboy bandit banker thug treasure halloween)

shopt -s nullglob
made=0
for dir in "$OUT"/*/; do
  style="$(basename "$dir")"
  args=()
  for subj in "${SUBJECTS[@]}"; do
    slug="${subj//[^a-z0-9]/_}"           # mirror pixelmon's slug() for lookup
    f=( "$dir$slug"_*_sprite_*.png )
    [ ${#f[@]} -gt 0 ] && args+=( -label "$subj" "${f[0]}" )
  done
  if [ ${#args[@]} -eq 0 ]; then echo "skip $style (no images)"; continue; fi
  "${MONTAGE[@]}" "${FONTARG[@]}" "${args[@]}" \
    -filter point -geometry "${CELL}x${CELL}+8+8" -tile "$TILE" \
    -background '#141414' -fill '#dddddd' -pointsize 15 \
    -title "$style" "$OUT/$style.png"
  echo "✓ $OUT/$style.png"
  made=$((made+1))
done
echo "--- built $made contact sheet(s) -> $OUT/<style>.png ---"
