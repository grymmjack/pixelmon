"""Palette registry for the PixelArtPalette node.

Each palette is just a list of hex colors. Add your own in MY_PALETTES at the
bottom — anything you put there shows up in the node's dropdown after you
restart ComfyUI. (For one-off palettes you don't want to keep, you can instead
paste hex codes into the node's `custom_hex` field without editing this file.)
"""

# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

PICO8 = [
    "000000", "1D2B53", "7E2553", "008751", "AB5236", "5F574F", "C2C3C7", "FFF1E8",
    "FF004D", "FFA300", "FFEC27", "00E436", "29ADFF", "83769C", "FF77A8", "FFCCAA",
]

# Sweetie-16 by GrafxKid — a modern, vibrant 16-color palette.
SWEETIE16 = [
    "1A1C2C", "5D275D", "B13E53", "EF7D57", "FFCD75", "A7F070", "38B764", "257179",
    "29366F", "3B5DC9", "41A6F6", "73EFF7", "F4F4F4", "94B0C2", "566C86", "333C57",
]

# Classic 16-color CGA/EGA palette (more useful than the 4-color mode).
CGA16 = [
    "000000", "0000AA", "00AA00", "00AAAA", "AA0000", "AA00AA", "AA5500", "AAAAAA",
    "555555", "5555FF", "55FF55", "55FFFF", "FF5555", "FF55FF", "FFFF55", "FFFFFF",
]

# CGA mode 4, palette 1 (high intensity) — the iconic cyan/magenta/white DOS look.
CGA4 = ["000000", "55FFFF", "FF55FF", "FFFFFF"]

# Curated NES hardware palette (duplicate blacks removed).
NES = [
    "7C7C7C", "0000FC", "0000BC", "4428BC", "940084", "A80020", "A81000", "881400",
    "503000", "007800", "006800", "005800", "004058", "000000",
    "BCBCBC", "0078F8", "0058F8", "6844FC", "D800CC", "E40058", "F83800", "E45C10",
    "AC7C00", "00B800", "00A800", "00A844", "008888",
    "F8F8F8", "3CBCFC", "6888FC", "9878F8", "F878F8", "F85898", "F87858", "FCA044",
    "F8B800", "B8F818", "58D854", "58F898", "00E8D8", "787878",
    "FCFCFC", "A4E4FC", "B8B8F8", "D8B8F8", "F8B8F8", "F8A4C0", "F0D0B0", "FCE0A8",
    "F8D878", "D8F878", "B8F8B8", "B8F8D8", "00FCFC", "F8D8F8",
]

# Bonus: original Game Boy DMG 4-shade green.
GAMEBOY_DMG = ["0F380F", "306230", "8BAC0F", "9BBC0F"]


# ---------------------------------------------------------------------------
# YOUR palettes — add as many as you like. Format: "Name": [list of hex].
# Example shows the structure; replace/extend with your own exact colors
# (e.g. grab a palette from https://lospec.com/palette-list and paste its hex).
# ---------------------------------------------------------------------------

MY_PALETTES = {
    # "Slime Cave": ["1b1b2f", "3a8c4f", "8be04e", "e0f8cf", "ff4d6d"],
}


# ---------------------------------------------------------------------------
# Registry assembled for the node. Order here = order in the dropdown.
# ---------------------------------------------------------------------------

ALL_PALETTES = {
    "PICO-8": PICO8,
    "Sweetie-16": SWEETIE16,
    "NES": NES,
    "CGA-16": CGA16,
    "CGA-4": CGA4,
    "Game Boy DMG": GAMEBOY_DMG,
    **MY_PALETTES,
}


def hex_to_rgb(h):
    """'#RRGGBB' or 'RRGGBB' -> (r, g, b) ints. Raises ValueError if malformed."""
    h = h.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad hex color: {h!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


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
