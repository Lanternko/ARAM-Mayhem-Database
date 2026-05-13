"""Snowball crawl recent LCU-visible match history across discovered players.

The LCU match-history list endpoint usually exposes only the last ~20 games for a puuid.
This crawler persists two separate crawl structures in SQLite:

1. `crawl_seen`: de-dup set of discovered puuids plus crawl metadata
2. `crawl_queue`: persistent priority queue of pending / in-progress / done nodes

That means we can pause at any time, then resume from the saved queue state.
Newer discovered matches get higher priority because they are more likely to be
current-patch and from active players. Exact match de-duplication still uses game_id,
not champion composition.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .client import (
    LCUClient,
    get_apex_league,
    get_current_summoner,
    get_friends,
    get_game_detail,
    get_league_ladders,
    get_match_history,
    lookup_summoners_by_riot_ids,
)
from .poller import DEFAULT_QUEUES, _parse_game_detail
from .process import get_credentials

_EMPTY_QUEUE_GRACE_SEC = 30.0

_CREATE_GAMES_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    queue_id     INTEGER NOT NULL,
    patch        TEXT NOT NULL,
    blue_champs  TEXT NOT NULL,
    red_champs   TEXT NOT NULL,
    blue_wins    INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_ms   INTEGER NOT NULL,
    captured_at  TEXT NOT NULL,
    participants_json TEXT
);
"""

_CREATE_CRAWL_SEEN_SQL = """
CREATE TABLE IF NOT EXISTS crawl_seen (
    puuid                         TEXT PRIMARY KEY,
    source                        TEXT NOT NULL,
    priority                      INTEGER NOT NULL,
    min_depth                     INTEGER NOT NULL,
    discovered_from_game_id       TEXT,
    first_seen_at                 TEXT NOT NULL,
    last_crawled_at               TEXT,
    process_count                 INTEGER NOT NULL DEFAULT 0,
    new_games_found               INTEGER NOT NULL DEFAULT 0,
    latest_seen_match_created_ms  INTEGER NOT NULL DEFAULT 0,
    last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0,
    processed                     INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_CRAWL_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_queue (
    queue_idx                   INTEGER PRIMARY KEY AUTOINCREMENT,
    puuid                       TEXT NOT NULL UNIQUE,
    depth                       INTEGER NOT NULL,
    source                      TEXT NOT NULL,
    priority                    INTEGER NOT NULL,
    discovered_from_game_id     TEXT,
    discovered_match_created_ms INTEGER NOT NULL DEFAULT 0,
    enqueued_at                 TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    claimed_by                  TEXT,
    claimed_at_ms               INTEGER NOT NULL DEFAULT 0,
    eligible_at_ms              INTEGER NOT NULL DEFAULT 0,
    status                      TEXT NOT NULL DEFAULT 'pending'
);
"""

_CREATE_CRAWL_QUEUE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_queue_status_priority
ON crawl_queue(
    status,
    eligible_at_ms,
    discovered_match_created_ms DESC,
    priority ASC,
    depth ASC,
    updated_at ASC,
    queue_idx ASC
);
"""

_CREATE_CRAWL_GAME_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS crawl_game_claims (
    game_id        TEXT PRIMARY KEY,
    claimed_by     TEXT,
    claimed_at_ms  INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
);
"""

_CREATE_RIOT_ID_BRIDGE_SQL = """
CREATE TABLE IF NOT EXISTS riot_id_bridge (
    public_puuid   TEXT PRIMARY KEY,
    riot_id        TEXT NOT NULL,
    lcu_puuid      TEXT,
    resolved_at    TEXT NOT NULL,
    resolve_status TEXT NOT NULL
);
"""

_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_game_claims_status
ON crawl_game_claims(status, claimed_at_ms, updated_at, game_id);
"""

_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450}
_SOURCE_PRIORITY = {
    "self": 0,
    "match": 10,
    # Leaderboard / manual seeds should only open new communities; once a seed
    # produces real matches, we want those fresher match-derived nodes first.
    "friend": 20,
    "apex": 30,
    "ladder": 40,
    "manual_riot_id": 60,
    "riot_tier": 70,
}
_LCU_RIOT_ID_LOOKUP_BATCH = 10
_RIOT_TIER_HYDRATION_DELAY_MS = 90_000


@dataclass
class CrawlStats:
    seeded_players: int = 0
    processed_players: int = 0
    expanded_games: int = 0
    saved_games: int = 0
    existing_games: int = 0
    filtered_games: int = 0
    failed_games: int = 0
    requeued_players: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _get_current_summoner_with_retry(lcu: LCUClient, attempts: int = 5, sleep_sec: float = 1.0) -> dict | None:
    for idx in range(max(1, attempts)):
        data = get_current_summoner(lcu)
        if data and data.get("puuid"):
            return data
        if idx + 1 < attempts:
            time.sleep(sleep_sec)
    return None


def _connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_column(con: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if column_name not in _table_columns(con, table_name):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _lookup_game_created_ms(con: sqlite3.Connection, game_id: str | None) -> int:
    if not game_id:
        return 0
    row = con.execute(
        "SELECT created_ms FROM games WHERE game_id = ?",
        (str(game_id),),
    ).fetchone()
    return int(row[0]) if row else 0


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(_CREATE_GAMES_SQL)
    con.execute(_CREATE_CRAWL_SEEN_SQL)
    con.execute(_CREATE_CRAWL_QUEUE_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_SQL)
    con.execute(_CREATE_RIOT_ID_BRIDGE_SQL)

    _ensure_column(
        con,
        "games",
        "participants_json",
        "participants_json TEXT",
    )

    _ensure_column(
        con,
        "crawl_seen",
        "latest_seen_match_created_ms",
        "latest_seen_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_seen",
        "last_crawled_match_created_ms",
        "last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "discovered_match_created_ms",
        "discovered_match_created_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "updated_at",
        "updated_at TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "claimed_by",
        "claimed_by TEXT",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "claimed_at_ms",
        "claimed_at_ms INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "eligible_at_ms",
        "eligible_at_ms INTEGER NOT NULL DEFAULT 0",
    )

    con.execute(_CREATE_CRAWL_QUEUE_INDEX_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL)

    if _table_exists(con, "crawl_queue"):
        con.execute(
            """
            UPDATE crawl_queue
            SET updated_at = CASE
                WHEN updated_at = '' THEN enqueued_at
                ELSE updated_at
            END
            """
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET discovered_match_created_ms = COALESCE(
                (
                    SELECT games.created_ms
                    FROM games
                    WHERE games.game_id = crawl_queue.discovered_from_game_id
                ),
                discovered_match_created_ms,
                0
            )
            WHERE discovered_match_created_ms = 0
              AND discovered_from_game_id IS NOT NULL
            """
        )
    if _table_exists(con, "crawl_seen"):
        con.execute(
            """
            UPDATE crawl_seen
            SET latest_seen_match_created_ms = COALESCE(
                (
                    SELECT games.created_ms
                    FROM games
                    WHERE games.game_id = crawl_seen.discovered_from_game_id
                ),
                latest_seen_match_created_ms,
                0
            )
            WHERE latest_seen_match_created_ms = 0
              AND discovered_from_game_id IS NOT NULL
            """
        )
        con.execute(
            """
            UPDATE crawl_seen
            SET last_crawled_match_created_ms = latest_seen_match_created_ms
            WHERE processed = 1 AND last_crawled_match_created_ms = 0
            """
        )
    con.commit()


