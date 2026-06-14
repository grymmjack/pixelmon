# pixelmon on Linux + NVIDIA (CUDA)

Run pixelmon on a native Linux box with an NVIDIA GPU. `install.sh` auto-detects
NVIDIA and installs the CUDA build of PyTorch. This is the simplest, fastest path —
modern NVIDIA cards (Ampere/Ada) are typically the quickest for SDXL.

> For **Windows + NVIDIA via WSL2**, see [README-WINDOWS-NVIDIA.md](README-WINDOWS-NVIDIA.md)
> instead (same idea, plus WSL networking).

> TL;DR: install the NVIDIA driver → clone → `./install.sh` → `./download-models.sh`
> → `pixelmon "a dragon"`.

---

## 0. Prerequisites

- The **NVIDIA proprietary driver** installed and working: `nvidia-smi` should list
  your GPU. (Recent driver for CUDA 12.4 wheels: ≥ 525.)
- **Python 3.10–3.12** + venv, and **git**:
  ```bash
  sudo apt install -y python3-venv python3-pip git      # Debian/Ubuntu
  ```

---

## 1. Install

```bash
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon
./install.sh            # auto-detects NVIDIA -> CUDA wheel (cu124); no render group/sudo
./download-models.sh    # ~7.6 GB of models
```

---

## 2. Verify CUDA (a real GPU op, not just `is_available`)

```bash
~/ComfyUI/.venv/bin/python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0), "sm_%d%d" % torch.cuda.get_device_capability(0))
    x = torch.randn(2048, 2048, device="cuda"); print("GPU matmul ->", float((x@x).sum()))
PY
pixelmon "a fierce dragon"
```

`★ Pascal / old-GPU note ──────────────────────────`
Newer PyTorch wheels may drop very old architectures. If the GPU matmul errors with
"no kernel image is available," your card's compute capability isn't in the wheel —
pin an older CUDA wheel: change `cu124` → `cu121` on the torch line in `install.sh`
and re-run. (A Pascal `sm_61` Titan Xp works on `cu124` because its `sm_60` cubin
covers it via minor-version compatibility — `is_available()` alone won't reveal a
mismatch, so always do the real matmul above.)
`─────────────────────────────────────────────────`

---

## 3. Run / keep it running

```bash
~/launch-comfyui.sh                 # banner: "NVIDIA CUDA (<your card>)"
# GUI: http://localhost:8188
```
On 12 GB+ cards SDXL runs fully loaded (no `--lowvram`). On an 8 GB NVIDIA card,
add `--lowvram` if you hit OOM: `~/launch-comfyui.sh --lowvram`.

**Headless / survives logout** — enable lingering once, then run under tmux:
```bash
loginctl enable-linger "$USER"
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
```
Without lingering, systemd kills the server when your SSH session ends.

---

## 4. Join a render farm

```json
// servers.json on your main box
{ "local": "http://127.0.0.1:8188", "rtx": "http://192.168.1.50:8188" }
```
```bash
pixelmon --batch "bat,skeleton,spider" -n 30 --server rtx,titan,local
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `torch.cuda.is_available()` False | driver not installed/loaded (`nvidia-smi` must work), or you got a CPU wheel — re-run `install.sh` |
| GPU matmul: "no kernel image available" | wheel lacks your GPU's arch — pin `cu124` → `cu121` in `install.sh` |
| `Illegal instruction (core dumped)` at ComfyUI start (old CPU) | a prebuilt wheel needs CPU instructions (e.g. AVX2) your CPU lacks. Remove the offender if optional: `pip uninstall -y kornia kornia_rs` (only used by ComfyUI post-processing nodes pixelmon doesn't need) |
| Server dies on logout | `loginctl enable-linger "$USER"`, run under tmux |
| First generation slow | normal — loads the 6.9 GB SDXL model; later runs reuse it |

See the main [README](README.md) for usage, palettes, styles, `--animate`, and the
render farm.
