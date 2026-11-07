"""Screen-OCR augment offer: matching, row detection, event lifecycle."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from aram_nn.augment_ocr import (
    BAND_HALF_HEIGHTS,
    FULL_SCAN_EVERY,
    AugmentOcrWatcher,
    Frame,
    OcrLine,
    OfferMatcher,
    OfferTracker,
    load_band,
    normalize,
    ocr_available,
)

NAMES = {"究極九頭蛇": 101, "頂狙武器": 102, "雙刀流": 103, "靈魂淨化": 104, "盛宴": 105}


def _card_row(names: list[str], y: float = 430, h: float = 30) -> list[OcrLine]:
    return [OcrLine(" ".join(name), 290 + i * 560, y, 30 * len(name), h) for i, name in enumerate(names)]


def _description(x: float, y: float = 508) -> OcrLine:
    return OcrLine("攻 擊 速 度 提 升 30 % , 持 續 5 秒", x, y, 260, 20)


class MatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = OfferMatcher(NAMES)

    def test_normalize_drops_ocr_spacing(self) -> None:
        self.assertEqual(normalize("究 極 九 頭 蛇"), "究極九頭蛇")

    def test_exact_and_one_misread_glyph(self) -> None:
        self.assertEqual(self.matcher.match_line("究 極 九 頭 蛇"), (101, "究極九頭蛇"))
        self.assertEqual(self.matcher.match_line("項狙武器"), (102, "頂狙武器"))

    def test_short_names_need_exact_match(self) -> None:
        self.assertEqual(self.matcher.match_line("盛宴"), (105, "盛宴"))
        self.assertIsNone(self.matcher.match_line("盛會"))

    def test_descriptions_never_match(self) -> None:
        self.assertIsNone(self.matcher.match_line(_description(0).text))

    def test_three_cards_in_screen_order(self) -> None:
        lines = _card_row(["頂狙武器", "究極九頭蛇", "雙刀流"]) + [_description(x) for x in (235, 795, 1355)]
        offer = self.matcher.find_offer(lines)
        self.assertEqual([opt.augment_id for opt in offer], [102, 101, 103])

    def test_two_readable_cards_still_count(self) -> None:
        lines = _card_row(["頂狙武器", "看不懂的字", "雙刀流"])
        self.assertEqual([opt.augment_id for opt in self.matcher.find_offer(lines)], [102, 103])

    def test_single_name_is_not_an_offer(self) -> None:
        # e.g. the picked augment shown in the scoreboard / a tooltip
        self.assertIsNone(self.matcher.find_offer(_card_row(["雙刀流"])))

    def test_names_on_different_rows_are_not_an_offer(self) -> None:
        lines = [OcrLine("雙刀流", 300, 200, 90, 30), OcrLine("頂狙武器", 900, 700, 120, 30)]
        self.assertIsNone(self.matcher.find_offer(lines))

    def test_adjacent_names_are_not_cards(self) -> None:
        lines = [OcrLine("雙刀流", 300, 430, 90, 30), OcrLine("頂狙武器", 400, 430, 120, 30)]
        self.assertIsNone(self.matcher.find_offer(lines))


class TrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = OfferMatcher(NAMES)
        self.full = self.matcher.find_offer(_card_row(["頂狙武器", "究極九頭蛇", "雙刀流"]))

    def test_offer_once_then_closed_after_misses(self) -> None:
        tracker = OfferTracker(close_after=3)
        event = tracker.update(self.full)
        self.assertEqual(event["type"], "offer")
        self.assertEqual([a["id"] for a in event["augments"]], [102, 101, 103])
        self.assertIsNone(tracker.update(self.full))
        self.assertIsNone(tracker.update(None))
        self.assertIsNone(tracker.update(None))
        self.assertEqual(tracker.update(None)["type"], "closed")
        self.assertIsNone(tracker.update(None))

    def test_partial_read_does_not_shrink_offer(self) -> None:
        tracker = OfferTracker()
        tracker.update(self.full)
        partial = self.matcher.find_offer(_card_row(["頂狙武器", "XX", "雙刀流"]))
        self.assertIsNone(tracker.update(partial))

    def test_reroll_emits_new_offer(self) -> None:
        tracker = OfferTracker()
        tracker.update(self.full)
        reroll = self.matcher.find_offer(_card_row(["頂狙武器", "靈魂淨化", "雙刀流"]))
        self.assertEqual([a["id"] for a in tracker.update(reroll)["augments"]], [102, 104, 103])

    def test_flicker_does_not_close(self) -> None:
        tracker = OfferTracker(close_after=3)
        tracker.update(self.full)
        tracker.update(None)
        tracker.update(self.full)
        tracker.update(None)
        self.assertIsNone(tracker.update(None))


class FakeOcr:
    def __init__(self, frames: list[list[OcrLine]]) -> None:
        self.frames = frames

    def recognize(self, _image) -> list[OcrLine]:
        return self.frames.pop(0)


class WatcherTest(unittest.TestCase):
    def test_writes_offer_in_bridge_shape_and_skips_unfocused_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            focused = iter([None, 1])
            watcher = AugmentOcrWatcher(
                out, NAMES, ocr=FakeOcr([_card_row(["頂狙武器", "究極九頭蛇", "雙刀流"])]),
                find_window=lambda: next(focused), capture=lambda _hwnd, _band: Frame(object(), 0, 1.0, 1000),
            )
            self.assertIsNone(watcher.scan_once())  # game not focused: no capture, no OCR
            self.assertFalse((out / "latest_augments.json").exists())
            watcher.scan_once()
            record = json.loads((out / "latest_augments.json").read_text(encoding="utf-8"))
            self.assertEqual(record["payload"]["app"], "screen-ocr")
            self.assertEqual([a["name"] for a in record["payload"]["event"]["augments"]],
                             ["頂狙武器", "究極九頭蛇", "雙刀流"])
            self.assertEqual(len((out / "augment_events.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_learns_name_band_then_scans_only_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            asked: list[tuple | None] = []

            def capture(_hwnd, band):
                asked.append(band)
                # wide scan starts at client y=100; a band scan starts at its own top
                top = 100 if band is None else band[0] * 1000
                return Frame(object(), top, 1.0, 1000)

            # names at image y=330 (h=30, cy=345) in a frame starting at client y=100 -> 0.445
            frames = [_card_row(["頂狙武器", "究極九頭蛇", "雙刀流"], y=330)] + [[] for _ in range(FULL_SCAN_EVERY)]
            watcher = AugmentOcrWatcher(out, NAMES, ocr=FakeOcr(frames), find_window=lambda: 1, capture=capture)
            for _ in range(FULL_SCAN_EVERY + 1):
                watcher.scan_once()
            self.assertIsNone(asked[0])  # nothing learned yet: wide scan
            band = asked[1]
            self.assertAlmostEqual((band[0] + band[1]) / 2, 0.445, places=3)
            self.assertAlmostEqual(band[1] - band[0], 2 * BAND_HALF_HEIGHTS * 30 / 1000, places=3)
            self.assertTrue(all(b == band for b in asked[1:FULL_SCAN_EVERY]))
            self.assertIsNone(asked[FULL_SCAN_EVERY])  # periodic wide re-check
            # remembered across restarts
            self.assertEqual(load_band(out), band)
            self.assertEqual(AugmentOcrWatcher(out, NAMES, find_window=lambda: None).band, band)

    def test_bad_layout_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ocr_layout.json").write_text('{"band": [0.9, 0.1]}', encoding="utf-8")
            self.assertIsNone(load_band(Path(tmp)))


@unittest.skipUnless(sys.platform == "win32" and ocr_available(), "needs Windows OCR")
class RealOcrTest(unittest.TestCase):
    def test_synthetic_offer_screen(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        from aram_nn.augment_ocr import WindowsOcr

        try:
            ocr = WindowsOcr()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        title = ImageFont.truetype("C:/Windows/Fonts/msjhbd.ttc", 30)
        body = ImageFont.truetype("C:/Windows/Fonts/msjh.ttc", 18)
        image = Image.new("RGB", (2048, 940), (12, 14, 30))
        draw = ImageDraw.Draw(image)
        for x, name in [(450, "究極九頭蛇"), (1010, "頂狙武器"), (1570, "雙刀流")]:
            draw.rectangle([x - 150, 100, x + 150, 800], fill=(40, 30, 70))
            draw.text((x - 75, 430), name, font=title, fill=(240, 230, 200))
            draw.text((x - 130, 510), "攻擊速度提升 30%，持續 5 秒", font=body, fill=(200, 200, 200))
        offer = OfferMatcher(NAMES).find_offer(ocr.recognize(image))
        ocr.close()
        self.assertEqual([opt.augment_id for opt in offer], [101, 102, 103])


if __name__ == "__main__":
    unittest.main()


def test_mask_rects_blacks_out_only_the_overlap():
    from PIL import Image

    from aram_nn.augment_ocr import mask_rects

    image = Image.new("RGB", (100, 50), (255, 255, 255))
    # Desktop origin of the image is (1000, 200); the window covers its left 30 px
    # and spills outside the image on every other side.
    mask_rects(image, (1000, 200), [(900, 100, 1030, 400), (5000, 5000, 5100, 5100)])
    assert image.getpixel((0, 0)) == (0, 0, 0)
    assert image.getpixel((29, 49)) == (0, 0, 0)
    assert image.getpixel((30, 0)) == (255, 255, 255)
    assert image.getpixel((99, 49)) == (255, 255, 255)


def test_own_window_rects_is_safe_without_windows():
    from aram_nn.augment_ocr import own_window_rects

    assert isinstance(own_window_rects(), list)
