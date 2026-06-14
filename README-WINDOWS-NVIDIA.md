# pixelmon on Windows + NVIDIA (via WSL2)

Run pixelmon on a Windows PC with an NVIDIA GPU, using **WSL2** (Windows Subsystem
for Linux). The whole Linux setup is identical to a native Linux box —
`install.sh` auto-detects NVIDIA and installs the CUDA build of PyTorch. The only
Windows-specific things are **GPU passthrough** and **networking** (so other
machines can reach it as a `--server` render target).

> TL;DR: install the NVIDIA *Windows* driver → `wsl` in → `git clone` →
> `./install.sh` → `./download-models.sh` → done. For LAN access, enable WSL
> mirrored networking (or a portproxy).

---

## 0. Prerequisites (on Windows)

- **Windows 11**, or Windows 10 21H2+.
- **WSL2** with a distro (Ubuntu is fine). If you don't have it yet, in an
  **admin PowerShell**: `wsl --install` then reboot.
- The **NVIDIA driver installed on Windows** (GeForce/Studio driver, or the
  NVIDIA app). **Do NOT install a Linux GPU driver inside WSL** — WSL borrows the
  Windows driver through `/dev/dxg`. Installing a Linux driver inside WSL breaks it.

Open a WSL terminal (type `wsl` in PowerShell, or launch your distro from Start).

---

## 1. Verify the GPU is visible in WSL

```bash
nvidia-smi
```

- ✅ Shows your card (e.g. "NVIDIA GeForce RTX 3070") → good, continue.
- ❌ "command not found" or no GPU → update the **Windows** NVIDIA driver, reboot,
  and `wsl --shutdown` (in PowerShell) before reopening WSL.

---

## 2. Install pixelmon (inside WSL)

```bash
sudo apt update && sudo apt install -y git python3-venv python3-pip
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon
./install.sh            # auto-detects NVIDIA -> CUDA wheel; no render group/sudo needed
./download-models.sh    # ~7.6 GB of models (SDXL + LoRAs)
```

`install.sh` reuses any existing `~/ComfyUI`, creates the venv, and links the CLI.

---

## 3. Confirm CUDA works

```bash
~/ComfyUI/.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# expect:  True NVIDIA GeForce RTX 3070
pixelmon "a fierce dragon"          # first generation (loads the model ~1 min)
```

If `torch.cuda.is_available()` is `False`: the Windows driver is too old or WSL
didn't get GPU passthrough — update the driver and `wsl --shutdown`.

---

## 4. Run it / watch progress

```bash
pixelmon "a goblin warrior" --size 128 -n 4      # generate locally in WSL
# or start the server and use the browser GUI:
~/launch-comfyui.sh                              # banner: "NVIDIA CUDA (...)"
```
Then open **http://localhost:8188** in your Windows browser (WSL forwards
`localhost`). ComfyUI shows live generation progress.

**Keep it running** (so it survives closing the terminal):
```bash
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
# attach: tmux attach -t comfy     (detach: Ctrl-b then d)
```
Note: WSL shuts the distro down when idle or on `wsl --shutdown`. For an always-on
server, keep a WSL window open, or create a Windows Task Scheduler entry that runs
`wsl -d <distro> -- ~/launch-comfyui.sh` at logon.

---

## 5. Make it reachable from your other machines (`--server`)

This is the one real WSL gotcha: WSL2 is behind a NAT, so `0.0.0.0:8188` inside WSL
is **not** reachable from other LAN machines by default. Pick one:

### Option A — Mirrored networking (Windows 11, recommended)
Create/edit `C:\Users\<you>\.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
```
Then in **PowerShell**: `wsl --shutdown`, reopen WSL. WSL now shares the PC's
network — ComfyUI is reachable at the **PC's own LAN IP**, port 8188.

### Option B — Port-proxy (Windows 10 / NAT mode)
In **PowerShell (Admin)**:
```powershell
netsh interface portproxy add v4tov4 listenport=8188 listenaddress=0.0.0.0 connectport=8188 connectaddress=$((wsl hostname -I).Trim())
netsh advfirewall firewall add rule name="ComfyUI 8188" dir=in action=allow protocol=TCP localport=8188
```
WSL's internal IP changes on reboot, so re-run the first line after a restart
(mirrored mode avoids this).

### Windows Firewall
Make sure inbound **TCP 8188** is allowed (the `netsh advfirewall` line above does
this; for mirrored mode, add an equivalent rule for port 8188).

### Then, from any other box (e.g. your main Linux machine)
Add an alias to `servers.json` (copy `servers.example.json`):
```json
{ "local": "http://127.0.0.1:8188", "rtx": "http://<pc-lan-ip>:8188" }
```
```bash
pixelmon "a goblin" --server rtx          # renders on the PC's GPU, result lands locally
```

---

## 6. (Optional) SSH into WSL — for headless control / driving it remotely

```bash
sudo apt install -y openssh-server
sudo sed -i 's/^#\?Port .*/Port 2222/' /etc/ssh/sshd_config   # avoid clashing with Windows'
sudo service ssh restart
```
Then forward the port from Windows (PowerShell Admin), same pattern as §5 Option B
but for 2222, and `ssh <wsl-user>@<pc-lan-ip> -p 2222`. (Mirrored networking makes
this work without the portproxy.)

---

## 7. Join the render farm

Once it's reachable, this box is just another pool member. From your main box:
```bash
pixelmon --batch "bat,skeleton,spider" -n 30 --server rtx,titan,local
#   -> fans the work across all three GPUs; results all land locally
```
pixelmon uses **dynamic dispatch** (each GPU pulls its next job when free), so a
fast card automatically does more — no manual balancing.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `nvidia-smi` not found in WSL | install/update the **Windows** NVIDIA driver; `wsl --shutdown`; do **not** install a Linux driver in WSL |
| `torch.cuda.is_available()` is False | driver too old, or no WSL GPU passthrough — update driver + `wsl --shutdown` |
| Out of memory on an 8 GB card (3070) | run `~/launch-comfyui.sh --lowvram`, or smaller `--size` / fewer `-n` at once |
| Other machines can't reach `:8188` | WSL NAT — enable mirrored networking (§5A) or add the portproxy + firewall rule (§5B) |
| `Illegal instruction` at ComfyUI start | a prebuilt wheel needs newer CPU instructions — rare on modern CPUs; if it happens, see the main README troubleshooting (drop `kornia`) |
| Server dies when WSL closes | keep a WSL window open / run under `tmux`; for always-on use a Task Scheduler entry |

---

## Notes

- A modern NVIDIA GPU (Ampere/Ada, e.g. RTX 3070/4070) is typically the **fastest**
  option for SDXL — strong fp16/tensor cores. 8 GB VRAM is enough for SDXL via
  ComfyUI's automatic VRAM management (add `--lowvram` if you hit OOM).
- Everything else (palettes, styles, `--animate`, the EGA/Wasteland LoRA) works
  identically — see the main [README](README.md).

---

## Apple Silicon (macOS) — note

A Mac with Apple Silicon (M1/M2/M3) can also join the farm via PyTorch's **MPS**
(Metal) backend, and unified memory is generous for SDXL. Support needs a small
`mps` branch in `install.sh`/`launch-comfyui.sh` (no CUDA/ROCm) — see the project
TODO / ask, as it's a separate setup path from this Windows guide.
