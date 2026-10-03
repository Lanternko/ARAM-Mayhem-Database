"""Two-pip die for browser tabs, with a circular raster search variant."""
from __future__ import annotations

from hashlib import sha256

BACKGROUND = "#101114"
FOREGROUND = "#f5c518"
PIPS = ((21, 21), (43, 43))
SEARCH_PIPS = ((23, 23), (41, 41))
PIP_RADIUS = 8


def icon_svg(*, circular: bool = False) -> str:
    pips = "".join(
        f'  <circle cx="{x}" cy="{y}" r="{PIP_RADIUS}" fill="{FOREGROUND}"/>\n'
        for x, y in (SEARCH_PIPS if circular else PIPS)
    )
    face = (
        f'  <circle cx="32" cy="32" r="32" fill="{BACKGROUND}"/>\n'
        if circular else
        f'  <rect x="3" y="3" width="58" height="58" rx="12" fill="{BACKGROUND}"/>\n'
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        + face
        + pips + '</svg>\n'
    )


def icon_version() -> str:
    return sha256((icon_svg() + icon_svg(circular=True)).encode()).hexdigest()[:12]


def icon_image(size: int, *, circular: bool = False):
    """Supersample a padded die or full circular face with gold pips."""
    from PIL import Image, ImageDraw

    scale = max(4, (size * 4 + 63) // 64)
    side = 64 * scale
    image = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(image)
    if circular:
        draw.ellipse((0, 0, side, side), fill=BACKGROUND)
    else:
        draw.rounded_rectangle((3 * scale, 3 * scale, 61 * scale, 61 * scale),
                               radius=12 * scale, fill=BACKGROUND)
    for x, y in (SEARCH_PIPS if circular else PIPS):
        draw.ellipse(((x - PIP_RADIUS) * scale, (y - PIP_RADIUS) * scale,
                      (x + PIP_RADIUS) * scale, (y + PIP_RADIUS) * scale),
                     fill=FOREGROUND)
    return image.resize((size, size), Image.Resampling.LANCZOS)
