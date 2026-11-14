"""Incremental slim replica of the current patch's games for the site build.

The tier-list build streams the current patch's ``participants_json`` a dozen
times.  Read straight from ``games.db`` that is ~11 KB of JSON per game pulled
out of overflow pages in a 200+ GB file, once per pass (measured 2026-11-14:
1.83M games, ~19 min per pass at best, 2-4.5 h per build, and every memory
abort restarts from zero).  ~90% of that JSON is the per-participant ``stats``
dict, which no tier-list scan reads.

So keep a small local copy of just the current patch:

    data/cache/patch_extract/q2400-16.22.lean.db    participants without ``stats``
    data/cache/patch_extract/q2400-16.22.stats.db   championId/teamId + the few
                                                    ``stats`` fields the empirical
                                                    champion profiles read

Both files hold a ``games`` table with the public column names, so every
consumer's SQL runs against them unchanged.

Sync is incremental.  ``games`` is a rowid table (``game_id TEXT PRIMARY KEY``)
and rows are only ever appended, so "everything new" is the rowid tail above the
last sync's high-water mark -- a sequential read of just the new rows, whatever
their ``created_ms`` (the crawler back-fills older games of the same patch).
Two things a tail read cannot see are handled explicitly:

* a row whose payload is later upgraded in place (the writer replaces a
  stats-less payload with a richer one): such rows are remembered as *poor* and
  re-read by primary key on every sync until they stop being poor;
* anything else that makes the copy disagree with the source (deleted rows, a
  rebuilt DB): the replica's row count is compared with the source's inside the
  same read snapshot, and a mismatch triggers one full rebuild.

The replica is a cache.  Callers must treat any failure here as "use the source
DB as before", never as a build failure.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

# Bump when the slimming rules change (a dropped key becomes needed, the stats
# allowlist grows): every existing replica is rebuilt on its next sync.
EXTRACT_VERSION = 1

DEFAULT_EXTRACT_DIR = Path("data/cache/patch_extract")

# Per-participant keys the lean copy drops.  Everything else is kept verbatim so
# a new small key never needs a version bump.
LEAN_DROP_KEYS = frozenset({"stats"})

# ``stats`` fields read by scripts/build_empirical_champion_scores.py.
STATS_KEEP_KEYS = frozenset(
    {
        "damage_self_mitigated",
        "effective_heal_and_shielding",
        "magic_damage_dealt_to_champions",
        "physical_damage_dealt_to_champions",
        "time_ccing_others",
        "total_damage_dealt_to_champions",
        "total_damage_shielded_on_teammates",
        "total_damage_taken",
        "total_heal",
        "total_heals_on_teammates",
        "total_units_healed",
        "true_damage_dealt_to_champions",
    }
)
STATS_PARTICIPANT_KEYS = ("championId", "teamId")

FULL_TEAM_SIZE = 10
POOR_RECHECK_LIMIT = 50_000
_BATCH = 2_000

_COLUMNS = (
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
_SELECT = "SELECT rowid, " + ", ".join(_COLUMNS) + " FROM games"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {schema}.games (
    game_id           TEXT PRIMARY KEY,
    queue_id          INTEGER NOT NULL,
    patch             TEXT NOT NULL,
    blue_champs       TEXT NOT NULL,
    red_champs        TEXT NOT NULL,
    blue_wins         INTEGER NOT NULL,
    duration_sec      INTEGER NOT NULL,
    created_ms        INTEGER NOT NULL,
    captured_at       TEXT NOT NULL,
    participants_json TEXT
);
CREATE INDEX IF NOT EXISTS {schema}.idx_games_queue_patch_created
ON games(queue_id, patch, created_ms);
"""

Log = Callable[[str], Any]


def patch_range(patch_prefix: str) -> tuple[str, str]:
    """Half-open ``[lo, hi)`` string range equivalent to ``patch LIKE 'prefix%'``.

    ``LIKE`` cannot narrow a seek on ``(queue_id, patch, created_ms)`` (SQLite's
    LIKE optimisation needs a case-sensitive LIKE or a NOCASE column), so a
    prefix match walks every patch of the queue.  The range form seeks.
    """
    prefix = str(patch_prefix)
    if not prefix:
        raise ValueError("patch_prefix must be non-empty")
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


# ---------------------------------------------------------------------------
# Scan routing: consumers ask which file to open for (db, queue, patch).
# ---------------------------------------------------------------------------

_ROUTES: dict[tuple[str, int, str], Path] = {}


def _route_key(db_path: Path | str, queue_id: int, patch_prefix: str) -> tuple[str, int, str]:
    return (str(Path(db_path)), int(queue_id), str(patch_prefix))


def register_scan_db(
    db_path: Path | str, queue_id: int, patch_prefix: str, replica: Path | str
) -> None:
    """Route scans of exactly this (db, queue, patch) scope to *replica*."""
    _ROUTES[_route_key(db_path, queue_id, patch_prefix)] = Path(replica)


def clear_scan_dbs() -> None:
    _ROUTES.clear()


