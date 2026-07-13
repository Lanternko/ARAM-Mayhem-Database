"""Meta Pick scoring, validation, snapshot loading, and leaderboard storage.

Server re-scores every submitted run against the configured tier-list JSON
snapshot (C(10,5)=252 combinations). Client-reported ranks/scores are ignored.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, ensure_public_schema

POOL_SIZE = 10
PICK_SIZE = 5
TOTAL_COMBOS = 252  # C(10, 5)
TIE_EPS = 1e-12
EST_WR_LO = 0.35
EST_WR_HI = 0.65
NICK_MIN = 2
NICK_MAX = 16
DEFAULT_RATE_LIMIT_PER_HOUR = 30

# High-precision UTC fallback when created_at is omitted (SQLite %f = ms).
# Prefer Python-generated microsecond timestamps on every insert/update.
CREATE_META_PICK_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS meta_pick_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname        TEXT NOT NULL,
    nickname_key    TEXT NOT NULL,
    patch           TEXT NOT NULL,
    avg_rank        REAL NOT NULL,
    ranks_json      TEXT NOT NULL,
    rounds_json     TEXT NOT NULL,
    total_combos    INTEGER NOT NULL DEFAULT 252,
    run_fp          TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(nickname_key, patch)
);
"""

CREATE_META_PICK_RUNS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_meta_pick_runs_patch_avg_created
ON meta_pick_runs(patch, avg_rank ASC, created_at DESC, id DESC);
"""

# One leaderboard row per unique 5-round replay (pool+picks) × patch.
# Blocks "change nickname → re-upload the same run".
CREATE_META_PICK_RUN_FP_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_pick_runs_fp_patch
ON meta_pick_runs(run_fp, patch)
WHERE run_fp IS NOT NULL AND run_fp != '';
"""

# Thread-safe snapshot cache: identity = (resolved path, mtime_ns, size).
_snapshot_cache_lock = threading.Lock()
_snapshot_cache_identity: tuple[str, int, int] | None = None
_snapshot_cache_data: dict[str, Any] | None = None

_WHITESPACE_RE = re.compile(r"\s+")


