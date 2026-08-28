"""Settled-patch snapshots: identical results, and the right re-settle triggers.

The point of a snapshot is that a closed patch stops being rescanned, so every
test here proves one of two things: the cached path returns exactly what the
scanning path would have, or the cache correctly refuses to be reused.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import tierlist_engine as engine  # noqa: E402
from aram_nn import patch_snapshot  # noqa: E402

BLUE = [1, 2, 3, 4, 5]
RED = [6, 7, 8, 9, 10]

# Two cheap items and one expensive one; only the expensive non-Boots item passes
# _is_recommendable_core_item (>= ITEM_MIN_TOTAL_GOLD, not Boots, not augment-gated).
ITEM_META = {
    3001: {"id": 3001, "price_total": 3000, "categories": ["Damage"], "name_en": "Core One"},
    3002: {"id": 3002, "price_total": 2900, "categories": ["Health"], "name_en": "Core Two"},
    1001: {"id": 1001, "price_total": 300, "categories": ["Boots"], "name_en": "Boots"},
}


def _participants(blue_wins: int, items: list[int]) -> str:
    rows = []
    for cid in BLUE:
        rows.append({"teamId": 100, "championId": cid, "augments": [70 + cid % 3], "items": items})
    for cid in RED:
        rows.append({"teamId": 200, "championId": cid, "augments": [80 + cid % 3], "items": items})
    return json.dumps(rows)


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE games (
            game_id TEXT PRIMARY KEY, queue_id INTEGER NOT NULL,
            patch TEXT NOT NULL, blue_champs TEXT NOT NULL,
            red_champs TEXT NOT NULL, blue_wins INTEGER NOT NULL,
            duration_sec INTEGER, created_ms INTEGER,
            captured_at TEXT, participants_json TEXT,
            seed_family TEXT, participants_private_json TEXT
        )"""
    )
    con.commit()
    con.close()


def _add_games(path: Path, patch: str, count: int, *, prefix: str, items=(3001, 1001)) -> None:
    con = sqlite3.connect(path)
    for i in range(count):
        blue_wins = i % 2
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)",
            (
                f"{prefix}{i}", 2400, f"{patch}.1", json.dumps(BLUE), json.dumps(RED),
                blue_wins, 1200, 1000 + i, _participants(blue_wins, list(items)),
            ),
        )
    con.commit()
    con.close()


class PatchSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "games.db"
        self.snapshots = self.root / "patch_snapshots"
        _make_db(self.db)
        _add_games(self.db, "16.14", 40, prefix="a")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _settled(self, patch="16.14"):
        return engine.compute_settled_winrates(
            self.db, 2400, patch, snapshot_dir=self.snapshots
        )

    def test_settled_records_match_a_direct_scan(self) -> None:
        direct = engine.compute_winrates(self.db, 2400, "16.14")
        settled = self._settled()
        self.assertEqual(direct, settled)
        ordered = next(
            row for row in direct[1]
            if row["champion_id"] == 1 and row["augment_id"] == 71
        )
        self.assertEqual(ordered["slots"][0]["games"], 40)
        self.assertEqual(ordered["slots"][0]["wins"], 20)
        self.assertIsNone(ordered["slots"][1])
        # ...and the second call is served from the file, not the DB.
        self.assertTrue(patch_snapshot.snapshot_path("16.14", queue_id=2400,
                                                     snapshot_dir=self.snapshots).exists())
        self.assertEqual(direct, self._settled())

    def test_snapshot_survives_a_small_tail_of_late_games(self) -> None:
        self._settled()
        _add_games(self.db, "16.14", 2, prefix="late")  # +5%, under the bar
        status = patch_snapshot.section_status(
            "16.14", queue_id=2400, section=engine.SNAPSHOT_CHAMP_SECTION,
            live_games=42, snapshot_dir=self.snapshots,
        )
        self.assertEqual(status.state, "fresh")
        # The frozen counters win: the two late games are deliberately not counted.
        self.assertEqual(self._settled()[0][0]["games"], 40)

    def test_growth_past_the_ratio_resettles(self) -> None:
        self._settled()
        _add_games(self.db, "16.14", 8, prefix="tail")  # +20%, over the bar
        status = patch_snapshot.section_status(
            "16.14", queue_id=2400, section=engine.SNAPSHOT_CHAMP_SECTION,
            live_games=48, snapshot_dir=self.snapshots,
        )
        self.assertEqual(status.state, "stale-growth")
        self.assertEqual(self._settled(), engine.compute_winrates(self.db, 2400, "16.14"))
        self.assertEqual(self._settled()[0][0]["games"], 48)

    def test_a_shrinking_patch_is_never_trusted(self) -> None:
        self._settled()
        status = patch_snapshot.section_status(
            "16.14", queue_id=2400, section=engine.SNAPSHOT_CHAMP_SECTION,
            live_games=10, snapshot_dir=self.snapshots,
        )
        self.assertEqual(status.state, "stale-growth")

    def test_schema_bump_invalidates(self) -> None:
        self._settled()
        path = patch_snapshot.snapshot_path("16.14", queue_id=2400, snapshot_dir=self.snapshots)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sections"][engine.SNAPSHOT_CHAMP_SECTION]["schema_version"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        status = patch_snapshot.section_status(
            "16.14", queue_id=2400, section=engine.SNAPSHOT_CHAMP_SECTION,
            live_games=40, snapshot_dir=self.snapshots,
        )
        self.assertEqual(status.state, "stale-schema")

    def test_corrupt_snapshot_degrades_to_a_rescan(self) -> None:
        self._settled()
        path = patch_snapshot.snapshot_path("16.14", queue_id=2400, snapshot_dir=self.snapshots)
        path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(self._settled(), engine.compute_winrates(self.db, 2400, "16.14"))


class CoreItemSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "games.db"
        self.snapshots = self.root / "patch_snapshots"
        _make_db(self.db)
        _add_games(self.db, "16.14", 40, prefix="a", items=(3001, 3002, 1001))
        self.records = engine.compute_winrates(self.db, 2400, "16.14")[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _settled(self, item_meta=None):
        return engine.settled_core_item_patch_stats(
            self.db, 2400, "16.14", item_meta or ITEM_META, self.records,
            snapshot_dir=self.snapshots,
        )

    def test_settled_item_stats_match_a_direct_scan(self) -> None:
        direct = engine._compute_core_item_patch_stats(
            self.db, 2400, "16.14", ITEM_META, self.records
        )
        settled = self._settled()
        self.assertEqual(dict(direct["item"]), dict(settled["item"]))
        self.assertEqual(dict(direct["champ_item"]), dict(settled["champ_item"]))
        self.assertEqual(dict(direct["champ_games"]), dict(settled["champ_games"]))
        self.assertAlmostEqual(direct["global_wr"], settled["global_wr"])
        # Boots are observed but never counted.
        self.assertNotIn(1001, settled["item"])
        self.assertEqual(self._settled()["item"], settled["item"])

    def test_baselines_follow_the_current_records_not_the_snapshot(self) -> None:
        self._settled()
        shifted = [dict(row, raw_wr=0.9) for row in self.records]
        stats = engine.settled_core_item_patch_stats(
            self.db, 2400, "16.14", ITEM_META, shifted, snapshot_dir=self.snapshots
        )
        bucket = stats["champ_item"][(1, 3001)]
        self.assertAlmostEqual(bucket["baseline_sum"], bucket["games"] * 0.9)

    def test_core_item_filter_change_invalidates(self) -> None:
        self._settled()
        # 3002 drops under the gold floor: the frozen tallies counted it, so the
        # snapshot must not be reused.
        cheapened = dict(ITEM_META)
        cheapened[3002] = dict(ITEM_META[3002], price_total=100)
        stats = self._settled(cheapened)
        self.assertNotIn(3002, stats["item"])
        self.assertIn(3001, stats["item"])

    def test_unrelated_new_item_does_not_invalidate(self) -> None:
        before = self._settled()
        widened = dict(ITEM_META)
        widened[3999] = {"id": 3999, "price_total": 3200, "categories": ["Damage"], "name_en": "New"}
        after = self._settled(widened)
        self.assertEqual(before["item"], after["item"])
        status = patch_snapshot.section_status(
            "16.14", queue_id=2400, section=engine.SNAPSHOT_ITEM_SECTION,
            live_games=40,
            fingerprint=engine._core_item_fingerprint([3001, 3002, 1001], widened),
            snapshot_dir=self.snapshots,
        )
        self.assertEqual(status.state, "fresh")


if __name__ == "__main__":
    unittest.main()
