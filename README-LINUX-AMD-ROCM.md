# pixelmon on Linux + AMD (ROCm)

Run pixelmon on a Linux box with an AMD Radeon GPU via **ROCm**. `install.sh`
auto-detects AMD and installs the ROCm build of PyTorch. This is the original
target platform (built + battle-tested on an **RX 6600**, Debian 13).

> TL;DR: get into the `render` group → clone → `./install.sh` → log out/in →
> `./download-models.sh` → `pixelmon "a dragon"`.

---

## 0. Prerequisites

- A **ROCm-capable AMD GPU** (RDNA2/RDNA3; older works with overrides).
- **ROCm** installed (the kernel `amdgpu` driver + `/dev/kfd` device).
- **Python 3.10–3.12** + venv, and **git**:
  ```bash
  sudo apt install -y python3-venv python3-pip git
  ```

`★ Two non-obvious AMD requirements (install.sh handles them) ─`
1. **`render` group.** ROCm talks to the GPU through `/dev/kfd`, owned by the
   `render` group. Without membership, `torch.cuda.device_count()` is 0. `install.sh`
   runs `sudo usermod -aG render $USER` — then you must **log out and back in**.
2. **`HSA_OVERRIDE_GFX_VERSION`.** Unsupported chips (e.g. RX 6600 = gfx1032) must
   masquerade as a supported one (gfx1030) via `HSA_OVERRIDE_GFX_VERSION=10.3.0`.
   `launch-comfyui.sh` sets this automatically on the AMD branch. Other cards may
   need a different value (or none on officially-supported GPUs).
`─────────────────────────────────────────────────`

---

## 1. Install

```bash
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon
./install.sh            # auto-detects AMD -> ROCm wheel; adds you to 'render'
#  >>> LOG OUT AND BACK IN now (so 'render' membership takes effect) <<<
./download-models.sh    # ~7.6 GB of models
```

---

## 2. Verify ROCm sees the GPU

```bash
~/ComfyUI/.venv/bin/python -c "import torch; print('cuda(rocm):', torch.cuda.is_available(), torch.cuda.device_count())"
# expect:  cuda(rocm): True 1
pixelmon "a fierce dragon"
```
If it says `False / 0`: you're not in the `render` group yet (log out/in), or the
`HSA_OVERRIDE_GFX_VERSION` is wrong for your card.

---

## 3. Run

```bash
~/launch-comfyui.sh                 # banner: "AMD ROCm (... override, --lowvram)"
# GUI: http://localhost:8188
```
`launch-comfyui.sh` runs AMD with **`--lowvram`** by default — it streams SDXL from
system RAM, which keeps an 8 GB card stable. Remove it for max speed once you trust
the GPU under load.

`★ Stability ──────────────────────────────────────`
A full-load SDXL run can hang the `amdgpu` driver and green-screen the machine (a
known RDNA2 + ROCm risk under sustained load). Mitigations: keep `--lowvram`,
prefer `--fast`, avoid huge `-n` at full quality. Enable persistent logs to debug a
recurrence: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`,
then after a hang: `journalctl -k -b -1 | grep -i amdgpu`.
`─────────────────────────────────────────────────`

**Headless / survives logout:**
```bash
loginctl enable-linger "$USER"
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
```

---

## 4. Join a render farm

```json
// servers.json on your main box
{ "local": "http://127.0.0.1:8188", "amd": "http://192.168.1.50:8188" }
```
```bash
pixelmon --batch "bat,skeleton,spider" -n 30 --server rtx,amd,local
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `torch.cuda.is_available()` False / 0 devices | not in `render` group (log out/in), or wrong `HSA_OVERRIDE_GFX_VERSION` |
| `rocminfo`: *"not a member of render group"* | `sudo usermod -aG render $USER`, then log out/in |
| Machine hangs / green-screens during generation | keep `--lowvram` (default in `launch-comfyui.sh`); prefer `--fast`; smaller batches |
| Output looks like a blurry photo, not pixels | you're not using the Pixel Art XL LoRA (`--no-lora` is on, or base only) |
| First generation slow | normal — loads the 6.9 GB SDXL model; later runs reuse it |

See the main [README](README.md) for usage, palettes, styles, `--animate`, and the
render farm. This is also the most detailed platform — the main README's "Lessons
learned" section covers the ROCm story in depth.
