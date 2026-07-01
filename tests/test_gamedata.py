import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aram_nn.gamedata import count_games, iter_games, load_games_df, participant_won


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
    rows = [
        # (game_id, queue, patch, blue, red, blue_wins, dur, created, participants)
        ("g2", 2400, "16.13.1", [1, 5, 9], [2, 6, 10], 1, 1200, 2000,
         [{"teamId": 100, "championId": 1, "augments": [11]},
          {"teamId": 200, "championId": 2, "augments": [12]}]),
        ("g1", 2400, "16.12.0", [3, 4, 7], [8, 11, 12], 0, 900, 1000, None),
        ("g3", 450, "16.13.1", [1, 2, 3], [4, 5, 6], 1, 800, 3000, None),
        ("g4", 2400, "16.13.2", [2, 3, 4], [5, 6, 7], 0, 200, 4000,
         "not-json"),
    ]
    for gid, q, patch, blue, red, bw, dur, ms, parts in rows:
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)",
            (gid, q, patch, json.dumps(blue), json.dumps(red), bw, dur, ms,
             json.dumps(parts) if isinstance(parts, list) else parts),
        )
    con.commit()
    con.close()


class GamedataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "games.db"
        _make_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_queue_and_patch_filter_and_time_order(self) -> None:
        games = list(iter_games(self.db, queue_id=2400))
        self.assertEqual([g["game_id"] for g in games], ["g1", "g2", "g4"])  # created_ms asc
        games = list(iter_games(self.db, queue_id=2400, patch_prefix="16.13"))
        self.assertEqual([g["game_id"] for g in games], ["g2", "g4"])
        self.assertEqual(count_games(self.db, 2400, "16.13"), 2)
        self.assertEqual(count_games(self.db, None, None), 4)

    def test_champ_lists_parsed_not_reordered(self) -> None:
        g = next(iter_games(self.db, queue_id=2400, patch_prefix="16.13"))
        self.assertEqual(g["blue_champs"], [1, 5, 9])
        self.assertEqual(g["red_champs"], [2, 6, 10])

    def test_participants_parsed_with_bad_json_tolerated(self) -> None:
        # Default for participant scans is UNORDERED (fast sequential scan) —
        # key on game_id, not position.
        games = {g["game_id"]: g for g in
                 iter_games(self.db, 2400, "16.13", parse_participants=True)}
        self.assertEqual(games["g2"]["participants"][0]["championId"], 1)
        self.assertEqual(games["g4"]["participants"], [])  # "not-json" -> []

    def test_participants_ordered_two_pass(self) -> None:
        games = list(iter_games(self.db, 2400, parse_participants=True, ordered=True))
        self.assertEqual([g["game_id"] for g in games], ["g1", "g2", "g4"])
        self.assertEqual(games[1]["participants"][1]["championId"], 2)

    def test_participant_won_convention(self) -> None:
        self.assertTrue(participant_won({"teamId": 100}, 1))
        self.assertFalse(participant_won({"teamId": 200}, 1))
        self.assertTrue(participant_won({"teamId": 200}, 0))

    def test_min_duration_filter(self) -> None:
        games = list(iter_games(self.db, 2400, min_duration_sec=300))
        self.assertEqual([g["game_id"] for g in games], ["g1", "g2"])  # g4 is 200s

    def test_load_games_df(self) -> None:
        df = load_games_df(self.db, queue_id=2400)
        self.assertEqual(df.height, 3)
        self.assertEqual(df["game_id"].to_list(), ["g1", "g2", "g4"])
        empty = load_games_df(self.db, queue_id=9999)
        self.assertEqual(empty.height, 0)
        self.assertIn("blue_champs", empty.columns)

    def test_read_only_connection(self) -> None:
        # Analysis scripts must never be able to write the collector's DB.
        import aram_nn.gamedata as gd
        con = gd._connect_ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM games")
        con.close()


if __name__ == "__main__":
    unittest.main()
