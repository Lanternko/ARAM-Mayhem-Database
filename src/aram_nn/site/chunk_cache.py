"""Per-chunk partial tallies for scans of the current-patch replica.

Every tier-list scan is a fold over the patch's games into counters, then a
finalize step that turns counters into the published numbers.  The fold is the
expensive part (~7 min of read + JSON parse per pass over 4M games, a dozen
passes per build) and almost all of it repeats work from the previous build:
between two builds only a few percent of the games are new.

So the replica (``aram_nn.site.patch_extract``) is cut into fixed rowid chunks,
each scan's fold result is cached per chunk, and a build only re-folds chunks
that changed since they were cached.  Change detection is exact, not
heuristic: triggers on the replica bump a per-chunk generation on every insert,
delete, update and ``INSERT OR REPLACE`` (which moves a re-checked game to the
tail and so changes two chunks).  A cached partial is keyed by

    (scan name, code + input fingerprint, replica id, chunk, generation)

and a rebuilt replica gets a fresh replica id, so nothing cached against an
older file can be reused by accident.

Merging is order-preserving (chunks in rowid order) and only defined for
additive states: ints/floats add, Counters/dicts merge key-wise, lists
concatenate, sets union, strings keep the later value (what a sequential
overwrite would leave).  A scan with another state passes its own ``merge``;
a scan whose fold is not additive at all must not use this.

Scans of anything other than a tracked replica (the source DB, a settled older
patch) take the plain path: one fold over the whole query, exactly as before.
"""
from __future__ import annotations

import hashlib
import pickle
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from aram_nn.site import patch_extract
from aram_nn.site.patch_extract import REPLICA_ID_KEY, partials_path, replica_tracks_chunks

# Bump to drop every cached partial (e.g. the merge rules change).
CACHE_VERSION = 1
# Partials not used by any build for this long are deleted.
PARTIAL_TTL_SEC = 3 * 86400

_CODE_FILES: tuple[Path, ...] = ()
_code_digest: str | None = None

Fold = Callable[[Iterable[tuple]], Any]
Merge = Callable[[Any, Any], Any]


def register_code_files(*paths: Path) -> None:
    """Source files whose bytes are part of every cache key.

    Any edit to a fold (or a helper it calls) must invalidate its partials;
    hashing the whole defining file is coarse but cannot miss a change.
    """
    global _CODE_FILES, _code_digest
    _CODE_FILES = tuple(sorted({*map(Path, _CODE_FILES), *map(Path, paths)}))
    _code_digest = None


def _code_fingerprint() -> str:
    global _code_digest
    if _code_digest is None:
        h = hashlib.sha256(f"v{CACHE_VERSION}".encode())
        for path in (Path(__file__), *_CODE_FILES):
            h.update(path.name.encode())
            h.update(path.read_bytes())
        _code_digest = h.hexdigest()
    return _code_digest


def _canonical(value: Any) -> Any:
    """A hash-seed-independent, order-stable form of *value* for fingerprints."""
    if isinstance(value, dict):
        return ("d", sorted(((repr(_canonical(k)), _canonical(v)) for k, v in value.items())))
    if isinstance(value, (set, frozenset)):
        return ("s", sorted(repr(_canonical(v)) for v in value))
    if isinstance(value, (list, tuple)):
        return ("l", [_canonical(v) for v in value])
    if isinstance(value, Path):
        return ("p", str(value))
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    raise TypeError(f"cannot fingerprint {type(value).__name__}; pass plain data")


def fingerprint(*parts: Any) -> str:
    h = hashlib.sha256(f"{_code_fingerprint()}:{patch_extract.CHUNK_ROWS}".encode())
    h.update(repr(_canonical(list(parts))).encode())
    return h.hexdigest()[:32]


def merge_into(acc: Any, part: Any) -> Any:
    """Add *part* into *acc* (mutating containers) and return the result."""
    if acc is None:
        return part
    if part is None:
        return acc
    if isinstance(acc, bool) or isinstance(part, bool):
        raise TypeError("bool partials are not additive")
    if isinstance(acc, (int, float)) and isinstance(part, (int, float)):
        return acc + part
    if isinstance(acc, Counter) and isinstance(part, Counter):
        # Counter.update adds but would drop nothing; keep zero/negative counts
        # exactly like the single-pass ``c[k] += 1`` loop would.
        for key, value in part.items():
            acc[key] = acc[key] + value if key in acc else value
        return acc
    if isinstance(acc, dict) and isinstance(part, dict):
        for key, value in part.items():
            if key in acc:
                acc[key] = merge_into(acc[key], value)
            else:
                acc[key] = value
        return acc
    if isinstance(acc, list) and isinstance(part, list):
        acc.extend(part)
        return acc
    if isinstance(acc, tuple) and isinstance(part, tuple) and len(acc) == len(part):
        return tuple(merge_into(a, b) for a, b in zip(acc, part))
    if isinstance(acc, set) and isinstance(part, set):
        acc |= part
        return acc
    if isinstance(acc, str) and isinstance(part, str):
        return part
    raise TypeError(f"cannot merge {type(acc).__name__} with {type(part).__name__}")


