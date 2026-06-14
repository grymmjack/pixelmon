#!/usr/bin/env bash
# Reproducible setup for pixelmon. Auto-detects the GPU vendor and installs the
# matching PyTorch: NVIDIA (CUDA) / AMD (ROCm) / CPU. Idempotent — safe to re-run.
# Does NOT download models (run ./download-models.sh after).
#
# Force a vendor with  PIXELMON_GPU=nvidia|amd|cpu ./install.sh  if needed.
# Pick a specific interpreter with  PYTHON=python3.11 ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"

# Python: prefer 3.10, but accept any 3.10–3.12 (Debian 12 ships 3.11, 13 ships 3.13).
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in python3.10 python3.11 python3.12 python3; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
command -v "$PY" >/dev/null 2>&1 || {
    echo "Need Python 3.10–3.12 (e.g. sudo apt install python3.11 python3.11-venv)"; exit 1; }

detect_gpu() {
    case "${PIXELMON_GPU:-}" in nvidia|amd|cpu) echo "$PIXELMON_GPU"; return;; esac
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        echo nvidia
    elif [ -e /dev/kfd ] || command -v rocminfo >/dev/null 2>&1; then
        echo amd
    else
        echo cpu
    fi
}
GPU="$(detect_gpu)"

echo "==> repo:    $REPO"
echo "==> ComfyUI: $COMFY"
echo "==> python:  $PY ($("$PY" --version 2>&1))"
echo "==> GPU:     $GPU"

# 1. ComfyUI engine
if [ ! -d "$COMFY/.git" ]; then
    echo "==> cloning ComfyUI"
    git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
fi

# 2. venv + the right PyTorch + ComfyUI deps
if [ ! -x "$COMFY/.venv/bin/python" ]; then
    echo "==> creating venv ($PY)"
    "$PY" -m venv "$COMFY/.venv"
fi
VENV_PY="$COMFY/.venv/bin/python"
# Always invoke pip as `python -m pip` — robust even when the venv's bin/pip
# wrapper was never created (e.g. a venv made with --without-pip). Bootstrap the
# pip *module* first if it's missing, so we can reuse a pre-existing venv.
"$VENV_PY" -m pip --version >/dev/null 2>&1 || {
    echo "==> bootstrapping pip into the venv (ensurepip)"
    "$VENV_PY" -m ensurepip --upgrade
}
pip() { "$VENV_PY" -m pip "$@"; }
pip install --upgrade pip wheel
echo "==> installing torch for '$GPU' — the big one"
case "$GPU" in
    nvidia)
        # CUDA build. Pascal (Titan Xp, sm_61) is still supported by current wheels;
        # if a future wheel drops it, pin an older cuXX index here.
        pip install torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/cu124 ;;
    amd)
        pip install torch==2.5.1+rocm6.2 torchvision==0.20.1+rocm6.2 torchaudio==2.5.1+rocm6.2 \
            --index-url https://download.pytorch.org/whl/rocm6.2 ;;
    cpu)
        echo "   ⚠  no GPU detected — installing CPU torch (generation will be very slow)"
        pip install torch torchvision torchaudio ;;
esac
pip install -r "$COMFY/requirements.txt"

# 3. link our files into place (this repo stays the source of truth)
link() { ln -sfn "$1" "$2"; echo "   linked $2 -> $1"; }
mkdir -p "$HOME/.local/bin" "$COMFY/custom_nodes"
chmod +x "$REPO/bin/pixelmon" "$REPO/launch-comfyui.sh"
link "$REPO/pixelmon.py"                   "$COMFY/pixelmon.py"
link "$REPO/custom_nodes/pixelart_palette" "$COMFY/custom_nodes/pixelart_palette"
link "$REPO/bin/pixelmon"                  "$HOME/.local/bin/pixelmon"
link "$REPO/launch-comfyui.sh"             "$HOME/launch-comfyui.sh"
# (animate.py lives next to pixelmon.py in this repo and is imported by path —
#  no symlink needed; the realpath of the linked pixelmon.py points back here.)

# 4. render group — AMD/ROCm only (compute needs /dev/kfd, gated behind this group)
if [ "$GPU" = amd ]; then
    if ! id -nG | tr ' ' '\n' | grep -qx render; then
        echo "==> adding $USER to the 'render' group (ROCm GPU access)"
        sudo usermod -aG render "$USER"
        echo "   ⚠  LOG OUT AND BACK IN for this to take effect."
    fi
fi

# 5. (optional) pixel-snapper for --snap-pixels — needs the Rust toolchain
SNAP="$REPO/tools/pixel-snapper"
if command -v cargo >/dev/null; then
    [ -d "$SNAP/.git" ] || git clone --depth 1 \
        https://github.com/Hugo-Dz/spritefusion-pixel-snapper.git "$SNAP"
    echo "==> building pixel-snapper (for --snap-pixels)…"
    (cd "$SNAP" && cargo build --release) && echo "   pixel-snapper ready"
else
    echo "==> no cargo found — skipping pixel-snapper; --snap-pixels will be unavailable"
    echo "    (install Rust via https://rustup.rs then re-run, if you want it)"
fi

LOGIN_NOTE=""
[ "$GPU" = amd ] && LOGIN_NOTE="
   2) log out/in once if you were just added to 'render'"
cat <<EOF

✅ install done ($GPU).
   1) ./download-models.sh        # ~7.6 GB of models (Hugging Face + Civitai)$LOGIN_NOTE
   3) pixelmon "a fierce dragon"

   Note: make sure ~/.local/bin is on your PATH.
EOF