class MetaPickError(Exception):
    """Base error with an HTTP-ish status code and public detail string."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SnapshotUnavailable(MetaPickError):
    def __init__(self, detail: str = "meta-pick snapshot unavailable") -> None:
        super().__init__(detail, status_code=503)


class PatchMismatch(MetaPickError):
    def __init__(self, submitted: str, snapshot: str) -> None:
        super().__init__(
            f"patch mismatch: submitted {submitted!r}, snapshot {snapshot!r}",
            status_code=409,
        )
        self.submitted = submitted
        self.snapshot = snapshot


@dataclass(frozen=True)
class ScoredRound:
    pool_ids: list[str]
    picked_ids: list[str]
    rank: int
    total: int
    user_score: float
    best_score: float


@dataclass(frozen=True)
class UpsertResult:
    updated: bool
    entry: dict[str, Any]
    retained: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Nickname
# ---------------------------------------------------------------------------

def normalize_nickname_display(raw: str) -> str:
    """Trim ends and collapse internal whitespace; preserve original case."""
    if raw is None:
        raise MetaPickError("nickname is required")
    text = _WHITESPACE_RE.sub(" ", str(raw).strip())
    if not text:
        raise MetaPickError("nickname is required")
    n = len(text)  # Unicode code points (str is UCS-4 / UTF-32 code points in Py3)
    if n < NICK_MIN or n > NICK_MAX:
        raise MetaPickError(
            f"nickname must be {NICK_MIN}..{NICK_MAX} characters after trim"
        )
    return text


def nickname_key(display: str) -> str:
    """NFKC + casefold key for best-per-nickname uniqueness."""
    return unicodedata.normalize("NFKC", display).casefold()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def default_snapshot_path() -> Path:
    env = os.environ.get("ARAM_META_PICK_SNAPSHOT", "").strip()
    if env:
        return Path(env)
    # Prefer the published split payload; fall back to a sibling site path.
    candidates = (
        Path("docs/api/tier-list.json"),
        Path("data/site/tier-list.json"),
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def utc_now_iso() -> str:
    """UTC ISO-8601 timestamp with microseconds (lexicographically sortable)."""
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")
        + "Z"
    )


def clear_snapshot_cache() -> None:
    """Drop the in-process snapshot cache (tests / explicit invalidation)."""
    global _snapshot_cache_identity, _snapshot_cache_data
    with _snapshot_cache_lock:
        _snapshot_cache_identity = None
        _snapshot_cache_data = None


def _snapshot_file_identity(snap_path: Path) -> tuple[str, int, int]:
    resolved = snap_path.resolve()
    st = resolved.stat()
    return (str(resolved), int(st.st_mtime_ns), int(st.st_size))


def _parse_snapshot_file(snap_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotUnavailable(f"snapshot unreadable: {snap_path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("champs"), dict):
        raise SnapshotUnavailable("snapshot missing champs")
    patch = data.get("patch_prefix") or data.get("patch") or ""
    if not str(patch).strip():
        raise SnapshotUnavailable("snapshot missing patch_prefix")
    # Fresh dict so callers cannot mutate the raw json.loads mapping before cache.
    out = dict(data)
    out["patch_prefix"] = str(patch).strip()
    return out


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Load tier-list JSON snapshot with a small process-local cache.

    Cache key is resolved path + (mtime_ns, size). Returns the cached dict on
    hit — callers must treat it as read-only (do not mutate).
    """
    global _snapshot_cache_identity, _snapshot_cache_data
    snap_path = path or default_snapshot_path()
    if not snap_path.is_file():
        raise SnapshotUnavailable(f"snapshot not found: {snap_path}")
    try:
        identity = _snapshot_file_identity(snap_path)
    except OSError as exc:
        raise SnapshotUnavailable(f"snapshot unreadable: {snap_path}") from exc

    with _snapshot_cache_lock:
        if (
            _snapshot_cache_identity == identity
            and _snapshot_cache_data is not None
        ):
            return _snapshot_cache_data

    # Parse outside the lock so concurrent first-loads can both parse; the
    # loser of the write race just leaves an equivalent cache entry.
    data = _parse_snapshot_file(snap_path)

    with _snapshot_cache_lock:
        # Re-check identity in case the file changed while we parsed.
        try:
            current = _snapshot_file_identity(snap_path)
        except OSError:
            current = identity
        if current == identity:
            _snapshot_cache_identity = identity
            _snapshot_cache_data = data
            return _snapshot_cache_data
        # File moved under us; return this parse without caching stale identity.
        return data


def snapshot_patch(snapshot: dict[str, Any]) -> str:
    return str(snapshot.get("patch_prefix") or snapshot.get("patch") or "").strip()


# ---------------------------------------------------------------------------
# Canonical ID order (Meta Pick only — order-invariant scoring)
# ---------------------------------------------------------------------------

def _id_sort_key(cid: Any) -> tuple[int, int | str]:
    s = str(cid)
    try:
        return (0, int(s))
    except (TypeError, ValueError):
        return (1, s)


def canonical_ids(ids: list[Any] | tuple[Any, ...]) -> list[str]:
    return [str(x) for x in sorted(ids, key=_id_sort_key)]


def run_fingerprint(
    rounds: list[dict[str, Any]] | list[tuple[list[str], list[str]]],
) -> str:
    """Stable SHA-256 of the five-round replay (canonical pool + picks only).

    Order of champions within a round is ignored; round order is kept (R1…R5).
    """
    parts: list[dict[str, list[str]]] = []
    for rnd in rounds:
        if isinstance(rnd, dict):
            pool = canonical_ids(rnd.get("pool_ids") or [])
            picked = canonical_ids(rnd.get("picked_ids") or [])
        else:
            pool = canonical_ids(rnd[0])
            picked = canonical_ids(rnd[1])
        parts.append({"pool_ids": pool, "picked_ids": picked})
    blob = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JS-parity team scoring (order-invariant after canonical_ids)
# ---------------------------------------------------------------------------

def _ad_bin(ad_share: float) -> str:
    if ad_share < 0.35:
        return "<35% AD"
    if ad_share < 0.45:
        return "35-45% AD"
    if ad_share < 0.55:
        return "45-55% AD"
    if ad_share < 0.65:
        return "55-65% AD"
    return ">=65% AD"


def _count_group(projected_count: float) -> str:
    if projected_count < 0.5:
        return "0"
    if projected_count < 1.5:
        return "1"
    return "2+"