def scan_db(db_path: Path | str, queue_id: int, patch_prefix: str | None) -> Path:
    """The file to open for a scan of this scope: a registered replica, else *db_path*."""
    if not patch_prefix:
        return Path(db_path)
    return _ROUTES.get(_route_key(db_path, queue_id, patch_prefix), Path(db_path))


# ---------------------------------------------------------------------------
# Slimming
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def slim_participants(raw: str | None) -> tuple[str | None, str | None, bool]:
    """Return ``(lean_json, stats_json, poor)`` for one ``participants_json``.

    Anything that is not a JSON list of dicts is passed through untouched to
    both copies, so a consumer sees exactly what it would have seen in the
    source (including its own error handling for malformed rows).  *poor* marks
    a payload the writer may still upgrade in place.
    """
    if not raw:
        return raw, raw, True
    try:
        participants = json.loads(raw)
    except (TypeError, ValueError):
        return raw, raw, False
    if not isinstance(participants, list) or not all(isinstance(p, dict) for p in participants):
        return raw, raw, False
    lean: list[dict] = []
    stats_rows: list[dict] = []
    has_stats = False
    for participant in participants:
        lean.append({k: v for k, v in participant.items() if k not in LEAN_DROP_KEYS})
        slim: dict[str, Any] = {
            k: participant[k] for k in STATS_PARTICIPANT_KEYS if k in participant
        }
        stats = participant.get("stats")
        if isinstance(stats, dict):
            if stats:
                has_stats = True
            slim["stats"] = {k: v for k, v in stats.items() if k in STATS_KEEP_KEYS}
        elif "stats" in participant:
            slim["stats"] = stats
        stats_rows.append(slim)
    poor = (not has_stats) or len(participants) < FULL_TEAM_SIZE
    return _dumps(lean), _dumps(stats_rows), poor


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchExtract:
    lean_db: Path
    stats_db: Path
    games: int
    added: int
    rechecked: int
    rebuilt: bool
    seconds: float


def extract_paths(
    queue_id: int, patch_prefix: str, extract_dir: Path | None = None
) -> tuple[Path, Path]:
    base = extract_dir or DEFAULT_EXTRACT_DIR
    safe = str(patch_prefix).replace("/", "_").replace("\\", "_")
    stem = f"q{int(queue_id)}-{safe}"
    return base / f"{stem}.lean.db", base / f"{stem}.stats.db"


