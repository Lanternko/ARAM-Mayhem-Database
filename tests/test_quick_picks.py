from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aram_nn.quick_picks import QuickPickSource, select_quick_overview

PAYLOAD = {
    "ddv": "16.20.1",
    "detailBase": "api/champions",
    "detailVersion": "v1",
    "itemLut": {
        "1": {"z": "核心一", "e": "Core One"},
        "2": {"z": "核心二", "e": "Core Two"},
        "3": {"e": "Third"},
        "4": {"z": "第四件"},
        "9": {"z": "鞋"},
    },
    "augs": {
        "100": {"name_zh": "銀增幅", "icon": "assets/icons/silver_large.png"},
        "200": {"name": "Gold Aug", "icon": "assets/icons/gold_large.png"},
    },
}


def _rows(*ids):
    return [{"id": i, "ic": 1} for i in ids]


def _detail(**group_overrides):
    group = {
        "core": _rows(1, 2),
        "g": 900,
        "wr": 0.53,
        "pick": 0.4,
        "completion": _rows(1, 2, 9, 3, 4),
        "completionGames": 120,
        "routeBoot": {"id": 9, "ic": 1, "g": 700, "wr": 0.52},
    }
    group.update(group_overrides)
    return {
        "itemClusters": {"groups": [group, {"core": _rows(3, 4), "g": 50, "wr": 0.5, "pick": 0.1}]},
        "spells": {"top": [{"items": [
            {"id": 4, "name_zh": "閃現", "icon": "https://example.test/flash.png"},
        ]}]},
        "top": {
            "kSilver": [
                {"id": 100, "wr": 0.61, "lift": 0.05},
                {"id": 999, "wr": 0.7},  # not in payload["augs"]
                {"id": 100, "wr": None},
            ],
            "kGold": [{"id": 200, "wr": 0.55}],
        },
    }


class SelectQuickOverviewTests(unittest.TestCase):
    def test_complete_build_shows_completion_and_marks_core(self) -> None:
        ov = select_quick_overview(7, _detail(), PAYLOAD)

        self.assertTrue(ov.complete)
        self.assertEqual([i.id for i in ov.items], [1, 2, 9, 3, 4])
        self.assertEqual([i.is_core for i in ov.items], [True, True, False, False, False])
        self.assertEqual([i.name for i in ov.items], ["核心一", "核心二", "鞋", "Third", "第四件"])
        self.assertEqual(ov.items[0].icon, "https://ddragon.leagueoflegends.com/cdn/16.20.1/img/item/1.png")
        self.assertEqual((ov.core_size, ov.core_wr, ov.core_games), (2, 0.53, 900))
        self.assertEqual((ov.boot.id, ov.boot_wr, ov.boot_games), (9, 0.52, 700))
        self.assertEqual(ov.completion_games, 120)

    def test_falls_back_to_core_when_completion_is_not_usable(self) -> None:
        cases = {
            "no games": {"completionGames": 0},
            "too short": {"completion": _rows(1, 2, 9)},
            "different core": {"completion": _rows(1, 3, 9, 4, 5)},
            "boots missing from build": {"completion": _rows(1, 2, 3, 4, 5)},
            "no boots": {"routeBoot": None},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                ov = select_quick_overview(7, _detail(**overrides), PAYLOAD)
                self.assertFalse(ov.complete)
                self.assertEqual([i.id for i in ov.items], [1, 2])

    def test_quick_core_overrides_route_core(self) -> None:
        ov = select_quick_overview(
            7, _detail(quickCore={"items": _rows(1, 2, 3), "g": 300, "wr": 0.56}), PAYLOAD,
        )

        self.assertEqual((ov.core_size, ov.core_wr, ov.core_games), (3, 0.56, 300))
        self.assertEqual([i.is_core for i in ov.items], [True, True, False, True, False])

    def test_win_rate_hidden_without_games(self) -> None:
        ov = select_quick_overview(7, _detail(g=0), PAYLOAD)
        self.assertIsNone(ov.core_wr)

    def test_routes_and_out_of_range_route(self) -> None:
        ov = select_quick_overview(7, _detail(), PAYLOAD, route=5)

        self.assertEqual(ov.route, 1)
        self.assertEqual([r.label for r in ov.routes], ["核心一 + 核心二", "Third + 第四件"])
        self.assertEqual([r.pick for r in ov.routes], [0.4, 0.1])
        self.assertEqual([i.id for i in ov.items], [3, 4])

    def test_spells_and_augments(self) -> None:
        ov = select_quick_overview(7, _detail(), PAYLOAD)

        self.assertEqual([(s.name, s.icon) for s in ov.spells], [("閃現", "https://example.test/flash.png")])
        self.assertEqual([(a.id, a.name, a.wr, a.lift) for a in ov.augments["kSilver"]], [(100, "銀增幅", 0.61, 0.05)])
        self.assertEqual([a.name for a in ov.augments["kGold"]], ["Gold Aug"])
        self.assertEqual(ov.augments["kPrismatic"], ())

    def test_empty_detail(self) -> None:
        ov = select_quick_overview(7, {}, PAYLOAD)

        self.assertEqual((ov.items, ov.routes, ov.spells, ov.boot), ((), (), (), None))
        self.assertFalse(ov.complete)


class QuickPickSourceTests(unittest.TestCase):
    def test_reads_shard_and_icons_from_local_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            (root / "api" / "champions").mkdir(parents=True)
            (root / "api" / "champions" / "7.json").write_text(json.dumps(_detail()), encoding="utf-8")
            (root / "assets" / "icons").mkdir(parents=True)
            icon = root / "assets" / "icons" / "silver_large.png"
            icon.write_bytes(b"png")
            source = QuickPickSource(PAYLOAD, Path(tmp) / "cache", local_root=root)

            self.assertTrue(source.overview(7).complete)
            self.assertEqual(source.icon_path("assets/icons/silver_large.png", fetch=False), icon)
            # Not on disk and not allowed to download: the Tk thread gets None, not a stall.
            self.assertIsNone(source.icon_path("https://example.test/flash.png", fetch=False))
            self.assertIsNone(source.icon_path("", fetch=False))


if __name__ == "__main__":
    unittest.main()
