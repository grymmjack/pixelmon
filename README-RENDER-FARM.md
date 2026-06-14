# pixelmon render farm — multiple GPUs over the LAN

Spread generation across **every GPU box on your network**. pixelmon submits jobs
to a pool of ComfyUI servers with **dynamic dispatch** (each GPU pulls its next job
the moment it's free, so faster cards do more) and **fetches all results back** to
the machine you ran it from. Mixed vendors work together — AMD/ROCm + NVIDIA/CUDA +
WSL + Apple/MPS in one fleet.

```bash
pixelmon --batch "bat,skeleton,spider" -n 30 --server rtx,titan,local,mac
#   -> 90 sprites fanned across 4 GPUs; all land in your local output
```

---

## How it works

- Each box runs a normal ComfyUI on `0.0.0.0:8188` (that's what `launch-comfyui.sh` does).
- `--server a,b,c` (a comma-list) = **farm mode**. You run pixelmon on *one* box (the
  "client"); it submits over HTTP to each server in the pool and downloads results
  via ComfyUI's `/view` endpoint. **No shared filesystem / NFS needed.**
- **Dynamic dispatch:** the client keeps one job in flight per box and hands the next
  job to whichever box just finished. Faster GPUs naturally do more; nobody idles.
- **Fault tolerance:** unreachable boxes are skipped at startup; if a box dies
  *mid-run*, its in-flight job is **requeued** to another box. Zero lost sprites.
- The client only needs this repo (no GPU/torch); each server needs ComfyUI + models.

---

## Setup

### 1. Install pixelmon on each GPU box
Follow the platform guide for each machine, get ComfyUI running, and confirm it
renders locally:
- [Linux + AMD (ROCm)](README-LINUX-AMD-ROCM.md)
- [Linux + NVIDIA (CUDA)](README-LINUX-NVIDIA.md)
- [Windows + NVIDIA (WSL2)](README-WINDOWS-NVIDIA.md)
- [macOS (Apple Silicon / MPS)](README-MACOS-APPLE-SILICON.md)

### 2. Make each box reachable on the LAN (port 8188)
`launch-comfyui.sh` already binds `0.0.0.0:8188`. The catch is the host firewall/NAT:

| Platform | What's needed |
|---|---|
| **Linux** (AMD/NVIDIA) | Usually nothing — reachable directly. Open 8188 if you run a firewall (`sudo ufw allow 8188/tcp`). |
| **macOS** | Reachable directly; the **first time** ComfyUI listens, macOS may prompt → **Allow** incoming for python. |
| **Windows/WSL2** | The tricky one. Use **mirrored networking** (`%UserProfile%\.wslconfig`: `[wsl2]` / `networkingMode=mirrored`, then `wsl --shutdown`), **and open 8188 in Windows Firewall** — a normal rule *and*, for mirrored mode, often a Hyper-V rule (PowerShell Admin): `New-NetFirewallRule -DisplayName "ComfyUI 8188" -Direction Inbound -Protocol TCP -LocalPort 8188 -Action Allow` and `New-NetFirewallHyperVRule -Name ComfyUI8188 -DisplayName "ComfyUI 8188" -Direction Inbound -Protocol TCP -LocalPorts 8188 -Action Allow`. |

**Verify from another machine:** `curl http://<box-ip>:8188/system_stats` → JSON = good.

### 3. Name your boxes
Copy `servers.example.json` → `servers.json` (gitignored, so your IPs stay private)
and add aliases:
```json
{
  "local": "http://127.0.0.1:8188",
  "rtx":   "http://192.168.1.50:8188",
  "titan": "http://192.168.1.51:8188",
  "mac":   "http://192.168.1.52:8188"
}
```
You can also skip the file and pass raw hosts: `--server 192.168.1.50,local`.

### 4. Run it
```bash
pixelmon "a goblin" -n 12 --server rtx,titan,local,mac
PIXELMON_FARM=rtx,local ./somescript.sh        # subset via env, in scripts
```
Down boxes are skipped automatically. Watch any box live in a browser at
`http://<box-ip>:8188`.

---

## Keeping the servers running (persistence)

Run each box's ComfyUI in **tmux** so it survives your SSH session:
```bash
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
# watch:  tmux attach -t comfy     (detach: Ctrl-b then d)
```
- **Linux:** also `loginctl enable-linger "$USER"` once, or systemd kills it on logout.
- **WSL:** keep a WSL window open, or add a Windows Task Scheduler entry running
  `wsl -d <distro> -- ~/launch-comfyui.sh` at logon (WSL stops when idle otherwise).
- **macOS:** no systemd; keep the session, and `caffeinate -i tmux attach -t comfy`
  to stop the Mac sleeping mid-run.

---

## SSH (optional — to drive boxes remotely / headless)

Handy for starting servers and managing the fleet from one machine.

1. **Key auth from your control box:**
   ```bash
   ssh-keygen -t ed25519                       # if you don't have a key
   ssh-copy-id <user>@<box-ip>                 # installs your key
   ```
   If `ssh-copy-id` fights you (see gotchas), just paste your public key directly on
   the box: `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '<your id_ed25519.pub>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`.
2. **Alias each box** in `~/.ssh/config`:
   ```
   Host rtx
       HostName 192.168.1.50
       User <user-on-that-box>
   ```
   Then `ssh rtx 'tmux new-session -d -s comfy "~/launch-comfyui.sh ..."'` etc.

---

## Gotchas we actually hit (and the fixes)

The real-world stuff, so you don't relearn it:

| Gotcha | Symptom | Fix |
|---|---|---|
| **Wrong username** | `ssh-copy-id` → "Permission denied" forever | the box's login user may differ (e.g. WSL user `gj` vs `grymmjack`). Check it; use the right one in `~/.ssh/config`. |
| **Keyring cached a bad password** | retries fail without re-prompting | a GUI askpass saved the wrong password. Force a terminal prompt: `SSH_ASKPASS_REQUIRE=never ssh-copy-id <host>`; clear the keyring entry (Seahorse). Easiest: paste the key directly (above). |
| **authorized_keys perms** | key ignored, still asks for password | `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys` (sshd's StrictModes). |
| **Box IP changed** | "host unreachable" after a reboot / Wi-Fi↔LAN switch | DHCP moved it. Find it via your router's DNS (`dig <name> @<router-ip>`), by MAC (`ip neigh`), or `nmap -p22,8188 192.168.1.0/24`. Set a DHCP reservation to pin it. |
| **WSL `nvidia-smi` not found** | install detects CPU, not the GPU | it's at `/usr/lib/wsl/lib` (not on PATH). `install.sh`/`launch-comfyui.sh` now probe it automatically. |
| **WSL port 8188 unreachable** | `curl` from LAN times out (but localhost works) | mirrored networking + a Windows Firewall rule for 8188 (regular **and** Hyper-V; see Setup §2). |
| **WSL missing venv** | `ensurepip is not available` | `sudo apt install python3.10-venv` (or your version). |
| **Windows owns port 22** | WSL sshd conflicts | Windows had its own OpenSSH on 22 — disable it, or run WSL sshd on another port (e.g. 2222). |
| **Old CPU, no AVX2** | `Illegal instruction (core dumped)` at ComfyUI start | a prebuilt wheel needs AVX2. Drop the offender if optional: `pip uninstall -y kornia kornia_rs` (only used by ComfyUI post-processing nodes pixelmon doesn't need). |
| **macOS Homebrew PATH** | `python3.11`/`gh`/`tmux` "not found" over SSH | `/opt/homebrew/bin` isn't on the non-interactive PATH — `export PATH="/opt/homebrew/bin:$PATH"` (or `PYTHON=python3.11 ./install.sh`). |
| **macOS `gh` over SSH** | `gh` clone fails 401 | its token is in the locked login keychain. Copy the repo over SSH (`tar \| ssh host 'tar x'`) or use an HTTPS token. |
| **Old GPU crashes under load** | a box dies mid-run (no error, not OOM) | e.g. a Pascal card under sustained SDXL. Give it `--lowvram`: `~/launch-comfyui.sh --lowvram`. The farm requeues its job regardless. |
| **A box keeps dropping** | repeated mid-run failures | exclude it: `--server rtx,local,mac` (or `PIXELMON_FARM=...`). You barely lose throughput if it was a slow box. |

---

## Tips

- **Benchmark each box** (warm, distinct seeds): `pixelmon "a dragon" --size 128 --seed 1` ×2,
  read the `all done in …s` line. Faster boxes auto-pull more work in the farm.
- The per-call `~Xm estimated` line is **not** farm-aware (assumes one GPU) — ignore it
  during farm runs; measure real throughput by counting finished files over time.
- Big unattended batch: `pixelmon --batch "…" -n 64 --server <all> --output-to out --create-dirs`.
- Add `--no-subdirs` to dump everything into one browsable folder (files are uniquely named).
