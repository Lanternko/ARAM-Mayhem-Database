"""Win-rate plates drawn over the in-game augment cards.

No injection: a separate always-on-top, click-through window placed using the
card-name positions the OCR watcher records.  It is a per-pixel-alpha layered
window (``UpdateLayeredWindow``), so the plates are anti-aliased, translucent
and can glow -- Tk's colour-key transparency only allows hard 1-bit edges.  It
only shows while the League window is in the foreground.  Exclusive
fullscreen cannot be drawn over; borderless/windowed can.

Focus safety: the window is created once and never mapped/unmapped again
(hiding = an empty 1x1 frame parked off-screen), so showing a plate cannot
pull focus from the game.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .augment_ocr import find_game_window

# Measured on a real 16.21 offer screen (name centre y=380, name height h=29):
# the description starts 3.4h below the name at ~0.74h per line, and the
# card's dark interior ends ~11.6h below it.  The longest current text (5 lines)
# ends at ~6.7h; the plate's top (incl. the "#N" tab) sits at ~8.3h so even a
# 6-line description (~7.5h) stays clear.  All sizes are in name heights so
# they scale with resolution.
DEFAULT_OFFSET = 10.0  # plate centre below the name centre
BOX_WIDTH = 9.0
BOX_HEIGHT = 2.4
GLOW = 0.7  # transparent margin around the plate (shadow, "#N" tab), in heights
SUPERSAMPLE = 3

# League client palette: cream text, hextech gold, card-rarity accents.
TEXT = (240, 230, 200)  # cream of the reroll icon
MUTED = (160, 155, 170)


def _button(rim, rim_hi, inner, top, bottom, sheen, ink, sub, **extra) -> dict:
    return {"rim": rim, "rim_hi": rim_hi, "inner": inner, "top": top, "bottom": bottom,
            "sheen": sheen, "ink": ink, "sub": sub, **extra}


# Plate = the client's square hextech button (shape sampled from the reroll
# button).  Non-recommended cards get a quiet smoke plate so they recede; the
# recommended card gets a highlight colour (ARAM_OVERLAY_THEME picks which).
LOW_KEY = _button((70, 76, 86), (98, 104, 114), (6, 8, 10), (22, 25, 30), (9, 11, 14),
                  (32, 36, 42), (0, 0, 0), (150, 152, 160), alpha=205)


def _highlight(rim, rim_hi, top, bottom, sheen, sub) -> dict:
    """Recommended-card plate: tinted fill, pastel rim and a tight glow of the rim colour."""
    return _button(rim, rim_hi, tuple(round(c * 0.35) for c in bottom), top, bottom, sheen,
                   tuple(round(c * 0.25) for c in bottom), sub, glow=rim)


THEMES = {
    "pink-diamond": {"label": "粉鑽", "normal": LOW_KEY, "best": _highlight(
        (255, 176, 212), (255, 226, 240), (96, 42, 74), (48, 18, 38), (128, 62, 100), (255, 214, 232))},
    "mist-purple": {"label": "霧紫", "normal": LOW_KEY, "best": _highlight(
        (178, 162, 208), (218, 208, 236), (64, 56, 88), (32, 28, 48), (84, 74, 112), (218, 208, 232))},
    "macaron-purple": {"label": "馬卡龍紫", "normal": LOW_KEY, "best": _highlight(
        (204, 172, 255), (236, 222, 255), (86, 60, 134), (42, 28, 72), (110, 82, 162), (230, 216, 255))},
    "champagne": {"label": "香檳金", "normal": LOW_KEY, "best": _highlight(
        (236, 210, 158), (252, 236, 200), (84, 68, 42), (40, 32, 18), (110, 90, 58), (240, 224, 190))},
    "ice-blue": {"label": "冰藍", "normal": LOW_KEY, "best": _highlight(
        (160, 220, 255), (220, 242, 255), (30, 64, 92), (12, 30, 46), (44, 86, 118), (205, 232, 250))},
    "mint": {"label": "薄荷", "normal": LOW_KEY, "best": _highlight(
        (160, 238, 208), (220, 252, 238), (24, 80, 62), (10, 40, 30), (36, 104, 82), (205, 245, 228))},
}
DEFAULT_THEME = "macaron-purple"


def overlay_theme() -> dict:
    return THEMES.get(os.environ.get("ARAM_OVERLAY_THEME", DEFAULT_THEME), THEMES[DEFAULT_THEME])


# Win-rate colour by pp vs this champion's average (its base rate cancels out).
TIERS = (  # (lower bound in pp, colour)
    (3.0, (46, 204, 96)),     # strong green
    (1.0, (160, 236, 176)),   # light green
    (-1.0, (240, 236, 228)),  # white: within +-1pp of average
    (-3.0, (255, 160, 150)),  # light red
)
STRONG_RED = (236, 72, 64)
RANK_SHOWN = 5  # show "#N" for the champion's top-N win rates in the rarity
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def overlay_offset() -> float:
    try:
        return float(os.environ.get("ARAM_OVERLAY_OFFSET", DEFAULT_OFFSET))
    except ValueError:
        return DEFAULT_OFFSET


def badge_text(row: dict) -> str:
    """``56%``: the champion's win rate with this augment."""
    return "—" if row.get("wr") is None else f"{round(float(row['wr']) * 100)}%"


