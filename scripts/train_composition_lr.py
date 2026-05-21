"""Train a champion-identity + team-composition logistic baseline.

This is the promotable version of the composition signal analysis.  The model
keeps the champion-identity LR baseline and appends explicit team-composition
features built from train-only empirical combat stats and semantic role/score
metadata.
"""
from __future__ import annotations

import csv
import json
import pickle
import sys
from pathlib import Path
from typing import Any

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from aram_nn.eval import accuracy_np, ece_np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import (  # noqa: E402
    AD_BINS,
    C_GRID,
    ENGAGE_GROUPS,
    FRONT_GROUPS,
    POKE_GROUPS,
    ROLE_COLUMNS,
    SCORE_COLUMNS,
    WAVE_GROUPS,
    ad_bin_index,
    build_champion_profiles,
    champion_matrix,
    count_group_index,
    signed_category_matrix,
    signed_numeric_matrix,
    team_profile,
)
from train_ability_nn import load_split_data  # noqa: E402
from train_semantic_tree import train_frame_for_empirical_scores  # noqa: E402


FEATURE_SET_CHOICES = (
    "all_composition",
    "all_interactions",
    "ad_balance",
    "ad_front",
    "selected_core",
    "selected_core_wave",
    "role_ad",
)


def metrics(y_true: np.ndarray, prob: np.ndarray) -> dict[str, float]:
    return {
        "log_loss": float(log_loss(y_true, prob)),
        "acc": float(accuracy_np(y_true, prob)),
        "ece": float(ece_np(y_true, prob)),
    }


def fit_with_val_c(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
) -> tuple[LogisticRegression, float]:
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
    return model, float(best_c)


def linear_feature_names() -> list[str]:
    return [
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


def linear_features(team) -> list[float]:
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


def role_ad_features(team) -> list[float]:
    ad_offset = ad_bin_index(team.ad_share) * len(ROLE_COLUMNS)
    values = [0.0] * (len(AD_BINS) * len(ROLE_COLUMNS))
    for role_idx, role in enumerate(ROLE_COLUMNS):
        values[ad_offset + role_idx] = team.roles[role]
    return values


def build_team_profiles(dataset, idx_to_cid: dict[int, int], profiles):
    return (
        [team_profile(team, idx_to_cid, profiles) for team in dataset.blue],
        [team_profile(team, idx_to_cid, profiles) for team in dataset.red],
    )


def build_feature_blocks(blue_profiles, red_profiles) -> dict[str, tuple[np.ndarray, list[str]]]:
    linear_names = linear_feature_names()
    linear = signed_numeric_matrix(blue_profiles, red_profiles, linear_features)
    ad_balance_cols = [linear_names.index("ad_share"), linear_names.index("ad_ap_balance")]
    damage_type_cols = [
        linear_names.index("ad_share"),
        linear_names.index("ad_ap_balance"),
        linear_names.index("true_share"),
    ]
    frontline_cols = [
        linear_names.index("front_count"),
        linear_names.index("front_sum"),
        linear_names.index("lack_frontline_score"),
    ]
    role_cols = [i for i, name in enumerate(linear_names) if name.startswith("role_")]
    wave_cols = [
        linear_names.index("sum_wave_clear_score"),
        linear_names.index("lack_wave_clear_score"),
    ]

    ad_front = signed_category_matrix(
        blue_profiles,
        red_profiles,
        n_features=len(AD_BINS) * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * len(AD_BINS)
        + ad_bin_index(team.ad_share),
    )
    ad_front_names = [
        f"ad_front:{front_group}:{ad_bin}"
        for front_group in FRONT_GROUPS
        for ad_bin in AD_BINS
    ]

    wave_engage = signed_category_matrix(
        blue_profiles,
        red_profiles,
        n_features=4,
        category_fn=lambda team: int(team.lacks["wave_clear_score"] == 0.0) * 2
        + int(team.lacks["engage_score"] == 0.0),
    )
    wave_engage_names = [
        f"wave_engage:{wave_group}:{engage_group}"
        for wave_group in WAVE_GROUPS
        for engage_group in ENGAGE_GROUPS
    ]

    poke_front = signed_category_matrix(
        blue_profiles,
        red_profiles,
        n_features=2 * len(FRONT_GROUPS),
        category_fn=lambda team: count_group_index(float(team.front_count)) * 2
        + int(team.lacks["poke_score"] == 0.0),
    )
    poke_front_names = [
        f"poke_front:{front_group}:{poke_group}"
        for front_group in FRONT_GROUPS
        for poke_group in POKE_GROUPS
    ]

    role_ad = signed_numeric_matrix(blue_profiles, red_profiles, role_ad_features)
    role_ad_names = [
        f"role_ad:{ad_bin}:{role.lower()}"
        for ad_bin in AD_BINS
        for role in ROLE_COLUMNS
    ]

    return {
        "linear": (linear, linear_names),
        "ad_balance": (linear[:, ad_balance_cols], [linear_names[i] for i in ad_balance_cols]),
        "damage_type": (linear[:, damage_type_cols], [linear_names[i] for i in damage_type_cols]),
        "frontline": (linear[:, frontline_cols], [linear_names[i] for i in frontline_cols]),
        "roles": (linear[:, role_cols], [linear_names[i] for i in role_cols]),
        "wave": (linear[:, wave_cols], [linear_names[i] for i in wave_cols]),
        "ad_front": (ad_front, ad_front_names),
        "wave_engage": (wave_engage, wave_engage_names),
        "poke_front": (poke_front, poke_front_names),
        "role_ad": (role_ad, role_ad_names),
    }


def select_blocks(blocks: dict[str, tuple[np.ndarray, list[str]]], feature_set: str) -> tuple[list[np.ndarray], list[str]]:
    if feature_set == "ad_balance":
        keys = ["ad_balance"]
    elif feature_set == "ad_front":
        keys = ["ad_front"]
    elif feature_set == "role_ad":
        keys = ["role_ad"]
    elif feature_set == "selected_core":
        keys = ["damage_type", "frontline", "roles", "ad_front", "poke_front", "role_ad"]
    elif feature_set == "selected_core_wave":
        keys = [
            "damage_type",
            "frontline",
            "roles",
            "wave",
            "ad_front",
            "poke_front",
            "role_ad",
            "wave_engage",
        ]
    elif feature_set == "all_interactions":
        keys = ["ad_front", "wave_engage", "poke_front", "role_ad"]
    elif feature_set == "all_composition":
        keys = ["linear", "ad_front", "wave_engage", "poke_front", "role_ad"]
    else:
        raise click.ClickException(f"Unknown feature_set={feature_set}")

    arrays: list[np.ndarray] = []
    names: list[str] = []
    for key in keys:
        array, feature_names = blocks[key]
        arrays.append(array)
        names.extend(feature_names)
    return arrays, names


def write_feature_names(path: Path, feature_names: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "feature_name"])
        for i, name in enumerate(feature_names):
            writer.writerow([i, name])


