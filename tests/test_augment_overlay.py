"""In-game overlay: badge placement, and the window never takes focus or clicks."""
from __future__ import annotations

import sys
import unittest

from aram_nn.augment_overlay import (
    STRONG_RED,
    TIERS,
    badge_layout,
    badge_sub,
    badge_text,
    badge_tone,
    compose,
)

ROWS = [  # AugmentAdvisor.recommend order: best first
    {"id": 2, "wr": 0.585, "lift": 0.021, "pick": 0.062, "rank": 2, "screen": {"x": 1280, "y": 700, "h": 30}},
    {"id": 1, "wr": 0.556, "lift": -0.009, "pick": 0.031, "rank": 9, "screen": {"x": 650, "y": 700, "h": 30}},
    {"id": 3, "wr": None, "lift": None, "screen": {"x": 1910, "y": 700, "h": 30}},
]


class BadgeLayoutTest(unittest.TestCase):
    def test_positions_and_best_marker(self) -> None:
        badges = badge_layout(ROWS, offset=9.5)
        self.assertEqual([(b["x"], b["y"]) for b in badges], [(1280, 985), (650, 985), (1910, 985)])
        self.assertEqual((badges[0]["w"], badges[0]["h"]), (270, 72))
        self.assertEqual([b["text"] for b in badges], ["58%", "56%", "—"])
        self.assertEqual([b["sub"] for b in badges], ["選用率 6.2%", "選用率 3.1%", "樣本不足"])
        self.assertEqual([b["rank"] for b in badges], [2, None, None])  # only top 5 get "#N"
        self.assertEqual([b["best"] for b in badges], [True, False, False])

    def test_five_colour_steps_by_lift(self) -> None:
        colours = [badge_tone({"wr": 0.5, "lift": pp / 100}) for pp in (4, 2, 0.5, -0.5, -2, -4)]
        self.assertEqual(colours, [TIERS[0][1], TIERS[1][1], TIERS[2][1], TIERS[2][1], TIERS[3][1], STRONG_RED])

    def test_sub_line(self) -> None:
        self.assertEqual(badge_sub({"wr": 0.53, "pick": 0.0046}), "選用率 0.5%")
        self.assertEqual(badge_text({"wr": 0.53}), "53%")

    def test_compose_places_plates_on_one_transparent_canvas(self) -> None:
        left, top, image = compose(badge_layout(ROWS, offset=9.5))
        self.assertEqual(image.mode, "RGBA")
        self.assertLess(left, 650 - 135)
        self.assertGreater(left + image.width, 1910 + 135)
        self.assertEqual(image.getpixel((0, 0))[3], 0)  # corners stay see-through
        plate = image.getpixel((1280 - left, 985 - top + 30))
        self.assertGreater(plate[3], 180)  # plate body is mostly opaque

    def test_rows_without_position_are_skipped(self) -> None:
        # Overwolf events carry no screen position: panel only, no overlay.
        self.assertEqual(badge_layout([{"id": 1, "wr": 0.5}], offset=-2.0), [])

    def test_no_best_when_champion_unknown(self) -> None:
        rows = [{**row, "wr": None, "lift": None} for row in ROWS]
        self.assertFalse(any(b["best"] for b in badge_layout(rows, offset=-2.0)))


@unittest.skipUnless(sys.platform == "win32", "Windows window styles")
class OverlayWindowTest(unittest.TestCase):
    def test_click_through_and_no_focus_steal(self) -> None:
        import ctypes
        import ctypes.wintypes
        import tkinter as tk

        from aram_nn.augment_overlay import AugmentOverlay

        user32 = ctypes.windll.user32
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            root.geometry("600x300+100+100")
            root.update()
            root.focus_force()
            root.update()
            overlay = AugmentOverlay(root, game_foreground=lambda: 1)
            root.update()
            before = user32.GetForegroundWindow()
            overlay.set_badges(badge_layout(
                [{"id": 1, "wr": 0.585, "lift": 0.02, "screen": {"x": 400, "y": 250, "h": 30}}], offset=0,
            ))
            root.update()
            self.assertEqual(user32.GetForegroundWindow(), before)
            hwnd = user32.GetParent(overlay.win.winfo_id()) or overlay.win.winfo_id()
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self.assertGreater(rect.right - rect.left, 100)
            self.assertGreater(rect.left, -1000)
            ex = user32.GetWindowLongW(hwnd, -20)
            self.assertTrue(ex & 0x20, "WS_EX_TRANSPARENT (click-through) missing")
            self.assertTrue(ex & 0x08000000, "WS_EX_NOACTIVATE missing")
            overlay.set_badges([])
            root.update()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self.assertLess(rect.left, -1000)  # parked, not unmapped
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
