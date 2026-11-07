"""Read the ARAM: Mayhem augment offer off the screen with Windows' built-in OCR.

Why OCR: the offer only exists inside the game process.  Blitz reads it with a
Riot-allowlisted injected DLL (not an option for us: Vanguard), and the
Overwolf route needs a Riot-approved app before Overwolf will load it.  The
three card names are plain text on screen, so a screenshot + the OS OCR engine
(``Windows.Media.Ocr``, ~0.1 s per frame, no model download) recovers them.

Cost control: a frame is only captured while the League game window is in the
foreground, once per ``interval`` seconds.  OCR cost scales with pixels, so the
first offer is found with a wide scan of the screen centre, and the row the
card names sat on is remembered (``ocr_layout.json``).  After that each tick
only reads that thin band at full resolution, with a wide scan every
``FULL_SCAN_EVERY`` ticks in case the layout changed.  Downscaling instead was
measured to lose 18-22 px names, so resolution is never reduced below native.

False-positive control: a line only counts when it matches a known augment
name, and an offer needs at least two distinct names on the same row spread
apart horizontally (the cards sit side by side; descriptions, the scoreboard
and tooltips do not look like that).

Events are written with :func:`aram_nn.augment_bridge.record_augment_event`, in
the same shape the Overwolf probe produces, so the GUI panel is source-agnostic.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .augment_bridge import record_augment_event

GAME_WINDOW_CLASS = "RiotWindowClass"
GAME_WINDOW_TITLE = "League of Legends (TM) Client"
# Fractions of the game client area that can contain the offer cards.
CROP = (0.08, 0.10, 0.92, 0.85)
# OCR cost grows with pixels; 4K text is still legible at half size.
MAX_OCR_HEIGHT = 1300
OCR_LANGUAGE = "zh-Hant-TW"
SOURCE = "screen-ocr"
LAYOUT_FILE = "ocr_layout.json"
# Once the name row is known, re-check the whole crop every N ticks.
FULL_SCAN_EVERY = 10
# Band half-height around the name row, in name-line heights.
BAND_HALF_HEIGHTS = 2.5

_STRIP = re.compile(r"[\s·・:：,，.。!！?？'\"「」『』（）()\[\]【】|/\\-]+")


def normalize(text: object) -> str:
    return _STRIP.sub("", str(text or "")).lower()


@dataclass(frozen=True)
class OcrLine:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class OfferOption:
    augment_id: int
    name: str
    x: float
    cy: float = 0.0
    h: float = 0.0
    # Card name position on the desktop ({"x", "y", "h"}: centre x, centre y,
    # text height in screen pixels) for the in-game overlay.
    screen: dict[str, int] | None = None


class OfferMatcher:
    """Map OCR lines onto known augment names and find the card row."""

    def __init__(self, names: dict[str, int], min_ratio: float = 0.72) -> None:
        self.min_ratio = min_ratio
        self._by_norm: dict[str, tuple[int, str]] = {}
        for name, aid in names.items():
            key = normalize(name)
            if len(key) >= 2:
                self._by_norm.setdefault(key, (int(aid), str(name)))
        self._keys = list(self._by_norm)
        self._max_len = max((len(k) for k in self._keys), default=0)

    def match_line(self, text: str) -> tuple[int, str] | None:
        key = normalize(text)
        if len(key) < 2 or len(key) > self._max_len + 2:
            return None
        hit = self._by_norm.get(key)
        if hit:
            return hit
        # One misread glyph in a 4-character name scores 0.75.
        if len(key) < 3:
            return None
        close = difflib.get_close_matches(key, self._keys, n=2, cutoff=self.min_ratio)
        if not close:
            return None
        if len(close) > 1:
            best = difflib.SequenceMatcher(None, key, close[0]).ratio()
            second = difflib.SequenceMatcher(None, key, close[1]).ratio()
            if best - second < 0.05:
                return None  # ambiguous misread; skip rather than guess
        return self._by_norm[close[0]]

    def find_offer(self, lines: Iterable[OcrLine]) -> list[OfferOption] | None:
        hits: list[tuple[OcrLine, int, str]] = []
        for line in lines:
            match = self.match_line(line.text)
            if match:
                hits.append((line, match[0], match[1]))
        if len(hits) < 2:
            return None
        hits.sort(key=lambda hit: hit[0].cy)
        rows: list[list[tuple[OcrLine, int, str]]] = []
        for hit in hits:
            line = hit[0]
            if rows:
                anchor = rows[-1][0][0]
                if abs(line.cy - anchor.cy) <= max(anchor.h, line.h) * 1.5:
                    rows[-1].append(hit)
                    continue
            rows.append([hit])

        best: list[OfferOption] | None = None
        for row in rows:
            options: dict[int, OfferOption] = {}
            for line, aid, name in sorted(row, key=lambda hit: hit[0].cx):
                options.setdefault(aid, OfferOption(aid, name, line.cx, line.cy, line.h))
            ordered = sorted(options.values(), key=lambda opt: opt.x)
            if len(ordered) < 2:
                continue
            height = max(hit[0].h for hit in row)
            gaps = [b.x - a.x for a, b in zip(ordered, ordered[1:])]
            if min(gaps) < height * 4:
                continue  # cards are far apart; adjacent words are not
            if best is None or len(ordered) > len(best):
                best = ordered[:3]
        return best


class OfferTracker:
    """Turn per-frame detections into offer / closed events."""

    def __init__(self, close_after: int = 3) -> None:
        self.close_after = close_after
        self.current: tuple[int, ...] | None = None
        self._misses = 0

    def update(self, offer: list[OfferOption] | None) -> dict[str, Any] | None:
        if offer:
            ids = tuple(opt.augment_id for opt in offer)
            self._misses = 0
            # A hover tooltip can cover one card; a partial read of the same
            # offer must not shrink the panel.
            if self.current and set(ids) <= set(self.current):
                return None
            self.current = ids
            return {
                "type": "offer",
                "augments": [
                    {"slot": f"slot_{i}", "id": opt.augment_id, "name": opt.name,
                     **({"screen": opt.screen} if opt.screen else {})}
                    for i, opt in enumerate(offer, start=1)
                ],
            }
        if self.current is None:
            return None
        self._misses += 1
        if self._misses < self.close_after:
            return None
        self.current = None
        self._misses = 0
        return {"type": "closed", "augments": []}


# ---------- Windows plumbing ----------

def find_game_window() -> int | None:
    """Return the League game window handle when it is the foreground window."""
    if sys.platform != "win32":
        return None
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(GAME_WINDOW_CLASS, GAME_WINDOW_TITLE) or user32.FindWindowW(None, GAME_WINDOW_TITLE)
    if not hwnd or user32.GetForegroundWindow() != hwnd or user32.IsIconic(hwnd):
        return None
    return int(hwnd)


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    if rect.right <= 0 or rect.bottom <= 0:
        return None
    return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom


Rect = tuple[int, int, int, int]  # left, top, right, bottom in desktop pixels


def own_window_rects() -> list[Rect]:
    """Visible top-level windows of this process, except click-through overlays.

    The augment overlay (layered + WS_EX_TRANSPARENT) only draws win-rate
    plates below the card names, so it stays unmasked; masking its bounding
    box could hide the very names being read.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    import os
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = os.getpid()
    ws_ex_transparent = 0x00000020
    rects: list[Rect] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, -20) & ws_ex_transparent:  # GWL_EXSTYLE
            return True
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
            rects.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumWindows(visit, 0)
    return rects


