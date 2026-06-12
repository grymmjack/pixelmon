#!/usr/bin/env bash
# Download the models pixelmon needs into ~/ComfyUI/models/.
# All public on Hugging Face — no login/token required. ~7.5 GB total.
set -euo pipefail

COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"
CKPT="$COMFY/models/checkpoints"
LORA="$COMFY/models/loras"
mkdir -p "$CKPT" "$LORA"

get() {  # url  dest
    local url="$1" dest="$2"
    if [ -f "$dest" ]; then echo "✓ already have $(basename "$dest")"; return; fi
    echo "↓ downloading $(basename "$dest") ..."
    curl -L --fail -o "$dest" "$url"
}

# SDXL base checkpoint (6.9 GB)
get "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
    "$CKPT/sd_xl_base_1.0.safetensors"

# Pixel Art XL LoRA (171 MB) — the thing that makes output real pixel art
get "https://huggingface.co/nerijs/pixel-art-xl/resolve/main/pixel-art-xl.safetensors" \
    "$LORA/pixel-art-xl.safetensors"

# LCM LoRA for --fast mode (394 MB)
get "https://huggingface.co/latent-consistency/lcm-lora-sdxl/resolve/main/pytorch_lora_weights.safetensors" \
    "$LORA/lcm-lora-sdxl.safetensors"

echo "✅ models ready in $COMFY/models/"
