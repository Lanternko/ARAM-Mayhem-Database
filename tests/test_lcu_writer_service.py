from __future__ import annotations

import pathlib
import sqlite3
import time
from unittest.mock import patch

import pytest

from aram_nn.lcu.snowball import _CLASSIC_RATE_BOOTSTRAP_FLAG
from aram_nn.lcu.writer_service import WriterService


def _game(game_id: str = "g1", captured_at: str = "2026-01-01T00:00:00+00:00", participants=None) -> dict[str, object]:
    return {
        "game_id": game_id,
        "queue_id": 2400,
        "patch": "16.1",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": 1,
        "duration_sec": 900,
        "created_ms": 123,
        "captured_at": captured_at,
        "participants": participants or [{"puuid": "p1"}],
    }


def test_game_claim_expiry_reclaim_and_old_token_stale(tmp_path: pathlib.Path) -> None:
    now = [1000]
    service = WriterService(tmp_path / "games.db", clock_ms=lambda: now[0], claim_lease_ms=10)
    first = service.handle({"version": 1, "command": "game_claim", "request_id": "c1", "game_id": "g", "now_ms": 1000}, channel_id="w1")
    now[0] = 1011
    second = service.handle({"version": 1, "command": "game_claim", "request_id": "c2", "game_id": "g", "now_ms": 1011}, channel_id="w2")
    assert second["generation"] == 2
    stale = service.handle({"version": 1, "command": "commit_game", "request_id": "old", "game_id": "g", "token": first["token"], "generation": first["generation"], "record": _game(), "now_ms": 1011}, channel_id="w1")
    assert stale["status"] == "STALE_CLAIM" and stale["mutated"] is False


def test_game_commit_is_atomic_and_ack_after_commit(tmp_path: pathlib.Path) -> None:
    calls: list[str] = []

    def fail(point: str) -> None:
        calls.append(point)
        if point == "participant_enqueue":
            raise RuntimeError("injected")

    service = WriterService(tmp_path / "games.db", failure_injector=fail)
    claim = service.handle({"version": 1, "command": "game_claim", "request_id": "c", "game_id": "g", "now_ms": 1000}, channel_id="w")
    with pytest.raises(RuntimeError):
        service.handle({"version": 1, "command": "commit_game", "request_id": "commit", "game_id": "g", "token": claim["token"], "generation": claim["generation"], "record": _game(), "now_ms": 1001}, channel_id="w")
    assert service.con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    assert service.con.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0] == 0
    # The claim remains usable because its transaction rolled back.
    service._failure_injector = None
    response = service.handle({"version": 1, "command": "commit_game", "request_id": "commit2", "game_id": "g", "token": claim["token"], "generation": claim["generation"], "record": _game(), "now_ms": 1001}, channel_id="w")
    assert response["status"] == "COMMITTED"
    assert service.con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1


