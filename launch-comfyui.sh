#!/usr/bin/env bash
# Launch ComfyUI, auto-detecting the GPU vendor: NVIDIA (CUDA) / AMD (ROCm) / CPU.
#
# AMD (ROCm) specifics — applied ONLY on the AMD branch:
#   * gfx1032 (RX 6600) isn't officially supported, so it masquerades as the
#     supported gfx1030 via HSA_OVERRIDE_GFX_VERSION=10.3.0.
#   * ROCm compute needs /dev/kfd, gated behind the "render" group; if the shell
#     isn't a member yet we transparently re-exec under `sg render`.
#   * --lowvram streams SDXL from system RAM — gentle on the 8GB card (stability).
# NVIDIA needs none of that (no override, no render group, no --lowvram by default).
# Force a vendor with  PIXELMON_GPU=nvidia|amd|cpu  if detection ever guesses wrong.
set -euo pipefail

COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"

detect_gpu() {
    case "${PIXELMON_GPU:-}" in nvidia|amd|cpu|mps) echo "$PIXELMON_GPU"; return;; esac
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
        echo mps               # Apple Silicon -> PyTorch Metal (MPS) backend
    elif command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        echo nvidia
    elif [ -x /usr/lib/wsl/lib/nvidia-smi ] && /usr/lib/wsl/lib/nvidia-smi -L >/dev/null 2>&1; then
        echo nvidia            # WSL2: driver libs live in /usr/lib/wsl/lib (not on PATH)
    elif [ -e /dev/kfd ] || command -v rocminfo >/dev/null 2>&1; then
        echo amd
    else
        echo cpu
    fi
}
GPU="$(detect_gpu)"

# AMD only: re-exec under the "render" group if this shell isn't a member yet
# (e.g. you were added with usermod but haven't logged out/in). No-op once permanent.
if [ "$GPU" = amd ] && ! id -nG | tr ' ' '\n' | grep -qx render; then
    echo "[launch] 'render' group not active in this shell; re-exec via sg render..."
    exec sg render -c "$(readlink -f "$0")"
fi

cd "$COMFY"
# Run via the venv interpreter directly — no `source activate` needed. Some venvs
# only ship bin/python (no activate scripts); this works regardless.
VENV_PY="$COMFY/.venv/bin/python"

EXTRA=()
case "$GPU" in
    amd)
        export HSA_OVERRIDE_GFX_VERSION=10.3.0   # treat gfx1032 as the supported gfx1030
        export HIP_VISIBLE_DEVICES=0             # discrete RX 6600 only, ignore the iGPU
        export TORCH_BLAS_PREFER_HIPBLASLT=0     # silence the unsupported-hipBLASLt warning
        EXTRA+=(--lowvram)                       # keep the 8GB card out of the danger zone
        LABEL="AMD ROCm  (gfx1032 -> gfx1030 override, --lowvram)"
        ;;
    nvidia)
        # CUDA needs no special env; ComfyUI auto-manages VRAM. 12GB+ cards run SDXL
        # fully loaded (no --lowvram); on an 8GB NVIDIA card add --lowvram if you OOM.
        if [ -d /usr/lib/wsl/lib ]; then   # WSL2: driver bins + libcuda live here, not on PATH
            export PATH="/usr/lib/wsl/lib:$PATH"
            export LD_LIBRARY_PATH="/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}"
        fi
        LABEL="NVIDIA CUDA  ($(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1))"
        ;;
    mps)
        # Apple Silicon (Metal). Let ops not yet implemented in MPS fall back to CPU.
        export PYTORCH_ENABLE_MPS_FALLBACK=1
        LABEL="Apple Silicon  (MPS / Metal)"
        ;;
    cpu)
        EXTRA+=(--cpu)
        LABEL="CPU  (no GPU detected — this will be slow)"
        ;;
esac

echo "╔════════════════════════════════════════════════════════════╗"
printf  "║  ComfyUI — %-48.48s║\n" "$LABEL"
echo "║  Open:  http://localhost:8188                              ║"
echo "╚════════════════════════════════════════════════════════════╝"

# Pass through any extra args you give (e.g. add --lowvram on a small NVIDIA card).
exec "$VENV_PY" main.py --listen 0.0.0.0 "${EXTRA[@]}" "$@"