def _purge_invalid_riot_tier_rows(con: sqlite3.Connection) -> int:
    rows = con.execute(
        """
        SELECT COUNT(*)
        FROM crawl_seen
        WHERE source = 'riot_tier'
          AND length(puuid) != 36
        """
    ).fetchone()
    removed = int(rows[0]) if rows else 0
    if removed <= 0:
        return 0
    con.execute(
        """
        DELETE FROM crawl_queue
        WHERE source = 'riot_tier'
          AND length(puuid) != 36
        """
    )
    con.execute(
        """
        DELETE FROM crawl_seen
        WHERE source = 'riot_tier'
          AND length(puuid) != 36
        """
    )
    con.commit()
    return removed


def _sync_source_priorities(con: sqlite3.Connection) -> int:
    updated = 0
    for source, priority in _SOURCE_PRIORITY.items():
        before = con.total_changes
        con.execute(
            """
            UPDATE crawl_seen
            SET priority = ?
            WHERE source = ?
              AND priority != ?
            """,
            (priority, source, priority),
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET priority = ?
            WHERE source = ?
              AND priority != ?
            """,
            (priority, source, priority),
        )
        updated += con.total_changes - before
    if updated:
        con.commit()
    return updated


def _migrate_legacy_crawl_players(con: sqlite3.Connection) -> int:
    """One-time migration from the older crawl_players frontier schema."""
    if not _table_exists(con, "crawl_players"):
        return 0
    if con.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0] > 0:
        return 0
    if con.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0] > 0:
        return 0

    rows = con.execute(
        """
        SELECT puuid, source, priority, depth, discovered_from_game_id, status,
               first_seen_at, last_crawled_at, process_count, new_games_found
        FROM crawl_players
        ORDER BY priority ASC, depth ASC, first_seen_at ASC
        """
    ).fetchall()
    for (
        puuid,
        source,
        priority,
        depth,
        discovered_from_game_id,
        status,
        first_seen_at,
        last_crawled_at,
        process_count,
        new_games_found,
    ) in rows:
        discovered_ms = _lookup_game_created_ms(con, discovered_from_game_id)
        processed = 1 if status == "done" else 0
        queue_status = "done" if processed else "pending"
        last_crawled_ms = discovered_ms if processed else 0

        con.execute(
            """
            INSERT OR IGNORE INTO crawl_seen (
                puuid, source, priority, min_depth, discovered_from_game_id,
                first_seen_at, last_crawled_at, process_count, new_games_found,
                latest_seen_match_created_ms, last_crawled_match_created_ms, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                puuid,
                source,
                priority,
                depth,
                discovered_from_game_id,
                first_seen_at,
                last_crawled_at,
                process_count,
                new_games_found,
                discovered_ms,
                last_crawled_ms,
                processed,
            ),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO crawl_queue (
                puuid, depth, source, priority, discovered_from_game_id,
                discovered_match_created_ms, enqueued_at, updated_at,
                claimed_by, claimed_at_ms, eligible_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)
            """,
            (
                puuid,
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_ms,
                first_seen_at,
                first_seen_at,
                queue_status,
            ),
        )
    con.commit()
    return len(rows)


def _queue_id_from_meta(game: dict) -> int:
    queue_id = int(game.get("queueId", -1))
    if queue_id != -1:
        return queue_id
    return _MODE_TO_QUEUE.get(str(game.get("gameMode", "")), -1)


def _extract_target_game_ids(history: list[dict], target_queues: set[int]) -> list[str]:
    game_ids: list[str] = []
    for game in history:
        queue_id = _queue_id_from_meta(game)
        game_id = game.get("gameId")
        if queue_id in target_queues and game_id is not None:
            game_ids.append(str(game_id))
    return game_ids


def _latest_target_match_created_ms(history: list[dict], target_queues: set[int]) -> int:
    latest = 0
    for game in history:
        queue_id = _queue_id_from_meta(game)
        if queue_id not in target_queues:
            continue
        created_ms = int(game.get("gameCreation") or 0)
        if created_ms > latest:
            latest = created_ms
    return latest


def _extract_participant_puuids(detail: dict) -> list[str]:
    puuids: list[str] = []
    for ident in detail.get("participantIdentities") or []:
        player = ident.get("player") or {}
        puuid = player.get("puuid")
        if puuid:
            puuids.append(str(puuid))
    return puuids


def _claim_game_id(
    con: sqlite3.Connection,
    game_id: str,
    worker_id: str,
    claim_timeout_ms: int,
) -> bool:
    now_text = _utc_now()
    now_ms = _now_ms()
    cutoff_ms = now_ms - claim_timeout_ms

    con.execute("BEGIN IMMEDIATE")
    if con.execute("SELECT 1 FROM games WHERE game_id = ?", (game_id,)).fetchone():
        con.commit()
        return False

    con.execute(
        """
        UPDATE crawl_game_claims
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (now_text, cutoff_ms),
    )
    row = con.execute(
        """
        SELECT status, claimed_at_ms
        FROM crawl_game_claims
        WHERE game_id = ?
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        con.execute(
            """
            INSERT INTO crawl_game_claims (
                game_id, claimed_by, claimed_at_ms, updated_at, status
            ) VALUES (?, ?, ?, ?, 'in_progress')
            """,
            (game_id, worker_id, now_ms, now_text),
        )
        con.commit()
        return True

    status, claimed_at_ms = row
    if str(status) == "done":
        con.commit()
        return False
    if str(status) == "pending" or int(claimed_at_ms) < cutoff_ms:
        con.execute(
            """
            UPDATE crawl_game_claims
            SET status = 'in_progress',
                claimed_by = ?,
                claimed_at_ms = ?,
                updated_at = ?
            WHERE game_id = ?
            """,
            (worker_id, now_ms, now_text, game_id),
        )
        con.commit()
        return True

    con.commit()
    return False


def _mark_game_done(con: sqlite3.Connection, game_id: str) -> None:
    now_text = _utc_now()
    con.execute(
        """
        INSERT INTO crawl_game_claims (
            game_id, claimed_by, claimed_at_ms, updated_at, status
        ) VALUES (?, NULL, 0, ?, 'done')
        ON CONFLICT(game_id) DO UPDATE SET
            status = 'done',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = excluded.updated_at
        """,
        (game_id, now_text),
    )
    con.commit()


def _release_game_claim(con: sqlite3.Connection, game_id: str) -> None:
    con.execute(
        """
        UPDATE crawl_game_claims
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE game_id = ?
        """,
        (_utc_now(), game_id),
    )
    con.commit()


def _insert_game(con: sqlite3.Connection, record: dict) -> bool:
    before = con.total_changes
    con.execute(
        """
        INSERT OR IGNORE INTO games (
            game_id, queue_id, patch, blue_champs, red_champs,
            blue_wins, duration_sec, created_ms, captured_at, participants_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record["game_id"],
            record["queue_id"],
            record["patch"],
            json.dumps(record["blue_champs"]),
            json.dumps(record["red_champs"]),
            record["blue_wins"],
            record["duration_sec"],
            record["created_ms"],
            record["captured_at"],
            json.dumps(record.get("participants", []), separators=(",", ":")),
        ),
    )
    con.commit()
    return con.total_changes > before


