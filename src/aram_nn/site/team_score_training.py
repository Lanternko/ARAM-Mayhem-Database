"""Time-split calibration for the public single-team win-rate score.

The site score is intentionally smaller than the full 5v5 Draft model: it
estimates one five-champion team's strength against an average opponent.  This
module keeps that product contract while learning the two non-solo terms from
the current patch instead of treating raw pair/composition residuals as
probability points.

Training protocol (chronological 70/15/15):

* TRAIN builds champion baselines, champion-pair residuals, and composition
  residual tables.
* VALIDATION chooses pair shrinkage plus non-negative pair/composition logit
  weights.
* TEST is untouched until the final report.
* Production tables are then refit on all available games; only the validated
  shrinkage/weights are reused.

Every normal tier-list build calls :func:`train_team_score_bundle`, so a new
patch receives its own tables and calibration automatically once it has enough
games.  The returned bundle is embedded in ``tier-list.json`` and consumed by
both the browser and the Meta Pick API.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from array import array
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import minimize
from sklearn.metrics import log_loss, roc_auc_score

from .meta_pick import _ad_bin, _count_group, _front_group
from .patch_extract import patch_range, scan_db


TRAIN_FRACTION = 0.70
VALIDATION_FRACTION = 0.15
MIN_TRAINING_GAMES = 10_000
SOLO_PRIOR_GAMES = 200
COMPOSITION_CELL_PRIOR_GAMES = 400
PAIR_PRIOR_CANDIDATES = (0.0, 50.0, 100.0, 200.0, 400.0, 800.0)
PAIR_WEIGHT_CANDIDATES = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0)
COMPOSITION_WEIGHT_CANDIDATES = (0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
PAIR_ROWS_EACH_SIDE = 12
TABLE_NAMES = (
    "ad_front",
    "poke_front",
    "wave_engage",
    "all_lacks",
    "mage_ad",
    "marksman_ad",
)


@dataclass(frozen=True)
class TeamGames:
    """Structurally valid 5v5 games as arrays, in chronological order.

    Arrays instead of one object per game: a 4M-game patch as ``TeamGame``
    objects was ~1.2 GB, and every training step is a column operation.
    Team rows are sorted champion ids (as ``_parse_team`` always did).
    """

    created_ms: np.ndarray  # (n,) int64
    blue: np.ndarray  # (n, 5) int64
    red: np.ndarray  # (n, 5) int64
    blue_wins: np.ndarray  # (n,) float64, 0/1

    def __len__(self) -> int:
        return int(self.created_ms.shape[0])

    def slice(self, start: int, stop: int) -> "TeamGames":
        return TeamGames(
            self.created_ms[start:stop],
            self.blue[start:stop],
            self.red[start:stop],
            self.blue_wins[start:stop],
        )


def _clip_probability(value: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, float(value)))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def _logit_array(values: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    """``_logit`` element-wise, bit-identical: numpy's SIMD log is not always
    correctly rounded, so the log itself stays ``math.log``."""
    p = np.minimum(1.0 - eps, np.maximum(eps, np.asarray(values, dtype=np.float64)))
    ratio = p / (1.0 - p)
    return np.fromiter(map(math.log, ratio.tolist()), dtype=np.float64, count=ratio.shape[0])


def _sigmoid_array(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    out = np.empty_like(values)
    positive = values >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    out[~positive] = exp_values / (1.0 + exp_values)
    return out


def _parse_team(raw: Any) -> tuple[int, ...]:
    if isinstance(raw, str):
        if raw.startswith("[") and raw.endswith("]"):
            try:
                # Fast path for the stored "[1,2,3,4,5]" form.
                return tuple(sorted(int(part) for part in raw[1:-1].split(",")))
            except ValueError:
                pass
        raw = json.loads(raw)
    return tuple(sorted(int(cid) for cid in (raw or [])))


def load_team_games(
    db_path: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
) -> TeamGames:
    """Load structurally valid 5v5 rows in chronological order."""
    con = sqlite3.connect(str(scan_db(db_path, queue_id, patch_prefix)))
    created = array("q")
    blue_ids = array("q")
    red_ids = array("q")
    wins = array("b")
    try:
        if patch_prefix:
            rows = con.execute(
                "SELECT created_ms, blue_champs, red_champs, blue_wins "
                "FROM games WHERE queue_id=? AND patch >= ? AND patch < ? "
                "ORDER BY created_ms, game_id",
                (queue_id, *patch_range(patch_prefix)),
            )
        else:
            rows = con.execute(
                "SELECT created_ms, blue_champs, red_champs, blue_wins "
                "FROM games WHERE queue_id=? ORDER BY created_ms, game_id",
                (queue_id,),
            )
        for created_ms, blue_raw, red_raw, blue_wins in rows:
            try:
                blue = _parse_team(blue_raw)
                red = _parse_team(red_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if len(blue) != 5 or len(red) != 5:
                continue
            created.append(int(created_ms or 0))
            blue_ids.extend(blue)
            red_ids.extend(red)
            wins.append(1 if bool(blue_wins) else 0)
    finally:
        con.close()
    return TeamGames(
        created_ms=np.frombuffer(created, dtype=np.int64).copy() if created else np.zeros(0, np.int64),
        blue=np.frombuffer(blue_ids, dtype=np.int64).reshape(-1, 5).copy() if blue_ids else np.zeros((0, 5), np.int64),
        red=np.frombuffer(red_ids, dtype=np.int64).reshape(-1, 5).copy() if red_ids else np.zeros((0, 5), np.int64),
        blue_wins=np.frombuffer(wins, dtype=np.int8).astype(np.float64),
    )


def chronological_split(games: TeamGames) -> tuple[TeamGames, TeamGames, TeamGames]:
    n = len(games)
    train_end = max(1, min(n - 2, int(n * TRAIN_FRACTION)))
    val_end = max(train_end + 1, min(n - 1, int(n * (TRAIN_FRACTION + VALIDATION_FRACTION))))
    return games.slice(0, train_end), games.slice(train_end, val_end), games.slice(val_end, n)


# Team edges in the order the per-team loops visited them: (0,1), (0,2), ...
_EDGES = tuple((i, j) for i in range(5) for j in range(i + 1, 5))


@dataclass(frozen=True)
class _Index:
    """Dense champion index; ascending, so (a < b) <=> (idx a < idx b)."""

    cids: np.ndarray  # (k,) sorted champion ids

    @property
    def size(self) -> int:
        return int(self.cids.shape[0])

    def of(self, teams: np.ndarray) -> np.ndarray:
        return np.searchsorted(self.cids, teams)


def _champion_index(games: TeamGames) -> _Index:
    return _Index(np.unique(np.concatenate([games.blue.ravel(), games.red.ravel()])))


def _sides(games: TeamGames, index: _Index) -> tuple[np.ndarray, np.ndarray]:
    """Team sides interleaved blue, red per game (the old ``_iter_sides``
    order, which fixes every floating-point summation order below)."""
    n = len(games)
    # int32 halves the biggest training array (2 sides x 4M games x 5).
    teams = np.empty((2 * n, 5), dtype=np.int32)
    teams[0::2] = index.of(games.blue)
    teams[1::2] = index.of(games.red)
    won = np.empty(2 * n, dtype=np.float64)
    won[0::2] = games.blue_wins
    won[1::2] = 1.0 - games.blue_wins
    return teams, won


def _builtin_sum(columns: list[np.ndarray]) -> np.ndarray:
    """Element-wise ``sum([c0, c1, ...])`` exactly as CPython >= 3.12 adds
    floats: left to right with Neumaier compensation (bltinmodule.c).

    The per-team loops this replaced used ``sum()``; plain sequential or
    pairwise numpy sums differ in the last bits, which moves the L-BFGS
    intercept fit.  The production interpreter is 3.13.
    """
    total = columns[0].astype(np.float64, copy=True)
    comp = np.zeros_like(total)
    for column in columns[1:]:
        t = total + column
        comp += np.where(np.abs(total) >= np.abs(column), (total - t) + column, (column - t) + total)
        total = t
    return np.where((comp != 0) & np.isfinite(comp), total + comp, total)


@dataclass(frozen=True)
class _SoloPair:
    solo_wr: np.ndarray  # (k,) shrunk solo win rate; 0.5 for unseen champions
    pair_lift: dict[tuple[int, int], tuple[float, int]]  # (cid a < cid b) -> (lift, games)


def _solo_and_pair_stats(teams: np.ndarray, won: np.ndarray, index: _Index) -> _SoloPair:
    # Counts are integers, so accumulating one column (or edge) at a time is
    # exact and avoids (sides x 10) temporaries -- 640 MB at 4M games.
    k = index.size
    champ_games = np.zeros(k, dtype=np.int64)
    champ_wins = np.zeros(k, dtype=np.int64)
    for col in range(5):
        champ_games += np.bincount(teams[:, col], minlength=k)
        champ_wins += np.bincount(teams[:, col], weights=won, minlength=k).astype(np.int64)
    seen = champ_games > 0
    safe_games = np.where(seen, champ_games, 1)
    raw_wr = np.where(seen, champ_wins / safe_games, 0.5)
    solo_wr = np.where(
        seen,
        (champ_wins + 0.5 * SOLO_PRIOR_GAMES) / (champ_games + SOLO_PRIOR_GAMES),
        0.5,
    )
    pair_games = np.zeros(k * k, dtype=np.int64)
    pair_wins = np.zeros(k * k, dtype=np.int64)
    for i, j in _EDGES:
        # Sorted teams: column i holds the smaller champion id of the pair.
        flat = teams[:, i].astype(np.int64) * k + teams[:, j]
        pair_games += np.bincount(flat, minlength=k * k)
        pair_wins += np.bincount(flat, weights=won, minlength=k * k).astype(np.int64)

    global_wr = 0.5  # both sides are emitted for every match
    pair_lift: dict[tuple[int, int], tuple[float, int]] = {}
    raw_list = raw_wr.tolist()
    cids = index.cids.tolist()
    for flat_key in np.flatnonzero(pair_games).tolist():
        a, b = divmod(flat_key, k)
        games_n = int(pair_games[flat_key])
        pair_wr = int(pair_wins[flat_key]) / games_n
        expected = 1.0 / (
            1.0
            + math.exp(
                -(
                    _logit(raw_list[a])
                    + _logit(raw_list[b])
                    - _logit(global_wr)
                )
            )
        )
        lift = pair_wr - expected
        pair_lift[(cids[a], cids[b])] = (lift, games_n)
    return _SoloPair(solo_wr, pair_lift)


def _mean_solo(teams: np.ndarray, solo_wr: np.ndarray) -> np.ndarray:
    values = solo_wr[teams]
    return _builtin_sum([values[:, col] for col in range(5)]) / 5


# --- composition keys ------------------------------------------------------

_SUM_KEYS = ("phys", "magic", "true", "wave", "cc", "engage", "damage", "poke", "sustain", "front")
_LACK_KEYS = ("wave", "cc", "engage", "damage", "poke", "sustain", "front")
_COUNT_LABELS = tuple(_count_group(float(v)) for v in (0, 1, 2))
_FRONT_LABELS = tuple(_front_group(float(v)) for v in (0, 1, 2))
_AD_CUTS = (0.35, 0.45, 0.55, 0.65)
_AD_LABELS = tuple(_ad_bin(v) for v in (0.0, 0.4, 0.5, 0.6, 0.7))
_LACK_LABELS = {
    "wave": ("wave ok", "wave lack"),
    "engage": ("engage ok", "engage lack"),
    "poke": ("poke ok", "poke lack"),
}
# Per table: (label of each code).  Codes are built in _composition_codes.
_TABLE_LABELS: dict[str, tuple[str, ...]] = {
    "ad_front": tuple(f"{f}|{a}" for f in _FRONT_LABELS for a in _AD_LABELS),
    "poke_front": tuple(f"{f}|{p}" for f in _FRONT_LABELS for p in _LACK_LABELS["poke"]),
    "wave_engage": tuple(
        f"{w}|{e}" for w in _LACK_LABELS["wave"] for e in _LACK_LABELS["engage"]
    ),
    "all_lacks": _COUNT_LABELS,
    "mage_ad": tuple(f"{m}|{a}" for m in _COUNT_LABELS for a in _AD_LABELS),
    "marksman_ad": tuple(f"{m}|{a}" for m in _COUNT_LABELS for a in _AD_LABELS),
}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return None


def _count_code(count: np.ndarray) -> np.ndarray:
    """``_count_group`` thresholds as codes 0 / 1 / 2 (labels _COUNT_LABELS)."""
    return np.where(count < 0.5, 0, np.where(count < 1.5, 1, 2))


def _composition_codes(teams: np.ndarray, snapshot: dict[str, Any], index: _Index) -> dict[str, np.ndarray]:
    """``meta_pick.team_composition`` keys for every 5-champion side, as codes.

    Same arithmetic in the same order: per-champion sums start at 0.0 and add
    team members left to right (an unparsable value, or a champion missing
    from the snapshot, adds nothing -- here it adds an exact 0.0).  Teams are
    always 5 wide, so the size projection is 1.  Pinned by a test against
    ``team_composition`` itself.
    """
    champs = snapshot.get("champs") or {}
    thresholds_raw = (snapshot.get("recommendation_composition") or {}).get("lack_thresholds") or {}
    k = index.size
    addends = np.zeros((k, len(_SUM_KEYS)), dtype=np.float64)
    is_front = np.zeros(k, dtype=np.int8)
    n_mage = np.zeros(k, dtype=np.int8)
    n_marksman = np.zeros(k, dtype=np.int8)
    for i, cid in enumerate(index.cids.tolist()):
        info = champs.get(str(cid))
        if not info:
            continue
        comp = info.get("comp") or {}
        for j, key in enumerate(_SUM_KEYS):
            value = _float_or_none(comp.get(key))
            if value is not None:
                addends[i, j] = value
        front = _float_or_none(comp.get("front"))
        is_front[i] = 1 if front is not None and front >= 2.0 else 0
        tags = info.get("tags") or []
        n_mage[i] = sum(1 for tag in tags if tag == "Mage")
        n_marksman[i] = sum(1 for tag in tags if tag == "Marksman")

    def team_sum(j: int) -> np.ndarray:
        # One key at a time: a (sides x 10) float block is 640 MB at 4M games.
        column = addends[:, j]
        total = column[teams[:, 0]]
        for member in range(1, 5):
            total = total + column[teams[:, member]]
        return total

    col = {key: team_sum(j) for j, key in enumerate(_SUM_KEYS) if key != "true"}
    ad_den = col["phys"] + col["magic"]
    positive = ad_den > 0
    ad_share = np.full(teams.shape[0], 0.5)
    np.divide(col["phys"], ad_den, out=ad_share, where=positive)
    ad_code = np.searchsorted(np.asarray(_AD_CUTS), ad_share, side="right")
    lacks: dict[str, np.ndarray] = {}
    for key in _LACK_KEYS:
        threshold = _float_or_none(thresholds_raw.get(key))
        threshold = 0.0 if threshold is None else threshold
        if threshold > 0:
            lacks[key] = col[key] < threshold * 1.0
        else:
            lacks[key] = np.zeros(teams.shape[0], dtype=bool)
    all_lacks = np.zeros(teams.shape[0], dtype=np.int8)
    for key in _LACK_KEYS:
        all_lacks += lacks[key]
    front_code = _count_code(is_front[teams].sum(axis=1))
    mage_code = _count_code(n_mage[teams].sum(axis=1))
    marksman_code = _count_code(n_marksman[teams].sum(axis=1))
    n_ad = len(_AD_LABELS)
    codes = {
        "ad_front": front_code * n_ad + ad_code,
        "poke_front": front_code * 2 + lacks["poke"],
        "wave_engage": lacks["wave"] * 2 + lacks["engage"],
        "all_lacks": _count_code(all_lacks),
        "mage_ad": mage_code * n_ad + ad_code,
        "marksman_ad": marksman_code * n_ad + ad_code,
    }
    # Every table has <= 15 cells; int8 keeps 6 code arrays at 8 MB per 1M sides.
    return {name: values.astype(np.int8) for name, values in codes.items()}


def fit_composition_tables(
    teams: np.ndarray,
    won: np.ndarray,
    codes: dict[str, np.ndarray],
    *,
    solo_wr: np.ndarray,
    prior_games: float = COMPOSITION_CELL_PRIOR_GAMES,
) -> dict[str, dict[str, float]]:
    """Fit centered, empirical-Bayes residual tables for one patch."""
    residual = won - _mean_solo(teams, solo_wr)
    # Sequential running sum, as the old ``residual_sum += residual`` loop.
    global_residual = float(np.cumsum(residual)[-1]) / residual.shape[0] if residual.shape[0] else 0.0
    centered = residual - global_residual
    tables: dict[str, dict[str, float]] = {}
    for name in TABLE_NAMES:
        labels = _TABLE_LABELS[name]
        # bincount accumulates in input order, like ``sums[key] += centered``.
        sums = np.bincount(codes[name], weights=centered, minlength=len(labels))
        counts = np.bincount(codes[name], minlength=len(labels))
        table: dict[str, float] = {}
        for code in sorted(np.flatnonzero(counts).tolist(), key=lambda c: labels[c]):
            n = int(counts[code])
            value = float(sums[code]) / (n + max(0.0, prior_games))
            table[labels[code]] = round(float(value), 6)
        tables[name] = table
    return tables


def _selected_pairs(
    pair_stats: dict[tuple[int, int], tuple[float, int]],
    *,
    min_games: int,
    prior_games: float,
    each_side: int = PAIR_ROWS_EACH_SIDE,
) -> set[tuple[int, int]]:
    by_champ: dict[int, list[tuple[float, int, int, tuple[int, int]]]] = defaultdict(list)
    for key, (lift, games_n) in pair_stats.items():
        if games_n < min_games:
            continue
        adjusted = lift * games_n / (games_n + prior_games) if games_n + prior_games > 0 else 0.0
        a, b = key
        by_champ[a].append((adjusted, games_n, b, key))
        by_champ[b].append((adjusted, games_n, a, key))
    selected: set[tuple[int, int]] = set()
    for rows in by_champ.values():
        rows.sort(key=lambda row: (-row[0], -row[1], row[2]))
        if len(rows) <= each_side * 2:
            selected.update(row[3] for row in rows)
        else:
            selected.update(row[3] for row in rows[:each_side])
            selected.update(row[3] for row in rows[-each_side:])
    return selected


def _base_and_composition_diffs(
    games: TeamGames,
    codes: dict[str, np.ndarray],
    teams: np.ndarray,
    *,
    solo_wr: np.ndarray,
    tables: dict[str, dict[str, float]],
    table_weights: dict[str, float],
    clamp: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per game: blue-minus-red base logit, composition score, label.

    *teams*/*codes* are the interleaved sides of *games* (blue rows 0::2).
    """
    terms = []
    for name in TABLE_NAMES:  # the old components dict order
        labels = _TABLE_LABELS[name]
        table = tables.get(name, {})
        lookup = np.asarray([float(table.get(label, 0.0)) for label in labels], dtype=np.float64)
        terms.append(float(table_weights.get(name, 0.0)) * lookup[codes[name]])
    score = _builtin_sum(terms)
    side_comp = np.maximum(-clamp, np.minimum(clamp, score))
    side_base = _logit_array(_mean_solo(teams, solo_wr))
    base_diff = side_base[0::2] - side_base[1::2]
    comp_diff = side_comp[0::2] - side_comp[1::2]
    return base_diff, comp_diff, games.blue_wins.astype(np.float64)


