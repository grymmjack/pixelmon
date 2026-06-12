#!/usr/bin/env bash
# Reproducible setup for pixelmon on an AMD ROCm box (built for RX 6600 / Debian 13).
# Idempotent — safe to re-run. Does NOT download models (run ./download-models.sh).
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"
PY=python3.10

echo "==> repo:    $REPO"
echo "==> ComfyUI: $COMFY"

command -v "$PY" >/dev/null || {
    echo "Need $PY  (e.g. sudo apt install python3.10 python3.10-venv)"; exit 1; }

# 1. ComfyUI engine
if [ ! -d "$COMFY/.git" ]; then
    echo "==> cloning ComfyUI"
    git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
fi

# 2. venv + ROCm PyTorch + ComfyUI deps
if [ ! -x "$COMFY/.venv/bin/python" ]; then
    echo "==> creating venv ($PY)"
    "$PY" -m venv "$COMFY/.venv"
fi
PIP="$COMFY/.venv/bin/pip"
"$PIP" install --upgrade pip wheel
echo "==> installing torch (ROCm 6.2) — the big one"
"$PIP" install torch==2.5.1+rocm6.2 torchvision==0.20.1+rocm6.2 torchaudio==2.5.1+rocm6.2 \
    --index-url https://download.pytorch.org/whl/rocm6.2
"$PIP" install -r "$COMFY/requirements.txt"

# 3. link our files into place (this repo stays the source of truth)
link() { ln -sfn "$1" "$2"; echo "   linked $2 -> $1"; }
mkdir -p "$HOME/.local/bin" "$COMFY/custom_nodes"
chmod +x "$REPO/bin/pixelmon" "$REPO/launch-comfyui.sh"
link "$REPO/pixelmon.py"                   "$COMFY/pixelmon.py"
link "$REPO/custom_nodes/pixelart_palette" "$COMFY/custom_nodes/pixelart_palette"
link "$REPO/bin/pixelmon"                  "$HOME/.local/bin/pixelmon"
link "$REPO/launch-comfyui.sh"             "$HOME/launch-comfyui.sh"

# 4. render group — ROCm compute needs /dev/kfd, gated behind this group
if ! id -nG | tr ' ' '\n' | grep -qx render; then
    echo "==> adding $USER to the 'render' group (ROCm GPU access)"
    sudo usermod -aG render "$USER"
    echo "   ⚠  LOG OUT AND BACK IN for this to take effect."
fi

cat <<EOF

✅ install done.
   1) ./download-models.sh        # ~7.5 GB of models from Hugging Face
   2) (log out/in once if you were just added to 'render')
   3) pixelmon "a fierce dragon"

   Note: make sure ~/.local/bin is on your PATH.
EOF
