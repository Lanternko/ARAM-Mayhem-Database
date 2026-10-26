from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_tier_list as tier_list  # noqa: E402
import tierlist_engine as engine  # noqa: E402

AUG = tier_list.DESTINY_AUGMENT_ID


def _write_games(db_path: Path, observations: list[tuple[int, int, bool, bool]]) -> None:
    """One minimal game per (champion, item, has_augment, won) observation.

    ``compute_champ_destiny_items`` only reads participant identity, augments,
    items and the win flag, so a one-participant row per observation is enough to
    exercise the counting and the selection rule.
    """
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER,"
        " participants_json TEXT)"
    )
    for champion_id, item_id, has_augment, won in observations:
        participant = {
            "championId": champion_id,
            "teamId": 100,
            "augments": [AUG] if has_augment else [],
            "items": [item_id],
        }
        con.execute(
            "INSERT INTO games VALUES (2400, '16.21.1', ?, ?)",
            (1 if won else 0, json.dumps([participant])),
        )
    con.commit()
    con.close()


def _side(champion_id: int, item_id: int, has_augment: bool, games: int, wins: int):
    return [
        (champion_id, item_id, has_augment, i < wins) for i in range(games)
    ]


class DestinyItemSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "games.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _recs(self, observations, legendary=(1, 2, 3), min_side_games=60):
        _write_games(self.db_path, observations)
        return tier_list.compute_champ_destiny_items(
            self.db_path,
            2400,
            "16.21",
            set(legendary),
            min_side_games=min_side_games,
        )

    def test_argmax_lcb_prefers_the_tighter_estimate_over_the_bigger_lift(self) -> None:
        # Item 1: lift +25pp off 60/60 games -> wide interval, LCB ~+7.5pp.
        # Item 2: lift +20pp off 400/400 games -> tight interval, LCB ~+13pp.
        # Argmax lift would pick item 1; argmax LCB must pick item 2.
        observations = (
            _side(1, 1, True, 60, 42)       # 70% with the augment
            + _side(1, 1, False, 60, 27)    # 45% without
            + _side(1, 2, True, 400, 240)   # 60% with
            + _side(1, 2, False, 400, 160)  # 40% without
        )
        recs = self._recs(observations)
        self.assertEqual(recs[1]["item_id"], 2)
        self.assertAlmostEqual(recs[1]["lift"], 0.20, places=6)
        self.assertGreater(recs[1]["lcb"], 0.10)

    def test_min_side_games_and_positive_lcb_are_required(self) -> None:
        # Item 1 is one game short on the "without 3081" side.
        # Item 2 clears the sample floor but its lift is inside the noise band.
        observations = (
            _side(1, 1, True, 200, 160)
            + _side(1, 1, False, 59, 20)
            + _side(1, 2, True, 80, 41)
            + _side(1, 2, False, 80, 40)
        )
        recs = self._recs(observations)
        self.assertNotIn(1, recs)

    def test_champion_without_a_qualifying_item_is_absent(self) -> None:
        # Champion 2's item loses win rate with the augment, champion 3 never
        # holds a legendary item: both must be absent so the card stays as-is.
        observations = (
            _side(2, 1, True, 100, 30)
            + _side(2, 1, False, 100, 55)
            + _side(3, 99, True, 100, 60)
            + _side(3, 99, False, 100, 40)
        )
        self.assertEqual(self._recs(observations), {})

    def test_ties_break_on_the_lower_item_id(self) -> None:
        observations = (
            _side(1, 3, True, 100, 60)
            + _side(1, 3, False, 100, 40)
            + _side(1, 2, True, 100, 60)
            + _side(1, 2, False, 100, 40)
        )
        self.assertEqual(self._recs(observations)[1]["item_id"], 2)


class LegendaryItemFilterTests(unittest.TestCase):
    def test_filters_components_boots_low_gold_and_other_maps(self) -> None:
        rows = {
            "100": {"gold": {"total": 2200}, "maps": {"12": True}, "tags": ["Damage"]},
            "101": {"gold": {"total": 2200}, "maps": {"12": True}, "tags": ["Boots"]},
            "102": {"gold": {"total": 2200}, "maps": {"12": True}, "into": ["103"]},
            "103": {"gold": {"total": 2199}, "maps": {"12": True}},
            "104": {"gold": {"total": 3000}, "maps": {"11": True}},
        }
        with mock.patch.object(engine, "_ddragon_item_rows", lambda *a, **k: rows):
            self.assertEqual(tier_list.legendary_item_ids("16.21.1"), {100})


if __name__ == "__main__":
    unittest.main()