def mask_rects(image, origin: tuple[int, int], rects: Iterable[Rect]):
    """Paint ``rects`` (desktop coords) black on ``image`` whose (0, 0) is ``origin``."""
    from PIL import ImageDraw

    draw = None
    for left, top, right, bottom in rects:
        box = (max(0, left - origin[0]), max(0, top - origin[1]),
               min(image.width, right - origin[0]), min(image.height, bottom - origin[1]))
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        draw = draw or ImageDraw.Draw(image)
        draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=(0, 0, 0))
    return image


@dataclass(frozen=True)
class Frame:
    """A captured strip of the game client plus how to map it back."""

    image: Any
    top: float  # client-area y of the image's first row
    scale: float  # image pixels per client pixel
    client_height: float
    origin: tuple[int, int] | None = None  # desktop x, y of the image's (0, 0)

    def client_frac(self, y: float) -> float:
        return (self.top + y / self.scale) / self.client_height

    def screen_pos(self, opt: OfferOption) -> dict[str, int] | None:
        if self.origin is None:
            return None
        return {
            "x": round(self.origin[0] + opt.x / self.scale),
            "y": round(self.origin[1] + opt.cy / self.scale),
            "h": round(opt.h / self.scale),
        }


Band = tuple[float, float]  # (top, bottom) as fractions of the client height


