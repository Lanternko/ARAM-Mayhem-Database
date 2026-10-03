"""Circular two-pip brand geometry shared by SVG and raster exports."""
from __future__ import annotations

from hashlib import sha256

BACKGROUND = "#101114"
FOREGROUND = "#f5c518"
PIPS = ((23, 23), (41, 41))
PIP_RADIUS = 8


def icon_svg() -> str:
    pips = "".join(
        f'  <circle cx="{x}" cy="{y}" r="{PIP_RADIUS}" fill="{FOREGROUND}"/>\n'
        for x, y in PIPS
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        f'  <circle cx="32" cy="32" r="32" fill="{BACKGROUND}"/>\n'
        + pips + '</svg>\n'
    )


def icon_version() -> str:
    return sha256(icon_svg().encode()).hexdigest()[:12]


def icon_image(size: int):
    """Supersample the full circular dark face and two inset gold pips."""
    from PIL import Image, ImageDraw

    scale = max(4, (size * 4 + 63) // 64)
    side = 64 * scale
    image = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, side, side), fill=BACKGROUND)
    for x, y in PIPS:
        draw.ellipse(((x - PIP_RADIUS) * scale, (y - PIP_RADIUS) * scale,
                      (x + PIP_RADIUS) * scale, (y + PIP_RADIUS) * scale),
                     fill=FOREGROUND)
    return image.resize((size, size), Image.Resampling.LANCZOS)