def write_coefficients(path: Path, model: LogisticRegression, feature_names: list[str]) -> None:
    coefs = model.coef_[0]
    rows = sorted(
        (
            {
                "index": i,
                "feature_name": name,
                "coef": float(coefs[i]),
                "abs_coef": float(abs(coefs[i])),
            }
            for i, name in enumerate(feature_names)
        ),
        key=lambda row: row["abs_coef"],
        reverse=True,
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["index", "feature_name", "coef", "abs_coef"])
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
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--feature-set", type=click.Choice(FEATURE_SET_CHOICES), default="all_composition", show_default=True)
@click.option("--empirical-min-games", default=20, show_default=True)
@click.option("--replace-sustain/--keep-static-sustain", default=True, show_default=True)
@click.option("--seed", default=42, show_default=True)
def main(
    data: Path,
    score_csv: Path,
    patch_prefix: str,
    out: Path,
    feature_set: str,
    empirical_min_games: int,
    replace_sustain: bool,
    seed: int,
) -> None:
    np.random.seed(seed)
    train_df = train_frame_for_empirical_scores(data, patch_prefix)
    profiles = build_champion_profiles(
        score_csv=score_csv,
        train_df=train_df,
        min_games=empirical_min_games,
        replace_sustain=replace_sustain,
    )
    splits = load_split_data(data, patch_prefix)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    n_champs = len(splits.champ_to_idx)

    y_train = np.asarray(splits.train.labels, dtype=np.float32)
    y_val = np.asarray(splits.val.labels, dtype=np.float32)
    y_test = np.asarray(splits.test.labels, dtype=np.float32)

    train_blue, train_red = build_team_profiles(splits.train, idx_to_cid, profiles)
    val_blue, val_red = build_team_profiles(splits.val, idx_to_cid, profiles)
    test_blue, test_red = build_team_profiles(splits.test, idx_to_cid, profiles)

    champ_train = champion_matrix(splits.train, n_champs)
    champ_val = champion_matrix(splits.val, n_champs)
    champ_test = champion_matrix(splits.test, n_champs)
    champion_feature_names = [f"champion:{cid}" for cid, _idx in sorted(splits.champ_to_idx.items(), key=lambda item: item[1])]

    train_blocks = build_feature_blocks(train_blue, train_red)
    val_blocks = build_feature_blocks(val_blue, val_red)
    test_blocks = build_feature_blocks(test_blue, test_red)
    comp_train_blocks, comp_feature_names = select_blocks(train_blocks, feature_set)
    comp_val_blocks, _ = select_blocks(val_blocks, feature_set)
    comp_test_blocks, _ = select_blocks(test_blocks, feature_set)

    x_train = np.concatenate([champ_train, *comp_train_blocks], axis=1)
    x_val = np.concatenate([champ_val, *comp_val_blocks], axis=1)
    x_test = np.concatenate([champ_test, *comp_test_blocks], axis=1)
    feature_names = champion_feature_names + comp_feature_names

    champ_model, champ_c = fit_with_val_c(champ_train, y_train, champ_val, y_val)
    model, best_c = fit_with_val_c(x_train, y_train, x_val, y_val)

    champ_val_pred = champ_model.predict_proba(champ_val)[:, 1]
    champ_test_pred = champ_model.predict_proba(champ_test)[:, 1]
    val_pred = model.predict_proba(x_val)[:, 1]
    test_pred = model.predict_proba(x_test)[:, 1]

    summary: dict[str, Any] = {
        "data": str(data),
        "score_csv": str(score_csv),
        "patch_prefix": patch_prefix,
        "feature_set": feature_set,
        "seed": seed,
        "empirical_min_games": empirical_min_games,
        "replace_sustain": replace_sustain,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "n_champs": n_champs,
        "n_features": int(x_train.shape[1]),
        "n_composition_features": len(comp_feature_names),
        "best_C": best_c,
        "champion_baseline_C": champ_c,
        "results": {
            "val/champion_only": metrics(y_val, champ_val_pred),
            "test/champion_only": metrics(y_test, champ_test_pred),
            "val/composition_lr": metrics(y_val, val_pred),
            "test/composition_lr": metrics(y_test, test_pred),
        },
    }
    summary["val_log_loss_delta_vs_champion"] = (
        summary["results"]["val/composition_lr"]["log_loss"]
        - summary["results"]["val/champion_only"]["log_loss"]
    )
    summary["test_log_loss_delta_vs_champion"] = (
        summary["results"]["test/composition_lr"]["log_loss"]
        - summary["results"]["test/champion_only"]["log_loss"]
    )

    out.mkdir(parents=True, exist_ok=True)
    write_feature_names(out / "feature_names.csv", feature_names)
    write_coefficients(out / "coefficients.csv", model, feature_names)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    profiles_payload = {
        int(cid): {
            "cid": int(profile.cid),
            "scores": profile.scores,
            "roles": profile.roles,
            "physical_dpm": profile.physical_dpm,
            "magic_dpm": profile.magic_dpm,
            "true_dpm": profile.true_dpm,
        }
        for cid, profile in profiles.items()
    }
    with (out / "model.pkl").open("wb") as f:
        pickle.dump(
            {
                "model": model,
                "champion_baseline_model": champ_model,
                "feature_set": feature_set,
                "feature_names": feature_names,
                "champ_to_idx": splits.champ_to_idx,
                "idx_to_cid": idx_to_cid,
                "champion_profiles": profiles_payload,
                "config": {
                    "data": str(data),
                    "score_csv": str(score_csv),
                    "patch_prefix": patch_prefix,
                    "empirical_min_games": empirical_min_games,
                    "replace_sustain": replace_sustain,
                    "seed": seed,
                },
            },
            f,
        )

    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={n_champs} n_features={x_train.shape[1]}"
    )
    click.echo(
        "[champion_only] "
        f"val_ll={summary['results']['val/champion_only']['log_loss']:.6f} "
        f"test_ll={summary['results']['test/champion_only']['log_loss']:.6f} "
        f"test_acc={summary['results']['test/champion_only']['acc']:.4f}"
    )
    click.echo(
        "[composition_lr] "
        f"feature_set={feature_set} C={best_c} "
        f"val_ll={summary['results']['val/composition_lr']['log_loss']:.6f} "
        f"test_ll={summary['results']['test/composition_lr']['log_loss']:.6f} "
        f"test_acc={summary['results']['test/composition_lr']['acc']:.4f} "
        f"test_delta={summary['test_log_loss_delta_vs_champion']:+.6f}"
    )
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'model.pkl'}")
    click.echo(f"[saved] {out / 'feature_names.csv'}")
    click.echo(f"[saved] {out / 'coefficients.csv'}")


if __name__ == "__main__":
    main()
