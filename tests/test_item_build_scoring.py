from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_tier_list as tier_list  # noqa: E402


def item_pair_pick_credit(pick_rate: float) -> float:
    credit = tier_list.ITEM_PAIR_PICK_RATE_WEIGHT * math.log1p(
        pick_rate / tier_list.ITEM_PAIR_PICK_RATE_REF
    )
    return min(tier_list.ITEM_PAIR_PICK_RATE_CAP, credit)


class ItemBuildScoringTests(unittest.TestCase):
    def test_low_pick_item_pair_can_surface_when_score_eligible(self) -> None:
        cs_games = Counter({(1, "secret"): 30, (1, "staple"): 1500})
        cs_wins = Counter({(1, "secret"): 30, (1, "staple"): 780})
        cs_baseline_games = Counter({(1, "secret"): 15.0, (1, "staple"): 750.0})
        champ_total_games = Counter({1: 6000})
        category_games = Counter({"secret": 30, "staple": 1500})
        category_wins = Counter({"secret": 30, "staple": 780})
        category_baseline_games = Counter({"secret": 15.0, "staple": 750.0})
        category_names = {
            "secret": {"name": "Secret OP", "name_zh": "Secret OP", "name_en": "Secret OP"},
            "staple": {"name": "Staple", "name_zh": "Staple", "name_en": "Staple"},
        }

        rows = tier_list._finalize_category_affinity(
            cs_games,
            cs_wins,
            cs_baseline_games,
            champ_total_games,
            category_games,
            category_wins,
            category_baseline_games,
            category_names,
            min_games=30,
            top_n=4,
            bot_n=0,
            rank_mode="lift",
            top_min_lift=-1.0,
        )[1]["top"]

        secret = next(row for row in rows if row["slug"] == "secret")
        self.assertAlmostEqual(secret["pick_rate"], 0.005)

    def test_popular_staple_can_beat_niche_high_lift_pair(self) -> None:
        niche_high_lift_score = 0.04 + item_pair_pick_credit(0.005)
        staple_modest_lift_score = 0.01 + item_pair_pick_credit(0.25)

        self.assertGreater(staple_modest_lift_score, niche_high_lift_score)

    def test_popular_bad_rows_skip_antiheal_items(self) -> None:
        cs_games = Counter({
            (1, "imperial"): 300,
            (1, "morello"): 320,
            (1, "malignance"): 180,
            (1, "despair"): 260,
        })
        cs_wins = Counter({
            (1, "imperial"): 120,
            (1, "morello"): 110,
            (1, "malignance"): 65,
            (1, "despair"): 129,
        })
        cs_baseline_games = Counter({
            (1, "imperial"): 150.0,
            (1, "morello"): 160.0,
            (1, "malignance"): 90.0,
            (1, "despair"): 130.0,
        })
        champ_total_games = Counter({1: 900})
        category_games = Counter({"imperial": 300, "morello": 320, "malignance": 180, "despair": 260})
        category_wins = Counter({"imperial": 120, "morello": 110, "malignance": 65, "despair": 129})
        category_baseline_games = Counter({"imperial": 150.0, "morello": 160.0, "malignance": 90.0, "despair": 130.0})
        category_names = {
            "imperial": {"name": "Imperial Mandate", "name_zh": "帝王命令", "name_en": "Imperial Mandate"},
            "morello": {"name": "Morellonomicon", "name_zh": "黑魔禁書", "name_en": "Morellonomicon"},
            "malignance": {"name": "Malignance", "name_zh": "惡意", "name_en": "Malignance"},
            "despair": {"name": "Unending Despair", "name_zh": "無盡絕望", "name_en": "Unending Despair"},
        }

        rows = tier_list._finalize_category_affinity(
            cs_games,
            cs_wins,
            cs_baseline_games,
            champ_total_games,
            category_games,
            category_wins,
            category_baseline_games,
            category_names,
            min_games=30,
            top_n=0,
            bot_n=0,
            rank_mode="lift",
            top_min_lift=-0.02,
            popular_bad_n=3,
        )[1]["popular_bad"]

        names = [row["name_en"] for row in rows]
        self.assertIn("Imperial Mandate", names)
        self.assertIn("Malignance", names)
        self.assertNotIn("Morellonomicon", names)
        self.assertNotIn("Unending Despair", names)

    def test_site_js_renders_item_build_sections(self) -> None:
        # Frontend JS lives in scripts/templates/site.js since the 2026-06-30
        # template extraction (render_html injects it via _read_site_template).
        source = (ROOT / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")

        self.assertIn("const bootInfo = info.boots || {}", source)
        self.assertIn("const itemInfo = info.items || {}", source)
        self.assertIn("const singleItemInfo = info.singleItems || {}", source)
        self.assertIn("const itemClusterInfo = info.itemClusters || {}", source)
        self.assertIn("buildDetailTabSet('main'", source)
        self.assertIn("label: mainTabLabels.items", source)
        # Negative-lift items with pick ≥ 10% must survive the common-trap slice.
        self.assertIn("COMMON_TRAP_FORCE_MIN_PICK = 0.10", source)
        self.assertIn("Math.max(maxRows, mustKeep.length)", source)

    def test_boot_selector_keeps_only_recommendable_boots(self) -> None:
        item_meta = {
            101: {"id": 101, "categories": ["Damage"], "price_total": 3000},
            2051: {"id": 2051, "categories": ["Health", "Lane"], "price_total": 950},
            3916: {"id": 3916, "name_en": "Oblivion Orb", "categories": ["MagicPenetration"], "price_total": 800},
            3076: {"id": 3076, "name_en": "Bramble Vest", "categories": ["Armor"], "price_total": 800},
            3123: {"id": 3123, "name_en": "Executioner's Calling", "categories": ["CriticalStrike"], "price_total": 800},
            1037: {"id": 1037, "name_en": "Pickaxe", "categories": ["Damage"], "price_total": 875},
            301: {"id": 301, "categories": ["Boots"], "price_total": 1100},
            302: {"id": 302, "categories": ["Boots"], "price_total": 300},
            223069: {"id": 223069, "categories": ["Boots"], "price_total": 6000},
        }

        self.assertEqual(
            tier_list._participant_boot_item_ids([101, 301, 302, 301, 223069], item_meta),
            [301],
        )
        self.assertEqual(
            tier_list._participant_recommendable_item_ids(
                [101, 2051, 3916, 3076, 3123, 1037, 301, 302, 223069],
                item_meta,
            ),
            [101, 2051, 3916, 3076, 3123],
        )
        self.assertEqual(
            tier_list._participant_core_item_ids([101, 2051, 3916, 3076, 3123, 301, 302, 223069], item_meta),
            [101],
        )

    def _build_games_db(self, db_path: Path, builds: list[tuple[list[int], int, int]]) -> None:
        """builds: list of (item_ids, copies, wins) for champion 1 on blue side."""
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER, participants_json TEXT)"
            )
            for items, copies, wins in builds:
                for idx in range(copies):
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < wins else 0,
                            json.dumps([{"championId": 1, "teamId": 100, "items": items}]),
                        ),
                    )
            con.commit()
        finally:
            con.close()

    def test_core_build_group_options_split_popular_and_winrate(self) -> None:
        item_meta = {
            item_id: {
                "id": item_id, "name": f"Item {item_id}", "name_zh": f"Item {item_id}",
                "name_en": f"Item {item_id}", "categories": ["Damage"], "price_total": 3000, "icon": "",
            }
            for item_id in (101, 102, 103, 104, 201, 202)
        }
        item_meta[301] = {
            "id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots",
            "categories": ["Boots"], "price_total": 1100, "icon": "",
        }
        # Shared rush 101 -> 102 (seen even in short games).  The 3rd item is the
        # real choice: 103 popular (~50% WR), 104 a niche high-win-rate option.
        # 201/202 only show up in completed builds (built late).
        builds = [
            ([101, 102, 301], 30, 15),
            ([101, 102, 103, 301], 30, 15),
            ([101, 102, 103, 201, 202, 301], 25, 12),
            ([101, 102, 104, 301], 12, 11),
            ([101, 102, 104, 201, 202, 301], 10, 8),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            self._build_games_db(db_path, builds)
            clusters = tier_list.compute_champ_item_build_clusters(
                db_path, 2400, "16.10", item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}], {},
                core_min_games=10, min_confirm_games=3, winrate_min_games=10,
                min_games=10, top_n=4,
            )

        groups = clusters[1]["groups"]
        self.assertTrue(groups)
        grp = groups[0]
        self.assertEqual(grp["core_ids"], (101, 102))  # shared rush, in build order
        self.assertEqual([int(it["id"]) for it in grp["core_items"]], [101, 102])
        opt_by_id = {int(o["id"]): o for o in grp["options"]}
        self.assertIn(103, opt_by_id)
        self.assertIn(104, opt_by_id)
        self.assertEqual(opt_by_id[103]["lane"], "popular")  # most-picked 3rd item
        self.assertEqual(opt_by_id[104]["lane"], "winrate")  # high-LCB 3rd item
        self.assertGreater(opt_by_id[104]["smoothed_wr"], opt_by_id[103]["smoothed_wr"])
        # 2026-06-17: options carry ALL pairing items clearing the pick-or-winrate
        # bar (搭配裝備), not just 3rd items — late items appear, but the ranked
        # popular/winrate lanes stay reserved for genuine 3rd-item picks.
        self.assertIn(201, opt_by_id)
        self.assertEqual(opt_by_id[201]["lane"], "")

    def test_core_build_keyed_on_core2_rush_without_completion(self) -> None:
        item_meta = {
            item_id: {
                "id": item_id, "name": f"Item {item_id}", "name_zh": f"Item {item_id}",
                "name_en": f"Item {item_id}", "categories": ["Damage"], "price_total": 3000, "icon": "",
            }
            for item_id in (101, 102, 103)
        }
        item_meta[301] = {
            "id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots",
            "categories": ["Boots"], "price_total": 1100, "icon": "",
        }
        # 2026-06-17: build cards are keyed on the core-2 rush observed in build
        # order — a frequent (101,102) rush yields a card even when no game ever
        # completes 6 items (the old core-3-from-completed-builds rule is gone).
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            self._build_games_db(db_path, [([101, 102, 103, 301], 40, 24)])
            clusters = tier_list.compute_champ_item_build_clusters(
                db_path, 2400, "16.10", item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}], {},
                core_min_games=10, min_confirm_games=3, winrate_min_games=10,
                min_games=10, top_n=4,
            )
        self.assertIn(1, clusters)
        groups = clusters[1]["groups"]
        self.assertTrue(groups)
        self.assertEqual(groups[0]["core_ids"], (101, 102))
        self.assertIn(103, {int(o["id"]) for o in groups[0]["options"]})

    def test_core_build_drops_oversized_item_beyond_six_slots(self) -> None:
        item_meta = {
            item_id: {
                "id": item_id, "name": f"Item {item_id}", "name_zh": f"Item {item_id}",
                "name_en": f"Item {item_id}", "categories": ["Damage"], "price_total": 3000, "icon": "",
            }
            for item_id in (101, 102, 103, 104, 105)
        }
        item_meta[223069] = {
            "id": 223069, "name": "Void Immolation", "name_zh": "Void Immolation",
            "name_en": "Void Immolation", "categories": ["Health", "Armor"], "price_total": 6000, "icon": "",
        }
        item_meta[301] = {
            "id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots",
            "categories": ["Boots"], "price_total": 1100, "icon": "",
        }
        single_item_affinity = {
            1: {
                "top": [
                    {"slug": str(item_id), "rank_score": 0.1, "lift": 0.05, "avg_lift": 0.02}
                    for item_id in (101, 102, 103, 104, 105)
                ],
                "bot": [],
            }
        }
        # 7-item builds (over the 6-slot cap); the low-affinity oversized item must be dropped.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            self._build_games_db(db_path, [([101, 102, 103, 104, 105, 223069, 301], 40, 24)])
            clusters = tier_list.compute_champ_item_build_clusters(
                db_path, 2400, "16.10", item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}], single_item_affinity,
                core_min_games=10, min_confirm_games=3, winrate_min_games=10,
                min_games=10, top_n=4,
            )
        groups = clusters[1]["groups"]
        self.assertTrue(groups)
        shown_ids = set()
        for grp in groups:
            shown_ids.update(int(it["id"]) for it in grp["core_items"])
            shown_ids.update(int(o["id"]) for o in grp["options"])
            shown_ids.update(int(it["id"]) for it in grp["tail_items"])
        self.assertNotIn(223069, shown_ids)  # oversized item dropped beyond the 6-slot cap

    def test_item_build_cluster_selection_prefers_distinct_routes(self) -> None:
        item_meta = {
            item_id: {
                "id": item_id,
                "name": f"Item {item_id}",
                "name_zh": f"Item {item_id}",
                "name_en": f"Item {item_id}",
                "categories": ["Damage"],
                "price_total": 3000,
                "icon": "",
            }
            for item_id in (101, 102, 103, 104, 105, 106, 107, 108, 109, 110)
        }
        item_meta[301] = {
            "id": 301,
            "name": "Boots",
            "name_zh": "Boots",
            "name_en": "Boots",
            "categories": ["Boots"],
            "price_total": 1100,
            "icon": "",
        }
        rows = [
            {
                "name_en": "Crit / AD bruiser",
                "items": [{"id": item_id} for item_id in (101, 102, 103, 104, 105, 301)],
                "rank_score": 0.90,
            },
            {
                "name_en": "AD bruiser / Crit",
                "items": [{"id": item_id} for item_id in (101, 102, 103, 104, 106, 301)],
                "rank_score": 0.88,
            },
            {
                "name_en": "Tank / AD bruiser",
                "items": [{"id": item_id} for item_id in (101, 102, 107, 108, 109, 301)],
                "rank_score": 0.80,
            },
            {
                "name_en": "Lethality assassin",
                "items": [{"id": item_id} for item_id in (102, 103, 107, 108, 110, 301)],
                "rank_score": 0.72,
            },
        ]

        selected = tier_list._select_diverse_item_cluster_rows(rows, item_meta, top_n=4)

        self.assertEqual(len(selected), 3)
        selected_sets = [
            frozenset(int(item["id"]) for item in row["items"])
            for row in selected
        ]
        self.assertIn(frozenset({101, 102, 103, 104, 105, 301}), selected_sets)
        self.assertIn(frozenset({101, 102, 107, 108, 109, 301}), selected_sets)
        self.assertIn(frozenset({102, 103, 107, 108, 110, 301}), selected_sets)
        self.assertNotIn(frozenset({101, 102, 103, 104, 106, 301}), selected_sets)

    def test_core_uses_build_order_from_short_games(self) -> None:
        item_meta = {
            item_id: {
                "id": item_id, "name": f"Item {item_id}", "name_zh": f"Item {item_id}",
                "name_en": f"Item {item_id}", "categories": ["Damage"], "price_total": 3000, "icon": "",
            }
            for item_id in (101, 102, 103, 201, 999)
        }
        item_meta[301] = {
            "id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots",
            "categories": ["Boots"], "price_total": 1100, "icon": "",
        }
        # 101,102 appear even in 2-item games (rushed); 103 in 3-item games;
        # 201 and 999 only ever appear in completed 6-item builds (built last),
        # despite 999 being frequent overall.
        builds = [
            ([101, 102, 301], 50, 25),
            ([101, 102, 103, 301], 30, 15),
            ([101, 102, 103, 201, 999, 301], 40, 22),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            self._build_games_db(db_path, builds)
            clusters = tier_list.compute_champ_item_build_clusters(
                db_path, 2400, "16.10", item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}], {},
                core_min_games=10, min_confirm_games=3, winrate_min_games=10,
                min_games=10, top_n=4,
            )
        groups = clusters[1]["groups"]
        self.assertTrue(groups)
        grp = groups[0]
        # The shared rush is the earliest-built pair (101,102); 103 is the 3rd
        # item; the frequent-but-late 999 is never part of the core rush.  Since
        # 2026-06-17 late items DO appear as lane-less pairing options (搭配裝備)
        # when they clear the pick-or-winrate bar, but the ranked "popular" lane
        # stays with the genuine 3rd item.
        self.assertEqual(grp["core_ids"], (101, 102))
        self.assertEqual([int(it["id"]) for it in grp["core_items"]], [101, 102])
        opt_by_id = {int(o["id"]): o for o in grp["options"]}
        self.assertEqual(opt_by_id[103]["lane"], "popular")
        self.assertNotIn(999, {int(it["id"]) for it in grp["core_items"]})
        self.assertIn(999, opt_by_id)
        self.assertEqual(opt_by_id[999]["lane"], "")

    def test_display_patch_prefix_only_changes_public_label(self) -> None:
        self.assertEqual(tier_list.display_patch_prefix("16.11"), "26.11")
        self.assertEqual(tier_list.display_patch_prefix("16.11.1"), "26.11.1")
        self.assertEqual(tier_list.display_patch_prefix("26.11"), "26.11")
        self.assertIsNone(tier_list.display_patch_prefix(None))


if __name__ == "__main__":
    unittest.main()
