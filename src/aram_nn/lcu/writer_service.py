"""Single-actor SQLite writer for LCU crawl state.

The live collector has many readers (LCU requests) but only one process should
mutate ``games`` and the crawl frontier.  :class:`WriterService` is the small
actor core used by that process.  It intentionally has no process supervision
or transport dependency: callers feed validated protocol frames and receive a
response only after the transaction has committed.

The implementation also works on a fresh temporary SQLite database, which is
useful for protocol/atomicity tests and for rebuilding an interrupted local
database.  Existing snowball tables are respected; only additive columns and
writer-owned claim/idempotency tables are created here.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import itertools
import json
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any

from .db_state import CAPTURE_WATERMARK_PREFIX
from .writer_protocol import ProtocolError, decode_frame, encode_frame, validate_message


_DEFAULT_LEASE_MS = 60_000
_MAX_LEASE_MS = 24 * 3600_000
_DEFAULT_REQUEUE_DELAY_MS = 45_000
_MAX_REQUEUE_DELAY_MS = 30 * 24 * 3600_000
_CLAIM_ACTIVE = "in_progress"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items()}


def _int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _snowball():
    from . import snowball
    return snowball


def _classic_found(incoming: Any) -> int:
    if not isinstance(incoming, Mapping):
        return 0
    value = incoming.get(4310, incoming.get("4310", 0))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _has_postgame_stats(payload: str | None) -> bool:
    """Return whether a participant JSON blob contains meaningful game stats."""
    if not payload:
        return False
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(value, list) or not value:
        return False
    for row in value:
        if not isinstance(row, Mapping):
            continue
        stats = row.get("stats")
        if isinstance(stats, Mapping) and stats:
            return True
        # The private participant representation can carry these fields flat.
        if any(key in row for key in ("kills", "deaths", "assists", "win", "totalDamageDealtToChampions")):
            return True
    return False


def _payload_richer(current: str | None, incoming: str | None) -> bool:
    if not incoming or incoming == "[]":
        return False
    if not current or current == "[]":
        return True
    current_stats = _has_postgame_stats(current)
    incoming_stats = _has_postgame_stats(incoming)
    if incoming_stats and not current_stats:
        return True
    if incoming_stats == current_stats:
        try:
            return len(json.loads(incoming)) > len(json.loads(current))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    return False


def _monotonic_timestamp(old: str | None, new: str | None) -> str | None:
    if not new:
        return old
    if not old:
        return str(new)
    try:
        old_dt = datetime.fromisoformat(str(old).replace("Z", "+00:00"))
        new_dt = datetime.fromisoformat(str(new).replace("Z", "+00:00"))
        if old_dt.tzinfo is None:
            old_dt = old_dt.replace(tzinfo=timezone.utc)
        if new_dt.tzinfo is None:
            new_dt = new_dt.replace(tzinfo=timezone.utc)
        return str(new) if new_dt >= old_dt else str(old)
    except (TypeError, ValueError, OverflowError):
        # ISO-8601 strings sort chronologically in the normal case.  Keep the
        # existing value on malformed input instead of moving a watermark back.
        return str(new) if str(new) >= str(old) else str(old)


class WriterService:
    """A synchronous single-writer actor.

    ``handle`` is deliberately serialized with a lock even when called from
    several transport threads.  Every mutating request has one SQLite
    transaction and its idempotency response is stored in that same
    transaction, so a response is never acknowledged before the write is
    durable.
    """

    def __init__(
        self,
        db: str | Path | sqlite3.Connection,
        *,
        claim_lease_ms: int = _DEFAULT_LEASE_MS,
        clock_ms: Callable[[], int] | None = None,
        failure_injector: Callable[[str], None] | None = None,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        lease = int(claim_lease_ms)
        if lease <= 0:
            raise ValueError("claim_lease_ms must be positive")
        self._default_lease_ms = min(lease, _MAX_LEASE_MS)
        self._clock_ms = clock_ms or _now_ms
        self._failure_injector = failure_injector or failure_hook
        self._lock = threading.RLock()
        self._closed = False
        self._shutdown = False
        self._active_channel = "default"
        self._owns_connection = not isinstance(db, sqlite3.Connection)
        self.con = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(
            str(db), timeout=30.0, check_same_thread=False
        )
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=30000")
        self._claim_count = itertools.count()
        self.ensure_schema()

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        """Create the known snowball shape and writer-owned tables.

        This is intentionally explicit SQL.  No protocol field can select a
        table, column or SQL expression.
        """
        with self._lock:
            self.con.executescript(
                """
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    queue_id INTEGER NOT NULL,
                    patch TEXT NOT NULL,
                    blue_champs TEXT NOT NULL,
                    red_champs TEXT NOT NULL,
                    blue_wins INTEGER NOT NULL,
                    duration_sec INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    participants_json TEXT,
                    participants_private_json TEXT,
                    seed_family TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS crawl_runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS riot_id_bridge (
                    public_puuid TEXT PRIMARY KEY,
                    riot_id TEXT NOT NULL,
                    lcu_puuid TEXT,
                    resolved_at TEXT NOT NULL,
                    resolve_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crawl_seen (
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
                    classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0,
                    classic_rate_num REAL NOT NULL DEFAULT 0,
                    classic_rate_den REAL NOT NULL DEFAULT 0,
                    classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS crawl_queue (
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
                    classic_affinity_rank INTEGER NOT NULL DEFAULT 0,
                    classic_lambda REAL NOT NULL DEFAULT 0,
                    classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0,
                    classic_span_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS crawl_visit_events (
                    visit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    puuid TEXT NOT NULL,
                    revisit_arm TEXT NOT NULL DEFAULT 'control',
                    is_revisit INTEGER NOT NULL DEFAULT 0,
                    visited_at TEXT NOT NULL,
                    previous_crawled_at TEXT,
                    revisit_interval_ms INTEGER,
                    process_number INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'match',
                    seed_family TEXT NOT NULL DEFAULT 'match',
                    worker_id TEXT,
                    current_patch TEXT,
                    history_game_count INTEGER NOT NULL DEFAULT 0,
                    target_game_count INTEGER NOT NULL DEFAULT 0,
                    new_games_found INTEGER NOT NULL DEFAULT 0,
                    new_games_by_queue_json TEXT,
                    claim_lane TEXT NOT NULL DEFAULT 'general',
                    classic_affinity TEXT NOT NULL DEFAULT 'none',
                    classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0,
                    lane_arm TEXT NOT NULL DEFAULT '',
                    claim_score REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS writer_player_claims (
                    puuid TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress'
                );
                CREATE TABLE IF NOT EXISTS writer_game_claims (
                    game_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress'
                );
                CREATE TABLE IF NOT EXISTS writer_requests (
                    request_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    channel_id TEXT NOT NULL DEFAULT '',
                    request_hash TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS writer_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_writer_player_claims_expiry
                    ON writer_player_claims(status, expires_at_ms);
                CREATE INDEX IF NOT EXISTS idx_writer_game_claims_expiry
                    ON writer_game_claims(status, expires_at_ms);
                CREATE INDEX IF NOT EXISTS idx_writer_queue_claim
                    ON crawl_queue(status, eligible_at_ms, priority, depth, queue_idx);
                """
            )
            # Databases created by older pollers predate these additive columns.
            self._ensure_column("games", "participants_json", "participants_json TEXT")
            self._ensure_column("games", "participants_private_json", "participants_private_json TEXT")
            self._ensure_column("games", "seed_family", "seed_family TEXT NOT NULL DEFAULT ''")
            self._ensure_column("crawl_seen", "seed_family", "seed_family TEXT NOT NULL DEFAULT 'match'")
            self._ensure_column("crawl_queue", "seed_family", "seed_family TEXT NOT NULL DEFAULT 'match'")
            self._ensure_column("writer_requests", "channel_id", "channel_id TEXT NOT NULL DEFAULT ''")
            self._ensure_column("writer_requests", "request_hash", "request_hash TEXT NOT NULL DEFAULT ''")
            for table, column, definition in (
                ("crawl_seen", "discovered_queue_id", "discovered_queue_id INTEGER NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_affinity", "classic_affinity TEXT NOT NULL DEFAULT 'none'"),
                ("crawl_seen", "classic_affinity_rank", "classic_affinity_rank INTEGER NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_games_24h", "classic_games_24h INTEGER NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_games_recent", "classic_games_recent INTEGER NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_last_seen_ms", "classic_last_seen_ms INTEGER NOT NULL DEFAULT 0"),
                (
                    "crawl_seen",
                    "classic_revisit_interval_ms",
                    "classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0",
                ),
                ("crawl_seen", "classic_rate_num", "classic_rate_num REAL NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_rate_den", "classic_rate_den REAL NOT NULL DEFAULT 0"),
                ("crawl_seen", "classic_last_crawl_ms", "classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0"),
                ("crawl_queue", "discovered_queue_id", "discovered_queue_id INTEGER NOT NULL DEFAULT 0"),
                ("crawl_queue", "classic_affinity_rank", "classic_affinity_rank INTEGER NOT NULL DEFAULT 0"),
                ("crawl_queue", "classic_lambda", "classic_lambda REAL NOT NULL DEFAULT 0"),
                ("crawl_queue", "classic_last_crawl_ms", "classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0"),
                ("crawl_queue", "classic_span_ms", "classic_span_ms INTEGER NOT NULL DEFAULT 0"),
                ("crawl_visit_events", "claim_lane", "claim_lane TEXT NOT NULL DEFAULT 'general'"),
                ("crawl_visit_events", "classic_affinity", "classic_affinity TEXT NOT NULL DEFAULT 'none'"),
                (
                    "crawl_visit_events",
                    "classic_revisit_interval_ms",
                    "classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0",
                ),
                ("crawl_visit_events", "lane_arm", "lane_arm TEXT NOT NULL DEFAULT ''"),
                ("crawl_visit_events", "claim_score", "claim_score REAL NOT NULL DEFAULT 0"),
            ):
                self._ensure_column(table, column, definition)
            sb = _snowball()
            sb._register_sql_functions(self.con)
            self.con.execute(sb._CREATE_CLASSIC_CLAIM_INDEX_SQL)
            # Same one-shot flags as snowball._ensure_schema.  Without these, a
            # DB that first meets the new columns here keeps classic_lambda=0
            # and the score arm silently uses the discovery prior for everyone.
            sb._backfill_classic_affinity(self.con)
            sb._bootstrap_classic_rate(self.con)
            self.con.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in self.con.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    # --------------------------------------------------------------- public API
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_connection:
                self.con.close()

    def __enter__(self) -> "WriterService":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def handle(self, frame_or_message: bytes | bytearray | memoryview | Mapping[str, Any], *, channel_id: str = "default") -> dict[str, Any]:
        """Handle one request and return its response after commit."""
        if not isinstance(channel_id, str) or not channel_id or len(channel_id.encode("utf-8")) > 1024:
            raise ProtocolError("INVALID_CHANNEL")
        message = decode_frame(frame_or_message) if isinstance(frame_or_message, (bytes, bytearray, memoryview)) else validate_message(frame_or_message)
        with self._lock:
            self._active_channel = channel_id
            if self._closed:
                return self._error(message, "CLOSED")
            replay = self._replay(message)
            if replay is not None:
                return replay
            command = str(message["command"])
            if command == "ping":
                return self._finish(message, {"ok": True, "status": "PONG"})
            if command == "ready":
                return self._finish(message, {"ok": True, "status": "READY"})
            if command == "shutdown":
                self._shutdown = True
                return self._finish(message, {"ok": True, "status": "SHUTDOWN"})
            if self._shutdown:
                return self._finish(message, {"ok": False, "status": "SHUTDOWN"})
            try:
                if command == "player_claim":
                    return self._player_claim(message, channel_id)
                if command == "game_claim":
                    return self._game_claim(message, channel_id)
                if command == "commit_game":
                    return self._commit_game(message, channel_id)
                if command == "release_game":
                    return self._release_game(message, channel_id)
                if command == "mark_game_done":
                    return self._mark_game_done(message, channel_id)
                if command == "finalize_player":
                    return self._finalize_player(message, channel_id)
                if command == "requeue_player":
                    return self._requeue_player(message, channel_id)
                if command == "snowball_init":
                    return self._snowball_init(message, channel_id)
                if command == "snowball_runtime":
                    return self._snowball_runtime(message, channel_id)
                if command == "snowball_queue":
                    return self._snowball_queue(message, channel_id)
                if command == "snowball_bridge":
                    return self._snowball_bridge(message, channel_id)
                if command == "snowball_player":
                    return self._snowball_player(message, channel_id)
            except Exception:
                self.con.rollback()
                raise
            return self._error(message, "UNKNOWN_COMMAND")

    # Common adapter names.  ``handle_bytes`` is useful for a socket loop; its
    # response is intentionally generic JSON and does not pretend to be a
    # request frame.
    handle_frame = handle
    process = handle

    def handle_bytes(self, frame: bytes, *, channel_id: str = "default") -> bytes:
        return json.dumps(self.handle(frame, channel_id=channel_id), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def submit(self, frame_or_message: bytes | Mapping[str, Any], channel_id: str = "default") -> dict[str, Any]:
        return self.handle(frame_or_message, channel_id=channel_id)

    def enqueue_player(
        self,
        puuid: str,
        *,
        source: str = "match",
        depth: int = 0,
        priority: int = 10,
        discovered_from_game_id: str | None = None,
        discovered_match_created_ms: int = 0,
        seed_family: str = "match",
    ) -> bool:
        """Insert a pending player idempotently (test/bootstrap primitive)."""
        if not isinstance(puuid, str) or not puuid:
            raise ValueError("puuid must be a non-empty string")
        now = _utc_now()
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_player_locked(
                    puuid,
                    source=source,
                    depth=depth,
                    priority=priority,
                    discovered_from_game_id=discovered_from_game_id,
                    discovered_match_created_ms=discovered_match_created_ms,
                    seed_family=seed_family,
                    now=now,
                )
                changed = self.con.execute("SELECT changes()").fetchone()[0] > 0
                self.con.commit()
                return bool(changed)
            except Exception:
                self.con.rollback()
                raise

    # -------------------------------------------------------------- idempotency
    def _replay(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT command, channel_id, request_hash, response_json FROM writer_requests WHERE request_id = ?",
            (str(message["request_id"]),),
        ).fetchone()
        if row is None:
            return None
        request_hash = hashlib.sha256(encode_frame(message)).hexdigest()
        if str(row[0]) != str(message["command"]) or str(row[1]) != self._active_channel or str(row[2]) != request_hash:
            raise ProtocolError("REQUEST_ID_CONFLICT")
        try:
            value = json.loads(str(row[3]))
            return value if isinstance(value, dict) else self._error(message, "INVALID_REPLAY")
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._error(message, "INVALID_REPLAY")

    def _finish(self, message: Mapping[str, Any], body: Mapping[str, Any], *, commit: bool = True) -> dict[str, Any]:
        response = {"version": 1, "request_id": str(message["request_id"]), **dict(body)}
        if commit:
            request_hash = hashlib.sha256(encode_frame(message)).hexdigest()
            self.con.execute(
                "INSERT INTO writer_requests(request_id, command, channel_id, request_hash, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(message["request_id"]), str(message["command"]), self._active_channel, request_hash, _json(response), _utc_now()),
            )
            self.con.commit()
        return response

    def _error(self, message: Mapping[str, Any], status: str) -> dict[str, Any]:
        # Keep error responses free of request payloads and sensitive tokens.
        return {"version": 1, "request_id": str(message.get("request_id", "")), "ok": False, "status": status, "mutated": False}

    def _checkpoint(self, name: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(str(name))

    def _begin(self) -> None:
        self.con.execute("BEGIN IMMEDIATE")

    # ------------------------------------------------------------- claim helpers
    def _claim_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _lease(self, message: Mapping[str, Any]) -> int:
        if message.get("lease_ms") is not None:
            value = _int(message.get("lease_ms"), self._default_lease_ms)
        elif message.get("claim_timeout_ms") is not None:
            value = _int(message.get("claim_timeout_ms"), self._default_lease_ms)
        else:
            value = self._default_lease_ms
        return min(max(value, 1), _MAX_LEASE_MS)

    def _claim_ok(self, table: str, item: str, channel_id: str, token: str, generation: int, now: int) -> bool:
        row = self.con.execute(
            f"SELECT channel_id, token, generation, expires_at_ms, status FROM {table} WHERE {('puuid' if table == 'writer_player_claims' else 'game_id')} = ?",
            (item,),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row[0]) == channel_id
            and secrets.compare_digest(str(row[1]), token)
            and int(row[2]) == int(generation)
            and str(row[4]) == _CLAIM_ACTIVE
            and int(row[3]) > int(now)
        )

    def _stale(self, message: Mapping[str, Any]) -> dict[str, Any]:
        return self._finish(message, {"ok": False, "status": "STALE_CLAIM", "mutated": False})

    def _player_claim(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        now = _int(message.get("now_ms"), self._clock_ms())
        puuid = str(message["puuid"]) if message.get("puuid") else None
        self._begin()
        try:
            if puuid is None:
                row = self.con.execute(
                    """SELECT puuid, depth, source, discovered_match_created_ms, seed_family
                       FROM crawl_queue
                       WHERE status = 'pending' AND eligible_at_ms <= ?
                       ORDER BY priority ASC, depth ASC, updated_at ASC, queue_idx ASC
                       LIMIT 1""",
                    (now,),
                ).fetchone()
                if row is None:
                    self.con.rollback()
                    return self._finish(message, {"ok": True, "status": "EMPTY", "mutated": False})
                puuid = str(row[0])
            else:
                row = self.con.execute(
                    "SELECT depth, source, discovered_match_created_ms, seed_family, status, eligible_at_ms FROM crawl_queue WHERE puuid = ?",
                    (puuid,),
                ).fetchone()
                if row is None:
                    # A direct claim is useful for a seed bootstrap and is
                    # equivalent to enqueueing a pending match node.
                    self._upsert_player_locked(puuid, source="match", depth=0, priority=10, discovered_from_game_id=None, discovered_match_created_ms=0, seed_family="match", now=_utc_now())
                    row = self.con.execute(
                        "SELECT depth, source, discovered_match_created_ms, seed_family, status, eligible_at_ms FROM crawl_queue WHERE puuid = ?",
                        (puuid,),
                    ).fetchone()
                if row is None or str(row[4]) != "pending" or int(row[5]) > now:
                    self.con.rollback()
                    return self._finish(message, {"ok": True, "status": "BUSY", "mutated": False})

            existing = self.con.execute("SELECT generation, status, expires_at_ms FROM writer_player_claims WHERE puuid = ?", (puuid,)).fetchone()
            if existing is not None and str(existing[1]) == _CLAIM_ACTIVE and int(existing[2]) > now:
                self.con.rollback()
                return self._finish(message, {"ok": True, "status": "BUSY", "mutated": False, "puuid": puuid})
            generation = (int(existing[0]) if existing is not None else 0) + 1
            token = self._claim_token()
            expires = now + self._lease(message)
            self.con.execute(
                """INSERT INTO writer_player_claims(puuid, channel_id, token, generation, claimed_at_ms, expires_at_ms, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'in_progress')
                   ON CONFLICT(puuid) DO UPDATE SET channel_id=excluded.channel_id, token=excluded.token,
                     generation=excluded.generation, claimed_at_ms=excluded.claimed_at_ms,
                     expires_at_ms=excluded.expires_at_ms, status='in_progress'""",
                (puuid, channel_id, token, generation, now, expires),
            )
            self.con.execute(
                "UPDATE crawl_queue SET status='in_progress', claimed_by=?, claimed_at_ms=?, updated_at=? WHERE puuid=?",
                (channel_id, now, _utc_now(), puuid),
            )
            self._checkpoint("player_claim")
            response = {"ok": True, "status": "CLAIMED", "mutated": True, "puuid": puuid, "token": token, "generation": generation, "expires_at_ms": expires}
            return self._finish(message, response)
        except Exception:
            self.con.rollback()
            raise

    def _game_claim(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        game_id = str(message["game_id"])
        now = _int(message.get("now_ms"), self._clock_ms())
        self._begin()
        try:
            if self.con.execute("SELECT 1 FROM games WHERE game_id=?", (game_id,)).fetchone() is not None:
                self.con.rollback()
                return self._finish(message, {"ok": True, "status": "DONE", "mutated": False, "game_id": game_id})
            existing = self.con.execute("SELECT generation, status, expires_at_ms FROM writer_game_claims WHERE game_id=?", (game_id,)).fetchone()
            if existing is not None and str(existing[1]) == _CLAIM_ACTIVE and int(existing[2]) > now:
                self.con.rollback()
                return self._finish(message, {"ok": True, "status": "BUSY", "mutated": False, "game_id": game_id})
            generation = (int(existing[0]) if existing is not None else 0) + 1
            token = self._claim_token()
            expires = now + self._lease(message)
            self.con.execute(
                """INSERT INTO writer_game_claims(game_id, channel_id, token, generation, claimed_at_ms, expires_at_ms, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'in_progress')
                   ON CONFLICT(game_id) DO UPDATE SET channel_id=excluded.channel_id, token=excluded.token,
                     generation=excluded.generation, claimed_at_ms=excluded.claimed_at_ms,
                     expires_at_ms=excluded.expires_at_ms, status='in_progress'""",
                (game_id, channel_id, token, generation, now, expires),
            )
            self._checkpoint("game_claim")
            response = {"ok": True, "status": "CLAIMED", "mutated": True, "game_id": game_id, "token": token, "generation": generation, "expires_at_ms": expires}
            return self._finish(message, response)
        except Exception:
            self.con.rollback()
            raise

    # --------------------------------------------------------------- game write
    def _normalise_record(self, message: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(_json_object(message.get("record")))
        for key in ("game_id", "queue_id", "patch", "blue_champs", "red_champs", "blue_wins", "duration_sec", "created_ms", "captured_at", "participants", "participants_private", "seed_family"):
            if key in message:
                record[key] = message[key]
        required = ("game_id", "queue_id", "patch", "blue_champs", "red_champs", "blue_wins", "duration_sec", "created_ms", "captured_at")
        if any(key not in record for key in required):
            raise ProtocolError("MISSING_FIELD")
        if not isinstance(record["game_id"], str) or not record["game_id"]:
            raise ProtocolError("INVALID_TYPE")
        for key in ("queue_id", "blue_wins", "duration_sec", "created_ms"):
            if not isinstance(record[key], int) or isinstance(record[key], bool):
                raise ProtocolError("INVALID_TYPE")
        for key in ("patch", "captured_at"):
            if not isinstance(record[key], str):
                raise ProtocolError("INVALID_TYPE")
        for key in ("blue_champs", "red_champs"):
            if not isinstance(record[key], (list, tuple)):
                raise ProtocolError("INVALID_TYPE")
        participants = record.get("participants", message.get("participants", []))
        private = record.get("participants_private", message.get("participants_private", []))
        record["participants"] = participants if isinstance(participants, list) else []
        record["participants_private"] = private if isinstance(private, list) else []
        record["seed_family"] = str(record.get("seed_family") or "match")
        return record

    def _participant_puuids(self, message: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
        values = message.get("participant_puuids")
        if not isinstance(values, list):
            values = []
        for row in record.get("participants", []):
            if isinstance(row, Mapping):
                candidate = row.get("puuid")
                if not candidate and isinstance(row.get("player"), Mapping):
                    candidate = row["player"].get("puuid")
                if candidate:
                    values.append(candidate)
        found: list[str] = []
        for value in values:
            if isinstance(value, str) and value and value not in found:
                found.append(value)
        return found[:10_000]

    def _commit_game(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        game_id = str(message["game_id"])
        now = _int(message.get("now_ms"), self._clock_ms())
        record = self._normalise_record(message)
        token = str(message["token"])
        generation = int(message["generation"])
        self._begin()
        try:
            if not self._claim_ok("writer_game_claims", game_id, channel_id, token, generation, now):
                self.con.rollback()
                return self._stale(message)
            participants = record["participants"]
            private = record["participants_private"]
            public_json = _json(participants)
            private_json = _json(private)
            existing = self.con.execute("SELECT participants_json, participants_private_json FROM games WHERE game_id=?", (game_id,)).fetchone()
            inserted = existing is None
            if inserted:
                self.con.execute(
                    """INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, blue_wins, duration_sec, created_ms, captured_at, participants_json, participants_private_json, seed_family)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, int(record["queue_id"]), str(record["patch"]), _json(record["blue_champs"]), _json(record["red_champs"]), int(record["blue_wins"]), int(record["duration_sec"]), int(record["created_ms"]), str(record["captured_at"]), public_json, private_json, str(record["seed_family"])),
                )
            else:
                assignments: list[str] = []
                params: list[Any] = []
                if _payload_richer(existing[0], public_json):
                    assignments.append("participants_json=?")
                    params.append(public_json)
                if _payload_richer(existing[1], private_json):
                    assignments.append("participants_private_json=?")
                    params.append(private_json)
                if assignments:
                    params.append(game_id)
                    self.con.execute(f"UPDATE games SET {', '.join(assignments)} WHERE game_id=?", params)
            self._checkpoint("game_insert")
            watermark_key = f"{CAPTURE_WATERMARK_PREFIX}{int(record['queue_id'])}"
            old_watermark = self.con.execute("SELECT state_value FROM crawl_runtime_state WHERE state_key=?", (watermark_key,)).fetchone()
            watermark = _monotonic_timestamp(str(old_watermark[0]) if old_watermark else None, str(record["captured_at"]))
            self.con.execute(
                """INSERT INTO crawl_runtime_state(state_key, state_value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value, updated_at=excluded.updated_at""",
                (watermark_key, str(watermark or record["captured_at"]), _utc_now()),
            )
            puuids = self._participant_puuids(message, record)
            for puuid in puuids:
                self._upsert_player_locked(puuid, source="match", depth=0, priority=10, discovered_from_game_id=game_id, discovered_match_created_ms=int(record["created_ms"]), seed_family=str(record["seed_family"]), now=_utc_now())
            self._checkpoint("participant_enqueue")
            self.con.execute("UPDATE writer_game_claims SET status='done', channel_id='', token='', expires_at_ms=0 WHERE game_id=?", (game_id,))
            self._checkpoint("game_claim_done")
            return self._finish(message, {"ok": True, "status": "COMMITTED" if inserted else "DUPLICATE", "mutated": bool(inserted or existing is not None), "inserted": inserted, "game_id": game_id, "participant_count": len(puuids), "watermark": watermark})
        except Exception:
            self.con.rollback()
            raise

    def _release_game(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        game_id = str(message["game_id"])
        now = _int(message.get("now_ms"), self._clock_ms())
        self._begin()
        try:
            if not self._claim_ok("writer_game_claims", game_id, channel_id, str(message["token"]), int(message["generation"]), now):
                self.con.rollback()
                return self._stale(message)
            self.con.execute("UPDATE writer_game_claims SET status='released', channel_id='', token='', expires_at_ms=0 WHERE game_id=?", (game_id,))
            self._checkpoint("game_release")
            return self._finish(message, {"ok": True, "status": "RELEASED", "mutated": True, "game_id": game_id})
        except Exception:
            self.con.rollback()
            raise

    def _mark_game_done(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        game_id = str(message["game_id"])
        now = _int(message.get("now_ms"), self._clock_ms())
        self._begin()
        try:
            if not self._claim_ok("writer_game_claims", game_id, channel_id, str(message["token"]), int(message["generation"]), now):
                self.con.rollback()
                return self._stale(message)
            self.con.execute(
                """UPDATE writer_game_claims SET status='done', channel_id='', token='', expires_at_ms=0
                   WHERE game_id=?""",
                (game_id,),
            )
            self._checkpoint("game_claim_done")
            return self._finish(message, {"ok": True, "status": "DONE", "mutated": True, "game_id": game_id})
        except Exception:
            self.con.rollback()
            raise

    # ------------------------------------------------------------- player write
    def _finalize_player(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        puuid = str(message["puuid"])
        now_ms = _int(message.get("now_ms"), self._clock_ms())
        self._begin()
        try:
            if not self._claim_ok("writer_player_claims", puuid, channel_id, str(message["token"]), int(message["generation"]), now_ms):
                self.con.rollback()
                return self._stale(message)
            sb = _snowball()
            now_text = _utc_now()
            row = self.con.execute(
                """SELECT latest_seen_match_created_ms, last_crawled_match_created_ms,
                          process_count, new_games_found, first_seen_at,
                          new_games_by_queue_json, last_crawled_at, source, seed_family,
                          classic_rate_num, classic_rate_den, classic_last_crawl_ms,
                          discovered_queue_id
                   FROM crawl_seen WHERE puuid=?""",
                (puuid,),
            ).fetchone()
            if row is None:
                self._upsert_player_locked(
                    puuid, source="match", depth=0, priority=10,
                    discovered_from_game_id=None, discovered_match_created_ms=0,
                    seed_family="match", now=now_text,
                )
                row = (0, 0, 0, 0, now_text, None, None, "match", "match", 0, 0, 0, 0)
            latest = _int(row[0])
            old_crawled = _int(row[1])
            process_count = _int(row[2])
            old_games = _int(row[3])
            first_seen_at = row[4]
            previous_crawled_at = str(row[6]) if row[6] else None
            claimed_ms = _int(message.get("claimed_match_created_ms"))
            observed_ms = _int(message.get("observed_match_created_ms"))
            crawled_ms = max(old_crawled, claimed_ms, observed_ms)
            found = max(0, _int(message.get("new_games_found")))
            merged = self._merge_queue_counts(row[5], message.get("new_games_by_queue"))
            classic_rank = max(0, _int(message.get("classic_rank")))
            classic_interval = max(0, _int(message.get("classic_revisit_interval_ms")))
            classic_min_ms = max(0, _int(message.get("classic_revisit_min_ms"), sb._CLASSIC_DEFAULT_REVISIT_MIN_MS))
            classic_discovered = int(row[12] or 0) == sb._CLASSIC_QUEUE_ID
            prior_rate_num = float(row[9] or 0.0)
            prior_rate_den = float(row[10] or 0.0)
            prior_last_crawl_ms = int(row[11] or 0)
            rate_num, rate_den = sb._decay_classic_rate(
                prior_rate_num,
                prior_rate_den,
                now_ms - prior_last_crawl_ms if prior_last_crawl_ms else 0,
                _classic_found(message.get("new_games_by_queue")),
            )
            lambda_after = sb._classic_lambda(
                rate_num, rate_den, classic_discovered=classic_discovered
            )
            # Producer floor, mirroring snowball._mark_player_done.  The producer
            # classifies from one 20-row window and sends rank 0 for a quiet one;
            # a player who has produced Classic before must not be ejected from
            # the lane on that alone, because nothing promotes them back except
            # chance re-draw from the general frontier.  This has to live here as
            # well as in the direct-SQLite path: RPC mode is what production runs,
            # so a floor only in _mark_player_done would never execute.
            classic_label = str(message.get("classic_affinity") or "none")
            if classic_rank == 0 and (
                sb._lifetime_classic_games(merged) > 0 or rate_num > 0.0
            ):
                classic_rank = 1
                classic_label = "dormant"
                classic_interval = max(classic_interval, sb._CLASSIC_DEFAULT_REVISIT_MIN_MS)
            revisit_interval_ms: int | None = None
            if previous_crawled_at:
                try:
                    previous_dt = datetime.fromisoformat(previous_crawled_at)
                    revisit_interval_ms = max(
                        0,
                        int((datetime.fromisoformat(now_text) - previous_dt).total_seconds() * 1000),
                    )
                except ValueError:
                    revisit_interval_ms = None
            self.con.execute(
                """INSERT INTO crawl_visit_events(
                       puuid, revisit_arm, is_revisit, visited_at, previous_crawled_at,
                       revisit_interval_ms, process_number, source, seed_family,
                       worker_id, current_patch, history_game_count, target_game_count,
                       new_games_found, new_games_by_queue_json, claim_lane,
                       classic_affinity, classic_revisit_interval_ms, lane_arm, claim_score
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    puuid,
                    sb.revisit_arm(puuid),
                    int(process_count > 0 and previous_crawled_at is not None),
                    now_text,
                    previous_crawled_at,
                    revisit_interval_ms,
                    process_count + 1,
                    str(message.get("source") or row[7] or "match"),
                    str(message.get("seed_family") or row[8] or "match"),
                    message.get("worker_id"),
                    message.get("current_patch"),
                    max(0, _int(message.get("history_game_count"))),
                    max(0, _int(message.get("target_game_count"))),
                    found,
                    _json(_json_object(message.get("new_games_by_queue"))),
                    str(message.get("claim_lane") or "general"),
                    classic_label,
                    classic_interval,
                    sb.lane_arm(puuid),
                    sb._classic_lambda(
                        prior_rate_num, prior_rate_den, classic_discovered=classic_discovered
                    ),
                ),
            )
            self._checkpoint("player_visit_event")
            needs_requeue = latest > crawled_ms or classic_rank > 0
            base_delay = max(0, _int(message.get("requeue_cooldown_ms"), _DEFAULT_REQUEUE_DELAY_MS))
            base_delay = min(base_delay, _MAX_REQUEUE_DELAY_MS)
            if not needs_requeue:
                eligible = 0
            elif classic_rank > 0:
                eligible = sb._classic_revisit_eligible_at_ms(
                    now_text,
                    max(classic_interval, classic_min_ms),
                    now_ms=now_ms,
                    min_revisit_ms=classic_min_ms,
                )
            elif sb.revisit_arm(puuid) == "treatment":
                eligible = now_ms + sb._treatment_cooldown_ms(
                    old_games + found, first_seen_at, time.time()
                )
            else:
                eligible = now_ms + sb._requeue_cooldown_for(
                    old_games + found, process_count + 1, base_delay
                )
            self.con.execute(
                """UPDATE crawl_seen SET processed=?, last_crawled_at=?,
                          process_count=process_count+1, new_games_found=new_games_found+?,
                          new_games_by_queue_json=?, last_crawled_match_created_ms=?,
                          classic_affinity=?, classic_affinity_rank=?, classic_games_24h=?,
                          classic_games_recent=?, classic_last_seen_ms=?,
                          classic_revisit_interval_ms=?, classic_rate_num=?,
                          classic_rate_den=?, classic_last_crawl_ms=? WHERE puuid=?""",
                (
                    0 if needs_requeue else 1, now_text, found, merged, crawled_ms,
                    classic_label, classic_rank,
                    max(0, _int(message.get("classic_games_24h"))),
                    max(0, _int(message.get("classic_games_recent"))),
                    max(0, _int(message.get("classic_last_seen_ms"))),
                    classic_interval, rate_num, rate_den, now_ms, puuid,
                ),
            )
            self.con.execute(
                """UPDATE crawl_queue SET status=?, claimed_by=NULL, claimed_at_ms=0,
                          eligible_at_ms=?, updated_at=?, classic_affinity_rank=?,
                          classic_lambda=?, classic_last_crawl_ms=?, classic_span_ms=?
                   WHERE puuid=?""",
                (
                    "pending" if needs_requeue else "done",
                    eligible,
                    now_text,
                    classic_rank,
                    lambda_after,
                    now_ms,
                    sb._classic_span_ms(classic_interval),
                    puuid,
                ),
            )
            self.con.execute("UPDATE writer_player_claims SET status='done', channel_id='', token='', expires_at_ms=0 WHERE puuid=?", (puuid,))
            self._checkpoint("player_claim_done")
            return self._finish(message, {"ok": True, "status": "REQUEUED" if needs_requeue else "FINALIZED", "mutated": True, "puuid": puuid, "new_games_found": found})
        except Exception:
            self.con.rollback()
            raise

    def _requeue_player(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        puuid = str(message["puuid"])
        now_ms = _int(message.get("now_ms"), self._clock_ms())
        delay = min(max(0, _int(message.get("delay_ms"), _DEFAULT_REQUEUE_DELAY_MS)), _MAX_REQUEUE_DELAY_MS)
        self._begin()
        try:
            if not self._claim_ok("writer_player_claims", puuid, channel_id, str(message["token"]), int(message["generation"]), now_ms):
                self.con.rollback()
                return self._stale(message)
            now_text = _utc_now()
            self.con.execute("UPDATE crawl_queue SET status='pending', claimed_by=NULL, claimed_at_ms=0, eligible_at_ms=?, updated_at=? WHERE puuid=?", (now_ms + delay, now_text, puuid))
            self.con.execute("UPDATE crawl_seen SET processed=0, last_crawled_at=? WHERE puuid=?", (now_text, puuid))
            self.con.execute("UPDATE writer_player_claims SET status='released', channel_id='', token='', expires_at_ms=0 WHERE puuid=?", (puuid,))
            self._checkpoint("player_requeue")
            return self._finish(message, {"ok": True, "status": "REQUEUED", "mutated": True, "puuid": puuid, "eligible_at_ms": now_ms + delay})
        except Exception:
            self.con.rollback()
            raise

    # ----------------------------------------------------------- snowball API
    # These handlers are deliberately higher-level than the generic writer
    # primitives above.  Producers can ask for the bounded queue/runtime
    # operations needed by snowball, but cannot select a table, column, or SQL
    # expression over the wire.
    def _snowball_init(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        claim_timeout_ms = max(1, _int(message.get("claim_timeout_ms"), 300_000))
        now = self._clock_ms()
        now_text = _utc_now()
        migrated = 0
        purged = 0
        synced = 0
        reclaimed = 0
        self._begin()
        try:
            # Legacy crawl_players migration is intentionally kept inside the
            # writer.  This path is only used when the old frontier exists and
            # the new tables are empty.
            has_legacy = self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawl_players'"
            ).fetchone()
            if has_legacy:
                seen_count = int(self.con.execute("SELECT COUNT(*) FROM crawl_seen").fetchone()[0])
                queue_count = int(self.con.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0])
                if seen_count == 0 and queue_count == 0:
                    rows = self.con.execute(
                        """SELECT puuid, source, priority, depth, discovered_from_game_id,
                                  status, first_seen_at, last_crawled_at,
                                  process_count, new_games_found
                           FROM crawl_players
                           ORDER BY priority ASC, depth ASC, first_seen_at ASC"""
                    ).fetchall()
                    for row in rows:
                        puuid, source, priority, depth, discovered_game, status, first_seen, last_crawled, process_count, new_games = row
                        game_row = self.con.execute(
                            "SELECT created_ms FROM games WHERE game_id=?",
                            (str(discovered_game),),
                        ).fetchone() if discovered_game else None
                        discovered_ms = int(game_row[0]) if game_row else 0
                        done = 1 if str(status) == "done" else 0
                        queue_status = "done" if done else "pending"
                        self.con.execute(
                            """INSERT OR IGNORE INTO crawl_seen(
                                puuid, source, priority, min_depth, discovered_from_game_id,
                                first_seen_at, last_crawled_at, process_count,
                                new_games_found, latest_seen_match_created_ms,
                                last_crawled_match_created_ms, processed, seed_family
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (str(puuid), str(source), int(priority), int(depth), discovered_game,
                             str(first_seen), last_crawled, int(process_count or 0),
                             int(new_games or 0), discovered_ms, discovered_ms if done else 0,
                             done, str(source) if str(source) != "match" else "legacy_match"),
                        )
                        self.con.execute(
                            """INSERT OR IGNORE INTO crawl_queue(
                                puuid, depth, source, priority, discovered_from_game_id,
                                discovered_match_created_ms, enqueued_at, updated_at,
                                claimed_by, claimed_at_ms, eligible_at_ms, status,
                                seed_family, discovered_queue_id, classic_affinity_rank
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, 0, 0)""",
                            (str(puuid), int(depth), str(source), int(priority), discovered_game,
                             discovered_ms, str(first_seen), str(first_seen), queue_status,
                             str(source) if str(source) != "match" else "legacy_match"),
                        )
                    migrated = len(rows)

            cutoff = int(now) - claim_timeout_ms
            before = self.con.total_changes
            self.con.execute(
                """UPDATE crawl_queue
                   SET status='pending', claimed_by=NULL, claimed_at_ms=0,
                       updated_at=?
                   WHERE status='in_progress' AND claimed_at_ms > 0
                     AND claimed_at_ms < ?""",
                (now_text, cutoff),
            )
            reclaimed = self.con.total_changes - before

            # A small, indexed maintenance pass keeps source priorities in
            # sync.  This is bounded to the known source allow-list.
            source_priorities = {
                "self": 0, "match": 10, "suggested": 15, "friend": 20,
                "apex": 30, "ladder": 40, "manual_riot_id": 60, "riot_tier": 70,
            }
            for source, priority in source_priorities.items():
                for table in ("crawl_seen", "crawl_queue"):
                    stale = self.con.execute(
                        f"SELECT 1 FROM {table} WHERE source=? AND priority != ? LIMIT 1",
                        (source, priority),
                    ).fetchone()
                    if stale is None:
                        continue
                    before = self.con.total_changes
                    self.con.execute(
                        f"UPDATE {table} SET priority=? WHERE source=? AND priority != ?",
                        (priority, source, priority),
                    )
                    synced += self.con.total_changes - before

            # Invalid public Riot PUUIDs are never useful to the producer and
            # are safe to purge as one bounded source-specific operation.
            row = self.con.execute(
                "SELECT COUNT(*) FROM crawl_seen WHERE source='riot_tier' AND length(puuid) != 36"
            ).fetchone()
            purged = int(row[0]) if row else 0
            if purged:
                self.con.execute("DELETE FROM crawl_queue WHERE source='riot_tier' AND length(puuid) != 36")
                self.con.execute("DELETE FROM crawl_seen WHERE source='riot_tier' AND length(puuid) != 36")
            self._checkpoint("snowball_init")
            return self._finish(message, {
                "ok": True, "status": "READY", "mutated": bool(migrated or purged or synced or reclaimed),
                "migrated": migrated, "purged": purged, "synced": synced, "reclaimed": reclaimed,
            })
        except Exception:
            self.con.rollback()
            raise

    def _snowball_runtime(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        operation = str(message.get("operation") or "")
        key = str(message.get("key") or "")
        if operation not in {"get", "set", "delete", "family_increment", "family_read"}:
            return self._error(message, "INVALID_OPERATION")
        if operation in {"family_increment", "family_read"}:
            family = str(message.get("seed_family") or "")
            if not family:
                return self._error(message, "MISSING_FIELD")
            key = f"family_yield:{family}"
        if not key:
            return self._error(message, "MISSING_FIELD")
        self._begin()
        try:
            if operation in {"get", "family_read"}:
                row = self.con.execute(
                    "SELECT state_value FROM crawl_runtime_state WHERE state_key=?", (key,)
                ).fetchone()
                value = str(row[0]) if row and row[0] is not None else None
                self.con.rollback()
                return self._finish(message, {"ok": True, "status": "FOUND" if value is not None else "MISSING", "mutated": False, "key": key, "value": value})
            if operation == "delete":
                before = self.con.total_changes
                self.con.execute("DELETE FROM crawl_runtime_state WHERE state_key=?", (key,))
                changed = self.con.total_changes > before
                return self._finish(message, {"ok": True, "status": "DELETED", "mutated": changed, "key": key})
            if operation == "family_increment":
                delta = max(0, _int(message.get("delta")))
                if delta <= 0:
                    self.con.rollback()
                    return self._finish(message, {"ok": True, "status": "NOOP", "mutated": False, "key": key, "value": 0})
                old = self.con.execute("SELECT state_value FROM crawl_runtime_state WHERE state_key=?", (key,)).fetchone()
                try:
                    previous = int(old[0]) if old else 0
                except (TypeError, ValueError):
                    previous = 0
                value = previous + delta
                self.con.execute(
                    """INSERT INTO crawl_runtime_state(state_key,state_value,updated_at)
                       VALUES (?, ?, ?) ON CONFLICT(state_key) DO UPDATE SET
                       state_value=excluded.state_value, updated_at=excluded.updated_at""",
                    (key, str(value), _utc_now()),
                )
                return self._finish(message, {"ok": True, "status": "UPDATED", "mutated": True, "key": key, "value": value})
            value = str(message.get("value") if message.get("value") is not None else "")
            self.con.execute(
                """INSERT INTO crawl_runtime_state(state_key,state_value,updated_at)
                   VALUES (?, ?, ?) ON CONFLICT(state_key) DO UPDATE SET
                   state_value=excluded.state_value, updated_at=excluded.updated_at""",
                (key, value, _utc_now()),
            )
            return self._finish(message, {"ok": True, "status": "UPDATED", "mutated": True, "key": key, "value": value})
        except Exception:
            self.con.rollback()
            raise

    def _snowball_bridge(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        operation = str(message.get("operation") or "")
        public_puuid = str(message.get("public_puuid") or "")
        if not public_puuid:
            return self._error(message, "MISSING_FIELD")
        if operation == "get":
            row = self.con.execute(
                "SELECT riot_id, lcu_puuid FROM riot_id_bridge WHERE public_puuid=?",
                (public_puuid,),
            ).fetchone()
            self.con.rollback()
            if row is None:
                return self._finish(message, {"ok": True, "status": "MISSING", "mutated": False})
            return self._finish(message, {"ok": True, "status": "FOUND", "mutated": False, "riot_id": str(row[0]), "lcu_puuid": str(row[1]) if row[1] else None})
        if operation != "upsert":
            return self._error(message, "INVALID_OPERATION")
        riot_id = str(message.get("riot_id") or "")
        status = str(message.get("resolve_status") or "")
        if not riot_id or not status:
            return self._error(message, "MISSING_FIELD")
        self._begin()
        try:
            self.con.execute(
                """INSERT INTO riot_id_bridge(public_puuid,riot_id,lcu_puuid,resolved_at,resolve_status)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(public_puuid) DO UPDATE SET
                   riot_id=excluded.riot_id, lcu_puuid=excluded.lcu_puuid,
                   resolved_at=excluded.resolved_at, resolve_status=excluded.resolve_status""",
                (public_puuid, riot_id, message.get("lcu_puuid") or None, _utc_now(), status),
            )
            return self._finish(message, {"ok": True, "status": "UPDATED", "mutated": True})
        except Exception:
            self.con.rollback()
            raise

    def _snowball_enqueue_locked(self, message: Mapping[str, Any]) -> str:
        puuid = str(message.get("puuid") or "")
        source = str(message.get("source") or "match")
        depth = max(0, _int(message.get("depth")))
        priority = _int(message.get("priority"), 10)
        discovered_game = message.get("discovered_from_game_id")
        discovered_game = str(discovered_game) if discovered_game else None
        discovered_ms = max(0, _int(message.get("discovered_match_created_ms")))
        incoming_family = str(message.get("seed_family") or source or "match")
        initial_delay = max(0, _int(message.get("initial_delay_ms")))
        requeue_cooldown = max(0, _int(message.get("requeue_cooldown_ms"), 0))
        discovered_queue_id = max(0, _int(message.get("discovered_queue_id")))
        classic_rank = max(0, _int(message.get("classic_affinity_rank")))
        if not puuid:
            return "noop"
        now = _utc_now()
        row = self.con.execute(
            """SELECT source, priority, min_depth, discovered_from_game_id,
                      latest_seen_match_created_ms, last_crawled_match_created_ms,
                      processed, seed_family, process_count, new_games_found,
                      first_seen_at, last_crawled_at, discovered_queue_id,
                      classic_affinity, classic_affinity_rank
               FROM crawl_seen WHERE puuid=?""",
            (puuid,),
        ).fetchone()
        if row is None:
            affinity = "candidate" if discovered_queue_id == 4310 or classic_rank > 0 else "none"
            self.con.execute(
                """INSERT INTO crawl_seen(
                    puuid,source,priority,min_depth,discovered_from_game_id,
                    first_seen_at,latest_seen_match_created_ms,last_crawled_match_created_ms,
                    processed,seed_family,discovered_queue_id,classic_affinity,
                    classic_affinity_rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)""",
                (puuid, source, priority, depth, discovered_game, now, discovered_ms,
                 incoming_family, discovered_queue_id, affinity, max(classic_rank, int(discovered_queue_id == 4310))),
            )
            self._snowball_upsert_queue_locked(
                puuid=puuid, depth=depth, source=source, priority=priority,
                discovered_from_game_id=discovered_game, discovered_match_created_ms=discovered_ms,
                requeue=True, eligible_at_ms=self._clock_ms() + initial_delay,
                seed_family=incoming_family, discovered_queue_id=discovered_queue_id,
                classic_affinity_rank=max(classic_rank, int(discovered_queue_id == 4310)),
            )
            return "new"

        old_source, old_priority, old_depth, old_game, old_latest, old_crawled, processed, old_family, process_count, new_games, first_seen, last_crawled, old_queue_id, old_affinity, old_rank = row
        best_source = source if priority < int(old_priority) or (priority == int(old_priority) and depth < int(old_depth)) else str(old_source)
        best_priority = min(priority, int(old_priority))
        best_depth = min(depth, int(old_depth))
        effective_family = str(old_family or "")
        if not effective_family or effective_family in {"unknown", "legacy_match"}:
            effective_family = incoming_family
        latest = max(int(old_latest or 0), discovered_ms)
        effective_queue = 4310 if discovered_queue_id == 4310 or int(old_queue_id or 0) == 4310 else max(0, int(old_queue_id or discovered_queue_id or 0))
        effective_rank = max(int(old_rank or 0), classic_rank, int(effective_queue == 4310))
        affinity = str(old_affinity or "none")
        if effective_rank and affinity == "none":
            affinity = "candidate"
        best_game = discovered_game if discovered_ms >= int(old_latest or 0) and discovered_game else old_game
        self.con.execute(
            """UPDATE crawl_seen SET source=?, priority=?, min_depth=?,
                      discovered_from_game_id=?, latest_seen_match_created_ms=?,
                      seed_family=?, discovered_queue_id=?, classic_affinity=?,
                      classic_affinity_rank=? WHERE puuid=?""",
            (best_source, best_priority, best_depth, best_game, latest, effective_family,
             effective_queue, affinity, effective_rank, puuid),
        )
        should_requeue = int(processed or 0) == 1 and (
            discovered_ms > int(old_crawled or 0)
            or (source == "manual_riot_id" and int(old_crawled or 0) == 0)
        )
        eligible = 0
        if should_requeue:
            eligible = self._clock_ms() + max(requeue_cooldown, initial_delay)
        became = self._snowball_upsert_queue_locked(
            puuid=puuid, depth=best_depth, source=best_source, priority=best_priority,
            discovered_from_game_id=best_game, discovered_match_created_ms=latest,
            requeue=should_requeue, eligible_at_ms=eligible, seed_family=effective_family,
            discovered_queue_id=effective_queue, classic_affinity_rank=effective_rank,
        )
        if should_requeue and became:
            self.con.execute("UPDATE crawl_seen SET processed=0 WHERE puuid=?", (puuid,))
            return "requeued"
        if int(processed or 0) == 0:
            return "updated"
        return "noop"

    def _snowball_upsert_queue_locked(
        self,
        *,
        puuid: str,
        depth: int,
        source: str,
        priority: int,
        discovered_from_game_id: str | None,
        discovered_match_created_ms: int,
        requeue: bool,
        eligible_at_ms: int,
        seed_family: str,
        discovered_queue_id: int,
        classic_affinity_rank: int,
    ) -> bool:
        now = _utc_now()
        row = self.con.execute(
            """SELECT status,priority,depth,discovered_match_created_ms,seed_family,
                      discovered_queue_id,classic_affinity_rank
               FROM crawl_queue WHERE puuid=?""",
            (puuid,),
        ).fetchone()
        if row is None:
            self.con.execute(
                """INSERT INTO crawl_queue(
                    puuid,depth,source,priority,discovered_from_game_id,
                    discovered_match_created_ms,enqueued_at,updated_at,claimed_by,
                    claimed_at_ms,eligible_at_ms,status,seed_family,discovered_queue_id,
                    classic_affinity_rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 'pending', ?, ?, ?)""",
                (puuid, depth, source, priority, discovered_from_game_id,
                 discovered_match_created_ms, now, now, max(0, int(eligible_at_ms)),
                 seed_family, max(0, int(discovered_queue_id)), max(0, int(classic_affinity_rank))),
            )
            return True
        status, old_priority, old_depth, old_match, old_family, old_queue_id, old_rank = row
        effective_family = str(old_family or "")
        if not effective_family or effective_family in {"unknown", "legacy_match"}:
            effective_family = seed_family or effective_family or "unknown"
        effective_queue = 4310 if discovered_queue_id == 4310 or int(old_queue_id or 0) == 4310 else max(0, int(old_queue_id or discovered_queue_id or 0))
        effective_rank = max(int(old_rank or 0), max(0, int(classic_affinity_rank)))
        became = False
        if str(status) != "pending" and requeue:
            self.con.execute(
                """UPDATE crawl_queue SET depth=?,source=?,priority=?,discovered_from_game_id=?,
                          discovered_match_created_ms=?,updated_at=?,eligible_at_ms=?,
                          claimed_by=NULL,claimed_at_ms=0,status='pending',seed_family=?,
                          discovered_queue_id=?,classic_affinity_rank=? WHERE puuid=?""",
                (depth, source, priority, discovered_from_game_id, discovered_match_created_ms,
                 now, max(0, int(eligible_at_ms)), effective_family, effective_queue,
                 effective_rank, puuid),
            )
            became = True
        elif str(status) in {"pending", "in_progress"} and (
            discovered_match_created_ms > int(old_match or 0)
            or priority < int(old_priority or priority)
            or depth < int(old_depth or depth)
            or effective_family != str(old_family or "")
            or effective_queue != int(old_queue_id or 0)
            or effective_rank != int(old_rank or 0)
        ):
            self.con.execute(
                """UPDATE crawl_queue SET depth=?,source=?,priority=?,discovered_from_game_id=?,
                          discovered_match_created_ms=?,updated_at=?,seed_family=?,
                          discovered_queue_id=?,classic_affinity_rank=? WHERE puuid=?""",
                (depth, source, priority, discovered_from_game_id, discovered_match_created_ms,
                 now, effective_family, effective_queue, effective_rank, puuid),
            )
        return became

    def _snowball_claim_next_locked(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        sb = _snowball()
        now = _int(message.get("now_ms"), self._clock_ms())
        timeout = max(1, _int(message.get("claim_timeout_ms"), 300_000))
        now_text = _utc_now()
        self.con.execute(
            """UPDATE crawl_queue SET status='pending',claimed_by=NULL,claimed_at_ms=0,updated_at=?
               WHERE status='in_progress' AND claimed_at_ms > 0 AND claimed_at_ms < ?""",
            (now_text, now - timeout),
        )
        classic_percent = min(100, max(0, _int(message.get("classic_claim_percent"), 10)))
        claim_number = next(self._claim_count)
        row = None
        lane = "general"
        if sb._classic_claim_slot(claim_number, classic_percent):
            arm = sb._classic_lane_arm_for_slot(claim_number, classic_percent)
            row = self.con.execute(
                sb._CLASSIC_LANE_SQL[arm], sb._classic_lane_params(now, arm)
            ).fetchone()
            if row is not None:
                lane = f"classic_{arm}"
        take_unvisited = (claim_number % sb._UNVISITED_CLAIM_PERIOD) == 0
        if row is None and take_unvisited:
            row = self.con.execute(
                """SELECT q.queue_idx, q.puuid, q.depth, q.source,
                          q.discovered_match_created_ms, q.seed_family,
                          q.discovered_queue_id
                   FROM crawl_queue q
                   WHERE q.status = 'pending'
                     AND q.eligible_at_ms <= ?
                     AND q.classic_affinity_rank = 0
                     AND NOT EXISTS (
                           SELECT 1 FROM crawl_seen s
                           WHERE s.puuid = q.puuid AND s.process_count > 0)
                   ORDER BY q.discovered_match_created_ms DESC,
                            q.priority ASC, q.depth ASC, q.queue_idx ASC
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if row is not None:
                lane = "unvisited"
        if row is None:
            row = self.con.execute(
                """SELECT queue_idx, puuid, depth, source, discovered_match_created_ms,
                          seed_family, discovered_queue_id
                   FROM crawl_queue
                   WHERE status = 'pending'
                     AND eligible_at_ms <= ?
                     AND classic_affinity_rank = 0
                   ORDER BY discovered_match_created_ms DESC, priority ASC, depth ASC,
                            updated_at ASC, queue_idx ASC
                   LIMIT 1""",
                (now,),
            ).fetchone()
        if row is None:
            arm = sb._classic_lane_arm_for_slot(claim_number, classic_percent)
            row = self.con.execute(
                sb._CLASSIC_LANE_SQL[arm], sb._classic_lane_params(now, arm)
            ).fetchone()
            if row is not None:
                lane = f"classic_fallback_{arm}"
        if row is None:
            return self._finish(message, {"ok": True, "status": "EMPTY", "mutated": False})
        queue_idx, puuid, depth, source, claimed_ms, family, discovered_queue = row
        before = self.con.total_changes
        self.con.execute(
            """UPDATE crawl_queue SET status='in_progress',claimed_by=?,claimed_at_ms=?,updated_at=?
               WHERE queue_idx=? AND status='pending'""",
            (channel_id, now, _utc_now(), queue_idx),
        )
        if self.con.total_changes <= before:
            return self._finish(message, {"ok": True, "status": "BUSY", "mutated": False})
        existing = self.con.execute(
            "SELECT generation FROM writer_player_claims WHERE puuid=?", (str(puuid),)
        ).fetchone()
        generation = (int(existing[0]) if existing else 0) + 1
        token = self._claim_token()
        expires = now + self._lease(message)
        self.con.execute(
            """INSERT INTO writer_player_claims(puuid,channel_id,token,generation,claimed_at_ms,expires_at_ms,status)
               VALUES (?, ?, ?, ?, ?, ?, 'in_progress')
               ON CONFLICT(puuid) DO UPDATE SET channel_id=excluded.channel_id,token=excluded.token,
               generation=excluded.generation,claimed_at_ms=excluded.claimed_at_ms,
               expires_at_ms=excluded.expires_at_ms,status='in_progress'""",
            (str(puuid), channel_id, token, generation, now, expires),
        )
        self._checkpoint("player_claim")
        return self._finish(message, {
            "ok": True, "status": "CLAIMED", "mutated": True, "puuid": str(puuid),
            "depth": int(depth), "source": str(source), "claimed_match_created_ms": int(claimed_ms),
            "seed_family": str(family or "unknown"), "discovered_queue_id": int(discovered_queue or 0),
            "claim_lane": lane, "token": token, "generation": generation, "expires_at_ms": expires,
        })

    def _snowball_queue(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        operation = str(message.get("operation") or "")
        read_ops = {"pending_count", "source_count", "next_wait"}
        if operation in read_ops:
            if operation == "pending_count":
                row = self.con.execute("SELECT COUNT(*) FROM crawl_queue WHERE status='pending'").fetchone()
                return self._finish(message, {"ok": True, "status": "COUNT", "mutated": False, "count": int(row[0]) if row else 0})
            if operation == "source_count":
                source = str(message.get("source") or "")
                if not source:
                    return self._error(message, "MISSING_FIELD")
                row = self.con.execute(
                    "SELECT COUNT(*) FROM crawl_queue WHERE source=? AND status IN ('pending','in_progress')",
                    (source,),
                ).fetchone()
                return self._finish(message, {"ok": True, "status": "COUNT", "mutated": False, "count": int(row[0]) if row else 0, "source": source})
            row = self.con.execute("SELECT MIN(eligible_at_ms) FROM crawl_queue WHERE status='pending'").fetchone()
            if row is None or row[0] is None:
                return self._finish(message, {"ok": True, "status": "EMPTY", "mutated": False, "wait_ms": None})
            return self._finish(message, {"ok": True, "status": "WAIT", "mutated": False, "wait_ms": max(0, int(row[0]) - self._clock_ms())})

        self._begin()
        try:
            if operation in {"enqueue", "upsert"}:
                result = self._snowball_enqueue_locked(message)
                return self._finish(message, {"ok": True, "status": result.upper(), "mutated": result != "noop", "result": result})
            if operation == "claim_next":
                return self._snowball_claim_next_locked(message, channel_id)
            if operation == "requeue_stale":
                timeout = max(1, _int(message.get("claim_timeout_ms"), 300_000))
                before = self.con.total_changes
                self.con.execute(
                    """UPDATE crawl_queue SET status='pending',claimed_by=NULL,claimed_at_ms=0,updated_at=?
                       WHERE status='in_progress' AND claimed_at_ms > 0 AND claimed_at_ms < ?""",
                    (_utc_now(), self._clock_ms() - timeout),
                )
                return self._finish(message, {"ok": True, "status": "RECLAIMED", "mutated": self.con.total_changes > before, "count": self.con.total_changes - before})
            if operation == "suggested_reseed":
                puuid = str(message.get("puuid") or "")
                cooldown = max(0, _int(message.get("cooldown_ms"), 0))
                cutoff = datetime.fromtimestamp(max(0.0, time.time() - cooldown / 1000.0), tz=timezone.utc).isoformat()
                row = self.con.execute(
                    """SELECT source,priority,min_depth,discovered_from_game_id,
                              latest_seen_match_created_ms,last_crawled_at,processed,seed_family
                       FROM crawl_seen WHERE puuid=?""",
                    (puuid,),
                ).fetchone()
                if row is None or int(row[6] or 0) != 1 or (row[5] is not None and str(row[5]) > cutoff):
                    self.con.rollback()
                    return self._finish(message, {"ok": True, "status": "NOOP", "mutated": False, "result": "noop"})
                became = self._snowball_upsert_queue_locked(
                    puuid=puuid, depth=int(row[2]), source=str(row[0]), priority=int(row[1]),
                    discovered_from_game_id=str(row[3]) if row[3] else None,
                    discovered_match_created_ms=int(row[4] or 0), requeue=True, eligible_at_ms=0,
                    seed_family=str(row[7] or "unknown"), discovered_queue_id=0,
                    classic_affinity_rank=0,
                )
                if became:
                    self.con.execute("UPDATE crawl_seen SET processed=0 WHERE puuid=?", (puuid,))
                return self._finish(message, {"ok": True, "status": "REQUEUED" if became else "NOOP", "mutated": became, "result": "requeued" if became else "noop"})
            if operation in {"reseed_recent", "reseed_source"}:
                cap = max(0, _int(message.get("cap"), 80 if operation == "reseed_recent" else 120))
                cooldown = max(0, _int(message.get("cooldown_ms"), 0))
                if cap <= 0:
                    self.con.rollback()
                    return self._finish(message, {"ok": True, "status": "NOOP", "mutated": False, "count": 0})
                cutoff = datetime.fromtimestamp(max(0.0, time.time() - cooldown / 1000.0), tz=timezone.utc).isoformat()
                if operation == "reseed_recent":
                    rows = self.con.execute(
                        """SELECT puuid,source,priority,min_depth,discovered_from_game_id,
                                  latest_seen_match_created_ms,seed_family
                           FROM crawl_seen WHERE new_games_found > 0
                             AND latest_seen_match_created_ms > 0
                             AND (last_crawled_at IS NULL OR last_crawled_at <= ?)
                           ORDER BY latest_seen_match_created_ms DESC,new_games_found DESC,
                                    priority ASC,min_depth ASC,first_seen_at DESC LIMIT ?""",
                        (cutoff, cap),
                    ).fetchall()
                else:
                    sources = tuple(str(item) for item in (message.get("sources") or []) if str(item))
                    if not sources:
                        self.con.rollback()
                        return self._finish(message, {"ok": True, "status": "NOOP", "mutated": False, "count": 0})
                    placeholders = ",".join("?" for _ in sources)
                    rows = self.con.execute(
                        f"""SELECT puuid,source,priority,min_depth,discovered_from_game_id,
                                  latest_seen_match_created_ms,seed_family
                           FROM crawl_seen WHERE source IN ({placeholders})
                             AND new_games_found > 0
                             AND (last_crawled_at IS NULL OR last_crawled_at <= ?)
                           ORDER BY priority ASC,latest_seen_match_created_ms DESC,
                                    process_count ASC,first_seen_at DESC LIMIT ?""",
                        (*sources, cutoff, cap),
                    ).fetchall()
                added = 0
                for row in rows:
                    became = self._snowball_upsert_queue_locked(
                        puuid=str(row[0]), depth=int(row[3]), source=str(row[1]), priority=int(row[2]),
                        discovered_from_game_id=str(row[4]) if row[4] else None,
                        discovered_match_created_ms=int(row[5] or 0), requeue=True, eligible_at_ms=0,
                        seed_family=str(row[6] or "unknown"), discovered_queue_id=0,
                        classic_affinity_rank=0,
                    )
                    if became:
                        self.con.execute("UPDATE crawl_seen SET processed=0 WHERE puuid=?", (str(row[0]),))
                        added += 1
                return self._finish(message, {"ok": True, "status": "RESEEDED", "mutated": bool(added), "count": added})
            return self._error(message, "INVALID_OPERATION")
        except Exception:
            self.con.rollback()
            raise

    def _snowball_player(self, message: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
        operation = str(message.get("operation") or "")
        if operation == "finalize":
            # Reuse the existing atomic finalize implementation.  It validates
            # the claim token/generation before touching either frontier table.
            return self._finalize_player(message, channel_id)
        puuid = str(message.get("puuid") or "")
        token = str(message.get("token") or "")
        generation = _int(message.get("generation"))
        now = _int(message.get("now_ms"), self._clock_ms())
        if operation not in {"defer", "release_unavailable", "mark_done"}:
            return self._error(message, "INVALID_OPERATION")
        if not puuid or not token or generation < 1:
            return self._error(message, "MISSING_FIELD")
        self._begin()
        try:
            if not self._claim_ok("writer_player_claims", puuid, channel_id, token, generation, now):
                self.con.rollback()
                return self._stale(message)
            if operation == "defer":
                seen = self.con.execute(
                    "SELECT process_count FROM crawl_seen WHERE puuid=?",
                    (puuid,),
                ).fetchone()
                process_count = int(seen[0]) if seen else 0
                if process_count >= _snowball()._EMPTY_HISTORY_RETRY_LIMIT:
                    self.con.rollback()
                    return self._finish(
                        message,
                        {
                            "ok": True,
                            "status": "RETRY_LIMIT",
                            "mutated": False,
                            "puuid": puuid,
                        },
                    )
            delay = max(0, _int(message.get("delay_ms"), 90_000 if operation == "defer" else 60_000))
            now_text = _utc_now()
            self.con.execute(
                """UPDATE crawl_queue SET status='pending',claimed_by=NULL,claimed_at_ms=0,
                          eligible_at_ms=?,updated_at=? WHERE puuid=?""",
                (now + delay, now_text, puuid),
            )
            if operation == "defer":
                self.con.execute("UPDATE crawl_seen SET processed=0,process_count=process_count+1,last_crawled_at=? WHERE puuid=?", (now_text, puuid))
            self.con.execute(
                "UPDATE writer_player_claims SET status='released',channel_id='',token='',expires_at_ms=0 WHERE puuid=?",
                (puuid,),
            )
            self._checkpoint("player_requeue")
            return self._finish(message, {"ok": True, "status": "REQUEUED", "mutated": True, "puuid": puuid, "eligible_at_ms": now + delay})
        except Exception:
            self.con.rollback()
            raise

    def _merge_queue_counts(self, old: str | None, incoming: Any) -> str | None:
        if not isinstance(incoming, Mapping) or not incoming:
            return old
        counts: dict[str, int] = {}
        try:
            parsed = json.loads(old) if old else {}
            if isinstance(parsed, Mapping):
                counts = {str(k): int(v) for k, v in parsed.items() if isinstance(v, int) and not isinstance(v, bool)}
        except (TypeError, ValueError, json.JSONDecodeError):
            counts = {}
        for key, value in incoming.items():
            if isinstance(value, int) and not isinstance(value, bool):
                counts[str(key)] = counts.get(str(key), 0) + int(value)
        return _json(counts) if counts else old

    def _upsert_player_locked(self, puuid: str, *, source: str, depth: int, priority: int, discovered_from_game_id: str | None, discovered_match_created_ms: int, seed_family: str, now: str) -> None:
        existing = self.con.execute("SELECT latest_seen_match_created_ms, last_crawled_match_created_ms, processed FROM crawl_seen WHERE puuid=?", (puuid,)).fetchone()
        if existing is None:
            self.con.execute("INSERT INTO crawl_seen(puuid, source, priority, min_depth, discovered_from_game_id, first_seen_at, latest_seen_match_created_ms, last_crawled_match_created_ms, processed, seed_family, discovered_queue_id, classic_affinity, classic_affinity_rank) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, 'none', 0)", (puuid, source, int(priority), int(depth), discovered_from_game_id, now, int(discovered_match_created_ms), seed_family))
        else:
            self.con.execute("UPDATE crawl_seen SET latest_seen_match_created_ms=MAX(latest_seen_match_created_ms, ?), discovered_from_game_id=COALESCE(?, discovered_from_game_id) WHERE puuid=?", (int(discovered_match_created_ms), discovered_from_game_id, puuid))
        queue = self.con.execute("SELECT status FROM crawl_queue WHERE puuid=?", (puuid,)).fetchone()
        if queue is None:
            self.con.execute("INSERT INTO crawl_queue(puuid, depth, source, priority, discovered_from_game_id, discovered_match_created_ms, enqueued_at, updated_at, claimed_by, claimed_at_ms, eligible_at_ms, status, seed_family, discovered_queue_id, classic_affinity_rank) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, 'pending', ?, 0, 0)", (puuid, int(depth), source, int(priority), discovered_from_game_id, int(discovered_match_created_ms), now, now, seed_family))
        elif str(queue[0]) == "done":
            self.con.execute("UPDATE crawl_queue SET status='pending', eligible_at_ms=0, updated_at=?, seed_family=? WHERE puuid=?", (now, seed_family, puuid))


__all__ = ["WriterService"]
