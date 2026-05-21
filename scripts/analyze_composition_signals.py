"""Analyze Mayhem team-composition signals after champion-strength control.

This script uses the frozen time split from the training pipeline:

1. fit champion-identity LR on train;
2. build team-level composition descriptors from train-only empirical combat
   stats plus semantic score tags;
3. compare incremental LR feature groups by validation log_loss;
4. summarize actual WR and champion-baseline residuals for key interaction
   cells.

Outputs are CSV/JSON artifacts under the requested output directory.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_empirical_champion_scores import (  # noqa: E402
    blended_percentile_scores,
    collect_empirical_stats_from_rows,
)
from train_ability_nn import load_split_data  # noqa: E402
from train_semantic_tree import LACK_THRESHOLDS, SCORE_COLUMNS, train_frame_for_empirical_scores  # noqa: E402
from champion_roles import ROLE_ORDER  # noqa: E402


ROLE_COLUMNS = ROLE_ORDER
AD_BINS = ("<35% AD", "35-45% AD", "45-55% AD", "55-65% AD", ">=65% AD")
FRONT_GROUPS = ("0 front", "1 front", "2+ front")
ENGAGE_GROUPS = ("engage lack", "engage ok")
WAVE_GROUPS = ("wave lack", "wave ok")
POKE_GROUPS = ("poke lack", "poke ok")
COUNT_GROUPS = ("0", "1", "2+")
C_GRID = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0)


@dataclass(frozen=True)
class ChampionProfile:
    cid: int
    scores: dict[str, float]
    roles: dict[str, float]
    physical_dpm: float
    magic_dpm: float
    true_dpm: float


@dataclass(frozen=True)
class TeamProfile:
    ad_share: float
    ap_share: float
    true_share: float
    ad_ap_balance: float
    front_count: int
    front_sum: float
    score_sums: dict[str, float]
    lacks: dict[str, float]
    roles: dict[str, float]
    core_lacks_count: float
    all_lacks_count: float


def load_score_rows(path: Path) -> dict[int, dict[str, str]]:
    return {
        int(row["champion_id"]): row
        for row in csv.DictReader(path.open(encoding="utf-8-sig"))
    }


def build_champion_profiles(
    *,
    score_csv: Path,
    train_df,
    min_games: int,
    replace_sustain: bool,
) -> dict[int, ChampionProfile]:
    rows = load_score_rows(score_csv)
    stats = collect_empirical_stats_from_rows(
        train_df.select(["blue_wins", "duration_sec", "participants_json"]).iter_rows()
    )
    damage_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="damage_share", metric_b="damage_per_min"
    )
    cc_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="cc_share", metric_b="cc_per_min"
    )
    frontline_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="frontline_share", metric_b="frontline_per_min"
    )
    sustain_scores = blended_percentile_scores(
        stats, min_games=min_games, metric_a="sustain_share", metric_b="sustain_per_min"
    )

    out: dict[int, ChampionProfile] = {}
    for cid, row in rows.items():
        champion_scores = {name: float(row[name]) for name in SCORE_COLUMNS}
        stat = stats.get(cid, {})
        has_empirical = stat.get("games", 0) >= min_games
        if has_empirical:
            if cid in damage_scores:
                champion_scores["damage_score"] = damage_scores[cid]
            if cid in cc_scores:
                champion_scores["cc_score"] = cc_scores[cid]
            if cid in frontline_scores:
                champion_scores["frontline_score"] = frontline_scores[cid]
            if replace_sustain and cid in sustain_scores:
                champion_scores["sustain_score"] = sustain_scores[cid]

        tags = set((row.get("tags") or "").split("|"))
        roles = {role: 1.0 if role in tags else 0.0 for role in ROLE_COLUMNS}

        damage_per_min = float(stat.get("damage_per_min", 0.0)) if has_empirical else 0.0
        physical_dpm = damage_per_min * float(stat.get("physical_damage_ratio", 0.0)) if has_empirical else 0.0
        magic_dpm = damage_per_min * float(stat.get("magic_damage_ratio", 0.0)) if has_empirical else 0.0
        true_dpm = damage_per_min * float(stat.get("true_damage_ratio", 0.0)) if has_empirical else 0.0

        out[cid] = ChampionProfile(
            cid=cid,
            scores=champion_scores,
            roles=roles,
            physical_dpm=physical_dpm,
            magic_dpm=magic_dpm,
            true_dpm=true_dpm,
        )
    return out


def ad_bin_index(ad_share: float) -> int:
    if ad_share < 0.35:
        return 0
    if ad_share < 0.45:
        return 1
    if ad_share < 0.55:
        return 2
    if ad_share < 0.65:
        return 3
    return 4


def count_group_index(count: float) -> int:
    if count <= 0:
        return 0
    if count == 1:
        return 1
    return 2


def team_profile(indices: list[int], idx_to_cid: dict[int, int], profiles: dict[int, ChampionProfile]) -> TeamProfile:
    physical = magic = true = 0.0
    score_sums = {name: 0.0 for name in SCORE_COLUMNS}
    roles = {role: 0.0 for role in ROLE_COLUMNS}
    for idx in indices:
        profile = profiles[idx_to_cid[int(idx)]]
        physical += profile.physical_dpm
        magic += profile.magic_dpm
        true += profile.true_dpm
        for name in SCORE_COLUMNS:
            score_sums[name] += profile.scores[name]
        for role in ROLE_COLUMNS:
            roles[role] += profile.roles[role]

    ad_ap_den = max(physical + magic, 1e-9)
    all_den = max(physical + magic + true, 1e-9)
    ad_share = physical / ad_ap_den
    ap_share = magic / ad_ap_den
    true_share = true / all_den
    lacks = {
        name: 1.0 if score_sums[name] < LACK_THRESHOLDS[name] else 0.0
        for name in SCORE_COLUMNS
    }
    core_names = ("wave_clear_score", "cc_score", "engage_score", "damage_score")
    front_count = sum(
        1
        for idx in indices
        if profiles[idx_to_cid[int(idx)]].scores["frontline_score"] >= 2.0
    )
    return TeamProfile(
        ad_share=ad_share,
        ap_share=ap_share,
        true_share=true_share,
        ad_ap_balance=1.0 - abs(ad_share - ap_share),
        front_count=front_count,
        front_sum=score_sums["frontline_score"],
        score_sums=score_sums,
        lacks=lacks,
        roles=roles,
        core_lacks_count=sum(lacks[name] for name in core_names),
        all_lacks_count=sum(lacks.values()),
    )


def signed_category_matrix(
    blue_profiles: list[TeamProfile],
    red_profiles: list[TeamProfile],
    *,
    n_features: int,
    category_fn: Callable[[TeamProfile], int],
) -> np.ndarray:
    x = np.zeros((len(blue_profiles), n_features), dtype=np.float32)
    for i, (blue, red) in enumerate(zip(blue_profiles, red_profiles, strict=True)):
        x[i, category_fn(blue)] += 1.0
        x[i, category_fn(red)] -= 1.0
    return x


def signed_numeric_matrix(
    blue_profiles: list[TeamProfile],
    red_profiles: list[TeamProfile],
    feature_fn: Callable[[TeamProfile], list[float]],
) -> np.ndarray:
    first = feature_fn(blue_profiles[0])
    x = np.zeros((len(blue_profiles), len(first)), dtype=np.float32)
    for i, (blue, red) in enumerate(zip(blue_profiles, red_profiles, strict=True)):
        x[i] = np.asarray(feature_fn(blue), dtype=np.float32) - np.asarray(feature_fn(red), dtype=np.float32)
    return x


def champion_matrix(dataset, n_champs: int) -> np.ndarray:
    x = np.zeros((len(dataset), n_champs), dtype=np.float32)
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red, strict=True)):
        for c in blue:
            x[i, int(c)] = 1.0
        for c in red:
            x[i, int(c)] = -1.0
    return x


def fit_eval(
    *,
    name: str,
    x_train_blocks: list[np.ndarray],
    x_val_blocks: list[np.ndarray],
    x_test_blocks: list[np.ndarray],
    y_train: np.ndarray,
    y_val: np.ndarray,
    y_test: np.ndarray,
) -> tuple[dict[str, float | str], np.ndarray, np.ndarray]:
    x_train = np.concatenate(x_train_blocks, axis=1)
    x_val = np.concatenate(x_val_blocks, axis=1)
    x_test = np.concatenate(x_test_blocks, axis=1)
    best_c = C_GRID[0]
    best_ll = float("inf")
    for c_value in C_GRID:
        model = LogisticRegression(C=c_value, max_iter=2000, solver="lbfgs")
        model.fit(x_train, y_train)
        pred_val = model.predict_proba(x_val)[:, 1]
        val_ll = log_loss(y_val, pred_val)
        if val_ll < best_ll:
            best_c = c_value
            best_ll = val_ll
    model = LogisticRegression(C=best_c, max_iter=2000, solver="lbfgs")
    model.fit(x_train, y_train)
    pred_val = model.predict_proba(x_val)[:, 1]
    pred_test = model.predict_proba(x_test)[:, 1]
    metrics = {
        "model": name,
        "C": best_c,
        "n_features": int(x_train.shape[1]),
        "val_log_loss": float(log_loss(y_val, pred_val)),
        "val_acc": float(((pred_val >= 0.5) == (y_val > 0.5)).mean()),
        "test_log_loss": float(log_loss(y_test, pred_test)),
        "test_acc": float(((pred_test >= 0.5) == (y_test > 0.5)).mean()),
    }
    return metrics, pred_val, pred_test


def summarize_rows(rows: list[dict[str, float | str]]) -> dict[str, float | int]:
    won = np.asarray([float(row["won"]) for row in rows], dtype=np.float64)
    pred = np.asarray([float(row["champ_pred"]) for row in rows], dtype=np.float64)
    residual = won - pred
    wr = float(won.mean())
    residual_mean = float(residual.mean())
    return {
        "n": len(rows),
        "actual_wr": wr,
        "actual_wr_ci95": float(1.96 * math.sqrt(max(wr * (1.0 - wr), 0.0) / len(rows))),
        "champ_pred_wr": float(pred.mean()),
        "residual": residual_mean,
        "residual_ci95": float(1.96 * residual.std(ddof=1) / math.sqrt(len(rows))) if len(rows) > 1 else 0.0,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--score-csv",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out-dir", type=click.Path(path_type=Path), default=Path("models/composition_signals_2026_05_18"), show_default=True)
@click.option("--empirical-min-games", default=20, show_default=True)
@click.option("--replace-sustain/--keep-static-sustain", default=True, show_default=True)
def main(
    data: Path,
    score_csv: Path,
    patch_prefix: str,
    out_dir: Path,
    empirical_min_games: int,
    replace_sustain: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    train_df = train_frame_for_empirical_scores(data, patch_prefix)
    profiles = build_champion_profiles(
        score_csv=score_csv,
        train_df=train_df,
        min_games=empirical_min_games,
        replace_sustain=replace_sustain,
    )
    splits = load_split_data(data, patch_prefix)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    y_train = np.asarray(splits.train.labels, dtype=np.float32)
    y_val = np.asarray(splits.val.labels, dtype=np.float32)
    y_test = np.asarray(splits.test.labels, dtype=np.float32)

    def build_team_profiles(dataset) -> tuple[list[TeamProfile], list[TeamProfile]]:
        blue_profiles = [
            team_profile(team, idx_to_cid, profiles)
            for team in dataset.blue
        ]
        red_profiles = [
            team_profile(team, idx_to_cid, profiles)
            for team in dataset.red
        ]
        return blue_profiles, red_profiles

    train_blue, train_red = build_team_profiles(splits.train)
    val_blue, val_red = build_team_profiles(splits.val)
    test_blue, test_red = build_team_profiles(splits.test)

    n_champs = len(splits.champ_to_idx)
    champ_train = champion_matrix(splits.train, n_champs)
    champ_val = champion_matrix(splits.val, n_champs)
    champ_test = champion_matrix(splits.test, n_champs)

    linear_names = [
        "ad_share",
        "ad_ap_balance",
        "true_share",
        "front_count",
        "front_sum",
        "core_lacks_count",
        "all_lacks_count",
        *[f"sum_{name}" for name in SCORE_COLUMNS],
        *[f"lack_{name}" for name in SCORE_COLUMNS],
        *[f"role_{role.lower()}" for role in ROLE_COLUMNS],
    ]

    def linear_features(team: TeamProfile) -> list[float]:
        return [
            team.ad_share,
            team.ad_ap_balance,
            team.true_share,
            float(team.front_count),
            team.front_sum,
            team.core_lacks_count,
            team.all_lacks_count,
            *[team.score_sums[name] for name in SCORE_COLUMNS],
            *[team.lacks[name] for name in SCORE_COLUMNS],
            *[team.roles[role] for role in ROLE_COLUMNS],
        ]

    linear_train = signed_numeric_matrix(train_blue, train_red, linear_features)
    linear_val = signed_numeric_matrix(val_blue, val_red, linear_features)
    linear_test = signed_numeric_matrix(test_blue, test_red, linear_features)

    ad_balance_cols = [linear_names.index("ad_share"), linear_names.index("ad_ap_balance")]
    true_cols = [linear_names.index("true_share")]
    frontline_cols = [linear_names.index("front_count"), linear_names.index("front_sum"), linear_names.index("lack_frontline_score")]
    lack_cols = [i for i, name in enumerate(linear_names) if name.startswith("lack_") or name.endswith("lacks_count")]
    semantic_sum_cols = [i for i, name in enumerate(linear_names) if name.startswith("sum_")]
    role_cols = [i for i, name in enumerate(linear_names) if name.startswith("role_")]

    ad_front_train = signed_category_matrix(
        train_blue,
        train_red,
        n_features=len(AD_BINS) * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * len(AD_BINS) + ad_bin_index(team.ad_share),
    )
    ad_front_val = signed_category_matrix(
        val_blue,
        val_red,
        n_features=len(AD_BINS) * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * len(AD_BINS) + ad_bin_index(team.ad_share),
    )
    ad_front_test = signed_category_matrix(
        test_blue,
        test_red,
        n_features=len(AD_BINS) * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * len(AD_BINS) + ad_bin_index(team.ad_share),
    )

    wave_engage_train = signed_category_matrix(
        train_blue,
        train_red,
        n_features=4,
        category_fn=lambda team: int(team.lacks["wave_clear_score"] == 0.0) * 2
        + int(team.lacks["engage_score"] == 0.0),
    )
    wave_engage_val = signed_category_matrix(
        val_blue,
        val_red,
        n_features=4,
        category_fn=lambda team: int(team.lacks["wave_clear_score"] == 0.0) * 2
        + int(team.lacks["engage_score"] == 0.0),
    )
    wave_engage_test = signed_category_matrix(
        test_blue,
        test_red,
        n_features=4,
        category_fn=lambda team: int(team.lacks["wave_clear_score"] == 0.0) * 2
        + int(team.lacks["engage_score"] == 0.0),
    )

    poke_front_train = signed_category_matrix(
        train_blue,
        train_red,
        n_features=2 * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * 2
        + int(team.lacks["poke_score"] == 0.0),
    )
    poke_front_val = signed_category_matrix(
        val_blue,
        val_red,
        n_features=2 * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * 2
        + int(team.lacks["poke_score"] == 0.0),
    )
    poke_front_test = signed_category_matrix(
        test_blue,
        test_red,
        n_features=2 * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * 2
        + int(team.lacks["poke_score"] == 0.0),
    )

    def role_ad_features(team: TeamProfile) -> list[float]:
        ad_offset = ad_bin_index(team.ad_share) * len(ROLE_COLUMNS)
        values = [0.0] * (len(AD_BINS) * len(ROLE_COLUMNS))
        for role_idx, role in enumerate(ROLE_COLUMNS):
            values[ad_offset + role_idx] = team.roles[role]
        return values

    role_ad_train = signed_numeric_matrix(train_blue, train_red, role_ad_features)
    role_ad_val = signed_numeric_matrix(val_blue, val_red, role_ad_features)
    role_ad_test = signed_numeric_matrix(test_blue, test_red, role_ad_features)

    feature_sets = [
        ("champ_only", [], [], []),
        ("composition_only_all", [linear_train, ad_front_train, wave_engage_train, poke_front_train, role_ad_train], [linear_val, ad_front_val, wave_engage_val, poke_front_val, role_ad_val], [linear_test, ad_front_test, wave_engage_test, poke_front_test, role_ad_test]),
        ("champ_plus_ad_balance", [linear_train[:, ad_balance_cols]], [linear_val[:, ad_balance_cols]], [linear_test[:, ad_balance_cols]]),
        ("champ_plus_true_only", [linear_train[:, true_cols]], [linear_val[:, true_cols]], [linear_test[:, true_cols]]),
        ("champ_plus_frontline", [linear_train[:, frontline_cols]], [linear_val[:, frontline_cols]], [linear_test[:, frontline_cols]]),
        ("champ_plus_lacks", [linear_train[:, lack_cols]], [linear_val[:, lack_cols]], [linear_test[:, lack_cols]]),
        ("champ_plus_semantic_sums", [linear_train[:, semantic_sum_cols]], [linear_val[:, semantic_sum_cols]], [linear_test[:, semantic_sum_cols]]),
        ("champ_plus_roles", [linear_train[:, role_cols]], [linear_val[:, role_cols]], [linear_test[:, role_cols]]),
        ("champ_plus_ad_front", [ad_front_train], [ad_front_val], [ad_front_test]),
        ("champ_plus_wave_engage", [wave_engage_train], [wave_engage_val], [wave_engage_test]),
        ("champ_plus_poke_front", [poke_front_train], [poke_front_val], [poke_front_test]),
        ("champ_plus_role_ad", [role_ad_train], [role_ad_val], [role_ad_test]),
        ("champ_plus_all_interactions", [ad_front_train, wave_engage_train, poke_front_train, role_ad_train], [ad_front_val, wave_engage_val, poke_front_val, role_ad_val], [ad_front_test, wave_engage_test, poke_front_test, role_ad_test]),
        ("champ_plus_all_composition", [linear_train, ad_front_train, wave_engage_train, poke_front_train, role_ad_train], [linear_val, ad_front_val, wave_engage_val, poke_front_val, role_ad_val], [linear_test, ad_front_test, wave_engage_test, poke_front_test, role_ad_test]),
    ]

    metrics: list[dict[str, float | str]] = []
    predictions: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, train_blocks, val_blocks, test_blocks in feature_sets:
        base_train = [] if name == "composition_only_all" else [champ_train]
        base_val = [] if name == "composition_only_all" else [champ_val]
        base_test = [] if name == "composition_only_all" else [champ_test]
        result, pred_val, pred_test = fit_eval(
            name=name,
            x_train_blocks=base_train + train_blocks,
            x_val_blocks=base_val + val_blocks,
            x_test_blocks=base_test + test_blocks,
            y_train=y_train,
            y_val=y_val,
            y_test=y_test,
        )
        metrics.append(result)
        predictions[name] = (pred_val, pred_test)

    champ_val_pred, champ_test_pred = predictions["champ_only"]
    baseline_val = next(row["val_log_loss"] for row in metrics if row["model"] == "champ_only")
    baseline_test = next(row["test_log_loss"] for row in metrics if row["model"] == "champ_only")
    for row in metrics:
        row["val_ll_delta_vs_champ"] = float(row["val_log_loss"]) - float(baseline_val)
        row["test_ll_delta_vs_champ"] = float(row["test_log_loss"]) - float(baseline_test)

    side_rows: list[dict[str, float | str]] = []
    for split, dataset, blue_profiles, red_profiles, preds in (
        ("val", splits.val, val_blue, val_red, champ_val_pred),
        ("test", splits.test, test_blue, test_red, champ_test_pred),
    ):
        for i, (blue, red, label) in enumerate(zip(blue_profiles, red_profiles, dataset.labels, strict=True)):
            blue_win = float(label)
            pred = float(preds[i])
            for side, profile, won, champ_pred in (
                ("blue", blue, blue_win, pred),
                ("red", red, 1.0 - blue_win, 1.0 - pred),
            ):
                side_rows.append(
                    {
                        "split": split,
                        "side": side,
                        "won": won,
                        "champ_pred": champ_pred,
                        "ad_bin": AD_BINS[ad_bin_index(profile.ad_share)],
                        "front_group": FRONT_GROUPS[count_group_index(float(profile.front_count))],
                        "wave_group": WAVE_GROUPS[int(profile.lacks["wave_clear_score"] == 0.0)],
                        "engage_group": ENGAGE_GROUPS[int(profile.lacks["engage_score"] == 0.0)],
                        "poke_group": POKE_GROUPS[int(profile.lacks["poke_score"] == 0.0)],
                        "core_lacks_group": COUNT_GROUPS[count_group_index(profile.core_lacks_count)],
                        "all_lacks_group": COUNT_GROUPS[count_group_index(profile.all_lacks_count)],
                        "mage_group": COUNT_GROUPS[count_group_index(profile.roles["Mage"])],
                        "marksman_group": COUNT_GROUPS[count_group_index(profile.roles["Marksman"])],
                        "ad_share": profile.ad_share,
                        "true_share": profile.true_share,
                        "front_count": float(profile.front_count),
                        "wave_sum": profile.score_sums["wave_clear_score"],
                        "engage_sum": profile.score_sums["engage_score"],
                        "poke_sum": profile.score_sums["poke_score"],
                    }
                )

    def summarize_interaction(keys: list[str], min_n: int = 0) -> list[dict[str, object]]:
        grouped: dict[tuple[object, ...], list[dict[str, float | str]]] = {}
        for row in side_rows:
            key = tuple(row[k] for k in keys)
            grouped.setdefault(key, []).append(row)
        out_rows: list[dict[str, object]] = []
        for key, rows_for_key in sorted(grouped.items(), key=lambda item: item[0]):
            if len(rows_for_key) < min_n:
                continue
            summary = summarize_rows(rows_for_key)
            out = {k: v for k, v in zip(keys, key, strict=True)}
            out.update(summary)
            out_rows.append(out)
        return out_rows

    interaction_outputs = {
        "ad_front": summarize_interaction(["front_group", "ad_bin"]),
        "wave_engage": summarize_interaction(["wave_group", "engage_group"]),
        "poke_front": summarize_interaction(["front_group", "poke_group"]),
        "core_lacks": summarize_interaction(["core_lacks_group"]),
        "all_lacks": summarize_interaction(["all_lacks_group"]),
        "mage_ad": summarize_interaction(["mage_group", "ad_bin"], min_n=300),
        "marksman_ad": summarize_interaction(["marksman_group", "ad_bin"], min_n=300),
    }

    write_csv(out_dir / "model_metrics.csv", metrics)
    for name, rows in interaction_outputs.items():
        write_csv(out_dir / f"{name}_cells.csv", rows)

    payload = {
        "data": str(data),
        "score_csv": str(score_csv),
        "patch_prefix": patch_prefix,
        "empirical_min_games": empirical_min_games,
        "replace_sustain": replace_sustain,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "linear_feature_names": linear_names,
        "model_metrics": metrics,
        "interactions": interaction_outputs,
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    click.echo(f"[saved] {out_dir / 'summary.json'}")
    click.echo(f"[saved] {out_dir / 'model_metrics.csv'}")
    for name in interaction_outputs:
        click.echo(f"[saved] {out_dir / f'{name}_cells.csv'}")

    click.echo("\n[model metrics]")
    for row in sorted(metrics, key=lambda r: float(r["val_log_loss"])):
        click.echo(
            f"  {row['model']:<30} val_ll={float(row['val_log_loss']):.6f} "
            f"test_ll={float(row['test_log_loss']):.6f} "
            f"val_delta={float(row['val_ll_delta_vs_champ']):+.6f} "
            f"test_delta={float(row['test_ll_delta_vs_champ']):+.6f}"
        )


if __name__ == "__main__":
    main()
