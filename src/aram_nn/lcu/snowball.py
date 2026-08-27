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

import functools
import hashlib
import itertools
import json
import math
import os
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Any, Mapping

from .client import (
    LCUClient,
    get_apex_league,
    get_current_summoner,
    get_friends,
    get_game_detail,
    get_game_version,
    get_league_ladders,
    get_match_history,
    get_summoner_by_id,
    get_suggested_players,
    lookup_summoners_by_riot_ids,
)
from .poller import DEFAULT_QUEUES, _parse_game_detail, _participants_payload_has_postgame_stats
from .process import get_credentials
from .db_state import update_capture_watermark

_EMPTY_QUEUE_GRACE_SEC = 30.0
_EMPTY_QUEUE_IDLE_POLL_SEC = 60.0
_SUGGESTED_RESEED_REQUEUE_COOLDOWN_SEC = 10 * 60
_RECENT_ACTIVE_RESEED_CAP = 80
_RECENT_ACTIVE_RESEED_COOLDOWN_SEC = 10 * 60
_RECENT_ACTIVE_BACKOFF_ZERO_STREAK = 25
_RECENT_ACTIVE_BACKOFF_SEC = 45 * 60
_SOURCE_FAMILY_RESEED_CAP = 120
_SOURCE_FAMILY_RESEED_COOLDOWN_SEC = 20 * 60
_SOURCE_FAMILY_BACKOFF_ZERO_STREAK = 25
_SOURCE_FAMILY_BACKOFF_SEC = 45 * 60
_MANUAL_SEED_HOT_WINDOW_HOURS = 24
_MANUAL_SEED_WARM_WINDOW_HOURS = 72
_LCU_UNAVAILABLE_RETRY_DELAY_MS = 60_000
_SCHEMA_INIT_RETRY_ATTEMPTS = 60
_SCHEMA_INIT_RETRY_SLEEP_SEC = 2.0
_DB_RETRY_ATTEMPTS = 10
_DB_RETRY_BASE_SLEEP_SEC = 1.0
_DB_RETRY_MAX_SLEEP_SEC = 15.0

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
    participants_json TEXT,
    participants_private_json TEXT
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
    new_games_by_queue_json       TEXT,
    latest_seen_match_created_ms  INTEGER NOT NULL DEFAULT 0,
    last_crawled_match_created_ms INTEGER NOT NULL DEFAULT 0,
    processed                     INTEGER NOT NULL DEFAULT 0,
    discovered_queue_id           INTEGER NOT NULL DEFAULT 0,
    classic_affinity              TEXT NOT NULL DEFAULT 'none',
    classic_affinity_rank         INTEGER NOT NULL DEFAULT 0,
    classic_games_24h             INTEGER NOT NULL DEFAULT 0,
    classic_games_recent          INTEGER NOT NULL DEFAULT 0,
    classic_last_seen_ms          INTEGER NOT NULL DEFAULT 0,
    classic_revisit_interval_ms   INTEGER NOT NULL DEFAULT 0
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
    status                      TEXT NOT NULL DEFAULT 'pending',
    discovered_queue_id         INTEGER NOT NULL DEFAULT 0,
    classic_affinity_rank       INTEGER NOT NULL DEFAULT 0
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

# _sync_source_priorities filters both frontier tables by ``source`` on every
# worker start.  Neither table had an index on it, so each of the 9 sources cost a
# full scan of ~600k rows, twice -- ~11M rows examined inside one write
# transaction.  Two workers starting together meant one held the write lock long
# enough for the other to blow past busy_timeout and die with "database is
# locked" before it consumed anything.  The existing
# idx_crawl_queue_status_priority cannot serve this: it leads with ``status``.
_CREATE_SOURCE_PRIORITY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_seen_source_priority
ON crawl_seen(source, priority);
"""

_CREATE_QUEUE_SOURCE_PRIORITY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_queue_source_priority
ON crawl_queue(source, priority);
"""

