from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
import sys
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

        self.assertIn(
            "buildAffinitySection(copy.itemSectionTitle, copy.itemSectionMeta, itemInfo, { minRows: 3, maxRows: 3 })",
            source,
        )


if __name__ == "__main__":
    unittest.main()
