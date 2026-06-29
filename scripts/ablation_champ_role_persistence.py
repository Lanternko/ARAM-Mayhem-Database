"""Ablation: champion x teammate-ROLE interaction — does pooling beat raw pairs?

Same residual-synergy methodology as the champion x champion pair test, but the
partner is replaced by the partner's primary role (Tank / Assassin / Mage / ...).
Pooling ~20-40 champions into one role bucket should raise sample density by an
order of magnitude, so if the champion-conditional role-fit signal is real it
should persist from train to test far better than raw pairwise synergy did.

Also drills into specific champion x role cells by teammate-count bucket to check
hand-picked hypotheses (e.g. Caitlyn likes tanks, Naafiri dislikes extra assassins).
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix, C_GRID  # noqa: E402
from train_ability_nn import load_split_data  # noqa: E402

ROLES = ["Tank", "Fighter", "Assassin", "Mage", "Marksman", "Support"]


def load_roles_and_names(path: Path):
    roles: dict[int, str] = {}
    names: dict[int, str] = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                cid = int(row["champion_id"])
            except (KeyError, ValueError):
                continue
            names[cid] = row.get("champion_alias") or str(cid)
            tag = (row.get("tags") or "").replace(",", "|").split("|")[0].strip()
            roles[cid] = tag or "Mage"
    return roles, names


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


def accumulate(dataset, residual, role_of):
    """signed residual per (champion, teammate-role) occurrence."""
    ssum = defaultdict(float)
    cnt = defaultdict(int)
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
        r = residual[i]
        for team, sign in ((blue, 1.0), (red, -1.0)):
            sr = r * sign
            for c in team:
                for t in team:
                    if t == c:
                        continue
                    key = (c, role_of.get(t, "Mage"))
                    ssum[key] += sr
                    cnt[key] += 1
    return ssum, cnt


def count_bucket(dataset, residual, role_of, champ_idx, role):
    """residual by number of `role` teammates, for one champion."""
    by_k = defaultdict(lambda: [0.0, 0])
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
        for team, sign in ((blue, 1.0), (red, -1.0)):
            if champ_idx not in team:
                continue
            k = sum(1 for t in team if t != champ_idx and role_of.get(t) == role)
            by_k[k][0] += residual[i] * sign
            by_k[k][1] += 1
    return {k: {"resid_pp": round(v[0] / v[1] * 100, 2), "n": v[1]} for k, v in sorted(by_k.items())}


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--scores-csv", default=Path("data/cache/champion_semantic_scores.csv"), type=click.Path(path_type=Path), show_default=True)
@click.option("--min-games", default=300, show_default=True)
@click.option("--out", default=Path("outputs/ablation_champ_role_persistence.json"), type=click.Path(path_type=Path), show_default=True)
def main(data, patch_prefix, scores_csv, min_games, out):
    roles_by_cid, names = load_roles_and_names(scores_csv)
    print("[1/4] loading split ...", flush=True)
    splits = load_split_data(data, patch_prefix)
    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    role_of = {idx: roles_by_cid.get(cid, "Mage") for idx, cid in idx_to_cid.items()}
    cid_to_idx_name = {names.get(cid, str(cid)).lower(): idx for idx, cid in idx_to_cid.items()}

    y_train = np.asarray(splits.train.labels, dtype=np.float64)
    y_val = np.asarray(splits.val.labels, dtype=np.float64)
    y_test = np.asarray(splits.test.labels, dtype=np.float64)

    print("[2/4] fitting additive Champion LR ...", flush=True)
    model = fit_champion_lr(champion_matrix(splits.train, n_champs), y_train,
                            champion_matrix(splits.val, n_champs), y_val)
    res_train = y_train - model.predict_proba(champion_matrix(splits.train, n_champs))[:, 1]
    res_test = y_test - model.predict_proba(champion_matrix(splits.test, n_champs))[:, 1]

    print("[3/4] accumulating champion x role residuals ...", flush=True)
    tr_sum, tr_cnt = accumulate(splits.train, res_train, role_of)
    te_sum, te_cnt = accumulate(splits.test, res_test, role_of)

    print("[4/4] persistence + named cells ...", flush=True)
    rows = []
    for key, c_tr in tr_cnt.items():
        c_te = te_cnt.get(key, 0)
        if c_tr < min_games or c_te < min_games:
            continue
        rows.append({
            "champ": names.get(idx_to_cid[key[0]], idx_to_cid[key[0]]),
            "role": key[1],
            "train_pp": tr_sum[key] / c_tr * 100,
            "train_games": c_tr,
            "test_pp": te_sum[key] / c_te * 100,
            "test_games": c_te,
        })

    tr = np.array([r["train_pp"] for r in rows])
    te = np.array([r["test_pp"] for r in rows])
    corr = float(np.corrcoef(tr, te)[0, 1]) if len(rows) > 2 else float("nan")
    rows_sorted = sorted(rows, key=lambda r: r["train_pp"], reverse=True)
    top = rows_sorted[:20]
    bot = rows_sorted[-20:]
    summary = {
        "min_games": min_games,
        "qualified_cells": len(rows),
        "avg_cell_cooccurrence_train": float(np.mean(list(tr_cnt.values()))),
        "train_test_correlation": round(corr, 4),
        "pairwise_baseline_correlation": 0.1693,
        "top20_train_pp_mean": round(float(np.mean([r["train_pp"] for r in top])), 2),
        "top20_test_pp_mean": round(float(np.mean([r["test_pp"] for r in top])), 2),
        "bottom20_train_pp_mean": round(float(np.mean([r["train_pp"] for r in bot])), 2),
        "bottom20_test_pp_mean": round(float(np.mean([r["test_pp"] for r in bot])), 2),
    }

    named = {}
    for who, role in [("caitlyn", "Tank"), ("caitlyn", "Assassin"), ("naafiri", "Assassin"), ("jhin", "Tank")]:
        idx = cid_to_idx_name.get(who)
        if idx is None:
            named[f"{who} x {role}"] = "champion not in vocab"
            continue
        named[f"{who} x {role}"] = {
            "train_by_teammate_count": count_bucket(splits.train, res_train, role_of, idx, role),
            "test_by_teammate_count": count_bucket(splits.test, res_test, role_of, idx, role),
        }

    result = {**summary, "top15_cells": rows_sorted[:15], "named_cells": named}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=lambda o: round(o, 2) if isinstance(o, float) else o), encoding="utf-8")

    print(f"\nqualified champion x role cells (>= {min_games} both): {len(rows)}")
    print(f"avg cell co-occurrence (train): {summary['avg_cell_cooccurrence_train']:.0f}")
    print(f"corr(train, test):  champion x ROLE = {corr:+.4f}   vs   champion x champion = +0.1693")
    print(f"top-20 train {summary['top20_train_pp_mean']:+.2f}pp -> test {summary['top20_test_pp_mean']:+.2f}pp"
          f"   |   bot-20 train {summary['bottom20_train_pp_mean']:+.2f}pp -> test {summary['bottom20_test_pp_mean']:+.2f}pp")
    print(f"\n{'champ x role':<28}{'train':>9}{'n_tr':>8}{'test':>9}{'n_te':>8}")
    for r in rows_sorted[:15]:
        print(f"{(r['champ']+' x '+r['role'])[:26]:<28}{r['train_pp']:>+8.2f}%{r['train_games']:>8}{r['test_pp']:>+8.2f}%{r['test_games']:>8}")
    print("\nnamed hypotheses (residual pp by # role teammates, train | test):")
    for k, v in named.items():
        print(f"  {k}:")
        if isinstance(v, str):
            print(f"    {v}")
            continue
        for split_name in ("train_by_teammate_count", "test_by_teammate_count"):
            cells = v[split_name]
            s = "  ".join(f"{kk}:{cc['resid_pp']:+.1f}%(n={cc['n']})" for kk, cc in cells.items())
            print(f"    {split_name.split('_')[0]:5}: {s}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
