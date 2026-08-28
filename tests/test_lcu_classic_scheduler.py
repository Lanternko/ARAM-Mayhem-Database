from __future__ import annotations

import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aram_nn.lcu.snowball import (
    ClassicAffinityProfile,
    _classic_affinity_profile,
    _classic_claim_slot,
    _classic_lane_arm_for_slot,
    _classic_revisit_eligible_at_ms,
    _claim_next_player,
    lane_arm,
    _enqueue_player,
    _ensure_schema,
    _mark_player_done,
)


HOUR_MS = 3_600_000


def _history(now_ms: int, *, count: int, spacing_hours: float, classic_count: int) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                "gameId": str(index),
                "gameCreation": now_ms - int(index * spacing_hours * HOUR_MS),
                "queueId": 4310 if index < classic_count else 2400,
            }
        )
    return rows


class ClassicAffinityTests(unittest.TestCase):
    def test_heavy_player_hits_ten_hour_hard_floor(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=20, spacing_hours=0.5, classic_count=12),
            now_ms=now_ms,
        )
        self.assertEqual(profile.label, "heavy")
        self.assertEqual(profile.rank, 3)
        self.assertEqual(profile.revisit_interval_ms, 10 * HOUR_MS)

    def test_slower_player_gets_formula_extended_interval(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=20, spacing_hours=6, classic_count=3),
            now_ms=now_ms,
        )
        self.assertEqual(profile.label, "regular")
        self.assertAlmostEqual(profile.revisit_interval_ms / HOUR_MS, 96.0, delta=0.01)

    def test_classic_origin_without_visible_classic_game_is_dormant(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=5, spacing_hours=8, classic_count=0),
            discovered_queue_id=4310,
            now_ms=now_ms,
        )
        self.assertEqual((profile.label, profile.rank), ("dormant", 1))
        self.assertGreaterEqual(profile.revisit_interval_ms, 10 * HOUR_MS)

    def test_due_time_is_measured_from_last_crawl(self) -> None:
        now = datetime.now(timezone.utc)
        five_hours_ago = (now - timedelta(hours=5)).isoformat()
        due_ms = _classic_revisit_eligible_at_ms(
            five_hours_ago,
            10 * HOUR_MS,
            now_ms=int(now.timestamp() * 1000),
        )
        self.assertAlmostEqual(
            due_ms - int(now.timestamp() * 1000),
            5 * HOUR_MS,
            delta=1000,
        )

    def test_lifetime_classic_producer_stays_dormant_without_visible_game(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=20, spacing_hours=2, classic_count=0),
            now_ms=now_ms,
            lifetime_classic_games=4,
        )
        self.assertEqual((profile.label, profile.rank), ("dormant", 1))

    def test_positive_rate_without_visible_game_stays_dormant(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=20, spacing_hours=2, classic_count=0),
            now_ms=now_ms,
            classic_rate_num=0.4,
        )
        self.assertEqual((profile.label, profile.rank), ("dormant", 1))

    def test_mayhem_only_player_without_evidence_is_none(self) -> None:
        now_ms = 2_000_000_000_000
        profile = _classic_affinity_profile(
            _history(now_ms, count=20, spacing_hours=2, classic_count=0),
            now_ms=now_ms,
        )
        self.assertEqual((profile.label, profile.rank), ("none", 0))

    def test_hard_floor_survives_a_lower_runtime_setting(self) -> None:
        now = datetime.now(timezone.utc)
        due_ms = _classic_revisit_eligible_at_ms(
            now.isoformat(),
            HOUR_MS,
            now_ms=int(now.timestamp() * 1000),
            min_revisit_ms=HOUR_MS,
        )
        self.assertGreaterEqual(due_ms - int(now.timestamp() * 1000), 10 * HOUR_MS)


class ClassicClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        _ensure_schema(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def _add(self, puuid: str, match_ms: int, *, classic: bool) -> None:
        _enqueue_player(
            self.con,
            puuid,
            depth=1,
            source="match",
            discovered_match_created_ms=match_ms,
            discovered_queue_id=4310 if classic else 2400,
        )

    def test_ten_percent_slots_are_evenly_dispersed(self) -> None:
        slots = [index for index in range(100) if _classic_claim_slot(index, 10)]
        self.assertEqual(slots, list(range(0, 100, 10)))

    def test_lane_arms_alternate_across_slots_not_claims(self) -> None:
        """Every reserved slot falls on a multiple of 100/percent, so a parity
        test on claim_number picks one arm forever and the experiment silently
        loses its control."""
        with patch("aram_nn.lcu.snowball._LANE_AB_ENABLED", True):
            for percent in (3, 10, 25):
                slots = [n for n in range(1000) if _classic_claim_slot(n, percent)]
                arms = [_classic_lane_arm_for_slot(n, percent) for n in slots]
                self.assertEqual(arms.count("score"), arms.count("due"), percent)
                self.assertEqual(arms[:4], ["score", "due", "score", "due"], percent)

    def test_disabled_ab_puts_both_halves_on_the_same_arm(self) -> None:
        """The slot selector and the per-player hash have to agree when the
        experiment is off.  If the selector still handed out 'score' slots while
        every player hashed to 'due', the WHERE clause would match nothing and
        the slot would fall through to the general frontier -- the Classic lane
        would quietly run at half its configured budget."""
        slot_arms = {
            _classic_lane_arm_for_slot(n, 10)
            for n in range(1000)
            if _classic_claim_slot(n, 10)
        }
        player_arms = {lane_arm(f"puuid-{index}") for index in range(500)}
        self.assertEqual(slot_arms, {"due"})
        self.assertEqual(player_arms, {"due"})

    def test_classic_slot_reserves_a_due_classic_player(self) -> None:
        self._add("general-newer", 200, classic=False)
        self._add("classic-older", 100, classic=True)
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=0), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="score"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="score"
        ):
            claimed = _claim_next_player(self.con, "W01", 300_000, 10)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "classic-older")
        self.assertEqual(claimed[-1], "classic_score")

    def test_unused_classic_slot_returns_to_general_frontier(self) -> None:
        self._add("general", 200, classic=False)
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=0), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="score"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="score"
        ):
            claimed = _claim_next_player(self.con, "W01", 300_000, 10)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "general")

    def test_general_slot_does_not_exceed_classic_quota(self) -> None:
        self._add("general-older", 100, classic=False)
        self._add("classic-newer", 200, classic=True)
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=1):
            claimed = _claim_next_player(self.con, "W01", 300_000, 10)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "general-older")
        self.assertEqual(claimed[-1], "general")

    def _add_two_classic_players(self) -> None:
        """One low-yield player overdue for ages, one high-yield just come due.

        This is the shape the whole lane argument turns on: measured on the live
        frontier, every rank>=2 player sat past position 24,640 of 34,531 due
        rows because the overdue tail always wins on timestamp.
        """
        self._add("stale-low-yield", 100, classic=True)
        self._add("fresh-high-yield", 200, classic=True)
        self.con.execute(
            "UPDATE crawl_queue SET classic_affinity_rank=1, eligible_at_ms=1, "
            "classic_lambda=0.05, classic_span_ms=?, classic_last_crawl_ms=? "
            "WHERE puuid='stale-low-yield'",
            (100 * HOUR_MS, int(time.time() * 1000) - 100 * HOUR_MS),
        )
        self.con.execute(
            "UPDATE crawl_queue SET classic_affinity_rank=3, eligible_at_ms=2, "
            "classic_lambda=3.4, classic_span_ms=?, classic_last_crawl_ms=? "
            "WHERE puuid='fresh-high-yield'",
            (10 * HOUR_MS, int(time.time() * 1000) - 10 * HOUR_MS),
        )
        self.con.commit()

    def test_due_arm_keeps_the_shipped_oldest_first_ordering(self) -> None:
        self._add_two_classic_players()
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=1), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="due"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="due"
        ):
            # percent=100 makes every claim a classic slot, so slot 1 is the
            # 'due' arm.
            claimed = _claim_next_player(self.con, "W01", 300_000, 100)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "stale-low-yield")
        self.assertEqual(claimed[-1], "classic_due")

    def test_score_arm_prefers_expected_yield_over_wait_time(self) -> None:
        self._add_two_classic_players()
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=0), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="score"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="score"
        ):
            claimed = _claim_next_player(self.con, "W01", 300_000, 100)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "fresh-high-yield")
        self.assertEqual(claimed[-1], "classic_score")

    def test_saturation_lets_a_starved_low_yield_player_through(self) -> None:
        """Both windows full, so only the rate separates them -- but the low-yield
        player must still win once the high-yield one has just been visited and
        has nothing new to give."""
        self._add_two_classic_players()
        self.con.execute(
            "UPDATE crawl_queue SET classic_last_crawl_ms=? "
            "WHERE puuid='fresh-high-yield'",
            (int(time.time() * 1000) - 6 * 60_000,),
        )
        self.con.commit()
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=0), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="score"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="score"
        ):
            claimed = _claim_next_player(self.con, "W01", 300_000, 100)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "stale-low-yield")

    def test_never_visited_row_scores_the_discovery_prior(self) -> None:
        """60% of the live lane has never been visited; at classic_lambda = 0 they
        would score zero and never be claimed at all."""
        self._add("never-visited", 100, classic=True)
        self.con.execute(
            "UPDATE crawl_queue SET classic_affinity_rank=1, eligible_at_ms=1 "
            "WHERE puuid='never-visited'"
        )
        self.con.commit()
        row = self.con.execute(
            "SELECT classic_lambda, classic_last_crawl_ms FROM crawl_queue "
            "WHERE puuid='never-visited'"
        ).fetchone()
        self.assertEqual((row[0], row[1]), (0.0, 0))
        with patch("aram_nn.lcu.snowball._claim_counter", return_value=0), patch(
            "aram_nn.lcu.snowball.lane_arm", return_value="score"
        ), patch(
            "aram_nn.lcu.snowball._classic_lane_arm_for_slot", return_value="score"
        ):
            claimed = _claim_next_player(self.con, "W01", 300_000, 100)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], "never-visited")

    def test_lane_arm_split_is_stable_and_balanced_when_enabled(self) -> None:
        with patch("aram_nn.lcu.snowball._LANE_AB_ENABLED", True):
            arms = [lane_arm(f"puuid-{index}") for index in range(4000)]
            self.assertEqual(set(arms), {"score", "due"})
            self.assertAlmostEqual(arms.count("score") / len(arms), 0.5, delta=0.03)
            self.assertEqual(lane_arm("puuid-7"), lane_arm("puuid-7"))


