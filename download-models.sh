#!/usr/bin/env bash
# Download the models pixelmon uses into ~/ComfyUI/models/ (or $COMFYUI_DIR/models).
# All public on Hugging Face / Civitai. Files you already have are skipped, so re-running is safe.
#
#   ./download-models.sh              core: SDXL + the LoRAs every style guide needs      (~7.9 GB)
#   ./download-models.sh --lab        + LAB: ControlNet (convert/restyle) + SDXL inpainting (~7.5 GB)
#   ./download-models.sh --steer      + steering / evolve: IPAdapter + CLIP-vision        (~3.2 GB)
#   ./download-models.sh --juggernaut + Juggernaut XL v9, an optional alternative checkpoint (~6.6 GB)
#   ./download-models.sh --all        everything above                                     (~25 GB)
#   ./download-models.sh --list       show what's installed and what's missing, download nothing
#
# Civitai sometimes asks for a login on downloads; if one fails, create a free API key at
# https://civitai.com/user/account and run with  CIVITAI_TOKEN=<key> ./download-models.sh …
set -euo pipefail

COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"
M="$COMFY/models"
WANT_LAB=0 WANT_STEER=0 WANT_JUGG=0 LIST=0
for a in "$@"; do
    case "$a" in
        --lab) WANT_LAB=1 ;;
        --steer) WANT_STEER=1 ;;
        --juggernaut) WANT_JUGG=1 ;;
        --all) WANT_LAB=1; WANT_STEER=1; WANT_JUGG=1 ;;
        --list) LIST=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $a (see --help)"; exit 1 ;;
    esac
done
[ "${STEER_MODELS:-0}" = 1 ] && WANT_STEER=1          # the old switch still works

MISSING=()
get() {  # url  folder  file  size-label  what-for
    local url="$1" dir="$M/$2" file="$3" size="$4" why="$5" dest
    dest="$dir/$file"
    if [ -s "$dest" ]; then echo "✓ $2/$file"; return; fi
    if [ "$LIST" = 1 ]; then echo "✗ $2/$file  ($size) — $why"; MISSING+=("$file"); return; fi
    mkdir -p "$dir"
    echo "↓ $2/$file  ($size) — $why"
    local auth=()
    if [[ "$url" == *civitai.com* && -n "${CIVITAI_TOKEN:-}" ]]; then auth=(-H "Authorization: Bearer $CIVITAI_TOKEN"); fi
    if curl -L --fail --progress-bar ${auth[@]+"${auth[@]}"} -o "$dest.part" "$url"; then
        mv "$dest.part" "$dest"
    else
        rm -f "$dest.part"
        echo "  ✗ download failed: $url"
        [[ "$url" == *civitai.com* ]] && echo "    (Civitai may need a free API key: CIVITAI_TOKEN=<key> $0)"
        MISSING+=("$file")
    fi
}

echo "== core (needed for everything)"
get "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
    checkpoints sd_xl_base_1.0.safetensors "6.9 GB" "the base model"
get "https://huggingface.co/nerijs/pixel-art-xl/resolve/main/pixel-art-xl.safetensors" \
    loras pixel-art-xl.safetensors "171 MB" "Pixel Art XL: real pixel art (the default LoRA)"
get "https://huggingface.co/latent-consistency/lcm-lora-sdxl/resolve/main/pytorch_lora_weights.safetensors" \
    loras lcm-lora-sdxl.safetensors "394 MB" "fast mode (--fast, LAB live preview)"
get "https://civitai.com/api/download/models/356290" \
    loras dosegafx.safetensors "82 MB" "EGA retro style (the 'dosega' style)"
get "https://civitai.com/api/download/models/615427?fileId=530365" \
    loras retro-game-art.safetensors "218 MB" "Retro Game Art (the 'r3tr0' style)"
get "https://huggingface.co/artificialguybr/PixelArtRedmond/resolve/main/PixelArtRedmond-Lite64.safetensors" \
    loras pixelartredmond.safetensors "163 MB" "PixelArtRedmond (the 'pixelartredmond' style)"

if [ "$WANT_LAB" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== LAB (convert / restyle images, edit masked areas)"
    get "https://huggingface.co/xinsir/controlnet-union-sdxl-1.0/resolve/main/diffusion_pytorch_model_promax.safetensors" \
        controlnet controlnet-union-sdxl-promax.safetensors "2.4 GB" "LAB convert (--control) and 'keep shape & direction'"
    get "https://huggingface.co/diffusers/stable-diffusion-xl-1.0-inpainting-0.1/resolve/main/unet/diffusion_pytorch_model.fp16.safetensors" \
        unet sdxl-inpainting-0.1.fp16.safetensors "5.1 GB" "LAB edits that paint something new (--inpaint-model)"
fi
if [ "$WANT_STEER" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== steering / evolve (reference images)"
    get "https://huggingface.co/h94/IP-Adapter/resolve/main/sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors" \
        ipadapter ip-adapter-plus_sdxl_vit-h.safetensors "848 MB" "--steer / the Steering tab / evolve"
    get "https://huggingface.co/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors" \
        clip_vision CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors "2.5 GB" "the image encoder IPAdapter uses"
fi
if [ "$WANT_JUGG" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== optional checkpoint"
    get "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors" \
        checkpoints juggernautXL_v9.safetensors "6.6 GB" "an alternative SDXL checkpoint (⚙ Advanced › Checkpoint)"
fi

echo
echo "ℹ  not downloaded here:"
echo "   • ega-art-v2 / dosart-vga — LoRAs trained on your own art collection (see README: 'Your own LoRAs')"
echo "   • CLIPSeg (LAB 'select by words', --animate) — fetched automatically (~600 MB) the first time it's used"
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo; echo "✗ missing: ${MISSING[*]}"
    if [ "$LIST" = 1 ]; then echo "  fetch a group with --lab / --steer / --juggernaut, or everything with --all"; else exit 1; fi
else
    echo; echo "✅ models ready in $M/"
fi
