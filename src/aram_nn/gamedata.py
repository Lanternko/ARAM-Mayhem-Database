"""Canonical loader for the local LCU games DB (data/lcu/games.db).

One shared home for the data-access conventions that ~35 scripts under
scripts/ currently hand-roll (sqlite connect + queue/patch filter +
participants_json parsing).  New analysis scripts should import from here
instead of copy-pasting a loader; frozen scripts are left as-is.

Conventions this module encodes (see CLAUDE.md "NEVER"):
- Exact game dedupe is by ``game_id`` (the table's PRIMARY KEY) — never by
  champion-composition hash.
- ``blue_champs`` / ``red_champs`` are stored as JSON lists already sorted by
  championId ascending; they are returned as-is, never re-ordered by slot.
- ``patch_prefix`` filters with ``patch LIKE '<prefix>%'``; None/"" means all
  patches.
- A participant won iff ``(teamId == 100) == bool(blue_wins)``.

Usage:
    from aram_nn.gamedata import iter_games, load_games_df

    for g in iter_games("data/lcu/games.db", queue_id=2400, patch_prefix="16.13",
                        parse_participants=True):
        for p in g["participants"]:
            ...
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

DEFAULT_DB = Path("data/lcu/games.db")

_BASE_COLUMNS = (
    "game_id, patch, blue_wins, duration_sec, created_ms, blue_champs, red_champs"
)


def _connect_ro(db: Path | str) -> sqlite3.Connection:
    """Read-only connection so analysis scripts can never corrupt the
    collector's DB (which may be mid-write by the watchdog)."""
    return sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)