def test_duplicate_request_replay_and_participant_backfill(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    claim = service.handle({"version": 1, "command": "game_claim", "request_id": "c", "game_id": "g", "now_ms": 1000}, channel_id="w")
    sparse = _game(participants=[{"puuid": "p1"}])
    first = service.handle({"version": 1, "command": "commit_game", "request_id": "commit", "game_id": "g", "token": claim["token"], "generation": claim["generation"], "record": sparse, "now_ms": 1001}, channel_id="w")
    assert service.handle({"version": 1, "command": "commit_game", "request_id": "commit", "game_id": "g", "token": claim["token"], "generation": claim["generation"], "record": sparse, "now_ms": 1001}, channel_id="w") == first

    # A later detail response may backfill participant stats but does not create
    # a second game or a second participant queue row.
    claim2 = service.handle({"version": 1, "command": "game_claim", "request_id": "c2", "game_id": "g2", "now_ms": 1000}, channel_id="w")
    rich = _game(game_id="g2", participants=[{"puuid": "p2", "stats": {"kills": 3}}])
    service.handle({"version": 1, "command": "commit_game", "request_id": "g2c", "game_id": "g2", "token": claim2["token"], "generation": claim2["generation"], "record": rich, "now_ms": 1001}, channel_id="w")
    assert service.con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
    assert service.con.execute("SELECT COUNT(*) FROM writer_events").fetchone()[0] == 0


def test_request_replay_is_bound_to_channel_and_canonical_request(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    first = service.handle({"version": 1, "command": "ping", "request_id": "same"}, channel_id="w1")
    assert first["status"] == "PONG"
    with pytest.raises(Exception) as exc:
        service.handle({"version": 1, "command": "ping", "request_id": "same"}, channel_id="w2")
    assert "REQUEST_ID_CONFLICT" in str(exc.value)


def test_watermark_is_monotonic_and_player_finalize_is_atomic(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    for request, game_id, stamp in (("c1", "g1", "2026-02-01T00:00:00+00:00"), ("c2", "g2", "2026-01-01T00:00:00+00:00")):
        claim = service.handle({"version": 1, "command": "game_claim", "request_id": request, "game_id": game_id, "now_ms": 1000}, channel_id="w")
        service.handle({"version": 1, "command": "commit_game", "request_id": request + "x", "game_id": game_id, "token": claim["token"], "generation": claim["generation"], "record": _game(game_id=game_id, captured_at=stamp), "now_ms": 1001}, channel_id="w")
    watermark = service.con.execute("SELECT state_value FROM crawl_runtime_state WHERE state_key='latest_capture:2400'").fetchone()[0]
    assert watermark.startswith("2026-02")

    service.enqueue_player("p")
    claim = service.handle({"version": 1, "command": "player_claim", "request_id": "pc", "puuid": "p", "now_ms": 1000}, channel_id="w")
    result = service.handle({"version": 1, "command": "finalize_player", "request_id": "pf", "puuid": "p", "token": claim["token"], "generation": claim["generation"], "new_games_found": 2, "new_games_by_queue": {"2400": 2}, "now_ms": 1001}, channel_id="w")
    assert result["status"] == "FINALIZED"
    row = service.con.execute("SELECT process_count, new_games_found FROM crawl_seen WHERE puuid='p'").fetchone()
    assert (row[0], row[1]) == (1, 2)


HOUR_MS = 3_600_000


def test_writer_classic_score_claim_prefers_yield_over_wait_time(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    now_ms = int(time.time() * 1000)
    service.enqueue_player("stale-low-yield", discovered_match_created_ms=100)
    service.enqueue_player("fresh-high-yield", discovered_match_created_ms=200)
    service.con.execute(
        "UPDATE crawl_queue SET classic_affinity_rank=1, eligible_at_ms=1, "
        "classic_lambda=0.05, classic_span_ms=?, classic_last_crawl_ms=? "
        "WHERE puuid='stale-low-yield'",
        (100 * HOUR_MS, now_ms - 100 * HOUR_MS),
    )
    service.con.execute(
        "UPDATE crawl_queue SET classic_affinity_rank=3, eligible_at_ms=2, "
        "classic_lambda=3.4, classic_span_ms=?, classic_last_crawl_ms=? "
        "WHERE puuid='fresh-high-yield'",
        (10 * HOUR_MS, now_ms - 10 * HOUR_MS),
    )
    service.con.commit()
    with patch("aram_nn.lcu.snowball.lane_arm", return_value="score"):
        claimed = service.handle(
            {
                "version": 1,
                "command": "snowball_queue",
                "operation": "claim_next",
                "request_id": "classic-score",
                "classic_claim_percent": 100,
                "claim_timeout_ms": 300_000,
                "now_ms": now_ms,
                "worker_id": "W01",
            },
            channel_id="w",
        )
    assert claimed["status"] == "CLAIMED"
    assert claimed["puuid"] == "fresh-high-yield"
    assert claimed["claim_lane"] == "classic_score"


def test_writer_finalize_persists_classic_yield_columns(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    service.enqueue_player("classic")
    claim = service.handle(
        {"version": 1, "command": "player_claim", "request_id": "pc2", "puuid": "classic", "now_ms": 1000},
        channel_id="w",
    )
    result = service.handle(
        {
            "version": 1,
            "command": "finalize_player",
            "request_id": "pf2",
            "puuid": "classic",
            "token": claim["token"],
            "generation": claim["generation"],
            "new_games_found": 2,
            "new_games_by_queue": {"4310": 2},
            "classic_rank": 3,
            "classic_affinity": "heavy",
            "classic_revisit_interval_ms": 10 * HOUR_MS,
            "now_ms": 1000,
        },
        channel_id="w",
    )
    assert result["status"] == "REQUEUED"
    seen = service.con.execute(
        "SELECT classic_rate_num, classic_rate_den, classic_last_crawl_ms "
        "FROM crawl_seen WHERE puuid='classic'"
    ).fetchone()
    queue = service.con.execute(
        "SELECT classic_lambda, classic_span_ms, classic_last_crawl_ms, classic_affinity_rank "
        "FROM crawl_queue WHERE puuid='classic'"
    ).fetchone()
    event = service.con.execute(
        "SELECT lane_arm, claim_score FROM crawl_visit_events WHERE puuid='classic'"
    ).fetchone()
    assert seen[0] == 2.0
    assert seen[1] == 1.0
    assert seen[2] == 1000
    assert queue[0] > 0
    assert queue[1] > 0
    assert queue[2] == 1000
    assert queue[3] == 3
    assert event[0] in {"score", "due"}
    columns = {
        row[1]
        for table in ("crawl_seen", "crawl_queue", "crawl_visit_events")
        for row in service.con.execute(f"PRAGMA table_info({table})")
    }
    assert {
        "classic_rate_num",
        "classic_rate_den",
        "classic_lambda",
        "lane_arm",
    }.issubset(columns)


def test_player_claim_lease_follows_claim_timeout_ms(tmp_path: pathlib.Path) -> None:
    now = [1_000]
    service = WriterService(tmp_path / "games.db", clock_ms=lambda: now[0])
    service.enqueue_player("p")
    claimed = service.handle(
        {
            "version": 1,
            "command": "snowball_queue",
            "operation": "claim_next",
            "request_id": "lease-claim",
            "classic_claim_percent": 0,
            "claim_timeout_ms": 300_000,
            "now_ms": 1_000,
            "worker_id": "w",
        },
        channel_id="w",
    )
    assert claimed["status"] == "CLAIMED"
    now[0] = 1_000 + 60_000 + 1
    result = service.handle(
        {
            "version": 1,
            "command": "snowball_player",
            "operation": "finalize",
            "request_id": "lease-fin",
            "puuid": claimed["puuid"],
            "token": claimed["token"],
            "generation": claimed["generation"],
            "new_games_found": 0,
            "now_ms": now[0],
        },
        channel_id="w",
    )
    assert result["status"] in {"FINALIZED", "REQUEUED"}
    assert result.get("status") != "STALE_CLAIM"


def test_writer_defer_stops_after_empty_history_retry_limit(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    service.enqueue_player("seed", source="manual_riot_id")
    last_claim: dict[str, object] | None = None
    for index in range(5):
        now_ms = 10_000 * (index + 1)
        last_claim = service.handle(
            {
                "version": 1,
                "command": "snowball_queue",
                "operation": "claim_next",
                "request_id": f"defer-c{index}",
                "classic_claim_percent": 0,
                "claim_timeout_ms": 300_000,
                "now_ms": now_ms,
                "worker_id": "w",
            },
            channel_id="w",
        )
        assert last_claim["status"] == "CLAIMED", last_claim
        result = service.handle(
            {
                "version": 1,
                "command": "snowball_player",
                "operation": "defer",
                "request_id": f"defer-d{index}",
                "puuid": last_claim["puuid"],
                "token": last_claim["token"],
                "generation": last_claim["generation"],
                "delay_ms": 0,
                "now_ms": now_ms,
                "reason": "history_hydration",
            },
            channel_id="w",
        )
        assert result["status"] == "REQUEUED"
    count = service.con.execute(
        "SELECT process_count FROM crawl_seen WHERE puuid='seed'"
    ).fetchone()[0]
    assert count == 5
    last_claim = service.handle(
        {
            "version": 1,
            "command": "snowball_queue",
            "operation": "claim_next",
            "request_id": "defer-c5",
            "classic_claim_percent": 0,
            "claim_timeout_ms": 300_000,
            "now_ms": 100_000,
            "worker_id": "w",
        },
        channel_id="w",
    )
    assert last_claim["status"] == "CLAIMED", last_claim
    limited = service.handle(
        {
            "version": 1,
            "command": "snowball_player",
            "operation": "defer",
            "request_id": "defer-d5",
            "puuid": last_claim["puuid"],
            "token": last_claim["token"],
            "generation": last_claim["generation"],
            "delay_ms": 0,
            "now_ms": 100_000,
            "reason": "history_hydration",
        },
        channel_id="w",
    )
    assert limited["status"] == "RETRY_LIMIT"
    assert limited["mutated"] is False
    count = service.con.execute(
        "SELECT process_count FROM crawl_seen WHERE puuid='seed'"
    ).fetchone()[0]
    assert count == 5
    finished = service.handle(
        {
            "version": 1,
            "command": "finalize_player",
            "request_id": "defer-f",
            "puuid": "seed",
            "token": last_claim["token"],
            "generation": last_claim["generation"],
            "new_games_found": 0,
            "now_ms": 100_000,
        },
        channel_id="w",
    )
    assert finished["status"] in {"FINALIZED", "REQUEUED"}


_PRE_ESTIMATOR_SCHEMA = """
CREATE TABLE crawl_runtime_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    queue_id INTEGER NOT NULL,
    patch TEXT NOT NULL,
    blue_champs TEXT NOT NULL,
    red_champs TEXT NOT NULL,
    blue_wins INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE TABLE crawl_seen (
    puuid TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'match',
    priority INTEGER NOT NULL DEFAULT 10,
    min_depth INTEGER NOT NULL DEFAULT 0,
    discovered_from_game_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_crawled_at TEXT,
    process_count INTEGER NOT NULL DEFAULT 0,
    new_games_found INTEGER NOT NULL DEFAULT 0,
    new_games_by_queue_json TEXT,
    latest_seen_match_created_ms INTEGER NOT NULL DEFAULT 0,
    last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    seed_family TEXT NOT NULL DEFAULT 'match',
    discovered_queue_id INTEGER NOT NULL DEFAULT 0,
    classic_affinity TEXT NOT NULL DEFAULT 'none',
    classic_affinity_rank INTEGER NOT NULL DEFAULT 0,
    classic_games_24h INTEGER NOT NULL DEFAULT 0,
    classic_games_recent INTEGER NOT NULL DEFAULT 0,
    classic_last_seen_ms INTEGER NOT NULL DEFAULT 0,
    classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE crawl_queue (
    queue_idx INTEGER PRIMARY KEY AUTOINCREMENT,
    puuid TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'match',
    priority INTEGER NOT NULL DEFAULT 10,
    discovered_from_game_id TEXT,
    discovered_match_created_ms INTEGER NOT NULL DEFAULT 0,
    enqueued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at_ms INTEGER NOT NULL DEFAULT 0,
    eligible_at_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    seed_family TEXT NOT NULL DEFAULT 'match',
    discovered_queue_id INTEGER NOT NULL DEFAULT 0,
    classic_affinity_rank INTEGER NOT NULL DEFAULT 0
);
"""


def _pre_estimator_db(path: pathlib.Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(_PRE_ESTIMATOR_SCHEMA)
    con.execute(
        "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, "
        "blue_wins, duration_sec, created_ms, captured_at) "
        "VALUES ('g4310', 4310, '16.15', '[]', '[]', 1, 1800, 1, '2026-08-01 00:00:00')"
    )
    con.execute(
        """
        INSERT INTO crawl_seen(
            puuid, source, priority, min_depth, discovered_from_game_id,
            first_seen_at, last_crawled_at, process_count, new_games_found,
            new_games_by_queue_json, latest_seen_match_created_ms,
            last_crawled_match_created_ms, processed, seed_family,
            discovered_queue_id, classic_affinity, classic_affinity_rank,
            classic_revisit_interval_ms
        ) VALUES (
            'visited-classic', 'match', 10, 1, 'g4310',
            '2026-08-01 00:00:00', '2026-08-20 00:00:00', 8, 4,
            '{"4310":4}', 1, 1, 0, 'match',
            4310, 'regular', 2, 36000000
        )
        """
    )
    con.execute(
        """
        INSERT INTO crawl_queue(
            puuid, depth, source, priority, discovered_from_game_id,
            discovered_match_created_ms, enqueued_at, updated_at,
            claimed_by, claimed_at_ms, eligible_at_ms, status,
            seed_family, discovered_queue_id, classic_affinity_rank
        ) VALUES (
            'visited-classic', 1, 'match', 10, 'g4310', 1,
            '2026-08-01 00:00:00', '2026-08-20 00:00:00',
            NULL, 0, 0, 'pending', 'match', 4310, 2
        )
        """
    )
    con.execute(
        """
        INSERT INTO crawl_seen(
            puuid, source, priority, min_depth, discovered_from_game_id,
            first_seen_at, last_crawled_at, process_count, new_games_found,
            new_games_by_queue_json, latest_seen_match_created_ms,
            last_crawled_match_created_ms, processed, seed_family,
            discovered_queue_id, classic_affinity, classic_affinity_rank
        ) VALUES (
            'legacy-classic', 'match', 10, 1, 'g4310',
            '2026-08-01 00:00:00', NULL, 0, 0,
            NULL, 1, 0, 1, 'match',
            0, 'none', 0
        )
        """
    )
    con.execute(
        """
        INSERT INTO crawl_queue(
            puuid, depth, source, priority, discovered_from_game_id,
            discovered_match_created_ms, enqueued_at, updated_at,
            claimed_by, claimed_at_ms, eligible_at_ms, status,
            seed_family, discovered_queue_id, classic_affinity_rank
        ) VALUES (
            'legacy-classic', 1, 'match', 10, 'g4310', 1,
            '2026-08-01 00:00:00', '2026-08-01 00:00:00',
            NULL, 0, 0, 'done', 'match', 0, 0
        )
        """
    )
    con.commit()
    con.close()


def test_writer_bootstraps_classic_yield_on_pre_estimator_db(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "pre.db"
    _pre_estimator_db(db_path)
    service = WriterService(db_path)
    try:
        columns = {
            row[1]
            for table in ("crawl_seen", "crawl_queue")
            for row in service.con.execute(f"PRAGMA table_info({table})")
        }
        assert {"classic_rate_num", "classic_rate_den", "classic_lambda"}.issubset(columns)
        index = service.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_crawl_queue_classic_claim'"
        ).fetchone()
        assert index is not None
        flag = service.con.execute(
            "SELECT 1 FROM crawl_runtime_state WHERE state_key=?",
            (_CLASSIC_RATE_BOOTSTRAP_FLAG,),
        ).fetchone()
        assert flag is not None
        seen = service.con.execute(
            "SELECT classic_rate_num, classic_rate_den, classic_last_crawl_ms "
            "FROM crawl_seen WHERE puuid='visited-classic'"
        ).fetchone()
        queue = service.con.execute(
            "SELECT classic_lambda, classic_span_ms, classic_last_crawl_ms, "
            "classic_affinity_rank FROM crawl_queue WHERE puuid='visited-classic'"
        ).fetchone()
        assert seen[0] == pytest.approx(4.0)
        assert seen[1] == pytest.approx(8.0)
        assert seen[2] > 0
        assert queue[0] > 0
        assert queue[1] > 0
        assert queue[2] == seen[2]
        assert queue[3] == 2
        legacy = service.con.execute(
            "SELECT s.classic_affinity, s.classic_affinity_rank, s.processed, "
            "q.status, q.classic_affinity_rank, q.classic_lambda "
            "FROM crawl_seen s JOIN crawl_queue q USING(puuid) "
            "WHERE s.puuid='legacy-classic'"
        ).fetchone()
        assert legacy[0] == "candidate"
        assert legacy[1] >= 1
        assert legacy[2] == 0
        assert legacy[3] == "pending"
        assert legacy[4] >= 1
        assert legacy[5] > 0
    finally:
        service.close()

    again = WriterService(db_path)
    try:
        seen = again.con.execute(
            "SELECT classic_rate_num, classic_rate_den "
            "FROM crawl_seen WHERE puuid='visited-classic'"
        ).fetchone()
        assert seen[0] == pytest.approx(4.0)
        assert seen[1] == pytest.approx(8.0)
    finally:
        again.close()


def test_writer_producer_floor_keeps_a_quiet_lifetime_producer_in_the_lane(
    tmp_path: pathlib.Path,
) -> None:
    """RPC mode is what production runs, so the floor has to hold here too.

    snowball._mark_player_done has the same rule, but the writer reimplements
    finalize independently -- a floor only in the direct-SQLite path would pass
    its own tests and never execute against the live crawler.
    """
    service = WriterService(tmp_path / "games.db")
    service.enqueue_player("producer")
    service.con.execute(
        "UPDATE crawl_seen SET new_games_by_queue_json=? WHERE puuid='producer'",
        ('{"4310":3}',),
    )
    service.con.commit()
    claim = service.handle(
        {"version": 1, "command": "player_claim", "request_id": "pc", "puuid": "producer", "now_ms": 1000},
        channel_id="w",
    )
    result = service.handle(
        {
            "version": 1,
            "command": "finalize_player",
            "request_id": "pf",
            "puuid": "producer",
            "token": claim["token"],
            "generation": claim["generation"],
            "new_games_found": 0,
            "new_games_by_queue": {},
            # The producer saw a quiet 20-row window and classified rank 0.
            "classic_affinity": "none",
            "classic_rank": 0,
            "now_ms": 1001,
        },
        channel_id="w",
    )
    # REQUEUED rather than FINALIZED is the floor firing: rank 1 puts the player
    # back on the frontier instead of parking them as done.
    assert result["status"] == "REQUEUED"
    seen = tuple(service.con.execute(
        "SELECT classic_affinity, classic_affinity_rank, processed "
        "FROM crawl_seen WHERE puuid='producer'"
    ).fetchone())
    queue = tuple(service.con.execute(
        "SELECT status, classic_affinity_rank FROM crawl_queue WHERE puuid='producer'"
    ).fetchone())
    assert seen == ("dormant", 1, 0)
    assert queue == ("pending", 1)


def test_writer_true_none_player_still_leaves_the_lane(tmp_path: pathlib.Path) -> None:
    """The floor must key on evidence, not just soften every rank-0 result."""
    service = WriterService(tmp_path / "games.db")
    service.enqueue_player("mayhem")
    claim = service.handle(
        {"version": 1, "command": "player_claim", "request_id": "pc", "puuid": "mayhem", "now_ms": 1000},
        channel_id="w",
    )
    service.handle(
        {
            "version": 1,
            "command": "finalize_player",
            "request_id": "pf",
            "puuid": "mayhem",
            "token": claim["token"],
            "generation": claim["generation"],
            "new_games_found": 2,
            "new_games_by_queue": {"2400": 2},
            "classic_affinity": "none",
            "classic_rank": 0,
            "now_ms": 1001,
        },
        channel_id="w",
    )
    seen = tuple(service.con.execute(
        "SELECT classic_affinity, classic_affinity_rank FROM crawl_seen WHERE puuid='mayhem'"
    ).fetchone())
    assert seen == ("none", 0)
