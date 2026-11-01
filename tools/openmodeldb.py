#!/usr/bin/env python3
"""OpenModelDB (https://openmodeldb.info) for download-models.sh --openmodeldb.

  openmodeldb.py pick ID     one tab-separated line: url, file name, size, sha256, what it's for — the
                             PyTorch file (safetensors before pth) at an address curl can fetch directly
  openmodeldb.py pick TAG    (or list TAG) the models with that tag (jpeg, photo, anime, pixel-art …); exit 3

ComfyUI loads these through spandrel; architectures it can't load (or that need spandrel_extra_arches)
are refused with the reason, so a download never ends in a model that only fails at render time."""
import json
import re
import sys
import urllib.request

API = "https://openmodeldb.info/api/v1/models.json"
# spandrel 0.4 (ComfyUI's loader) — OpenModelDB architecture ids it opens
SPANDREL = {"esrgan", "esrgan+", "esrgan-2c2", "compact", "span", "swinir", "swin2sr", "hat", "dat", "rgt", "realplksr",
            "plksr", "rcan", "omnisr", "atd", "craft", "dctlsa", "ditn", "drct", "grl", "safmn", "seemore", "scunet",
            "fbcnn", "real-cugan", "cugan", "mosr", "moesr", "sr-res-net", "srresnet", "nafnet", "swiftsrgan",
            "aurasr", "ipt", "dnc-nn", "dncnn", "drunet", "uformer", "kbnet", "mmrealsr", "fdat", "lmlt"}
EXTRA = {"srformer", "codeformer", "restoreformer", "mat", "ddcolor", "adacode", "femasr", "m3snet", "mirnet2",
         "mprnet", "restormer"}
# hosts that answer a plain curl with the file (Google Drive, Mega, pCloud … want a browser)
DIRECT = re.compile(r"^https://(github\.com/.+/releases/download/|raw\.githubusercontent\.com/|github\.com/.+/raw/|"
                    r"huggingface\.co/|objectstorage\.[\w.-]+\.oraclecloud\.com/)")


def load():
    req = urllib.request.Request(API, headers={"User-Agent": "pixelmon download-models.sh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def mb(n):
    return f"{n / 1e6:.0f} MB" if n else "? MB"


def listing(models, tag):
    rows = sorted((k, v) for k, v in models.items() if tag in (v.get("tags") or []))
    if not rows:
        return False
    print(f"{len(rows)} OpenModelDB models tagged {tag!r}  (✓ = downloads here; get one with --openmodeldb ID)")
    for k, v in rows:
        ok = (v.get("architecture") in SPANDREL) and any(DIRECT.match(u) for r in v.get("resources") or []
                                                          if r.get("platform") == "pytorch" for u in r.get("urls") or [])
        print(f"  {'✓' if ok else ' '} {k:46} {v.get('architecture', '?'):10} {str(v.get('license') or '?'):16}"
              f" {', '.join(t for t in v.get('tags') or [] if t != tag)[:60]}")
    print("  (no ✓: an architecture ComfyUI can't load, or a Google Drive / Mega link — fetch it from the model's\n"
          "   page in a browser, then drop the file into models/upscale_models/)")
    return True


def pick(models, want):
    key = next((k for k in models if k.lower() == want.lower()), None)
    if key is None:
        if listing(models, want.lower()):
            sys.exit(3)
        close = [k for k in models if want.lower().replace("_", "-") in k.lower()][:8]
        sys.exit(f"  ✗ no OpenModelDB model {want!r}" + (f" — did you mean: {', '.join(close)}" if close else "")
                 + "\n    (the ID is the end of its page's address: https://openmodeldb.info/models/ID)")
    v, page = models[key], f"https://openmodeldb.info/models/{key}"
    arch = v.get("architecture", "?")
    if arch in EXTRA:
        sys.exit(f"  ✗ {key}: {arch} needs spandrel_extra_arches on the render server (pip install it in ComfyUI's "
                 f"venv), then fetch it from {page}")
    if arch not in SPANDREL:
        sys.exit(f"  ✗ {key}: ComfyUI can't load the {arch} architecture ({page})")
    res = [r for r in v.get("resources") or [] if r.get("platform") == "pytorch"]
    res.sort(key=lambda r: r.get("type") != "safetensors")
    for r in res:
        for url in r.get("urls") or []:
            url = url.replace("/blob/", "/resolve/") if "huggingface.co/" in url else url
            if DIRECT.match(url):
                ext = "safetensors" if r.get("type") == "safetensors" else "pth"
                why = f"{v.get('name', key)}: {arch}, {v.get('scale', '?')}x, {v.get('license') or '?'}"
                print("\t".join([url, f"{key}.{ext}", mb(r.get("size")), r.get("sha256") or "", why]))
                return
    hosts = sorted({re.sub(r"^https?://([^/]+).*", r"\1", u) for r in res for u in r.get("urls") or []})
    sys.exit(f"  ✗ {key}: no direct download (it's on {', '.join(hosts) or 'nowhere'})."
             f"\n    get it in a browser from {page} and save it as models/upscale_models/{key}.pth (or .safetensors)")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("pick", "list"):
        sys.exit(__doc__)
    try:
        models = load()
    except Exception as e:
        sys.exit(f"  ✗ couldn't reach OpenModelDB ({e})")
    if sys.argv[1] == "list":
        if not listing(models, sys.argv[2].lower()):
            sys.exit(f"  ✗ no models tagged {sys.argv[2]!r}")
        sys.exit(3)
    pick(models, sys.argv[2])


if __name__ == "__main__":
    main()