def capture_offer_region(hwnd: int, band: Band | None = None) -> Frame | None:
    from PIL import ImageGrab

    rect = client_rect(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    y0, y1 = band or (CROP[1], CROP[3])
    box = (
        left + int(width * CROP[0]),
        top + int(height * y0),
        left + int(width * CROP[2]),
        top + int(height * y1),
    )
    image = ImageGrab.grab(bbox=box, all_screens=True)
    # The grab is screen pixels, so a recommender window pinned over the game
    # gets read too — its TOP3 lists are augment names in a row and were
    # reported as a (fake) offer.  Black out our own windows first.
    image = mask_rects(image, (box[0], box[1]), own_window_rects())
    scale = 1.0
    if image.height > MAX_OCR_HEIGHT:
        scale = MAX_OCR_HEIGHT / image.height
        image = image.resize((int(image.width * scale), MAX_OCR_HEIGHT))
    return Frame(image, int(height * y0), scale, height, origin=(box[0], box[1]))


def band_for_offer(offer: list[OfferOption], frame: Frame) -> Band:
    centre = frame.client_frac(sorted(opt.cy for opt in offer)[len(offer) // 2])
    half = BAND_HALF_HEIGHTS * max(opt.h for opt in offer) / frame.scale / frame.client_height
    return max(0.0, round(centre - half, 4)), min(1.0, round(centre + half, 4))


def load_band(out_dir: Path) -> Band | None:
    import json

    try:
        band = json.loads((Path(out_dir) / LAYOUT_FILE).read_text(encoding="utf-8"))["band"]
        y0, y1 = float(band[0]), float(band[1])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
    return (y0, y1) if 0.0 <= y0 < y1 <= 1.0 else None


def save_band(out_dir: Path, band: Band) -> None:
    import json

    path = Path(out_dir) / LAYOUT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"band": list(band)}), encoding="utf-8")


class WindowsOcr:
    """Thin wrapper over Windows.Media.Ocr (needs the zh-Hant OCR language pack)."""

    def __init__(self, language: str = OCR_LANGUAGE) -> None:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        self._SoftwareBitmap = SoftwareBitmap
        self._format = BitmapPixelFormat.RGBA8
        self._alpha = BitmapAlphaMode.PREMULTIPLIED
        self._DataWriter = DataWriter
        self._engine = OcrEngine.try_create_from_language(Language(language))
        if self._engine is None:
            raise RuntimeError(f"Windows OCR language {language} is not installed")
        self._loop = asyncio.new_event_loop()

    def recognize(self, image) -> list[OcrLine]:
        rgba = image.convert("RGBA")
        writer = self._DataWriter()
        writer.write_bytes(rgba.tobytes())
        bitmap = self._SoftwareBitmap(self._format, rgba.width, rgba.height, self._alpha)
        bitmap.copy_from_buffer(writer.detach_buffer())
        result = self._loop.run_until_complete(self._engine.recognize_async(bitmap))
        lines: list[OcrLine] = []
        for line in result.lines:
            rects = [word.bounding_rect for word in line.words]
            if not rects:
                continue
            x0 = min(r.x for r in rects)
            y0 = min(r.y for r in rects)
            x1 = max(r.x + r.width for r in rects)
            y1 = max(r.y + r.height for r in rects)
            lines.append(OcrLine(line.text, x0, y0, x1 - x0, y1 - y0))
        return lines

    def close(self) -> None:
        self._loop.close()