def _where(queue_id: int | None, patch_prefix: str | None) -> tuple[str, list]:
    clauses, params = [], []
    if queue_id is not None:
        clauses.append("queue_id=?")
        params.append(queue_id)
    if patch_prefix:
        clauses.append("patch LIKE ?")
        params.append(f"{patch_prefix}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


JADE_CHAMPION_ID_OFFSET = 60_000
JADE_CHAMPION_ID_RANGE = (60_000, 61_000)


def base_champion_id(champion_id: int) -> int:
    """Map a 經典 (queue 4310, gameMode JADE) champion id onto the normal one.

    That mode ships every champion as a separate ``Jade_*`` entry numbered
    ``60000 + base_id`` -- 60001 Jade_Annie for Annie (1), 60062 Jade_Wukong for
    MonkeyKing (62) -- so its games store ids no other queue uses.  Verified
    against the live LCU catalogue: all 60 entries map exactly, with only the
    two alias spellings differing (``FiddleSticks`` casing and Wukong's internal
    ``MonkeyKing``); the numeric offset itself is exact.

    Anything joining 4310 games against champion metadata must go through this,
    or all 60 champions silently become "unknown" and the games are dropped.
    Ids outside the Jade range are returned unchanged, so this is safe to apply
    unconditionally across mixed-queue data.
    """
    low, high = JADE_CHAMPION_ID_RANGE
    if low <= champion_id < high:
        return champion_id - JADE_CHAMPION_ID_OFFSET
    return champion_id


def is_jade_champion_id(champion_id: int) -> bool:
    """True for the 經典-mode champion id space (see base_champion_id)."""
    low, high = JADE_CHAMPION_ID_RANGE
    return low <= champion_id < high


def participant_won(participant: dict, blue_wins: int | bool) -> bool:
    """A participant won iff they are on blue (teamId=100) and blue won,
    or on red (200) and blue lost."""
    return (participant.get("teamId") == 100) == bool(blue_wins)


def iter_games(
    db: Path | str = DEFAULT_DB,
    queue_id: int | None = 2400,
    patch_prefix: str | None = None,
    *,
    parse_participants: bool = False,
    min_duration_sec: int = 0,
    ordered: bool | None = None,
) -> Iterator[dict]:
    """Yield one dict per game.

    Keys: game_id, patch, blue_wins, duration_sec, created_ms,
    blue_champs / red_champs (list[int], championId ascending), and — when
    ``parse_participants`` — participants (list[dict] with teamId /
    championId / augments / items / stats, or [] when the row has none).

    ``queue_id=None`` disables the queue filter (diagnostics only — bulk
    consumers should always pass a queue).

    ``ordered`` (created_ms ascending, for temporal train/val/test splits —
    never split randomly) defaults by use case, because the covering index is
    (queue_id, patch, created_ms) and ``ORDER BY created_ms`` always sorts:

    - light iteration (no participants): ordered=True — sorting the small
      columns is cheap, and time order is what splits need;
    - ``parse_participants``: ordered=False — aggregation scans don't need
      order, and this keeps the fast sequential table scan (measured ~2 min
      full DB, same as tierlist_engine).  Forcing ordered=True here switches
      to a two-pass sort-then-fetch-by-PK plan, which is seek-bound on the
      ~19 GB DB (~20 ms/game cold cache — ~75 min per patch); only use it
      when you truly need participants in time order.
    """
    if ordered is None:
        ordered = not parse_participants
    where, params = _where(queue_id, patch_prefix)
    if min_duration_sec:
        where += (" AND " if where else " WHERE ") + "duration_sec >= ?"
        params.append(min_duration_sec)
    order_sql = " ORDER BY created_ms" if ordered else ""
    con = _connect_ro(db)
    try:
        if parse_participants and ordered:
            # Two passes: sort the light columns, then fetch the ~25 KB/row
            # participants_json by PRIMARY KEY in that order (a single-pass
            # ORDER BY would drag the blobs through a multi-GB temp sort).
            cur = con.execute(
                f"SELECT {_BASE_COLUMNS} FROM games{where}{order_sql}", params
            )
            fetch = con.cursor()
            for row in cur:
                game = _light_row(row)
                pj = fetch.execute(
                    "SELECT participants_json FROM games WHERE game_id=?",
                    (row[0],),
                ).fetchone()[0]
                game["participants"] = _parse_pj(pj)
                yield game
        else:
            cols = _BASE_COLUMNS + (
                ", participants_json" if parse_participants else ""
            )
            cur = con.execute(f"SELECT {cols} FROM games{where}{order_sql}", params)
            for row in cur:
                game = _light_row(row)
                if parse_participants:
                    game["participants"] = _parse_pj(row[7])
                yield game
    finally:
        con.close()


def _light_row(row) -> dict:
    return {
        "game_id": row[0],
        "patch": row[1],
        "blue_wins": int(row[2]),
        "duration_sec": row[3],
        "created_ms": row[4],
        "blue_champs": json.loads(row[5]),
        "red_champs": json.loads(row[6]),
    }


def _parse_pj(pj) -> list:
    try:
        return json.loads(pj) if pj else []
    except (TypeError, ValueError):
        return []


def load_games_df(
    db: Path | str = DEFAULT_DB,
    queue_id: int | None = 2400,
    patch_prefix: str | None = None,
):
    """The same rows as :func:`iter_games` as a polars DataFrame (one row per
    game, created_ms ascending).  blue_champs / red_champs are list[int]
    columns.  polars is imported lazily so iter_games callers don't pay."""
    import polars as pl

    rows = list(iter_games(db, queue_id, patch_prefix))
    if not rows:
        return pl.DataFrame(
            schema={
                "game_id": pl.Utf8, "patch": pl.Utf8, "blue_wins": pl.Int64,
                "duration_sec": pl.Int64, "created_ms": pl.Int64,
                "blue_champs": pl.List(pl.Int64), "red_champs": pl.List(pl.Int64),
            }
        )
    return pl.DataFrame(rows)


def count_games(
    db: Path | str = DEFAULT_DB,
    queue_id: int | None = 2400,
    patch_prefix: str | None = None,
) -> int:
    where, params = _where(queue_id, patch_prefix)
    con = _connect_ro(db)
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM games{where}", params).fetchone()[0])
    finally:
        con.close()
