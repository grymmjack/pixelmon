# pixelmon — pixel-art sprite generator (NVIDIA · AMD · CPU)

Generate game-ready pixel-art sprites from a text prompt with **one command**,
using ComfyUI + Stable Diffusion XL + the *Pixel Art XL* LoRA. Setup
**auto-detects your GPU** — NVIDIA (CUDA), AMD (ROCm), or CPU — and configures
itself. Built and battle-tested on an **AMD Radeon RX 6600** (Debian 13, ROCm);
see [GPU support](#gpu-support-nvidia--amd--cpu) for the NVIDIA path.

```bash
pixelmon "a fierce dragon"                     # full-quality sprite
pixelmon "a goblin" -n 8 --fast                # 8 quick variations
pixelmon "a knight" --palette PICO-8 --size 32 --transparent
```

# GUI
<img width="3840" height="1978" alt="image" src="https://github.com/user-attachments/assets/d9030441-db14-4a07-8872-fac22e25568b" />

<p>
  <img src="examples/knight.png" width="180" alt="knight sprite">
  <img src="examples/wizard-endesga.png" width="180" alt="wizard sprite, ENDESGA-32 palette">
  <img src="examples/portrait-dosrpg.png" width="180" alt="DOS CRPG portrait, dosrpg style">
  <img src="examples/spider-1.png" width="150" alt="spider sprite">
</p>

*(`pixelmon "a knight in armor"` · `--palette ENDESGA-32` · `--style dosrpg` (Ultima/Wasteland portrait) · `"a spider"`)*

---

## Platform setup guides

Pick your machine — each is a self-contained walkthrough:

| Platform | Guide |
|---|---|
| **Linux + AMD (ROCm)** | [README-LINUX-AMD-ROCM.md](README-LINUX-AMD-ROCM.md) |
| **Linux + NVIDIA (CUDA)** | [README-LINUX-NVIDIA.md](README-LINUX-NVIDIA.md) |
| **Windows + NVIDIA (WSL2)** | [README-WINDOWS-NVIDIA.md](README-WINDOWS-NVIDIA.md) |
| **macOS (Apple Silicon / MPS)** | [README-MACOS-APPLE-SILICON.md](README-MACOS-APPLE-SILICON.md) |
| **Multi-GPU render farm** | [README-RENDER-FARM.md](README-RENDER-FARM.md) |

`install.sh` auto-detects your GPU, so the core is the same everywhere
(`./install.sh` → `./download-models.sh` → `pixelmon "…"`); the guides just cover
each platform's quirks (drivers, networking, persistence). Mixed fleets work great
together — see [Render farm](#render-farm--fan-jobs-across-multiple-gpus).

---

## What this is

A thin, friendly CLI (`pixelmon`) over a local **ComfyUI** server, plus a custom
ComfyUI node (`pixelart_palette`) that turns the model's output into a true,
small, palette-locked sprite. You type a prompt; you get a PNG. The visual
node-graph is handled behind the scenes.

**The single most important lesson:** real pixel art needs a model *trained on
pixel art*. A general model (SD 1.5 / SDXL base) downscaled just looks like a
crushed photo. The **Pixel Art XL LoRA on SDXL** is what makes it genuinely
sprite-shaped — see [Lessons learned](#lessons-learned-the-gotchas).

---

## GPU support (NVIDIA · AMD · CPU)

`install.sh` and `launch-comfyui.sh` **auto-detect the GPU vendor** and configure
the matching PyTorch wheel + launch flags — the same repo runs on any of these
with no flags. The CLI itself (`pixelmon.py`, `animate.py`) is vendor-agnostic: it
talks to ComfyUI over HTTP and runs CLIPSeg on CPU, so only those two setup
scripts are GPU-specific.

| Vendor | PyTorch | Launch config applied |
|---|---|---|
| **NVIDIA (CUDA)** | `cu124` wheel | none — ComfyUI auto-manages VRAM (12 GB+ runs SDXL fully loaded) |
| **AMD (ROCm)** | `2.5.1+rocm6.2` | `HSA_OVERRIDE_GFX_VERSION=10.3.0` · `render` group · `--lowvram` |
| **CPU** | cpu wheel | `--cpu` (works, but very slow) |

Detection: `nvidia-smi` present → NVIDIA; `/dev/kfd` present → AMD ROCm; else CPU.
Override with `PIXELMON_GPU=nvidia|amd|cpu`; pick an interpreter with `PYTHON=python3.11`.

### Tested / target machines

| | GPU | OS · stack | Notes |
|---|---|---|---|
| **Dev box** (battle-tested) | AMD Radeon RX 6600 (Navi 23, **gfx1032**, 8 GB) | Debian 13 · ROCm 6.2 · torch 2.5.1+rocm6.2 · Py 3.10 · 62 GB RAM | needs the gfx1032→gfx1030 override + `render` group + `--lowvram` |
| **NVIDIA box** (port target) | NVIDIA Titan Xp (**Pascal**, 12 GB) | Debian 12 · CUDA | no override / group / lowvram needed |

Other RDNA2/RDNA3 AMD cards likely work too (you may need a different
`HSA_OVERRIDE_GFX_VERSION`, or none on officially-supported cards).

### Performance note

Raw SDXL speed tracks the GPU's **fp16 throughput**, not its age or VRAM. Measured
on the two machines above — one 128px sprite (SDXL + Pixel-Art-XL, 25 steps, warm):

| GPU | per sprite |
|---|---|
| AMD RX 6600 (RDNA2, 8 GB) | ~49 s |
| NVIDIA Titan Xp (Pascal, 12 GB) | ~66 s |

The newer RDNA2 card is ~1.35× faster here — Pascal has weak native fp16. An older
big-VRAM card's edge is **headroom** (runs SDXL fully loaded, bigger batches/res)
and **stability** (CUDA, no ROCm driver hangs), not throughput. Benchmark your own:
`pixelmon "a dragon" --size 128` and read the `all done in …s` line (use a fresh
`--seed` each run — identical prompts hit ComfyUI's cache and report ~0s).

### Running on an NVIDIA box

Simplest model: run pixelmon **on** the NVIDIA machine (everything local — no
network or filesystem plumbing). With the NVIDIA driver installed so `nvidia-smi`
works:

```bash
sudo apt install -y python3.11-venv git          # if needed (Debian 12)
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon && ./install.sh                     # auto-detects nvidia → CUDA wheel
./download-models.sh --all                        # every model (or no flag = core only)
# confirm CUDA actually sees the card:
~/ComfyUI/.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
~/launch-comfyui.sh                               # banner should read "NVIDIA CUDA (…)"
pixelmon "a fierce dragon"
```

> **Pascal caveat (Titan Xp = sm_61).** Current PyTorch still ships Pascal kernels.
> If the `torch.cuda` check above ever errors about an unsupported architecture,
> pin an older CUDA wheel — swap `cu124` → `cu121` on the torch line in `install.sh`.

**Keep it running headless.** A ComfyUI you start over SSH gets killed when the
session ends — unless you (a) enable lingering once so your processes survive
logout, and (b) run it under `tmux`:

```bash
loginctl enable-linger "$USER"
tmux new-session -d -s comfy '~/launch-comfyui.sh 2>&1 | tee ~/comfyui.log'
# watch it:   tmux attach -t comfy        (detach: Ctrl-b then d)
# or the GUI: http://<gpu-host-ip>:8188   (ComfyUI shows live generation progress)
```

### Render from another machine (`--server`)

Once ComfyUI is up on the GPU box, drive it from **any other machine** on the LAN —
pixelmon submits over HTTP and **fetches the results back to you** (no NFS/SSHFS):

```bash
pixelmon "a dragon" --server 192.168.1.50            # raw host (defaults to :8188)
pixelmon "a dragon" --server http://192.168.1.50:8188
pixelmon "a dragon" --server gpubox                  # a named alias (below)
```

Name your machines in **`servers.json`** (copy `servers.example.json`) so you can
use short aliases — it's gitignored, so your IPs stay out of the repo:

```json
{ "_default": "gpubox", "local": "http://127.0.0.1:8188", "gpubox": "http://192.168.1.50:8188" }
```

**`"_default"`** is where renders go when you don't pass `--server` (an alias, host/URL or
comma-list farm; leave it out for `local`). `$PIXELMON_SERVER` works too, and beats `"_default"`. The remote box just needs ComfyUI + models running;
the client only needs this repo (no GPU/torch). Results land in your local
`~/ComfyUI/output/pixelmon/` (or wherever `--output-to` points). The `pixelmon`
wrapper only starts a local ComfyUI when the target **is** this machine's default one
(`127.0.0.1:8188`, the `local` alias, or a farm list that includes it) — never for a remote
box, another local port, `--version`, `--help`, the `--list-*` flags or `--dry-run`.
With no target at all pixelmon uses the `local` alias, so pointing `"local"` at another
address in `servers.json` moves the default everywhere.

Before sending work, pixelmon **checks the server has what the render needs** — every
node, every node input (an outdated `pixelart_palette` on a farm box would silently
ignore `--thin-lines`, `--pixel-size`, oklab …) and every model file (checkpoint,
LoRAs, ControlNets, IPAdapter, BiRefNet) — and says what to run if not (`provision it:
./download-models.sh --lab`, `update its pixelmon … then restart its ComfyUI`).
`pixelmon --list-servers [your usual flags]` shows every box: up or down, its GPU, its
queue, and whether it can render that command. `--no-check` skips the check.

If ComfyUI rejects a request anyway, the error names the node and the input
(`node 15 (LoraLoader): Value not in list: lora_name …`). A server that breaks
(the `model_size` / `is_dynamic` errors that fail every prompt until a restart) is
restarted with its `"_restart"` command from `servers.json` when it has one, and the
render is sent again once it's back.

**Stopping a run cleans up after it.** Ctrl-C, the GUI's Cancel (SIGTERM) or any early
exit takes *this run's* queued prompts off every server's queue and interrupts the one
rendering — only if it's ours, so a shared ComfyUI keeps rendering soundmon's, the
GUI's or another pixelmon's work. A job that fails is reported (`✗ [3/8] … — the
node's error`) and the batch carries on; pixelmon exits non-zero if any job produced
nothing.

### Render farm — fan jobs across multiple GPUs

Pass `--server` a **comma-list** and pixelmon turns into a render farm: it spreads
the work across every box and fetches all results back to you.

```bash
pixelmon --batch "bat,skeleton,spider" -n 30 --server rtx,titan,local
#   -> 90 sprites fanned across 3 GPUs; all land in your local output
```

It uses **dynamic dispatch** — each GPU is handed its next job the moment it goes
free, so faster cards automatically do more (no manual balancing) and nobody idles.
Unreachable boxes are skipped, and so are boxes missing a model or an up-to-date node
(with the command that fixes them). If a box stops answering for 20 s mid-run its job
goes to another; a job that fails is tried once more **on another box**; a box that
fails 2 jobs in a row is dropped (a broken box fails fast, frees up first and would
otherwise eat the queue). The run exits non-zero if any job produced nothing.
Throughput scales ~linearly with the number of boxes. (Each ComfyUI still runs one
job at a time, so parallelism = number of boxes.) More in [README-RENDER-FARM.md](README-RENDER-FARM.md).

> **Windows + NVIDIA?** See **[README-WINDOWS-NVIDIA.md](README-WINDOWS-NVIDIA.md)**
> for the WSL2 setup (GPU passthrough + the networking needed to join the farm).

---

## Quickstart (already installed)

```bash
pixelmon "a cute slime monster"     # generate (auto-starts the server)
pixelmon --help                     # friendly, colorized help — all options
pixelmon --list-palettes            # available palettes
```

Output PNGs land in `~/ComfyUI/output/pixelmon/`:
- `*_sprite_*.png` — the **true-size** sprite (e.g. real 128×128) — your game asset
- `*_preview_*.png` — an **enlarged** copy to eyeball easily — only with `--preview`

The seed is in every filename, so to make a full-quality version of a fast draft
you liked, just re-run that seed:
```bash
pixelmon "a dragon" --fast            # prints e.g. seed=12345
pixelmon "a dragon" --seed 12345      # same dragon, full quality
```

---

## Install from scratch

Everything pixelmon needs, start to finish. Steps 1–3 are all a single machine needs; step 5
is for rendering on a second, faster GPU box.

**1. Get the code and build the engine**
```bash
git clone https://github.com/grymmjack/pixelmon.git ~/pixelmon
cd ~/pixelmon
./install.sh
```
`install.sh` clones ComfyUI into `~/ComfyUI`, builds its Python venv with the right PyTorch for
your GPU, installs OpenCV, the IPAdapter node and the pixel-snapper, links pixelmon's node and
commands into place (`pixelmon`, `pixelmon-gui`), and copies the shipped preset folders
(`presets/`, with their images) into `~/pixelmon-gallery/gui-presets/`. It's safe to re-run.

**2. Download the models**
```bash
./download-models.sh --list     # what you have / what's missing
./download-models.sh            # core: SDXL + the style LoRAs              (~7.9 GB)
./download-models.sh --all      # + LAB, steering, Juggernaut XL           (~25 GB)
```
Pick groups with `--lab`, `--steer`, `--depth`, `--juggernaut` instead of `--all`. Files you already have are
skipped. If a Civitai download asks for a login, make a free key at civitai.com (Account → API
keys) and run `CIVITAI_TOKEN=<key> ./download-models.sh`.

| Group | File (in `~/ComfyUI/models/…`) | Size | Used by |
|---|---|---|---|
| core | `checkpoints/sd_xl_base_1.0.safetensors` — [SDXL base 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) | 6.9 GB | everything |
| core | `loras/pixel-art-xl.safetensors` — [Pixel Art XL](https://huggingface.co/nerijs/pixel-art-xl) | 171 MB | pixel art (the default LoRA), most style guides |
| core | `loras/lcm-lora-sdxl.safetensors` — [LCM-LoRA SDXL](https://huggingface.co/latent-consistency/lcm-lora-sdxl) | 394 MB | `--fast`, LAB live preview |
| core | `loras/dosegafx.safetensors` — [EGA retro style](https://civitai.com/models/290771) | 82 MB | the `dosega` style |
| core | `loras/retro-game-art.safetensors` — [Retro Game Art](https://civitai.com/models/553027) | 218 MB | the `r3tr0` style |
| core | `loras/pixelartredmond.safetensors` — [PixelArtRedmond](https://huggingface.co/artificialguybr/PixelArtRedmond) | 163 MB | the `pixelartredmond` style |
| `--lab` | `controlnet/controlnet-union-sdxl-promax.safetensors` — [ControlNet union](https://huggingface.co/xinsir/controlnet-union-sdxl-1.0) | 2.4 GB | LAB convert, `--control`, "keep shape & direction" |
| `--lab` | `unet/sdxl-inpainting-0.1.fp16.safetensors` — [SDXL inpainting](https://huggingface.co/diffusers/stable-diffusion-xl-1.0-inpainting-0.1) | 5.1 GB | LAB edits that paint something new, `--inpaint-model` |
| `--steer` | `ipadapter/ip-adapter-plus_sdxl_vit-h.safetensors` — [IP-Adapter](https://huggingface.co/h94/IP-Adapter) | 848 MB | Steering tab, evolve, `--steer` |
| `--steer` | `clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | 2.5 GB | the image encoder for IP-Adapter |
| `--depth` | `controlnet/controlnet-depth-sdxl-1.0.safetensors` — [ControlNet depth SDXL](https://huggingface.co/diffusers/controlnet-depth-sdxl-1.0) | 2.5 GB | Steering tab depth lock, `--depth` |
| `--depth` | `background_removal/birefnet.safetensors` — [BiRefNet](https://huggingface.co/ZhengPeng7/BiRefNet) | 444 MB | `--depth-mask-bg` |
| `--juggernaut` | `checkpoints/juggernautXL_v9.safetensors` — [Juggernaut XL v9](https://huggingface.co/RunDiffusion/Juggernaut-XL-v9) | 6.6 GB | optional alternative checkpoint (⚙ Advanced) |
| automatic | Depth Anything V2 (via [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux)) | ~1.3 GB | `--depth`; fetched on first use |
| automatic | CLIPSeg ([CIDAS/clipseg-rd64-refined](https://huggingface.co/CIDAS/clipseg-rd64-refined)) | ~600 MB | LAB "select by words", `--animate`; fetched on first use |

The GUI only offers what the render server actually has: LoRAs, checkpoints, ControlNet,
IPAdapter and inpainting models are read live from ComfyUI, so a missing group just means that
feature's menu is empty.

**3. Render**
```bash
pixelmon "a fierce dragon" --no-open     # the CLI
pixelmon-gui                             # the web GUI → http://127.0.0.1:8190
```
Both render on `local` (this machine's ComfyUI, port 8188) unless told otherwise, and `pixelmon`
starts that ComfyUI by itself the first time it's needed (~15 s; log in `~/ComfyUI/server.log`).
To keep it running yourself, use `~/launch-comfyui.sh`. On **AMD/ROCm**, log out and back in once
first (so the `render` group sticks).

**4. Your own LoRAs (optional)**
The `ega-art-v2` and `dosart-vga` LoRAs in the GUI's LoRA list were trained on a private art
collection and aren't downloadable. Any SDXL LoRA works: drop the `.safetensors` into
`~/ComfyUI/models/loras/` (on the render server) and it appears in the LoRA menu. To give it a
trigger word, default strength and palette in the GUI, add it to `gui/presets.json`.

**5. A separate render server (optional)**
Run steps 1–2 on the GPU box and start ComfyUI there with `~/launch-comfyui.sh` (it listens on the
network). On your desk machine, copy `servers.example.json` to `servers.json` and name the box:
```json
{ "local": "http://127.0.0.1:8188", "gpubox": "http://192.168.1.50:8188" }
```
Then `pixelmon … --server gpubox` renders there. For the GUI, pick `gpubox` in 🛠 Setup ›
**Render server** (the default is `local`). Model files live on the render server, so run
`download-models.sh` there. Details: [render farm guide](README-RENDER-FARM.md).

> **GPU auto-detection.** `install.sh` and `launch-comfyui.sh` detect your card —
> **NVIDIA (CUDA)**, **AMD (ROCm)**, or **CPU** — and configure the matching
> PyTorch wheel and launch flags automatically. The `render`-group + HSA-override
> + `--lowvram` steps are **AMD-only**; NVIDIA skips them. Force a vendor with
> `PIXELMON_GPU=nvidia|amd|cpu`, and pick an interpreter with `PYTHON=python3.11`.
> Per-OS guides: [Linux AMD](README-LINUX-AMD-ROCM.md) · [Linux NVIDIA](README-LINUX-NVIDIA.md) ·
> [Windows NVIDIA (WSL2)](README-WINDOWS-NVIDIA.md) · [macOS Apple Silicon](README-MACOS-APPLE-SILICON.md)

---

## Usage

Run `pixelmon --help` for the full, colorized list. The essentials:

| Flag | What it does | Default |
|---|---|---|
| `-n, --number N` | how many to make, each a different seed | `1` |
| `--size N\|WxH` | square `N` (128 = sharpest), or non-square `WxH` e.g. `32x48` for tall character sprites | `128` |
| `--palette NAME` | `none` (model's colors), `random` (a different one per image), one of **55 bundled** (PICO-8, DAWNBRINGER-16, ENDESGA-32, NES, …, `--list-palettes`), or `Custom` | `none` |
| `--style NAMES` | append proven style guide(s), comma-separated (e.g. `geometric,detailed`) — `--list-styles` | — |
| `--batch "a,b,c"` | round-robin subjects, one of each per pass, each into its own folder (`-n` = how many of each) | — |
| `--batch-file FILE` | a sprite list, one per line: `key \| prompt` (or `key \| style \| size \| prompt`); files are named by key — see [Batches](#batches--organizing-output) | — |
| `--skip-existing` | with `--batch`/`--batch-file`/`-n`: only make the takes that aren't there yet (resume a stopped list) | off |
| `--palette-from IMG` | lock to a reference picture's own colours: all of them up to 64 (pixel art), else its 32 main colours — the GUI's pick; `--palette-max N` caps it | — |
| `--out N\|WxH` | exact final canvas size, up to 4096 (sampling still follows `--size`, which defaults to `--out`); also forces the size with `--snap-pixels`. Pixel art past 1024 is made at 1/2–1/16 of the size and enlarged by exactly that factor (crisp square pixels) | `--size` |
| `--art` | digital art instead of pixel art — see [Art mode](#art-mode---art) | off |
| `--raw` | the LoRA's own full-res picture: same prompt + LoRA as a pixel render, no downscale / palette / cleanup | off |
| `--palette-strength F` | with `--art` and a `--palette`: how far colors move toward the palette, 0–1 (1 = exact palette colors) | `0.6` |
| `--post-sweep SPEC` | render **once**, then save every combination of post-processing settings — e.g. `'angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1'` (fields: `angle_grid`, `dither`, `dither_amount`, `pixel_angles`, `thin_lines`, `despeckle`, `palette`); ~1–2 s per picture after the render | — |
| `--snap-pixels` | snap to a perfect grid with the [pixel-snapper](https://github.com/Hugo-Dz/spritefusion-pixel-snapper) — extra crisp (picks its own grid; add `--out` for an exact size) | off |
| `--snap-method M` | `--snap-pixels` engine: `unfake` finds the grid the model really drew and reduces it to `--out` (sharpest); `snapper` = pixel-snapper | `unfake` |
| `--fit-subject` | **fill frame**: crop the full-res render to its subject before shrinking, so tiny sprites (16–32 px) spend their pixels on the subject, not the background | off |
| `--color-match M` | how the palette lock picks each pixel's nearest color: `oklab` (perceptual: keeps hue and lightness as the eye sees them) or `redmean` (the pre-1.9 RGB approximation) | `oklab` |
| `--init IMG` / `--denoise F` | img2img: start from a picture (keeps its composition; `--denoise` 0–1 = how much changes). Without `--size`/`--out` the result takes the picture's shape (a 320×200 screen → 128×80) | — / `0.6` |
| `--depth IMG` | **depth lock**: keep a reference's camera angle + geometry, the prompt sets the rest — see [Depth lock](#depth-lock--camera-angle--geometry-from-a-reference---depth) | — |
| `--despeckle N` | after the palette lock, recolor stray same-color islands of ≤ N px (removes speckle noise); 0 = off | `2` |
| `--transparent` | cut out the background → transparent PNG | off |
| `--cutout` | cut the subject out with BiRefNet (a background-removal model on the render server); copes with any background — see [Cut-outs](#cut-outs--remake-a-games-sprites-at-the-same-size---cutout---fit-to) | off |
| `--cutout-key [TOL]` | with `--cutout`, also keep anything further than TOL from the background colour (parts as grey as the backdrop) | off (34 when given) |
| `--fit-to OLD.png` | fit the cut-out into an old picture's footprint: same width-or-height, centred, standing on the same line; implies `--cutout` | — |
| `--fit-scale N` | `--fit-to` canvas = the old picture's size × N | 4 with `--art`/`--raw`, 1 for pixel art |
| `--mesh IMG` | make a 3D model (`.glb`) of the picture's subject with Hunyuan3D 2, plus a Blender preview — see [3D](#3d-models-turnarounds-and-low-poly---mesh---turnaround---lowpoly) | — |
| `--turnaround SRC` | rotation sprites from a picture (or a `.glb/.gltf/.obj/.stl/.ply/.fbx` model): N views, each painted to match | — |
| `--angles N` / `--elev DEG` | `--turnaround`: how many views around the model / camera height above eye level | 8 / 0 |
| `--shape SPEC` | build the model straight from the picture's outline instead of Hunyuan3D: `extrude[:DEPTH[,SHARE:DEPTH…]]` (its outline pushed out DEPTH × its width; boxes 0.5–0.8, guns 0.08, cards 0.04) or `lathe[:SEGMENTS]` (spun round its middle: barrels, tanks, jars). A clean, solid low-poly model wearing the picture, crisp, as `NAME_textured.glb` (+ `.gltf`/`.bin`/`_texture.png`); no render server | off |
| `--shape-grid N` | `--shape`: cells across the outline (8–256); fewer = blockier, fewer triangles | 64 |
| `--lowpoly FACES` | also make a faceted low-poly model of about FACES faces; a turnaround renders and paints that one | off |
| `--texture` | also make a clean, **textured** model: quads, a UV map and the picture baked into a texture (`NAME_textured.glb`, plus `.gltf`/`.bin`/`_texture.png` to paint) — about 6000 quads, or `--lowpoly FACES` flat shaded | off |
| `--texture-size PX` / `--texture-elev DEG` | `--texture`: the texture's size (256–8192) / how far above the subject the picture was seen from (0–80) | 1024 up to 1500 faces, else 2048 / guessed (0, 30 or 55) |
| `--blender CMD` | how to run Blender (3D) | `$PIXELMON_BLENDER`, `blender`, the `org.blender.Blender` flatpak, or a usual install folder |
| `--tile [x\|y]` | **seamless tileable texture**: the model and VAE wrap at the edges (circular padding) and the pixel pass filters across them, so the result repeats with no seam (both axes, or just `x` / `y`). Drops the sprite wording and adds an edge/object negative. Also saves a 3×3 `_tiled_` preview for checking seams. Needs the updated `pixelart_palette` nodes on the render server | off |
| `--preview` | also save an enlarged, zoomed-in PNG (else only the true-size sprite) | off |
| `--output-to DIR` / `--move-to-dirs` / `--create-dirs` | where finished files go — see [Batches](#batches--organizing-output) | — |
| `--dither [NAME]` | dither between palette colors: `bayer2/4/8/16`, `clustered`, `floyd-steinberg`, `jarvis`, `stucki`, `burkes`, `sierra`, `sierra2`, `sierra-lite`, `atkinson` (bare = floyd-steinberg) | off |
| `--dither-amount F` | dither strength 0..1 | `0.75` |
| `--exact-prompt` | send the prompt and `--negative` word for word: no added prefix/suffix, style guides or default negative (the GUI's ✎ edit the full prompt) | off |
| `--show-prompt` | print the exact positive + negative prompts sent to the model (after styles and pixelmon's additions) | off |
| `--no-sprite-suffix` | don't append `game sprite, simple flat colors, solid background` (automatic when the prompt contains `scene background`) | off |
| `--fast` | LCM mode: ~5× faster (8 steps), slightly softer | off |
| `--server NAME\|host` | render on a remote ComfyUI (alias from `servers.json`, or `host[:port]`/URL); results fetched back over HTTP — see [Render from another machine](#render-from-another-machine---server) | `servers.json` `"_default"`, else local |
| `--seed N` | lock / repeat a result | random |
| `--steps`, `--cfg` | refinement steps / prompt adherence | 25 / 7 |
| `--lora-strength N` | how strongly to pixelate | 1.0 |
| `--custom-hex "…"` | colors for `--palette Custom` | — |
| `--dry-run` / `--print-graph` | show the jobs, prompts and destinations / print the ComfyUI API graph (JSON) — nothing is sent | — |
| `--list-servers` | every render server: up?, GPU, queue, can it render this command? | — |

### Style guides (`--style`)

Style guides are proven prompt snippets appended to your prompt to steer the
look — they live in editable `styles.json` (`--list-styles` shows all). Combine
them: `--style geometric,detailed`. The `geometric` guide is built to fight
"too tame / rounded" output — it emphasizes `(sharp angular geometric:1.3)` and
pushes *rounded, smooth, organic, blobby* into the negative prompt:

```bash
pixelmon "a spider" --style geometric           # angular, spiky
pixelmon "a hero" --style 16bit,outline          # detailed + bold outline
pixelmon "a temple" --style blasphemous           # "in the style of" a game
```
Push harder with `--lora-strength 1.3` or a higher `--cfg`. Add your own guides
by editing `styles.json` (`{"name": {"prompt": "...", "negative": "..."}}`).

**Workflow tip:** explore with `--fast`, then re-run the `--seed` you liked
*without* `--fast` for the full-quality keeper.

### EGA / Wasteland portrait look

The vibrant 16-color **EGA / Wasteland** aesthetic — bold black outlines,
*purposeful* ordered dithering, saturated colors, like the 1988 game *Wasteland* —
comes from a dedicated LoRA, **EGA retro style SDXL**
([Civitai 290771](https://civitai.com/models/290771/ega-retro-style-sdxl), fetched
by `download-models.sh` as `dosegafx.safetensors`). Use it via `--lora` plus the
`dosega` style, which injects its `dosegagfx style` trigger word:

```bash
pixelmon "a grizzled raider" --lora dosegafx.safetensors --style dosega,portrait --palette EGA --size 128
```

Pair with `--palette EGA` to hard-lock the authentic 16 colors. Companion styles:
`ega` (palette/dither descriptors), `wasteland` (full Wasteland portrait look),
`portrait` (head-and-shoulders bust framing). Generate at **128–256px** so the
dithering reads — it averages out at tiny sizes.

### Steering with reference images (`--steer`)

Nudge output toward the *look* of a folder of reference images — palette, texture,
mood — via **IPAdapter** (a CLIP-vision "image prompt" injected alongside your text
prompt and the pixel LoRA). Great for matching a target art style, or a feedback loop
("make more like the sprites I already liked").

```bash
pixelmon "a knight" --steer ~/refs/wasteland --steer-strength 0.5
```

- `--steer DIR|IMG` — a folder (blended) or a single image. pixelmon uploads them to the
  target ComfyUI (so it works on remote/farm boxes too) and batches them in.
- `--steer-strength` (0.7) — **~0.5 keeps your subject and just borrows the look; 0.8+
  starts dictating composition.**
- `--steer-combine` (`concat`) — `average` blends many refs into one style centroid
  (best for big folders); `--steer-max` (16) caps how many are sampled.
- Animated GIFs: extract a static first frame first (`magick in.gif[0] out.png`) —
  ComfyUI's loader otherwise explodes a GIF into all its frames.

**Setup:** the `ComfyUI_IPAdapter_plus` node (cloned by `install.sh`) + the IPAdapter
models (`./download-models.sh --steer`, ~3.4 GB). **VRAM:** the CLIP-ViT-H encoder is
heavy — on an 8 GB card launch ComfyUI with `--lowvram` or it OOMs; 12 GB+ is fine.

### Cut-outs — remake a game's sprites at the same size (`--cutout`, `--fit-to`)

`--cutout` cuts the subject out with **BiRefNet**, a background-removal model on the render
server. It runs on the full-size picture, before pixelation, so it copes with busy backgrounds
and with a subject the palette later turns the same colour as its backdrop. Pixel art gets
hard-edged whole pixels. Art mode gets a soft edge whose see-through pixels carry the edge colour,
so a game that smooths sprites shows no dark halo. `--cutout-key` adds a colour key for parts
as grey as the background, which BiRefNet drops.

`--fit-to OLD.png` then fits the cut-out into an old picture's **footprint**: the same
width-or-height, centred the same way, standing on the same line, on a canvas the old picture's
size × `--fit-scale` (4 for art, 1 for pixel art). Remade monsters stay the same size and stand
on the same line in the game.

```bash
pixelmon "a sci-fi supply crate, plain grey backdrop" --art --fit-to ~/game/crate.png   # 64x64 -> 256x256
```

In **pixelmon-gui**: ✂ **cut out** and **+ colour key** sit with the canvas options. The
**Batch** tab's **✂ fit into the old sprites' footprints** fits every row into
`<old sprites folder>/<row path>.png` (rows with no old picture just render). Export to a
different folder, or a second run would fit into the new pictures.

**Setup:** `./download-models.sh --depth` fetches BiRefNet (444 MB), together with the depth model.

### 3D models, turnarounds and low-poly (`--mesh`, `--turnaround`, `--lowpoly`)

One picture → a **3D model** → **rotation sprites** that all match: the same monster, the same
size, from every side (Doom-style 8 directions, or 4, or 16).
New to 3D? Start with the [3D field guide for 2D artists](docs/3d-guide.html).

```bash
pixelmon --mesh monster.png                         # -> monster.glb + a 4-view preview
pixelmon "a green battle mech" --turnaround monster.png --art              # 8 painted angles
pixelmon "a green battle mech" --turnaround monster.glb --steer monster.png \
    --lowpoly 1500 --angles 4 --palette EGA --out 64                        # low-poly, pixel-art angles
pixelmon --mesh monster.png --lowpoly 400 --texture  # + monster_textured.glb, wearing the picture
pixelmon --mesh crate.png --shape extrude:0.8         # a prop built from its outline: crate_textured.glb
pixelmon --mesh barrel.png --shape lathe             # ... spun round: barrel_textured.glb
```

1. **The model**: Hunyuan3D 2 on the render server turns the picture's subject into a mesh
   (`.glb`, about a minute). An opaque picture is cut out first with BiRefNet. Best from a full-body,
   front-on picture on a plain background.
2. **The angles**: Blender, on the machine running pixelmon, renders the model from `--angles`
   directions with one camera size, so the model is the same size in every view. Each view gets
   an exact depth map. Azimuth 0 is the side the picture showed. `--elev` raises the camera.
3. **The sprites**: each view is painted by pixelmon with **depth lock** on that view's depth map,
   **steering** from the picture (its colours and style), **✂ cut out**, and one seed for all. Every
   other flag applies, so `--palette`, `--out`, `--art`, `--fit-to` and the rest work as usual.
   Files are `<name>_a000…`, `<name>_a045…`, and so on. The Blender renders and depth maps are kept
   in `turn/`.

`--lowpoly FACES` remeshes and decimates the model to about that many faces with flat shading.
That's the faceted low-poly look: 400 is chunky, 1500 detailed. The low-poly `.glb` is kept, and
a turnaround renders and paints it, so the sprites come out faceted too.

`--texture` also makes a **textured model** you can paint, the way the neon-caster 3D set was made:
Blender guesses how high the picture was seen from (by matching the model's outline from 0°, 30° and
55° up; `--texture-elev` sets it), projects the picture onto the model from there, rebuilds the model
as clean quads (QuadriFlow; about 6000, or the `--lowpoly` count with flat shading; plain triangles
for shapes QuadriFlow refuses), unwraps it (Smart UV Project) and bakes the projected colours into a
texture, the picture's pixels kept crisp (and a `--lowpoly` model's texture filtered *nearest*, old-school
Quake style); the backdrop's tint is taken out of the cut-out's edge first, so a magenta backdrop
doesn't fringe the model. The front is the picture, the back its colours seen through, the sides stretched. Files:
`NAME_textured.glb` (texture inside), `NAME_textured.gltf` + `.bin` + `NAME_textured_texture.png`
(paint the PNG, reopen the `.gltf`, and your paint is on the model) and a 4-view
`NAME_textured_s<seed>_views.png`. A turnaround of a model needs `--steer PICTURE` for `--texture`.

`--shape` skips the AI guess for **props seen face-on**, where the outline already holds the shape: a
model from one picture has to invent the depth and the back, so props come out lumpy and leaning and
thin parts split off (a gun painted at a diagonal is the worst case). Built on this machine in about a
second, as one solid, connected, straight-edged model that matches the picture exactly from the front:

- `extrude:DEPTH` pushes the outline out DEPTH × its width. Front and back are the picture; each side
  shows the picture's colours just inside that edge, so it reads as panelling. `,SHARE:DEPTH` makes the
  top SHARE of it that deep instead, with a step down: `extrude:0.55,0.42:0.18` is a desk with a monitor
  on it. Depths that work: crate 0.8, vending machine 0.7, locker 0.55, screens 0.12, guns 0.08–0.1,
  keycard 0.06, map 0.04.
- `lathe` spins each row's half-width round the outline's middle (16 segments; `lathe:24` for more):
  steps where the width jumps (a lid, a rim), tapers elsewhere, capped top and bottom. The picture
  wraps round from the front, mirrored behind.

The picture is the texture (nothing baked): cells of the outline grid sit on whole art pixels and the
`.glb` asks for nearest filtering, so it stays crisp and blocky. Typically 50–700 flat-shaded
triangles. It writes the same files as `--texture` (`NAME_textured.glb`, `.gltf` + `.bin` +
`_texture.png` to paint, a 4-view preview), and `--turnaround PICTURE --shape …` paints its angles
from that model. Organic, irregular things (a plant, a creature) still suit `--mesh` alone.

In **pixelmon-gui**, the **🧊** button on any picture opens its 🧊 3D row. **make 3D model** makes
the model; **turnaround** makes the sprites, with angles, camera height, low-poly and **texture**
(off · on · on with the picture at eye level / a little above / above) options. **shape** builds it from
the outline instead: 📐 box (deep / normal), slab, thin, flat, sheet, round, or custom (`extrude:…` / `lathe`). A model's card (and
the lightbox of its preview) has a row per model (**🧊 model**, **🔺 low-poly**, **🎨 textured**):
the `.glb` to download, **👁 Kaleidotron**, **🧊 Blender** (Blender starts with the model imported,
framed and in Material Preview; 🛠 Setup › **3D editor** picks another program), and **🧊 turnaround**
from that model. The textured one adds **🖌 texture**, which opens its texture in DRAW; save, and
Kaleidotron / Blender show the paint (they open the `.gltf`, which reads the PNG).

**Setup:** `./download-models.sh --3d` (Hunyuan3D 2, 4.9 GB) plus `--steer` and `--depth`, and
**Blender** on the machine running pixelmon (found on the PATH, as the `org.blender.Blender` flatpak,
or in the usual install folders: an unpacked `~/blender-*` or `/opt/blender-*`, the Snap, Steam,
`/Applications`, Program Files; it runs headless on the CPU). An 8 GB card is enough.

### Depth lock — camera angle + geometry from a reference (`--depth`)

Keep the *shape* of a reference — camera angle, pose, what's near and far — while the prompt
decides the subject, backdrop and style. The reference becomes a depth map with
**Depth Anything V2** and drives the SDXL **depth ControlNet**. Built for things like FPS weapon
viewmodels: one render that has the angle right locks every variation to it.

```bash
pixelmon "a laser pistol held in a right hand, plain solid magenta backdrop" --art \
  --depth gun-from-behind.png --depth-mask-bg -n 6
```

- `--depth IMG` — the reference. The depth map is made **once**, at the native SDXL render size
  (e.g. 1024×1024 — not the `--out` size), saved to `<output>/depth/`, and every render (on any
  farm GPU) gets that exact map.
- `--depth-mask-bg` — finds the subject (BiRefNet) and sets all background depth to black, so a
  reference's scenery (a Mars landscape behind the gun) doesn't come back — only the subject's shape.
  **Automatic** for a picture on a plain backdrop (3 of its 4 sides one colour or see-through: a sprite
  padded with black, a cut-out): left as is, Depth Anything reads the empty space as a wall or ceiling
  and the image model paints it, often as a second copy of the subject stacked on the first.
  `--depth-keep-bg` turns that off.
- `--depth-strength` (0.7), `--depth-start` / `--depth-end` (0 / 0.65) — how hard, and for which
  part of the sampling steps; ending early leaves the last steps free for detail.
- `--depth-raw` — the image already *is* a depth map (white = near): no preprocessing.
- `--depth-only` — make and save the depth map, then stop (the GUI's preview uses this).
- Works alongside `--steer` (look) and `--control` (edges) in the same run; not with `--mask` / `--animate`.

**Setup:** the `comfyui_controlnet_aux` node pack (cloned by `install.sh`) + `./download-models.sh --depth`.

### Animation (experimental)

`--animate` makes a **looping portrait-gesture GIF**, the way the original Wasteland
portraits animated: generate one base portrait, then re-paint *only* a small masked
region across a few frames while the rest stays frozen. It auto-masks the region
with text-prompted segmentation (CLIPSeg), so it understands `"the cigar"` /
`"the gun"` / `"the dog's mouth"` — not just human faces.

```bash
pixelmon "a mutant" --animate glow --anim-region "the eyes"          # eye-glow pulse
pixelmon "a mayor" --animate "smoke rising" --anim-box 0,0,0.5,0.62   # custom gesture + manual mask box
```

Knobs: `--anim-fps` (speed), `--anim-hold`, `--anim-frames`, `--anim-denoise`,
`--anim-loop`, and `--anim-region` / `--anim-box` (auto-mask vs. manual). Presets:
`blink`, `talk`, `glow`, `smoke`, `breathe`. Full list in `pixelmon --help`.

> ⚗ **Experimental.** At sprite scale the model can't author crisp 2-pixel motion —
> glow/light gestures read well, but subtle ones (blink, small mouths, rising smoke)
> often misread. For production sprites, render a **static** image and hand-animate
> it. Fully opt-in: nothing runs unless you pass `--animate`.

### Art mode (`--art`)

A full-resolution painting instead of pixel art: no pixel LoRA, no pixelation.

```bash
pixelmon "a spaceship above a ringed planet" --art --out 1024x576
pixelmon "…" --art --out 256x224 --palette PINEAPPLE-32 --palette-strength 0.6     # tinted by a palette
pixelmon "…" --art --out 3840x2160 --palette 1BIT --palette-strength 1 --dither bayer8   # dithered 4K wallpaper
```

- **Size:** SDXL paints at 1024 on the long side. A smaller `--out` is a smooth resize of that
  picture (SDXL can't draw at 256 px — it makes abstract blobs); a bigger one (wallpapers up to
  4096) is painted at about 1 megapixel in that shape and resized up, so it's a little soft.
- **Palette:** `--palette` pulls every color toward its nearest palette color without pixelating.
  `--palette-strength` 0.4–0.7 keeps smooth shading in the palette's colors; `1` = only exact
  palette colors, a posterized painting you can open in DRAW with the palette.
- **Dither:** `--dither` / `--dither-amount` work with the palette too (strongest at strength 1):
  bayer is instant, error diffusion takes ~7 s per megapixel.

### Batches & organizing output

By default files stay in `~/ComfyUI/output/pixelmon/`. To organize a run into a
folder **relative to where you run the command**, use `--move-to-dirs` (one
folder per prompt) or `--output-to DIR` (a specific folder); add `--create-dirs`
to make missing folders.

**`--batch` is the overnight workhorse.** Give it several subjects and it
round-robins — one of each per pass — so every folder fills *evenly* instead of
finishing one subject before starting the next:

```bash
cd ~/sprites
pixelmon --batch "bat,skeleton,spider" -n 128 --fast
#  -> ./bat/ ./skeleton/ ./spider/, each filling up 1-at-a-time as it runs
```
Every other flag still applies (`--style`, `--palette random`, `--transparent`,
`--snap-pixels`, …). An interrupted run is still organized — files are moved out
of ComfyUI's output as each one finishes.

**`--batch-file` is for a game's sprite list.** `--batch` splits on commas, so it can't
hold real prompts ("a goblin, red cloak, holding a torch"). A list file can — one
sprite per line, the same lists the GUI's **Sets** tab reads:

```text
# sprites.txt
[Enemies]
enemies/bat | a cave bat, wings spread, red eyes
enemies/rat | geometric | 32x48 | a sewer rat holding a dagger     <- its own --style and --size
door | 64 | a wooden door                                          <- its own size
a glowing potion bottle                                            <- named by its slug
```

```bash
cd ~/mygame/sprites
pixelmon --batch-file sprites.txt -n 4 --palette EGA
#  -> ./enemies/bat_128x128_EGA_s…_sprite_00001_.png (4 takes), ./enemies/rat_32x48_…, ./door_64x64_…, …
pixelmon --batch-file sprites.txt -n 4 --palette EGA --skip-existing   # after a stop: only what's missing
```

Each row's files start with its key (a key with a folder goes into that folder),
rows round-robin like `--batch`, and `-n` is takes per row. `--skip-existing` counts
what each key already has (`-n 4` with 2 made → 2 more; with `--seed`, a seed whose
file exists is skipped). Rows prefixed with another medium (`ansi-art/…`) are skipped.

**Every PNG says how it was made.** ComfyUI embeds its graph in each PNG (a `prompt`
text chunk) and pixelmon adds a `pixelmon` chunk: version, prompt, seed, server and
the command line — kept through every resize, cut-out and palette pass.

**Match an old sprite's colours** with `--palette-from old.png`: the picture's own
colours become the palette lock — all of them when it has up to 64 (pixel art), else its
32 main colours, exactly as the GUI picks them (`--palette-max N` caps it at N). With
`--art` it's a colour pull at `--palette-strength`.

### Extra crispness (`--snap-pixels`)

`--snap-pixels` runs the render through [Hugo-Dz/spritefusion-pixel-snapper](https://github.com/Hugo-Dz/spritefusion-pixel-snapper)
(bundled, built by `install.sh` — needs the Rust toolchain). It auto-detects the
true pixel grid and snaps every pixel to it, removing the faint speckle/drift AI
output has. The snapper picks its own native resolution; add `--out WxH` to force
the exact final canvas. Pair with `--palette` to then lock the snapped result to
specific colors.

---

## GUI (`pixelmon-gui`)

A local web front end for everything above.

```bash
pixelmon-gui                 # http://127.0.0.1:8190
pixelmon-gui --lan           # also reachable from other devices on your LAN
pixelmon-gui --server gpubox # render somewhere else for this run
```

**Where it renders:** 🛠 Setup › **Render server**. Left empty it uses `servers.json`'s
`"_default"`, else `local` (ComfyUI on this machine, port 8188). Type a `servers.json` name, `host`, `host:port` or URL, and **test** shows
whether it answers. A comma list (`gpubox,local`) spreads batches across several. The choice is
saved in `gui-setup.json`; `--server` or `PIXELMON_SERVER` override it for one run.

If the render server stops answering, the center pane blacks out with a notice (check now /
setup / hide) and clears by itself when it's back; a `local` ComfyUI that isn't running yet
just starts with your first render.

A ComfyUI can also answer and still be broken. Either its prompt worker died, so prompts wait and
nothing runs (**stuck**, after 20 seconds). Or its model manager broke, so every prompt fails at
once with `'NoneType' object has no attribute 'model_size'` (**broken**). The status bar calls out
both. Give the server a restart command in `servers.json` and pixelmon-gui restarts it by itself,
when it's stuck, broken, or hasn't answered for 30 seconds, at most once every 3 minutes:

```json
"_restart": {"gpubox": "ssh gpubox 'sudo systemctl restart comfyui'"}
```

Renders waiting on it ride out the restart, and one the server forgot is sent again.

**⛔ CANCEL AND ABORT** (left end of the status bar, while anything is queued or rendering) stops
everything at once: every queued job, the running render, the render server's own ComfyUI queue
and what it's sampling, and any batch still being sent.

**📖 DOCS** (top of the form) opens the [settings atlas](docs/settings-atlas.html):
every GUI control, CLI flag and ComfyUI node input with its range, default and an
example, plus a picture-by-picture walkthrough of how a render is built. The GUI also
serves it locally at `/docs/settings-atlas.html`.

**🧊 3D GUIDE** (next to it) opens the [3D field guide for 2D artists](docs/3d-guide.html):
meshes, the X/Y/Z axes, moving and modeling, modifiers and booleans, UVs and textures,
low-poly, cameras and light, and pixelmon's 3D tools in the GUI and on the command line,
with pictures throughout ([online](https://claude.ai/artifact/XpPiZXs79wUHHXJ2zTq8ou);
local copy at `/docs/3d-guide.html`).

**The form (left):**
- **Prompt:** LoRA picker (live list from the render server) with strength.
  dos-art LoRA presets add their trigger + Kind/Genre/Era caption tags for you, the
  exact prompt sent is shown under the style-guide chips, 🎲 invents a subject and
  🕘 recalls old ones. Weighting like `(castle:1.3)` works in both prompts (see the ⓘ).
- **Setup check:** a yellow box warns when settings fight each other (a style that bans
  a word in your subject, outlines too thick for a small sprite…) with one-click fixes.
- **Canvas / palette / dither:** exact output size from grouped presets — DOS screens,
  text-mode grids, squares, **retro computers** (Apple II, C64, ZX Spectrum, Amiga,
  Atari 400/800; `@` presets also set the machine's pixel shape, like C64 multicolor's 2×1)
  and **wallpapers & displays** up to 4K — pixel size (1×1, 2×1 wide pixels…), palette with
  swatches, dither method + amount, despeckle, 1-px outlines, pixel-art angles with DRAW's
  grids (pixel, square, diagonal, isometric, hex, triangle). In **art mode** the palette,
  **Palette strength** and dither tint the painting instead.
- **Seed & batch:** lock / reroll seeds, `-n` counts, steps / CFG / sampler / scheduler /
  checkpoint, and **sweeps**: the same seed across several values of one setting, side by side —
  tick the values from a list, or one click: **sweep samplers / schedulers / checkpoints / grids /
  dithers**. The **combo** sweep (grid × dither × amount) renders the picture once and applies
  every combination to it, each captioned with its settings.

**The tabs (right):**
- **Presets:** full snapshots of the form, steering, evolve, LAB and Advanced settings, each
  with a sample picture and its palette, organized in **folders** (FACTORY, TUNED FACTORY,
  USER… create/rename/delete your own). Drag cards to reorder them or onto a folder to move
  them; drag folders to reorder. Right-click a card to **rename** it, edit its note, or move it
  to the top / bottom / another folder; hover its picture for the zoomed preview. 65 factory presets ship: scenes, sprites, hardware looks
  (CGA, Tandy, C64, ZX, Game Boy, Apple II) and a **demo for every `--style`**. A preset or a
  whole folder exports/imports as a `.zip`.
- **⚗ Lab:** convert any picture into pixel art with ControlNet (shape or layout+colors),
  adjust the source first (brightness, contrast, sharpen, posterize, crop…) — the whole picture, only your 🎨 strokes, or just
  a selected part — then **✓ apply now** to stack adjustments (with undo / redo), and **edit** part of an
  image (inpainting): paint the mask with brush / line / rectangle / ellipse / polygon tools
  (undo, brush-shaped cursor) — or **🎨 paint in color** (color picker + 💧 screen picker, hard / soft
  tip, opacity) to rough in what you want, e.g. a brown cigar with an orange lit tip; **⬚ select (box, ➰ lasso or ⬠ polygon), ✥ move
  and ⤡ transform** (scale / rotate around an anchored or free pivot) what you've drawn; select by words, or load a layered `.draw` / `.ora` / `.psd`
  whose `mask` layer is the mask and `art` layer the input. Edits can keep the picture's
  colors, light & shadow, and shape, and **paint it in first** (fill the mask with the new
  thing's color — guessed from your words — so a masked eye becomes a patch instead of coming
  back as an eye); the edit box has an **avoid** field (edit-only negative) and **checkpoint / sampler / scheduler
  overrides**, and shows (and picks) which model draws the
  edit — the SDXL inpainting model or your checkpoint. **🎯 Refine** makes a result the new
  target, a docked **zoom loupe** (600 / 300 / 200 / 50 / 33 / 10 %) follows your pointer, and
  **use as thumbnail** picks the saved preset's picture.
- **Steering:** push renders toward reference images (IPAdapter), with strength, weight
  type and a start/end window. Its **🗺 depth lock** slot takes one reference (drop a file, drag
  in a render / ref / corkboard item, or press 🗺 on a ref) and shows the depth map the sampler
  will get; the run folder keeps it in `depth/`, next to `steer/`.
- **🔬 Analyze:** right-click any image anywhere → **Send to Analysis**, tick two or more and
  compare them against a ★ reference: a difference mask (color / tolerance / opacity, as in
  Kaleidotron) with the % of pixels that differ, a loupe that shows the same spot in every
  picture, R/G/B/brightness histograms with the differences shaded, the colors unique to each
  picture, and brightness / contrast / saturation stats. **Export** the ticked pictures as layers
  of a PSD, XCF, DRAW or ORA file, with ☐ **Include difference layer**, and open it in GIMP,
  Photoshop, Krita or DRAW.
- **Batch:** a list of prompts (`path | style | size | prompt`) queued in one go.
- **🎚 Sets:** for a game's sprite list when one take per row isn't enough. It's soundmon-gui's Sets,
  for pictures.
  1. Paste a list in the Batch formats (with `[Group]` lines if you like), or press
     **🎚 Make this list a Set** in the Batch tab. The form's current settings become the set's
     **theme**.
  2. Every sprite renders **N takes**, shown as thumbnails beside **NOW**, the sprite the game has
     today in the set's game folder. Look at them (keys `1`–`0`, `N`) and **PICK** one or
     **Keep old** (`P`). Flag bad ones (`F`), render more (**＋N**), render a take again from its
     seed, or edit a sprite's prompt. Optionally ✂ fit every take into the old sprite's footprint.
  3. **Export picks** copies each pick to `<game folder>/<path>.png`. Anything it replaces goes to
     `.pixelmon-backup/<date>/` first, and picks already exported are skipped.

  A set is a frozen copy of the list, its theme and your picks, kept in
  `~/pixelmon-gallery/sets/<set>/set.json`. Its takes are ordinary gallery renders tagged with the
  set and sprite (`gui/sets.py`).
- **Gallery / Queue:** every render with its settings and CLI command; hover for a
  blown-up preview, 🧬 to **evolve** variations of any result, reload or re-run a seed.
- **⚙ Advanced:** every remaining pixelmon setting (sampler, scheduler, checkpoint,
  generation resolution, pixel-pipeline internals, ControlNet/inpaint tuning, IPAdapter
  models, animation). Blank = default; the tab shows how many overrides are active.
- **📌 Corkboard:** collect images from anywhere into boards and lay them out like a mood
  board: drag an image onto another to move it, drag its corner to make it span more or
  fewer grid cells (↺ layout resets); right-click an image to move it to the top, the bottom
  or another board. Hover to preview, rename or delete boards, zip a board or send it to steering.
- **Layout:** drag the edge of the settings pane or the corkboard to make it wider or narrower
  (double-click the edge to reset); dragging near the top or bottom of a list scrolls it.
- **🛠 Setup:** where pixelmon hands your images and folders to other programs. It finds
  [DRAW](https://github.com/grymmjack/DRAW) and [Kaleidotron](https://github.com/grymmjack/kaleidotron)
  (or set their paths), and **➕ add program** lists what's installed on your OS: image editors
  and viewers (GIMP, Aseprite, Krita, Inkscape… / Photoshop, Illustrator, Paint / Preview, Pixelmator)
  and file managers (Dolphin, Files, Thunar… / Explorer / Finder). **Right-click any image** for
  *Open in DRAW / Kaleidotron / …*, and **right-click any 📂 button** for *Open folder in …*; the
  lightbox and hover preview have ✎ DRAW and *Open in…* buttons too. Open in DRAW also loads the
  image's own colors as DRAW's palette (`DRAW.run art.png --palette art.gpl`). Pick what 📂 buttons open
  by default (e.g. Kaleidotron). **3D editor** is what a model's **🧊 Blender** button opens: Blender,
  found automatically (or set its path, or another 3D program); a Blender set here also renders the
  🧊 3D views. Saved in `~/pixelmon-gallery/gui-setup.json`.
- **Housekeeping:** each tab has its own clear/reset (✕ clear queue, ↺ reset lab, ✕ clear
  steering, ✕ clear batch, ↺ reset defaults in Advanced). **🗄 backup…** in the Gallery zips
  your whole `~/pixelmon-gallery` (renders, presets, corkboards, LAB inputs) into
  `~/pixelmon-gallery/backups/pixelmon-gallery-<keyword>-<date>.zip`; **♻ restore…** puts back
  whatever is missing from a backup (never overwrites), **📂 backups** opens that folder, and
  **✕ clear gallery…** deletes every render after making a backup first (type CLEAR to confirm). **⚡** next to reset
  puts the entire app back to first-launch state (with a typed confirmation); no files are
  ever deleted by it.

Every render is kept in `~/pixelmon-gallery/gui/<job>/` with a `job.json`.
LoRA presets (trigger tags, default strength and palette) live in `gui/presets.json`.
The factory presets are built by `~/ComfyUI/.venv/bin/python gui/make_starter_presets.py`
into `~/pixelmon-gallery/gui-presets/FACTORY/`; hand-tuned preset folders ship in [`presets/`](presets/README.md)
(copied in by `install.sh`) (`--try 4 NAME` renders candidate seeds to pick from;
presets you save yourself are never touched).

---

## Full command reference (`pixelmon --help`)

The complete, self-documenting CLI — every flag, default in `[brackets]`:

```text
pixelmon — generate pixel-art sprites from a text prompt

USAGE
  pixelmon "a prompt" [options]

EXAMPLES
  pixelmon "a fierce dragon"                     best quality (the default)
  pixelmon "a fierce dragon" --art               full-res digital art, not pixels
  pixelmon "a spider" --style geometric          sharp, angular style guide
  pixelmon "a goblin" -n 8 --palette random      8 variations, random palettes
  pixelmon "a knight" --transparent --preview    transparent + zoomed preview
  pixelmon --batch "bat,skeleton,spider" -n 128  128 of each → own folders
  pixelmon --batch-file sprites.txt -n 4         a game's sprite list: 'key | prompt' rows
  pixelmon "a slime" --palette-from old.png      use an old sprite's colours
  pixelmon "a bandit" --animate "smoke from cigar"  looping animated GIF

OPTIONS
  prompt              what to draw (in quotes)
  -n, --number N      how many to make, each a different seed  [1]
  --batch "a,b,c"     round-robin subjects → a folder each (N of each)
  --batch-file FILE   a sprite list: 'key | prompt' rows (+ 'key | style | size | prompt'), files named by key
  --skip-existing     --batch/--batch-file/-n: only make the takes that aren't there yet (resume a list)
  --size N|WxH        square N, or non-square WxH e.g. 32x48  [128]
  --out N|WxH         exact final canvas size (default: --size); also with --snap-pixels
  --art               DIGITAL ART (not pixels): full-res illustration, no downscale  [1024]
  --raw               the LoRA's own full-res picture: pixel prompt + LoRA, no palette / downscale step
  --palette-strength F --art + --palette: pull colors toward the palette, 1 = exact (+ --dither)  [0.6]
  --post-sweep SPEC   render once, save every combination: 'angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1'
  --palette NAME      none / random / a name (--list-palettes)  [none]
  --palette-from IMG  lock to a reference picture's own colours (sets --palette Custom)
  --palette-max N     --palette-from: most colours to take  [all up to 64, else 32]
  --style NAMES       append proven style guide(s) — see --list-styles
  --transparent       cut out background -> transparent PNG
  --cutout            cut out the subject with BiRefNet on the render server (any background)
  --cutout-key [TOL]  --cutout: also keep what's far from the bg colour (grey-on-grey parts)  [34]
  --cutout-model FILE background-removal model (models/background_removal/)  [birefnet]
  --fit-to OLD.png    fit the cut-out into an old sprite's footprint (same size + ground line)
  --fit-scale N       --fit-to canvas = old size x N  [4 art / 1 pixel]
  --mesh IMG          make a 3D model (.glb) of the picture's subject (Hunyuan3D 2 + Blender preview)
  --turnaround SRC    rotation sprites from a picture or a model: 3D -> N views -> painted to match
  --angles N          --turnaround: views around the model  [8]
  --elev DEG          --turnaround: camera height above eye level  [0]
  --shape SPEC        --mesh/--turnaround: build it from the outline: extrude[:DEPTH[,SHARE:DEPTH]] | lathe
  --shape-grid N      --shape: cells across the outline  [64]
  --lowpoly FACES     --mesh/--turnaround: faceted low-poly model (e.g. 400 faces)
  --texture           --mesh/--turnaround: + a clean, textured model (quads, UVs, baked picture)
  --texture-size PX   --texture: texture size  [1024 up to 1500 faces, else 2048]
  --texture-elev DEG  --texture: how high the picture was seen from  [guessed: 0/30/55]
  --blender CMD       Blender for 3D  [blender / org.blender.Blender flatpak]
  --mesh-model FILE   Hunyuan3D 2 checkpoint (models/checkpoints/)  [hunyuan3d-dit-v2_fp16]
  --tile [x|y]        seamless tileable texture (+ a 3x3 _tiled_ preview)
  --dither [NAME]     dither between palette colors: bayer2/4/8/16, clustered, floyd-steinberg,
                        jarvis, stucki, burkes, sierra, sierra2, sierra-lite, atkinson  [floyd-steinberg]
  --dither-amount F   dither strength 0..1  [0.75]
  --snap-pixels       find the model's own pixel grid instead of shrinking to --out — extra crisp
  --snap-method M     unfake (model's own grid, reduced to --out) or snapper  [unfake]
  --color-match M     palette lock: oklab (perceptual) or redmean (classic)  [oklab]
  --fit-subject       crop to the subject first so it fills the frame — tiny sprites stay readable
  --despeckle N       remove stray color islands of <= N px (0 = off)  [2]
  --pixel-angles F    EXPERIMENTAL: clean pixel-art edge angles (0 off, ~1.5)  [0]
  --angle-grid G      pixel / square / diagonal / isometric / hex / triangle  [pixel]
  --pixel-size WxH    exact art pixel size: 1x1, 2x1, 2x2, 4x1 … (fixes huge snapped pixels)  [auto]
  --thin-lines N      1-px outlines: thin dark strokes up to N px (0 off, ~4)  [0]
  --fast              LCM mode: ~5x faster, slightly softer
  --seed N            lock / repeat a result (re-run a favorite)  [random]
  --steps N           refinement steps (more = slower)  [25]
  --cfg N             prompt adherence (higher = stricter)  [7]
  --lora-strength N   how strongly to pixelate  [1.0]
  --bg-tolerance N    bg color match for --transparent  [16]
  --custom-hex "..."  colors for --palette Custom
  --list-palettes     show every palette name
  --list-styles       show every style guide
  --list-servers      each render server: up?, GPU, queue, has what this command needs?
  -h, --help          show this help
  --version           print the pixelmon version

ADVANCED
  --server NAMES      render on a remote ComfyUI (alias/host/URL); comma-list = render farm across GPUs  [local]
  --no-check          don't check the server has every node / input / model first
  --dry-run           show the jobs, prompts and where files go; send nothing
  --print-graph       print the first job's ComfyUI API graph (JSON) and exit
  --smooth MODE       pre-downscale flatten: mode / median / none  [mode]
  --filter MODE       downscale: nearest (crisp) / box (soft)  [nearest]
  --preview           also save an enlarged zoomed-in PNG
  --view-scale N      enlarge factor for --preview  [8]
  --negative "..."    negative prompt (what to avoid)
  --name NAME         output filename base  [from prompt]
  --res N             SDXL generation resolution  [1024]
  --pixel-grid N      art-grid long side before the resize to --size (0 = auto)  [0]
  --snap-colors N     colour cap for --snap-pixels (0 = auto)  [0]
  --sampler NAME      ksampler sampler  [euler / lcm]
  --scheduler NAME    ksampler noise schedule (karras, exponential …)  [normal / sgm_uniform]
  --base FILE         SDXL checkpoint  [sd_xl_base_1.0]
  --lora FILE         pixel-art LoRA  [pixel-art-xl]
  --lcm-lora FILE     LCM LoRA (used with --fast)  [lcm-lora-sdxl]
  --no-lora           base model only (skip pixel LoRA)
  --steer DIR         steer toward a folder of reference images (IPAdapter)
  --steer-max N       most references taken from the folder (evenly sampled)  [16]
  --steer-weight-type T IPAdapter weight type (linear, composition …)  [style transfer]
  --steer-combine M   blend the refs: concat / average / norm average / add / subtract  [concat]
  --steer-model FILE  IPAdapter model (models/ipadapter/)  [ip-adapter-plus_sdxl_vit-h]
  --steer-clip FILE   CLIP-vision model (models/clip_vision/)  [CLIP-ViT-H-14-laion2B]
  --steer-start/--steer-end F when (fraction of steps) the refs apply  [0 / 1]
  --depth IMG         depth ControlNet: lock the camera angle + geometry of an image; the prompt sets the rest
  --depth-strength F  how strictly to follow the depth map, 0..2  [0.7]
  --depth-start/--depth-end F when (fraction of steps) the depth map applies  [0 / 0.65]
  --depth-raw         the --depth image is already a depth map (skip Depth Anything V2)
  --depth-mask-bg     cut out the foreground: all background depth goes black (no scenery)
  --depth-keep-bg     keep a plain backdrop's depth (it's blacked out automatically)
  --depth-only        make and save the processed depth map, then stop
  --depth-model FILE  depth ControlNet (models/controlnet/)  [controlnet-depth-sdxl-1.0]
  --depth-bg-model FILE background-removal model for --depth-mask-bg  [birefnet]
  --init IMG          img2img: start from an image (keeps its composition and, without --size, its shape)
  --control IMG       ControlNet: keep an image's shape, restyle it (--control-mode canny|tile)
  --control-strength F how strictly to follow the --control image, 0..2  [0.8]
  --control-end F     fraction of the steps the control applies for  [0.8]
  --control-model FILE ControlNet (models/controlnet/)  [controlnet-union-sdxl-promax]
  --canny-low/--canny-high F canny edge thresholds (lower = more edges)  [0.3 / 0.7]
  --inpaint-model FILE with --mask: an SDXL inpainting UNet (models/unet/) — paints new things far better
  --mask IMG          inpaint with --init: redraw only the mask's white area
  --mask-mode M       fill (draw something new) / blend (tweak what's there)  [fill]
  --mask-grow PX      blend margin around the mask  [6]
  --no-inpaint-crop   --mask: render the whole picture, not just the area around the mask
  --no-keep-outside   --mask: don't paste the result back onto the original
  --denoise F         with --init: how much to change, 0..1  [0.6]
  --steer-strength N  how strongly the refs influence the result  [0.7]
  --no-open           don't auto-open the result
  --show-prompt       print the exact positive/negative prompts sent to the model
  --exact-prompt      send the prompt + --negative word for word: no added prefix/suffix/styles/default negative
  --no-sprite-suffix  drop 'game sprite … solid background' (auto for 'scene background')
  --output-to DIR     move outputs into DIR (relative to cwd)
  --move-to-dirs      put a run in its own ./<prompt>/ folder
  --create-dirs       create output folders if missing
  --no-subdirs        with --batch/--output-to: dump all into one flat folder

ANIMATION  (EXPERIMENTAL — looping portrait gestures → GIF; best for glow/light. for crisp sprites, hand-animate a static render)
  --animate GESTURE   preset (blink/talk/glow/smoke/breathe) or free text
  --anim-region WHAT  what to auto-mask, e.g. "the cigar" (CLIPSeg)  [guessed]
  --anim-frames N     2 = toggle (blink); 3-5 = motion (smoke)  [preset]
  --anim-fps N        loop SPEED (frames/sec during the gesture)  [6]
  --anim-hold S       seconds the resting pose lingers  [1.2]
  --anim-denoise D    region change strength 0.3 subtle .. 0.9 strong  [0.65]
  --anim-loop MODE    pingpong / cycle / once-return  [preset]
  --anim-box L,T,R,B  manual mask box (fractions) if auto-mask misses
  --anim-res N        base/inpaint gen resolution (detail)  [768]

OUTPUT
  ~/ComfyUI/output/pixelmon/
  true-size _sprite_ PNG  (add --preview for an enlarged _preview_ PNG)

  TIP  explore with --fast, then re-run the --seed you liked (without --fast) for the full-quality keeper.
```

---

## How it works

```
prompt ──► ComfyUI API
            CheckpointLoader (SDXL base)
              └─ LoraLoader (Pixel Art XL / ega-art / dosart-vga …)  [─ LoraLoader (LCM) if --fast]
                   └─ [IPAdapter --steer] ─ [ControlNet --control] ─ [depth ControlNet --depth] ─ KSampler ─► VAEDecode ─► PixelArtPalette ─► SaveImage
                                                                        ▲
                                                   [--init img2img / --mask inpaint]
```

The custom **`PixelArtPalette`** node (`custom_nodes/pixelart_palette/`) is the
finishing pass that makes output a *true* sprite. Everything in it runs after the model
is done, so none of it changes what gets drawn:

1. **smooth** (mode/median filter) — flattens soft gradients so backgrounds don't
   shatter into speckle when quantized.
2. **downscale**, grid-aware, to the art grid (output size ÷ `--pixel-size`) — this
   is what keeps edges **crisp** instead of soft. (`--filter box` gives a soft look.)
3. **palette** lock — perceptual (redmean) color matching to a palette, optionally with
   **dithering** (Bayer, clustered or error diffusion) done in palette space.
4. **despeckle** — stray single-color islands of ≤ N px take their surroundings' color.
5. **1-px outlines** (`--thin-lines`) — strokes of the darkest color are skeletonized to
   single-pixel lines; wider dark areas are kept.
6. **pixel-art angles** (`--pixel-angles`, `--angle-grid`) — region edges are redrawn
   along clean integer-ratio angles (1:1, 2:1, 3:1 …, as in DRAW).
7. **transparent** (optional) — border flood-fill removes the background (hard 1-bit alpha).
8. **final resize** — each art pixel is scaled up to its `--pixel-size` (e.g. 2×1 wide
   pixels) to land exactly on `--out`.

The [settings atlas](docs/settings-atlas.html) shows each of these layers with real renders.

Palettes **auto-load** from `custom_nodes/pixelart_palette/gpl/*.GPL` — 55 are
bundled (PICO-8, DAWNBRINGER, ENDESGA, NES, Game Boy, C=64, VGA, QUAKE, and more).
To add one, drop a GIMP `.GPL` file (export from GIMP/Aseprite, or grab one from
[lospec.com](https://lospec.com/palette-list)) into that folder and restart — the
filename minus its ` (N)` count becomes the palette name. You can also add hex
lists directly to the `MY_PALETTES` dict in `palettes.py`.

---

## Lessons learned (the gotchas)

These cost real time; they're why the setup looks the way it does. **Items 1, 2,
4, 5 are AMD/ROCm-specific** — `install.sh`/`launch-comfyui.sh` handle them
automatically on AMD and skip them on NVIDIA. Item 3 applies to every vendor.

1. **`render` group, not just `video`.** ROCm talks to the GPU through `/dev/kfd`,
   which is owned by the `render` group. Without membership, `torch.cuda.device_count()`
   is `0` and `rocminfo` says *"not a member of render group"*. Fix:
   `sudo usermod -aG render $USER` then **log out/in**.
2. **`HSA_OVERRIDE_GFX_VERSION=10.3.0`.** The RX 6600 is **gfx1032**, which ROCm
   doesn't officially support — only gfx1030. This *runtime* env var makes it
   masquerade as gfx1030. (`PYTORCH_ROCM_ARCH` is build-time and does nothing here.)
3. **The model is everything.** SD 1.5 / SDXL-base downscaled = crushed photo, not
   pixel art. The **Pixel Art XL LoRA on SDXL** is what produces genuine sprites.
4. **SDXL can crash an 8 GB card.** A full-load run hung the `amdgpu` driver and
   green-screened the machine (a known RDNA2 + ROCm risk under sustained load).
   Fix/cushion: launch ComfyUI with **`--lowvram`** (streams the model from the
   62 GB of system RAM). Slightly slower, much safer. Remove it once you trust it.
5. **Enable persistent logs** so the *next* GPU hang is diagnosable:
   `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`,
   then on a crash: `journalctl -k -b -1 | grep -i amdgpu`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Wrong vendor detected | force it: `PIXELMON_GPU=nvidia\|amd\|cpu ./install.sh` (and same env for `launch-comfyui.sh`) |
| **(AMD)** `torch.cuda.is_available()` False / 0 devices | not in `render` group, or missing `HSA_OVERRIDE_GFX_VERSION=10.3.0` |
| **(AMD)** `rocminfo`: *"not a member of render group"* | `sudo usermod -aG render $USER`, log out/in |
| **(AMD)** machine hangs / green-screens during generation | run with `--lowvram` (auto on AMD in `launch-comfyui.sh`); prefer `--fast` |
| **(NVIDIA)** `torch.cuda.is_available()` False | NVIDIA driver not installed/loaded (`nvidia-smi` must work), or you got a CPU torch wheel — re-run `install.sh` |
| **(NVIDIA)** torch errors about unsupported arch (old card) | pin an older CUDA wheel: `cu124` → `cu121` on the torch line in `install.sh` |
| Output looks like a blurry photo, not pixels | you're not using the Pixel Art XL LoRA (`--no-lora` is on, or base model only) |
| First generation is slow | normal — it loads the 6.9 GB SDXL model; later runs reuse it |
| `Illegal instruction (core dumped)` when ComfyUI starts (older CPU) | a prebuilt wheel uses CPU instructions (e.g. AVX2) your CPU lacks. Find the culprit (`python -c "import kornia"` etc.) and remove it if optional — e.g. `pip uninstall -y kornia kornia_rs` (only used by ComfyUI post-processing nodes pixelmon doesn't need) |
| Server dies when you log out / close SSH | `loginctl enable-linger "$USER"` once, then run ComfyUI under `tmux` (see [Keep it running headless](#running-on-an-nvidia-box)) |
| `--server` job runs but no file appears locally | the result is fetched to `~/ComfyUI/output/pixelmon/` on the *client*; make sure that path is writable, or pass `--output-to DIR` |
| `… can't render this: PixelArtPalette doesn't know inputs …` | that box runs an older `pixelart_palette` node (or its ComfyUI wasn't restarted after an update): update `custom_nodes/` there and restart its ComfyUI. `pixelmon --list-servers` shows every box at once |
| `… isn't installed (LoraLoader)` / `(ControlNetLoader)` | the model isn't on that render server: run the `./download-models.sh --…` line printed under it on that box |
| `[pixelmon] ComfyUI exited while starting` | the end of `~/ComfyUI/server.log` is printed with it (a missing venv, the port taken, a bad flag) |
| a stopped run keeps the GPU busy | it shouldn't any more: Ctrl-C / Cancel clears this run's prompts. Prompts from runs killed with `kill -9` stay — clear them from the GUI's ⛔ or ComfyUI's queue |

---

## Repo layout

```
pixelmon/
├── README.md
├── README-WINDOWS-NVIDIA.md   Windows + NVIDIA (WSL2) setup guide
├── install.sh                 reproducible setup — auto-detects NVIDIA/AMD/CPU (venv + torch + links)
├── download-models.sh         fetch SDXL + Pixel Art XL + LCM + EGA-style LoRA (HF + Civitai)
├── pixelmon.py                the CLI brains (talks to ComfyUI's API); `pixelmon --version`
├── animate.py                 experimental --animate engine (region inpaint + CLIPSeg auto-mask → GIF)
├── colors.py                  a picture's colours (--palette-from, and the GUI's palette pick / .gpl export)
├── manifest.py                sprite lists (--batch-file, and the GUI's Sets tab)
├── tools/selfcheck.py         offline invariants (no server): run after touching pixelmon.py / animate.py / bin/
├── turnaround.py              --mesh / --turnaround: picture → 3D model (Hunyuan3D 2) → N angles → painted sprites
├── blender/turnaround.py      the Blender side: renders a model from every angle (+ depth maps), low-poly, .glb export
├── shapes.py                  --shape: a model built from the picture's outline (extrude / lathe), glTF writer, preview
├── blender/texture.py         --texture: retopology, UV map, the picture baked into a texture
├── blender/open_model.py      the GUI's 🧊 Blender button: imports a model into Blender, framed
├── styles.json                --style guide snippets (edit / add your own)
├── servers.example.json       template for --server aliases (copy to servers.json, gitignored)
├── bin/pixelmon               wrapper: ensures the server is up, then runs pixelmon.py
├── bin/pixelmon-gui           launches the web GUI (gui/server.py)
├── gui/                       pixelmon-gui: server.py (stdlib), index.html, sets.py (Sets tab), presets.json (LoRA presets),
│                              make_starter_presets.py (builds the factory presets), presets_sync.py (tuned
│                              presets ↔ repo), layered.py (reads/writes layered PSD · XCF · ORA · DRAW files)
├── native/                    build_dataset.py: native-pixel EGA training sets from the LoRA dataset (experimental, not used by pixelmon yet)
├── docs/                      settings-atlas.html (every setting + how a render is built) and its pipeline/ images;
│                              3d-guide.html (3D for 2D artists) and its 3d-guide/ images
├── launch-comfyui.sh          ComfyUI launcher — auto-detects vendor (AMD: gfx override + render group + lowvram)
├── custom_nodes/
│   └── pixelart_palette/       the finishing node (smooth→downscale→palette/dither→cleanup→outlines→angles→transparent)
│       ├── nodes.py
│       ├── palettes.py         palette registry — add your own here
│       ├── thin_lines.py       1-px outlines (Zhang–Suen thinning)
│       ├── pixel_angles.py     pixel-art angle snapping + grids
│       ├── seamless.py         --tile: circular-padding UNet / VAE decode + 3x3 seam preview
│       └── web/                ComfyUI extension: loads the running pixelmon job onto the canvas
└── examples/                   sample sprites + the full style gallery (examples/README.md)
```

---

## Credits & licenses

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) — the engine (GPL-3.0)
- [SDXL base 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) — Stability AI (CreativeML Open RAIL++-M)
- [Pixel Art XL](https://huggingface.co/nerijs/pixel-art-xl) — nerijs
- [LCM-LoRA SDXL](https://huggingface.co/latent-consistency/lcm-lora-sdxl) — Latent Consistency
- [EGA retro style SDXL](https://civitai.com/models/290771/ega-retro-style-sdxl) — the EGA/Wasteland look (commercial use + derivatives OK, no credit required)
- [CLIPSeg](https://huggingface.co/CIDAS/clipseg-rd64-refined) — CIDAS, text-prompted segmentation for `--animate` auto-masking
- [spritefusion-pixel-snapper](https://github.com/Hugo-Dz/spritefusion-pixel-snapper) — Hugo Duprez (MIT), used by `--snap-pixels`

The code in this repo (the CLI, wrapper, launcher, and custom node) is released
under the MIT License — see `LICENSE`.

---

## Style gallery

Every `--style` rendered against the **same 12 subjects** (`castle, knight, dragon,
spaceship, starbase, alien, cowboy, bandit, banker, thug, treasure, halloween`) — and
crucially, with the **same seed per subject across every style**, so the *only* variable
between sheets is the style itself. `castle` in `dark` and `castle` in `mario` share an
identical seed/composition; the difference you see is purely the style. Full details +
the regenerate scripts: [`examples/`](examples/README.md).

> Hardware-palette looks (Game Boy, EGA, NES) are *exact* color sets — a text prompt
> can only nudge hue, so those styles **lock to a real palette** via `--palette`; the
> prompt just sets the vibe. Prompt-driven styles keep the model's own colors.

### Palette-locked (true hardware palettes)

| | |
|---|---|
| **8bit** — NES palette | **gameboy** — Game Boy DMG 4-shade green |
| ![8bit](examples/styles/8bit.png) | ![gameboy](examples/styles/gameboy.png) |
| **ega** — 16-color IBM EGA | **wasteland** — EGA, 1988 Wasteland CRPG |
| ![ega](examples/styles/ega.png) | ![wasteland](examples/styles/wasteland.png) |

### LoRA-driven

| | |
|---|---|
| **dosega** — dosegafx EGA LoRA + EGA palette | **r3tr0** — retro-game-art LoRA |
| ![dosega](examples/styles/dosega.png) | ![r3tr0](examples/styles/r3tr0.png) |
| **pixelartredmond** — PixelArtRedmond LoRA | |
| ![pixelartredmond](examples/styles/pixelartredmond.png) | |

### Prompt-driven (default Pixel Art XL LoRA)

| | |
|---|---|
| **clean** — crisp, flat shading | **detailed** — intricate shading, rich color |
| ![clean](examples/styles/clean.png) | ![detailed](examples/styles/detailed.png) |
| **minimal** — few colors, open space | **16bit** — vibrant SNES-era sprite |
| ![minimal](examples/styles/minimal.png) | ![16bit](examples/styles/16bit.png) |
| **geometric** — sharp angular forms | **outline** — bold outline, strong silhouette |
| ![geometric](examples/styles/geometric.png) | ![outline](examples/styles/outline.png) |
| **cute** — chibi mascot | **dark** — gritty, muted, ominous |
| ![cute](examples/styles/cute.png) | ![dark](examples/styles/dark.png) |
| **horror** — creepy, grotesque | **hyperlight** — Hyper Light Drifter neon |
| ![horror](examples/styles/horror.png) | ![hyperlight](examples/styles/hyperlight.png) |
| **deadcells** — fluid, glowing rim light | **blasphemous** — gothic, ornate, dark |
| ![deadcells](examples/styles/deadcells.png) | ![blasphemous](examples/styles/blasphemous.png) |
| **owlboy** — polished hi-bit, colorful | **stardew** — cozy farm-RPG |
| ![owlboy](examples/styles/owlboy.png) | ![stardew](examples/styles/stardew.png) |
| **dosrpg** — MS-DOS CRPG portrait, VGA | **darkest** — Darkest Dungeon ink gothic |
| ![dosrpg](examples/styles/dosrpg.png) | ![darkest](examples/styles/darkest.png) |
| **undertale** — simple, white-outline | **mario** — bright Nintendo platformer |
| ![undertale](examples/styles/undertale.png) | ![mario](examples/styles/mario.png) |
| **zelda** — SNES top-down action-RPG | **hollowknight** — inky monochrome gothic |
| ![zelda](examples/styles/zelda.png) | ![hollowknight](examples/styles/hollowknight.png) |
| **metroid** — moody sci-fi, armored | **finalfantasy** — 16-bit JRPG sprite |
| ![metroid](examples/styles/metroid.png) | ![finalfantasy](examples/styles/finalfantasy.png) |
| **pokemon** — cute creature, bold outline | |
| ![pokemon](examples/styles/pokemon.png) | |

*Combine styles with modifiers like `solo` (one centered subject), `item` (object icon),
or `portrait` (head-and-shoulders) — e.g. `--style "dark,solo"`. Full list: `pixelmon --list-styles`.*
