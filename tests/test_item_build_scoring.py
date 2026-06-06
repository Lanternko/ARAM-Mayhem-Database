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

    def test_item_build_section_renders_three_ranked_recommendations(self) -> None:
        source = (ROOT / "scripts" / "build_tier_list.py").read_text(encoding="utf-8")

        self.assertIn("const bootInfo = info.boots || {}", source)
        self.assertIn("buildDetailTabSet('main'", source)
        self.assertIn("buildDetailTabSet('items'", source)
        self.assertIn("label: itemTabLabels.routes", source)
        self.assertIn("label: itemTabLabels.single", source)
        self.assertIn("label: itemTabLabels.pairs", source)
        self.assertIn("label: itemTabLabels.boots", source)

    def test_boot_selector_keeps_only_recommendable_boots(self) -> None:
        item_meta = {
            101: {"id": 101, "categories": ["Damage"], "price_total": 3000},
            2051: {"id": 2051, "categories": ["Health", "Lane"], "price_total": 950},
            301: {"id": 301, "categories": ["Boots"], "price_total": 1100},
            302: {"id": 302, "categories": ["Boots"], "price_total": 300},
            223069: {"id": 223069, "categories": ["Boots"], "price_total": 6000},
        }

        self.assertEqual(
            tier_list._participant_boot_item_ids([101, 301, 302, 301, 223069], item_meta),
            [301],
        )
        self.assertEqual(
            tier_list._participant_recommendable_item_ids([101, 2051, 301, 302, 223069], item_meta),
            [101, 2051],
        )
        self.assertEqual(
            tier_list._participant_core_item_ids([101, 2051, 301, 302, 223069], item_meta),
            [101],
        )

    def test_item_build_clusters_keep_hybrid_routes_separate(self) -> None:
        item_meta = {
            101: {"id": 101, "name": "Damage A", "name_zh": "Damage A", "name_en": "Damage A", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            102: {"id": 102, "name": "Damage B", "name_zh": "Damage B", "name_en": "Damage B", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            103: {"id": 103, "name": "Damage C", "name_zh": "Damage C", "name_en": "Damage C", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            104: {"id": 104, "name": "Damage D", "name_zh": "Damage D", "name_en": "Damage D", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            105: {"id": 105, "name": "Damage E", "name_zh": "Damage E", "name_en": "Damage E", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            201: {"id": 201, "name": "Tank A", "name_zh": "Tank A", "name_en": "Tank A", "categories": ["Health", "Armor"], "price_total": 3000, "icon": ""},
            202: {"id": 202, "name": "Tank B", "name_zh": "Tank B", "name_en": "Tank B", "categories": ["Health", "Armor"], "price_total": 3000, "icon": ""},
            203: {"id": 203, "name": "Tank C", "name_zh": "Tank C", "name_en": "Tank C", "categories": ["Health", "Armor"], "price_total": 3000, "icon": ""},
            204: {"id": 204, "name": "Tank D", "name_zh": "Tank D", "name_en": "Tank D", "categories": ["Health", "Armor"], "price_total": 3000, "icon": ""},
            205: {"id": 205, "name": "Tank E", "name_zh": "Tank E", "name_en": "Tank E", "categories": ["Health", "Armor"], "price_total": 3000, "icon": ""},
            301: {"id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots", "categories": ["Boots"], "price_total": 1100, "icon": ""},
            223069: {"id": 223069, "name": "Void Immolation", "name_zh": "Void Immolation", "name_en": "Void Immolation", "categories": ["Health", "Armor"], "price_total": 6000, "icon": ""},
        }
        single_item_affinity = {
            1: {
                "top": [
                    {
                        "slug": str(item_id),
                        "rank_score": 0.05,
                        "lift": 0.04,
                        "avg_lift": 0.02,
                    }
                    for item_id in item_meta
                    if item_id not in {301, 223069}
                ],
                "bot": [],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER, participants_json TEXT)"
                )
                for idx in range(30):
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < 24 else 0,
                            json.dumps([{
                                "championId": 1,
                                "teamId": 100,
                                "items": [101, 102, 103, 104, 105, 223069, 301],
                            }]),
                        ),
                    )
                for idx in range(30):
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < 21 else 0,
                            json.dumps([{
                                "championId": 1,
                                "teamId": 100,
                                "items": [201, 202, 203, 204, 205, 223069, 301],
                            }]),
                        ),
                    )
                con.commit()
            finally:
                con.close()

            clusters = tier_list.compute_champ_item_build_clusters(
                db_path,
                2400,
                "16.10",
                item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}],
                single_item_affinity,
                min_pair_games=10,
                min_games=10,
                max_items=6,
                top_n=4,
            )

        rows = clusters[1]["top"]
        route_sets = {
            frozenset(int(item["id"]) for item in row["items"])
            for row in rows
        }
        self.assertIn(frozenset({101, 102, 103, 104, 105, 301}), route_sets)
        self.assertIn(frozenset({201, 202, 203, 204, 205, 301}), route_sets)
        self.assertFalse(any(223069 in route for route in route_sets))
        self.assertLessEqual(max(len(row["items"]) for row in rows), 6)

    def test_item_build_clusters_require_observed_exact_six_item_route(self) -> None:
        item_meta = {
            101: {"id": 101, "name": "A", "name_zh": "A", "name_en": "A", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            102: {"id": 102, "name": "B", "name_zh": "B", "name_en": "B", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            103: {"id": 103, "name": "C", "name_zh": "C", "name_en": "C", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            104: {"id": 104, "name": "D", "name_zh": "D", "name_en": "D", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            105: {"id": 105, "name": "E", "name_zh": "E", "name_en": "E", "categories": ["Damage"], "price_total": 3000, "icon": ""},
            301: {"id": 301, "name": "Boots", "name_zh": "Boots", "name_en": "Boots", "categories": ["Boots"], "price_total": 1100, "icon": ""},
        }
        single_item_affinity = {
            1: {
                "top": [
                    {"slug": str(item_id), "rank_score": 0.05, "lift": 0.04, "avg_lift": 0.02}
                    for item_id in (101, 102, 103, 104, 105)
                ],
                "bot": [],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER, participants_json TEXT)"
                )
                partial_routes = [
                    [101, 102, 103, 104, 301],
                    [101, 102, 103, 105, 301],
                ]
                for idx in range(40):
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < 30 else 0,
                            json.dumps([{
                                "championId": 1,
                                "teamId": 100,
                                "items": partial_routes[idx % len(partial_routes)],
                            }]),
                        ),
                    )
                con.commit()
            finally:
                con.close()

            clusters = tier_list.compute_champ_item_build_clusters(
                db_path,
                2400,
                "16.10",
                item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}],
                single_item_affinity,
                min_pair_games=5,
                min_games=5,
                max_items=6,
                top_n=4,
            )

        self.assertNotIn(1, clusters)

    def test_item_build_clusters_rank_late_items_by_stability(self) -> None:
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
            for item_id in (101, 102, 103, 104, 105, 106, 107)
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
        single_item_affinity = {
            1: {
                "top": [
                    {"slug": str(item_id), "rank_score": 0.04, "lift": 0.02, "avg_lift": 0.01}
                    for item_id in (101, 102, 103, 104, 105, 106, 107)
                ],
                "bot": [],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "games.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER, participants_json TEXT)"
                )
                # Small high-win flex branch: enough single-item evidence, but few exact six-item games.
                for idx in range(20):
                    exact = idx < 5
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < 13 else 0,
                            json.dumps([{
                                "championId": 1,
                                "teamId": 100,
                                "items": [101, 102, 103, 105, 107, 301] if exact else [101, 102, 103, 105, 107],
                            }]),
                        ),
                    )
                # Stable flex branch: more exact route evidence and thicker co-build support.
                for idx in range(30):
                    con.execute(
                        "INSERT INTO games VALUES (?, ?, ?, ?)",
                        (
                            2400,
                            "16.10",
                            1 if idx < 18 else 0,
                            json.dumps([{
                                "championId": 1,
                                "teamId": 100,
                                "items": [101, 102, 103, 104, 106, 301],
                            }]),
                        ),
                    )
                con.commit()
            finally:
                con.close()

            clusters = tier_list.compute_champ_item_build_clusters(
                db_path,
                2400,
                "16.10",
                item_meta,
                [{"champion_id": 1, "raw_wr": 0.5}],
                single_item_affinity,
                min_pair_games=3,
                min_games=5,
                max_items=6,
                top_n=4,
            )

        top_route = frozenset(int(item["id"]) for item in clusters[1]["top"][0]["items"])
        self.assertEqual(top_route, frozenset({101, 102, 103, 104, 106, 301}))
        self.assertGreaterEqual(clusters[1]["top"][0]["exact_games"], 30)

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


if __name__ == "__main__":
    unittest.main()
