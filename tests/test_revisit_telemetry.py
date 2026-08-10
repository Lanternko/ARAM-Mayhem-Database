import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone

from aram_nn.lcu.snowball import (
    _enqueue_player,
    _ensure_schema,
    _mark_player_done,
    revisit_arm,
)


class RevisitTelemetryTests(unittest.TestCase):
    @staticmethod
    def _treatment_puuid() -> str:
        for index in range(100):
            puuid = f"treatment-{index}"
            if revisit_arm(puuid) == "treatment":
                return puuid
        raise AssertionError("no treatment puuid found")

    def test_mark_done_records_event_with_actual_interval_and_yield(self):
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        puuid = "telemetry-player"
        previous = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        con.execute(
            """
            INSERT INTO crawl_seen (
                puuid, source, priority, min_depth, first_seen_at,
                last_crawled_at, process_count, new_games_found,
                latest_seen_match_created_ms, last_crawled_match_created_ms,
                processed, seed_family
            ) VALUES (?, 'match', 10, 1, ?, ?, 1, 4, 0, 0, 0, 'manual_riot_id')
            """,
            (puuid, previous, previous),
        )
        con.execute(
            """
            INSERT INTO crawl_queue (
                puuid, depth, source, priority, discovered_match_created_ms,
                enqueued_at, updated_at, status, eligible_at_ms,
                seed_family
            ) VALUES (?, 1, 'match', 10, 0, ?, ?, 'in_progress', 0, 'manual_riot_id')
            """,
            (puuid, previous, previous),
        )
        con.commit()

        _mark_player_done(
            con,
            puuid,
            new_games_found=17,
            claimed_match_created_ms=0,
            observed_match_created_ms=0,
            requeue_cooldown_ms=0,
            new_games_by_queue={2400: 16, 4310: 1},
            source="match",
            seed_family="manual_riot_id",
            worker_id="W01",
            current_patch="16.15",
            history_game_count=20,
            target_game_count=18,
        )

        row = con.execute(
            """
            SELECT revisit_arm, is_revisit, revisit_interval_ms,
                   process_number, new_games_found, target_game_count,
                   current_patch
            FROM crawl_visit_events
            """
        ).fetchone()
        self.assertEqual(row[0], revisit_arm(puuid))
        self.assertEqual(row[1], 1)
        self.assertGreaterEqual(row[2], 24 * 3_600_000)
        self.assertEqual(row[3:], (2, 17, 18, "16.15"))
        con.close()

    def test_match_rediscovery_enforces_treatment_minimum(self):
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        puuid = self._treatment_puuid()
        first_seen = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        con.execute(
            """
            INSERT INTO crawl_seen (
                puuid, source, priority, min_depth, first_seen_at,
                process_count, new_games_found, latest_seen_match_created_ms,
                last_crawled_match_created_ms, processed, seed_family
            ) VALUES (?, 'match', 10, 1, ?, 20, 10, 100, 100, 1, 'manual_riot_id')
            """,
            (puuid, first_seen),
        )
        con.execute(
            """
            INSERT INTO crawl_queue (
                puuid, depth, source, priority, discovered_match_created_ms,
                enqueued_at, updated_at, status, eligible_at_ms,
                seed_family
            ) VALUES (?, 1, 'match', 10, 100, ?, ?, 'done', 0, 'manual_riot_id')
            """,
            (puuid, first_seen, first_seen),
        )
        con.commit()

        before_ms = int(time.time() * 1000)
        result = _enqueue_player(
            con,
            puuid,
            1,
            source="match",
            discovered_from_game_id="new-game",
            discovered_match_created_ms=200,
            requeue_cooldown_ms=45_000,
            seed_family="manual_riot_id",
        )
        eligible_at_ms = con.execute(
            "SELECT eligible_at_ms FROM crawl_queue WHERE puuid = ?", (puuid,)
        ).fetchone()[0]
        self.assertEqual(result, "requeued")
        self.assertGreaterEqual(eligible_at_ms - before_ms, 24 * 3_600_000)
        con.close()


if __name__ == "__main__":
    unittest.main()
