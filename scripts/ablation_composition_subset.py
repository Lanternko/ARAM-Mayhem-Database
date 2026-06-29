"""Ablation: where do composition features actually matter?

Trains the champion-identity LR and the composition LR on the standard
benchmark split, then compares their test accuracy conditioned on how
extreme the damage-type mix of the match is (max team AD share).  Also
reports a no-learning per-champion win-rate-table baseline to show how
much of Champion LR is just "look up the win rates".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import (  # noqa: E402
    build_champion_profiles,
    champion_matrix,
)
from train_ability_nn import load_split_data  # noqa: E402
from train_composition_lr import (  # noqa: E402
    build_feature_blocks,
    build_team_profiles,
    fit_with_val_c,
    metrics,
    select_blocks,
)
from train_semantic_tree import train_frame_for_empirical_scores  # noqa: E402


def winrate_table_scores(dataset, n_champs: int, table: np.ndarray) -> np.ndarray:
    scores = np.zeros(len(dataset.blue), dtype=np.float64)
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
        scores[i] = sum(table[idx] for idx in blue) - sum(table[idx] for idx in red)
    return scores


def build_winrate_table(dataset, n_champs: int, labels: np.ndarray) -> np.ndarray:
    wins = np.zeros(n_champs)
    games = np.zeros(n_champs)
    for (blue, red), y in zip(zip(dataset.blue, dataset.red), labels):
        for idx in blue:
            games[idx] += 1
            wins[idx] += y
        for idx in red:
            games[idx] += 1
            wins[idx] += 1.0 - y
    wr = (wins + 5.0) / (games + 10.0)  # light shrinkage toward 0.5
    return np.log(wr / (1.0 - wr))


def paired_delta_se(correct_a: np.ndarray, correct_b: np.ndarray) -> float:
    diff = correct_a.astype(np.float64) - correct_b.astype(np.float64)
    return float(diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else float("nan")


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--feature-set", default="all_composition", show_default=True)
@click.option("--out", default=Path("outputs/ablation_composition_subset.json"), type=click.Path(path_type=Path), show_default=True)
@click.option("--seed", default=42, show_default=True)
def main(data: Path, score_csv: Path, patch_prefix: str, feature_set: str, out: Path, seed: int) -> None:
    np.random.seed(seed)

    print("[1/5] building champion profiles ...", flush=True)
    train_df = train_frame_for_empirical_scores(data, patch_prefix)
    profiles = build_champion_profiles(
        score_csv=score_csv, train_df=train_df, min_games=20, replace_sustain=True
    )
    splits = load_split_data(data, patch_prefix)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    n_champs = len(splits.champ_to_idx)
    print(f"      split: {len(splits.train)} / {len(splits.val)} / {len(splits.test)}", flush=True)

    y_train = np.asarray(splits.train.labels, dtype=np.float32)
    y_val = np.asarray(splits.val.labels, dtype=np.float32)
    y_test = np.asarray(splits.test.labels, dtype=np.float32)

    print("[2/5] team profiles + feature blocks ...", flush=True)
    train_blue, train_red = build_team_profiles(splits.train, idx_to_cid, profiles)
    val_blue, val_red = build_team_profiles(splits.val, idx_to_cid, profiles)
    test_blue, test_red = build_team_profiles(splits.test, idx_to_cid, profiles)

    champ_train = champion_matrix(splits.train, n_champs)
    champ_val = champion_matrix(splits.val, n_champs)
    champ_test = champion_matrix(splits.test, n_champs)

    comp_train_blocks, comp_names = select_blocks(build_feature_blocks(train_blue, train_red), feature_set)
    comp_val_blocks, _ = select_blocks(build_feature_blocks(val_blue, val_red), feature_set)
    comp_test_blocks, _ = select_blocks(build_feature_blocks(test_blue, test_red), feature_set)

    x_train = np.concatenate([champ_train, *comp_train_blocks], axis=1)
    x_val = np.concatenate([champ_val, *comp_val_blocks], axis=1)
    x_test = np.concatenate([champ_test, *comp_test_blocks], axis=1)

    print("[3/5] training champion LR ...", flush=True)
    champ_model, _ = fit_with_val_c(champ_train, y_train, champ_val, y_val)
    print("[4/5] training composition LR ...", flush=True)
    comp_model, _ = fit_with_val_c(x_train, y_train, x_val, y_val)

    champ_test_pred = champ_model.predict_proba(champ_test)[:, 1]
    comp_test_pred = comp_model.predict_proba(x_test)[:, 1]

    print("[5/5] win-rate table baseline + subset analysis ...", flush=True)
    table = build_winrate_table(splits.train, n_champs, y_train)
    naive_scores = winrate_table_scores(splits.test, n_champs, table)
    naive_pred = 1.0 / (1.0 + np.exp(-naive_scores))

    overall = {
        "winrate_table": metrics(y_test, naive_pred),
        "champion_lr": metrics(y_test, champ_test_pred),
        "composition_lr": metrics(y_test, comp_test_pred),
    }

    max_ad = np.array([max(b.ad_share, r.ad_share) for b, r in zip(test_blue, test_red)])
    champ_correct = (champ_test_pred > 0.5) == (y_test > 0.5)
    comp_correct = (comp_test_pred > 0.5) == (y_test > 0.5)

    buckets = [(0.0, 0.6, "<60% AD"), (0.6, 0.7, "60-70% AD"), (0.7, 0.8, "70-80% AD"), (0.8, 1.01, ">=80% AD")]
    subset_rows = []
    for lo, hi, name in buckets:
        mask = (max_ad >= lo) & (max_ad < hi)
        n = int(mask.sum())
        if n == 0:
            subset_rows.append({"bucket": name, "n": 0})
            continue
        acc_champ = float(champ_correct[mask].mean())
        acc_comp = float(comp_correct[mask].mean())
        subset_rows.append({
            "bucket": name,
            "n": n,
            "acc_champion_lr": round(acc_champ * 100, 2),
            "acc_composition_lr": round(acc_comp * 100, 2),
            "delta_pp": round((acc_comp - acc_champ) * 100, 2),
            "delta_se_pp": round(paired_delta_se(comp_correct[mask], champ_correct[mask]) * 100, 2),
        })

    result = {
        "data": str(data),
        "patch_prefix": patch_prefix,
        "feature_set": feature_set,
        "split": [len(splits.train), len(splits.val), len(splits.test)],
        "overall_test": overall,
        "subset_by_max_team_ad_share": subset_rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps(result["overall_test"], indent=2))
    print(f"{'bucket':<12} {'n':>7} {'champLR':>9} {'compLR':>9} {'delta':>7} {'SE':>6}")
    for row in subset_rows:
        if row["n"] == 0:
            print(f"{row['bucket']:<12} {0:>7}")
            continue
        print(f"{row['bucket']:<12} {row['n']:>7} {row['acc_champion_lr']:>8.2f}% {row['acc_composition_lr']:>8.2f}% {row['delta_pp']:>+6.2f} {row['delta_se_pp']:>6.2f}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
