"""Generate the Notes Graph app icon (Midnight Observatory constellation).

A letter-N constellation of glowing cluster-colored nodes on a deep-space
squircle, matching the UI's visual system (src/ui/graph/colors.js).

Writes assets/icon.png (1024x1024 master) and, on macOS, assets/icon.icns.

Run: uv run --with pillow python scripts/generate_app_icon.py
"""

from __future__ import annotations

import random
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
RECT = 824  # macOS icon-grid squircle size within the 1024 canvas
RADIUS = 186  # ~22.5% corner radius (Big Sur style)
INSET = (SIZE - RECT) // 2

BG_TOP = (19, 26, 51)  # lifted midnight
BG_BOTTOM = (9, 11, 22)  # near-black space
VIOLET = (167, 139, 250)  # #a78bfa — selection/hub color
CYAN = (103, 232, 249)  # #67e8f9
AMBER = (251, 191, 36)  # #fbbf24
SLATE = (203, 213, 225)  # #cbd5e1

# Constellation: the four strokes-corners of an "N", plus two satellites.
NODES = [
    {"pos": (368, 690), "r": 22, "color": SLATE},  # A bottom-left
    {"pos": (368, 350), "r": 30, "color": VIOLET},  # B top-left (hub)
    {"pos": (656, 690), "r": 22, "color": CYAN},  # C bottom-right
    {"pos": (656, 350), "r": 22, "color": SLATE},  # D top-right
    {"pos": (272, 476), "r": 13, "color": AMBER},  # satellite of B
    {"pos": (748, 528), "r": 13, "color": (124, 141, 176)},  # satellite of D
]
EDGES = [(0, 1, 7, 120), (1, 2, 7, 120), (2, 3, 7, 120), (1, 4, 4, 70), (3, 5, 4, 70)]


def rounded_mask() -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle(
        [INSET, INSET, SIZE - INSET - 1, SIZE - INSET - 1], RADIUS, fill=255
    )
    return mask


def gradient_background() -> Image.Image:
    grad = Image.new("RGB", (1, SIZE))
    grad.putdata(
        [
            tuple(
                round(a + (b - a) * (y / (SIZE - 1))) for a, b in zip(BG_TOP, BG_BOTTOM)
            )
            for y in range(SIZE)
        ]
    )
    return grad.resize((SIZE, SIZE))


def build_master() -> Image.Image:
    art = gradient_background().convert("RGBA")

    # Soft nebula tints so the field isn't flat
    nebula = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    nd = ImageDraw.Draw(nebula)
    nd.ellipse([180, 60, 860, 520], fill=(56, 62, 110, 70))
    nd.ellipse([520, 560, 980, 920], fill=(26, 60, 78, 55))
    art = Image.alpha_composite(art, nebula.filter(ImageFilter.GaussianBlur(120)))

    # Deterministic starfield
    stars = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    rng = random.Random(113)  # issue number, naturally
    for _ in range(70):
        x = rng.uniform(INSET + 30, SIZE - INSET - 30)
        y = rng.uniform(INSET + 30, SIZE - INSET - 30)
        r = rng.uniform(1.2, 3.0)
        alpha = rng.randint(35, 130)
        sd.ellipse([x - r, y - r, x + r, y + r], fill=(226, 232, 240, alpha))
    art = Image.alpha_composite(art, stars)

    # Constellation edges beneath the nodes
    lines = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for a, b, width, alpha in EDGES:
        ld.line([NODES[a]["pos"], NODES[b]["pos"]], fill=(*VIOLET, alpha), width=width)
    art = Image.alpha_composite(art, lines)

    # Node glows (blurred), then crisp cores
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for n in NODES:
        x, y = n["pos"]
        gr = n["r"] * 2.6
        gd.ellipse([x - gr, y - gr, x + gr, y + gr], fill=(*n["color"], 95))
    art = Image.alpha_composite(art, glow.filter(ImageFilter.GaussianBlur(38)))

    dots = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for n in NODES:
        x, y = n["pos"]
        r = n["r"]
        dd.ellipse([x - r, y - r, x + r, y + r], fill=(*n["color"], 255))
        cr = r * 0.45
        dd.ellipse([x - cr, y - cr, x + cr, y + cr], fill=(244, 246, 255, 225))
    art = Image.alpha_composite(art, dots)

    # Clip to the squircle on a transparent canvas
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(art, (0, 0), rounded_mask())
    return out


def write_icns(master: Image.Image, icns_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "icon.iconset"
        iconset.mkdir()
        for pts in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = pts * scale
                suffix = "" if scale == 1 else "@2x"
                master.resize((px, px), Image.LANCZOS).save(
                    iconset / f"icon_{pts}x{pts}{suffix}.png"
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            check=True,
        )


def main() -> None:
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)
    master = build_master()
    master.save(assets / "icon.png")
    print(f"wrote {assets / 'icon.png'}")
    if sys.platform == "darwin":
        write_icns(master, assets / "icon.icns")
        print(f"wrote {assets / 'icon.icns'}")
    else:
        print("skipping .icns (iconutil requires macOS)")


if __name__ == "__main__":
    main()
