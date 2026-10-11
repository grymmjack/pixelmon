"""Build the native-pixel EGA training sets from the LoRA dataset (~/Pictures/dos-art/_dataset/ega16).

The LoRA copies are the original game art blown up by an exact integer factor (nearest neighbour) to ~1024px.
This undoes that: finds the factor, takes one pixel per block, checks the round trip is exact, maps every pixel
to its EGA palette index, and packs two sets:

  sprites64.npz  game sprites whose native size fits 64x64, centred on a 64x64 black canvas
  scenes.npz     full 320x200 screens (scene backgrounds)

each with  idx (N,H,W) uint8 EGA indices,  caps (N,) str captions.

usage: build_dataset.py [SRC] [OUT_DIR]
"""
import glob
import math
import os
import re
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Pictures/dos-art/_dataset/ega16")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/pixelmon-native-data")

EGA = np.array([(0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170), (170, 0, 0), (170, 0, 170), (170, 85, 0),
                (170, 170, 170), (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255), (255, 85, 85),
                (255, 85, 255), (255, 255, 85), (255, 255, 255)], dtype=np.int32)
TRIGGER = re.compile(r"^\s*dosart_ega,\s*EGA 16-color pixel art,\s*", re.I)


def block_size(a):
    """the integer upscale factor: gcd of every run length along sampled rows and columns"""
    g = 0
    for line in list(a[:: max(1, a.shape[0] // 24)]) + list(a.transpose(1, 0, 2)[:: max(1, a.shape[1] // 24)]):
        change = np.any(line[1:] != line[:-1], axis=1)
        edges = np.flatnonzero(change) + 1
        runs = np.diff(np.concatenate(([0], edges, [len(line)])))
        g = math.gcd(g, int(np.gcd.reduce(runs)))
        if g == 1:
            break
    return g


def caption(path):
    cap = TRIGGER.sub("", open(path[:-4] + ".txt", encoding="utf-8", errors="replace").read().strip())
    game = os.path.basename(path).split("__")[0].replace("-", " ")
    return f"{cap}, from {game}"


def one(path):
    try:
        a = np.asarray(Image.open(path).convert("RGB"), dtype=np.int32)
        k = block_size(a)
        if k < 1:
            return None
        h, w = a.shape[0] // k, a.shape[1] // k
        nat = a[k // 2:: k, k // 2:: k][:h, :w]
        if not np.array_equal(np.repeat(np.repeat(nat, k, 0), k, 1), a[: h * k, : w * k]):
            return None                                     # not a clean integer upscale
        d = ((nat[:, :, None, :] - EGA[None, None]) ** 2).sum(-1)
        idx = d.argmin(-1).astype(np.uint8)
        if d.min(-1).max() > 3 * 30 ** 2:                    # some pixel is far from every EGA color
            return None
        cap = caption(path)
        if "game sprite" in cap and h <= 64 and w <= 64:
            c = np.zeros((64, 64), np.uint8)
            y, x = (64 - h) // 2, (64 - w) // 2
            c[y:y + h, x:x + w] = idx
            return "sprite", c, cap
        if (w, h) == (320, 200):
            return "scene", idx, cap
        return None
    except Exception:
        return None


def main():
    files = sorted(glob.glob(os.path.join(SRC, "*.png")))
    os.makedirs(OUT, exist_ok=True)
    sets = {"sprite": ([], []), "scene": ([], [])}
    with Pool() as pool:
        for i, r in enumerate(pool.imap_unordered(one, files, chunksize=64), 1):
            if r:
                sets[r[0]][0].append(r[1])
                sets[r[0]][1].append(r[2])
            if i % 2000 == 0:
                print(f"{i}/{len(files)}  sprites {len(sets['sprite'][0])}  scenes {len(sets['scene'][0])}", flush=True)
    for kind, name in (("sprite", "sprites64.npz"), ("scene", "scenes.npz")):
        idx, caps = sets[kind]
        np.savez_compressed(os.path.join(OUT, name), idx=np.stack(idx), caps=np.array(caps))
        print(f"{name}: {len(idx)}")


if __name__ == "__main__":
    main()
