"""Anchor-conditional champion x teammate-ROLE synergy stats.

Raw champion x champion pair synergy is winner's-curse noise: train->test
persistence is only r~0.17 (scripts/ablation_pair_synergy_persistence.py).
Pooling each teammate into its primary ROLE bucket (Tank / Mage / Marksman /
...) raises per-cell support by ~20x and lifts persistence to r~0.37
(scripts/ablation_champ_role_persistence.py), so this is the shipped same-team
chemistry signal that replaces raw pairs.

For an already-picked anchor champion A and a candidate X with primary role R:

    raw_delta(A, R) = WR(A's team has >=1 OTHER champ of role R)
                    - WR(A's team has NO other champ of role R)

The shipped delta is shrunk toward 0 by n/(n+k) (kills low-support cells) and
multiplied by a global persistence factor (~r, the conservative train->test
regression-to-mean haircut).

RoleSynergyStats.get(anchor_id, candidate_id) has the SAME signature as
PairSynergyStats.get: it resolves the candidate's role internally, so the
recommender's synergy combiners (_combine_synergy / _team_pair_synergy) are
drop-in unchanged.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SCHEMA_KIND = "champ_role_synergy"


@dataclass(frozen=True)
class RoleSynergyRow:
    anchor_id: int
    role: str
    games: int
    wins: int
    rest_games: int
    rest_wins: int
    pair_wr: float
    rest_wr: float
    raw_delta: float
    delta: float
    se: float


@dataclass
class RoleSynergyStats:
    rows: dict[tuple[int, str], RoleSynergyRow]
    role_by_champ: dict[int, str]
    min_pair: int = 0
    shrink_k: float = 150.0
    persistence_factor: float = 0.5
    roles: tuple[str, ...] = ()
    queue_id: int | None = None
    patch_prefix: str | None = None
    total_matches: int | None = None

    def role_of(self, champ_id: int) -> str | None:
        return self.role_by_champ.get(int(champ_id))

    def get_role(self, anchor_id: int, role: str) -> RoleSynergyRow | None:
        return self.rows.get((int(anchor_id), str(role)))

    def get(self, anchor_id: int, candidate_id: int) -> RoleSynergyRow | None:
        """Drop-in for PairSynergyStats.get: candidate is mapped to its role."""
        role = self.role_by_champ.get(int(candidate_id))
        if role is None:
            return None
        return self.rows.get((int(anchor_id), role))


def _team_rows_from_parquet(
    data_path: Path,
    *,
    min_duration_sec: int,
) -> tuple[list[tuple[list[int], int]], int]:
    import polars as pl  # local import: keeps the GUI loader free of polars

    df = pl.read_parquet(data_path)
    if min_duration_sec and "duration_sec" in df.columns:
        df = df.filter(pl.col("duration_sec") >= min_duration_sec)
    blue = df["blue_champions"].to_list()
    red = df["red_champions"].to_list()
    wins = df["blue_wins"].to_list()

    team_rows: list[tuple[list[int], int]] = []
    for b, r, bw in zip(blue, red, wins):
        won = 1 if bw else 0
        team_rows.append(([int(c) for c in b], won))
        team_rows.append(([int(c) for c in r], 1 - won))
    return team_rows, len(blue)


def build_champ_role_synergy(
    data_path: Path,
    *,
    role_by_champ: dict[int, str],
    queue_id: int | None = 2400,
    patch_prefix: str | None = None,
    min_cell: int = 150,
    min_rest: int | None = None,
    shrink_k: float = 150.0,
    persistence_factor: float = 0.5,
    min_duration_sec: int = 300,
) -> RoleSynergyStats:
    """Build ordered anchor -> teammate-role synergy rows from a pooled parquet.

    A cell qualifies only when BOTH the present bucket (anchor's team has the
    role) and the rest bucket (anchor's team lacks it) clear their minimums.
    The rest guard matters for common roles (Mage/Tank): almost every team has
    one, so "team without that role" is a rare, unrepresentative subset whose
    win rate would otherwise inject noise into the difference.
    """
    if min_rest is None:
        min_rest = min_cell
    role_by_champ = {int(k): str(v) for k, v in role_by_champ.items() if v}
    team_rows, total_matches = _team_rows_from_parquet(
        Path(data_path), min_duration_sec=min_duration_sec
    )

    anchor_games: Counter[int] = Counter()
    anchor_wins: Counter[int] = Counter()
    cell_games: Counter[tuple[int, str]] = Counter()
    cell_wins: Counter[tuple[int, str]] = Counter()

    for team, won in team_rows:
        ids = sorted({c for c in team if c > 0})
        for anchor in ids:
            anchor_games[anchor] += 1
            anchor_wins[anchor] += won
            # roles present among the OTHER team members (binary membership)
            other_roles = {
                role_by_champ[c] for c in ids if c != anchor and c in role_by_champ
            }
            for role in other_roles:
                key = (anchor, role)
                cell_games[key] += 1
                cell_wins[key] += won

    out: dict[tuple[int, str], RoleSynergyRow] = {}
    for (anchor, role), n_pair in cell_games.items():
        if n_pair < min_cell:
            continue
        n_rest = anchor_games[anchor] - n_pair
        if n_rest < min_rest:
            continue

        w_pair = cell_wins[(anchor, role)]
        w_rest = anchor_wins[anchor] - w_pair
        pair_wr = w_pair / n_pair
        rest_wr = w_rest / n_rest
        raw_delta = pair_wr - rest_wr
        shrunk = raw_delta * (n_pair / (n_pair + shrink_k)) * persistence_factor
        var_pair = pair_wr * (1.0 - pair_wr) / max(n_pair, 1)
        var_rest = rest_wr * (1.0 - rest_wr) / max(n_rest, 1)
        se = math.sqrt(var_pair + var_rest)

        out[(anchor, role)] = RoleSynergyRow(
            anchor_id=anchor,
            role=role,
            games=n_pair,
            wins=w_pair,
            rest_games=n_rest,
            rest_wins=w_rest,
            pair_wr=pair_wr,
            rest_wr=rest_wr,
            raw_delta=raw_delta,
            delta=shrunk,
            se=se,
        )

    roles = tuple(sorted({r for _, r in out.keys()}))
    return RoleSynergyStats(
        rows=out,
        role_by_champ=role_by_champ,
        min_pair=min_cell,
        shrink_k=shrink_k,
        persistence_factor=persistence_factor,
        roles=roles,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        total_matches=total_matches,
    )


def save_role_synergy(stats: RoleSynergyStats, path: Path) -> None:
    payload = {
        "kind": SCHEMA_KIND,
        "version": 1,
        "queue_id": stats.queue_id,
        "patch_prefix": stats.patch_prefix,
        "min_cell": stats.min_pair,
        "shrink_k": stats.shrink_k,
        "persistence_factor": stats.persistence_factor,
        "total_matches": stats.total_matches,
        "roles": list(stats.roles),
        "role_by_champ": {str(cid): role for cid, role in sorted(stats.role_by_champ.items())},
        "cells": [
            {
                "anchor_id": row.anchor_id,
                "role": row.role,
                "games": row.games,
                "wins": row.wins,
                "rest_games": row.rest_games,
                "rest_wins": row.rest_wins,
                "pair_wr": row.pair_wr,
                "rest_wr": row.rest_wr,
                "raw_delta": row.raw_delta,
                "delta": row.delta,
                "se": row.se,
            }
            for row in sorted(stats.rows.values(), key=lambda r: (r.anchor_id, r.role))
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_role_synergy(path: Path) -> RoleSynergyStats:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("kind") != SCHEMA_KIND:
        raise ValueError(
            f"{path} is not a champ_role_synergy file (kind={payload.get('kind')!r})"
        )
    role_by_champ = {int(k): str(v) for k, v in (payload.get("role_by_champ") or {}).items()}
    rows: dict[tuple[int, str], RoleSynergyRow] = {}
    for item in payload.get("cells", []):
        row = RoleSynergyRow(
            anchor_id=int(item["anchor_id"]),
            role=str(item["role"]),
            games=int(item["games"]),
            wins=int(item["wins"]),
            rest_games=int(item["rest_games"]),
            rest_wins=int(item["rest_wins"]),
            pair_wr=float(item["pair_wr"]),
            rest_wr=float(item["rest_wr"]),
            raw_delta=float(item.get("raw_delta", item["delta"])),
            delta=float(item["delta"]),
            se=float(item["se"]),
        )
        rows[(row.anchor_id, row.role)] = row

    return RoleSynergyStats(
        rows=rows,
        role_by_champ=role_by_champ,
        min_pair=int(payload.get("min_cell", 0)),
        shrink_k=float(payload.get("shrink_k", 150.0)),
        persistence_factor=float(payload.get("persistence_factor", 0.5)),
        roles=tuple(payload.get("roles") or sorted({r for _, r in rows.keys()})),
        queue_id=payload.get("queue_id"),
        patch_prefix=payload.get("patch_prefix"),
        total_matches=payload.get("total_matches"),
    )
