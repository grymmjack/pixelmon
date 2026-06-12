"""Palette registry for the PixelArtPalette node.

Palettes are loaded automatically from the bundled GIMP-palette files in the
`gpl/` folder next to this file — drop a new `.GPL` in there (and restart
ComfyUI) and it shows up as a selectable palette. The palette NAME is the
filename with any trailing " (N)" count stripped, e.g. `PICO-8 (16).GPL`
becomes "PICO-8".

You can also add ad-hoc palettes in MY_PALETTES below (a name -> list-of-hex
dict), or — for one-offs — paste hex codes into the node's `custom_hex` field.
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
GPL_DIR = os.path.join(_HERE, "gpl")


# ---------------------------------------------------------------------------
# Extra palettes you want in code (loaded on top of the .GPL files).
# Format:  "Name": ["RRGGBB", "RRGGBB", ...]
# ---------------------------------------------------------------------------
MY_PALETTES = {
    # "Slime Cave": ["1b1b2f", "3a8c4f", "8be04e", "e0f8cf", "ff4d6d"],
}


def hex_to_rgb(h):
    """'#RRGGBB' or 'RRGGBB' -> (r, g, b) ints. Raises ValueError if malformed."""
    h = h.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad hex color: {h!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _parse_gpl(path):
    """Parse a GIMP .GPL palette -> list of 'RRGGBB' hex strings.

    Handles both common variants (4th column is a hex code or a color name) by
    only trusting the first three whitespace-separated ints as R G B.
    """
    colors = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith(("gimp palette", "name:", "columns:")):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                r, g, b = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            colors.append(f"{r:02X}{g:02X}{b:02X}")
    return colors


def _load_gpl_dir(directory):
    """Load every *.GPL in `directory` -> {name: [hex, ...]}, sorted by name."""
    out = {}
    if not os.path.isdir(directory):
        return out
    for fn in sorted(os.listdir(directory)):
        if not fn.lower().endswith(".gpl"):
            continue
        # "PICO-8 (16).GPL" -> "PICO-8"
        name = re.sub(r"\s*\(\d+\)\s*$", "", os.path.splitext(fn)[0]).strip()
        cols = _parse_gpl(os.path.join(directory, fn))
        if cols:
            out[name] = cols
    return dict(sorted(out.items()))


# Bundled .GPL palettes first, then any MY_PALETTES (which can override by name).
ALL_PALETTES = {**_load_gpl_dir(GPL_DIR), **MY_PALETTES}


def parse_palette(name, custom_hex):
    """Return a list of (r,g,b) tuples for the chosen palette.

    If name == 'Custom', parse custom_hex (hex codes separated by commas,
    spaces, or newlines). Otherwise look the name up in ALL_PALETTES.
    """
    if name == "Custom":
        tokens = custom_hex.replace(",", " ").split()
        if not tokens:
            raise ValueError("Custom palette selected but custom_hex is empty.")
        return [hex_to_rgb(t) for t in tokens]
    return [hex_to_rgb(h) for h in ALL_PALETTES[name]]
