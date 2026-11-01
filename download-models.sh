#!/usr/bin/env bash
# Download the models pixelmon uses into ~/ComfyUI/models/ (or $COMFYUI_DIR/models).
# All public on Hugging Face / Civitai. Files you already have are skipped, so re-running is safe.
#
#   ./download-models.sh              core: SDXL + the LoRAs every style guide needs      (~7.9 GB)
#   ./download-models.sh --lab        + LAB: ControlNet (convert/restyle) + SDXL inpainting (~7.5 GB)
#   ./download-models.sh --steer      + steering / evolve: IPAdapter + CLIP-vision        (~3.2 GB)
#   ./download-models.sh --depth      + depth lock (--depth) + cut-outs (--cutout): depth ControlNet + BiRefNet  (~2.9 GB)
#   ./download-models.sh --3d         + 3D models / turnarounds (--mesh, --turnaround): Hunyuan3D 2  (~4.9 GB)
#                                      (they also need --steer, --depth, and Blender on the machine running pixelmon)
#   ./download-models.sh --juggernaut + Juggernaut XL v9, an optional alternative checkpoint (~6.6 GB)
#   ./download-models.sh --upscale    + JPEG repair / upscale models for --upscale (OpenModelDB)  (~380 MB)
#   ./download-models.sh --all        everything above                                     (~30 GB)
#   ./download-models.sh --list       show what's installed and what's missing, download nothing
#   ./download-models.sh --openmodeldb ID[,ID…]   any model on https://openmodeldb.info by its ID
#                                      (the last part of its page's address, e.g. 4x-Nomos2-otf-esrgan),
#                                      checked against the site's checksum, into models/upscale_models/
#   ./download-models.sh --openmodeldb jpeg      list OpenModelDB's models with that tag (jpeg, photo, anime…)
#
# An interrupted download resumes where it stopped the next time you run it.
# Civitai sometimes asks for a login on downloads; if one fails, create a free API key at
# https://civitai.com/user/account and run with  CIVITAI_TOKEN=<key> ./download-models.sh …
set -euo pipefail

COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"
M="$COMFY/models"
WANT_LAB=0 WANT_STEER=0 WANT_DEPTH=0 WANT_3D=0 WANT_JUGG=0 WANT_UPSCALE=0 LIST=0 OMDB=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
want_omdb=0
for a in "$@"; do
    if [ "$want_omdb" = 1 ]; then OMDB="$a"; want_omdb=0; continue; fi
    case "$a" in
        --lab) WANT_LAB=1 ;;
        --steer) WANT_STEER=1 ;;
        --depth) WANT_DEPTH=1 ;;
        --3d) WANT_3D=1 ;;
        --juggernaut) WANT_JUGG=1 ;;
        --upscale) WANT_UPSCALE=1 ;;
        --openmodeldb) want_omdb=1 ;;
        --openmodeldb=*) OMDB="${a#*=}" ;;
        --all) WANT_LAB=1; WANT_STEER=1; WANT_DEPTH=1; WANT_3D=1; WANT_JUGG=1; WANT_UPSCALE=1 ;;
        --list) LIST=1 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown option: $a (see --help)"; exit 1 ;;
    esac
done
[ "$want_omdb" = 1 ] && { echo "--openmodeldb needs an ID (or a tag to list, e.g. jpeg)"; exit 1; }
[ "${STEER_MODELS:-0}" = 1 ] && WANT_STEER=1          # the old switch still works

