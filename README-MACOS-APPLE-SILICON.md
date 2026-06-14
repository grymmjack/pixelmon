# pixelmon on macOS (Apple Silicon / MPS)

Run pixelmon on an Apple Silicon Mac (M1/M2/M3) using PyTorch's **MPS** (Metal)
backend. `install.sh` auto-detects Apple Silicon and installs the Metal build of
PyTorch — no CUDA/ROCm involved. Unified memory is generous, so big SDXL batches
fit comfortably.

> TL;DR: `brew install python@3.11 git` → clone → `./install.sh` (auto-detects
> `mps`) → `./download-models.sh` → `pixelmon "a dragon"`.

---

## 0. Prerequisites

- **Apple Silicon** Mac (M-series). (Intel Macs fall back to CPU — slow.)
- **Homebrew** — https://brew.sh
- **Python 3.10–3.12** and **git**:
  ```bash
  xcode-select --install            # git + compilers (if needed)
  brew install python@3.11          # macOS's built-in python3 (3.9) is too old
  ```
- To drive it remotely / join a render farm: **Remote Login** (SSH) —
  System Settings → General → Sharing → **Remote Login: On**.

`★ Homebrew PATH gotcha ───────────────────────────`
Homebrew installs to `/opt/homebrew/bin`, which is **not** on the PATH of a
*non-interactive* SSH shell (it's added in `~/.zprofile`, only sourced for login
shells). So over SSH, `python3.11`, `gh`, `tmux` etc. may be "command not found".
Fix per-command with `export PATH="/opt/homebrew/bin:$PATH"`, or run `install.sh`
with an explicit interpreter: `PYTHON=/opt/homebrew/bin/python3.11 ./install.sh`.
`─────────────────────────────────────────────────`

---

## 1. Install

```bash
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon
PYTHON=python3.11 ./install.sh      # auto-detects Apple Silicon -> 'mps' -> Metal torch
./download-models.sh                # ~7.6 GB of models
```
(Private repo: if `git clone` can't auth, copy the repo over from another machine,
or use a GitHub token. `gh` may fail over SSH because its token is in the locked
login keychain.)

---

## 2. Verify MPS

```bash
~/ComfyUI/.venv/bin/python -c "import torch; print(torch.__version__, 'mps', torch.backends.mps.is_available())"
# expect:  2.x.x mps True
pixelmon "a fierce dragon"          # first render (loads the model into unified memory)
```

---

## 3. Run / watch

```bash
~/launch-comfyui.sh                 # banner: "Apple Silicon (MPS / Metal)"
```
Open **http://localhost:8188** — ComfyUI shows live progress. The log will read
`Device: mps` and `Set vram state to: SHARED` (unified memory — no `--lowvram`).

**Keep it running headless:**
```bash
brew install tmux
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
# attach: tmux attach -t comfy     (detach: Ctrl-b then d)
```
(macOS has no systemd; just keep the Mac awake / the session alive. To prevent
sleep during long runs: `caffeinate -i tmux attach -t comfy`.)

---

## 4. Join a render farm

macOS puts ComfyUI on the LAN directly (no NAT like WSL). The **first time** it
listens, macOS may pop a firewall prompt → click **Allow**. Then from your main box:

```json
// servers.json
{ "local": "http://127.0.0.1:8188", "mac": "http://192.168.1.50:8188" }
```
```bash
pixelmon --batch "bat,skeleton" -n 20 --server rtx,mac,local   # fan across all GPUs
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: python3.11 / tmux / gh` over SSH | Homebrew PATH not loaded — `export PATH="/opt/homebrew/bin:$PATH"` (see gotcha above) |
| venv created but `import torch` says no MPS | you used the old system python 3.9 — recreate with `PYTHON=python3.11 ./install.sh` |
| `EXTRA[@]: unbound variable` on launch | old bug on bash 3.2 — `git pull` (fixed) |
| Other machines can't reach `:8188` | macOS firewall — allow incoming for the python process (System Settings → Network → Firewall) |
| `gh` clone fails over SSH (401) | the gh token is in the locked login keychain — copy the repo over SSH or use an HTTPS token |

---

## Notes

- **Speed:** MPS runs SDXL fine but is slower per image than NVIDIA CUDA — expect it
  to be the slowest of a mixed CUDA/MPS fleet. Its strength is **large unified
  memory** (e.g. 32 GB) for big batches/resolutions, plus zero driver hassle.
- Being ARM, the x86 AVX2 `Illegal instruction` issue (see other guides) can't occur.
- Everything else — palettes, styles, `--animate`, `--server` farms — works the same;
  see the main [README](README.md).
