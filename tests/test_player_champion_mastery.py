from __future__ import annotations

import unittest

import polars as pl

from scripts.analyze_player_champion_mastery import (
    build_champion_concentration,
    build_player_champion_stats,
    chronological_backtest,
)


def _participants() -> pl.DataFrame:
    rows = []
    for created_ms in range(1, 11):
        # Player 1 is consistently better on champion 10; player 2 is not.
        rows.extend([
            {"created_ms": created_ms, "pid": 1, "champ": 10, "win": 1 if created_ms <= 8 else 0},
            {"created_ms": created_ms, "pid": 2, "champ": 10, "win": 0 if created_ms <= 8 else 1},
            {"created_ms": created_ms, "pid": 1, "champ": 20, "win": 0 if created_ms <= 8 else 1},
            {"created_ms": created_ms, "pid": 2, "champ": 20, "win": 1 if created_ms <= 8 else 0},
        ])
    return pl.DataFrame(rows)


class PlayerChampionMasteryTests(unittest.TestCase):
    def test_mastery_shrinks_toward_player_baseline(self) -> None:
        stats = build_player_champion_stats(_participants(), k_interaction=10)
        p1c10 = stats.filter((pl.col("pid") == 1) & (pl.col("champ") == 10)).row(0, named=True)
        p1c20 = stats.filter((pl.col("pid") == 1) & (pl.col("champ") == 20)).row(0, named=True)
        self.assertGreater(p1c10["mastery_lift"], p1c20["mastery_lift"])
        self.assertTrue(0 < p1c10["mastery_confidence"] < 1)


    def test_concentration_is_bounded(self) -> None:
        concentration = build_champion_concentration(_participants())
        self.assertEqual(concentration.height, 2)
        self.assertGreaterEqual(concentration["champion_herfindahl"].min(), 0.5)
        self.assertLessEqual(concentration["champion_herfindahl"].max(), 1.0)


    def test_backtest_has_future_rows_and_three_models(self) -> None:
        result = chronological_backtest(_participants(), test_frac=0.2)
        self.assertGreater(result["train_rows"], 0)
        self.assertGreater(result["test_rows"], 0)
        self.assertEqual(set(result["models"]), {"champion_only", "champion_plus_player", "champion_player_mastery", "champion_player_performance_mastery"})