def _backfill_participants_json(con: sqlite3.Connection, record: dict) -> bool:
    before = con.total_changes
    con.execute(
        """
        UPDATE games
        SET participants_json = ?
        WHERE game_id = ?
          AND (participants_json IS NULL OR participants_json = '')
        """,
        (
            json.dumps(record.get("participants", []), separators=(",", ":")),
            record["game_id"],
        ),
    )
    con.commit()
    return con.total_changes > before


def _load_existing_game_ids(con: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in con.execute("SELECT game_id FROM games").fetchall()}


def _pick_best_metadata(
    old_source: str,
    old_priority: int,
    old_depth: int,
    new_source: str,
    new_priority: int,
    new_depth: int,
) -> tuple[str, int, int]:
    best_source = old_source
    best_priority = old_priority
    best_depth = old_depth
    if new_priority < old_priority or (new_priority == old_priority and new_depth < old_depth):
        best_source = new_source
        best_priority = new_priority
    if new_depth < old_depth:
        best_depth = new_depth
    return best_source, best_priority, best_depth


def _upsert_queue_row(
    con: sqlite3.Connection,
    puuid: str,
    depth: int,
    source: str,
    priority: int,
    discovered_from_game_id: str | None,
    discovered_match_created_ms: int,
    requeue: bool,
    eligible_at_ms: int = 0,
) -> bool:
    """Insert or refresh a queue row. Returns True if it became pending now."""
    now = _utc_now()
    row = con.execute(
        """
        SELECT status, priority, depth, discovered_match_created_ms
        FROM crawl_queue
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()

    if row is None:
        con.execute(
            """
            INSERT INTO crawl_queue (
                puuid, depth, source, priority, discovered_from_game_id,
                discovered_match_created_ms, enqueued_at, updated_at,
                claimed_by, claimed_at_ms, eligible_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 'pending')
            """,
            (
                puuid,
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_match_created_ms,
                now,
                now,
                eligible_at_ms,
            ),
        )
        con.commit()
        return True

    queue_status, queue_priority, queue_depth, queue_match_ms = row
    became_pending = False
    if str(queue_status) != "pending" and requeue:
        con.execute(
            """
            UPDATE crawl_queue
            SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                discovered_match_created_ms = ?, updated_at = ?, eligible_at_ms = ?,
                claimed_by = NULL, claimed_at_ms = 0, status = 'pending'
            WHERE puuid = ?
            """,
            (
                depth,
                source,
                priority,
                discovered_from_game_id,
                discovered_match_created_ms,
                now,
                eligible_at_ms,
                puuid,
            ),
        )
        became_pending = True
    elif str(queue_status) in ("pending", "in_progress"):
        should_update = (
            discovered_match_created_ms > int(queue_match_ms)
            or priority < int(queue_priority)
            or depth < int(queue_depth)
        )
        if should_update:
            con.execute(
                f"""
                UPDATE crawl_queue
                SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                    discovered_match_created_ms = ?, updated_at = ?
                    {", claimed_by = NULL, claimed_at_ms = 0" if str(queue_status) == "pending" else ""}
                WHERE puuid = ?
                """,
                (
                    depth,
                    source,
                    priority,
                    discovered_from_game_id,
                    discovered_match_created_ms,
                    now,
                    puuid,
                ),
            )
    con.commit()
    return became_pending


def _enqueue_player(
    con: sqlite3.Connection,
    puuid: str,
    depth: int,
    source: str,
    discovered_from_game_id: str | None = None,
    discovered_match_created_ms: int = 0,
    requeue_cooldown_ms: int = 0,
    initial_delay_ms: int = 0,
) -> str:
    """Add puuid to seen-set and queue when needed.

    Returns:
      - 'new' if the puuid was unseen and newly queued
      - 'requeued' if it had been processed before and a newer match reactivated it
      - 'updated' if metadata / priority changed but it was already queued or in progress
      - 'noop' otherwise
    """
    if not puuid:
        return "noop"

    now = _utc_now()
    priority = _SOURCE_PRIORITY.get(source, 99)
    row = con.execute(
        """
        SELECT source, priority, min_depth, discovered_from_game_id,
               latest_seen_match_created_ms, last_crawled_match_created_ms, processed
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()

    if row is None:
        con.execute(
            """
            INSERT INTO crawl_seen (
                puuid, source, priority, min_depth, discovered_from_game_id,
                first_seen_at, latest_seen_match_created_ms,
                last_crawled_match_created_ms, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                puuid,
                source,
                priority,
                depth,
                discovered_from_game_id,
                now,
                discovered_match_created_ms,
            ),
        )
        con.commit()
        _upsert_queue_row(
            con,
            puuid,
            depth,
            source,
            priority,
            discovered_from_game_id,
            discovered_match_created_ms,
            requeue=True,
            eligible_at_ms=_now_ms() + max(0, initial_delay_ms),
        )
        return "new"

    (
        old_source,
        old_priority,
        old_depth,
        old_discovered_game_id,
        old_latest_match_ms,
        last_crawled_match_ms,
        processed,
    ) = row
    best_source, best_priority, best_depth = _pick_best_metadata(
        str(old_source),
        int(old_priority),
        int(old_depth),
        source,
        priority,
        depth,
    )
    latest_match_ms = max(int(old_latest_match_ms), int(discovered_match_created_ms))
    best_game_id = old_discovered_game_id
    if discovered_match_created_ms >= int(old_latest_match_ms) and discovered_from_game_id:
        best_game_id = discovered_from_game_id

    con.execute(
        """
        UPDATE crawl_seen
        SET source = ?, priority = ?, min_depth = ?, discovered_from_game_id = ?,
            latest_seen_match_created_ms = ?
        WHERE puuid = ?
        """,
        (
            best_source,
            best_priority,
            best_depth,
            best_game_id,
            latest_match_ms,
            puuid,
        ),
    )
    con.commit()

    should_requeue = int(processed) == 1 and int(discovered_match_created_ms) > int(last_crawled_match_ms)
    became_pending = _upsert_queue_row(
        con,
        puuid,
        best_depth,
        best_source,
        best_priority,
        best_game_id,
        latest_match_ms,
        requeue=should_requeue,
        eligible_at_ms=_now_ms() + requeue_cooldown_ms if should_requeue else 0,
    )
    if should_requeue and became_pending:
        con.execute(
            "UPDATE crawl_seen SET processed = 0 WHERE puuid = ?",
            (puuid,),
        )
        con.commit()
        return "requeued"
    if int(processed) == 0:
        return "updated"
    return "noop"


def _requeue_stale_claims(con: sqlite3.Connection, claim_timeout_ms: int) -> int:
    cutoff_ms = _now_ms() - claim_timeout_ms
    before = con.total_changes
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (_utc_now(), cutoff_ms),
    )
    con.commit()
    return con.total_changes - before


def _claim_next_player(
    con: sqlite3.Connection,
    worker_id: str,
    claim_timeout_ms: int,
) -> tuple[str, int, str, int] | None:
    """Atomically claim one pending queue item for this worker."""
    now_text = _utc_now()
    now_ms = _now_ms()
    cutoff_ms = now_ms - claim_timeout_ms

    con.execute("BEGIN IMMEDIATE")
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE status = 'in_progress'
          AND claimed_at_ms > 0
          AND claimed_at_ms < ?
        """,
        (now_text, cutoff_ms),
    )
    row = con.execute(
        """
        SELECT queue_idx, puuid, depth, source, discovered_match_created_ms
        FROM crawl_queue
        WHERE status = 'pending'
          AND eligible_at_ms <= ?
        ORDER BY discovered_match_created_ms DESC,
                 priority ASC,
                 depth ASC,
                 updated_at ASC,
                 queue_idx ASC
        LIMIT 1
        """
    , (now_ms,)).fetchone()
    if row is None:
        con.commit()
        return None

    queue_idx, puuid, depth, source, claimed_match_ms = row
    before = con.total_changes
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'in_progress',
            claimed_by = ?,
            claimed_at_ms = ?,
            updated_at = ?
        WHERE queue_idx = ?
          AND status = 'pending'
        """,
        (worker_id, now_ms, now_text, queue_idx),
    )
    claimed = con.total_changes > before
    con.commit()
    if not claimed:
        return None
    return str(puuid), int(depth), str(source), int(claimed_match_ms)


def _pending_player_count(con: sqlite3.Connection) -> int:
    return int(
        con.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        ).fetchone()[0]
    )


def _open_queue_source_count(con: sqlite3.Connection, source: str) -> int:
    return int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM crawl_queue
            WHERE source = ?
              AND status IN ('pending', 'in_progress')
            """,
            (source,),
        ).fetchone()[0]
    )


