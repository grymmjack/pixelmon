---
name: create-release
description: Cut a pixelmon release — bump the version, update docs, verify (syntax, a smoke render on the rtx box, the GUI), then commit + tag + push and publish the GitHub Release with curated notes
---

# Release Skill (pixelmon)

When the user invokes this skill (e.g. "do a release", "create release", "release skill", "push and create release"), execute the steps **in order**. Do not skip steps. Do not batch steps that need user input.

pixelmon is a **Python CLI** (`pixelmon.py`) + a **stdlib web GUI** (`gui/server.py` + a single-page `gui/index.html`) + **ComfyUI custom nodes** (`custom_nodes/pixelart_palette/`). There is **no CI**: nothing builds or publishes on a tag push. A release is **a `vX.Y.Z` git tag plus a GitHub Release created with `gh release create`** (GitHub attaches the source archives automatically). The Release notes are exactly what Step 8 writes.

Repo: `grymmjack/pixelmon`. Default branch: `main`. Local clone: `~/pixelmon` (NOT `~/git/pixelmon`).

---

## Step 1 — Determine the version bump

The version lives in `pixelmon.py`:

```python
__version__ = "X.Y.Z"
```

If there is **no `__version__` yet** (the first release), add it near the top of `pixelmon.py` (after the imports), wire a `--version` flag into the argparse parser (`action="version", version=f"pixelmon {__version__}"`), and propose **`0.1.0`**.

Otherwise ask the user (unless they already said which):

> "Is this a **major**, **minor**, or **patch** release?"
> (major = breaking, minor = new features, patch = fixes / small features)

- **major** → `(X+1).0.0`  ·  **minor** → `X.(Y+1).0`  ·  **patch** → `X.Y.(Z+1)`

The project is pre-1.0: suggest **minor** for new features (a new tab, new flags, new node options) and **patch** for fixes. Breaking = a removed/renamed CLI flag, a changed preset/snapshot format that old presets can't load, or a node input rename that breaks saved ComfyUI workflows.

Show `Current: X.Y.Z → New: A.B.C` and confirm before proceeding.

---

## Step 2 — Collect changes

```bash
cd ~/pixelmon
LAST=$(git describe --tags --abbrev=0 2>/dev/null)
git log --oneline --no-merges ${LAST:+$LAST..}HEAD
```

(No tag yet → this lists the whole history; summarise it as the first release.)

Merged PRs since the last tag, if any:

```bash
gh pr list --repo grymmjack/pixelmon --state merged --limit 20 \
  --json number,title,mergedAt --jq '.[] | "PR #\(.number): \(.title)  \(.mergedAt[0:10])"'
```

Also review the **current conversation** for work done this session that isn't committed yet.

Produce a deduplicated, categorised list: **New Features · Improvements · Bug Fixes · Internal · Breaking Changes**. Keep it **user-facing**: name GUI tabs/controls and CLI flags the way a user sees them.

---

## Step 3 — Bump the version

Edit `__version__` in `pixelmon.py` to `A.B.C`. Confirm:

```bash
~/ComfyUI/.venv/bin/python pixelmon.py --version     # → pixelmon A.B.C
```

---

## Step 4 — Update the docs

Review against the Step 2 list. Update only what's stale; don't rewrite accurate sections.

- `README.md`: feature list, the pixelmon-gui section, flag tables, Repo layout. The platform READMEs (`README-LINUX-*.md`, `README-WINDOWS-NVIDIA.md`, `README-MACOS-*.md`, `README-RENDER-FARM.md`) only if install/launch steps changed.
- `docs/settings-atlas.html`: the settings reference (every GUI control / CLI flag / ComfyUI node input with range, default, example). If a setting was **added, renamed or had its range/default changed**, update its row. It is also published as an artifact (`https://claude.ai/artifact/Howqpc3cfKV7Xjt4aAbrSS`); republish that URL from the updated file, passing `url`, and attach the `docs/pipeline/*` images again through `files`.
- `examples/` style gallery: only if a style in `styles.json` was added or changed (see `examples/generate-style-demos.sh`).

---

## Step 5 — Starter presets

If `gui/make_starter_presets.py` changed since the last release (new/edited `SPECS`, `STYLE_SPECS`, `USER_SPECS`, `SEEDS`, `ADV_OF`), or a pixelmon/node change alters how existing presets render, rebuild the changed presets so their sample images match:

```bash
cd ~/pixelmon && ~/ComfyUI/.venv/bin/python gui/make_starter_presets.py <names…>    # or no names = all 65
```

They land in `~/pixelmon-gallery/gui-presets/FACTORY/` (not in the repo); the user's own presets live in the other folders there and are never touched. Look at the samples before moving on. Skip if nothing preset-related changed.

---

### Ship the user's tuned presets

Always, every release: copy the user's own preset folders (every folder except FACTORY — e.g. TUNED FACTORY) from the gallery into the repo, **with the images they use**:

```bash
cd ~/pixelmon && python3 gui/presets_sync.py export
git status --short presets/          # review what changed; it's committed with the release
```