def badge_tone(row: dict) -> tuple[int, int, int]:
    """Five-step colour: strong/light green, white, light/strong red."""
    if row.get("wr") is None:
        return MUTED
    lift = row.get("lift")
    pp = 0.0 if lift is None else float(lift) * 100
    for bound, colour in TIERS:
        if pp >= bound:
            return colour
    return STRONG_RED


def badge_sub(row: dict) -> str:
    """Second line: how often players on this champion take it."""
    if row.get("wr") is None:
        return "樣本不足"
    pick = row.get("pick")
    return "選用率 —" if pick is None else f"選用率 {float(pick) * 100:.1f}%"


def badge_rank(row: dict) -> int | None:
    rank = row.get("rank")
    return int(rank) if isinstance(rank, int) and 1 <= rank <= RANK_SHOWN else None


def badge_layout(rows: list[dict], offset: float | None = None) -> list[dict]:
    """Rows from AugmentAdvisor.recommend (best first, with ``screen``) -> plates."""
    offset = overlay_offset() if offset is None else offset
    best_id = rows[0].get("id") if rows and rows[0].get("wr") is not None else None
    placed = [row for row in rows
              if isinstance(row.get("screen"), dict) and {"x", "y", "h"} <= set(row["screen"])]
    if not placed:
        return []
    # One size and baseline for the row: OCR boxes differ by a pixel or two per name.
    h = max(8, sorted(int(row["screen"]["h"]) for row in placed)[len(placed) // 2])
    base_y = sorted(int(row["screen"]["y"]) for row in placed)[len(placed) // 2]
    badges = []
    for row in placed:
        badges.append({
            "x": int(row["screen"]["x"]),
            "y": round(base_y + offset * h),
            "unit": h,
            "w": round(BOX_WIDTH * h),
            "h": round(BOX_HEIGHT * h),
            "text": badge_text(row),
            "sub": badge_sub(row),
            "color": badge_tone(row),
            "rank": badge_rank(row),
            "rarity": row.get("rarity"),
            "best": best_id is not None and row.get("id") == best_id,
        })
    return badges


# ---------------------------------------------------------------- rendering


@lru_cache(maxsize=32)
def _font(name: str, px: int) -> ImageFont.ImageFont:
    for candidate in (name, "msjhbd.ttc", "seguisb.ttf"):
        try:
            return ImageFont.truetype(str(FONT_DIR / candidate), px)
        except OSError:
            continue
    return ImageFont.load_default()


def _number_font(px: int) -> ImageFont.ImageFont:
    font = _font("bahnschrift.ttf", px)
    try:  # variable font: pick the SemiBold instance when available
        font.set_variation_by_name("SemiBold")
    except (OSError, ValueError, AttributeError):
        pass
    return font


def _vertical_fill(draw: ImageDraw.ImageDraw, box: tuple, top: tuple, bottom: tuple, alpha: int = 255) -> None:
    x0, y0, x1, y1 = (round(v) for v in box)
    span = max(1, y1 - y0)
    for y in range(y0, y1 + 1):
        t = (y - y0) / span
        draw.line([(x0, y), (x1, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (alpha,))


def _hextech_frame(draw: ImageDraw.ImageDraw, box: tuple, unit: float, style: dict) -> None:
    """Square-cornered client button: pale-gold rim, dark inner line, gradient fill."""
    x0, y0, x1, y1 = box
    rim = max(1, round(0.08 * unit))
    gap = max(1, round(0.06 * unit))
    draw.rectangle(box, fill=(*style["rim"], 255))
    draw.line([(x0, y0), (x1, y0)], fill=(*style["rim_hi"], 255), width=rim)  # lit top edge
    alpha = style.get("alpha", 255)
    draw.rectangle((x0 + rim, y0 + rim, x1 - rim, y1 - rim), fill=(*style["inner"], alpha))
    body = (x0 + rim + gap, y0 + rim + gap, x1 - rim - gap, y1 - rim - gap)
    _vertical_fill(draw, body, style["top"], style["bottom"], alpha)
    # faint sheen under the top edge, darker lip along the bottom (pressed-metal look)
    draw.line([(body[0], body[1]), (body[2], body[1])], fill=(*style["sheen"], alpha), width=gap)
    draw.line([(body[0], body[3]), (body[2], body[3])], fill=(*style["inner"], alpha), width=gap)


def render_badge(badge: dict, theme: dict | None = None) -> Image.Image:
    """One plate as an RGBA image, plate centred, ``GLOW*unit`` margin around it."""
    s = SUPERSAMPLE
    unit = badge["unit"] * s
    w, h = badge["w"] * s, badge["h"] * s
    pad = round(GLOW * unit)
    img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    box = (pad, pad, pad + w - 1, pad + h - 1)
    theme = theme or overlay_theme()
    style = theme["best"] if badge["best"] else theme["normal"]

    # Drop shadow so the plate sits on the card; the pick glows instead.
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    if style.get("glow"):
        ImageDraw.Draw(halo).rectangle(box, fill=(*style["glow"], 110))
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(0.12 * unit)))
    else:
        ImageDraw.Draw(halo).rectangle((box[0], box[1] + 0.12 * unit, box[2], box[3] + 0.12 * unit),
                                       fill=(0, 0, 0, 150))
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(0.18 * unit)))

    draw = ImageDraw.Draw(img)
    _hextech_frame(draw, box, unit, style)

    cx = pad + w / 2
    big = _number_font(round(1.15 * unit))
    small = _font("msjhbd.ttc", round(0.46 * unit))
    stroke = max(1, round(0.06 * unit))
    draw.text((cx, pad + h * 0.41), badge["text"], font=big, fill=(*badge["color"], 255), anchor="mm",
              stroke_width=stroke, stroke_fill=(*style["ink"], 255))
    draw.text((cx, pad + h * 0.78), badge["sub"], font=small, fill=(*style["sub"], 255), anchor="mm")

    if badge.get("rank"):  # "#N" tab on the top edge, a miniature of the same button
        chip_font = _number_font(round(0.5 * unit))
        cw, ch = round(1.7 * unit), round(0.72 * unit)
        chip = (cx - cw / 2, pad - ch / 2, cx + cw / 2, pad + ch / 2)
        chip_style = theme["normal"]
        if badge["rank"] <= 3:  # top 3: gold-lit rim
            chip_style = {**chip_style, "rim": (200, 170, 110), "rim_hi": (240, 215, 150)}
        _hextech_frame(draw, chip, unit * 0.6, {**chip_style, "alpha": 255})
        draw.text((cx, pad), f"#{badge['rank']}", font=chip_font, fill=(*TEXT, 255), anchor="mm")

    return img.resize((img.width // s, img.height // s), Image.LANCZOS)


def compose(badges: list[dict], theme: dict | None = None) -> tuple[int, int, Image.Image]:
    """All plates on one transparent canvas -> (screen left, screen top, image)."""
    tiles = [(b, render_badge(b, theme)) for b in badges]
    left = min(b["x"] - im.width // 2 for b, im in tiles)
    top = min(b["y"] - im.height // 2 for b, im in tiles)
    right = max(b["x"] - im.width // 2 + im.width for b, im in tiles)
    bottom = max(b["y"] - im.height // 2 + im.height for b, im in tiles)
    canvas = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    for b, im in tiles:
        canvas.alpha_composite(im, (b["x"] - im.width // 2 - left, b["y"] - im.height // 2 - top))
    return left, top, canvas


# ---------------------------------------------------------------- window


def _hwnd(win: tk.Toplevel) -> int:
    import ctypes

    return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


def _make_click_through(win: tk.Toplevel) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _hwnd(win)
    GWL_EXSTYLE = -20
    WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
    WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
    )


def _update_layered(hwnd: int, left: int, top: int, image: Image.Image) -> None:
    """Push a straight-alpha RGBA image to a layered window (premultiplied BGRA DIB)."""
    import ctypes
    from ctypes import wintypes

    import numpy as np

    arr = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    alpha = arr[..., 3:4]
    bgra = np.empty(arr.shape, dtype=np.uint8)
    bgra[..., :3] = (arr[..., 2::-1] * alpha // 255).astype(np.uint8)
    bgra[..., 3] = arr[..., 3].astype(np.uint8)
    width, height = image.size

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]

    # Private library handles: the argtypes below must not leak into ctypes.windll users.
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
        wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION),
        wintypes.DWORD,
    ]

    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    try:
        ctypes.memmove(bits, bgra.tobytes(), bgra.nbytes)
        old = gdi32.SelectObject(mem_dc, bitmap)
        user32.UpdateLayeredWindow(
            hwnd, screen_dc, ctypes.byref(wintypes.POINT(left, top)), ctypes.byref(wintypes.SIZE(width, height)),
            mem_dc, ctypes.byref(wintypes.POINT(0, 0)), 0,
            ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2,  # AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA
        )
        gdi32.SelectObject(mem_dc, old)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


PARK = (-32000, -32000)


class AugmentOverlay:
    def __init__(
        self,
        root: tk.Misc,
        game_foreground: Callable[[], int | None] = find_game_window,
        poll_ms: int = 400,
    ) -> None:
        if sys.platform != "win32":
            raise tk.TclError("layered overlay needs Windows")
        self.root = root
        self.game_foreground = game_foreground
        self.poll_ms = poll_ms
        self.badges: list[dict] = []
        self._shown_key: tuple | None = None
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"1x1+{PARK[0]}+{PARK[1]}")
        self.win.update_idletasks()
        _make_click_through(self.win)
        self._park()
        self.root.after(self.poll_ms, self._tick)

    def set_badges(self, badges: list[dict]) -> None:
        self.badges = badges
        self._refresh()

    def _park(self) -> None:
        _update_layered(_hwnd(self.win), *PARK, Image.new("RGBA", (1, 1), (0, 0, 0, 0)))

    def _refresh(self) -> None:
        visible = bool(self.badges) and self.game_foreground() is not None
        key = (visible, tuple(tuple(sorted(b.items())) for b in self.badges)) if visible else (False,)
        if key == self._shown_key:
            return
        self._shown_key = key
        if not visible:
            self._park()
            return
        left, top, image = compose(self.badges)
        _update_layered(_hwnd(self.win), left, top, image)

    def _tick(self) -> None:
        try:
            self._refresh()
        except tk.TclError:
            return  # window destroyed with the app
        self.root.after(self.poll_ms, self._tick)