class AugmentOcrWatcher(threading.Thread):
    def __init__(
        self,
        out_dir: Path,
        names: dict[str, int],
        interval: float = 1.0,
        ocr: WindowsOcr | None = None,
        find_window: Callable[[], int | None] = find_game_window,
        capture: Callable[[int, Band | None], Frame | None] = capture_offer_region,
    ) -> None:
        super().__init__(name="augment-ocr", daemon=True)
        self.out_dir = Path(out_dir)
        self.matcher = OfferMatcher(names)
        self.tracker = OfferTracker()
        self.interval = interval
        self.ocr = ocr
        self.find_window = find_window
        self.capture = capture
        self.band = load_band(self.out_dir)
        self.last_error: str | None = None
        self._ticks = 0
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def scan_once(self) -> dict[str, Any] | None:
        hwnd = self.find_window()
        if hwnd is None:
            return None  # alt-tabbed or no game: keep the current offer as-is
        wide = self.band is None or self._ticks % FULL_SCAN_EVERY == 0
        self._ticks += 1
        frame = self.capture(hwnd, None if wide else self.band)
        if frame is None:
            return None
        if self.ocr is None:
            self.ocr = WindowsOcr()
        offer = self.matcher.find_offer(self.ocr.recognize(frame.image))
        if offer:
            band = band_for_offer(offer, frame)
            if self.band is None or max(abs(a - b) for a, b in zip(band, self.band)) > 0.01:
                self.band = band
                save_band(self.out_dir, band)
            offer = [replace(opt, screen=frame.screen_pos(opt)) for opt in offer]
        event = self.tracker.update(offer)
        if event:
            record_augment_event(self.out_dir, {"app": SOURCE, "event": event})
        return event

    def run(self) -> None:
        backoff = self.interval
        while not self._halt.wait(backoff):
            try:
                self.scan_once()
                backoff = self.interval
                self.last_error = None
            except Exception as exc:  # keep the GUI alive; retry slowly
                self.last_error = repr(exc)
                backoff = min(30.0, backoff * 2)


def ocr_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winrt.windows.media.ocr  # noqa: F401
    except Exception:
        return False
    return True


def start_ocr_watcher(out_dir: Path, names: dict[str, int], interval: float = 1.0) -> AugmentOcrWatcher | None:
    if not names or not ocr_available():
        return None
    watcher = AugmentOcrWatcher(out_dir, names, interval=interval)
    watcher.start()
    return watcher


def _main(argv: list[str] | None = None) -> int:
    """Debug: OCR an image file and print what the matcher sees."""
    import argparse
    import json

    from PIL import Image

    parser = argparse.ArgumentParser(description=_main.__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--tier-list", type=Path, default=Path("docs/api/tier-list.json"))
    args = parser.parse_args(argv)
    payload = json.loads(args.tier_list.read_text(encoding="utf-8"))
    augs = payload.get("augs") or payload.get("augments") or {}
    names = {meta.get("name_zh"): int(aid) for aid, meta in augs.items() if str(aid).isdigit() and meta.get("name_zh")}
    image = Image.open(args.image)
    width, height = image.size
    image = image.crop((int(width * CROP[0]), int(height * CROP[1]), int(width * CROP[2]), int(height * CROP[3])))
    lines = WindowsOcr().recognize(image)
    for line in lines:
        print(f"{line.text!r} @({line.x:.0f},{line.y:.0f}) h={line.h:.0f}")
    offer = OfferMatcher(names).find_offer(lines)
    print("offer:", [(opt.augment_id, opt.name) for opt in offer] if offer else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