MISSING=()
get() {  # url  folder  file  size-label  what-for  [sha256]
    local url="$1" dir="$M/$2" file="$3" size="$4" why="$5" sum="${6:-}" dest
    dest="$dir/$file"
    if [ -s "$dest" ]; then echo "✓ $2/$file"; return; fi
    if [ "$LIST" = 1 ]; then echo "✗ $2/$file  ($size) — $why"; MISSING+=("$file"); return; fi
    mkdir -p "$dir"
    echo "↓ $2/$file  ($size) — $why"
    local auth=()
    if [[ "$url" == *civitai.com* && -n "${CIVITAI_TOKEN:-}" ]]; then auth=(-H "Authorization: Bearer $CIVITAI_TOKEN"); fi
    # -C - resumes a .part left by an interrupted run (a 6.9 GB checkpoint shouldn't restart from zero). A 416
    # means the .part doesn't fit what the server has now (a newer file, or a stale one): start that one over.
    # (curl exit 33 = the server can't resume at all: the same.)
    local code rc
    code="$(curl -L --fail --progress-bar -C - -w '%{http_code}' ${auth[@]+"${auth[@]}"} -o "$dest.part" "$url")" && rc=0 || rc=$?
    if [ "$code" = 416 ] || [ "$rc" = 33 ]; then
        rm -f "$dest.part"
        code="$(curl -L --fail --progress-bar -w '%{http_code}' ${auth[@]+"${auth[@]}"} -o "$dest.part" "$url")" && rc=0 || rc=$?
    fi
    if [ "$rc" = 0 ] && [ -n "$sum" ] && ! echo "$sum  $dest.part" | sha256sum -c --status 2>/dev/null; then
        echo "  ✗ checksum doesn't match OpenModelDB's ($sum): kept nothing"
        rm -f "$dest.part"
        MISSING+=("$file")
        return
    fi
    if [ "$rc" = 0 ]; then
        mv "$dest.part" "$dest"
    else
        echo "  ✗ download failed (HTTP ${code:-no answer}): $url"
        [ -s "$dest.part" ] && echo "    (kept $dest.part — re-run to resume it)"
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
if [ "$WANT_DEPTH" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== depth lock (camera angle + geometry from a reference)"
    get "https://huggingface.co/diffusers/controlnet-depth-sdxl-1.0/resolve/main/diffusion_pytorch_model.fp16.safetensors" \
        controlnet controlnet-depth-sdxl-1.0.safetensors "2.5 GB" "--depth / the Steering tab's depth lock"
    get "https://huggingface.co/ZhengPeng7/BiRefNet/resolve/main/model.safetensors" \
        background_removal birefnet.safetensors "444 MB" "--cutout and --depth-mask-bg (find the subject)"
fi
if [ "$WANT_3D" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== 3D (a model of a picture, rotation sprites)"
    get "https://huggingface.co/Comfy-Org/hunyuan3D_2.0_repackaged/resolve/main/split_files/hunyuan3d-dit-v2_fp16.safetensors" \
        checkpoints hunyuan3d-dit-v2_fp16.safetensors "4.9 GB" "--mesh / --turnaround (Hunyuan3D 2, picture -> 3D model)"
fi
if [ "$WANT_JUGG" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== optional checkpoint"
    get "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors" \
        checkpoints juggernautXL_v9.safetensors "6.6 GB" "an alternative SDXL checkpoint (⚙ Advanced › Checkpoint)"
fi
if [ "$WANT_UPSCALE" = 1 ] || [ "$LIST" = 1 ]; then
    echo "== JPEG repair / upscaling (--upscale, the GUI's 🔍 upscale; https://openmodeldb.info/?t=jpeg)"
    get "https://github.com/Phhofm/models/releases/download/1xDeJPG_realplksr_otf/1xDeJPG_realplksr_otf.safetensors" \
        upscale_models 1x-DeJPG-realplksr-otf.safetensors "29 MB" "JPEG repair, same size: the default --repair (CC-BY-4.0)"
    get "https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax6ygfvpvzka/b/open-modeldb-files/o/1x-NMKD-Jaywreck3-Lite.pth" \
        upscale_models 1x-NMKD-Jaywreck3-Lite.pth "20 MB" "JPEG repair, crisper, shifts colours a little (WTFPL)"
    get "https://github.com/Kim2091/Kim2091-Models/releases/download/2x-AnimeSharpV4/2x-AnimeSharpV4_Fast_RCAN_PU.safetensors" \
        upscale_models 2x-AnimeSharpV4-Fast-RCAN-PU.safetensors "31 MB" "JPEG of pixel art -> clean: used by --pixel-art (CC-BY-NC-SA: non-commercial)" \
        b641c9eb10b43f26538177aa8f0fef8b9fc2a153afd1431d0a062a84c49ce6d0
    get "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth" \
        upscale_models 4x-realesrgan-x4plus.pth "67 MB" "4x photos + art, copes with JPEG: the default --upscale (BSD)"
    get "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth" \
        upscale_models 2x-realesrgan-x2plus.pth "67 MB" "2x of the same (BSD)"
    get "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth" \
        upscale_models 4x-realesrgan-x4plus-anime-6b.pth "18 MB" "4x drawings, cartoons, flat colour (BSD)"
    get "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth" \
        upscale_models 4x-realesr-general-x4v3.pth "5 MB" "4x, small and fast (BSD)"
    get "https://github.com/Phhofm/models/releases/download/4xNomos2_otf_esrgan/4xNomos2_otf_esrgan.safetensors" \
        upscale_models 4x-Nomos2-otf-esrgan.safetensors "33 MB" "4x photos with JPEG, noise and blur (CC-BY-4.0)"
    get "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors" \
        upscale_models 4x-UltraSharpV2.safetensors "140 MB" "4x sharpest for AI art + photos, slower (CC-BY-NC-SA: non-commercial)"
fi
if [ -n "$OMDB" ]; then
    command -v python3 >/dev/null || { echo "--openmodeldb needs python3"; exit 1; }
    echo "== OpenModelDB"
    IFS=',' read -ra IDS <<< "$OMDB"
    for id in "${IDS[@]}"; do
        # prints url, file, size, sha256, what-for; a tag (jpeg, anime…) lists its models instead (exit 3)
        row="$(python3 -I "$HERE/tools/openmodeldb.py" pick "$id")" && rc=0 || rc=$?
        if [ "$rc" = 0 ]; then
            IFS=$'\t' read -r url file size sum why <<< "$row"
            get "$url" upscale_models "$file" "$size" "$why" "$sum"
        elif [ "$rc" = 3 ]; then
            printf '%s\n' "$row"
        else
            MISSING+=("$id")
        fi
    done
fi

echo
echo "ℹ  not downloaded here:"
echo "   • ega-art-v2 / dosart-vga — LoRAs trained on your own art collection (see README: 'Your own LoRAs')"
echo "   • CLIPSeg (LAB 'select by words', --animate) — fetched automatically (~600 MB) the first time it's used"
echo "   • Depth Anything V2 (--depth) — fetched by comfyui_controlnet_aux (~1.3 GB) the first time it's used"
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo; echo "✗ missing: ${MISSING[*]}"
    if [ "$LIST" = 1 ]; then echo "  fetch a group with --lab / --steer / --depth / --juggernaut / --upscale, or everything with --all"; else exit 1; fi
else
    echo; echo "✅ models ready in $M/"
fi
