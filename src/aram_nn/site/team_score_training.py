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
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from scipy.optimize import minimize
from sklearn.metrics import log_loss, roc_auc_score

from .meta_pick import team_composition


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
class TeamGame:
    created_ms: int
    blue: tuple[int, ...]
    red: tuple[int, ...]
    blue_wins: int


def _clip_probability(value: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, float(value)))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


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
        raw = json.loads(raw)
    return tuple(sorted(int(cid) for cid in (raw or [])))


def load_team_games(
    db_path: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
) -> list[TeamGame]:
    """Load structurally valid 5v5 rows in chronological order."""
    con = sqlite3.connect(str(db_path))
    try:
        if patch_prefix:
            rows = con.execute(
                "SELECT created_ms, blue_champs, red_champs, blue_wins "
                "FROM games WHERE queue_id=? AND patch LIKE ? "
                "ORDER BY created_ms, game_id",
                (queue_id, f"{patch_prefix}%"),
            )
        else:
            rows = con.execute(
                "SELECT created_ms, blue_champs, red_champs, blue_wins "
                "FROM games WHERE queue_id=? ORDER BY created_ms, game_id",
                (queue_id,),
            )
        games: list[TeamGame] = []
        for created_ms, blue_raw, red_raw, blue_wins in rows:
            try:
                blue = _parse_team(blue_raw)
                red = _parse_team(red_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if len(blue) != 5 or len(red) != 5:
                continue
            games.append(
                TeamGame(
                    created_ms=int(created_ms or 0),
                    blue=blue,
                    red=red,
                    blue_wins=1 if bool(blue_wins) else 0,
                )
            )
        return games
    finally:
        con.close()


def chronological_split(
    games: Sequence[TeamGame],
) -> tuple[list[TeamGame], list[TeamGame], list[TeamGame]]:
    n = len(games)
    train_end = max(1, min(n - 2, int(n * TRAIN_FRACTION)))
    val_end = max(train_end + 1, min(n - 1, int(n * (TRAIN_FRACTION + VALIDATION_FRACTION))))
    return list(games[:train_end]), list(games[train_end:val_end]), list(games[val_end:])


def _iter_sides(games: Iterable[TeamGame]) -> Iterable[tuple[tuple[int, ...], int]]:
    for game in games:
        yield game.blue, game.blue_wins
        yield game.red, 1 - game.blue_wins


def _solo_and_pair_stats(
    games: Sequence[TeamGame],
) -> tuple[dict[int, float], dict[tuple[int, int], tuple[float, int]]]:
    champ_games: Counter[int] = Counter()
    champ_wins: Counter[int] = Counter()
    pair_games: Counter[tuple[int, int]] = Counter()
    pair_wins: Counter[tuple[int, int]] = Counter()

    for team, won in _iter_sides(games):
        for cid in team:
            champ_games[cid] += 1
            champ_wins[cid] += won
        for i, cid in enumerate(team):
            for teammate in team[i + 1 :]:
                key = (cid, teammate) if cid < teammate else (teammate, cid)
                pair_games[key] += 1
                pair_wins[key] += won

    raw_wr = {
        cid: champ_wins[cid] / games_n
        for cid, games_n in champ_games.items()
        if games_n > 0
    }
    solo_wr = {
        cid: (champ_wins[cid] + 0.5 * SOLO_PRIOR_GAMES) / (games_n + SOLO_PRIOR_GAMES)
        for cid, games_n in champ_games.items()
        if games_n > 0
    }
    global_wr = 0.5  # both sides are emitted for every match
    pair_stats: dict[tuple[int, int], tuple[float, int]] = {}
    for key, games_n in pair_games.items():
        cid, teammate = key
        pair_wr = pair_wins[key] / games_n
        expected = 1.0 / (
            1.0
            + math.exp(
                -(
                    _logit(raw_wr.get(cid, global_wr))
                    + _logit(raw_wr.get(teammate, global_wr))
                    - _logit(global_wr)
                )
            )
        )
        pair_stats[key] = (pair_wr - expected, games_n)
    return solo_wr, pair_stats


def _mean_solo(team: Sequence[int], solo_wr: dict[int, float]) -> float:
    values = [solo_wr.get(int(cid), 0.5) for cid in team]
    return sum(values) / len(values) if values else 0.5


def _composition_keys(team: Sequence[int], snapshot: dict[str, Any]) -> dict[str, str]:
    comp = team_composition([str(cid) for cid in team], snapshot)
    return {
        "ad_front": f"{comp['frontGroup']}|{comp['adBin']}",
        "poke_front": f"{comp['frontGroup']}|{comp['pokeGroup']}",
        "wave_engage": f"{comp['waveGroup']}|{comp['engageGroup']}",
        "all_lacks": str(comp["allLacksGroup"]),
        "mage_ad": f"{comp['mageGroup']}|{comp['adBin']}",
        "marksman_ad": f"{comp['marksmanGroup']}|{comp['adBin']}",
    }


def fit_composition_tables(
    games: Sequence[TeamGame],
    *,
    solo_wr: dict[int, float],
    snapshot: dict[str, Any],
    prior_games: float = COMPOSITION_CELL_PRIOR_GAMES,
) -> dict[str, dict[str, float]]:
    """Fit centered, empirical-Bayes residual tables for one patch."""
    sums: dict[str, defaultdict[str, float]] = {
        name: defaultdict(float) for name in TABLE_NAMES
    }
    counts: dict[str, Counter[str]] = {name: Counter() for name in TABLE_NAMES}
    residual_sum = 0.0
    residual_n = 0
    for team, won in _iter_sides(games):
        residual = float(won) - _mean_solo(team, solo_wr)
        residual_sum += residual
        residual_n += 1
    global_residual = residual_sum / residual_n if residual_n else 0.0
    for team, won in _iter_sides(games):
        residual = float(won) - _mean_solo(team, solo_wr)
        keys = _composition_keys(team, snapshot)
        centered = residual - global_residual
        for name, key in keys.items():
            sums[name][key] += centered
            counts[name][key] += 1

    tables: dict[str, dict[str, float]] = {}
    for name in TABLE_NAMES:
        table: dict[str, float] = {}
        for key in sorted(counts[name]):
            n = counts[name][key]
            value = sums[name][key] / (n + max(0.0, prior_games))
            table[key] = round(float(value), 6)
        tables[name] = table
    return tables


def _table_components(
    team: Sequence[int],
    *,
    snapshot: dict[str, Any],
    tables: dict[str, dict[str, float]],
    table_weights: dict[str, float],
    clamp: float,
) -> tuple[float, dict[str, float]]:
    keys = _composition_keys(team, snapshot)
    components = {
        name: float(tables.get(name, {}).get(key, 0.0))
        for name, key in keys.items()
    }
    score = sum(float(table_weights.get(name, 0.0)) * value for name, value in components.items())
    return max(-clamp, min(clamp, score)), components


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


def _pair_score(
    team: Sequence[int],
    *,
    pair_stats: dict[tuple[int, int], tuple[float, int]],
    selected: set[tuple[int, int]],
    prior_games: float,
) -> float:
    if len(team) < 2:
        return 0.0
    total = 0.0
    edges = 0
    for i, cid in enumerate(team):
        for teammate in team[i + 1 :]:
            edges += 1
            key = (cid, teammate) if cid < teammate else (teammate, cid)
            if key not in selected:
                continue
            lift, games_n = pair_stats.get(key, (0.0, 0))
            if games_n + prior_games > 0:
                total += lift * games_n / (games_n + prior_games)
    return total / edges if edges else 0.0


def _base_and_composition_diffs(
    games: Sequence[TeamGame],
    *,
    solo_wr: dict[int, float],
    snapshot: dict[str, Any],
    tables: dict[str, dict[str, float]],
    table_weights: dict[str, float],
    clamp: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    base_diff = np.empty(len(games), dtype=np.float64)
    comp_diff = np.empty(len(games), dtype=np.float64)
    labels = np.empty(len(games), dtype=np.float64)
    for i, game in enumerate(games):
        blue_base = _mean_solo(game.blue, solo_wr)
        red_base = _mean_solo(game.red, solo_wr)
        blue_comp, _ = _table_components(
            game.blue,
            snapshot=snapshot,
            tables=tables,
            table_weights=table_weights,
            clamp=clamp,
        )
        red_comp, _ = _table_components(
            game.red,
            snapshot=snapshot,
            tables=tables,
            table_weights=table_weights,
            clamp=clamp,
        )
        base_diff[i] = _logit(blue_base) - _logit(red_base)
        comp_diff[i] = blue_comp - red_comp
        labels[i] = game.blue_wins
    return base_diff, comp_diff, labels


def _pair_diff_array(
    games: Sequence[TeamGame],
    *,
    pair_stats: dict[tuple[int, int], tuple[float, int]],
    min_games: int,
    prior_games: float,
) -> np.ndarray:
    selected = _selected_pairs(
        pair_stats,
        min_games=min_games,
        prior_games=prior_games,
    )
    values = np.empty(len(games), dtype=np.float64)
    for i, game in enumerate(games):
        blue = _pair_score(
            game.blue,
            pair_stats=pair_stats,
            selected=selected,
            prior_games=prior_games,
        )
        red = _pair_score(
            game.red,
            pair_stats=pair_stats,
            selected=selected,
            prior_games=prior_games,
        )
        values[i] = blue - red
    return values


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
    games: Sequence[TeamGame],
    pair_prior_games: float,
    pair_weight: float,
    composition_weight: float,
    tables: dict[str, dict[str, float]],
) -> str:
    material = {
        "patch": patch_prefix or "all",
        "queue": queue_id,
        "games": len(games),
        "latest_ms": max((game.created_ms for game in games), default=0),
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
    train_solo, train_pairs = _solo_and_pair_stats(train)
    train_tables = fit_composition_tables(
        train,
        solo_wr=train_solo,
        snapshot=snapshot,
    )
    val_base, val_comp, val_labels = _base_and_composition_diffs(
        validation,
        solo_wr=train_solo,
        snapshot=snapshot,
        tables=train_tables,
        table_weights=table_weights,
        clamp=composition_clamp,
    )

    pair_by_prior: dict[float, np.ndarray] = {}
    for prior_games in PAIR_PRIOR_CANDIDATES:
        pair_by_prior[prior_games] = _pair_diff_array(
            validation,
            pair_stats=train_pairs,
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
        solo_wr=train_solo,
        snapshot=snapshot,
        tables=train_tables,
        table_weights=table_weights,
        clamp=composition_clamp,
    )
    test_pair = _pair_diff_array(
        test,
        pair_stats=train_pairs,
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
    full_solo, _full_pairs = _solo_and_pair_stats(games)
    full_tables = fit_composition_tables(
        games,
        solo_wr=full_solo,
        snapshot=snapshot,
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
                "train_end_ms": train[-1].created_ms,
                "validation_end_ms": validation[-1].created_ms,
                "test_end_ms": test[-1].created_ms,
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