def _pair_diff_array(
    teams: np.ndarray,
    *,
    stats: _SoloPair,
    index: _Index,
    min_games: int,
    prior_games: float,
) -> np.ndarray:
    selected = _selected_pairs(
        stats.pair_lift,
        min_games=min_games,
        prior_games=prior_games,
    )
    k = index.size
    adjusted = np.zeros(k * k, dtype=np.float64)
    position = {int(cid): i for i, cid in enumerate(index.cids.tolist())}
    for a, b in selected:
        lift, games_n = stats.pair_lift.get((a, b), (0.0, 0))
        if games_n + prior_games > 0:
            adjusted[position[a] * k + position[b]] = lift * games_n / (games_n + prior_games)
    total = None
    for i, j in _EDGES:
        term = adjusted[teams[:, i] * k + teams[:, j]]
        total = term if total is None else total + term
    assert total is not None
    side = total / len(_EDGES)
    return side[0::2] - side[1::2]


def _binary_log_loss(labels: np.ndarray, logits: np.ndarray) -> float:
    return float(np.mean(np.logaddexp(0.0, logits) - labels * logits))


def _fit_offset_weights(
    labels: np.ndarray,
    base_diff: np.ndarray,
    pair_diff: np.ndarray,
    comp_diff: np.ndarray,
) -> tuple[float, float, float, float]:
    blue_rate = _clip_probability(float(np.mean(labels)))
    start = np.asarray([_logit(blue_rate), 4.0, 1.0], dtype=np.float64)

    def objective(params: np.ndarray) -> float:
        blue_bias, pair_weight, composition_weight = params
        logits = base_diff + blue_bias + pair_weight * pair_diff + composition_weight * comp_diff
        # Tiny ridge term prevents an unstable huge coefficient when a new patch
        # has sparse selected-pair coverage.  The data term dominates at 10k+ rows.
        penalty = 1e-6 * (pair_weight * pair_weight + composition_weight * composition_weight)
        return _binary_log_loss(labels, logits) + penalty

    result = minimize(
        objective,
        start,
        method="L-BFGS-B",
        bounds=((-0.5, 0.5), (0.0, 20.0), (0.0, 8.0)),
    )
    params = result.x if result.success else start
    return float(params[0]), float(params[1]), float(params[2]), float(objective(params))


