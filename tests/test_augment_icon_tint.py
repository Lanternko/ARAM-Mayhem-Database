from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tierlist_render as render  # noqa: E402


class TintGrayAugmentIconsTest(unittest.TestCase):
    def test_gray_small_icons_get_rarity_tinted_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            Image.new("LA", (64, 64), (255, 255)).save(assets / "bees_small.png")
            payload = {
                "augs": {
                    "1": {"icon": "assets/icons/bees_small.png", "rarity": "kGold"},
                    "2": {"icon": "assets/icons/done_large.png", "rarity": "kGold"},
                },
                "patchChanges": [{"icon": "assets/icons/bees_small.png", "rarity": "kPrismatic"}],
            }

            count = render.tint_gray_augment_icons(payload, assets)

            self.assertEqual(count, 2)
            self.assertEqual(payload["augs"]["1"]["icon"], "assets/icons/bees_gold_tint1.png")
            self.assertEqual(payload["augs"]["2"]["icon"], "assets/icons/done_large.png")
            self.assertEqual(payload["patchChanges"][0]["icon"], "assets/icons/bees_prismatic_tint1.png")
            with Image.open(assets / "bees_gold_tint1.png") as tinted:
                self.assertEqual(tinted.mode, "RGBA")
                r, g, b, _ = tinted.getpixel((128, 128))
                self.assertGreater(r, b)

    def test_missing_source_keeps_gray_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"augs": {"1": {"icon": "assets/icons/gone_small.png", "rarity": "kSilver"}}}
            self.assertEqual(render.tint_gray_augment_icons(payload, Path(tmp)), 0)
            self.assertEqual(payload["augs"]["1"]["icon"], "assets/icons/gone_small.png")


if __name__ == "__main__":
    unittest.main()
