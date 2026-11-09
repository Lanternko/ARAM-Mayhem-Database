"""Two-pip die for browser tabs, with a circular raster search variant."""
from __future__ import annotations

from hashlib import sha256

# Vertical magenta → deep-berry face with white pips.
GRADIENT_TOP = "#ec5a8c"
GRADIENT_BOTTOM = "#8e1747"
FOREGROUND = "#ffffff"
PIPS = ((21, 21), (43, 43))
SEARCH_PIPS = ((23, 23), (41, 41))
PIP_RADIUS = 8


def icon_svg(*, circular: bool = False) -> str:
    pips = "".join(
        f'  <circle cx="{x}" cy="{y}" r="{PIP_RADIUS}" fill="{FOREGROUND}"/>\n'
        for x, y in (SEARCH_PIPS if circular else PIPS)
    )
    defs = (
        '  <defs><linearGradient id="face" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{GRADIENT_TOP}"/>'
        f'<stop offset="1" stop-color="{GRADIENT_BOTTOM}"/>'
        '</linearGradient></defs>\n'
    )
    face = (
        '  <circle cx="32" cy="32" r="32" fill="url(#face)"/>\n'
        if circular else
        '  <rect x="3" y="3" width="58" height="58" rx="12" fill="url(#face)"/>\n'
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        + defs + face + pips + '</svg>\n'
    )


def icon_version() -> str:
    return sha256((icon_svg() + icon_svg(circular=True)).encode()).hexdigest()[:12]


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def icon_image(size: int, *, circular: bool = False):
    """Supersample a padded die or full circular face with white pips."""
    from PIL import Image, ImageDraw

    scale = max(4, (size * 4 + 63) // 64)
    side = 64 * scale
    # Gradient spans the face's own box, matching the SVG objectBoundingBox.
    top_y, bottom_y = (0, side) if circular else (3 * scale, 61 * scale)
    top, bottom = _hex_rgb(GRADIENT_TOP), _hex_rgb(GRADIENT_BOTTOM)
    gradient = Image.new("RGBA", (1, side))
    span = max(1, bottom_y - top_y)
    for y in range(side):
        t = min(1.0, max(0.0, (y - top_y) / span))
        gradient.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    gradient = gradient.resize((side, side))

    mask = Image.new("L", (side, side), 0)
    mask_draw = ImageDraw.Draw(mask)
    if circular:
        mask_draw.ellipse((0, 0, side, side), fill=255)
    else:
        mask_draw.rounded_rectangle((3 * scale, 3 * scale, 61 * scale, 61 * scale),
                                    radius=12 * scale, fill=255)
    image = Image.new("RGBA", (side, side))
    image.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    for x, y in (SEARCH_PIPS if circular else PIPS):
        draw.ellipse(((x - PIP_RADIUS) * scale, (y - PIP_RADIUS) * scale,
                      (x + PIP_RADIUS) * scale, (y + PIP_RADIUS) * scale),
                     fill=FOREGROUND)
    return image.resize((size, size), Image.Resampling.LANCZOS)
