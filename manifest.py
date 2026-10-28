"""Sprite lists ("manifests"): one row per sprite, read by `pixelmon --batch-file` and pixelmon-gui's Sets tab.

One parser on purpose, so a list that works in the GUI works on the command line and the other way round. The
formats are the GUI Batch tab's (parseBatch in gui/index.html), plus [Group] lines:

    key | prompt                         a prompt named by its key (keys may hold folders: enemies/bat)
    key | style | size | prompt          its own --style and --size (either may be empty)
    key | style-or-size | prompt         one of them
    folder | name | style | size | prompt
    a prompt on its own                  key = its slug
    [Group name]                         the rows below go in that group
    # comment                            skipped

Canonical manifests list every medium ("ansi-art/…", "pixel-art/…"): only pixel-art rows are kept.
Stdlib only.
"""
import re

KEY_RE = re.compile(r"[\w.\-/ ]{1,160}")


def key_ok(key):
    """A sprite key is a relative path inside the game folder: no leading /, no .. or empty segments."""
    key = str(key or "")
    return bool(KEY_RE.fullmatch(key)) and not key.startswith(("/", " ")) and all(
        seg.strip() and seg.strip() not in (".", "..") for seg in key.split("/"))


def slug(text, n=48):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:n] or "sprite"


def parse_manifest(text):
    """[{"key", "style", "size", "prompt", "group"}, ...] for every row; ValueError on a bad or empty list."""
    rows, seen, group = [], set(), ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            group = m.group(1).strip()
            continue
        c = [x.strip() for x in line.split("|")]
        style = size = ""
        if len(c) >= 5:
            path, style, size, prompt = f"{c[0]}/{c[1]}", c[2], c[3], " | ".join(c[4:])
        elif len(c) == 4:
            path, style, size, prompt = c
        elif len(c) == 3:
            path, prompt = c[0], c[2]
            if re.fullmatch(r"\d+(x\d+)?", c[1], re.I):
                size = c[1]
            else:
                style = c[1]
        elif len(c) == 2:
            path, prompt = c
        else:
            prompt = c[0]
            path = slug(prompt)
        mm = re.fullmatch(r"([\w-]+-art)/(.*)", path)
        if mm and mm.group(1) != "pixel-art":
            continue
        path = re.sub(r"\.(png|gif|ans)$", "", mm.group(2) if mm else path, flags=re.I).rstrip("/")
        if not path or not key_ok(path):
            raise ValueError(f"{path!r} can't be a file path — letters, digits, spaces, . - _ /")
        if path in seen:
            raise ValueError(f"{path!r} appears twice")
        if not prompt:
            raise ValueError(f"{path}: no prompt")
        seen.add(path)
        g = group or (path.split("/")[0] if "/" in path else "")
        rows.append({"key": path, "style": re.sub(r"\s+", "", style), "size": size, "prompt": prompt, "group": g})
    if not rows:
        raise ValueError("no sprites in that list — one per line: `path | style | size | prompt`, `path | prompt`, or a prompt")
    return rows
