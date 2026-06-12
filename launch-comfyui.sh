#!/usr/bin/env bash
# Launch ComfyUI for an AMD RX 6600 (Navi 23 / gfx1032) on ROCm.
#
# Why the special handling:
#   * gfx1032 isn't officially supported by ROCm, so we make it masquerade as
#     the supported gfx1030 via HSA_OVERRIDE_GFX_VERSION=10.3.0.
#   * ROCm compute needs /dev/kfd, which lives behind the "render" group; if the
#     current shell isn't a member yet we transparently re-exec under `sg render`.
#   * --lowvram streams SDXL from system RAM instead of fully loading the 8GB
#     card — much gentler on the GPU (added after a crash under full load).
set -euo pipefail

COMFY="${COMFYUI_DIR:-$HOME/ComfyUI}"

# Re-exec under the render group if this shell isn't a member yet (e.g. you were
# added with usermod but haven't logged out/in). No-op once it's permanent.
if ! id -nG | tr ' ' '\n' | grep -qx render; then
    echo "[launch] 'render' group not active in this shell; re-exec via sg render..."
    exec sg render -c "$(readlink -f "$0")"
fi

cd "$COMFY"
source .venv/bin/activate

export HSA_OVERRIDE_GFX_VERSION=10.3.0   # treat gfx1032 as the supported gfx1030
export HIP_VISIBLE_DEVICES=0             # discrete RX 6600 only, ignore the iGPU
export TORCH_BLAS_PREFER_HIPBLASLT=0     # silence the unsupported-hipBLASLt warning

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ComfyUI — AMD RX 6600 (gfx1032 → gfx1030 override)         ║"
echo "║  Open:  http://localhost:8188                              ║"
echo "╚════════════════════════════════════════════════════════════╝"

# --lowvram keeps the 8GB card out of the danger zone (stability). Remove it for
# maximum speed once you're confident the GPU is stable under full load.
exec python main.py --listen 0.0.0.0 --lowvram "$@"