class ClassicPersistenceTests(unittest.TestCase):
    def test_classic_visit_is_periodically_requeued_at_or_after_floor(self) -> None:
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        _enqueue_player(con, "classic", 1, "match", discovered_queue_id=4310)
        con.execute("UPDATE crawl_queue SET status='in_progress' WHERE puuid='classic'")
        con.commit()
        before_ms = int(time.time() * 1000)
        profile = ClassicAffinityProfile("heavy", 3, 8, 12, before_ms, 10 * HOUR_MS)
        requeued = _mark_player_done(
            con,
            "classic",
            new_games_found=0,
            claimed_match_created_ms=0,
            observed_match_created_ms=0,
            requeue_cooldown_ms=45_000,
            classic_profile=profile,
            claim_lane="classic",
        )
        row = con.execute(
            "SELECT status, eligible_at_ms, classic_affinity_rank "
            "FROM crawl_queue WHERE puuid='classic'"
        ).fetchone()
        seen = con.execute(
            "SELECT classic_affinity, processed FROM crawl_seen WHERE puuid='classic'"
        ).fetchone()
        event = con.execute(
            "SELECT claim_lane, classic_affinity FROM crawl_visit_events"
        ).fetchone()
        self.assertTrue(requeued)
        self.assertEqual(row[0], "pending")
        self.assertGreaterEqual(row[1] - before_ms, 10 * HOUR_MS - 1000)
        self.assertEqual(row[2], 3)
        self.assertEqual(seen, ("heavy", 0))
        self.assertEqual(event, ("classic", "heavy"))
        con.close()

    def test_one_time_backfill_requeues_legacy_classic_origin(self) -> None:
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        con.execute(
            "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, "
            "blue_wins, duration_sec, created_ms, captured_at) "
            "VALUES ('g4310', 4310, '16.15', '[]', '[]', 1, 1800, 1, 'now')"
        )
        _enqueue_player(con, "legacy", 1, "match", discovered_from_game_id="g4310")
        con.execute("UPDATE crawl_queue SET status='done' WHERE puuid='legacy'")
        con.execute("UPDATE crawl_seen SET processed=1 WHERE puuid='legacy'")
        con.execute(
            "DELETE FROM crawl_runtime_state WHERE state_key='classic_affinity_v1_backfill_done'"
        )
        con.commit()

        _ensure_schema(con)
        row = con.execute(
            "SELECT s.classic_affinity, s.classic_affinity_rank, s.processed, "
            "q.status, q.classic_affinity_rank "
            "FROM crawl_seen s JOIN crawl_queue q USING(puuid) WHERE s.puuid='legacy'"
        ).fetchone()
        self.assertEqual(row, ("candidate", 1, 0, "pending", 1))
        con.close()

    def test_quiet_window_does_not_eject_lifetime_producer(self) -> None:
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        _enqueue_player(con, "producer", 1, "match", discovered_queue_id=2400)
        con.execute(
            "UPDATE crawl_seen SET new_games_by_queue_json=? WHERE puuid='producer'",
            ('{"4310":3}',),
        )
        con.execute("UPDATE crawl_queue SET status='in_progress' WHERE puuid='producer'")
        con.commit()
        requeued = _mark_player_done(
            con,
            "producer",
            new_games_found=0,
            claimed_match_created_ms=0,
            observed_match_created_ms=0,
            requeue_cooldown_ms=45_000,
            classic_profile=ClassicAffinityProfile("none", 0, 0, 0, 0, 0),
            claim_lane="general",
        )
        row = con.execute(
            "SELECT status, classic_affinity_rank FROM crawl_queue WHERE puuid='producer'"
        ).fetchone()
        seen = con.execute(
            "SELECT classic_affinity, classic_affinity_rank, processed "
            "FROM crawl_seen WHERE puuid='producer'"
        ).fetchone()
        self.assertTrue(requeued)
        self.assertEqual(row, ("pending", 1))
        self.assertEqual(seen, ("dormant", 1, 0))
        con.close()

    def test_true_none_player_still_leaves_the_lane(self) -> None:
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        _enqueue_player(con, "mayhem", 1, "match", discovered_queue_id=2400)
        con.execute("UPDATE crawl_queue SET status='in_progress' WHERE puuid='mayhem'")
        con.commit()
        requeued = _mark_player_done(
            con,
            "mayhem",
            new_games_found=2,
            claimed_match_created_ms=0,
            observed_match_created_ms=0,
            requeue_cooldown_ms=45_000,
            new_games_by_queue={2400: 2},
            classic_profile=ClassicAffinityProfile("none", 0, 0, 0, 0, 0),
            claim_lane="general",
        )
        row = con.execute(
            "SELECT status, classic_affinity_rank FROM crawl_queue WHERE puuid='mayhem'"
        ).fetchone()
        self.assertFalse(requeued)
        self.assertEqual(row, ("done", 0))
        con.close()

    def test_producer_floor_backfill_requeues_ejected_lifetime_producer(self) -> None:
        con = sqlite3.connect(":memory:")
        _ensure_schema(con)
        _enqueue_player(con, "ejected", 1, "match", discovered_queue_id=2400)
        con.execute(
            "UPDATE crawl_seen SET new_games_by_queue_json=?, "
            "classic_affinity='none', classic_affinity_rank=0, processed=1 "
            "WHERE puuid='ejected'",
            ('{"4310":5}',),
        )
        con.execute(
            "UPDATE crawl_queue SET status='done', classic_affinity_rank=0 "
            "WHERE puuid='ejected'"
        )
        con.execute(
            "DELETE FROM crawl_runtime_state WHERE state_key=?",
            ("classic_producer_floor_v1_backfill_done",),
        )
        con.commit()
        _ensure_schema(con)
        row = con.execute(
            "SELECT s.classic_affinity, s.classic_affinity_rank, s.processed, "
            "q.status, q.classic_affinity_rank "
            "FROM crawl_seen s JOIN crawl_queue q USING(puuid) WHERE s.puuid='ejected'"
        ).fetchone()
        self.assertEqual(row, ("dormant", 1, 0, "pending", 1))
        con.close()


if __name__ == "__main__":
    unittest.main()