def _front_group(projected_count: float) -> str:
    return _count_group(projected_count) + " front"


def _table_value(config: dict[str, Any], name: str, key: str) -> float:
    tables = config.get("tables") or {}
    table = tables.get(name) or {}
    raw = table.get(key)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def team_composition(ids: list[str], snapshot: dict[str, Any]) -> dict[str, Any]:
    champs = snapshot.get("champs") or {}
    config = snapshot.get("recommendation_composition") or {}
    thresholds = config.get("lack_thresholds") or {}
    sums = {
        "phys": 0.0,
        "magic": 0.0,
        "true": 0.0,
        "wave": 0.0,
        "cc": 0.0,
        "engage": 0.0,
        "damage": 0.0,
        "poke": 0.0,
        "sustain": 0.0,
        "front": 0.0,
    }
    roles = {"Mage": 0, "Marksman": 0}
    front_count = 0
    for raw_id in ids:
        info = champs.get(str(raw_id)) or champs.get(raw_id)
        if not info:
            continue
        comp = info.get("comp") or {}
        for key in sums:
            try:
                sums[key] += float(comp.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            if float(comp.get("front") or 0) >= 2.0:
                front_count += 1
        except (TypeError, ValueError):
            pass
        for tag in info.get("tags") or []:
            if tag in roles:
                roles[tag] += 1

    size = max(1, len(ids))
    projection = 5 / size
    threshold_scale = size / 5
    ad_den = sums["phys"] + sums["magic"]
    ad_share = (sums["phys"] / ad_den) if ad_den > 0 else 0.5
    lacks: dict[str, bool] = {}
    for key in ("wave", "cc", "engage", "damage", "poke", "sustain", "front"):
        try:
            threshold = float(thresholds.get(key) or 0)
        except (TypeError, ValueError):
            threshold = 0.0
        lacks[key] = threshold > 0 and sums[key] < threshold * threshold_scale
    all_lacks = sum(1 for v in lacks.values() if v)
    return {
        "adShare": ad_share,
        "lacks": lacks,
        "sums": sums,
        "adBin": _ad_bin(ad_share),
        "frontGroup": _front_group(front_count * projection),
        "mageGroup": _count_group(roles["Mage"] * projection),
        "marksmanGroup": _count_group(roles["Marksman"] * projection),
        "waveGroup": "wave lack" if lacks["wave"] else "wave ok",
        "engageGroup": "engage lack" if lacks["engage"] else "engage ok",
        "pokeGroup": "poke lack" if lacks["poke"] else "poke ok",
        "allLacksGroup": _count_group(all_lacks * projection),
    }


def team_composition_score(ids: list[str], snapshot: dict[str, Any]) -> float:
    if not ids:
        return 0.0
    config = snapshot.get("recommendation_composition") or {}
    weights = config.get("table_weights") or {}
    try:
        clamp = float(config.get("clamp") or 0.05)
    except (TypeError, ValueError):
        clamp = 0.05
    comp = team_composition(ids, snapshot)
    score = 0.0
    score += float(weights.get("ad_front") or 0) * _table_value(
        config, "ad_front", f"{comp['frontGroup']}|{comp['adBin']}"
    )
    score += float(weights.get("poke_front") or 0) * _table_value(
        config, "poke_front", f"{comp['frontGroup']}|{comp['pokeGroup']}"
    )
    score += float(weights.get("wave_engage") or 0) * _table_value(
        config, "wave_engage", f"{comp['waveGroup']}|{comp['engageGroup']}"
    )
    score += float(weights.get("all_lacks") or 0) * _table_value(
        config, "all_lacks", comp["allLacksGroup"]
    )
    score += float(weights.get("mage_ad") or 0) * _table_value(
        config, "mage_ad", f"{comp['mageGroup']}|{comp['adBin']}"
    )
    score += float(weights.get("marksman_ad") or 0) * _table_value(
        config, "marksman_ad", f"{comp['marksmanGroup']}|{comp['adBin']}"
    )
    size_weight = min(1.0, max(0.0, (len(ids) - 1) / 4))
    clamped = max(-clamp, min(clamp, score)) * size_weight
    return clamped


def _pair_entry(champs: dict[str, Any], from_id: str, to_id: str) -> dict[str, Any] | None:
    """Look up one directed pair row from_id → to_id (JS pairEntry parity)."""
    info = champs.get(from_id) or champs.get(
        int(from_id) if from_id.isdigit() else from_id
    )
    if not info:
        return None
    want = str(to_id)
    for p in info.get("pairs") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("id")) == want:
            return p
    return None