def _fit_base_bias(labels: np.ndarray, base_diff: np.ndarray) -> float:
    blue_rate = _clip_probability(float(np.mean(labels)))

    def objective(value: np.ndarray) -> float:
        return _binary_log_loss(labels, base_diff + float(value[0]))

    result = minimize(
        objective,
        np.asarray([_logit(blue_rate)], dtype=np.float64),
        method="L-BFGS-B",
        bounds=((-0.5, 0.5),),
    )
    return float(result.x[0]) if result.success else _logit(blue_rate)


def _auc(labels: np.ndarray, logits: np.ndarray) -> float:
    try:
        return float(roc_auc_score(labels, logits))
    except ValueError:
        return 0.5


def _select_ranking_weights(
    labels: np.ndarray,
    base_diff: np.ndarray,
    comp_diff: np.ndarray,
    pair_by_prior: dict[float, np.ndarray],
) -> tuple[float, float, float, np.ndarray]:
    """Select the roster-ranking terms by validation AUC, then log-loss.

    Meta Pick ranks C(10,5) rosters, so an unconstrained log-loss optimum can be
    actively harmful even when it looks better calibrated.  The small fixed
    grid includes the old additive-equivalent settings (pair=4, comp=1) and
    zero, making "do not ship this signal" a first-class outcome.
    """
    blue_rate = _clip_probability(float(np.mean(labels)))
    rough_bias = _logit(blue_rate)
    best_key: tuple[float, float, float, float, float] | None = None
    best_value: tuple[float, float, float, np.ndarray] | None = None
    for prior_games, pair_diff in pair_by_prior.items():
        for pair_weight in PAIR_WEIGHT_CANDIDATES:
            for composition_weight in COMPOSITION_WEIGHT_CANDIDATES:
                logits = (
                    base_diff
                    + pair_weight * pair_diff
                    + composition_weight * comp_diff
                )
                auc = _auc(labels, logits)
                loss = _binary_log_loss(labels, logits + rough_bias)
                nonzero = float(pair_weight > 0) + float(composition_weight > 0)
                # Prefer higher AUC, then lower loss, then the simpler/smaller
                # model when numerical ties land on the same ranking.
                key = (
                    round(auc, 10),
                    -round(loss, 10),
                    -nonzero,
                    -pair_weight,
                    -composition_weight,
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_value = (
                        prior_games,
                        pair_weight,
                        composition_weight,
                        pair_diff,
                    )
    assert best_value is not None
    prior_games, pair_weight, composition_weight, pair_diff = best_value
    if pair_weight == 0:
        prior_games = 0.0
    return prior_games, pair_weight, composition_weight, pair_diff


def _metrics(labels: np.ndarray, logits: np.ndarray) -> dict[str, float]:
    probs = _sigmoid_array(logits)
    try:
        auc = float(roc_auc_score(labels, probs))
    except ValueError:
        auc = 0.5
    return {
        "log_loss": round(float(log_loss(labels, probs, labels=[0.0, 1.0])), 6),
        "auc": round(auc, 6),
        "accuracy": round(float(np.mean((probs >= 0.5) == labels)), 6),
    }


def _score_version(
    *,
    patch_prefix: str | None,
    queue_id: int,
    games: TeamGames,
    pair_prior_games: float,
    pair_weight: float,
    composition_weight: float,
    tables: dict[str, dict[str, float]],
) -> str:
    material = {
        "patch": patch_prefix or "all",
        "queue": queue_id,
        "games": len(games),
        "latest_ms": int(games.created_ms.max()) if len(games) else 0,
        "pair_prior": round(pair_prior_games, 6),
        "pair_weight": round(pair_weight, 8),
        "composition_weight": round(composition_weight, 8),
        "tables": tables,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"team-logit-v2:{patch_prefix or 'all'}:{len(games)}:{digest}"


def training_snapshot(
    *,
    champ_profiles: dict[int, dict[str, object]],
    champ_meta: dict[int, dict[str, Any]],
    lack_thresholds: dict[str, float],
) -> dict[str, Any]:
    champs: dict[str, dict[str, Any]] = {}
    for cid, profile in champ_profiles.items():
        champs[str(cid)] = {
            "tags": list((champ_meta.get(cid) or {}).get("tags") or []),
            "comp": {
                "phys": float(profile.get("physical_dpm") or 0.0),
                "magic": float(profile.get("magic_dpm") or 0.0),
                "true": float(profile.get("true_dpm") or 0.0),
                "wave": float(profile.get("wave") or 0.0),
                "cc": float(profile.get("cc") or 0.0),
                "engage": float(profile.get("engage") or 0.0),
                "damage": float(profile.get("damage_score") or 0.0),
                "poke": float(profile.get("poke") or 0.0),
                "sustain": float(profile.get("sustain") or 0.0),
                "front": float(profile.get("front") or 0.0),
            },
        }
    return {
        "champs": champs,
        "recommendation_composition": {"lack_thresholds": dict(lack_thresholds)},
    }


def train_team_score_bundle(
    db_path: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
    champ_profiles: dict[int, dict[str, object]],
    champ_meta: dict[int, dict[str, Any]],
    lack_thresholds: dict[str, float],
    table_weights: dict[str, float],
    composition_clamp: float,
    min_synergy_games: int,
    min_training_games: int = MIN_TRAINING_GAMES,
) -> dict[str, Any]:
    """Train and return ``{"team_score": ..., "composition": ...}``.

    Raises ``ValueError`` when the current patch is too small.  The site builder
    catches that and emits an explicit legacy fallback instead of silently
    claiming a model was trained.
    """
    games = load_team_games(
        db_path,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
    )
    if len(games) < min_training_games:
        raise ValueError(
            f"team-score calibration needs >= {min_training_games:,} games; got {len(games):,}"
        )
    train, validation, test = chronological_split(games)
    snapshot = training_snapshot(
        champ_profiles=champ_profiles,
        champ_meta=champ_meta,
        lack_thresholds=lack_thresholds,
    )
    index = _champion_index(games)
    # Sides of every game (blue, red interleaved) and their composition codes,
    # computed once; the chronological splits are contiguous row slices.
    all_teams, all_won = _sides(games, index)
    all_codes = _composition_codes(all_teams, snapshot, index)
    train_rows = slice(0, 2 * len(train))
    val_rows = slice(2 * len(train), 2 * (len(train) + len(validation)))
    test_rows = slice(2 * (len(train) + len(validation)), 2 * len(games))

    def codes_of(rows: slice) -> dict[str, np.ndarray]:
        return {name: values[rows] for name, values in all_codes.items()}

    train_stats = _solo_and_pair_stats(all_teams[train_rows], all_won[train_rows], index)
    train_solo = train_stats.solo_wr
    train_tables = fit_composition_tables(
        all_teams[train_rows],
        all_won[train_rows],
        codes_of(train_rows),
        solo_wr=train_solo,
    )
    val_base, val_comp, val_labels = _base_and_composition_diffs(
        validation,
        codes_of(val_rows),
        all_teams[val_rows],
        solo_wr=train_solo,
        tables=train_tables,
        table_weights=table_weights,
        clamp=composition_clamp,
    )

    pair_by_prior: dict[float, np.ndarray] = {}
    for prior_games in PAIR_PRIOR_CANDIDATES:
        pair_by_prior[prior_games] = _pair_diff_array(
            all_teams[val_rows],
            stats=train_stats,
            index=index,
            min_games=min_synergy_games,
            prior_games=prior_games,
        )
    pair_prior_games, pair_weight, composition_weight, val_pair = _select_ranking_weights(
        val_labels,
        val_base,
        val_comp,
        pair_by_prior,
    )
    blue_bias = _fit_base_bias(
        val_labels,
        val_base + pair_weight * val_pair + composition_weight * val_comp,
    )

    test_base, test_comp, test_labels = _base_and_composition_diffs(
        test,
        codes_of(test_rows),
        all_teams[test_rows],
        solo_wr=train_solo,
        tables=train_tables,
        table_weights=table_weights,
        clamp=composition_clamp,
    )
    test_pair = _pair_diff_array(
        all_teams[test_rows],
        stats=train_stats,
        index=index,
        min_games=min_synergy_games,
        prior_games=pair_prior_games,
    )
    base_bias = _fit_base_bias(val_labels, val_base)
    validation_logits = val_base + blue_bias + pair_weight * val_pair + composition_weight * val_comp
    test_logits = test_base + blue_bias + pair_weight * test_pair + composition_weight * test_comp

    metrics = {
        "validation": {
            "base": _metrics(val_labels, val_base + base_bias),
            "base_pair": _metrics(val_labels, val_base + blue_bias + pair_weight * val_pair),
            "base_composition": _metrics(
                val_labels, val_base + blue_bias + composition_weight * val_comp
            ),
            "full": _metrics(val_labels, validation_logits),
        },
        "test": {
            "base": _metrics(test_labels, test_base + base_bias),
            "base_pair": _metrics(test_labels, test_base + blue_bias + pair_weight * test_pair),
            "base_composition": _metrics(
                test_labels, test_base + blue_bias + composition_weight * test_comp
            ),
            "full": _metrics(test_labels, test_logits),
        },
    }

    # Standard production refit: the validation/test protocol above fixes all
    # hyperparameters; final public tables then use every current-patch game.
    full_solo = _solo_and_pair_stats(all_teams, all_won, index).solo_wr
    full_tables = fit_composition_tables(
        all_teams,
        all_won,
        all_codes,
        solo_wr=full_solo,
    )
    version = _score_version(
        patch_prefix=patch_prefix,
        queue_id=queue_id,
        games=games,
        pair_prior_games=pair_prior_games,
        pair_weight=pair_weight,
        composition_weight=composition_weight,
        tables=full_tables,
    )
    composition_probability_weight = composition_weight * 0.25
    return {
        "team_score": {
            "kind": "logit_v2",
            "score_version": version,
            "trained_patch": patch_prefix,
            "trained_games": len(games),
            "pair_prior_games": round(pair_prior_games, 3),
            "pair_logit_weight": round(pair_weight, 8),
            "composition_logit_weight": round(composition_weight, 8),
            "composition_probability_weight_at_50": round(composition_probability_weight, 8),
            "blue_side_intercept": round(blue_bias, 8),
            "split": {
                "method": "chronological_70_15_15",
                "train_games": len(train),
                "validation_games": len(validation),
                "test_games": len(test),
                "train_end_ms": int(train.created_ms[-1]),
                "validation_end_ms": int(validation.created_ms[-1]),
                "test_end_ms": int(test.created_ms[-1]),
            },
            "metrics": metrics,
        },
        "composition": {
            # Incremental candidate scoring lives on probability scale.  At 50%
            # sigmoid'(0)=0.25, so this is the exact local probability equivalent
            # of the validated logit coefficient (the previously tested 0.25
            # factor appears here when composition_logit_weight ~= 1).
            "weight": round(composition_probability_weight, 8),
            "clamp": float(composition_clamp),
            "lack_thresholds": dict(lack_thresholds),
            "table_weights": dict(table_weights),
            "tables": full_tables,
            "trained_patch": patch_prefix,
            "trained_games": len(games),
            "cell_prior_games": COMPOSITION_CELL_PRIOR_GAMES,
        },
    }
