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

    def _recs(self, observations, legendary=(1, 2, 3), min_side_games=60,
              min_destiny_games=None):
        _write_games(self.db_path, observations)
        # The selection tests exercise the argmax rule, not the production sample
        # floors, so both sides default to the small synthetic floor.
        return tier_list.compute_champ_destiny_items(
            self.db_path,
            2400,
            "16.21",
            set(legendary),
            min_side_games=min_side_games,
            min_destiny_games=(
                min_side_games if min_destiny_games is None else min_destiny_games
            ),
        )

    def test_with_augment_side_requires_150_games(self) -> None:
        # 149 games with 命定武器 is one short; 150 clears the floor.  Both sides
        # otherwise show the same large, obviously positive lift.
        observations = (
            _side(1, 1, True, 149, 110)
            + _side(1, 1, False, 60, 24)
            + _side(2, 1, True, 150, 110)
            + _side(2, 1, False, 60, 24)
        )
        _write_games(self.db_path, observations)
        recs = tier_list.compute_champ_destiny_items(self.db_path, 2400, "16.21", {1})
        self.assertNotIn(1, recs)
        self.assertEqual(recs[2]["item_id"], 1)
        self.assertGreater(recs[2]["lift"], 0.30)

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


class DestinyPreviousPatchFallbackTests(unittest.TestCase):
    """Cross-patch fallback (merge_destiny_items_with_previous)."""

    CUR = "16.21.1"
    PREV = "16.20.1"

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    @staticmethod
    def _item(description="<mainText>+80 ability power</mainText>", ap=80, gold=2600):
        return {
            "description": description,
            "stats": {"FlatMagicDamageMod": ap},
            "gold": {"base": 900, "total": gold, "sell": gold // 2},
        }

    def _merge(self, current, previous, *, prev_row=None, cur_row=None,
               previous_version=PREV):
        cur_row = cur_row if cur_row is not None else self._item()
        prev_row = prev_row if prev_row is not None else self._item()
        rows = {self.CUR: {"100": cur_row}, self.PREV: {"100": prev_row}}
        with mock.patch.object(
            engine, "_ddragon_item_rows", lambda version, *a, **k: rows.get(version, {})
        ):
            return tier_list.merge_destiny_items_with_previous(
                current,
                previous,
                current_version=self.CUR,
                previous_version=previous_version,
                cache_dir=self.cache_dir,
            )

    def test_current_pick_wins_and_missing_pick_falls_back(self) -> None:
        current = {1: {"item_id": 100, "lift": 0.05, "lcb": 0.02}}
        previous = {
            1: {"item_id": 100, "lift": 0.01, "lcb": 0.001},   # already recommended
            2: {"item_id": 100, "lift": 0.04, "lcb": 0.01},    # only in last patch
        }
        merged = self._merge(current, previous)
        self.assertEqual(merged[1]["item_id"], 100)
        self.assertAlmostEqual(merged[1]["lift"], 0.05)
        self.assertNotIn("prev", merged[1])
        self.assertAlmostEqual(merged[2]["lift"], 0.04)
        self.assertTrue(merged[2]["prev"])

    def test_fallback_is_skipped_when_the_item_changed(self) -> None:
        previous = {2: {"item_id": 100, "lift": 0.04, "lcb": 0.01}}
        variants = {
            "description": self._item(description="<mainText>+100 ability power</mainText>"),
            "stats": self._item(ap=100),
            "gold.total": self._item(gold=2800),
        }
        for field, prev_row in variants.items():
            with self.subTest(field=field):
                self.assertEqual(
                    self._merge({}, previous, prev_row=prev_row), {},
                    f"changed {field} must drop the fallback",
                )

    def test_fallback_is_skipped_when_a_catalogue_is_unavailable(self) -> None:
        previous = {2: {"item_id": 100, "lift": 0.04, "lcb": 0.01}}
        # No previous Data Dragon version resolved -> no comparison, no fallback.
        self.assertEqual(self._merge({}, previous, previous_version=None), {})
        # Neither catalogue fetched -> same conservative outcome.
        with mock.patch.object(engine, "_ddragon_item_rows", lambda *a, **k: {}):
            self.assertEqual(
                tier_list.merge_destiny_items_with_previous(
                    {}, previous, current_version=self.CUR,
                    previous_version=self.PREV, cache_dir=self.cache_dir,
                ),
                {},
            )

    def test_fallback_is_skipped_when_the_item_is_missing_from_a_catalogue(self) -> None:
        previous = {2: {"item_id": 100, "lift": 0.04, "lcb": 0.01}}
        rows = {self.CUR: {}, self.PREV: {"100": self._item()}}
        with mock.patch.object(
            engine, "_ddragon_item_rows", lambda version, *a, **k: rows.get(version, {})
        ):
            # Empty current catalogue -> entire fallback disabled.
            self.assertEqual(
                tier_list.merge_destiny_items_with_previous(
                    {}, previous, current_version=self.CUR,
                    previous_version=self.PREV, cache_dir=self.cache_dir,
                ),
                {},
            )


class PreviousDdragonVersionTests(unittest.TestCase):
    def test_resolves_the_newest_version_for_the_patch_prefix(self) -> None:
        versions = ["16.21.1", "16.20.1", "16.20.0", "16.19.1"]
        with mock.patch.object(engine, "_cached_get_json", lambda *a, **k: versions):
            self.assertEqual(engine.ddragon_version_for_patch("16.20"), "16.20.1")
            self.assertIsNone(engine.ddragon_version_for_patch("16.18"))
            self.assertIsNone(engine.ddragon_version_for_patch(None))

    def test_fetch_failure_returns_none_instead_of_raising(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("offline")

        with mock.patch.object(engine, "_cached_get_json", boom):
            self.assertIsNone(engine.ddragon_version_for_patch("16.20"))


if __name__ == "__main__":
    unittest.main()
