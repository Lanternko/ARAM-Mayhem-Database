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


def _write_games(db_path: Path, players: list[tuple[int, tuple[int, ...], bool, bool]]) -> None:
    """One minimal game per (champion, held items, has_augment, won) player.

    ``compute_champ_destiny_items`` only reads participant identity, augments,
    items and the win flag, so a one-participant row per observation is enough to
    exercise the stratified counting and the selection rule.
    """
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER,"
        " participants_json TEXT)"
    )
    for champion_id, items, has_augment, won in players:
        participant = {
            "championId": champion_id,
            "teamId": 100,
            "augments": [AUG] if has_augment else [],
            "items": list(items),
        }
        con.execute(
            "INSERT INTO games VALUES (2400, '16.21.1', ?, ?)",
            (1 if won else 0, json.dumps([participant])),
        )
    con.commit()
    con.close()


def _g(champion_id: int, items, has_augment: bool, games: int, wins: int):
    """``games`` players holding exactly ``items``, ``wins`` of them winning."""
    items = tuple(items)
    return [(champion_id, items, has_augment, i < wins) for i in range(games)]


def _both(champion_id: int, items, with_games: int, with_wins: int,
          without_games: int, without_wins: int):
    return (
        _g(champion_id, items, True, with_games, with_wins)
        + _g(champion_id, items, False, without_games, without_wins)
    )


class DestinyItemSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "games.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _recs(self, players, legendary=(1, 2, 3), min_side_games=60,
              min_destiny_games=None):
        _write_games(self.db_path, players)
        # The selection tests exercise the ranking rule, not the production sample
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
        # 149 games with 命定武器 is one short; 150 clears the floor.  Holding
        # item 1 wins 75% vs 40% for the single-item alternative (item 9).
        players = (
            _both(1, (1,), 149, 112, 60, 30)
            + _both(1, (9,), 149, 60, 60, 30)
            + _both(2, (1,), 150, 112, 60, 30)
            + _both(2, (9,), 150, 60, 60, 30)
        )
        _write_games(self.db_path, players)
        recs = tier_list.compute_champ_destiny_items(self.db_path, 2400, "16.21", {1, 9})
        self.assertNotIn(1, recs)
        self.assertEqual(recs[2]["item_id"], 1)
        self.assertGreater(recs[2]["lift"], 0.30)

    def test_gate_is_the_stratified_win_rate_gain_with_the_augment(self) -> None:
        # Single stratum (k=1): item 1 wins 70% vs 50% for item 2 among 3081
        # holders, so lift is +20pp; the without-3081 side only sets the synergy.
        players = (
            _both(1, (1,), 300, 210, 100, 50)
            + _both(1, (2,), 300, 150, 100, 50)
        )
        rec = self._recs(players, legendary=(1, 2))[1]
        self.assertEqual(rec["item_id"], 1)
        self.assertAlmostEqual(rec["lift"], 0.20, places=6)
        self.assertGreater(rec["lcb"], 0.10)
        self.assertLess(rec["lcb"], rec["lift"])
        self.assertAlmostEqual(rec["synergy"], 0.20, places=6)  # base is 0
        self.assertLess(rec["synergy_lcb"], rec["synergy"])

    def test_big_boost_but_losing_with_the_augment_is_not_recommended(self) -> None:
        # The 巨蛇鋒牙 shape: item 1 gains +20pp from 3081 (45% vs 25% without),
        # so the old with-minus-without delta picks it, yet among 3081 holders it
        # still wins less than the alternatives (45% vs 50%).  No item here has a
        # gate lower bound above 0, so the champion must be absent.
        players = (
            _both(1, (1,), 100, 45, 100, 25)
            + _both(1, (2,), 100, 50, 100, 50)
            + _both(1, (3,), 100, 50, 100, 50)
        )
        self.assertNotIn(1, self._recs(players))

    def test_qualifying_items_rank_by_synergy_lcb_not_by_gate(self) -> None:
        # Item 1 has the larger gate (+13pp) but wins just as much without 3081
        # (+5pp), so its synergy is only +8pp; item 2's gate is +7pp yet it is
        # -10pp without 3081, so its synergy is +17pp and it must rank first.
        players = (
            _both(1, (1,), 300, 186, 300, 150)
            + _both(1, (2,), 300, 174, 300, 120)
            + _both(1, (3,), 300, 120, 300, 150)
        )
        rec = self._recs(players)[1]
        self.assertEqual(rec["item_id"], 2)
        self.assertAlmostEqual(rec["lift"], 0.07, places=6)
        self.assertAlmostEqual(rec["synergy"], 0.17, places=6)
        self.assertNotIn(3, {rec["item_id"], rec.get("alt", {}).get("item_id")})
        if "alt" in rec:
            self.assertEqual(rec["alt"]["item_id"], 1)
            self.assertGreater(rec["alt"]["lift"], rec["lift"])
            self.assertLessEqual(rec["alt"]["synergy_lcb"], rec["synergy_lcb"])

    def test_min_side_games_and_positive_gate_lcb_are_required(self) -> None:
        # Champion 1: item 1 is one game short on the "without 3081" side (59).
        # Champion 2: same data with 60 games clears the floor (control).
        # Champion 3: the gate is inside the noise band.
        players = (
            _both(1, (1,), 200, 160, 59, 30)
            + _both(1, (2,), 200, 80, 59, 30)
            + _both(2, (1,), 200, 160, 60, 30)
            + _both(2, (2,), 200, 80, 60, 30)
            + _both(3, (1,), 80, 41, 80, 40)
            + _both(3, (2,), 80, 40, 80, 40)
        )
        recs = self._recs(players, legendary=(1, 2))
        self.assertNotIn(1, recs)
        self.assertEqual(recs[2]["item_id"], 1)
        self.assertNotIn(3, recs)

    def test_champion_without_a_qualifying_item_is_absent(self) -> None:
        # Champion 2's item loses win rate with the augment, champion 3 never
        # holds a legendary item: both must be absent so the card stays as-is.
        players = (
            _both(2, (1,), 100, 30, 100, 55)
            + _both(2, (2,), 100, 60, 100, 55)
            + _both(3, (99,), 100, 60, 100, 40)
        )
        recs = self._recs(players, legendary=(1, 2))
        self.assertNotIn(3, recs)
        self.assertNotEqual(recs.get(2, {}).get("item_id"), 1)

    def test_ties_break_on_the_lower_item_id_and_tied_runner_up_is_alt(self) -> None:
        players = (
            _both(1, (1,), 400, 160, 400, 200)
            + _both(1, (3,), 400, 240, 400, 200)
            + _both(1, (2,), 400, 240, 400, 200)
        )
        rec = self._recs(players)[1]
        self.assertEqual(rec["item_id"], 2)
        self.assertEqual(rec["alt"]["item_id"], 3)

    def test_excluded_item_is_never_recommended_but_still_counts_as_held(self) -> None:
        hydra = 226668
        # Hydra would clearly win; item 1 still clears the gate against the
        # hydra and item-2 holders in the same one-item stratum.
        players = (
            _both(1, (hydra,), 400, 320, 400, 200)
            + _both(1, (1,), 400, 280, 400, 200)
            + _both(1, (2,), 400, 160, 400, 200)
        )
        _write_games(self.db_path, players)
        args = (self.db_path, 2400, "16.21", {1, 2, hydra})
        kwargs = {"min_side_games": 60, "min_destiny_games": 60}
        with mock.patch.object(engine, "DESTINY_EXCLUDED_ITEM_IDS", frozenset()):
            self.assertEqual(
                tier_list.compute_champ_destiny_items(*args, **kwargs)[1]["item_id"], hydra
            )
        rec = tier_list.compute_champ_destiny_items(*args, **kwargs)[1]
        self.assertEqual(rec["item_id"], 1)
        self.assertNotIn("alt", rec)

    def test_clear_winner_has_no_alt(self) -> None:
        # Among 3081 holders item 1 gains +25pp and item 2 +10pp, equal without
        # it: item 2's synergy sits far below item 1's lower bound.
        players = (
            _both(1, (1,), 1000, 700, 1000, 500)
            + _both(1, (2,), 1000, 600, 1000, 500)
            + _both(1, (3,), 1000, 300, 1000, 500)
        )
        rec = self._recs(players)[1]
        self.assertEqual(rec["item_id"], 1)
        self.assertNotIn("alt", rec)

    def test_alt_boundary_is_inclusive(self) -> None:
        # Pin the per-item statistics so the runner-up's synergy lands exactly on
        # (or one ulp under) the winner's synergy lower bound.  Items are told
        # apart by how many 3081 games they have (10 / 20).
        players = []
        for item, n in ((1, 10), (2, 20)):
            players += _both(1, (item,), n, n // 2, 10, 5)
        # winner: synergy .25, half .125 -> synergy_lcb exactly .125
        for second_gate, expect_alt in ((0.375, True), (0.375 - 2 ** -20, False)):
            stats = {
                10: {2: (0.5, 0.125), 0: (0.25, 0.0)},
                20: {2: (second_gate, 0.125), 0: (0.25, 0.0)},
            }

            def fake(tot, held, offset, stats=stats):
                return stats[sum(held[3::4])][offset]

            with self.subTest(second_gate=second_gate):
                self.db_path.unlink(missing_ok=True)
                with mock.patch.object(engine, "_mh_stratified_diff", fake):
                    rec = self._recs(players, min_side_games=1)[1]
                self.assertEqual(rec["item_id"], 1)
                self.assertEqual(rec["synergy_lcb"], 0.125)
                self.assertEqual("alt" in rec, expect_alt)
                if expect_alt:
                    self.assertEqual(rec["alt"]["item_id"], 2)
                    self.assertEqual(rec["alt"]["synergy"], 0.125)

    def test_stratifying_by_item_count_removes_simpson_reversal(self) -> None:
        # Among 3081 holders item 1 is held mostly by full-build players (k=2,
        # high win rate) while the alternative is held mostly at k=1, so the
        # pooled rates favour item 1 (+13pp).  Inside each stratum item 1 is
        # 10pp worse, so it must not be recommended.
        players = (
            _g(1, (1,), True, 50, 20) + _g(1, (2,), True, 400, 200)
            + _g(1, (1, 3), True, 400, 280) + _g(1, (2, 3), True, 50, 40)
            + _g(1, (1,), False, 100, 50) + _g(1, (2,), False, 100, 50)
            + _g(1, (1, 3), False, 100, 50) + _g(1, (2, 3), False, 100, 50)
        )
        held_wins, held_n = 20 + 280, 450
        other_wins, other_n = 200 + 40, 450
        self.assertGreater(held_wins / held_n - other_wins / other_n, 0.10)  # naive
        recs = self._recs(players)
        self.assertNotEqual(recs.get(1, {}).get("item_id"), 1)
        self.assertNotEqual(recs.get(1, {}).get("alt", {}).get("item_id"), 1)

    def test_no_shared_item_count_stratum_means_no_recommendation(self) -> None:
        # Item-1 holders sit at k=1 and everyone else at k=2: there is no layer
        # where both arms exist, so no gate can be computed for any item.
        players = _both(2, (1,), 200, 160, 100, 50) + _both(2, (2, 3), 200, 80, 100, 50)
        self.assertNotIn(2, self._recs(players))


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

    def test_alt_survives_only_when_its_own_item_is_unchanged(self) -> None:
        previous = {
            2: {"item_id": 100, "lift": 0.04, "lcb": 0.01, "synergy": 0.05,
                "synergy_lcb": 0.01,
                "alt": {"item_id": 101, "lift": 0.03, "lcb": 0.005}},
        }
        same = {"100": self._item(), "101": self._item(gold=2700)}
        rows = {
            self.CUR: same,
            self.PREV: {"100": self._item(), "101": self._item(gold=2700)},
        }
        changed_alt = {
            self.CUR: same,
            self.PREV: {"100": self._item(), "101": self._item(gold=2900)},
        }
        for label, catalogue, keeps_alt in (
            ("unchanged", rows, True),
            ("alt item changed", changed_alt, False),
        ):
            with self.subTest(label):
                with mock.patch.object(
                    engine, "_ddragon_item_rows",
                    lambda version, *a, c=catalogue, **k: c.get(version, {}),
                ):
                    merged = tier_list.merge_destiny_items_with_previous(
                        {}, previous, current_version=self.CUR,
                        previous_version=self.PREV, cache_dir=self.cache_dir,
                    )
                self.assertEqual(merged[2]["item_id"], 100)
                self.assertTrue(merged[2]["prev"])
                self.assertAlmostEqual(merged[2]["synergy"], 0.05)  # new fields kept
                self.assertEqual("alt" in merged[2], keeps_alt)
                if keeps_alt:
                    self.assertEqual(merged[2]["alt"]["item_id"], 101)

    def test_current_pick_is_used_whole_including_alt_presence(self) -> None:
        current = {1: {"item_id": 100, "lift": 0.05, "lcb": 0.02}}  # no alt
        previous = {
            1: {"item_id": 100, "lift": 0.01, "lcb": 0.0,
                "alt": {"item_id": 100, "lift": 0.01, "lcb": 0.0}},
        }
        merged = self._merge(current, previous)
        self.assertNotIn("alt", merged[1])
        self.assertNotIn("prev", merged[1])


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
