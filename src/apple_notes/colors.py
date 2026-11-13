"""Color name/RGB mapping for Apple Notes text colors and highlights.

Apple Notes stores background highlights as a palette index in protobuf
field 14. For rare cases of foreground RGB color (field 10), bbpb decodes
float32 values as uint32 (IEEE 754 bit patterns).

This module provides bidirectional mapping between named colors, RGB values,
and highlight palette indices, plus conversion utilities for the uint32↔float32
encoding.
"""

import struct

# Named color → (R, G, B) as float32 values (0.0-1.0).
# These are the Apple Notes system colors, captured from real note data.
COLOR_MAP: dict[str, tuple[float, float, float]] = {
    "red": (1.0, 0.2314, 0.1882),
    "orange": (1.0, 0.5843, 0.0),
    "yellow": (1.0, 0.8, 0.0),
    "green": (0.2039, 0.7804, 0.349),
    "teal": (0.3529, 0.7843, 0.9804),
    "blue": (0.0, 0.4784, 1.0),
    "indigo": (0.3451, 0.3373, 0.8392),
    "purple": (0.6863, 0.3216, 0.8706),
    "pink": (1.0, 0.1765, 0.3333),
}

# Apple Notes highlight palette: color name → field 14 palette index.
# Only indices 1-5 are valid. Indices 6-9 render incorrectly and crash
# Apple Notes when the user opens the formatting (Aa) menu.
HIGHLIGHT_INDEX_MAP: dict[str, int] = {
    "purple": 1,
    "pink": 2,
    "orange": 3,
    "green": 4,
    "blue": 5,
}

# Valid color names for the {color:} syntax (highlight colors only).
VALID_COLOR_NAMES: set[str] = set(HIGHLIGHT_INDEX_MAP.keys())

# Tolerance for matching float RGB values to named colors.
# Apple Notes colors are well-separated, so 0.1 is sufficient.
_COLOR_MATCH_TOLERANCE = 0.1


def color_name_to_highlight_index(name: str) -> int | None:
    """Look up the highlight palette index for a named color.

    Args:
        name: Color name (case-insensitive). Valid: purple, pink, orange, green, blue

    Returns:
        Palette index (1-5) for field 14, or None if name not a valid highlight color
    """
    return HIGHLIGHT_INDEX_MAP.get(name.lower())


def color_name_to_rgb(name: str) -> tuple[float, float, float] | None:
    """Look up RGB float values for a named color.

    Args:
        name: Color name (case-insensitive), e.g. "red", "Blue"

    Returns:
        (R, G, B) floats 0.0-1.0, or None if name not recognized
    """
    return COLOR_MAP.get(name.lower())


def rgb_to_color_name(
    r: float, g: float, b: float, highlight_only: bool = False
) -> str | None:
    """Find the nearest named color for RGB float values.

    Args:
        r, g, b: RGB values as floats (0.0-1.0)
        highlight_only: If True, only match writable highlight colors
            (purple, pink, orange, green, blue). Non-highlight colors
            like red/yellow/teal return None so callers can fall back
            to hex, avoiding round-trip failures with the write path.

    Returns:
        Color name string, or None if no close match
    """
    best_name = None
    best_dist = float("inf")

    candidates = (
        {name: COLOR_MAP[name] for name in VALID_COLOR_NAMES}
        if highlight_only
        else COLOR_MAP
    )
    for name, (cr, cg, cb) in candidates.items():
        dist = abs(r - cr) + abs(g - cg) + abs(b - cb)
        if dist < best_dist:
            best_dist = dist
            best_name = name

    if best_dist <= _COLOR_MATCH_TOLERANCE:
        return best_name
    return None


def rgb_ints_to_floats(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert bbpb uint32 values to float32 RGB.

    bbpb decodes protobuf fixed32 fields as Python ints representing
    the IEEE 754 bit pattern of the original float32 value.

    Args:
        r, g, b: uint32 bit patterns from bbpb

    Returns:
        (R, G, B) as float32 values (0.0-1.0)
    """
    return (
        struct.unpack("f", struct.pack("I", r))[0],
        struct.unpack("f", struct.pack("I", g))[0],
        struct.unpack("f", struct.pack("I", b))[0],
    )


def rgb_floats_to_ints(r: float, g: float, b: float) -> tuple[int, int, int]:
    """Convert float32 RGB values to bbpb uint32 bit patterns.

    This is the inverse of rgb_ints_to_floats — converts float32 values
    back to the uint32 representation that bbpb expects for fixed32 fields.

    Args:
        r, g, b: RGB values as floats (0.0-1.0)

    Returns:
        (R, G, B) as uint32 IEEE 754 bit patterns
    """
    return (
        struct.unpack("I", struct.pack("f", r))[0],
        struct.unpack("I", struct.pack("f", g))[0],
        struct.unpack("I", struct.pack("f", b))[0],
    )


def hex_to_rgb_floats(hex_str: str) -> tuple[float, float, float] | None:
    """Convert a hex color string to RGB float values.

    Args:
        hex_str: Hex color string like "#FF3B30" or "FF3B30" (case-insensitive)

    Returns:
        (R, G, B) floats 0.0-1.0, or None if format is invalid
    """
    s = hex_str.lstrip("#")
    if len(s) != 6:
        return None
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
    except ValueError:
        return None
    return (r, g, b)


def rgb_floats_to_hex(r: float, g: float, b: float) -> str:
    """Convert float32 RGB values to hex color string.

    Args:
        r, g, b: RGB values as floats (0.0-1.0)

    Returns:
        Hex color string like "#FF3B30"
    """
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"
