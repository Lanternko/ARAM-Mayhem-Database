"""Approved two-pip die geometry shared by SVG and raster exports."""
from __future__ import annotations

from hashlib import sha256

BACKGROUND = "#101114"
FOREGROUND = "#f5c518"
PIPS = ((21, 21), (43, 43))
PIP_RADIUS = 8


def icon_svg() -> str:
    pips = "".join(
        f'  <circle cx="{x}" cy="{y}" r="{PIP_RADIUS}" fill="{FOREGROUND}"/>\n'
        for x, y in PIPS
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        f'  <rect x="3" y="3" width="58" height="58" rx="12" fill="{BACKGROUND}"/>\n'
        + pips + '</svg>\n'
    )


def icon_version() -> str:
    return sha256(icon_svg().encode()).hexdigest()[:12]


def icon_image(size: int):
    """Supersample the dark die with transparent padding and two gold pips."""
    from PIL import Image, ImageDraw

    scale = max(4, (size * 4 + 63) // 64)
    side = 64 * scale
    image = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3 * scale, 3 * scale, 61 * scale, 61 * scale),
                           radius=12 * scale, fill=BACKGROUND)
    for x, y in PIPS:
        draw.ellipse(((x - PIP_RADIUS) * scale, (y - PIP_RADIUS) * scale,
                      (x + PIP_RADIUS) * scale, (y + PIP_RADIUS) * scale),
                     fill=FOREGROUND)
    return image.resize((size, size), Image.Resampling.LANCZOS)
