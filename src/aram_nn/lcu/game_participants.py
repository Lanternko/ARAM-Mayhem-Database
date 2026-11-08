"""Player -> game index (``game_participants``) shared by writer, crawler and backfill.

``WITHOUT ROWID`` with PK ``(puuid, created_ms, game_id)`` makes one player's
game timeline a single range scan; no secondary index is needed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import sqlite3
from typing import Any

CREATE_GAME_PARTICIPANTS_SQL = """
CREATE TABLE IF NOT EXISTS game_participants (
    puuid      TEXT    NOT NULL,
    created_ms INTEGER NOT NULL,
    game_id    TEXT    NOT NULL,
    queue_id   INTEGER NOT NULL,
    PRIMARY KEY (puuid, created_ms, game_id)
) WITHOUT ROWID;
"""

INSERT_GAME_PARTICIPANT_SQL = (
    "INSERT OR IGNORE INTO game_participants(puuid, created_ms, game_id, queue_id) VALUES (?, ?, ?, ?)"
)


def _payload_rows(payload: Any) -> list[Any]:
    if isinstance(payload, (str, bytes)):
        if not payload:
            return []
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    return payload if isinstance(payload, list) else []


def extract_puuids(*payloads: Any) -> list[str]:
    """Deduplicated puuids (first-seen order) from participants JSON text or lists.

    Looks at ``puuid`` and ``player.puuid`` of every row; public participants
    usually lack it while ``participants_private_json`` carries it, so callers
    pass both payloads.  Malformed JSON or rows are ignored.
    """
    found: dict[str, None] = {}
    for payload in payloads:
        for row in _payload_rows(payload):
            if not isinstance(row, Mapping):
                continue
            candidate = row.get("puuid")
            if not candidate and isinstance(row.get("player"), Mapping):
                candidate = row["player"].get("puuid")
            if isinstance(candidate, str) and candidate:
                found.setdefault(candidate, None)
    return list(found)


def insert_game_participants(
    con: sqlite3.Connection,
    puuids: Iterable[str],
    created_ms: int,
    game_id: str,
    queue_id: int,
) -> int:
    """Idempotently index ``puuids`` for one game; returns rows actually added."""
    before = con.total_changes
    con.executemany(
        INSERT_GAME_PARTICIPANT_SQL,
        [(puuid, int(created_ms), str(game_id), int(queue_id)) for puuid in puuids],
    )
    return con.total_changes - before


def participant_timeline(con: sqlite3.Connection, puuid: str, since_ms: int = 0) -> list[tuple[int, str, int]]:
    """``(created_ms, game_id, queue_id)`` for one player, oldest first."""
    rows = con.execute(
        "SELECT created_ms, game_id, queue_id FROM game_participants "
        "WHERE puuid = ? AND created_ms >= ? ORDER BY created_ms, game_id",
        (puuid, int(since_ms)),
    ).fetchall()
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]
