from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from aram_nn.site.db import count_games, insert_public_games, iter_public_games
from aram_nn.site.sync import decide_sync


def game_row(game_id: str, *, created_ms: int = 1) -> dict:
    return {
        "game_id": game_id,
        "queue_id": 2400,
        "patch": "16.10.1",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": True,
        "duration_sec": 900,
        "created_ms": created_ms,
        "captured_at": "2026-05-23T00:00:00Z",
        "participants_json": [
            {"teamId": 100, "championId": 1, "augments": [1001, 1002]},
            {"teamId": 200, "championId": 6, "augments": [1003, 1004]},
        ],
    }


class SiteBackendTests(unittest.TestCase):
    def test_bulk_insert_is_idempotent_by_game_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            first = insert_public_games(db, [game_row("TW_1"), game_row("TW_2")])
            second = insert_public_games(db, [game_row("TW_1"), game_row("TW_2")])

            self.assertEqual(first.inserted, 2)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.skipped, 2)
            self.assertEqual(count_games(db), 2)

    def test_private_identifiers_are_rejected(self) -> None:
        row = game_row("TW_private")
        row["participants_json"] = [
            {
                "teamId": 100,
                "championId": 1,
                "puuid": "00000000-0000-0000-0000-000000000000",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                insert_public_games(Path(tmp) / "site.db", [row])

    def test_iter_public_games_filters_queue_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            rows = [
                game_row("TW_1", created_ms=2),
                {**game_row("TW_2", created_ms=1), "patch": "16.9.1"},
                {**game_row("TW_3", created_ms=3), "queue_id": 450},
            ]
            insert_public_games(db, rows)

            filtered = list(iter_public_games(db, queue_id=2400, patch_prefix="16.10"))
            self.assertEqual([row["game_id"] for row in filtered], ["TW_1"])

    def test_sync_decision_waits_until_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            insert_public_games(db, [game_row(f"TW_{idx}") for idx in range(3)])

            wait = decide_sync(
                db=db,
                state={"last_uploaded_total": 0},
                threshold=10,
                force=False,
                queue_id=2400,
            )
            push = decide_sync(
                db=db,
                state={"last_uploaded_total": 0},
                threshold=3,
                force=False,
                queue_id=2400,
            )

            self.assertFalse(wait.should_push)
            self.assertTrue(push.should_push)


if __name__ == "__main__":
    unittest.main()
