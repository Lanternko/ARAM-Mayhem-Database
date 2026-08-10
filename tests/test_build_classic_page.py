"""Focused contracts for the standalone Classic research-page builder."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_classic_page", ROOT / "scripts" / "build_classic_page.py"
)
assert SPEC and SPEC.loader
classic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classic)


class ClassicResearchPageTests(unittest.TestCase):
    def test_jade_item_namespace_maps_to_standard_catalogue(self) -> None:
        self.assertEqual(classic.base_item_id(773006), 3006)
        self.assertEqual(classic.base_item_id(771055), 1055)
        self.assertEqual(classic.base_item_id(3006), 3006)

    def test_components_stay_out_of_complete_item_filter(self) -> None:
        self.assertEqual(
            classic.classify_item({"item_id": 3086, "upgrades": True, "price_total": 1200}),
            "starter",
        )
        self.assertEqual(
            classic.classify_item({"item_id": 3031, "upgrades": False, "price_total": 3500}),
            "complete",
        )
        self.assertEqual(
            classic.classify_item({"item_id": 3340, "categories": ["Trinket"]}),
            "trinket",
        )

    def test_public_page_contains_interactive_research_surfaces_and_metadata(self) -> None:
        heroes = classic.build_rows(
            {1: 120},
            {1: 70},
            12,
            {
                1: {
                    "alias": "Jade_Annie",
                    "name_zh": "安妮",
                    "name_zh_cn": "安妮",
                    "name_en": "Annie",
                    "title_zh": "黑暗之女",
                    "title_zh_cn": "黑暗之女",
                    "title_en": "the Dark Child",
                    "image": "assets/icons/classic/60001.png",
                }
            },
        )
        item_meta = {
            3006: {
                "item_id": 3006,
                "name_zh": "狂戰士護脛",
                "name_zh_cn": "狂战士胫甲",
                "name_en": "Berserker's Greaves",
                "categories": ["Boots"],
                "price_total": 1100,
                "upgrades": False,
                "image": "assets/icons/classic-items/3006.png",
            }
        }
        items = classic.build_item_rows({3006: 80}, {3006: 46}, 12, item_meta)
        classic.attach_hero_items(
            heroes,
            {(1, 3006): 80},
            {(1, 3006): 46},
            {},
            {},
            item_meta,
        )
        classic.attach_hero_position_profiles(
            heroes,
            {(1, "MIDDLE"): 120},
            {(1, "MIDDLE"): 72},
            {(1, "MIDDLE", 3006): 80},
            {(1, "MIDDLE", 3006): 48},
            {},
            {},
            item_meta,
        )

        page = classic.render_research_preview(heroes, items, 12, {"16.15.800": 12}, 0)

        self.assertIn("id='classic-data'", page)
        self.assertIn("英雄 Tier", page)
        self.assertIn("id='hero-detail'", page)
        self.assertIn("data-detail-position", page)
        self.assertIn('"MIDDLE"', page)
        self.assertIn("終局持有者勝率", page)
        self.assertIn("index,follow,max-image-preview:large", page)
        self.assertIn("rel='canonical' href='https://arammeta.com/classic.html'", page)
        self.assertIn("name='description'", page)
        self.assertNotIn("noindex,nofollow", page)
        self.assertNotIn("經典模式 · 預覽", page)
        self.assertNotIn("95% CI", page)

        simplified = classic.render_research_preview(
            heroes, items, 12, {"16.15.800": 12}, 0, locale="zh-Hans"
        )
        self.assertIn("<html lang='zh-Hans'>", simplified)
        self.assertIn("rel='canonical' href='https://arammeta.com/zh-CN/classic.html'", simplified)
        self.assertIn("经典模式英雄胜率", simplified)
        self.assertIn("狂战士胫甲", simplified)
        self.assertIn("hreflang='en' href='https://arammeta.com/en/classic.html'", simplified)

        english = classic.render_research_preview(
            heroes, items, 12, {"16.15.800": 12}, 0, locale="en"
        )
        self.assertIn("<html lang='en'>", english)
        self.assertIn("rel='canonical' href='https://arammeta.com/en/classic.html'", english)
        self.assertIn("Classic Mode champion win rates", english)
        self.assertIn("Berserker's Greaves", english)
        self.assertIn("Data and limitations", english)
        self.assertNotIn("資料與限制", english)
        self.assertIn("toLocaleString('en-US')", english)
        self.assertIn("Switch win rate and items by position", english)


if __name__ == "__main__":
    unittest.main()
