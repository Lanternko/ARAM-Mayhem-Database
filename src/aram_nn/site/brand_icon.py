"""Deterministic arammeta icon geometry shared by SVG and raster exports."""
from __future__ import annotations

from hashlib import sha256

BACKGROUND = "#101114"
FOREGROUND = "#f5c518"
# User-approved regular octagonal ring, uniformly scaled onto a square canvas.
from math import sqrt

CENTER = (29.683327, 31.985513)
RING_WIDTH = 7.423404

def _canvas_point(x, y):
    return ((x - 0.40218306) * 64 / 61.25341,
            (y - 0.35327884) * 64 / 61.25341)

def _octagon(radius):
    x, y = CENTER
    k = radius * (sqrt(2) - 1)
    return tuple(_canvas_point(x + dx, y + dy) for dx, dy in
                 ((-k, -radius), (k, -radius), (radius, -k), (radius, k),
                  (k, radius), (-k, radius), (-radius, k), (-radius, -k)))

OUTER = _octagon(18)
COUNTER = _octagon(18 - RING_WIDTH)
STEM = tuple(_canvas_point(x, y) for x, y in
             ((48.5 - RING_WIDTH, CENTER[1] - 18), (48.5, CENTER[1] - 18),
              (48.5, CENTER[1] + 18), (48.5 - RING_WIDTH, CENTER[1] + 18)))
PIPS = tuple(_canvas_point(CENTER[0] + d, CENTER[1] + d) for d in (-4.5, 0, 4.5))
PIP_RADIUS = 2.4 * 64 / 61.25341


def icon_svg() -> str:
    def contour(points):
        return "M" + " L".join(f"{x} {y}" for x, y in points) + " Z"

    pips = "".join(
        f'  <circle cx="{x}" cy="{y}" r="{PIP_RADIUS}" fill="{FOREGROUND}"/>\n'
        for x, y in PIPS
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        f'  <rect width="64" height="64" rx="12" fill="{BACKGROUND}"/>\n'
        f'  <path fill="{FOREGROUND}" fill-rule="evenodd" '
        f'd="{contour(OUTER)} {contour(COUNTER)}"/>\n'
         + f'  <path fill="{FOREGROUND}" d="{contour(STEM)}"/>\n'
        + pips + '</svg>\n'
    )


def icon_version() -> str:
    return sha256(icon_svg().encode()).hexdigest()[:12]


def icon_image(size: int):
    """Supersample the same closed contours, including the transparent corners."""
    from PIL import Image, ImageDraw

    scale = max(4, (size * 4 + 63) // 64)
    side = 64 * scale
    image = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, side - 1, side - 1), radius=12 * scale,
                           fill=BACKGROUND)
    for points, color in ((OUTER, FOREGROUND), (COUNTER, BACKGROUND), (STEM, FOREGROUND)):
        draw.polygon([(x * scale, y * scale) for x, y in points], fill=color)
    for x, y in PIPS:
        draw.ellipse(((x - PIP_RADIUS) * scale, (y - PIP_RADIUS) * scale,
                      (x + PIP_RADIUS) * scale, (y + PIP_RADIUS) * scale),
                     fill=FOREGROUND)
    return image.resize((size, size), Image.Resampling.LANCZOS)
