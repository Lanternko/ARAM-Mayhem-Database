"""Backfill the ``game_participants`` index (puuid -> created_ms, game_id) in games.db.

Safe to run beside the live single writer: reads use a short-lived read-only
query per batch (no long WAL snapshot), writes commit at most ``--batch-games``
games (~10 rows each) per transaction with a short busy timeout, sleep between
batches and back off when the DB is locked.  Progress is checkpointed after
every commit, so the script can be interrupted and re-run; re-running also
covers games committed before the writer started indexing.  Use ``pythonw`` for
windowless background runs (no subprocess is used here).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from aram_nn.lcu.game_participants import (  # noqa: E402
    CREATE_GAME_PARTICIPANTS_SQL,
    extract_puuids,
    insert_game_participants,
)

DEFAULT_DB = REPO_ROOT / "data" / "lcu" / "games.db"
DEFAULT_CHECKPOINT = REPO_ROOT / "data" / "lcu" / "game_participants_backfill.json"
WRITE_BUSY_TIMEOUT_MS = 5000
MAX_LOCK_RETRIES = 200
MAX_BACKOFF_SEC = 15.0

_SELECT_BATCH_SQL = (
    "SELECT rowid, game_id, queue_id, created_ms, participants_json, participants_private_json "
    "FROM games WHERE rowid > ? ORDER BY rowid LIMIT ?"
)


def _log(message: str) -> None:
    if sys.stdout is not None:  # pythonw has no stdout
        print(message, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def load_checkpoint(path: Path, db: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or str(data.get("db")) != str(db):
        _log(f"checkpoint {path} is for a different db; starting from rowid 0")
        return {}
    return data


def save_checkpoint(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _parse_row(row: tuple) -> tuple[int, str, int, int, list[str]]:
    rowid, game_id, queue_id, created_ms, public_json, private_json = row
    return int(rowid), str(game_id), int(queue_id or 0), int(created_ms or 0), extract_puuids(public_json, private_json)


def _write_batch(con: sqlite3.Connection, parsed: list[tuple[int, str, int, int, list[str]]]) -> int:
    """One transaction for the whole batch; rolls back and retries when locked."""
    for attempt in range(MAX_LOCK_RETRIES):
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                added = 0
                for _rowid, game_id, queue_id, created_ms, puuids in parsed:
                    added += insert_game_participants(con, puuids, created_ms, game_id, queue_id)
                con.commit()
                return added
            except BaseException:
                con.rollback()
                raise
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            delay = min(0.25 * 2 ** min(attempt, 6), MAX_BACKOFF_SEC)
            _log(f"database locked (attempt {attempt + 1}); backing off {delay:.2f}s")
            time.sleep(delay)
    raise RuntimeError("giving up after repeated lock timeouts; checkpoint is intact, re-run to resume")


def backfill(
    db: Path,
    checkpoint: Path,
    batch_games: int = 500,
    sleep_sec: float = 0.05,
    limit_games: int | None = None,
    dry_run: bool = False,
    progress_every: int = 20,
) -> dict:
    state = {} if dry_run else load_checkpoint(checkpoint, db)
    last_rowid = int(state.get("last_rowid", 0))
    rows_inserted = int(state.get("rows_inserted", 0))
    games_scanned_total = int(state.get("games_scanned", 0))
    stats = {"games": 0, "puuid_rows": 0, "rows_inserted": 0, "games_without_puuid": 0, "batches": 0}

    reader = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    writer = None
    if not dry_run:
        writer = sqlite3.connect(str(db), timeout=WRITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        writer.execute(f"PRAGMA busy_timeout={WRITE_BUSY_TIMEOUT_MS}")
        writer.execute(CREATE_GAME_PARTICIPANTS_SQL)
    started = time.monotonic()
    start_rowid = last_rowid
    try:
        while True:
            take = batch_games
            if limit_games is not None:
                take = min(take, limit_games - stats["games"])
                if take <= 0:
                    break
            # Fresh short read per batch: no long-lived WAL snapshot, bounded memory.
            cursor = reader.execute(_SELECT_BATCH_SQL, (last_rowid, take))
            parsed = []
            while True:
                chunk = cursor.fetchmany(100)
                if not chunk:
                    break
                parsed.extend(_parse_row(row) for row in chunk)
            cursor.close()
            if not parsed:
                break
            added = 0 if dry_run else _write_batch(writer, parsed)
            last_rowid = parsed[-1][0]
            stats["games"] += len(parsed)
            stats["puuid_rows"] += sum(len(item[4]) for item in parsed)
            stats["games_without_puuid"] += sum(1 for item in parsed if not item[4])
            stats["rows_inserted"] += added
            stats["batches"] += 1
            rows_inserted += added
            games_scanned_total += len(parsed)
            if not dry_run:
                save_checkpoint(
                    checkpoint,
                    {
                        "db": str(db),
                        "last_rowid": last_rowid,
                        "rows_inserted": rows_inserted,
                        "games_scanned": games_scanned_total,
                        "updated_at": _now(),
                    },
                )
            if stats["batches"] % max(1, progress_every) == 0:
                elapsed = max(time.monotonic() - started, 1e-9)
                max_rowid = reader.execute("SELECT MAX(rowid) FROM games").fetchone()[0] or last_rowid
                rate = (last_rowid - start_rowid) / elapsed  # rowids/s, ~games/s
                remaining = max(max_rowid - last_rowid, 0)
                eta = f"{remaining / rate / 60:.1f}min" if rate > 0 else "?"
                _log(
                    f"rowid={last_rowid} games/s={stats['games'] / elapsed:.0f} "
                    f"rows_inserted={rows_inserted} puuid_rows_seen={stats['puuid_rows']} "
                    f"remaining_rowids={remaining} eta={eta}"
                )
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    finally:
        reader.close()
        if writer is not None:
            writer.close()
    stats["last_rowid"] = last_rowid
    stats["elapsed_sec"] = round(time.monotonic() - started, 3)
    stats["dry_run"] = dry_run
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--batch-games", type=int, default=500, help="games per write transaction")
    parser.add_argument("--sleep", type=float, default=0.05, help="seconds to sleep between batches")
    parser.add_argument("--limit-games", type=int, default=None, help="stop after scanning N games (testing)")
    parser.add_argument("--dry-run", action="store_true", help="parse and count only; no writes, no checkpoint")
    parser.add_argument("--progress-every", type=int, default=20, help="log a progress line every N batches")
    args = parser.parse_args(argv)
    if args.batch_games < 1:
        parser.error("--batch-games must be >= 1")
    if not args.db.exists():
        parser.error(f"db not found: {args.db}")
    stats = backfill(
        db=args.db.resolve(),
        checkpoint=args.checkpoint,
        batch_games=args.batch_games,
        sleep_sec=args.sleep,
        limit_games=args.limit_games,
        dry_run=args.dry_run,
        progress_every=args.progress_every,
    )
    _log(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