def _next_pending_wait_ms(con: sqlite3.Connection) -> int | None:
    row = con.execute(
        """
        SELECT MIN(eligible_at_ms)
        FROM crawl_queue
        WHERE status = 'pending'
        """
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return max(0, int(row[0]) - _now_ms())


def _mark_player_done(
    con: sqlite3.Connection,
    puuid: str,
    new_games_found: int,
    claimed_match_created_ms: int,
    requeue_cooldown_ms: int,
) -> bool:
    """Finalize a claimed player.

    Returns True if the player was re-queued immediately due to a newer discovery
    arriving while this worker was processing it.
    """
    now = _utc_now()
    row = con.execute(
        """
        SELECT latest_seen_match_created_ms, last_crawled_match_created_ms
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()
    latest_seen_match_ms = int(row[0]) if row else 0
    last_crawled_match_ms = int(row[1]) if row else 0
    needs_requeue = latest_seen_match_ms > int(claimed_match_created_ms)

    if needs_requeue:
        eligible_at_ms = _now_ms() + max(0, requeue_cooldown_ms)
        con.execute(
            """
            UPDATE crawl_seen
            SET processed = 0,
                last_crawled_at = ?,
                process_count = process_count + 1,
                new_games_found = new_games_found + ?,
                last_crawled_match_created_ms = ?
            WHERE puuid = ?
            """,
            (now, new_games_found, max(last_crawled_match_ms, int(claimed_match_created_ms)), puuid),
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET status = 'pending',
                claimed_by = NULL,
                claimed_at_ms = 0,
                eligible_at_ms = ?,
                updated_at = ?
            WHERE puuid = ?
            """,
            (eligible_at_ms, now, puuid),
        )
        con.commit()
        return True

    con.execute(
        """
        UPDATE crawl_seen
        SET processed = 1,
            last_crawled_at = ?,
            process_count = process_count + 1,
            new_games_found = new_games_found + ?,
            last_crawled_match_created_ms = ?
        WHERE puuid = ?
        """,
        (now, new_games_found, max(last_crawled_match_ms, int(claimed_match_created_ms)), puuid),
    )
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'done',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?
        WHERE puuid = ?
        """,
        (now, puuid),
    )
    con.commit()
    return False


def _seed_ladder_neighbors(
    con: sqlite3.Connection,
    lcu: LCUClient,
    puuid: str,
    ladder_cap: int,
) -> int:
    added = 0
    for ladder in get_league_ladders(lcu, puuid):
        for division in ladder.get("divisions") or []:
            for standing in division.get("standings") or []:
                standing_puuid = standing.get("puuid")
                if not standing_puuid:
                    continue
                result = _enqueue_player(con, str(standing_puuid), depth=0, source="ladder")
                if result == "new":
                    added += 1
                if added >= ladder_cap:
                    return added
    return added


def _seed_apex_players(
    con: sqlite3.Connection,
    lcu: LCUClient,
    apex_queues: tuple[str, ...],
    apex_tiers: tuple[str, ...],
    apex_cap: int,
) -> int:
    added = 0
    for queue_type in apex_queues:
        for tier in apex_tiers:
            payload = get_apex_league(lcu, queue_type, tier)
            if not payload:
                continue
            for division in payload.get("divisions") or []:
                for standing in division.get("standings") or []:
                    standing_puuid = standing.get("puuid")
                    if not standing_puuid:
                        continue
                    result = _enqueue_player(con, str(standing_puuid), depth=0, source="apex")
                    if result == "new":
                        added += 1
                    if added >= apex_cap:
                        return added
    return added


def _iter_chunks(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    chunk_size = max(1, size)
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _normalize_riot_id_seed(raw: str) -> str | None:
    value = raw.strip()
    if not value or value.startswith("#"):
        return None

    candidate = value
    if "op.gg/" in candidate.lower():
        parsed = urlparse(candidate)
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            candidate = unquote(path_parts[-1]).strip()

    candidate = candidate.strip().strip("/")
    if not candidate:
        return None

    if "#" in candidate:
        game_name, tag_line = candidate.split("#", 1)
        game_name = game_name.strip()
        tag_line = tag_line.strip()
        if game_name and tag_line:
            return f"{game_name}#{tag_line}"
        return None

    if "-" in candidate:
        game_name, tag_line = candidate.rsplit("-", 1)
        game_name = game_name.strip()
        tag_line = tag_line.strip()
        if game_name and re.fullmatch(r"[A-Za-z0-9]{2,5}", tag_line):
            return f"{game_name}#{tag_line}"
    return None


def _load_riot_id_seeds(
    *,
    riot_ids: tuple[str, ...] = (),
    riot_id_files: tuple[Path, ...] = (),
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add_candidate(raw: str) -> None:
        normalized = _normalize_riot_id_seed(raw)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        ordered.append(normalized)

    for riot_id in riot_ids:
        add_candidate(str(riot_id))

    for path in riot_id_files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            add_candidate(line)

    return ordered


def _get_riot_bridge(con: sqlite3.Connection, public_puuid: str) -> tuple[str, str | None] | None:
    row = con.execute(
        """
        SELECT riot_id, lcu_puuid
        FROM riot_id_bridge
        WHERE public_puuid = ?
        """,
        (public_puuid,),
    ).fetchone()
    if not row:
        return None
    return (str(row[0]), str(row[1]) if row[1] else None)


def _upsert_riot_bridge(
    con: sqlite3.Connection,
    *,
    public_puuid: str,
    riot_id: str,
    lcu_puuid: str | None,
    resolve_status: str,
) -> None:
    con.execute(
        """
        INSERT INTO riot_id_bridge(public_puuid, riot_id, lcu_puuid, resolved_at, resolve_status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(public_puuid) DO UPDATE SET
            riot_id = excluded.riot_id,
            lcu_puuid = excluded.lcu_puuid,
            resolved_at = excluded.resolved_at,
            resolve_status = excluded.resolve_status
        """,
        (public_puuid, riot_id, lcu_puuid, _utc_now(), resolve_status),
    )
    con.commit()


def _seed_riot_tier_players(
    con: sqlite3.Connection,
    lcu: LCUClient,
    *,
    region: str,
    riot_queues: tuple[str, ...],
    riot_tiers: tuple[str, ...],
    riot_divisions: tuple[str, ...],
    riot_page_limit: int,
    riot_cap: int,
) -> int:
    from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired

    added = 0
    page_limit = max(1, riot_page_limit)
    tiers = tuple(str(t).upper() for t in riot_tiers)
    divisions = tuple(str(d).upper() for d in riot_divisions)
    apex_like = {"CHALLENGER", "GRANDMASTER", "MASTER"}

    def _enqueue_lcu_puuid(lcu_puuid: str) -> bool:
        nonlocal added
        result = _enqueue_player(
            con,
            lcu_puuid,
            depth=0,
            source="riot_tier",
            initial_delay_ms=_RIOT_TIER_HYDRATION_DELAY_MS,
        )
        if result == "new":
            added += 1
            return True
        return False

    try:
        with RiotClient(region=region) as client:
            pending_aliases: list[tuple[str, str]] = []
            for queue_type in riot_queues:
                for tier in tiers:
                    tier_divisions = ("I",) if tier in apex_like else divisions
                    for division in tier_divisions:
                        for page in range(1, page_limit + 1):
                            entries = client.league_entries(
                                tier=tier,
                                division=division,
                                queue=queue_type,
                                page=page,
                            )
                            if not entries:
                                break
                            for entry in entries:
                                public_puuid = str(entry.get("puuid") or "")
                                if not public_puuid:
                                    continue

                                cached = _get_riot_bridge(con, public_puuid)
                                if cached is not None:
                                    riot_id, lcu_puuid = cached
                                    if lcu_puuid:
                                        _enqueue_lcu_puuid(lcu_puuid)
                                    if added >= riot_cap:
                                        return added
                                    continue

                                account = client.account_by_puuid(public_puuid)
                                game_name = str(account.get("gameName") or "").strip()
                                tag_line = str(account.get("tagLine") or "").strip()
                                if not game_name or not tag_line:
                                    _upsert_riot_bridge(
                                        con,
                                        public_puuid=public_puuid,
                                        riot_id="",
                                        lcu_puuid=None,
                                        resolve_status="missing_riot_alias",
                                    )
                                    continue
                                riot_id = f"{game_name}#{tag_line}"
                                pending_aliases.append((public_puuid, riot_id))

                                if len(pending_aliases) >= _LCU_RIOT_ID_LOOKUP_BATCH:
                                    for chunk in _iter_chunks(pending_aliases, _LCU_RIOT_ID_LOOKUP_BATCH):
                                        resolved = lookup_summoners_by_riot_ids(
                                            lcu,
                                            [riot_id for _, riot_id in chunk],
                                        )
                                        by_alias = {
                                            f"{str(item.get('gameName') or '').strip()}#{str(item.get('tagLine') or '').strip()}": item
                                            for item in resolved
                                        }
                                        for pending_public_puuid, pending_riot_id in chunk:
                                            match = by_alias.get(pending_riot_id)
                                            lcu_puuid = str(match.get("puuid") or "").strip() if match else ""
                                            _upsert_riot_bridge(
                                                con,
                                                public_puuid=pending_public_puuid,
                                                riot_id=pending_riot_id,
                                                lcu_puuid=(lcu_puuid or None),
                                                resolve_status=("resolved" if lcu_puuid else "lcu_lookup_empty"),
                                            )
                                            if lcu_puuid:
                                                _enqueue_lcu_puuid(lcu_puuid)
                                            if added >= riot_cap:
                                                return added
                                    pending_aliases.clear()

                            if len(entries) < 200:
                                break

            if pending_aliases:
                for chunk in _iter_chunks(pending_aliases, _LCU_RIOT_ID_LOOKUP_BATCH):
                    resolved = lookup_summoners_by_riot_ids(
                        lcu,
                        [riot_id for _, riot_id in chunk],
                    )
                    by_alias = {
                        f"{str(item.get('gameName') or '').strip()}#{str(item.get('tagLine') or '').strip()}": item
                        for item in resolved
                    }
                    for pending_public_puuid, pending_riot_id in chunk:
                        match = by_alias.get(pending_riot_id)
                        lcu_puuid = str(match.get("puuid") or "").strip() if match else ""
                        _upsert_riot_bridge(
                            con,
                            public_puuid=pending_public_puuid,
                            riot_id=pending_riot_id,
                            lcu_puuid=(lcu_puuid or None),
                            resolve_status=("resolved" if lcu_puuid else "lcu_lookup_empty"),
                        )
                        if lcu_puuid:
                            _enqueue_lcu_puuid(lcu_puuid)
                        if added >= riot_cap:
                            return added
    except RiotKeyExpired as exc:
        raise RuntimeError(str(exc)) from exc
    except RuntimeError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrapper around external API
        raise RuntimeError(f"riot-tier seeding failed: {exc}") from exc

    return added


def _seed_manual_riot_ids(
    con: sqlite3.Connection,
    lcu: LCUClient,
    *,
    riot_ids: tuple[str, ...],
    target_queues: set[int],
    history_window: int,
    games_per_player: int | None,
    pending_cap: int = 0,
) -> int:
    added = 0
    normalized_ids = _load_riot_id_seeds(riot_ids=riot_ids)
    if not normalized_ids:
        return 0

    existing_open = _open_queue_source_count(con, "manual_riot_id")
    remaining_budget = max(0, pending_cap - existing_open) if pending_cap > 0 else None
    if remaining_budget == 0:
        print(
            f"[snowball] manual_riot_id seed skipped  "
            f"open_manual_queue={existing_open}  pending_cap={pending_cap}",
            flush=True,
        )
        return 0

    total_chunks = (len(normalized_ids) + _LCU_RIOT_ID_LOOKUP_BATCH - 1) // _LCU_RIOT_ID_LOOKUP_BATCH
    resolved_total = 0
    for chunk_idx, chunk in enumerate(
        _iter_chunks([("", riot_id) for riot_id in normalized_ids], _LCU_RIOT_ID_LOOKUP_BATCH),
        start=1,
    ):
        resolved = lookup_summoners_by_riot_ids(lcu, [riot_id for _, riot_id in chunk])
        by_alias = {
            f"{str(item.get('gameName') or '').strip()}#{str(item.get('tagLine') or '').strip()}": item
            for item in resolved
        }
        resolved_total += len(by_alias)
        for _, riot_id in chunk:
            match = by_alias.get(riot_id)
            lcu_puuid = str(match.get("puuid") or "").strip() if match else ""
            if not lcu_puuid:
                continue
            history = get_match_history(lcu, lcu_puuid, begin=0, end=history_window)
            game_ids = _extract_target_game_ids(history, target_queues)
            if games_per_player is not None and games_per_player > 0:
                game_ids = game_ids[:games_per_player]
            if not game_ids:
                continue
            latest_match_ms = _latest_target_match_created_ms(history, target_queues)
            result = _enqueue_player(
                con,
                lcu_puuid,
                depth=0,
                source="manual_riot_id",
                discovered_match_created_ms=latest_match_ms,
                initial_delay_ms=0,
            )
            if result == "new":
                added += 1
                if remaining_budget is not None:
                    remaining_budget -= 1
                    if remaining_budget <= 0:
                        print(
                            f"[snowball] manual_riot_id pending cap reached  "
                            f"added={added}  pending_cap={pending_cap}",
                            flush=True,
                        )
                        return added
        if chunk_idx == 1 or chunk_idx == total_chunks or chunk_idx % 5 == 0:
            print(
                f"[snowball] manual_riot_id seed progress  chunks={chunk_idx}/{total_chunks}  "
                f"resolved={resolved_total}  enqueued={added}",
                flush=True,
            )
    return added


def run_snowball(
    db_path: Path,
    target_games: int = 500,
    max_players: int = 250,
    history_window: int = 20,
    games_per_player: int | None = None,
    worker_id: str | None = None,
    claim_timeout_sec: int = 300,
    player_requeue_cooldown_sec: int = 45,
    target_queues: set[int] | None = None,
    include_self: bool = True,
    include_friends: bool = True,
    include_ladder: bool = False,
    ladder_cap: int = 100,
    include_apex: bool = False,
    apex_queues: tuple[str, ...] = ("RANKED_SOLO_5x5", "RANKED_FLEX_SR"),
    apex_tiers: tuple[str, ...] = ("CHALLENGER", "GRANDMASTER", "MASTER"),
    apex_cap: int = 300,
    include_riot_tier: bool = False,
    riot_region: str = "tw",
    riot_queues: tuple[str, ...] = ("RANKED_SOLO_5x5",),
    riot_tiers: tuple[str, ...] = ("GOLD",),
    riot_divisions: tuple[str, ...] = ("I", "II", "III", "IV"),
    riot_page_limit: int = 2,
    riot_cap: int = 400,
    seed_riot_ids: tuple[str, ...] = (),
    seed_riot_id_files: tuple[Path, ...] = (),
    manual_seed_pending_cap: int = 40,
    max_depth: int = 3,
) -> CrawlStats:
    """Expand the LCU-visible player graph and save unseen target-queue matches."""
    if target_queues is None:
        target_queues = DEFAULT_QUEUES

    creds = get_credentials()
    if creds is None:
        raise RuntimeError("League client not found")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect_db(db_path)
    _ensure_schema(con)
    migrated = _migrate_legacy_crawl_players(con)
    purged_riot_tier = _purge_invalid_riot_tier_rows(con)
    synced_priorities = _sync_source_priorities(con)
    claim_timeout_ms = max(1, claim_timeout_sec) * 1000
    player_requeue_cooldown_ms = max(0, player_requeue_cooldown_sec) * 1000
    worker_id = worker_id or f"pid-{os.getpid()}"

    existing_game_ids = _load_existing_game_ids(con)
    expanded_game_ids: set[str] = set()
    local_puuid_latest_ms: dict[str, int] = {}
    stats = CrawlStats()

    with LCUClient(creds) as lcu:
        me = _get_current_summoner_with_retry(lcu)
        if not me or not me.get("puuid"):
            raise RuntimeError("Could not resolve current summoner")

        my_puuid = str(me["puuid"])
        my_name = me.get("gameName") or me.get("displayName") or "?"

        if include_self:
            result = _enqueue_player(con, my_puuid, depth=0, source="self")
            if result == "new":
                stats.seeded_players += 1
            elif result == "requeued":
                stats.requeued_players += 1

        if include_friends:
            for friend in get_friends(lcu):
                friend_puuid = friend.get("puuid")
                if not friend_puuid:
                    continue
                result = _enqueue_player(con, str(friend_puuid), depth=0, source="friend")
                if result == "new":
                    stats.seeded_players += 1
                elif result == "requeued":
                    stats.requeued_players += 1

        if include_ladder:
            stats.seeded_players += _seed_ladder_neighbors(con, lcu, my_puuid, ladder_cap)

        if include_apex:
            stats.seeded_players += _seed_apex_players(
                con, lcu, apex_queues=apex_queues, apex_tiers=apex_tiers, apex_cap=apex_cap
            )

        if include_riot_tier:
            stats.seeded_players += _seed_riot_tier_players(
                con,
                lcu,
                region=riot_region,
                riot_queues=riot_queues,
                riot_tiers=riot_tiers,
                riot_divisions=riot_divisions,
                riot_page_limit=riot_page_limit,
                riot_cap=riot_cap,
            )

        manual_riot_ids = _load_riot_id_seeds(
            riot_ids=seed_riot_ids,
            riot_id_files=seed_riot_id_files,
        )
        if manual_riot_ids:
            print(
                f"[snowball] preparing manual_riot_id seeds  count={len(manual_riot_ids)}  worker={worker_id}",
                flush=True,
            )
            stats.seeded_players += _seed_manual_riot_ids(
                con,
                lcu,
                riot_ids=tuple(manual_riot_ids),
                target_queues=target_queues,
                history_window=history_window,
                games_per_player=games_per_player,
                pending_cap=max(0, manual_seed_pending_cap),
            )
            print(
                f"[snowball] finished manual_riot_id seeds  enqueued={stats.seeded_players}  worker={worker_id}",
                flush=True,
            )

        pending = _pending_player_count(con)
        print(
            f"[snowball] connected as {my_name}  pending={pending}  "
            f"newly_seeded={stats.seeded_players}  requeued={stats.requeued_players}  "
            f"existing_games={len(existing_game_ids)}  queues={sorted(target_queues)}  worker={worker_id}"
        )
        if migrated:
            print(f"[snowball] migrated legacy crawl_players -> seen+priority-queue  rows={migrated}")
        if purged_riot_tier:
            print(f"[snowball] purged invalid riot_tier public-puuid rows={purged_riot_tier}")
        if synced_priorities:
            print(f"[snowball] synced source priorities  rows={synced_priorities}")
        reclaimed = _requeue_stale_claims(con, claim_timeout_ms)
        if reclaimed:
            print(f"[snowball] reclaimed stale claims={reclaimed}")

        waiting_logged = False
        empty_queue_wait_started_at: float | None = None
        while stats.saved_games < target_games and stats.processed_players < max_players:
            next_player = _claim_next_player(con, worker_id=worker_id, claim_timeout_ms=claim_timeout_ms)
            if next_player is None:
                wait_ms = _next_pending_wait_ms(con)
                if wait_ms is None:
                    now_monotonic = time.monotonic()
                    if empty_queue_wait_started_at is None:
                        empty_queue_wait_started_at = now_monotonic
                        print(
                            f"[snowball] queue empty, waiting briefly for new seeds  "
                            f"grace={_EMPTY_QUEUE_GRACE_SEC:.0f}s  worker={worker_id}"
                        )
                    elif now_monotonic - empty_queue_wait_started_at >= _EMPTY_QUEUE_GRACE_SEC:
                        break
                    time.sleep(1.0)
                    continue
                empty_queue_wait_started_at = None
                sleep_sec = min(max(wait_ms / 1000.0, 0.25), 5.0)
                if not waiting_logged:
                    print(
                        f"[snowball] waiting for eligible queue items  "
                        f"pending={_pending_player_count(con)}  sleep={sleep_sec:.2f}s  "
                        f"worker={worker_id}"
                    )
                    waiting_logged = True
                time.sleep(sleep_sec)
                continue

            puuid, depth, source, claimed_match_created_ms = next_player
            stats.processed_players += 1
            waiting_logged = False
            empty_queue_wait_started_at = None

            history = get_match_history(lcu, puuid, begin=0, end=history_window)
            game_ids = _extract_target_game_ids(history, target_queues)
            if games_per_player is not None and games_per_player > 0:
                game_ids = game_ids[:games_per_player]
            print(
                f"[snowball] player {stats.processed_players}/{max_players}  "
                f"depth={depth}  source={source:<6}  puuid={puuid[:12]}  "
                f"target_games={len(game_ids)}  pending={max(0, _pending_player_count(con) - 1)}  "
                f"worker={worker_id}"
            )

            new_games_for_player = 0
            for game_id in game_ids:
                if stats.saved_games >= target_games:
                    break
                if game_id in expanded_game_ids:
                    continue
                if not _claim_game_id(con, game_id, worker_id=worker_id, claim_timeout_ms=claim_timeout_ms):
                    continue

                detail = get_game_detail(lcu, game_id)
                if not detail:
                    _release_game_claim(con, game_id)
                    stats.failed_games += 1
                    continue

                expanded_game_ids.add(game_id)
                stats.expanded_games += 1

                record = _parse_game_detail(detail, target_queues)
                if record is None:
                    _mark_game_done(con, game_id)
                    stats.filtered_games += 1
                    continue

                if record["game_id"] in existing_game_ids:
                    _backfill_participants_json(con, record)
                    _mark_game_done(con, record["game_id"])
                    stats.existing_games += 1
                else:
                    record["captured_at"] = _utc_now()
                    if _insert_game(con, record):
                        existing_game_ids.add(record["game_id"])
                        _mark_game_done(con, record["game_id"])
                        stats.saved_games += 1
                        new_games_for_player += 1
                        label = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                        print(
                            f"  [saved] {label:<6}  game_id={record['game_id']}  "
                            f"patch={record['patch']}  total_saved={stats.saved_games}  "
                            f"worker={worker_id}"
                        )
                    else:
                        _release_game_claim(con, record["game_id"])
                        stats.failed_games += 1
                        continue

                if depth >= max_depth:
                    continue

                for participant_puuid in _extract_participant_puuids(detail):
                    cached_match_ms = local_puuid_latest_ms.get(participant_puuid)
                    if cached_match_ms is not None and cached_match_ms >= int(record["created_ms"]):
                        continue
                    local_puuid_latest_ms[participant_puuid] = int(record["created_ms"])
                    result = _enqueue_player(
                        con,
                        participant_puuid,
                        depth + 1,
                        source="match",
                        discovered_from_game_id=record["game_id"],
                        discovered_match_created_ms=int(record["created_ms"]),
                        requeue_cooldown_ms=player_requeue_cooldown_ms,
                    )
                    if result == "new":
                        stats.seeded_players += 1
                    elif result == "requeued":
                        stats.requeued_players += 1

            requeued_on_finish = _mark_player_done(
                con,
                puuid,
                new_games_found=new_games_for_player,
                claimed_match_created_ms=claimed_match_created_ms,
                requeue_cooldown_ms=player_requeue_cooldown_ms,
            )
            if requeued_on_finish:
                stats.requeued_players += 1

    pending_after = _pending_player_count(con)
    con.close()
    print(
        f"[snowball] done  processed_players={stats.processed_players}  "
        f"expanded_games={stats.expanded_games}  saved_games={stats.saved_games}  "
        f"existing_games={stats.existing_games}  filtered={stats.filtered_games}  "
        f"failed={stats.failed_games}  requeued={stats.requeued_players}  "
        f"pending={pending_after}  worker={worker_id}"
    )
    return stats