def pair_lift_between(
    champs: dict[str, Any], a_id: str, b_id: str
) -> float | None:
    """Unordered pair lift for {a,b} — JS pairLiftBetween parity.

    Each champ only stores a short top-pairs list, so A→B and B→A are often
    missing one side. Prefer the average when both exist; otherwise the single
    known direction. Order-invariant.
    """
    ab = _pair_entry(champs, a_id, b_id)
    ba = _pair_entry(champs, b_id, a_id)
    if not ab and not ba:
        return None
    if ab and ba:
        try:
            la = float(ab.get("lift") or 0)
        except (TypeError, ValueError):
            la = 0.0
        try:
            lb = float(ba.get("lift") or 0)
        except (TypeError, ValueError):
            lb = 0.0
        return (la + lb) / 2.0
    p = ab or ba
    assert p is not None
    try:
        return float(p.get("lift") or 0)
    except (TypeError, ValueError):
        return 0.0


def score_team(ids: list[Any], snapshot: dict[str, Any]) -> float:
    """estWr = clamp(mean WR + mean known pair lift + composition score).

    IDs are canonically sorted and pair lifts are bidirectional so Meta Pick
    scoring is order-invariant (matches site.js evaluateFullTeam + pairLiftBetween).
    """
    list_ids = canonical_ids(ids)
    if not list_ids:
        return 0.5
    champs = snapshot.get("champs") or {}
    wr_sum = 0.0
    wr_n = 0
    for cid in list_ids:
        info = champs.get(cid) or champs.get(int(cid) if cid.isdigit() else cid)
        if not info:
            continue
        try:
            wr = float(info.get("wr"))
        except (TypeError, ValueError):
            continue
        if wr == wr:  # not NaN
            wr_sum += wr
            wr_n += 1
    base_wr = (wr_sum / wr_n) if wr_n else 0.5

    lift_sum = 0.0
    pair_n = 0
    for i, a in enumerate(list_ids):
        for j in range(i + 1, len(list_ids)):
            b = list_ids[j]
            hit = pair_lift_between(champs, a, b)
            if hit is None:
                continue
            lift_sum += hit
            pair_n += 1
    pair_lift = (lift_sum / pair_n) if pair_n else 0.0
    composition_score = team_composition_score(list_ids, snapshot)
    est_raw = base_wr + pair_lift + composition_score
    return max(EST_WR_LO, min(EST_WR_HI, est_raw))


def combinations_of(pool: list[str], k: int = PICK_SIZE) -> list[tuple[str, ...]]:
    return list(itertools.combinations(pool, k))


def score_all_teams(
    pool: list[Any], snapshot: dict[str, Any], *, k: int = PICK_SIZE
) -> tuple[list[float], float, list[str]]:
    """Return (all scores in combo order, best score, best ids)."""
    pool_s = [str(x) for x in pool]
    combos = combinations_of(pool_s, k)
    scores: list[float] = []
    best_score = float("-inf")
    best_ids: list[str] = []
    best_key = ""
    for combo in combos:
        ids = list(combo)
        sc = score_team(ids, snapshot)
        scores.append(sc)
        key = ",".join(canonical_ids(ids))
        if sc > best_score + TIE_EPS or (
            abs(sc - best_score) <= TIE_EPS and (not best_ids or key < best_key)
        ):
            best_score = sc
            best_ids = canonical_ids(ids)
            best_key = key
    if not scores:
        return [], 0.5, []
    return scores, best_score, best_ids


def rank_among(user_score: float, all_scores: list[float]) -> int:
    """rank 1 = best; rank = count(better) + 1 with tie tolerance 1e-12."""
    if not all_scores:
        return 1
    u = float(user_score)
    better = 0
    for s in all_scores:
        if float(s) > u + TIE_EPS:
            better += 1
    return better + 1