def _unlink(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _open_replica(lean_db: Path, stats_db: Path) -> sqlite3.Connection:
    lean_db.parent.mkdir(parents=True, exist_ok=True)
    # Rollback-journal mode (the default) on purpose: a transaction spanning the
    # main and the attached file commits atomically, which WAL does not give.
    con = sqlite3.connect(str(lean_db), timeout=60.0)
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-65536")
    con.execute("ATTACH DATABASE ? AS st", (str(stats_db),))
    con.executescript(_SCHEMA.format(schema="main"))
    con.executescript(_SCHEMA.format(schema="st"))
    con.execute("CREATE TABLE IF NOT EXISTS extract_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS extract_poor (game_id TEXT PRIMARY KEY)")
    con.commit()
    return con


def _read_meta(con: sqlite3.Connection) -> dict[str, str]:
    return {str(k): str(v) for k, v in con.execute("SELECT key, value FROM extract_meta")}


def _write_meta(con: sqlite3.Connection, meta: dict[str, Any]) -> None:
    con.executemany(
        "INSERT INTO extract_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(k, str(v)) for k, v in meta.items()],
    )


def _apply_rows(con: sqlite3.Connection, rows: Iterable[tuple], *, log: Log, label: str) -> int:
    """Upsert source rows into both copies; returns how many rows were written."""
    insert = (
        "INSERT OR REPLACE INTO {table} (" + ", ".join(_COLUMNS) + ") VALUES ("
        + ", ".join("?" for _ in _COLUMNS) + ")"
    )
    lean_batch: list[tuple] = []
    stats_batch: list[tuple] = []
    poor_add: list[tuple[str]] = []
    poor_drop: list[tuple[str]] = []
    written = 0
    started = time.monotonic()

    def flush() -> None:
        if lean_batch:
            con.executemany(insert.format(table="main.games"), lean_batch)
            con.executemany(insert.format(table="st.games"), stats_batch)
        if poor_add:
            con.executemany("INSERT OR IGNORE INTO extract_poor(game_id) VALUES (?)", poor_add)
        if poor_drop:
            con.executemany("DELETE FROM extract_poor WHERE game_id=?", poor_drop)
        lean_batch.clear()
        stats_batch.clear()
        poor_add.clear()
        poor_drop.clear()

    for row in rows:
        head = row[1:-1]
        lean_json, stats_json, poor = slim_participants(row[-1])
        lean_batch.append((*head, lean_json))
        stats_batch.append((*head, stats_json))
        (poor_add if poor else poor_drop).append((str(row[1]),))
        written += 1
        if len(lean_batch) >= _BATCH:
            flush()
        if written % 100_000 == 0:
            rate = written / max(time.monotonic() - started, 1e-9)
            log(f"[extract] {label}: {written:,} rows ({rate:,.0f}/s)")
    flush()
    return written


def sync_patch_extract(
    source_db: Path | str,
    *,
    queue_id: int,
    patch_prefix: str,
    extract_dir: Path | None = None,
    log: Log = print,
    prune_other_patches: bool = True,
) -> PatchExtract:
    """Bring the (queue, patch) replica up to date with *source_db* and return it.

    Raises on any inconsistency it cannot repair; the caller falls back to the
    source DB.
    """
    started = time.monotonic()
    source_db = Path(source_db)
    queue_id = int(queue_id)
    lo, hi = patch_range(patch_prefix)
    lean_db, stats_db = extract_paths(queue_id, patch_prefix, extract_dir)
    identity = {
        "version": str(EXTRACT_VERSION),
        "source": str(source_db.resolve()),
        "queue_id": str(queue_id),
        "patch_prefix": str(patch_prefix),
    }
    scope = "queue_id=? AND patch>=? AND patch<?"
    scope_params = (queue_id, lo, hi)

    src = sqlite3.connect(str(source_db), timeout=60.0)
    try:
        src.execute("PRAGMA query_only=1")
        # One read snapshot for the whole sync: the tail read, the poor re-reads
        # and the row count below all describe the same instant.
        src.execute("BEGIN")
        max_rowid = int(src.execute("SELECT COALESCE(MAX(rowid), 0) FROM games").fetchone()[0])
        source_games = int(
            src.execute(f"SELECT COUNT(*) FROM games WHERE {scope}", scope_params).fetchone()[0]
        )

        added = rechecked = 0
        rebuilt = False
        for attempt in ("incremental", "full"):
            usable = lean_db.exists() and stats_db.exists()
            con = _open_replica(lean_db, stats_db) if usable else None
            meta = _read_meta(con) if con is not None else {}
            if con is not None and any(meta.get(k) != v for k, v in identity.items()):
                usable = False
            watermark = int(meta.get("max_rowid", "-1") or -1)
            if attempt == "full" or not usable or watermark < 0 or watermark > max_rowid:
                if con is not None:
                    con.close()
                _unlink(lean_db)
                _unlink(stats_db)
                con = _open_replica(lean_db, stats_db)
                rebuilt = True
                log(
                    f"[extract] building {lean_db.name} from scratch "
                    f"({source_games:,} games; one full pass over the source)"
                )
                con.execute("BEGIN")
                added = _apply_rows(
                    con,
                    src.execute(f"{_SELECT} WHERE {scope}", scope_params),
                    log=log,
                    label="full",
                )
                rechecked = 0
            else:
                assert con is not None
                con.execute("BEGIN")
                # NOT INDEXED: walk the rowid tail, do not seek the patch index
                # (that would visit every game of the patch to test its rowid).
                added = _apply_rows(
                    con,
                    src.execute(
                        f"{_SELECT} NOT INDEXED WHERE rowid>? AND rowid<=? AND {scope}",
                        (watermark, max_rowid, *scope_params),
                    ),
                    log=log,
                    label="tail",
                )
                poor_ids = [
                    str(r[0])
                    for r in con.execute(
                        "SELECT game_id FROM extract_poor LIMIT ?", (POOR_RECHECK_LIMIT,)
                    )
                ]

                def _poor_rows() -> Iterable[tuple]:
                    for game_id in poor_ids:
                        row = src.execute(
                            f"{_SELECT} WHERE game_id=? AND {scope}", (game_id, *scope_params)
                        ).fetchone()
                        if row is not None:
                            yield row

                rechecked = _apply_rows(con, _poor_rows(), log=log, label="recheck")
            replica_games = int(con.execute("SELECT COUNT(*) FROM main.games").fetchone()[0])
            stats_games = int(con.execute("SELECT COUNT(*) FROM st.games").fetchone()[0])
            if replica_games == source_games == stats_games:
                _write_meta(con, {**identity, "max_rowid": max_rowid, "games": source_games})
                con.commit()
                con.close()
                break
            con.rollback()
            con.close()
            if rebuilt:
                raise RuntimeError(
                    f"patch extract disagrees with source after a full rebuild: "
                    f"lean={replica_games} stats={stats_games} source={source_games}"
                )
            log(
                f"[extract] replica has {replica_games:,} games, source {source_games:,}: "
                "rebuilding from scratch"
            )
    finally:
        src.close()

    if prune_other_patches:
        for stale in lean_db.parent.glob(f"q{queue_id}-*.db"):
            if stale not in (lean_db, stats_db):
                _unlink(stale)
                log(f"[extract] removed stale replica {stale.name}")

    seconds = time.monotonic() - started
    log(
        f"[extract] {lean_db.name}: {source_games:,} games "
        f"(+{added:,} new, {rechecked:,} rechecked, {'rebuilt' if rebuilt else 'incremental'}) "
        f"in {seconds:.1f}s"
    )
    return PatchExtract(
        lean_db=lean_db,
        stats_db=stats_db,
        games=source_games,
        added=added,
        rechecked=rechecked,
        rebuilt=rebuilt,
        seconds=seconds,
    )