def _open_partials(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS partials ("
        " scan TEXT NOT NULL, fp TEXT NOT NULL, replica TEXT NOT NULL,"
        " chunk INTEGER NOT NULL, gen INTEGER NOT NULL, state BLOB NOT NULL,"
        " used_at REAL NOT NULL, PRIMARY KEY (scan, fp, chunk))"
    )
    con.commit()
    return con


def chunked_fold(
    replica: Path,
    *,
    scan: str,
    fp: str,
    sql: str,
    params: tuple,
    fold: Fold,
    merge: Merge | None = None,
    log: Callable[[str], Any] | None = None,
) -> Any:
    """Fold ``sql`` over *replica* chunk by chunk, reusing cached chunk partials.

    *sql* must be a ``SELECT ... FROM games WHERE ...`` with no ORDER BY / LIMIT;
    ``AND rowid >= ? AND rowid < ?`` is appended per chunk.  Returns the merged
    state (``None`` if there are no rows at all, same as folding nothing).
    """
    merge = merge or merge_into
    chunk_rows = int(patch_extract.CHUNK_ROWS)
    started = time.monotonic()
    src = sqlite3.connect(f"file:{replica}?mode=ro", uri=True, timeout=60.0)
    store = _open_partials(partials_path(replica))
    reused = folded = 0
    try:
        src.execute("BEGIN")  # one snapshot: generations and rows agree
        replica_id = src.execute(
            "SELECT value FROM extract_meta WHERE key=?", (REPLICA_ID_KEY,)
        ).fetchone()[0]
        max_rowid = int(src.execute("SELECT COALESCE(MAX(rowid), 0) FROM games").fetchone()[0])
        gens = dict(src.execute("SELECT chunk, gen FROM extract_chunk_gen"))
        cached = {
            int(chunk): (rid, int(gen), blob)
            for chunk, rid, gen, blob in store.execute(
                "SELECT chunk, replica, gen, state FROM partials WHERE scan=? AND fp=?",
                (scan, fp),
            )
        }
        # NOT INDEXED: with the patch predicate SQLite otherwise seeks the
        # (queue, patch, created) index and filters rowid, i.e. walks the whole
        # patch once per chunk.
        if " FROM games WHERE " not in sql:
            raise ValueError("chunked scans need 'SELECT ... FROM games WHERE ...'")
        chunk_sql = sql.replace(" FROM games WHERE ", " FROM games NOT INDEXED WHERE ", 1)
        chunk_sql = f"{chunk_sql} AND rowid >= ? AND rowid < ?"
        acc: Any = None
        for chunk in range(max_rowid // chunk_rows + 1):
            gen = int(gens.get(chunk, 0))
            hit = cached.get(chunk)
            if hit is not None and hit[0] == replica_id and hit[1] == gen:
                part = pickle.loads(hit[2])
                reused += 1
            else:
                lo = chunk * chunk_rows
                part = fold(src.execute(chunk_sql, (*params, lo, lo + chunk_rows)))
                store.execute(
                    "INSERT OR REPLACE INTO partials(scan, fp, replica, chunk, gen, state, used_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (scan, fp, replica_id, chunk, gen,
                     pickle.dumps(part, protocol=pickle.HIGHEST_PROTOCOL), time.time()),
                )
                store.commit()
                folded += 1
            acc = part if acc is None else merge(acc, part)
        # A scan may run under several fingerprints in one build (different
        # arguments), so superseded ones age out instead of being dropped at once.
        now = time.time()
        store.execute("UPDATE partials SET used_at=? WHERE scan=? AND fp=?", (now, scan, fp))
        store.execute(
            "DELETE FROM partials WHERE (scan=? AND fp=? AND chunk>?) OR used_at<?",
            (scan, fp, max_rowid // chunk_rows, now - PARTIAL_TTL_SEC),
        )
        store.commit()
    finally:
        src.rollback()
        src.close()
        store.close()
    if log is not None:
        log(
            f"[chunks] {scan}: {folded} folded, {reused} reused "
            f"in {time.monotonic() - started:.1f}s"
        )
    return acc


def fold_scan(
    target: Path,
    *,
    scan: str,
    fp_parts: tuple,
    sql: str,
    params: tuple,
    fold: Fold,
    merge: Merge | None = None,
    log: Callable[[str], Any] | None = None,
) -> Any:
    """Chunk-cached fold when *target* is a tracked replica, else one plain pass."""
    if replica_tracks_chunks(target):
        return chunked_fold(
            target, scan=scan, fp=fingerprint(scan, *fp_parts), sql=sql,
            params=params, fold=fold, merge=merge, log=log,
        )
    con = sqlite3.connect(str(target))
    try:
        return fold(con.execute(sql, params))
    finally:
        con.close()


def counter_dict() -> defaultdict:
    """``defaultdict(Counter)`` that pickles (no lambda)."""
    return defaultdict(Counter)