def score_round(
    pool_ids: list[Any],
    picked_ids: list[Any],
    snapshot: dict[str, Any],
) -> ScoredRound:
    pool = [str(x) for x in pool_ids]
    picked = [str(x) for x in picked_ids]
    scores, best_score, _best_ids = score_all_teams(pool, snapshot)
    user_score = score_team(picked, snapshot)
    rank = rank_among(user_score, scores)
    return ScoredRound(
        pool_ids=pool,
        picked_ids=picked,
        rank=rank,
        total=len(scores),
        user_score=user_score,
        best_score=best_score if scores else 0.5,
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _as_id_list(value: Any, *, field: str, size: int) -> list[str]:
    if not isinstance(value, list):
        raise MetaPickError(f"{field} must be a list of {size} ids")
    if len(value) != size:
        raise MetaPickError(f"{field} must have exactly {size} ids")
    out = [str(x) for x in value]
    if any(not x for x in out):
        raise MetaPickError(f"{field} contains empty id")
    if len(set(out)) != len(out):
        raise MetaPickError(f"{field} ids must be unique")
    return out


def validate_round_input(
    round_obj: Any,
    snapshot: dict[str, Any],
    *,
    index: int,
) -> tuple[list[str], list[str]]:
    if not isinstance(round_obj, dict):
        raise MetaPickError(f"rounds[{index}] must be an object")
    pool = _as_id_list(round_obj.get("pool_ids"), field=f"rounds[{index}].pool_ids", size=POOL_SIZE)
    picked = _as_id_list(
        round_obj.get("picked_ids"), field=f"rounds[{index}].picked_ids", size=PICK_SIZE
    )
    pool_set = set(pool)
    if not set(picked).issubset(pool_set):
        raise MetaPickError(f"rounds[{index}].picked_ids must be a subset of pool_ids")
    champs = snapshot.get("champs") or {}
    known = set(str(k) for k in champs.keys())
    for cid in pool:
        if cid not in known:
            raise MetaPickError(f"rounds[{index}]: unknown champion id {cid}")
    return pool, picked


def validate_submit_payload(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[str, str, list[tuple[list[str], list[str]]]]:
    nickname = normalize_nickname_display(payload.get("nickname") or "")
    patch = str(payload.get("patch") or "").strip()
    if not patch:
        raise MetaPickError("patch is required")
    snap_patch = snapshot_patch(snapshot)
    if patch != snap_patch:
        raise PatchMismatch(patch, snap_patch)
    rounds = payload.get("rounds")
    if not isinstance(rounds, list) or len(rounds) != 5:
        raise MetaPickError("rounds must be a list of exactly 5 items")
    parsed: list[tuple[list[str], list[str]]] = []
    for i, rnd in enumerate(rounds):
        parsed.append(validate_round_input(rnd, snapshot, index=i))
    return nickname, patch, parsed


def recompute_run(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Validate + recompute all ranks. Ignores any client score fields."""
    nickname, patch, rounds = validate_submit_payload(payload, snapshot)
    scored: list[ScoredRound] = []
    for pool, picked in rounds:
        sc = score_round(pool, picked, snapshot)
        if sc.total != TOTAL_COMBOS:
            raise MetaPickError(
                f"expected {TOTAL_COMBOS} combinations, got {sc.total}"
            )
        scored.append(sc)
    ranks = [s.rank for s in scored]
    avg_rank = sum(ranks) / len(ranks)
    rounds_out = [
        {
            "pool_ids": s.pool_ids,
            "picked_ids": s.picked_ids,
            "rank": s.rank,
            "total": s.total,
            "user_score": s.user_score,
            "best_score": s.best_score,
        }
        for s in scored
    ]
    return {
        "nickname": nickname,
        "nickname_key": nickname_key(nickname),
        "patch": patch,
        "ranks": ranks,
        "avg_rank": avg_rank,
        "total_combos": TOTAL_COMBOS,
        "run_fp": run_fingerprint(rounds_out),
        "rounds": rounds_out,
    }


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def ensure_meta_pick_schema(con: sqlite3.Connection) -> None:
    ensure_public_schema(con)
    con.execute(CREATE_META_PICK_RUNS_SQL)
    con.execute(CREATE_META_PICK_RUNS_INDEX_SQL)
    # Migrate older DBs created before run_fp existed.
    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(meta_pick_runs)").fetchall()}
    if "run_fp" not in cols:
        con.execute("ALTER TABLE meta_pick_runs ADD COLUMN run_fp TEXT")
    con.execute(CREATE_META_PICK_RUN_FP_INDEX_SQL)
    con.commit()


def _row_to_entry(row: sqlite3.Row | tuple) -> dict[str, Any]:
    """Public leaderboard/API entry dict (omits internal nickname_key)."""
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        # id, nickname, nickname_key, patch, avg_rank, ranks_json, rounds_json,
        # total_combos, created_at
        keys = (
            "id",
            "nickname",
            "nickname_key",
            "patch",
            "avg_rank",
            "ranks_json",
            "rounds_json",
            "total_combos",
            "created_at",
        )
        d = {k: row[i] for i, k in enumerate(keys)}
    ranks = d.get("ranks_json")
    if isinstance(ranks, str):
        try:
            ranks = json.loads(ranks)
        except json.JSONDecodeError:
            ranks = []
    return {
        "id": int(d["id"]),
        "nickname": str(d["nickname"]),
        "patch": str(d["patch"]),
        "avg_rank": float(d["avg_rank"]),
        "ranks": ranks if isinstance(ranks, list) else [],
        "total_combos": int(d.get("total_combos") or TOTAL_COMBOS),
        "created_at": str(d["created_at"]),
    }


def upsert_best_run(db_path: Path, run: dict[str, Any]) -> UpsertResult:
    """Keep only the best (lowest avg_rank) per nickname_key×patch.

    Also enforces one row per run fingerprint×patch so the same 5-round replay
    cannot be re-uploaded under a different nickname.

    Equal score replaces (refresh to newer run). Worse returns retained best.

    Uses BEGIN IMMEDIATE so concurrent first-submits for the same key cannot
    both pass a SELECT-then-INSERT race on the unique constraint.
    """
    nick = str(run["nickname"])
    key = str(run["nickname_key"])
    patch = str(run["patch"])
    avg_rank = float(run["avg_rank"])
    ranks_json = json.dumps(run["ranks"], ensure_ascii=False, separators=(",", ":"))
    # Persist pool/picked only (no client-only fields); ranks are server-side.
    rounds_payload = [
        {"pool_ids": r["pool_ids"], "picked_ids": r["picked_ids"], "rank": r["rank"]}
        for r in run.get("rounds") or []
    ]
    rounds_json = json.dumps(rounds_payload, ensure_ascii=False, separators=(",", ":"))
    total_combos = int(run.get("total_combos") or TOTAL_COMBOS)
    run_fp = str(run.get("run_fp") or run_fingerprint(rounds_payload))

    con = connect(db_path)
    try:
        ensure_meta_pick_schema(con)
        con.row_factory = sqlite3.Row
        # Schema setup commits; start an exclusive write transaction for the
        # read/decision/write path so two concurrent first inserts serialize.
        # created_at is stamped only after this lock (and only on write paths)
        # so timestamps reflect serialized write order, not pre-lock wait time.
        con.execute("BEGIN IMMEDIATE")
        try:
            # Same replay already on the board under another nickname → hard reject.
            by_fp = con.execute(
                "SELECT * FROM meta_pick_runs WHERE run_fp = ? AND patch = ?",
                (run_fp, patch),
            ).fetchone()
            if by_fp is not None and str(by_fp["nickname_key"]) != key:
                con.rollback()
                raise MetaPickError(
                    "this run was already submitted — change nickname cannot re-upload the same score",
                    status_code=409,
                )

            existing = con.execute(
                "SELECT * FROM meta_pick_runs WHERE nickname_key = ? AND patch = ?",
                (key, patch),
            ).fetchone()
            if existing is not None:
                old_avg = float(existing["avg_rank"])
                if avg_rank > old_avg:
                    entry = _row_to_entry(existing)
                    # Release the write lock cleanly without writing.
                    # Worse score: no timestamp generation.
                    con.rollback()
                    return UpsertResult(
                        updated=False,
                        entry=entry,
                        retained=entry,
                    )
                # Stamp after decision + under the write lock.
                now_iso = utc_now_iso()
                con.execute(
                    """
                    UPDATE meta_pick_runs
                    SET nickname = ?, avg_rank = ?, ranks_json = ?, rounds_json = ?,
                        total_combos = ?, run_fp = ?, created_at = ?
                    WHERE nickname_key = ? AND patch = ?
                    """,
                    (
                        nick,
                        avg_rank,
                        ranks_json,
                        rounds_json,
                        total_combos,
                        run_fp,
                        now_iso,
                        key,
                        patch,
                    ),
                )
            else:
                # Stamp after decision + under the write lock.
                now_iso = utc_now_iso()
                con.execute(
                    """
                    INSERT INTO meta_pick_runs (
                        nickname, nickname_key, patch, avg_rank, ranks_json,
                        rounds_json, total_combos, run_fp, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nick,
                        key,
                        patch,
                        avg_rank,
                        ranks_json,
                        rounds_json,
                        total_combos,
                        run_fp,
                        now_iso,
                    ),
                )
            row = con.execute(
                "SELECT * FROM meta_pick_runs WHERE nickname_key = ? AND patch = ?",
                (key, patch),
            ).fetchone()
            assert row is not None
            entry = _row_to_entry(row)
            con.commit()
            return UpsertResult(updated=True, entry=entry, retained=None)
        except MetaPickError:
            con.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            con.rollback()
            # Concurrent insert of the same run_fp×patch (or nick×patch).
            raise MetaPickError(
                "this run was already submitted — change nickname cannot re-upload the same score",
                status_code=409,
            ) from exc
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()


def list_leaderboard(
    db_path: Path,
    *,
    patch: str,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(100, int(limit)))
    con = connect(db_path)
    try:
        ensure_meta_pick_schema(con)
        con.row_factory = sqlite3.Row
        total_row = con.execute(
            "SELECT COUNT(*) AS n FROM meta_pick_runs WHERE patch = ?",
            (patch,),
        ).fetchone()
        total = int(total_row["n"] if total_row else 0)
        rows = con.execute(
            """
            SELECT * FROM meta_pick_runs
            WHERE patch = ?
            ORDER BY avg_rank ASC, created_at DESC, id DESC
            LIMIT ?
            """,
            (patch, limit),
        ).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return {"patch": patch, "total": total, "entries": entries}
    finally:
        con.close()


def submit_run(db_path: Path, payload: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    run = recompute_run(payload, snapshot)
    result = upsert_best_run(db_path, run)
    return {
        "ok": True,
        "updated": result.updated,
        "avg_rank": run["avg_rank"],
        "ranks": run["ranks"],
        "total_combos": TOTAL_COMBOS,
        "entry": result.entry,
        "retained": result.retained,
        "patch": run["patch"],
    }


# ---------------------------------------------------------------------------
# Best-effort in-process rate limit (single-process only; not multi-worker safe)
# ---------------------------------------------------------------------------

class InProcessRateLimiter:
    """Sliding-window counter keyed by opaque client id (usually remote host).

    Does not persist raw IPs. Limited to the current process — multiple workers
    each get their own budget.
    """

    def __init__(self, limit_per_hour: int) -> None:
        self.limit = max(0, int(limit_per_hour))
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        window = 3600.0
        bucket = self._hits.setdefault(key, [])
        cutoff = now - window
        # Drop expired timestamps in place.
        i = 0
        while i < len(bucket) and bucket[i] < cutoff:
            i += 1
        if i:
            del bucket[:i]
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        # Opportunistic prune of empty keys when map grows.
        if len(self._hits) > 4096:
            stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
            for k in stale[:512]:
                self._hits.pop(k, None)
        return True


def rate_limit_per_hour() -> int:
    raw = os.environ.get("ARAM_META_PICK_RATE_LIMIT_PER_HOUR", "").strip()
    if raw == "":
        return DEFAULT_RATE_LIMIT_PER_HOUR
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_HOUR


def trust_proxy() -> bool:
    return os.environ.get("ARAM_META_PICK_TRUST_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def client_key_from_request(request: Any) -> str:
    """Derive a rate-limit key. Trust X-Forwarded-For only when configured."""
    if trust_proxy():
        xff = None
        try:
            xff = request.headers.get("x-forwarded-for") or request.headers.get(
                "X-Forwarded-For"
            )
        except Exception:
            xff = None
        if xff:
            first = str(xff).split(",")[0].strip()
            if first:
                return first
    try:
        client = request.client
        if client is not None and getattr(client, "host", None):
            return str(client.host)
    except Exception:
        pass
    return "unknown"
