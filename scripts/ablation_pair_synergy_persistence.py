"""Ablation: does train-selected pairwise synergy survive out of sample?

Tests the natural intuition "surely one pair stands out". Defines a pair's
synergy as the average residual win it adds on top of the additive Champion-LR
prediction, then asks whether pairs that look synergistic on TRAIN still look
synergistic on the held-out TEST split. If the signal is real, train and test
synergy correlate and the top pairs stay high. If it is Bernoulli noise, the
top-train pairs collapse toward zero on test (winner's curse).
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix, C_GRID  # noqa: E402
from train_ability_nn import load_split_data  # noqa: E402


def load_names(path: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    if not path.exists():
        return names
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                cid = int(row["champion_id"])
            except (KeyError, ValueError):
                continue
            names[cid] = row.get("champion_alias") or row.get("champion_name_en") or str(cid)
    return names


def fit_champion_lr(x_train, y_train, x_val, y_val):
    best_c, best_ll = C_GRID[0], float("inf")
    for c in C_GRID:
        m = LogisticRegression(C=c, max_iter=2000, solver="lbfgs")
        m.fit(x_train, y_train)
        ll = log_loss(y_val, m.predict_proba(x_val)[:, 1])
        if ll < best_ll:
            best_c, best_ll = c, ll
    m = LogisticRegression(C=best_c, max_iter=2000, solver="lbfgs")
    m.fit(x_train, y_train)
    return m


def pair_synergy(dataset, residual):
    """sum/count of signed residual for each within-team pair.

    blue-side pair contributes +residual, red-side pair contributes -residual,
    so 'positive synergy' always means 'the pair's own team over-performed the
    additive prediction'.
    """
    ssum = defaultdict(float)
    cnt = defaultdict(int)
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
        r = residual[i]
        for a, b in combinations(sorted(blue), 2):
            ssum[(a, b)] += r
            cnt[(a, b)] += 1
        for a, b in combinations(sorted(red), 2):
            ssum[(a, b)] -= r
            cnt[(a, b)] += 1
    return ssum, cnt


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--names-csv", default=Path("data/cache/champion_semantic_scores.csv"), type=click.Path(path_type=Path), show_default=True)
@click.option("--min-games", default=150, show_default=True, help="min co-occurrence in BOTH splits to qualify")
@click.option("--out", default=Path("outputs/ablation_pair_synergy_persistence.json"), type=click.Path(path_type=Path), show_default=True)
def main(data, patch_prefix, names_csv, min_games, out):
    names = load_names(names_csv)
    print("[1/4] loading split ...", flush=True)
    splits = load_split_data(data, patch_prefix)
    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}

    def label(idx_pair):
        a, b = idx_pair
        ca, cb = idx_to_cid[a], idx_to_cid[b]
        return f"{names.get(ca, ca)} + {names.get(cb, cb)}"

    y_train = np.asarray(splits.train.labels, dtype=np.float64)
    y_test = np.asarray(splits.test.labels, dtype=np.float64)
    y_val = np.asarray(splits.val.labels, dtype=np.float64)

    print("[2/4] fitting additive Champion LR ...", flush=True)
    xtr = champion_matrix(splits.train, n_champs)
    xva = champion_matrix(splits.val, n_champs)
    xte = champion_matrix(splits.test, n_champs)
    model = fit_champion_lr(xtr, y_train, xva, y_val)
    p_train = model.predict_proba(xtr)[:, 1]
    p_test = model.predict_proba(xte)[:, 1]
    res_train = y_train - p_train
    res_test = y_test - p_test

    print("[3/4] accumulating pair residuals ...", flush=True)
    tr_sum, tr_cnt = pair_synergy(splits.train, res_train)
    te_sum, te_cnt = pair_synergy(splits.test, res_test)

    print("[4/4] persistence analysis ...", flush=True)
    rows = []
    for pair, c_tr in tr_cnt.items():
        c_te = te_cnt.get(pair, 0)
        if c_tr < min_games or c_te < min_games:
            continue
        rows.append({
            "pair": pair,
            "train_syn_pp": tr_sum[pair] / c_tr * 100.0,
            "train_games": c_tr,
            "test_syn_pp": te_sum[pair] / c_te * 100.0,
            "test_games": c_te,
        })

    tr = np.array([r["train_syn_pp"] for r in rows])
    te = np.array([r["test_syn_pp"] for r in rows])
    corr = float(np.corrcoef(tr, te)[0, 1]) if len(rows) > 2 else float("nan")

    rows_sorted = sorted(rows, key=lambda r: r["train_syn_pp"], reverse=True)
    top = rows_sorted[:20]
    bottom = rows_sorted[-20:]
    top_train_mean = float(np.mean([r["train_syn_pp"] for r in top]))
    top_test_mean = float(np.mean([r["test_syn_pp"] for r in top]))
    bot_train_mean = float(np.mean([r["train_syn_pp"] for r in bottom]))
    bot_test_mean = float(np.mean([r["test_syn_pp"] for r in bottom]))
    noise_se_train = 0.5 / np.sqrt(min_games) * 100.0

    result = {
        "data": str(data),
        "patch_prefix": patch_prefix,
        "split": [len(splits.train), len(splits.val), len(splits.test)],
        "min_games": min_games,
        "qualified_pairs": len(rows),
        "avg_pair_cooccurrence_train": float(np.mean(list(tr_cnt.values()))),
        "train_test_synergy_correlation": round(corr, 4),
        "noise_floor_se_pp_at_min_games": round(float(noise_se_train), 2),
        "top20_train_synergy_pp_mean": round(top_train_mean, 2),
        "top20_same_pairs_test_pp_mean": round(top_test_mean, 2),
        "bottom20_train_synergy_pp_mean": round(bot_train_mean, 2),
        "bottom20_same_pairs_test_pp_mean": round(bot_test_mean, 2),
        "top15_pairs": [
            {
                "pair": label(r["pair"]),
                "train_syn_pp": round(r["train_syn_pp"], 2),
                "train_games": r["train_games"],
                "test_syn_pp": round(r["test_syn_pp"], 2),
                "test_games": r["test_games"],
            }
            for r in rows_sorted[:15]
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nqualified pairs (>= {min_games} games in both): {len(rows)}")
    print(f"avg pair co-occurrence in train: {result['avg_pair_cooccurrence_train']:.0f}")
    print(f"noise floor SE at min_games: +-{noise_se_train:.2f} pp")
    print(f"corr(train synergy, test synergy): {corr:+.4f}")
    print(f"\ntop-20 by TRAIN synergy:  train mean {top_train_mean:+.2f} pp  ->  test mean {top_test_mean:+.2f} pp")
    print(f"bot-20 by TRAIN synergy:  train mean {bot_train_mean:+.2f} pp  ->  test mean {bot_test_mean:+.2f} pp")
    print(f"\n{'pair':<34}{'train':>9}{'n_tr':>8}{'test':>9}{'n_te':>8}")
    for r in rows_sorted[:15]:
        print(f"{label(r['pair'])[:32]:<34}{r['train_syn_pp']:>+8.2f}%{r['train_games']:>8}{r['test_syn_pp']:>+8.2f}%{r['test_games']:>8}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