It prints each folder's preset and image counts and flags anything it couldn't bundle (a missing steering ref or LAB input). `install.sh` installs them on new machines (`presets_sync.py install`, never overwriting).

---

## Step 6 — Verify

**Before touching the rtx box, check nobody is rendering** (the user's GUI queue, and rtx's ComfyUI queue):

```bash
curl -s 127.0.0.1:8190/api/jobs | python3 -c "import json,sys; print([j['status'] for j in json.load(sys.stdin)['jobs'] if j['status'] in ('queued','running')])"
curl -s http://192.168.1.77:8188/queue | python3 -c "import json,sys; q=json.load(sys.stdin); print(len(q['queue_running']), len(q['queue_pending']))"
```

Smoke renders queue fine behind other jobs, but **never restart rtx's ComfyUI while anything is queued or running**.

1. **Syntax**: every Python file and the GUI's script:
   ```bash
   cd ~/pixelmon
   for f in pixelmon.py animate.py gui/*.py custom_nodes/pixelart_palette/*.py; do ~/ComfyUI/.venv/bin/python -m py_compile "$f" || echo "FAIL $f"; done
   python3 -c "import re; s=open('gui/index.html').read(); open('/tmp/pm-gui.js','w').write(re.search(r'<script>(.*)</script>', s, re.S).group(1))" && node --check /tmp/pm-gui.js
   ~/ComfyUI/.venv/bin/python pixelmon.py --help >/dev/null
   ```
2. **Smoke render on rtx** (**always `--no-open`**: without it pixelmon opens the image on the user's screen):
   ```bash
   ~/ComfyUI/.venv/bin/python pixelmon.py "a stone castle on a hill" --server rtx --no-open --fast \
     --out 160x100 --palette EGA --thin-lines 3 --pixel-angles 1.25 --seed 7 --output-to /tmp/pm-smoke --create-dirs
   ```
   Read the PNG it prints. If this release touched a feature (LAB/ControlNet, inpaint, steering, Advanced flags, animation), smoke that path too.
3. **Custom node changes** (`custom_nodes/pixelart_palette/`): rtx's ComfyUI loads them from `~/pixelmon/custom_nodes` on the rtx box. That copy has local edits, so **don't `git pull` there**: `scp` the changed node files to `daw:~/pixelmon/custom_nodes/pixelart_palette/`, then (only when both queues above are empty) restart:
   ```bash
   ssh daw 'tmux kill-session -t comfy; sleep 2; tmux new-session -d -s comfy "bash -lc ~/launch-comfyui.sh"'
   ```
   Then confirm `curl -s http://192.168.1.77:8188/object_info/PixelArtPalette` answers, and re-run the smoke render.
4. **GUI**: start a throwaway server on a spare port, so the user's running `pixelmon-gui` on 8190 is untouched:
   ```bash
   (timeout 30 ~/ComfyUI/.venv/bin/python gui/server.py --port 8199 >/dev/null 2>&1 &); sleep 3
   curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:8199/api/meta          # 200
   ```
   If the UI changed, screenshot the affected tab headlessly (tabs open by hash: `/#adv`, `/#lab`, `/#presets` …):
   ```bash
   google-chrome --headless=new --disable-gpu --no-sandbox --window-size=1500,1200 \
     --virtual-time-budget=6000 --screenshot=/tmp/pm-gui.png "http://127.0.0.1:8199/#adv"
   ```

Fix any real failure before continuing.

---

## Step 7 — Commit, tag, push

**Confirm with the user before this step.** The tag and the Release are public.

Never `git add -A` blindly: `.venv-animate/` and personal files sit in the tree. Stage the release's files explicitly and check the diff:

```bash
git status --short
git diff --stat                      # only the intended files
git add pixelmon.py README.md docs/ …   # explicit paths
git commit -m "Release vA.B.C"       # end with the Co-Authored-By / Claude-Session trailers from the system reminder
git push origin main
git tag -a vA.B.C -m "pixelmon vA.B.C"
git push origin vA.B.C
```

---

## Step 8 — Publish the GitHub Release

Nothing publishes it automatically; create it with the curated Step 2 notes:

```bash
gh release create vA.B.C --repo grymmjack/pixelmon --title "pixelmon vA.B.C" --notes-file - <<'EOF'
## pixelmon vA.B.C — YYYY-MM-DD

### New Features
- …

### Improvements
- …

### Bug Fixes
- …

### Breaking Changes
- …
EOF
```

Drop empty sections. Print the same markdown in chat, and the Release URL (`gh release view vA.B.C --repo grymmjack/pixelmon --json url --jq .url`).

---

## Rules

- **Always confirm** the version bump (Step 1) and the tag push + Release (Steps 7–8). Never assume either.
- The tag and the Release are public, outward-facing actions. Treat them that way.
- **Always pass `--no-open`** to any pixelmon render.
- **Never restart the rtx ComfyUI while a render is queued or running** (check both queues in Step 6).
- The rtx box is the only render box to use (`--server rtx`); don't render on the local GPU.
- Keep the diff scoped: stage explicit paths, never `git add -A`.
- If a step produces no changes (README already accurate, no preset changes), say so and move on.
- Release-notes date = today, `YYYY-MM-DD`.
