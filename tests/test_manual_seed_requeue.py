"""Manual (OPGG) seed roots must not jump the queue on every fleet start.

Live on 2026-11-09 the seed pass stamped ``discovered_match_created_ms=now`` on
every root it resolved, which always beat the root's last crawled match, so the
same ~60 roots were re-crawled each ~35-minute restart cycle for ~0.1 new games
per visit while the rest of the 6k-root seed file was never reached.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from aram_nn.lcu import snowball
from aram_nn.lcu.snowball import (
    _MANUAL_SEED_CURSOR_KEY,
    _enqueue_player,
    _ensure_schema,
    _get_runtime_state_text,
    _manual_seed_recently_crawled,
    _seed_manual_riot_ids,
)
from aram_nn.lcu.writer_service import WriterService


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_recently_crawled_only_applies_to_manual_roots() -> None:
    now = _now_ms()
    assert _manual_seed_recently_crawled("manual_riot_id", _ago(1), now)
    assert not _manual_seed_recently_crawled("manual_riot_id", _ago(30), now)
    assert not _manual_seed_recently_crawled("manual_riot_id", None, now)
    assert not _manual_seed_recently_crawled("manual_riot_id", "not-a-date", now)
    # Match rediscovery keeps its own revisit policy.
    assert not _manual_seed_recently_crawled("match", _ago(1), now)


def _crawled_seed_db(last_crawled_at: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    _ensure_schema(con)
    first_seen = _ago(24 * 10)
    con.execute(
        """
        INSERT INTO crawl_seen (
            puuid, source, priority, min_depth, first_seen_at, last_crawled_at,
            process_count, new_games_found, latest_seen_match_created_ms,
            last_crawled_match_created_ms, processed, seed_family
        ) VALUES ('seed', 'manual_riot_id', 0, 0, ?, ?, 20, 10, 100, 100, 1, 'manual_riot_id')
        """,
        (first_seen, last_crawled_at),
    )
    con.execute(
        """
        INSERT INTO crawl_queue (
            puuid, depth, source, priority, discovered_match_created_ms,
            enqueued_at, updated_at, status, eligible_at_ms, seed_family
        ) VALUES ('seed', 0, 'manual_riot_id', 0, 100, ?, ?, 'done', 0, 'manual_riot_id')
        """,
        (first_seen, first_seen),
    )
    con.commit()
    return con


@pytest.mark.parametrize(("hours", "expected"), [(1, "noop"), (30, "requeued")])
def test_sqlite_seed_pass_requeues_only_roots_outside_hot_window(hours: float, expected: str) -> None:
    con = _crawled_seed_db(_ago(hours))
    result = _enqueue_player(
        con, "seed", depth=0, source="manual_riot_id",
        discovered_match_created_ms=_now_ms(), seed_family="manual_riot_id",
    )
    status = con.execute("SELECT status FROM crawl_queue WHERE puuid='seed'").fetchone()[0]
    latest = con.execute(
        "SELECT latest_seen_match_created_ms FROM crawl_seen WHERE puuid='seed'"
    ).fetchone()[0]
    assert result == expected
    if expected == "noop":
        assert status == "done"
        # "now" is not a real match time; it must not leak into the watermark.
        assert latest == 100
    else:
        assert status == "pending"
    con.close()


def test_sqlite_match_rediscovery_of_recent_root_still_requeues() -> None:
    """A root seen in a genuinely newer game is not a seed-pass stamp."""
    con = _crawled_seed_db(_ago(1))
    result = _enqueue_player(
        con, "seed", depth=1, source="match", discovered_from_game_id="g",
        discovered_match_created_ms=200, seed_family="manual_riot_id",
    )
    assert result == "requeued"
    con.close()


@pytest.mark.parametrize(("hours", "expected"), [(1, "noop"), (30, "requeued")])
def test_writer_seed_pass_requeues_only_roots_outside_hot_window(
    tmp_path: pathlib.Path, hours: float, expected: str
) -> None:
    service = WriterService(tmp_path / "games.db")
    service.enqueue_player("seed", source="manual_riot_id", discovered_match_created_ms=100)
    service.con.execute(
        "UPDATE crawl_seen SET processed=1, process_count=20, new_games_found=10, "
        "first_seen_at=?, last_crawled_at=?, last_crawled_match_created_ms=100 "
        "WHERE puuid='seed'",
        (_ago(24 * 10), _ago(hours)),
    )
    service.con.execute("UPDATE crawl_queue SET status='done', eligible_at_ms=0 WHERE puuid='seed'")
    service.con.commit()
    result = service.handle(
        {
            "version": 1,
            "command": "snowball_queue",
            "operation": "enqueue",
            "request_id": f"seed-pass-{hours}",
            "puuid": "seed",
            "source": "manual_riot_id",
            "seed_family": "manual_riot_id",
            "discovered_match_created_ms": _now_ms(),
        },
        channel_id="w",
    )
    status = service.con.execute("SELECT status FROM crawl_queue WHERE puuid='seed'").fetchone()[0]
    latest = service.con.execute(
        "SELECT latest_seen_match_created_ms FROM crawl_seen WHERE puuid='seed'"
    ).fetchone()[0]
    assert result["result"] == expected
    if expected == "noop":
        assert status == "done"
        assert latest == 100
    else:
        assert status == "pending"


def test_seed_pass_resumes_from_persisted_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    con = sqlite3.connect(":memory:")
    _ensure_schema(con)
    riot_ids = tuple(f"seed{i:02d}#TW2" for i in range(25))
    lookups: list[list[str]] = []

    def fake_lookup(_lcu, names):
        lookups.append(list(names))
        out = []
        for name in names:
            game_name, tag = name.split("#", 1)
            out.append({"gameName": game_name, "tagLine": tag, "puuid": f"puuid-{game_name}"})
        return out

    monkeypatch.setattr(snowball, "lookup_summoners_by_riot_ids", fake_lookup)

    def run() -> int:
        return _seed_manual_riot_ids(
            con, object(), riot_ids=riot_ids, target_queues={2400},
            history_window=20, games_per_player=None, pending_cap=5,
        )

    assert run() == 5
    assert _get_runtime_state_text(con, _MANUAL_SEED_CURSOR_KEY) == "5"
    assert lookups[0][0] == riot_ids[0]

    # Workers crawl the five roots; the next fleet start must move on to new
    # roots instead of re-resolving the ones it just finished.
    con.execute("UPDATE crawl_queue SET status='done'")
    con.execute("UPDATE crawl_seen SET processed=1, last_crawled_at=?", (_ago(0.5),))
    con.commit()
    lookups.clear()
    assert run() == 5
    assert lookups[0][0] == riot_ids[5]
    assert _get_runtime_state_text(con, _MANUAL_SEED_CURSOR_KEY) == "10"
    con.close()