_CREATE_CLASSIC_CLAIM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_queue_classic_claim
ON crawl_queue(
    status,
    eligible_at_ms ASC,
    classic_affinity_rank DESC,
    discovered_match_created_ms DESC,
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

_CREATE_CRAWL_RUNTIME_STATE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_runtime_state (
    state_key   TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

_CREATE_CRAWL_VISIT_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS crawl_visit_events (
    visit_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    puuid                     TEXT NOT NULL,
    revisit_arm               TEXT NOT NULL,
    is_revisit                INTEGER NOT NULL,
    visited_at                TEXT NOT NULL,
    previous_crawled_at       TEXT,
    revisit_interval_ms       INTEGER,
    process_number            INTEGER NOT NULL,
    source                    TEXT NOT NULL,
    seed_family               TEXT NOT NULL,
    worker_id                 TEXT,
    current_patch             TEXT,
    history_game_count        INTEGER NOT NULL,
    target_game_count         INTEGER NOT NULL,
    new_games_found           INTEGER NOT NULL,
    new_games_by_queue_json   TEXT,
    claim_lane                TEXT NOT NULL DEFAULT 'general',
    classic_affinity          TEXT NOT NULL DEFAULT 'none',
    classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_CRAWL_VISIT_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_visit_events_revisit_interval
ON crawl_visit_events(is_revisit, revisit_arm, revisit_interval_ms, visited_at);
"""

_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_game_claims_status
ON crawl_game_claims(status, claimed_at_ms, updated_at, game_id);
"""

# Fallback only: consulted when LCU history omits queueId (see
# _queue_id_from_meta).  JADE is the 經典 mode (queue 4310, map 453) and was
# missing, so those games resolved to -1 and went invisible to both the
# classifier and _extract_target_game_ids.
#
# KIWI is ambiguous and cannot be fixed here: queue 2400 (大混戰) and 2450
# (大混戰經典風) both report gameMode=KIWI, so a row missing queueId can only
# be guessed at.  It maps to 2400 as the overwhelmingly more common case.
_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450, "JADE": 4310}
_SOURCE_PRIORITY = {
    "self": 0,
    "match": 10,
    "suggested": 15,
    # Leaderboard / manual seeds should only open new communities; once a seed
    # produces real matches, we want those fresher match-derived nodes first.
    "friend": 20,
    "apex": 30,
    "ladder": 40,
    "manual_riot_id": 60,
    "riot_tier": 70,
}
# Sources that are *root entry points* into the player graph. Anything else
# (currently only "match") inherits its seed_family from the parent node that
# discovered it, so the family represents transitive attribution.
_SEED_FAMILY_ROOTS = frozenset(
    {"self", "friend", "ladder", "apex", "manual_riot_id", "riot_tier", "suggested"}
)
# Used during schema backfill for legacy match-source rows whose parent chain
# is unknown — they neither credit nor debit any active source family.
_LEGACY_MATCH_FAMILY = "legacy_match"
_UNKNOWN_FAMILY = "unknown"
_LCU_RIOT_ID_LOOKUP_BATCH = 10
_RIOT_TIER_HYDRATION_DELAY_MS = 90_000
_MANUAL_SEED_HYDRATION_DELAY_MS = 90_000
_EMPTY_HISTORY_RETRY_LIMIT = 5
_CLASSIC_QUEUE_ID = 4310
_CLASSIC_DEFAULT_CLAIM_PERCENT = 10
_CLASSIC_DEFAULT_REVISIT_MIN_MS = 10 * 3600_000
_CLASSIC_DEFAULT_REVISIT_MAX_MS = 7 * 24 * 3600_000
_CLASSIC_HISTORY_TARGET_GAMES = 20.0
_CLASSIC_HISTORY_FILL_FRACTION = 0.8

# --- Classic yield estimator ---------------------------------------------
# classic_affinity_rank is a single-visit snapshot, and the Classic lane used to
# order purely by eligible_at_ms, so rank never actually broke a tie (timestamps
# are per-millisecond).  Measured 2026-08-27: every one of the 747 rank>=2 rows
# sat past position 24,640 of 34,531 due rows, behind candidates yielding 52
# Classic games per 1,000 visits against their 821-3,384.
#
# The snapshot also only demotes.  One quiet window sends a player to rank 0 with
# no memory of what they produced before: 2,316 rank-0 players had 5,266 lifetime
# Classic games between them, 44.3% of everything ever captured, and 652 of them
# had >=3 -- against a rank>=2 pool of just 768.
#
# So the lane orders by expected yield instead: a decayed games-per-visit rate
# times how much of the player's ~20-row window has refilled since we last looked.
# The saturation term is what keeps this from starving the tail -- a quiet
# player's score climbs until their window is full and then stops, so they
# eventually outrank a heavy player who was visited an hour ago, but they never
# outrank one whose window has refilled.
_CLASSIC_RATE_TAU_MS = 7 * 24 * 3600_000
# Prior for a player we have never visited.  Measured over visited players:
# 0.055 Classic/visit for ones discovered in a Classic game, 0.0102 for the
# population.  60% of the Classic lane's due rows have never been visited, so
# without a prior they would all score zero and the lane would only ever revisit.
_CLASSIC_RATE_PRIOR_DISCOVERED = 0.055
_CLASSIC_RATE_PRIOR_POPULATION = 0.0102
# Pseudo-visits of prior weight.  Stops one lucky first visit from impersonating
# a heavy player.
_CLASSIC_RATE_PRIOR_WEIGHT = 3.0
# Bootstrap cap.  Lifetime counters seed the estimator so the 2,316 misfiled
# players return to the pool immediately, but crediting a player with 40 visits
# of evidence would take weeks of decay to forget if they have since quit.
_CLASSIC_RATE_BOOTSTRAP_MAX_DEN = 10.0



def _lifetime_classic_games(queue_counts_json: str | None) -> int:
    """Classic games this player has produced across every visit so far."""
    if not queue_counts_json:
        return 0
    try:
        return int(json.loads(queue_counts_json).get(str(_CLASSIC_QUEUE_ID), 0))
    except (ValueError, TypeError, AttributeError):
        return 0

def _decay_classic_rate(
    num: float,
    den: float,
    elapsed_ms: int,
    classic_found: int,
    *,
    tau_ms: int = _CLASSIC_RATE_TAU_MS,
) -> tuple[float, float]:
    """Fold one visit into the decayed games-per-visit estimate.

    Counts are in visits, but the decay runs on wall time: a player who took a
    day off decays by exp(-24/168) = 0.87 rather than being reset by one empty
    window, which is the failure mode of the single-visit label.
    """
    decay = math.exp(-max(0, int(elapsed_ms)) / float(max(1, tau_ms)))
    return num * decay + max(0, int(classic_found)), den * decay + 1.0



def _classic_span_ms(revisit_interval_ms: int) -> int:
    """How long this player's ~20-row history takes to refill.

    revisit_interval_ms is already that fill time scaled by the safety factor,
    so undo the factor to recover the span the saturation term needs.
    """
    return max(
        _CLASSIC_DEFAULT_REVISIT_MIN_MS,
        int(max(0, int(revisit_interval_ms)) / _CLASSIC_HISTORY_FILL_FRACTION),
    )

def _classic_lambda(num: float, den: float, *, classic_discovered: bool) -> float:
    """Shrink the decayed rate toward the prior for this player's discovery."""
    prior = (
        _CLASSIC_RATE_PRIOR_DISCOVERED
        if classic_discovered
        else _CLASSIC_RATE_PRIOR_POPULATION
    )
    weight = _CLASSIC_RATE_PRIOR_WEIGHT
    return (float(num) + prior * weight) / (float(den) + weight)

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


@dataclass(frozen=True)
class ClassicAffinityProfile:
    label: str
    rank: int
    games_24h: int
    games_recent: int
    last_seen_ms: int
    revisit_interval_ms: int


@dataclass(frozen=True)
class PlayerClaim:
    puuid: str
    depth: int
    source: str
    claimed_match_created_ms: int
    seed_family: str
    discovered_queue_id: int
    claim_lane: str
    token: str
    generation: int


@dataclass(frozen=True)
class GameClaim:
    game_id: str
    token: str
    generation: int


class SnowballWriterError(RuntimeError):
    """A writer response that cannot safely drive the producer forward."""


class SnowballWriterStaleClaim(SnowballWriterError):
    """The writer rejected a producer mutation because its lease is stale."""


class SnowballWriterClaimsStopped(SnowballWriterError):
    """The writer is no longer handing out new player claims."""


class SnowballWriterFacade:
    """Narrow snowball storage facade over a writer client.

    The producer never receives a SQLite connection in this mode.  Every
    mutable read/write is a fixed capability request and writer failures are
    raised rather than silently falling back to direct SQLite.
    """

    _is_rpc_storage = True

    def __init__(self, client: Any, *, request_prefix: str | None = None) -> None:
        if client is None:
            raise ValueError("writer client is required")
        self.client = client
        self._request_prefix = request_prefix or f"snowball-{uuid.uuid4().hex[:12]}"
        self._request_counter = itertools.count()

    def _request(self, command: str, **fields: Any) -> dict[str, Any]:
        request_id = f"{self._request_prefix}-{next(self._request_counter)}"
        message = {"version": 1, "command": command, "request_id": request_id, **fields}
        submit = getattr(self.client, "submit", None) or getattr(self.client, "call", None) or getattr(self.client, "request", None)
        if submit is None:
            submit = getattr(self.client, "handle", None)
        if submit is None:
            raise TypeError("writer client must expose submit/call/request/handle")
        try:
            response = submit(message)
        except Exception as exc:
            if type(exc).__name__ == "WriterLifecycleError" and "CLAIMS_STOPPED" in str(exc):
                raise SnowballWriterClaimsStopped("CLAIMS_STOPPED") from exc
            # The transport is fail-closed; preserve that contract for the
            # producer instead of attempting a local DB fallback.
            raise SnowballWriterError("writer request failed") from exc
        if not isinstance(response, Mapping):
            raise SnowballWriterError("invalid writer response")
        result = dict(response)
        if result.get("status") == "STALE_CLAIM":
            raise SnowballWriterStaleClaim("STALE_CLAIM")
        if result.get("ok") is False:
            raise SnowballWriterError(str(result.get("status") or "WRITER_ERROR"))
        return result

    def initialize(self, *, worker_id: str | None, claim_timeout_ms: int) -> dict[str, Any]:
        return self._request("snowball_init", worker_id=worker_id or "", claim_timeout_ms=max(1, int(claim_timeout_ms)))

    def runtime_get(self, key: str) -> str | None:
        response = self._request("snowball_runtime", operation="get", key=str(key))
        value = response.get("value")
        return str(value) if value is not None else None

    def runtime_set(self, key: str, value: str) -> None:
        self._request("snowball_runtime", operation="set", key=str(key), value=str(value))

    def runtime_delete(self, key: str) -> None:
        self._request("snowball_runtime", operation="delete", key=str(key))

    def family_increment(self, seed_family: str, delta: int) -> None:
        self._request("snowball_runtime", operation="family_increment", seed_family=str(seed_family), delta=max(0, int(delta)))

    def family_read(self, seed_family: str) -> int:
        value = self._request("snowball_runtime", operation="family_read", seed_family=str(seed_family)).get("value")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def enqueue_player(self, **kwargs: Any) -> str:
        response = self._request("snowball_queue", operation="enqueue", **kwargs)
        return str(response.get("result") or "noop")

    def upsert_queue(self, **kwargs: Any) -> bool:
        response = self._request("snowball_queue", operation="upsert", **kwargs)
        return str(response.get("result") or "noop") == "requeued"

    def suggested_reseed(self, puuid: str, cooldown_ms: int) -> bool:
        response = self._request("snowball_queue", operation="suggested_reseed", puuid=str(puuid), cooldown_ms=max(0, int(cooldown_ms)))
        return str(response.get("result") or "noop") == "requeued"

    def reseed_recent(self, *, cap: int, cooldown_ms: int) -> int:
        response = self._request("snowball_queue", operation="reseed_recent", cap=max(0, int(cap)), cooldown_ms=max(0, int(cooldown_ms)))
        return int(response.get("count") or 0)

    def reseed_source(self, *, sources: tuple[str, ...], cap: int, cooldown_ms: int) -> int:
        response = self._request("snowball_queue", operation="reseed_source", sources=list(sources), cap=max(0, int(cap)), cooldown_ms=max(0, int(cooldown_ms)))
        return int(response.get("count") or 0)

    def requeue_stale(self, claim_timeout_ms: int) -> int:
        response = self._request("snowball_queue", operation="requeue_stale", claim_timeout_ms=max(1, int(claim_timeout_ms)))
        return int(response.get("count") or 0)

    def pending_count(self) -> int:
        response = self._request("snowball_queue", operation="pending_count")
        return int(response.get("count") or 0)

    def source_count(self, source: str) -> int:
        response = self._request("snowball_queue", operation="source_count", source=str(source))
        return int(response.get("count") or 0)

    def next_wait_ms(self) -> int | None:
        value = self._request("snowball_queue", operation="next_wait").get("wait_ms")
        return None if value is None else max(0, int(value))

    def claim_next(self, *, worker_id: str, claim_timeout_ms: int, classic_claim_percent: int) -> PlayerClaim | None:
        response = self._request(
            "snowball_queue", operation="claim_next", worker_id=str(worker_id),
            claim_timeout_ms=max(1, int(claim_timeout_ms)), classic_claim_percent=min(100, max(0, int(classic_claim_percent))),
        )
        if str(response.get("status")) in {"EMPTY", "BUSY"}:
            return None
        required = ("puuid", "token", "generation")
        if any(key not in response for key in required):
            raise SnowballWriterError("invalid player claim response")
        return PlayerClaim(
            puuid=str(response["puuid"]), depth=int(response.get("depth") or 0),
            source=str(response.get("source") or "match"),
            claimed_match_created_ms=int(response.get("claimed_match_created_ms") or 0),
            seed_family=str(response.get("seed_family") or _UNKNOWN_FAMILY),
            discovered_queue_id=int(response.get("discovered_queue_id") or 0),
            claim_lane=str(response.get("claim_lane") or "general"),
            token=str(response["token"]), generation=int(response["generation"]),
        )

    def claim_game(self, game_id: str, *, claim_timeout_ms: int) -> GameClaim | None:
        response = self._request("game_claim", game_id=str(game_id), lease_ms=max(1, int(claim_timeout_ms)))
        if str(response.get("status")) in {"DONE", "BUSY"}:
            return None
        if str(response.get("status")) != "CLAIMED" or "token" not in response or "generation" not in response:
            raise SnowballWriterError("invalid game claim response")
        return GameClaim(game_id=str(game_id), token=str(response["token"]), generation=int(response["generation"]))

    def commit_game(self, claim: GameClaim, record: Mapping[str, Any], participant_puuids: list[str]) -> bool:
        response = self._request(
            "commit_game", game_id=claim.game_id, token=claim.token, generation=claim.generation,
            record=dict(record), participant_puuids=list(participant_puuids),
        )
        return str(response.get("status")) in {"COMMITTED", "DUPLICATE"}

    def mark_game_done(self, claim: GameClaim) -> None:
        self._request("mark_game_done", game_id=claim.game_id, token=claim.token, generation=claim.generation)

    def release_game(self, claim: GameClaim) -> None:
        self._request("release_game", game_id=claim.game_id, token=claim.token, generation=claim.generation)

    def finalize_player(self, claim: PlayerClaim, **kwargs: Any) -> bool:
        response = self._request(
            "snowball_player", operation="finalize", puuid=claim.puuid,
            token=claim.token, generation=claim.generation, **kwargs,
        )
        return str(response.get("status")) == "REQUEUED"

    def defer_player(self, claim: PlayerClaim, *, delay_ms: int, reason: str) -> bool:
        response = self._request(
            "snowball_player", operation="defer", puuid=claim.puuid,
            token=claim.token, generation=claim.generation, delay_ms=max(0, int(delay_ms)), reason=str(reason),
        )
        return str(response.get("status")) == "REQUEUED"

    def release_player_unavailable(self, claim: PlayerClaim, *, delay_ms: int) -> None:
        self._request(
            "snowball_player", operation="release_unavailable", puuid=claim.puuid,
            token=claim.token, generation=claim.generation, delay_ms=max(0, int(delay_ms)), reason="lcu_unavailable",
        )

    def bridge_get(self, public_puuid: str) -> tuple[str, str | None] | None:
        response = self._request("snowball_bridge", operation="get", public_puuid=str(public_puuid))
        if str(response.get("status")) != "FOUND":
            return None
        return str(response.get("riot_id") or ""), str(response["lcu_puuid"]) if response.get("lcu_puuid") else None

    def bridge_upsert(self, *, public_puuid: str, riot_id: str, lcu_puuid: str | None, resolve_status: str) -> None:
        self._request(
            "snowball_bridge", operation="upsert", public_puuid=str(public_puuid),
            riot_id=str(riot_id), lcu_puuid=lcu_puuid, resolve_status=str(resolve_status),
        )


def _is_rpc_storage(value: object) -> bool:
    return isinstance(value, SnowballWriterFacade)

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
    # Lets the Classic lane filter by A/B arm in SQL without materializing the
    # arm into a column, which would need a 600k-row backfill and could then
    # drift from the hash.  Only the Classic lane calls it, over ~34k rows a few
    # dozen times an hour.
    _register_sql_functions(con)
    return con


def _register_sql_functions(con: sqlite3.Connection) -> None:
    """Expose lane_arm() to SQL.

    Wrapped rather than passed directly so the module-level name is resolved per
    call, which keeps the function patchable in tests.  Not marked deterministic
    for the same reason.
    """
    con.create_function("lane_arm", 1, lambda puuid: lane_arm(puuid))


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


# Re-entrancy guard for _retry_on_locked.  Workers are single-threaded, so a
# plain module-level counter is enough.  Only the outermost decorated call
# retries: an inner one must not roll back a transaction its caller owns.
_db_retry_depth = 0


def _retry_on_locked(fn):
    """Retry a DB unit that lost the write lock instead of killing the worker.

    Two workers share one ~68GB games.db, so a write occasionally waits out the
    30s busy_timeout in _connect_db and raises "database is locked".  That was
    fatal: the traceback escaped run_snowball and the whole process died, and the
    watchdog only noticed on its next 60s sweep.  Across the crash logs this is
    by far the top cause of worker restarts (412 of 513 recorded exits), spread
    over every write site rather than concentrated in a buggy one -- so the fix
    belongs here, not at any single statement.

    _ensure_schema_with_retry already treats a busy lock as recoverable during
    startup; this extends the same policy to the steady-state crawl path.  Each
    decorated function is a self-contained unit that re-reads whatever state it
    needs, so rolling back and re-running it is safe: a failed attempt committed
    nothing, and the merges these functions perform are monotone.
    """

    @functools.wraps(fn)
    def wrapper(con: sqlite3.Connection, *args, **kwargs):
        global _db_retry_depth
        if _db_retry_depth > 0 or not isinstance(con, sqlite3.Connection):
            return fn(con, *args, **kwargs)

        last_error: sqlite3.OperationalError | None = None
        for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
            _db_retry_depth += 1
            try:
                return fn(con, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_error = exc
            finally:
                _db_retry_depth -= 1

            try:
                con.rollback()
            except sqlite3.Error:
                pass
            if attempt >= _DB_RETRY_ATTEMPTS:
                break
            sleep_sec = min(
                _DB_RETRY_MAX_SLEEP_SEC,
                _DB_RETRY_BASE_SLEEP_SEC * (2 ** (attempt - 1)),
            ) * (0.5 + random.random())
            print(
                f"[snowball] db locked  op={fn.__name__}  "
                f"attempt={attempt}/{_DB_RETRY_ATTEMPTS}  sleep={sleep_sec:.1f}s",
                flush=True,
            )
            time.sleep(sleep_sec)

        assert last_error is not None
        raise last_error

    return wrapper


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


_SCHEMA_BACKFILL_FLAG = "schema_backfill_v1_done"
_CLASSIC_AFFINITY_BACKFILL_FLAG = "classic_affinity_v1_backfill_done"
_CLASSIC_RATE_BOOTSTRAP_FLAG = "classic_rate_v1_bootstrap_done"
_CLASSIC_PRODUCER_FLOOR_FLAG = "classic_producer_floor_v1_backfill_done"

def _schema_backfill_done(con: sqlite3.Connection) -> bool:
    """Return True once the one-time column backfills have completed.

    These backfills scan crawl_queue (~450k) and games (~820k) and hold the
    write lock; re-running them on every worker startup starves game inserts.
    New rows are populated at insert time, so the backfills only need to run
    once after the columns are added — gate them behind this persisted flag.
    """
    try:
        row = con.execute(
            "SELECT 1 FROM crawl_runtime_state WHERE state_key = ?",
            (_SCHEMA_BACKFILL_FLAG,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _mark_schema_backfill_done(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO crawl_runtime_state(state_key, state_value, updated_at) "
        "VALUES (?, '1', ?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value='1', updated_at=excluded.updated_at",
        (_SCHEMA_BACKFILL_FLAG, _utc_now()),
    )


def _classic_affinity_backfill_done(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM crawl_runtime_state WHERE state_key = ?",
        (_CLASSIC_AFFINITY_BACKFILL_FLAG,),
    ).fetchone()
    return row is not None




def _backfill_classic_producer_floor(con: sqlite3.Connection) -> None:
    """Re-admit players the single-visit label had already ejected.

    The floor in _mark_player_done only protects players from here on.  The ones
    already sitting at rank 0 with lifetime Classic behind them -- 2,316 of them
    holding 44.3% of every Classic game ever captured -- would otherwise stay
    invisible to the Classic lane until chance re-drew them from a 600k-row
    general frontier.  One-shot, flagged, and it only ever promotes.
    """
    row = con.execute(
        "SELECT 1 FROM crawl_runtime_state WHERE state_key = ?",
        (_CLASSIC_PRODUCER_FLOOR_FLAG,),
    ).fetchone()
    if row is not None:
        return

    con.execute(
        """
        UPDATE crawl_seen
        SET classic_affinity = 'dormant',
            classic_affinity_rank = 1,
            processed = 0
        WHERE classic_affinity_rank = 0
          AND CAST(COALESCE(
                json_extract(new_games_by_queue_json, '$."4310"'), 0
              ) AS INTEGER) > 0
        """
    )
    con.execute(
        """
        UPDATE crawl_queue
        SET classic_affinity_rank = 1,
            status = CASE WHEN status = 'done' THEN 'pending' ELSE status END,
            eligible_at_ms = CASE WHEN status = 'done' THEN 0 ELSE eligible_at_ms END,
            claimed_by = CASE WHEN status = 'done' THEN NULL ELSE claimed_by END,
            claimed_at_ms = CASE WHEN status = 'done' THEN 0 ELSE claimed_at_ms END
        WHERE classic_affinity_rank = 0
          AND puuid IN (
              SELECT puuid FROM crawl_seen WHERE classic_affinity = 'dormant'
          )
        """
    )
    con.execute(
        "INSERT OR REPLACE INTO crawl_runtime_state(state_key, state_value, updated_at) "
        "VALUES (?, ?, ?)",
        (_CLASSIC_PRODUCER_FLOOR_FLAG, "1", _utc_now()),
    )
    con.commit()

def _bootstrap_classic_rate(con: sqlite3.Connection) -> None:
    """Seed the decayed rate estimator from the lifetime per-queue counters.

    Without this the estimator starts empty and every player falls back to the
    prior, which would take weeks of visits to rediscover what the counters
    already record -- including the 2,316 players the single-visit label had
    written off despite 5,266 Classic games between them.

    den is capped: a player with 40 visits of history would otherwise need weeks
    of decay to forget, and the whole point of the decay is that a player who has
    since quit stops being chased.
    """
    row = con.execute(
        "SELECT 1 FROM crawl_runtime_state WHERE state_key = ?",
        (_CLASSIC_RATE_BOOTSTRAP_FLAG,),
    ).fetchone()
    if row is not None:
        return

    con.execute(
        """
        UPDATE crawl_seen
        SET classic_rate_den = MIN(process_count, ?),
            classic_rate_num = MIN(process_count, ?)
                * (CAST(COALESCE(
                        json_extract(new_games_by_queue_json, '$."4310"'), 0
                   ) AS REAL) / process_count),
            classic_last_crawl_ms = CAST(
                (julianday(last_crawled_at) - 2440587.5) * 86400000 AS INTEGER
            )
        WHERE process_count > 0
          AND last_crawled_at IS NOT NULL
          AND classic_rate_den = 0
        """,
        (_CLASSIC_RATE_BOOTSTRAP_MAX_DEN, _CLASSIC_RATE_BOOTSTRAP_MAX_DEN),
    )
    # Propagate into the frontier copies the claim query reads.  Unvisited rows
    # keep classic_lambda = 0, so they are given the prior here rather than
    # scoring zero and never being claimed.
    con.execute(
        """
        UPDATE crawl_queue
        SET classic_lambda = COALESCE((
                SELECT (s.classic_rate_num + ? * ?) / (s.classic_rate_den + ?)
                FROM crawl_seen s WHERE s.puuid = crawl_queue.puuid
            ), ?),
            classic_last_crawl_ms = COALESCE((
                SELECT s.classic_last_crawl_ms
                FROM crawl_seen s WHERE s.puuid = crawl_queue.puuid
            ), 0),
            classic_span_ms = COALESCE((
                SELECT MAX(?, CAST(s.classic_revisit_interval_ms / ? AS INTEGER))
                FROM crawl_seen s WHERE s.puuid = crawl_queue.puuid
            ), ?)
        WHERE classic_affinity_rank > 0
        """,
        (
            _CLASSIC_RATE_PRIOR_DISCOVERED,
            _CLASSIC_RATE_PRIOR_WEIGHT,
            _CLASSIC_RATE_PRIOR_WEIGHT,
            _CLASSIC_RATE_PRIOR_DISCOVERED,
            _CLASSIC_DEFAULT_REVISIT_MIN_MS,
            _CLASSIC_HISTORY_FILL_FRACTION,
            _CLASSIC_DEFAULT_REVISIT_MIN_MS,
        ),
    )
    con.execute(
        "INSERT OR REPLACE INTO crawl_runtime_state(state_key, state_value, updated_at) "
        "VALUES (?, ?, ?)",
        (_CLASSIC_RATE_BOOTSTRAP_FLAG, "1", _utc_now()),
    )
    con.commit()
def _backfill_classic_affinity(con: sqlite3.Connection) -> None:
    """Tag legacy frontier rows whose discovery match is known Classic.

    This is deliberately one-shot: resolving every discovery game can scan the
    large frontier, while all new rows carry the queue id directly.
    """
    if _classic_affinity_backfill_done(con):
        return

    con.execute(
        """
        UPDATE crawl_seen
        SET discovered_queue_id = 4310,
            classic_affinity = CASE
                WHEN classic_affinity = 'none' THEN 'candidate'
                ELSE classic_affinity
            END,
            classic_affinity_rank = MAX(classic_affinity_rank, 1)
        WHERE discovered_from_game_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM games
              WHERE games.game_id = crawl_seen.discovered_from_game_id
                AND games.queue_id = 4310
          )
        """
    )
    con.execute(
        """
        UPDATE crawl_queue
        SET discovered_queue_id = 4310,
            classic_affinity_rank = MAX(classic_affinity_rank, 1),
            status = CASE WHEN status = 'done' THEN 'pending' ELSE status END,
            eligible_at_ms = CASE WHEN status = 'done' THEN 0 ELSE eligible_at_ms END,
            claimed_by = CASE WHEN status = 'done' THEN NULL ELSE claimed_by END,
            claimed_at_ms = CASE WHEN status = 'done' THEN 0 ELSE claimed_at_ms END
        WHERE puuid IN (
            SELECT puuid FROM crawl_seen WHERE classic_affinity_rank > 0
        )
        """
    )
    con.execute(
        """
        UPDATE crawl_seen
        SET processed = 0
        WHERE classic_affinity_rank > 0
          AND puuid IN (
              SELECT puuid FROM crawl_queue WHERE status = 'pending'
          )
        """
    )
    con.execute(
        "INSERT INTO crawl_runtime_state(state_key, state_value, updated_at) "
        "VALUES (?, '1', ?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value='1', updated_at=excluded.updated_at",
        (_CLASSIC_AFFINITY_BACKFILL_FLAG, _utc_now()),
    )


def _ensure_schema(con: sqlite3.Connection) -> None:
    _register_sql_functions(con)
    con.execute(_CREATE_GAMES_SQL)
    con.execute(_CREATE_CRAWL_SEEN_SQL)
    con.execute(_CREATE_CRAWL_QUEUE_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_SQL)
    con.execute(_CREATE_RIOT_ID_BRIDGE_SQL)
    con.execute(_CREATE_CRAWL_RUNTIME_STATE_SQL)
    con.execute(_CREATE_CRAWL_VISIT_EVENTS_SQL)

    _ensure_column(
        con,
        "games",
        "participants_json",
        "participants_json TEXT",
    )
    _ensure_column(
        con,
        "games",
        "participants_private_json",
        "participants_private_json TEXT",
    )

    # Per-queue yield attribution. `new_games_found` alone cannot answer whether a
    # classifier change bought us 經典 or merely more Mayhem, which is the whole
    # question the history A/B exists to settle.
    _ensure_column(
        con,
        "crawl_seen",
        "new_games_by_queue_json",
        "new_games_by_queue_json TEXT",
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
    _ensure_column(
        con,
        "crawl_seen",
        "seed_family",
        "seed_family TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        con,
        "crawl_queue",
        "seed_family",
        "seed_family TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        con,
        "games",
        "seed_family",
        "seed_family TEXT NOT NULL DEFAULT ''",
    )
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
        # Decayed Classic yield estimate (see _decay_classic_rate).  num/den are
        # in visits; last_crawl_ms mirrors last_crawled_at as epoch ms so the
        # claim query can compute a saturation term without parsing ISO text on
        # every row of a 34k-row scan.
        ("crawl_seen", "classic_rate_num", "classic_rate_num REAL NOT NULL DEFAULT 0"),
        ("crawl_seen", "classic_rate_den", "classic_rate_den REAL NOT NULL DEFAULT 0"),
        (
            "crawl_seen",
            "classic_last_crawl_ms",
            "classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0",
        ),        ("crawl_queue", "discovered_queue_id", "discovered_queue_id INTEGER NOT NULL DEFAULT 0"),
        (
            "crawl_queue",
            "classic_affinity_rank",
            "classic_affinity_rank INTEGER NOT NULL DEFAULT 0",
        ),
        # Denormalized from crawl_seen so the Classic lane's score ordering does
        # not need a per-row PK lookup into a 674k-row table -- measured at 449ms
        # per claim inside BEGIN IMMEDIATE, against 8ms for the due ordering.
        # Written wherever classic_affinity_rank is written.
        ("crawl_queue", "classic_lambda", "classic_lambda REAL NOT NULL DEFAULT 0"),
        (
            "crawl_queue",
            "classic_last_crawl_ms",
            "classic_last_crawl_ms INTEGER NOT NULL DEFAULT 0",
        ),
        ("crawl_queue", "classic_span_ms", "classic_span_ms INTEGER NOT NULL DEFAULT 0"),        ("crawl_visit_events", "claim_lane", "claim_lane TEXT NOT NULL DEFAULT 'general'"),
        (
            "crawl_visit_events",
            "classic_affinity",
            "classic_affinity TEXT NOT NULL DEFAULT 'none'",
        ),
        (
            "crawl_visit_events",
            "classic_revisit_interval_ms",
            "classic_revisit_interval_ms INTEGER NOT NULL DEFAULT 0",
        ),
        ("crawl_visit_events", "lane_arm", "lane_arm TEXT NOT NULL DEFAULT ''"),
        ("crawl_visit_events", "claim_score", "claim_score REAL NOT NULL DEFAULT 0"),    ):
        _ensure_column(con, table, column, definition)

    con.execute(_CREATE_CRAWL_QUEUE_INDEX_SQL)
    con.execute(_CREATE_CRAWL_GAME_CLAIMS_INDEX_SQL)
    con.execute(_CREATE_CRAWL_VISIT_EVENTS_INDEX_SQL)
    con.execute(_CREATE_SOURCE_PRIORITY_INDEX_SQL)
    con.execute(_CREATE_QUEUE_SOURCE_PRIORITY_INDEX_SQL)
    con.execute(_CREATE_CLASSIC_CLAIM_INDEX_SQL)

    # Independent from the older backfill flag: production databases have
    # already set that flag, but still need their Classic discovery rows tagged.
    _backfill_classic_affinity(con)
    _bootstrap_classic_rate(con)
    _backfill_classic_producer_floor(con)
    # The large one-time backfills below scan crawl_queue / games and hold the
    # write lock. Skip them once completed so worker startup stays sub-second
    # and never monopolizes the lock against game inserts.
    if _schema_backfill_done(con):
        con.commit()
        return

    # Backfill seed_family for any rows pre-dating the column.
    # Root sources self-attribute; match-source rows with no traceable parent
    # become 'legacy_match' (excluded from backoff accounting).
    if _table_exists(con, "crawl_seen"):
        con.execute(
            f"""
            UPDATE crawl_seen
            SET seed_family = CASE
                WHEN source IN ({",".join("?" * len(_SEED_FAMILY_ROOTS))}) THEN source
                WHEN source = 'match' THEN ?
                ELSE ?
            END
            WHERE seed_family = ''
            """,
            (*sorted(_SEED_FAMILY_ROOTS), _LEGACY_MATCH_FAMILY, _UNKNOWN_FAMILY),
        )
    if _table_exists(con, "crawl_queue"):
        con.execute(
            f"""
            UPDATE crawl_queue
            SET seed_family = COALESCE(
                (SELECT seed_family FROM crawl_seen
                 WHERE crawl_seen.puuid = crawl_queue.puuid),
                CASE
                    WHEN source IN ({",".join("?" * len(_SEED_FAMILY_ROOTS))}) THEN source
                    WHEN source = 'match' THEN ?
                    ELSE ?
                END
            )
            WHERE seed_family = ''
            """,
            (*sorted(_SEED_FAMILY_ROOTS), _LEGACY_MATCH_FAMILY, _UNKNOWN_FAMILY),
        )
    if _table_exists(con, "games"):
        # Pre-attribution rows: every captured game came from the legacy match
        # subgraph; later inserts populate seed_family from the processing loop.
        con.execute(
            "UPDATE games SET seed_family = ? WHERE seed_family = ''",
            (_LEGACY_MATCH_FAMILY,),
        )

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
    _mark_schema_backfill_done(con)
    con.commit()


def _ensure_schema_with_retry(
    con: sqlite3.Connection,
    *,
    worker_id: str | None = None,
    attempts: int = _SCHEMA_INIT_RETRY_ATTEMPTS,
    sleep_sec: float = _SCHEMA_INIT_RETRY_SLEEP_SEC,
) -> None:
    """Initialize / migrate schema with retries for concurrent worker startup.

    Multiple workers can launch at nearly the same time.  The first worker may
    briefly hold a write lock while running CREATE/ALTER/UPDATE migration work.
    Treat transient SQLITE_BUSY / database-locked failures here as recoverable
    instead of crashing the whole worker before it even starts consuming queue.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            _ensure_schema(con)
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "database table is locked" not in message:
                raise
            last_error = exc
            if attempt >= max(1, attempts):
                break
            print(
                f"[snowball] schema init locked  attempt={attempt}/{max(1, attempts)}  "
                f"sleep={sleep_sec:.1f}s  worker={worker_id or '?'}",
                flush=True,
            )
            time.sleep(max(0.0, sleep_sec))
    if last_error is not None:
        raise last_error


def _get_runtime_state_text(con: sqlite3.Connection, key: str) -> str | None:
    if _is_rpc_storage(con):
        return con.runtime_get(key)
    row = con.execute(
        "SELECT state_value FROM crawl_runtime_state WHERE state_key = ?",
        (str(key),),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _set_runtime_state_text(con: sqlite3.Connection, key: str, value: str) -> None:
    if _is_rpc_storage(con):
        con.runtime_set(key, value)
        return
    con.execute(
        """
        INSERT INTO crawl_runtime_state(state_key, state_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            state_value = excluded.state_value,
            updated_at = excluded.updated_at
        """,
        (str(key), str(value), _utc_now()),
    )
    con.commit()


def _delete_runtime_state(con: sqlite3.Connection, key: str) -> None:
    if _is_rpc_storage(con):
        con.runtime_delete(key)
        return
    con.execute("DELETE FROM crawl_runtime_state WHERE state_key = ?", (str(key),))
    con.commit()


def _family_yield_key(seed_family: str) -> str:
    return f"family_yield:{seed_family}"


def _increment_persisted_family_yield(
    con: sqlite3.Connection, seed_family: str, delta: int
) -> None:
    """Atomically add `delta` to the per-family run yield counter.

    Used to share transitive-yield credit across workers: when one worker
    captures a game for a family, all workers see the bumped counter and
    can avoid spuriously backing the family off based on their own local
    zero-streaks. SQLite serializes concurrent INSERT...ON CONFLICT DO
    UPDATE so the arithmetic is atomic.
    """
    if delta <= 0:
        return
    if _is_rpc_storage(con):
        con.family_increment(seed_family, delta)
        return
    con.execute(
        """
        INSERT INTO crawl_runtime_state(state_key, state_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            state_value = CAST(
                CAST(crawl_runtime_state.state_value AS INTEGER) + ? AS TEXT
            ),
            updated_at = excluded.updated_at
        """,
        (_family_yield_key(seed_family), str(int(delta)), _utc_now(), int(delta)),
    )
    con.commit()


def _read_persisted_family_yield(con: sqlite3.Connection, seed_family: str) -> int:
    if _is_rpc_storage(con):
        return con.family_read(seed_family)
    raw = _get_runtime_state_text(con, _family_yield_key(seed_family))
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


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


def _sync_source_priorities(con: sqlite3.Connection, *, worker_id: str | None = None) -> int:
    """Re-apply _SOURCE_PRIORITY to the frontier tables.  Best-effort.

    This used to issue an unconditional UPDATE per (source, table) -- 18 writes
    inside one transaction on every worker start.  Priorities only actually change
    when _SOURCE_PRIORITY is edited, so in the steady state all 18 matched zero
    rows yet still took (and held) the write lock.  With both frontier tables near
    600k rows and no index on ``source``, that was ~11M rows scanned under lock,
    long enough for a second worker starting alongside to exceed busy_timeout and
    die with "database is locked" before consuming anything.

    Now each (source, table) is probed with an index-backed read first and only
    written when something genuinely differs, so a normal start performs no writes
    at all.  Lock contention is downgraded from fatal to skipped: this is
    maintenance, and a worker that cannot re-stamp priorities right now should get
    on with crawling rather than abort -- the next start retries it.
    """
    updated = 0
    try:
        for source, priority in _SOURCE_PRIORITY.items():
            for table in ("crawl_seen", "crawl_queue"):
                stale = con.execute(
                    f"SELECT 1 FROM {table} WHERE source = ? AND priority != ? LIMIT 1",
                    (source, priority),
                ).fetchone()
                if stale is None:
                    continue
                before = con.total_changes
                con.execute(
                    f"UPDATE {table} SET priority = ? WHERE source = ? AND priority != ?",
                    (priority, source, priority),
                )
                updated += con.total_changes - before
        if updated:
            con.commit()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" not in message:
            raise
        print(
            f"[snowball] source-priority sync skipped (db busy): {exc}  "
            f"worker={worker_id or '?'}",
            flush=True,
        )
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        return 0
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


def _major_minor_patch(version: object) -> str | None:
    """Normalize an LCU build string to a major.minor gameplay patch."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", str(version or "").strip())
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _is_current_patch_game(game: dict, current_patch: str | None) -> bool:
    if current_patch is None:
        return True
    return _major_minor_patch(game.get("gameVersion")) == current_patch


def _extract_current_patch_target_game_ids(
    history: list[dict], target_queues: set[int], current_patch: str | None
) -> list[str]:
    return _extract_target_game_ids(
        [game for game in history if _is_current_patch_game(game, current_patch)],
        target_queues,
    )


def _adaptive_target_game_ids(
    history: list[dict],
    target_queues: set[int],
    *,
    probe_size: int = 4,
    full_history_min_mayhem: int = 3,
    puuid: str | None = None,
    current_patch: str | None = None,
) -> list[str]:
    """Choose a cheap or full expansion based on recent target-queue density.

    LCU exposes a recent mixed-queue window.  We always inspect the first four
    metadata rows. One or two target rows fetch only the probe; three or more
    fetch the full recent window. A completed A/B test showed that counting all
    target queues increased Classic capture by 52.5% for about 13% more detail
    work, so Classic and Jade no longer ride only on Mayhem discovery. When the
    current patch is known, old-patch rows neither make a player active nor get
    expanded; the crawler's scarce detail bandwidth stays on current data.
    """
    normalized_patch = _major_minor_patch(current_patch)
    all_target = _extract_current_patch_target_game_ids(
        history, target_queues, normalized_patch
    )
    probe = history[: max(0, int(probe_size))]
    target_count = sum(
        _queue_id_from_meta(game) in target_queues
        and _is_current_patch_game(game, normalized_patch)
        for game in probe
    )
    if target_count >= max(1, int(full_history_min_mayhem)):
        return all_target
    if target_count >= 1:
        return _extract_current_patch_target_game_ids(
            probe, target_queues, normalized_patch
        )
    return []


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


def _latest_any_match_created_ms(history: list[dict]) -> int:
    latest = 0
    for game in history:
        created_ms = int(game.get("gameCreation") or 0)
        if created_ms > latest:
            latest = created_ms
    return latest


def _count_recent_matches(history: list[dict], cutoff_ms: int) -> int:
    count = 0
    for game in history:
        created_ms = int(game.get("gameCreation") or 0)
        if created_ms >= cutoff_ms:
            count += 1
    return count


def _classic_affinity_profile(
    history: list[dict],
    *,
    discovered_queue_id: int = 0,
    now_ms: int | None = None,
    min_revisit_ms: int = _CLASSIC_DEFAULT_REVISIT_MIN_MS,
    max_revisit_ms: int = _CLASSIC_DEFAULT_REVISIT_MAX_MS,
    lifetime_classic_games: int = 0,
    classic_rate_num: float = 0.0,
) -> ClassicAffinityProfile:
    """Classify Classic affinity and estimate when 20 history rows refill.

    The affinity label only uses Classic (4310) activity.  The interval uses all
    games because every mode consumes one of the LCU's roughly 20 history rows.
    The 0.8 safety factor revisits before the estimated window is completely
    replaced, then the user-selected 10-hour floor prevents wasteful rescans.

    ``lifetime_classic_games`` / ``classic_rate_num`` are the producer floor.  The
    label is otherwise a single-visit snapshot that can only demote: one window
    with no Classic in it sends a player to rank 0 with no memory of what they
    produced before, and promotion back requires being re-drawn by luck from the
    general frontier.  Measured 2026-08-27, that had ejected 2,316 players who
    between them account for 5,266 lifetime Classic games -- 44.3% of everything
    ever captured -- against a rank>=2 pool of 768.  Past production keeps a
    player at dormant/1 so they stay addressable by the Classic lane.
    """
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    lower_ms = max(_CLASSIC_DEFAULT_REVISIT_MIN_MS, int(min_revisit_ms))
    upper_ms = max(lower_ms, int(max_revisit_ms))
    created_times = sorted(
        int(game.get("gameCreation") or 0)
        for game in history
        if int(game.get("gameCreation") or 0) > 0
    )
    classic_times = [
        int(game.get("gameCreation") or 0)
        for game in history
        if _queue_id_from_meta(game) == _CLASSIC_QUEUE_ID
        and int(game.get("gameCreation") or 0) > 0
    ]
    games_24h = sum(created_ms >= current_ms - 24 * 3600_000 for created_ms in classic_times)
    games_recent = len(classic_times)
    last_seen_ms = max(classic_times, default=0)

    if games_24h >= 5:
        label, rank = "heavy", 3
    elif games_24h >= 2:
        label, rank = "regular", 2
    elif games_recent >= 1:
        label, rank = "candidate", 1
    elif int(discovered_queue_id) == _CLASSIC_QUEUE_ID:
        label, rank = "dormant", 1
    elif int(lifetime_classic_games) > 0 or float(classic_rate_num) > 0.0:
        label, rank = "dormant", 1
    else:
        return ClassicAffinityProfile("none", 0, 0, 0, 0, 0)

    interval_ms = upper_ms
    if len(created_times) >= 2:
        span_hours = (created_times[-1] - created_times[0]) / 3600_000
        if span_hours > 0:
            all_game_rate_per_hour = (len(created_times) - 1) / span_hours
            fill_hours = _CLASSIC_HISTORY_TARGET_GAMES / all_game_rate_per_hour
            estimated_ms = int(fill_hours * _CLASSIC_HISTORY_FILL_FRACTION * 3600_000)
            interval_ms = min(max(estimated_ms, lower_ms), upper_ms)

    return ClassicAffinityProfile(
        label=label,
        rank=rank,
        games_24h=games_24h,
        games_recent=games_recent,
        last_seen_ms=last_seen_ms,
        revisit_interval_ms=interval_ms,
    )


def _classic_revisit_eligible_at_ms(
    previous_crawled_at: str | None,
    revisit_interval_ms: int,
    *,
    now_ms: int | None = None,
    min_revisit_ms: int = _CLASSIC_DEFAULT_REVISIT_MIN_MS,
) -> int:
    """Return a due time with a hard minimum measured from the last crawl."""
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    interval_ms = max(
        int(revisit_interval_ms),
        _CLASSIC_DEFAULT_REVISIT_MIN_MS,
        int(min_revisit_ms),
    )
    previous_ms = current_ms
    if previous_crawled_at:
        try:
            previous_dt = datetime.fromisoformat(str(previous_crawled_at))
            if previous_dt.tzinfo is None:
                previous_dt = previous_dt.replace(tzinfo=timezone.utc)
            previous_ms = int(previous_dt.timestamp() * 1000)
        except (TypeError, ValueError):
            previous_ms = current_ms
    return max(current_ms, previous_ms + interval_ms)


def _extract_participant_puuids(detail: dict) -> list[str]:
    puuids: list[str] = []
    for ident in detail.get("participantIdentities") or []:
        player = ident.get("player") or {}
        puuid = player.get("puuid")
        if puuid:
            puuids.append(str(puuid))
    return puuids


@_retry_on_locked
def _claim_game_id(
    con: sqlite3.Connection,
    game_id: str,
    worker_id: str,
    claim_timeout_ms: int,
) -> bool | GameClaim | None:
    if _is_rpc_storage(con):
        return con.claim_game(game_id, claim_timeout_ms=claim_timeout_ms)
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


@_retry_on_locked
def _mark_game_done(con: sqlite3.Connection, game_id: str, claim: GameClaim | None = None) -> None:
    if _is_rpc_storage(con):
        if claim is None:
            raise SnowballWriterError("game claim required")
        con.mark_game_done(claim)
        return
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


@_retry_on_locked
def _release_game_claim(con: sqlite3.Connection, game_id: str, claim: GameClaim | None = None) -> None:
    if _is_rpc_storage(con):
        if claim is None:
            raise SnowballWriterError("game claim required")
        con.release_game(claim)
        return
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


@_retry_on_locked
def _insert_game(con: sqlite3.Connection, record: dict) -> bool:
    if _is_rpc_storage(con):
        raise SnowballWriterError("RPC game insert requires a game claim")
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO games (
            game_id, queue_id, patch, blue_champs, red_champs,
            blue_wins, duration_sec, created_ms, captured_at, participants_json,
            participants_private_json, seed_family
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            json.dumps(record.get("participants_private", []), ensure_ascii=False, separators=(",", ":")),
            str(record.get("seed_family") or _UNKNOWN_FAMILY),
        ),
    )
    inserted = cursor.rowcount > 0
    if inserted:
        update_capture_watermark(
            con,
            queue_id=int(record["queue_id"]),
            captured_at=str(record["captured_at"]),
        )
    con.commit()
    return inserted


def _backfill_participants_json(con: sqlite3.Connection, record: dict) -> bool:
    if _is_rpc_storage(con):
        return False
    row = con.execute(
        "SELECT participants_json, participants_private_json FROM games WHERE game_id = ?",
        (record["game_id"],),
    ).fetchone()
    if row is None:
        return False
    current_json = str(row[0] or "")
    new_json = json.dumps(record.get("participants", []), separators=(",", ":"))
    current_private_json = str(row[1] or "")
    new_private_json = json.dumps(
        record.get("participants_private", []),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    should_update_public = True
    if current_json:
        if _participants_payload_has_postgame_stats(current_json):
            should_update_public = False
        if not _participants_payload_has_postgame_stats(new_json):
            should_update_public = False
    elif not new_json or new_json == "[]":
        should_update_public = False

    should_update_private = bool(new_private_json and new_private_json != "[]") and (
        not current_private_json
        or not _participants_payload_has_postgame_stats(current_private_json)
        or _participants_payload_has_postgame_stats(new_private_json)
    )
    if not should_update_public and not should_update_private:
        return False

    assignments: list[str] = []
    params: list[object] = []
    if should_update_public:
        assignments.append("participants_json = ?")
        params.append(new_json)
    if should_update_private:
        assignments.append("participants_private_json = ?")
        params.append(new_private_json)
    params.append(record["game_id"])

    before = con.total_changes
    con.execute(
        f"UPDATE games SET {', '.join(assignments)} WHERE game_id = ?",
        params,
    )
    con.commit()
    return con.total_changes > before


@_retry_on_locked
def _load_existing_game_ids(con: sqlite3.Connection) -> set[str]:
    if _is_rpc_storage(con):
        return set()
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


@_retry_on_locked
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
    seed_family: str = _UNKNOWN_FAMILY,
    discovered_queue_id: int = 0,
    classic_affinity_rank: int = 0,
) -> bool:
    """Insert or refresh a queue row. Returns True if it became pending now.

    seed_family is set on insert and only upgraded over an unresolved value
    ('', _UNKNOWN_FAMILY, _LEGACY_MATCH_FAMILY) so the first known root family
    sticks across re-discovery.
    """
    if _is_rpc_storage(con):
        return con.upsert_queue(
            puuid=str(puuid), depth=int(depth), source=str(source), priority=int(priority),
            discovered_from_game_id=discovered_from_game_id,
            discovered_match_created_ms=int(discovered_match_created_ms), requeue=bool(requeue),
            eligible_at_ms=max(0, int(eligible_at_ms)), seed_family=str(seed_family or _UNKNOWN_FAMILY),
            discovered_queue_id=int(discovered_queue_id), classic_affinity_rank=int(classic_affinity_rank),
        )
    now = _utc_now()
    row = con.execute(
        """
        SELECT status, priority, depth, discovered_match_created_ms, seed_family,
               discovered_queue_id, classic_affinity_rank
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
                claimed_by, claimed_at_ms, eligible_at_ms, status, seed_family,
                discovered_queue_id, classic_affinity_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 'pending', ?, ?, ?)
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
                seed_family,
                int(discovered_queue_id),
                max(0, int(classic_affinity_rank)),
            ),
        )
        con.commit()
        return True

    (
        queue_status,
        queue_priority,
        queue_depth,
        queue_match_ms,
        queue_seed_family,
        queue_discovered_queue_id,
        queue_classic_rank,
    ) = row
    effective_family = _resolve_seed_family_update(str(queue_seed_family or ""), seed_family)
    effective_discovered_queue_id = int(queue_discovered_queue_id or 0)
    if int(discovered_queue_id) == _CLASSIC_QUEUE_ID:
        effective_discovered_queue_id = _CLASSIC_QUEUE_ID
    elif effective_discovered_queue_id == 0:
        effective_discovered_queue_id = int(discovered_queue_id)
    effective_classic_rank = max(
        int(queue_classic_rank or 0), max(0, int(classic_affinity_rank))
    )
    became_pending = False
    if str(queue_status) != "pending" and requeue:
        con.execute(
            """
            UPDATE crawl_queue
            SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                discovered_match_created_ms = ?, updated_at = ?, eligible_at_ms = ?,
                claimed_by = NULL, claimed_at_ms = 0, status = 'pending',
                seed_family = ?, discovered_queue_id = ?, classic_affinity_rank = ?
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
                effective_family,
                effective_discovered_queue_id,
                effective_classic_rank,
                puuid,
            ),
        )
        became_pending = True
    elif str(queue_status) in ("pending", "in_progress"):
        should_update = (
            discovered_match_created_ms > int(queue_match_ms)
            or priority < int(queue_priority)
            or depth < int(queue_depth)
            or effective_family != str(queue_seed_family or "")
            or effective_discovered_queue_id != int(queue_discovered_queue_id or 0)
            or effective_classic_rank != int(queue_classic_rank or 0)
        )
        if should_update:
            con.execute(
                f"""
                UPDATE crawl_queue
                SET depth = ?, source = ?, priority = ?, discovered_from_game_id = ?,
                    discovered_match_created_ms = ?, updated_at = ?, seed_family = ?
                    , discovered_queue_id = ?, classic_affinity_rank = ?
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
                    effective_family,
                    effective_discovered_queue_id,
                    effective_classic_rank,
                    puuid,
                ),
            )
    con.commit()
    return became_pending


def _resolve_seed_family_update(existing: str, incoming: str) -> str:
    """Decide which seed_family value to keep: first known root wins.

    Unresolved values ('', _UNKNOWN_FAMILY, _LEGACY_MATCH_FAMILY) can be
    overwritten by any incoming value. Otherwise existing wins to avoid
    re-attribution ping-pong when the same puuid is re-discovered through a
    different root.
    """
    incoming = incoming or ""
    if not existing or existing in (_UNKNOWN_FAMILY, _LEGACY_MATCH_FAMILY):
        return incoming or existing or _UNKNOWN_FAMILY
    if existing == "manual_riot_id" and incoming.startswith("opgg_"):
        return incoming
    if not incoming or incoming in (_UNKNOWN_FAMILY, _LEGACY_MATCH_FAMILY):
        return existing
    return existing


def _derive_seed_family(source: str, explicit: str | None) -> str:
    """Pick the seed_family to write. Callers may pass an explicit family
    (e.g., propagated from a parent puuid for match-source children); for
    root sources we default to the source itself.
    """
    if explicit:
        return explicit
    if source in _SEED_FAMILY_ROOTS:
        return source
    return _UNKNOWN_FAMILY


@_retry_on_locked
def _enqueue_player(
    con: sqlite3.Connection,
    puuid: str,
    depth: int,
    source: str,
    discovered_from_game_id: str | None = None,
    discovered_match_created_ms: int = 0,
    requeue_cooldown_ms: int = 0,
    initial_delay_ms: int = 0,
    seed_family: str | None = None,
    discovered_queue_id: int = 0,
    classic_revisit_min_ms: int = _CLASSIC_DEFAULT_REVISIT_MIN_MS,
) -> str:
    """Add puuid to seen-set and queue when needed.

    seed_family records the original *root* entry point (manual_riot_id, apex,
    friend, ...) for transitive yield attribution. Callers must pass it when
    enqueueing a match-source child — otherwise children would default to
    _UNKNOWN_FAMILY and break backoff accounting.

    Returns:
      - 'new' if the puuid was unseen and newly queued
      - 'requeued' if it had been processed before and a newer match reactivated it
      - 'updated' if metadata / priority changed but it was already queued or in progress
      - 'noop' otherwise
    """
    if not puuid:
        return "noop"

    if _is_rpc_storage(con):
        derived_family = _derive_seed_family(source, seed_family)
        return con.enqueue_player(
            puuid=str(puuid), depth=int(depth), source=str(source),
            priority=int(_SOURCE_PRIORITY.get(source, 99)),
            discovered_from_game_id=discovered_from_game_id,
            discovered_match_created_ms=int(discovered_match_created_ms),
            requeue_cooldown_ms=max(0, int(requeue_cooldown_ms)),
            initial_delay_ms=max(0, int(initial_delay_ms)), seed_family=derived_family,
            discovered_queue_id=int(discovered_queue_id), classic_affinity_rank=int(int(discovered_queue_id) == _CLASSIC_QUEUE_ID),
        )

    now = _utc_now()
    priority = _SOURCE_PRIORITY.get(source, 99)
    derived_family = _derive_seed_family(source, seed_family)
    row = con.execute(
        """
        SELECT source, priority, min_depth, discovered_from_game_id,
               latest_seen_match_created_ms, last_crawled_match_created_ms,
               processed, seed_family, process_count, new_games_found,
               first_seen_at, last_crawled_at, discovered_queue_id,
               classic_affinity, classic_affinity_rank,
               classic_revisit_interval_ms
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()

    if row is None:
        initial_classic_rank = int(int(discovered_queue_id) == _CLASSIC_QUEUE_ID)
        initial_classic_affinity = "candidate" if initial_classic_rank else "none"
        con.execute(
            """
            INSERT INTO crawl_seen (
                puuid, source, priority, min_depth, discovered_from_game_id,
                first_seen_at, latest_seen_match_created_ms,
                last_crawled_match_created_ms, processed, seed_family,
                discovered_queue_id, classic_affinity, classic_affinity_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
            """,
            (
                puuid,
                source,
                priority,
                depth,
                discovered_from_game_id,
                now,
                discovered_match_created_ms,
                derived_family,
                int(discovered_queue_id),
                initial_classic_affinity,
                initial_classic_rank,
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
            seed_family=derived_family,
            discovered_queue_id=int(discovered_queue_id),
            classic_affinity_rank=initial_classic_rank,
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
        old_seed_family,
        process_count,
        new_games_found,
        first_seen_at,
        last_crawled_at,
        old_discovered_queue_id,
        old_classic_affinity,
        old_classic_rank,
        old_classic_revisit_interval_ms,
    ) = row
    best_source, best_priority, best_depth = _pick_best_metadata(
        str(old_source),
        int(old_priority),
        int(old_depth),
        source,
        priority,
        depth,
    )
    effective_family = _resolve_seed_family_update(str(old_seed_family or ""), derived_family)
    latest_match_ms = max(int(old_latest_match_ms), int(discovered_match_created_ms))
    effective_discovered_queue_id = int(old_discovered_queue_id or 0)
    if int(discovered_queue_id) == _CLASSIC_QUEUE_ID:
        effective_discovered_queue_id = _CLASSIC_QUEUE_ID
    elif effective_discovered_queue_id == 0:
        effective_discovered_queue_id = int(discovered_queue_id)
    effective_classic_rank = max(
        int(old_classic_rank or 0), int(effective_discovered_queue_id == _CLASSIC_QUEUE_ID)
    )
    effective_classic_affinity = str(old_classic_affinity or "none")
    if effective_classic_rank and effective_classic_affinity == "none":
        effective_classic_affinity = "candidate"
    best_game_id = old_discovered_game_id
    if discovered_match_created_ms >= int(old_latest_match_ms) and discovered_from_game_id:
        best_game_id = discovered_from_game_id

    con.execute(
        """
        UPDATE crawl_seen
        SET source = ?, priority = ?, min_depth = ?, discovered_from_game_id = ?,
            latest_seen_match_created_ms = ?, seed_family = ?,
            discovered_queue_id = ?, classic_affinity = ?,
            classic_affinity_rank = ?
        WHERE puuid = ?
        """,
        (
            best_source,
            best_priority,
            best_depth,
            best_game_id,
            latest_match_ms,
            effective_family,
            effective_discovered_queue_id,
            effective_classic_affinity,
            effective_classic_rank,
            puuid,
        ),
    )
    con.commit()

    should_requeue = int(processed) == 1 and (
        int(discovered_match_created_ms) > int(last_crawled_match_ms)
        or (source == "manual_riot_id" and int(last_crawled_match_ms) == 0)
    )
    eligible_at_ms = 0
    if should_requeue:
        if effective_classic_rank > 0:
            classic_interval_ms = max(
                int(old_classic_revisit_interval_ms or 0),
                max(0, int(classic_revisit_min_ms)),
            )
            eligible_at_ms = _classic_revisit_eligible_at_ms(
                str(last_crawled_at) if last_crawled_at else None,
                classic_interval_ms,
                min_revisit_ms=classic_revisit_min_ms,
            )
            eligible_at_ms = max(
                eligible_at_ms,
                _now_ms() + max(0, int(initial_delay_ms)),
            )
        elif revisit_arm(puuid) == "treatment":
            effective_cooldown_ms = _treatment_cooldown_ms(
                int(new_games_found or 0), first_seen_at, time.time()
            )
            eligible_at_ms = _now_ms() + max(
                int(effective_cooldown_ms), int(initial_delay_ms)
            )
        else:
            effective_cooldown_ms = _requeue_cooldown_for(
                int(new_games_found or 0),
                int(process_count or 0),
                max(0, int(requeue_cooldown_ms)),
            )
            eligible_at_ms = _now_ms() + max(
                int(effective_cooldown_ms), int(initial_delay_ms)
            )
    became_pending = _upsert_queue_row(
        con,
        puuid,
        best_depth,
        best_source,
        best_priority,
        best_game_id,
        latest_match_ms,
        requeue=should_requeue,
        eligible_at_ms=eligible_at_ms,
        seed_family=effective_family,
        discovered_queue_id=effective_discovered_queue_id,
        classic_affinity_rank=effective_classic_rank,
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


@_retry_on_locked
def _requeue_stale_claims(con: sqlite3.Connection, claim_timeout_ms: int) -> int:
    if _is_rpc_storage(con):
        return con.requeue_stale(claim_timeout_ms)
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


_CLASSIC_LANE_SELECT = """
    SELECT q.queue_idx, q.puuid, q.depth, q.source,
           q.discovered_match_created_ms, q.seed_family, q.discovered_queue_id
    FROM crawl_queue q
    WHERE q.status = 'pending'
      AND q.eligible_at_ms <= :now_ms
      AND q.classic_affinity_rank > 0
      AND lane_arm(q.puuid) = :arm
"""

# Expected Classic games from visiting this player now: the shrunk decayed rate
# times the fraction of their ~20-row window that has refilled since the last
# visit.  A never-visited row has last_crawl_ms = 0, so it saturates at 1.0 and
# scores its prior -- which is the point, since 60% of the lane has never been
# visited.
_CLASSIC_LANE_SCORE_EXPR = """
    (CASE WHEN q.classic_lambda > 0 THEN q.classic_lambda ELSE :prior END)
    * MIN(1.0,
          MAX(0, :now_ms - q.classic_last_crawl_ms)
          / MAX(CAST(:min_span_ms AS REAL), CAST(q.classic_span_ms AS REAL)))
"""

_CLASSIC_LANE_SQL = {
    "score": _CLASSIC_LANE_SELECT + f"""
    ORDER BY ({_CLASSIC_LANE_SCORE_EXPR}) DESC,
             q.eligible_at_ms ASC,
             q.queue_idx ASC
    LIMIT 1
    """,
    # The shipped ordering, kept verbatim as the control arm.
    "due": _CLASSIC_LANE_SELECT + """
    ORDER BY q.eligible_at_ms ASC,
             q.classic_affinity_rank DESC,
             q.discovered_match_created_ms DESC,
             q.priority ASC,
             q.depth ASC,
             q.queue_idx ASC
    LIMIT 1
    """,
}



def _classic_lane_arm_for_slot(claim_number: int, percent: int) -> str:
    """Alternate the two lane orderings across reserved slots.

    Must count SLOTS, not claims: _classic_claim_slot fires only on multiples of
    100/percent, which are all even at the default 10%, so alternating on
    claim_number parity would have silently run one arm 100% of the time and
    left the experiment with no control.
    """
    normalized = min(100, max(0, int(percent)))
    if normalized <= 0:
        return "score"
    slot_index = int(claim_number) * normalized // 100
    return "score" if slot_index % 2 == 0 else "due"

def _classic_lane_params(now_ms: int, arm: str) -> dict[str, object]:
    return {
        "now_ms": int(now_ms),
        "arm": arm,
        "min_span_ms": float(_CLASSIC_DEFAULT_REVISIT_MIN_MS),
        # Rows enqueued but never visited carry classic_lambda = 0.  Shrinkage
        # means a visited player's estimate is never exactly 0, so 0 uniquely
        # identifies "no observation yet" and gets the discovery prior -- without
        # which 60% of the lane would score zero and never be claimed.
        "prior": _CLASSIC_RATE_PRIOR_DISCOVERED,
    }


@_retry_on_locked
def _claim_next_player(
    con: sqlite3.Connection,
    worker_id: str,
    claim_timeout_ms: int,
    classic_claim_percent: int = _CLASSIC_DEFAULT_CLAIM_PERCENT,
) -> tuple[str, int, str, int, str, int, str] | None:
    """Atomically claim one pending queue item for this worker.

    Returns (puuid, depth, source, claimed_match_ms, seed_family,
    discovered_queue_id, claim_lane).
    """
    if _is_rpc_storage(con):
        return con.claim_next(
            worker_id=str(worker_id), claim_timeout_ms=int(claim_timeout_ms),
            classic_claim_percent=int(classic_claim_percent),
        )
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
    claim_number = _claim_counter()
    row = None
    claim_lane = "general"

    # Reserve a dispersed share of claims for due Classic-tagged players.  An
    # empty Classic lane falls through and returns its capacity to the main
    # frontier.  Alternating slots between the two lane_arm orderings keeps the
    # A/B halves at equal claim budget; see lane_arm for what is being compared.
    if _classic_claim_slot(claim_number, classic_claim_percent):
        arm = _classic_lane_arm_for_slot(claim_number, classic_claim_percent)
        row = con.execute(
            _CLASSIC_LANE_SQL[arm], _classic_lane_params(now_ms, arm)
        ).fetchone()
        if row is not None:
            claim_lane = f"classic_{arm}"
    # Reserve a share of claims for players never crawled before.
    #
    # Ordering by discovered_match_created_ms DESC means an active player jumps to
    # the front every time they turn up in a new game, so the crawler re-milled a
    # hot set of ~15k while 250k never-crawled players sat behind them: 91% of all
    # claims were revisits, first visits ran at ~44/hour, and draining the backlog
    # at that rate would have taken ~237 days. It is starvation, not a small player
    # pool.
    #
    # Revisits are still worth doing -- they are how fresh games arrive -- so this
    # reserves rather than reorders. Unvisited players are also worth more per
    # scan: 6.21 games on a first visit against ~1.5 on a revisit, because a
    # player's recent games are largely already captured via their teammates.
    take_unvisited = (claim_number % _UNVISITED_CLAIM_PERIOD) == 0
    if row is None and take_unvisited:
        row = con.execute(
            """
            SELECT q.queue_idx, q.puuid, q.depth, q.source,
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
                     q.priority ASC,
                     q.depth ASC,
                     q.queue_idx ASC
            LIMIT 1
            """
        , (now_ms,)).fetchone()
        if row is not None:
            claim_lane = "unvisited"
    if row is None:
        row = con.execute(
            """
            SELECT queue_idx, puuid, depth, source, discovered_match_created_ms,
                   seed_family, discovered_queue_id
            FROM crawl_queue
            WHERE status = 'pending'
              AND eligible_at_ms <= ?
              AND classic_affinity_rank = 0
            ORDER BY discovered_match_created_ms DESC,
                     priority ASC,
                     depth ASC,
                     updated_at ASC,
                     queue_idx ASC
            LIMIT 1
            """
        , (now_ms,)).fetchone()
    if row is None:
        # General frontier is empty, so this is spare capacity rather than the
        # reserved budget.  It stays on the same alternating arms; tagging it
        # separately keeps it out of the A/B comparison, which is only valid over
        # the reserved slots where both arms get equal capacity.
        arm = _classic_lane_arm_for_slot(claim_number, classic_claim_percent)
        row = con.execute(
            _CLASSIC_LANE_SQL[arm], _classic_lane_params(now_ms, arm)
        ).fetchone()
        if row is not None:
            claim_lane = f"classic_fallback_{arm}"
    if row is None:
        con.commit()
        return None

    queue_idx, puuid, depth, source, claimed_match_ms, seed_family, discovered_queue_id = row
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
    return (
        str(puuid),
        int(depth),
        str(source),
        int(claimed_match_ms),
        str(seed_family or "") or _UNKNOWN_FAMILY,
        int(discovered_queue_id or 0),
        claim_lane,
    )


@_retry_on_locked
def _pending_player_count(con: sqlite3.Connection) -> int:
    if _is_rpc_storage(con):
        return con.pending_count()
    return int(
        con.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        ).fetchone()[0]
    )


def _open_queue_source_count(con: sqlite3.Connection, source: str) -> int:
    if _is_rpc_storage(con):
        return con.source_count(source)
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
    if _is_rpc_storage(con):
        return con.next_wait_ms()
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


# Visits needed before a player's observed yield is trusted enough to throttle on.
# Below this the crawler has no real evidence and keeps the flat base cooldown.
# Every Nth claim is reserved for a never-crawled player. 1-in-3 lifts first
# visits from ~9% of claims to ~33% without starving revisits in turn; at the
# current ~5k games/hour that drains the 250k backlog in weeks rather than the
# ~237 days the unreserved ordering implied. Falls through to the normal query
# when no unvisited player is eligible, so an empty backlog costs nothing.
_UNVISITED_CLAIM_PERIOD = 3
_CLAIM_COUNT = itertools.count()


def _claim_counter() -> int:
    return next(_CLAIM_COUNT)


def _classic_claim_slot(claim_number: int, percent: int) -> bool:
    """Disperse exactly ``percent`` reserved slots over each 100 claims."""
    normalized = min(100, max(0, int(percent)))
    if normalized == 0:
        return False
    return (int(claim_number) * normalized) % 100 < normalized


_YIELD_BACKOFF_MIN_VISITS = 3
# Yield (new games per visit) above which a player is treated as productive and
# left completely alone.  Measured over 1.63M visits: players at >=0.5 account for
# 83% of all visits and essentially all output, and revisits overall are nearly as
# productive as first visits (1.304 vs 1.388 games/visit) -- so a blanket revisit
# reduction would cost real data.  The waste is concentrated far below this line:
# the <0.01 band burns 122k visits (7.5% of everything) to produce 20 games total.
_YIELD_PRODUCTIVE = 0.5
_YIELD_MARGINAL = 0.1
_YIELD_BACKOFF_MARGINAL = 20    # base 45s -> 15min
_YIELD_BACKOFF_DEAD = 240       # base 45s -> 3h


# --- Revisit-interval A/B -------------------------------------------------
# Arm is a stable hash of the puuid, NOT a per-worker setting: both workers share
# one crawl_queue, and the cooldown is written to a shared eligible_at_ms, so a
# per-worker split would have each arm overwriting the other's decisions. Hashing
# the player keeps the treatment consistent no matter who claims them.
#
# Treatment tests the interval implied by measurement rather than the guessed
# tiers. Sampling 60k games / 209k players: a player needs a median 178h (7.4
# days) to actually play 20 games -- P10 71h, P90 580h -- while control revisits
# after 45s to 3h. That is three orders of magnitude early, which is why a revisit
# returns ~1.5 games instead of anything near the 20 the LCU could hold.
_REVISIT_AB_ENABLED = True
_REVISIT_TREATMENT_TARGET_GAMES = 15.0   # under 20 so nothing rolls off unseen
_REVISIT_TREATMENT_MIN_MS = 24 * 3600_000
_REVISIT_TREATMENT_MAX_MS = 21 * 24 * 3600_000


_QUEUE_LOG_LABEL = {2400: "Mayhem", 450: "ARAM", 2450: "KiwiCl", 4310: "Jade"}

_HISTORY_AB_ENABLED = True


def history_arm(puuid: str) -> str:
    """Legacy A/B label retained for historical metrics.

    control  Mayhem-only classifier. A player with no Mayhem in the probe window
             is skipped entirely -- the behaviour that made 經典 invisible.
    probe    Any target queue makes the player visible, but qualifying on a
             non-Mayhem queue only ever expands the probe window (~4 games).
             Cheap visibility: 經典 players stop being skipped without any one of
             them costing a full 20-game expansion.
    full     Any target queue counts for everything, including the >=3 rule that
             triggers a full 20-game expansion.

    probe exists because the interesting question is not merely whether including
    經典 helps, but whether it is worth a full expansion per player. Separating
    the two isolates the cost from the benefit instead of confounding them.

    Salted differently from revisit_arm on purpose: reusing that split would place
    the same players in both concurrent treatments and make the experiments
    impossible to attribute separately.
    """
    if not _HISTORY_AB_ENABLED:
        return "full"
    digest = hashlib.sha1(b"history-ab|" + str(puuid).encode("utf-8", "replace")).digest()
    return ("control", "probe", "full")[digest[0] % 3]


_LANE_AB_ENABLED = True


def lane_arm(puuid: str) -> str:
    """Stable 50/50 split for the Classic lane's ordering.

    due    the shipped ordering: most-overdue first, affinity rank as a tie-break
           that per-millisecond timestamps never actually reach.
    score  order by expected Classic yield (decayed rate x window saturation).

    Split rather than switched because the offline comparison can only argue one
    direction honestly.  Ranking players by lifetime yield and then scoring the
    result by lifetime yield is circular, so the 34x it reported is an upper
    bound; what is not circular is that the shipped ordering's top 1,000 held
    zero rank>=2 players and zero Classic history at all.  The real number has to
    come from forward measurement, which is what this arm is for.

    Salted apart from revisit_arm and history_arm so a player's lane arm is
    independent of the treatments they are already in.
    """
    if not _LANE_AB_ENABLED:
        return "score"
    digest = hashlib.sha1(b"lane-ab|" + str(puuid).encode("utf-8", "replace")).digest()
    return "score" if digest[0] & 1 else "due"


def revisit_arm(puuid: str) -> str:
    """Stable 50/50 split. 'control' keeps the shipped tiers, 'treatment' uses
    the measured accumulation rate."""
    if not _REVISIT_AB_ENABLED:
        return "control"
    digest = hashlib.sha1(str(puuid).encode("utf-8", "replace")).digest()
    return "treatment" if digest[0] & 1 else "control"


def _treatment_cooldown_ms(
    new_games_found_total: int, first_seen_at: str | None, now_ts: float
) -> int:
    """Wait roughly until the player should have accumulated the target games.

    Rate comes from what this player has actually produced for us over their
    observed lifetime, so it adapts per player instead of assuming one interval
    fits a population whose 20-game span runs from 3 days to 24.
    """
    hours = 0.0
    if first_seen_at:
        try:
            seen = datetime.fromisoformat(first_seen_at)
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            hours = max(0.0, (now_ts - seen.timestamp()) / 3600.0)
        except (ValueError, TypeError):
            hours = 0.0
    if hours < 24 or new_games_found_total <= 0:
        # Not enough observation to estimate a rate; use the low end rather than
        # guessing high and stranding a possibly-active player for weeks.
        return _REVISIT_TREATMENT_MIN_MS
    rate_per_hour = new_games_found_total / hours
    wait_hours = _REVISIT_TREATMENT_TARGET_GAMES / max(rate_per_hour, 1e-6)
    return int(min(max(wait_hours * 3600_000, _REVISIT_TREATMENT_MIN_MS),
                   _REVISIT_TREATMENT_MAX_MS))


def _requeue_cooldown_for(
    new_games_found_total: int, process_count: int, base_cooldown_ms: int
) -> int:
    """Scale a player's requeue cooldown by how much they have actually yielded.

    The frontier re-queues a player whenever they show up in a game newer than the
    one we last crawled them at, which for an active player can happen constantly;
    with a flat 45s cooldown that produced individuals crawled 1,200+ times for
    almost nothing. Productive players keep the base cooldown -- they are where the
    data comes from -- while players with a long, well-evidenced record of yielding
    nothing back off hard instead of being rescanned every minute forever.
    """
    if process_count < _YIELD_BACKOFF_MIN_VISITS:
        return base_cooldown_ms
    rate = new_games_found_total / float(process_count)
    if rate >= _YIELD_PRODUCTIVE:
        return base_cooldown_ms
    if rate >= _YIELD_MARGINAL:
        return base_cooldown_ms * _YIELD_BACKOFF_MARGINAL
    return base_cooldown_ms * _YIELD_BACKOFF_DEAD


def _merge_queue_counts(stored_json: str | None, added: dict[int, int] | None) -> str | None:
    """Fold this visit's per-queue saves into the player's cumulative counter.

    Merged in Python rather than SQL because the row is already read here; a
    corrupt or missing blob restarts from the current visit instead of throwing,
    since losing one player's attribution history must never stall a crawl.
    """
    if not added:
        return stored_json
    totals: dict[str, int] = {}
    if stored_json:
        try:
            parsed = json.loads(stored_json)
            if isinstance(parsed, dict):
                totals = {str(k): int(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            totals = {}
    for queue_id, count in added.items():
        totals[str(queue_id)] = totals.get(str(queue_id), 0) + int(count)
    return json.dumps(totals, sort_keys=True, separators=(",", ":"))


@_retry_on_locked
def _mark_player_done(
    con: sqlite3.Connection,
    puuid: str,
    new_games_found: int,
    claimed_match_created_ms: int,
    observed_match_created_ms: int,
    requeue_cooldown_ms: int,
    new_games_by_queue: dict[int, int] | None = None,
    *,
    source: str = "unknown",
    seed_family: str = _UNKNOWN_FAMILY,
    worker_id: str | None = None,
    current_patch: str | None = None,
    history_game_count: int = 0,
    target_game_count: int = 0,
    claim_lane: str = "general",
    classic_profile: ClassicAffinityProfile | None = None,
    classic_revisit_min_ms: int = _CLASSIC_DEFAULT_REVISIT_MIN_MS,
    claim: PlayerClaim | None = None,
) -> bool:
    """Finalize a claimed player.

    Returns True if the player was re-queued immediately due to a newer discovery
    arriving while this worker was processing it.
    """
    if _is_rpc_storage(con):
        if claim is None:
            raise SnowballWriterError("player claim required")
        profile = classic_profile or ClassicAffinityProfile("none", 0, 0, 0, 0, 0)
        return con.finalize_player(
            claim,
            new_games_found=max(0, int(new_games_found)),
            claimed_match_created_ms=int(claimed_match_created_ms),
            observed_match_created_ms=int(observed_match_created_ms),
            requeue_cooldown_ms=max(0, int(requeue_cooldown_ms)),
            new_games_by_queue={str(k): int(v) for k, v in (new_games_by_queue or {}).items()},
            source=str(source), seed_family=str(seed_family), worker_id=worker_id,
            current_patch=current_patch, history_game_count=max(0, int(history_game_count)),
            target_game_count=max(0, int(target_game_count)), claim_lane=str(claim_lane),
            classic_affinity=str(profile.label), classic_rank=int(profile.rank),
            classic_games_24h=int(profile.games_24h), classic_games_recent=int(profile.games_recent),
            classic_last_seen_ms=int(profile.last_seen_ms),
            classic_revisit_interval_ms=max(0, int(profile.revisit_interval_ms)),
            classic_revisit_min_ms=max(0, int(classic_revisit_min_ms)),
        )
    now = _utc_now()
    row = con.execute(
        """
        SELECT latest_seen_match_created_ms, last_crawled_match_created_ms,
               process_count, new_games_found, first_seen_at,
               new_games_by_queue_json, last_crawled_at,
               classic_rate_num, classic_rate_den, classic_last_crawl_ms,
               discovered_queue_id
        FROM crawl_seen
        WHERE puuid = ?
        """,
        (puuid,),
    ).fetchone()
    queue_counts_json = _merge_queue_counts(row[5] if row else None, new_games_by_queue)
    latest_seen_match_ms = int(row[0]) if row else 0
    last_crawled_match_ms = int(row[1]) if row else 0
    # Counts BEFORE this visit is folded in; add the current result so the
    # backoff decision reflects the visit that just happened.
    prior_process_count = int(row[2] or 0) if row else 0
    prior_new_games = int(row[3] or 0) if row else 0
    prior_first_seen_at = row[4] if row else None
    previous_crawled_at = str(row[6]) if row and row[6] else None
    profile = classic_profile or ClassicAffinityProfile("none", 0, 0, 0, 0, 0)

    # Fold this visit into the decayed Classic yield estimate.  Decay runs on the
    # gap since the previous visit, so a player revisited every 10 hours and one
    # revisited weekly are weighted on the same clock rather than by visit count.
    now_epoch_ms = _now_ms()
    prior_rate_num = float(row[7] or 0.0) if row else 0.0
    prior_rate_den = float(row[8] or 0.0) if row else 0.0
    prior_last_crawl_ms = int(row[9] or 0) if row else 0
    classic_discovered = bool(row and int(row[10] or 0) == _CLASSIC_QUEUE_ID)
    rate_num, rate_den = _decay_classic_rate(
        prior_rate_num,
        prior_rate_den,
        now_epoch_ms - prior_last_crawl_ms if prior_last_crawl_ms else 0,
        int((new_games_by_queue or {}).get(_CLASSIC_QUEUE_ID, 0)),
    )
    # Producer floor.  The caller classified from one 20-row window; a player who
    # has produced Classic for us before must not be ejected to rank 0 just
    # because this window happened to be quiet, because nothing promotes them
    # back except being re-drawn from the general frontier by chance.
    if profile.rank == 0 and (
        _lifetime_classic_games(queue_counts_json) > 0 or rate_num > 0.0
    ):
        profile = ClassicAffinityProfile(
            "dormant",
            1,
            profile.games_24h,
            profile.games_recent,
            profile.last_seen_ms,
            max(int(profile.revisit_interval_ms), _CLASSIC_DEFAULT_REVISIT_MIN_MS),
        )
    revisit_interval_ms: int | None = None
    if previous_crawled_at:
        try:
            previous_dt = datetime.fromisoformat(previous_crawled_at)
            revisit_interval_ms = max(
                0,
                int(
                    (datetime.fromisoformat(now) - previous_dt).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            revisit_interval_ms = None
    con.execute(
        """
        INSERT INTO crawl_visit_events (
            puuid, revisit_arm, is_revisit, visited_at, previous_crawled_at,
            revisit_interval_ms, process_number, source, seed_family,
            worker_id, current_patch, history_game_count, target_game_count,
            new_games_found, new_games_by_queue_json, claim_lane,
            classic_affinity, classic_revisit_interval_ms, lane_arm, claim_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)        """,
        (
            puuid,
            revisit_arm(puuid),
            int(prior_process_count > 0 and previous_crawled_at is not None),
            now,
            previous_crawled_at,
            revisit_interval_ms,
            prior_process_count + 1,
            source,
            seed_family,
            worker_id,
            current_patch,
            max(0, int(history_game_count)),
            max(0, int(target_game_count)),
            max(0, int(new_games_found)),
            json.dumps(
                {str(k): int(v) for k, v in (new_games_by_queue or {}).items()},
                sort_keys=True,
                separators=(",", ":"),
            ),
            str(claim_lane or "general"),
            profile.label,
            max(0, int(profile.revisit_interval_ms)),
            lane_arm(puuid),
            # The rate estimate this player carried INTO the visit, logged for
            # both arms so the two pools can be compared on the same axis.  The
            # saturation half of the score is recoverable from
            # previous_crawled_at in this same row, so only the rate is stored.
            _classic_lambda(
                prior_rate_num, prior_rate_den, classic_discovered=classic_discovered
            ),
        ),
    )
    crawled_match_ms = max(
        last_crawled_match_ms,
        int(claimed_match_created_ms),
        int(observed_match_created_ms),
    )
    needs_requeue = latest_seen_match_ms > crawled_match_ms

    if needs_requeue or profile.rank > 0:
        total_games = prior_new_games + int(new_games_found)
        if profile.rank > 0:
            effective_cooldown_ms = max(
                int(profile.revisit_interval_ms),
                max(0, int(classic_revisit_min_ms)),
            )
            eligible_at_ms = _classic_revisit_eligible_at_ms(
                now,
                effective_cooldown_ms,
                min_revisit_ms=classic_revisit_min_ms,
            )
        elif revisit_arm(puuid) == "treatment":
            effective_cooldown_ms = _treatment_cooldown_ms(
                total_games, prior_first_seen_at, time.time()
            )
            eligible_at_ms = _now_ms() + effective_cooldown_ms
        else:
            effective_cooldown_ms = _requeue_cooldown_for(
                total_games,
                prior_process_count + 1,
                max(0, requeue_cooldown_ms),
            )
            eligible_at_ms = _now_ms() + effective_cooldown_ms
        con.execute(
            """
            UPDATE crawl_seen
            SET processed = 0,
                last_crawled_at = ?,
                process_count = process_count + 1,
                new_games_found = new_games_found + ?,
                new_games_by_queue_json = ?,
                last_crawled_match_created_ms = ?,
                classic_affinity = ?,
                classic_affinity_rank = ?,
                classic_games_24h = ?,
                classic_games_recent = ?,
                classic_last_seen_ms = ?,
                classic_revisit_interval_ms = ?,
                classic_rate_num = ?,
                classic_rate_den = ?,
                classic_last_crawl_ms = ?            WHERE puuid = ?
            """,
            (
                now,
                new_games_found,
                queue_counts_json,
                crawled_match_ms,
                profile.label,
                profile.rank,
                profile.games_24h,
                profile.games_recent,
                profile.last_seen_ms,
                profile.revisit_interval_ms,
                rate_num,
                rate_den,
                now_epoch_ms,                puuid,
            ),
        )
        con.execute(
            """
            UPDATE crawl_queue
            SET status = 'pending',
                claimed_by = NULL,
                claimed_at_ms = 0,
                eligible_at_ms = ?,
                updated_at = ?,
                classic_affinity_rank = ?,
                classic_lambda = ?,
                classic_last_crawl_ms = ?,
                classic_span_ms = ?
            WHERE puuid = ?
            """,
            (
                eligible_at_ms,
                now,
                profile.rank,
                _classic_lambda(
                    rate_num, rate_den, classic_discovered=classic_discovered
                ),
                now_epoch_ms,
                _classic_span_ms(profile.revisit_interval_ms),
                puuid,
            ),
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
            new_games_by_queue_json = ?,
            last_crawled_match_created_ms = ?,
            classic_affinity = ?,
            classic_affinity_rank = ?,
            classic_games_24h = ?,
            classic_games_recent = ?,
            classic_last_seen_ms = ?,
            classic_revisit_interval_ms = ?,
            classic_rate_num = ?,
            classic_rate_den = ?,
            classic_last_crawl_ms = ?        WHERE puuid = ?
        """,
        (
            now,
            new_games_found,
            queue_counts_json,
            crawled_match_ms,
            profile.label,
            profile.rank,
            profile.games_24h,
            profile.games_recent,
            profile.last_seen_ms,
            profile.revisit_interval_ms,
            rate_num,
            rate_den,
            now_epoch_ms,            puuid,
        ),
    )
    con.execute(
        """
        UPDATE crawl_queue
        SET status = 'done',
            claimed_by = NULL,
            claimed_at_ms = 0,
            updated_at = ?,
            classic_affinity_rank = ?,
            classic_lambda = ?,
            classic_last_crawl_ms = ?,
            classic_span_ms = ?
        WHERE puuid = ?
        """,
        (
            now,
            profile.rank,
            _classic_lambda(rate_num, rate_den, classic_discovered=classic_discovered),
            now_epoch_ms,
            _classic_span_ms(profile.revisit_interval_ms),
            puuid,
        ),
    )
    con.commit()
    return False


def _defer_player_for_history_hydration(
    con: sqlite3.Connection,
    puuid: str,
    *,
    delay_ms: int = _MANUAL_SEED_HYDRATION_DELAY_MS,
    claim: PlayerClaim | None = None,
) -> bool:
    if _is_rpc_storage(con):
        # The caller supplies the claim token through ``_rpc_player_claim``;
        # direct invocation without one is unsafe and therefore fails closed.
        if claim is None:
            raise SnowballWriterError("player claim required")
        return bool(con.defer_player(claim, delay_ms=delay_ms, reason="history_hydration"))
    row = con.execute(
        "SELECT process_count FROM crawl_seen WHERE puuid = ?",
        (puuid,),
    ).fetchone()
    process_count = int(row[0]) if row else 0
    if process_count >= _EMPTY_HISTORY_RETRY_LIMIT:
        return False

    now = _utc_now()
    con.execute(
        """
        UPDATE crawl_seen
        SET processed = 0,
            process_count = process_count + 1,
            last_crawled_at = ?
        WHERE puuid = ?
        """,
        (now, puuid),
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
        (_now_ms() + max(0, delay_ms), now, puuid),
    )
    con.commit()
    return True


@_retry_on_locked
def _release_player_for_lcu_unavailable(
    con: sqlite3.Connection,
    puuid: str,
    *,
    delay_ms: int = _LCU_UNAVAILABLE_RETRY_DELAY_MS,
    claim: PlayerClaim | None = None,
) -> None:
    if _is_rpc_storage(con):
        if claim is None:
            raise SnowballWriterError("player claim required")
        con.release_player_unavailable(claim, delay_ms=delay_ms)
        return
    now = _utc_now()
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
        (_now_ms() + max(0, delay_ms), now, puuid),
    )
    con.commit()


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


def _seed_suggested_players(
    con: sqlite3.Connection,
    lcu: LCUClient,
    suggested_cap: int,
) -> int:
    added = 0
    if suggested_cap <= 0:
        return 0
    for item in get_suggested_players(lcu):
        puuid = (
            item.get("puuid")
            or (item.get("player") or {}).get("puuid")
            or (item.get("summoner") or {}).get("puuid")
        )
        if not puuid:
            summoner_id = item.get("summonerId") or (item.get("summoner") or {}).get("summonerId")
            if summoner_id:
                summoner = get_summoner_by_id(lcu, summoner_id)
                if isinstance(summoner, dict):
                    puuid = summoner.get("puuid")
        if not puuid:
            continue
        result = _enqueue_player(con, str(puuid), depth=0, source="suggested")
        if result in ("new", "requeued"):
            added += 1
        elif result == "noop":
            if _is_rpc_storage(con):
                if con.suggested_reseed(str(puuid), _SUGGESTED_RESEED_REQUEUE_COOLDOWN_SEC * 1000):
                    added += 1
                if added >= suggested_cap:
                    return added
                continue
            cutoff_text = datetime.fromtimestamp(
                max(0.0, time.time() - _SUGGESTED_RESEED_REQUEUE_COOLDOWN_SEC),
                tz=timezone.utc,
            ).isoformat()
            row = con.execute(
                """
                SELECT source, priority, min_depth, discovered_from_game_id,
                       latest_seen_match_created_ms, last_crawled_at, processed,
                       seed_family
                FROM crawl_seen
                WHERE puuid = ?
                """,
                (str(puuid),),
            ).fetchone()
            if row is not None:
                (
                    seen_source,
                    seen_priority,
                    seen_depth,
                    seen_game_id,
                    latest_seen_match_ms,
                    last_crawled_at,
                    processed,
                    seen_seed_family,
                ) = row
                if int(processed) == 1 and (last_crawled_at is None or str(last_crawled_at) <= cutoff_text):
                    became_pending = _upsert_queue_row(
                        con,
                        str(puuid),
                        int(seen_depth),
                        str(seen_source),
                        int(seen_priority),
                        str(seen_game_id) if seen_game_id else None,
                        int(latest_seen_match_ms),
                        requeue=True,
                        eligible_at_ms=0,
                        seed_family=str(seen_seed_family or "") or _UNKNOWN_FAMILY,
                    )
                    if became_pending:
                        con.execute(
                            "UPDATE crawl_seen SET processed = 0 WHERE puuid = ?",
                            (str(puuid),),
                        )
                        con.commit()
                        added += 1
        if added >= suggested_cap:
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


def _normalize_seed_family_label(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value)
    value = value.strip("_")
    return value[:120] if value else None


def _parse_riot_id_seed_record(raw: str) -> tuple[str, str] | None:
    if "\t" in raw:
        seed_text, family_text = raw.split("\t", 1)
    else:
        seed_text, family_text = raw, ""
    normalized = _normalize_riot_id_seed(seed_text)
    if not normalized:
        return None
    seed_family = _normalize_seed_family_label(family_text) or "manual_riot_id"
    return normalized, seed_family


def _load_riot_id_seed_records(
    *,
    riot_ids: tuple[str, ...] = (),
    riot_id_files: tuple[Path, ...] = (),
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_candidate(raw: str) -> None:
        parsed = _parse_riot_id_seed_record(raw)
        if not parsed:
            return
        normalized, seed_family = parsed
        if normalized in seen:
            return
        seen.add(normalized)
        ordered.append((normalized, seed_family))

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


def _load_riot_id_seeds(
    *,
    riot_ids: tuple[str, ...] = (),
    riot_id_files: tuple[Path, ...] = (),
) -> list[str]:
    return [
        riot_id
        for riot_id, _ in _load_riot_id_seed_records(
            riot_ids=riot_ids,
            riot_id_files=riot_id_files,
        )
    ]


def _get_riot_bridge(con: sqlite3.Connection, public_puuid: str) -> tuple[str, str | None] | None:
    if _is_rpc_storage(con):
        return con.bridge_get(public_puuid)
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
    if _is_rpc_storage(con):
        con.bridge_upsert(
            public_puuid=public_puuid, riot_id=riot_id, lcu_puuid=lcu_puuid,
            resolve_status=resolve_status,
        )
        return
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
    seed_records = _load_riot_id_seed_records(riot_ids=riot_ids)
    if not seed_records:
        return 0

    now_ms = _now_ms()
    hot_cutoff_ms = now_ms - (_MANUAL_SEED_HOT_WINDOW_HOURS * 60 * 60 * 1000)
    warm_cutoff_ms = now_ms - (_MANUAL_SEED_WARM_WINDOW_HOURS * 60 * 60 * 1000)

    existing_open = _open_queue_source_count(con, "manual_riot_id")
    remaining_budget = max(0, pending_cap - existing_open) if pending_cap > 0 else None
    if remaining_budget == 0:
        print(
            f"[snowball] manual_riot_id seed skipped  "
            f"open_manual_queue={existing_open}  pending_cap={pending_cap}",
            flush=True,
        )
        return 0

    total_chunks = (len(seed_records) + _LCU_RIOT_ID_LOOKUP_BATCH - 1) // _LCU_RIOT_ID_LOOKUP_BATCH
    resolved_total = 0
    target_ready_total = 0
    for chunk_idx, chunk in enumerate(
        _iter_chunks([(seed_family, riot_id) for riot_id, seed_family in seed_records], _LCU_RIOT_ID_LOOKUP_BATCH),
        start=1,
    ):
        resolved = lookup_summoners_by_riot_ids(lcu, [riot_id for _, riot_id in chunk])
        by_alias = {
            f"{str(item.get('gameName') or '').strip()}#{str(item.get('tagLine') or '').strip()}": item
            for item in resolved
        }
        resolved_total += len(by_alias)
        for seed_family, riot_id in chunk:
            match = by_alias.get(riot_id)
            lcu_puuid = str(match.get("puuid") or "").strip() if match else ""
            if not lcu_puuid:
                continue
            # Enqueue immediately without fetching match history — history calls
            # for cold summoners take 5-10s each and block the seed loop.
            # Consume workers will fetch history and filter by queue/target_games.
            result = _enqueue_player(
                con,
                lcu_puuid,
                depth=0,
                source="manual_riot_id",
                # Keep freshly refreshed OPGG/manual roots ahead of stale match
                # backlog; their own history fetch will establish the true
                # newest match timestamp before descendants are enqueued.
                discovered_match_created_ms=now_ms,
                initial_delay_ms=_MANUAL_SEED_HYDRATION_DELAY_MS,
                seed_family=seed_family,
            )
            if result not in {"new", "requeued"}:
                continue
            target_ready_total += 1
            added += 1
            if remaining_budget is not None:
                remaining_budget -= 1
                if remaining_budget <= 0:
                    print(
                        f"[snowball] manual_riot_id pending cap reached  "
                        f"added={added}  pending_cap={pending_cap}",
                        flush=True,
                    )
                    break
        if remaining_budget is not None and remaining_budget <= 0:
            break
        if chunk_idx == 1 or chunk_idx == total_chunks or chunk_idx % 5 == 0:
            print(
                f"[snowball] manual_riot_id seed progress  chunks={chunk_idx}/{total_chunks}  "
                f"resolved={resolved_total}  enqueued={added}",
                flush=True,
            )

    print(
        f"[snowball] manual_riot_id activity  "
        f"resolved={resolved_total}  enqueued={added}",
        flush=True,
    )
    return added


def _reseed_recent_active_players(
    con: sqlite3.Connection,
    *,
    cap: int = _RECENT_ACTIVE_RESEED_CAP,
    cooldown_sec: int = _RECENT_ACTIVE_RESEED_COOLDOWN_SEC,
) -> int:
    """Requeue recently productive players when external seed windows dry up.

    These players already yielded unseen target-queue games before, so they are
    a better fallback than continuing to sweep low-yield leaderboard pages.
    """
    if cap <= 0:
        return 0
    if _is_rpc_storage(con):
        return con.reseed_recent(cap=cap, cooldown_ms=max(0, int(cooldown_sec * 1000)))

    cutoff_text = datetime.fromtimestamp(
        max(0.0, time.time() - max(0, cooldown_sec)),
        tz=timezone.utc,
    ).isoformat()
    rows = con.execute(
        """
        SELECT puuid, source, priority, min_depth, discovered_from_game_id,
               latest_seen_match_created_ms, seed_family
        FROM crawl_seen
        WHERE new_games_found > 0
          AND latest_seen_match_created_ms > 0
          AND (last_crawled_at IS NULL OR last_crawled_at <= ?)
        ORDER BY latest_seen_match_created_ms DESC,
                 new_games_found DESC,
                 priority ASC,
                 min_depth ASC,
                 first_seen_at DESC
        LIMIT ?
        """,
        (cutoff_text, cap),
    ).fetchall()
    if not rows:
        return 0

    added = 0
    for (
        puuid,
        source,
        priority,
        depth,
        discovered_from_game_id,
        latest_seen_match_ms,
        seed_family,
    ) in rows:
        became_pending = _upsert_queue_row(
            con,
            str(puuid),
            int(depth),
            str(source),
            int(priority),
            str(discovered_from_game_id) if discovered_from_game_id else None,
            int(latest_seen_match_ms),
            requeue=True,
            eligible_at_ms=0,
            seed_family=str(seed_family or "") or _UNKNOWN_FAMILY,
        )
        if became_pending:
            con.execute(
                "UPDATE crawl_seen SET processed = 0 WHERE puuid = ?",
                (str(puuid),),
            )
            added += 1
    con.commit()
    return added


def _reseed_source_family_players(
    con: sqlite3.Connection,
    *,
    sources: tuple[str, ...] = ("self", "friend", "ladder", "apex", "manual_riot_id", "riot_tier"),
    cap: int = _SOURCE_FAMILY_RESEED_CAP,
    cooldown_sec: int = _SOURCE_FAMILY_RESEED_COOLDOWN_SEC,
) -> int:
    """Requeue older source-family players so static seed pools can be revisited later.

    This is weaker than recent-active reseed, but it lets the overnight crawler
    revisit known social-graph entry points after enough time has passed for new
    matches to appear in the local LCU history window. We only requeue source
    families that have previously produced at least one unseen target-queue
    game; otherwise the worker can spend the whole night replaying zero-yield
    seed families.
    """
    if cap <= 0 or not sources:
        return 0
    if _is_rpc_storage(con):
        return con.reseed_source(
            sources=tuple(str(source) for source in sources if str(source)),
            cap=cap, cooldown_ms=max(0, int(cooldown_sec * 1000)),
        )

    normalized_sources = tuple(str(source) for source in sources if str(source))
    if not normalized_sources:
        return 0

    cutoff_text = datetime.fromtimestamp(
        max(0.0, time.time() - max(0, cooldown_sec)),
        tz=timezone.utc,
    ).isoformat()
    placeholders = ",".join("?" for _ in normalized_sources)
    rows = con.execute(
        f"""
        SELECT puuid, source, priority, min_depth, discovered_from_game_id,
               latest_seen_match_created_ms, seed_family
        FROM crawl_seen
        WHERE source IN ({placeholders})
          AND new_games_found > 0
          AND (last_crawled_at IS NULL OR last_crawled_at <= ?)
        ORDER BY priority ASC,
                 latest_seen_match_created_ms DESC,
                 process_count ASC,
                 first_seen_at DESC
        LIMIT ?
        """,
        (*normalized_sources, cutoff_text, cap),
    ).fetchall()
    if not rows:
        return 0

    added = 0
    for (
        puuid,
        source,
        priority,
        depth,
        discovered_from_game_id,
        latest_seen_match_ms,
        seed_family,
    ) in rows:
        became_pending = _upsert_queue_row(
            con,
            str(puuid),
            int(depth),
            str(source),
            int(priority),
            str(discovered_from_game_id) if discovered_from_game_id else None,
            int(latest_seen_match_ms),
            requeue=True,
            eligible_at_ms=0,
            seed_family=str(seed_family or "") or _UNKNOWN_FAMILY,
        )
        if became_pending:
            con.execute(
                "UPDATE crawl_seen SET processed = 0 WHERE puuid = ?",
                (str(puuid),),
            )
            added += 1
    con.commit()
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
    suggested_cap: int = 100,
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
    classic_claim_percent: int = _CLASSIC_DEFAULT_CLAIM_PERCENT,
    classic_revisit_min_hours: float = 10.0,
    classic_revisit_max_hours: float = 168.0,
    writer_client: Any | None = None,
    storage: SnowballWriterFacade | None = None,
) -> CrawlStats:
    """Expand the LCU-visible player graph and save unseen target-queue matches."""
    if target_queues is None:
        target_queues = DEFAULT_QUEUES

    creds = get_credentials()
    if creds is None:
        raise RuntimeError("League client not found")

    if writer_client is not None and storage is not None:
        raise ValueError("pass writer_client or storage, not both")
    if writer_client is not None:
        storage = SnowballWriterFacade(writer_client)
    rpc_mode = storage is not None
    if rpc_mode and not isinstance(storage, SnowballWriterFacade):
        raise TypeError("storage must be SnowballWriterFacade")
    con: sqlite3.Connection | None = None
    if rpc_mode:
        # No sqlite3.connect, path mkdir, or read-only preload in RPC mode.
        worker_id = worker_id or f"pid-{os.getpid()}"
        init_response = storage.initialize(worker_id=worker_id, claim_timeout_ms=max(1, claim_timeout_sec) * 1000)
        migrated = int(init_response.get("migrated") or 0)
        purged_riot_tier = int(init_response.get("purged") or 0)
        synced_priorities = int(init_response.get("synced") or 0)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = _connect_db(db_path)
        _ensure_schema_with_retry(con, worker_id=worker_id)
        migrated = _migrate_legacy_crawl_players(con)
        purged_riot_tier = _purge_invalid_riot_tier_rows(con)
        synced_priorities = _sync_source_priorities(con, worker_id=worker_id)
    claim_timeout_ms = max(1, claim_timeout_sec) * 1000
    player_requeue_cooldown_ms = max(0, player_requeue_cooldown_sec) * 1000
    classic_claim_percent = min(100, max(0, int(classic_claim_percent)))
    classic_revisit_min_ms = max(
        _CLASSIC_DEFAULT_REVISIT_MIN_MS,
        int(float(classic_revisit_min_hours) * 3600_000),
    )
    classic_revisit_max_ms = max(
        classic_revisit_min_ms,
        int(float(classic_revisit_max_hours) * 3600_000),
    )
    worker_id = worker_id or f"pid-{os.getpid()}"

    existing_game_ids = set() if rpc_mode else _load_existing_game_ids(con)
    db = storage if rpc_mode else con
    expanded_game_ids: set[str] = set()
    local_puuid_latest_ms: dict[str, int] = {}
    stats = CrawlStats()
    # The keys here are seed_family names, not immediate sources. A match-source
    # puuid discovered through a manual_riot_id seed has seed_family
    # "manual_riot_id", so any games it (or its descendants) yield credit the
    # manual_riot_id family — not "match". This is the core of transitive
    # attribution: cold seeds get credit for the games their downstream subgraph
    # produces, not just the (usually zero) games their immediate puuid yields.
    source_family_sources = ("self", "friend", "ladder", "apex", "manual_riot_id", "riot_tier")
    source_family_zero_streaks: dict[str, int] = {source: 0 for source in source_family_sources}
    source_family_run_yield: dict[str, int] = {source: 0 for source in source_family_sources}
    source_family_backoff_until: dict[str, float] = {}
    for source in source_family_sources:
        state_text = _get_runtime_state_text(db, f"backoff:source-family:{source}")
        if state_text:
            try:
                source_family_backoff_until[source] = float(state_text)
            except ValueError:
                source_family_backoff_until[source] = 0.0
    recent_active_zero_streak = 0
    recent_active_state = _get_runtime_state_text(db, "backoff:recent-active")
    try:
        recent_active_backoff_until = float(recent_active_state) if recent_active_state else 0.0
    except ValueError:
        recent_active_backoff_until = 0.0
    active_reseed_mode: str | None = None

    def _available_source_family_sources() -> tuple[str, ...]:
        now = time.time()
        return tuple(
            source
            for source in source_family_sources
            if source_family_backoff_until.get(source, 0.0) <= now
        )

    def _source_family_available(source: str) -> bool:
        return source_family_backoff_until.get(source, 0.0) <= time.time()

    def _record_source_family_result(
        seed_family: str,
        *,
        target_games_found: int,
        new_games_found: int,
    ) -> None:
        """Update the per-family backoff streak using *transitive* yield.

        seed_family is the root entry point that owns this puuid (e.g.
        manual_riot_id), not the immediate source (which may be 'match').
        We back off only when no descendant of this family has produced
        a captured game in this run after _SOURCE_FAMILY_BACKOFF_ZERO_STREAK
        consecutive processed players — which is the right signal for cold
        seeds whose value is to open the frontier rather than yield directly.
        """
        if seed_family not in source_family_zero_streaks:
            return
        if new_games_found > 0:
            source_family_zero_streaks[seed_family] = 0
            source_family_run_yield[seed_family] = (
                source_family_run_yield.get(seed_family, 0) + new_games_found
            )
            # Mirror to persisted state so sister workers see the credit.
            _increment_persisted_family_yield(db, seed_family, new_games_found)
            source_family_backoff_until.pop(seed_family, None)
            _delete_runtime_state(db, f"backoff:source-family:{seed_family}")
            print(
                f"[snowball] seed-family yield  fam={seed_family}  "
                f"+{new_games_found}  run_total={source_family_run_yield[seed_family]}  "
                f"worker={worker_id}",
                flush=True,
            )
            return

        streak = source_family_zero_streaks.get(seed_family, 0) + 1
        source_family_zero_streaks[seed_family] = streak
        if streak < _SOURCE_FAMILY_BACKOFF_ZERO_STREAK:
            return
        # If the family has produced anything (transitive) in this run, don't
        # back off — the streak is just a local dry patch within a productive
        # subgraph. Pull from BOTH local and persisted yield so a sibling
        # worker that captured games can rescue this worker's streak.
        local_yield = source_family_run_yield.get(seed_family, 0)
        persisted_yield = _read_persisted_family_yield(db, seed_family)
        effective_yield = max(local_yield, persisted_yield)
        if effective_yield > 0:
            source_family_zero_streaks[seed_family] = 0
            print(
                f"[snowball] seed-family backoff prevented (transitive yield)  "
                f"fam={seed_family}  streak={streak}  "
                f"local_yield={local_yield}  persisted_yield={persisted_yield}  "
                f"worker={worker_id}",
                flush=True,
            )
            return

        source_family_zero_streaks[seed_family] = 0
        backoff_until = time.time() + _SOURCE_FAMILY_BACKOFF_SEC
        source_family_backoff_until[seed_family] = backoff_until
        _set_runtime_state_text(
            db,
            f"backoff:source-family:{seed_family}",
            str(backoff_until),
        )
        print(
            f"[snowball] source-family backoff  seed_family={seed_family}  "
            f"cooldown={_SOURCE_FAMILY_BACKOFF_SEC:.0f}s  worker={worker_id}",
            flush=True,
        )

    def _recent_active_available() -> bool:
        return time.time() >= recent_active_backoff_until

    def _set_active_reseed_mode(mode: str | None) -> None:
        nonlocal active_reseed_mode, recent_active_zero_streak
        active_reseed_mode = mode
        if mode != "recent-active":
            recent_active_zero_streak = 0

    def _record_recent_active_result(
        *,
        target_games_found: int,
        new_games_found: int,
    ) -> None:
        nonlocal active_reseed_mode, recent_active_zero_streak, recent_active_backoff_until
        if active_reseed_mode != "recent-active":
            return
        if new_games_found > 0:
            recent_active_zero_streak = 0
            if recent_active_backoff_until > 0.0:
                recent_active_backoff_until = 0.0
                _delete_runtime_state(db, "backoff:recent-active")
            return

        recent_active_zero_streak += 1
        if recent_active_zero_streak < _RECENT_ACTIVE_BACKOFF_ZERO_STREAK:
            return

        recent_active_zero_streak = 0
        active_reseed_mode = None
        recent_active_backoff_until = time.time() + _RECENT_ACTIVE_BACKOFF_SEC
        _set_runtime_state_text(
            db,
            "backoff:recent-active",
            str(recent_active_backoff_until),
        )
        print(
            f"[snowball] recent-active backoff  "
            f"cooldown={_RECENT_ACTIVE_BACKOFF_SEC:.0f}s  worker={worker_id}",
            flush=True,
        )

    with LCUClient(creds) as lcu:
        current_patch = _major_minor_patch(get_game_version(lcu))
        if games_per_player == 0 and current_patch is None:
            raise RuntimeError("Could not determine current League patch from LCU")
        me = _get_current_summoner_with_retry(lcu)
        my_puuid = str(me["puuid"]) if me and me.get("puuid") else ""
        my_name = (me or {}).get("gameName") or (me or {}).get("displayName") or "?"
        has_current_summoner = bool(my_puuid)
        if not has_current_summoner:
            print(
                f"[snowball] startup warning  current_summoner_unavailable  "
                f"worker={worker_id}  mode=degraded",
                flush=True,
            )

        if include_self and has_current_summoner and _source_family_available("self"):
            result = _enqueue_player(db, my_puuid, depth=0, source="self")
            if result == "new":
                stats.seeded_players += 1
            elif result == "requeued":
                stats.requeued_players += 1
        elif include_self and not has_current_summoner:
            print(
                f"[snowball] startup skip  source=self  "
                f"reason=current_summoner_unavailable  worker={worker_id}",
                flush=True,
            )
        elif include_self:
            print(f"[snowball] startup skip  source=self  reason=backoff  worker={worker_id}", flush=True)

        if include_friends and has_current_summoner and _source_family_available("friend"):
            for friend in get_friends(lcu):
                friend_puuid = friend.get("puuid")
                if not friend_puuid:
                    continue
                result = _enqueue_player(db, str(friend_puuid), depth=0, source="friend")
                if result == "new":
                    stats.seeded_players += 1
                elif result == "requeued":
                    stats.requeued_players += 1
        elif include_friends and not has_current_summoner:
            print(
                f"[snowball] startup skip  source=friend  "
                f"reason=current_summoner_unavailable  worker={worker_id}",
                flush=True,
            )
        elif include_friends:
            print(f"[snowball] startup skip  source=friend  reason=backoff  worker={worker_id}", flush=True)

        if include_ladder and has_current_summoner and _source_family_available("ladder"):
            stats.seeded_players += _seed_ladder_neighbors(db, lcu, my_puuid, ladder_cap)
        elif include_ladder and not has_current_summoner:
            print(
                f"[snowball] startup skip  source=ladder  "
                f"reason=current_summoner_unavailable  worker={worker_id}",
                flush=True,
            )
        elif include_ladder:
            print(f"[snowball] startup skip  source=ladder  reason=backoff  worker={worker_id}", flush=True)

        stats.seeded_players += _seed_suggested_players(db, lcu, suggested_cap)

        if include_apex and _source_family_available("apex"):
            stats.seeded_players += _seed_apex_players(
                db, lcu, apex_queues=apex_queues, apex_tiers=apex_tiers, apex_cap=apex_cap
            )
        elif include_apex:
            print(f"[snowball] startup skip  source=apex  reason=backoff  worker={worker_id}", flush=True)

        if include_riot_tier and _source_family_available("riot_tier"):
            stats.seeded_players += _seed_riot_tier_players(
                db,
                lcu,
                region=riot_region,
                riot_queues=riot_queues,
                riot_tiers=riot_tiers,
                riot_divisions=riot_divisions,
                riot_page_limit=riot_page_limit,
                riot_cap=riot_cap,
            )
        elif include_riot_tier:
            print(f"[snowball] startup skip  source=riot_tier  reason=backoff  worker={worker_id}", flush=True)

        manual_riot_id_records = _load_riot_id_seed_records(
            riot_ids=seed_riot_ids,
            riot_id_files=seed_riot_id_files,
        )
        if manual_riot_id_records and _source_family_available("manual_riot_id"):
            print(
                f"[snowball] preparing manual_riot_id seeds  count={len(manual_riot_id_records)}  worker={worker_id}",
                flush=True,
            )
            stats.seeded_players += _seed_manual_riot_ids(
                db,
                lcu,
                riot_ids=tuple(
                    f"{riot_id}\t{seed_family}"
                    for riot_id, seed_family in manual_riot_id_records
                ),
                target_queues=target_queues,
                history_window=history_window,
                games_per_player=games_per_player,
                pending_cap=max(0, manual_seed_pending_cap),
            )
            print(
                f"[snowball] finished manual_riot_id seeds  enqueued={stats.seeded_players}  worker={worker_id}",
                flush=True,
            )
        elif manual_riot_id_records:
            print(
                f"[snowball] startup skip  source=manual_riot_id  reason=backoff  worker={worker_id}",
                flush=True,
            )

        pending = _pending_player_count(db)
        if pending == 0:
            suggested_reseeded = _seed_suggested_players(db, lcu, suggested_cap)
            if suggested_reseeded:
                stats.seeded_players += suggested_reseeded
                pending = _pending_player_count(db)
                _set_active_reseed_mode("suggested")
                print(
                    f"[snowball] suggested-player reseed  "
                    f"enqueued={suggested_reseeded}  pending={pending}  worker={worker_id}",
                    flush=True,
                )
        if pending == 0:
            source_reseeded = _reseed_source_family_players(
                db,
                sources=_available_source_family_sources(),
            )
            if source_reseeded:
                stats.requeued_players += source_reseeded
                pending = _pending_player_count(db)
                _set_active_reseed_mode("source-family")
                print(
                    f"[snowball] source-family reseed  "
                    f"requeued={source_reseeded}  pending={pending}  worker={worker_id}",
                    flush=True,
                )
        if pending == 0:
            recent_reseeded = _reseed_recent_active_players(db) if _recent_active_available() else 0
            if recent_reseeded:
                stats.requeued_players += recent_reseeded
                pending = _pending_player_count(db)
                _set_active_reseed_mode("recent-active")
                print(
                    f"[snowball] recent-active reseed  "
                    f"requeued={recent_reseeded}  pending={pending}  worker={worker_id}",
                    flush=True,
                )
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
        reclaimed = _requeue_stale_claims(db, claim_timeout_ms)
        if reclaimed:
            print(f"[snowball] reclaimed stale claims={reclaimed}")

        waiting_logged = False
        empty_queue_wait_started_at: float | None = None
        while stats.saved_games < target_games and stats.processed_players < max_players:
            try:
                next_player = _claim_next_player(
                    db,
                    worker_id=worker_id,
                    claim_timeout_ms=claim_timeout_ms,
                    classic_claim_percent=classic_claim_percent,
                )
            except SnowballWriterClaimsStopped:
                print(
                    f"[snowball] writer stopped new claims  worker={worker_id}",
                    flush=True,
                )
                break
            if next_player is None:
                wait_ms = _next_pending_wait_ms(db)
                if wait_ms is None:
                    now_monotonic = time.monotonic()
                    if empty_queue_wait_started_at is None:
                        empty_queue_wait_started_at = now_monotonic
                        print(
                            f"[snowball] queue empty, waiting briefly for new seeds  "
                            f"grace={_EMPTY_QUEUE_GRACE_SEC:.0f}s  worker={worker_id}"
                        )
                    elif now_monotonic - empty_queue_wait_started_at >= _EMPTY_QUEUE_GRACE_SEC:
                        suggested_reseeded = _seed_suggested_players(db, lcu, suggested_cap)
                        if suggested_reseeded:
                            stats.seeded_players += suggested_reseeded
                            empty_queue_wait_started_at = None
                            waiting_logged = False
                            _set_active_reseed_mode("suggested")
                            print(
                                f"[snowball] suggested-player reseed  "
                                f"enqueued={suggested_reseeded}  worker={worker_id}",
                                flush=True,
                            )
                            continue
                        source_reseeded = _reseed_source_family_players(
                            db,
                            sources=_available_source_family_sources(),
                        )
                        if source_reseeded:
                            stats.requeued_players += source_reseeded
                            empty_queue_wait_started_at = None
                            waiting_logged = False
                            _set_active_reseed_mode("source-family")
                            print(
                                f"[snowball] source-family reseed  "
                                f"requeued={source_reseeded}  worker={worker_id}",
                                flush=True,
                            )
                            continue
                        recent_reseeded = (
                            _reseed_recent_active_players(db)
                            if _recent_active_available()
                            else 0
                        )
                        if recent_reseeded:
                            stats.requeued_players += recent_reseeded
                            empty_queue_wait_started_at = None
                            waiting_logged = False
                            _set_active_reseed_mode("recent-active")
                            print(
                                f"[snowball] recent-active reseed  "
                                f"requeued={recent_reseeded}  worker={worker_id}",
                                flush=True,
                            )
                            continue
                        empty_queue_wait_started_at = None
                        waiting_logged = False
                        print(
                            f"[snowball] idle: no reseed candidates, sleeping  "
                            f"{_EMPTY_QUEUE_IDLE_POLL_SEC:.0f}s  worker={worker_id}",
                            flush=True,
                        )
                        time.sleep(_EMPTY_QUEUE_IDLE_POLL_SEC)
                        continue
                    time.sleep(1.0)
                    continue
                empty_queue_wait_started_at = None
                sleep_sec = min(max(wait_ms / 1000.0, 0.25), 5.0)
                if not waiting_logged:
                    print(
                        f"[snowball] waiting for eligible queue items  "
                        f"pending={_pending_player_count(db)}  sleep={sleep_sec:.2f}s  "
                        f"worker={worker_id}"
                    )
                    waiting_logged = True
                time.sleep(sleep_sec)
                continue

            player_claim: PlayerClaim | None = next_player if isinstance(next_player, PlayerClaim) else None
            if player_claim is not None:
                puuid = player_claim.puuid
                depth = player_claim.depth
                source = player_claim.source
                claimed_match_created_ms = player_claim.claimed_match_created_ms
                claimed_seed_family = player_claim.seed_family
                discovered_queue_id = player_claim.discovered_queue_id
                claim_lane = player_claim.claim_lane
            else:
                (
                    puuid,
                    depth,
                    source,
                    claimed_match_created_ms,
                    claimed_seed_family,
                    discovered_queue_id,
                    claim_lane,
                ) = next_player
            stats.processed_players += 1
            waiting_logged = False
            empty_queue_wait_started_at = None

            history = get_match_history(lcu, puuid, begin=0, end=history_window)
            observed_match_created_ms = _latest_any_match_created_ms(history)
            classic_profile = _classic_affinity_profile(
                history,
                discovered_queue_id=discovered_queue_id,
                min_revisit_ms=classic_revisit_min_ms,
                max_revisit_ms=classic_revisit_max_ms,
            )
            if observed_match_created_ms == 0 and get_current_summoner(lcu) is None:
                _release_player_for_lcu_unavailable(db, puuid, claim=player_claim)
                print(
                    f"[snowball] LCU unavailable; released player  "
                    f"source={source}  player=redacted  "
                    f"delay_ms={_LCU_UNAVAILABLE_RETRY_DELAY_MS}  worker={worker_id}",
                    flush=True,
                )
                time.sleep(5.0)
                continue
            if (
                observed_match_created_ms == 0
                and source in {"manual_riot_id", "riot_tier"}
                and _defer_player_for_history_hydration(db, puuid, claim=player_claim)
            ):
                stats.requeued_players += 1
                print(
                    f"[snowball] history not hydrated; deferred player  "
                    f"source={source}  player=redacted  "
                    f"delay_ms={_MANUAL_SEED_HYDRATION_DELAY_MS}  worker={worker_id}",
                    flush=True,
                )
                continue
            game_ids = _extract_target_game_ids(history, target_queues)
            if games_per_player == 0:
                game_ids = _adaptive_target_game_ids(
                    history,
                    target_queues,
                    puuid=puuid,
                    current_patch=current_patch,
                )
            elif games_per_player is not None and games_per_player > 0:
                game_ids = game_ids[:games_per_player]
            print(
                f"[snowball] player {stats.processed_players}/{max_players}  "
                f"depth={depth}  source={source:<6}  player=redacted  "
                f"target_games={len(game_ids)}  pending={max(0, _pending_player_count(db) - 1)}  "
                f"worker={worker_id}"
            )

            new_games_for_player = 0
            new_games_by_queue: dict[int, int] = {}
            for game_id in game_ids:
                # Finish the claimed player's history window before honoring the
                # worker target.  Stopping mid-player strands the remaining recent
                # games until a newer rediscovery, defeating full-window capture.
                if game_id in expanded_game_ids:
                    continue
                game_claim_response = _claim_game_id(db, game_id, worker_id=worker_id, claim_timeout_ms=claim_timeout_ms)
                if not game_claim_response:
                    continue
                game_claim = game_claim_response if isinstance(game_claim_response, GameClaim) else None

                detail = get_game_detail(lcu, game_id)
                if not detail:
                    _release_game_claim(db, game_id, game_claim)
                    stats.failed_games += 1
                    continue

                expanded_game_ids.add(game_id)
                stats.expanded_games += 1

                record = _parse_game_detail(detail, target_queues)
                if record is None:
                    _mark_game_done(db, game_id, game_claim)
                    stats.filtered_games += 1
                    continue

                if not rpc_mode and record["game_id"] in existing_game_ids:
                    _backfill_participants_json(db, record)
                    _mark_game_done(db, record["game_id"], game_claim)
                    stats.existing_games += 1
                else:
                    record["captured_at"] = _utc_now()
                    record["seed_family"] = claimed_seed_family
                    participant_puuids = _extract_participant_puuids(detail)
                    inserted = (
                        storage.commit_game(game_claim, record, participant_puuids)
                        if rpc_mode and game_claim is not None
                        else _insert_game(db, record)
                    )
                    if inserted:
                        existing_game_ids.add(record["game_id"])
                        if not rpc_mode:
                            _mark_game_done(db, record["game_id"], game_claim)
                        stats.saved_games += 1
                        new_games_for_player += 1
                        queue_id = int(record["queue_id"])
                        new_games_by_queue[queue_id] = new_games_by_queue.get(queue_id, 0) + 1
                        # Was "Mayhem" or else "ARAM", which printed every 經典
                        # (4310) and 大混戰經典風 (2450) save as ARAM and made the
                        # log actively misleading when diagnosing queue coverage.
                        label = _QUEUE_LOG_LABEL.get(record["queue_id"], str(record["queue_id"]))
                        print(
                            f"  [saved] {label:<6}  game_id={record['game_id']}  "
                            f"patch={record['patch']}  total_saved={stats.saved_games}  "
                            f"worker={worker_id}"
                        )
                    else:
                        _release_game_claim(db, record["game_id"], game_claim)
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
                        db,
                        participant_puuid,
                        depth + 1,
                        source="match",
                        discovered_from_game_id=record["game_id"],
                        discovered_match_created_ms=int(record["created_ms"]),
                        requeue_cooldown_ms=player_requeue_cooldown_ms,
                        seed_family=claimed_seed_family,
                        discovered_queue_id=int(record["queue_id"]),
                        classic_revisit_min_ms=classic_revisit_min_ms,
                    )
                    if result == "new":
                        stats.seeded_players += 1
                    elif result == "requeued":
                        stats.requeued_players += 1

            _record_source_family_result(
                claimed_seed_family,
                target_games_found=len(game_ids),
                new_games_found=new_games_for_player,
            )
            _record_recent_active_result(
                target_games_found=len(game_ids),
                new_games_found=new_games_for_player,
            )
            requeued_on_finish = _mark_player_done(
                db,
                puuid,
                new_games_found=new_games_for_player,
                claimed_match_created_ms=claimed_match_created_ms,
                observed_match_created_ms=observed_match_created_ms,
                requeue_cooldown_ms=player_requeue_cooldown_ms,
                new_games_by_queue=new_games_by_queue,
                source=source,
                seed_family=claimed_seed_family,
                worker_id=worker_id,
                current_patch=current_patch,
                history_game_count=len(history),
                target_game_count=len(game_ids),
                claim_lane=claim_lane,
                classic_profile=classic_profile,
                classic_revisit_min_ms=classic_revisit_min_ms,
                claim=player_claim,
            )
            if requeued_on_finish:
                stats.requeued_players += 1

    pending_after = _pending_player_count(db)
    if con is not None:
        con.close()
    print(
        f"[snowball] done  processed_players={stats.processed_players}  "
        f"expanded_games={stats.expanded_games}  saved_games={stats.saved_games}  "
        f"existing_games={stats.existing_games}  filtered={stats.filtered_games}  "
        f"failed={stats.failed_games}  requeued={stats.requeued_players}  "
        f"pending={pending_after}  worker={worker_id}"
    )
    return stats
