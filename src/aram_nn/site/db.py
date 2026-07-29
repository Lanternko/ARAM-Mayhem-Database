from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PUBLIC_GAME_COLUMNS = (
    "game_id",
    "queue_id",
    "patch",
    "blue_champs",
    "red_champs",
    "blue_wins",
    "duration_sec",
    "created_ms",
    "captured_at",
    "participants_json",
)

CREATE_PUBLIC_GAMES_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id           TEXT PRIMARY KEY,
    queue_id          INTEGER NOT NULL,
    patch             TEXT NOT NULL,
    blue_champs       TEXT NOT NULL,
    red_champs        TEXT NOT NULL,
    blue_wins         INTEGER NOT NULL,
    duration_sec      INTEGER NOT NULL,
    created_ms        INTEGER NOT NULL,
    captured_at       TEXT NOT NULL,
    participants_json TEXT,
    received_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_PUBLIC_GAMES_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_games_queue_patch_created
ON games(queue_id, patch, created_ms);
"""

INSERT_PUBLIC_GAME_SQL = """
INSERT OR IGNORE INTO games (
    game_id, queue_id, patch, blue_champs, red_champs,
    blue_wins, duration_sec, created_ms, captured_at, participants_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

PUUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BulkInsertResult:
    received: int
    inserted: int
    skipped: int


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ensure_public_schema(con: sqlite3.Connection) -> None:
    con.execute(CREATE_PUBLIC_GAMES_SQL)
    con.execute(CREATE_PUBLIC_GAMES_INDEX_SQL)
    con.commit()


def _patch_major_minor(patch: str) -> str | None:
    parts = patch.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return patch or None


# Games a patch needs before the SITE will switch to it.  Still gated by
# teammate-synergy coverage rather than win-rate accuracy, but both of those are
# now shrunk toward the previous patch, which moved the floor twice:
#   50,000 -> 30,000  once champion WR stopped needing a mature sample
#   30,000 -> 10,000  once pair lift did too.  With the display floor at 10 games
#                     (see --min-synergy-games), replaying 16.14 at a 10,000-game
#                     patch covers 169 of 173 champions at 2.12pp RMSE -- better
#                     than the 4.92pp the old raw build shipped at 120,000 games.
# Both the tier-list builder's "--patch-prefix auto" and the publisher's
# "--auto-patch-min-games" must use this same floor, or `auto` silently means two
# different patches (it did: the builder's old 1,000 default would flip the site to
# a day-old patch while the publisher was still holding the mature one).
SITE_PATCH_MIN_GAMES = 10_000


def latest_patch_prefix(
    db_path: Path,
    *,
    queue_id: int = 2400,
    min_games: int = 1000,
    fallback_latest: bool = True,
) -> str | None:
    """Return the most recent major.minor prefix that has at least *min_games*.

    Recency is determined by the newest game's ``created_ms`` within each
    prefix group.  When *fallback_latest* is true, falls back to the absolute
    most-recent prefix if none meet *min_games*; callers that publish models can
    disable the fallback so a thin new patch cannot replace a mature one.
    """
    if not db_path.exists():
        return None
    con = connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT patch, COUNT(*) AS n, MAX(created_ms) AS latest
            FROM games WHERE queue_id = ?
            GROUP BY patch ORDER BY latest DESC
            """,
            (queue_id,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    prefix_stats: dict[str, tuple[int, int]] = {}
    for patch_full, count, latest_ms in rows:
        prefix = _patch_major_minor(str(patch_full))
        if not prefix:
            continue
        prev_n, prev_latest = prefix_stats.get(prefix, (0, 0))
        prefix_stats[prefix] = (prev_n + count, max(prev_latest, latest_ms or 0))
    if not prefix_stats:
        return None
    qualified = [(latest, pfx) for pfx, (n, latest) in prefix_stats.items() if n >= min_games]
    if not qualified and fallback_latest:
        qualified = [(latest, pfx) for pfx, (_, latest) in prefix_stats.items()]
    if not qualified:
        return None
    qualified.sort(reverse=True)
    return qualified[0][1]


def count_games(db_path: Path, *, queue_id: int | None = None, patch_prefix: str | None = None) -> int:
    if not db_path.exists():
        return 0
    con = connect(db_path)
    try:
        ensure_public_schema(con)
        clauses: list[str] = []
        params: list[Any] = []
        if queue_id is not None:
            clauses.append("queue_id = ?")
            params.append(int(queue_id))
        if patch_prefix:
            clauses.append("patch LIKE ?")
            params.append(f"{patch_prefix}%")
        sql = "SELECT COUNT(*) FROM games"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def _compact_json_array(value: Any, *, field: str) -> str:
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _assert_no_private_identifiers(participants_json: str | None) -> None:
    if not participants_json:
        return
    if PUUID_RE.search(participants_json):
        raise ValueError("participants_json contains a PUUID-like identifier")
    lowered = participants_json.lower()
    if "puuid" in lowered or "summonername" in lowered or "riotid" in lowered:
        raise ValueError("participants_json contains private identifier fields")


def normalize_public_game(row: dict[str, Any]) -> tuple:
    game_id = str(row.get("game_id") or "").strip()
    if not game_id:
        raise ValueError("game_id is required")

    blue_champs = _compact_json_array(row.get("blue_champs"), field="blue_champs")
    red_champs = _compact_json_array(row.get("red_champs"), field="red_champs")
    participants_raw = row.get("participants_json")
    participants_json = (
        _compact_json_array(participants_raw, field="participants_json")
        if participants_raw not in (None, "")
        else None
    )
    _assert_no_private_identifiers(participants_json)

    return (
        game_id,
        int(row["queue_id"]),
        str(row.get("patch") or ""),
        blue_champs,
        red_champs,
        1 if bool(row.get("blue_wins")) else 0,
        int(row.get("duration_sec") or 0),
        int(row.get("created_ms") or 0),
        str(row.get("captured_at") or ""),
        participants_json,
    )


def insert_public_games(db_path: Path, games: Iterable[dict[str, Any]]) -> BulkInsertResult:
    normalized = [normalize_public_game(game) for game in games]
    con = connect(db_path)
    try:
        ensure_public_schema(con)
        before = con.total_changes
        con.executemany(INSERT_PUBLIC_GAME_SQL, normalized)
        con.commit()
        inserted = con.total_changes - before
    finally:
        con.close()
    return BulkInsertResult(
        received=len(normalized),
        inserted=int(inserted),
        skipped=len(normalized) - int(inserted),
    )


def iter_public_games(
    db_path: Path,
    *,
    queue_id: int | None = None,
    patch_prefix: str | None = None,
    chunk_size: int = 1000,
) -> Iterator[dict[str, Any]]:
    if not db_path.exists():
        return
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if queue_id is not None:
            clauses.append("queue_id = ?")
            params.append(int(queue_id))
        if patch_prefix:
            clauses.append("patch LIKE ?")
            params.append(f"{patch_prefix}%")
        sql = f"SELECT {', '.join(PUBLIC_GAME_COLUMNS)} FROM games"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_ms, game_id"
        cursor = con.execute(sql, params)
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            for row in rows:
                yield {column: row[column] for column in PUBLIC_GAME_COLUMNS}
    finally:
        con.close()


def public_game_batch(
    db_path: Path,
    *,
    queue_id: int | None = None,
    patch_prefix: str | None = None,
    chunk_size: int = 1000,
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in iter_public_games(
        db_path,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        chunk_size=chunk_size,
    ):
        batch.append(row)
        if len(batch) >= chunk_size:
            yield batch
            batch = []
    if batch:
        yield batch
