"""Ablation B: is additive composition redundant with champion identity?

Claim under test: any team feature that is a SUM of per-champion attributes
(role counts, frontline sum, score sums) is already in the span of the champion
one-hot features, so adding it to Champion LR should give ~+0. Only NON-additive
features (ratios like AD share, threshold 'lacks', interactions) carry new signal.

Splits the composition features into additive vs non-additive and fits:
  champion-only / +additive-only / +nonadditive-only / +all
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import numpy as np
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix, team_profile, build_champion_profiles, SCORE_COLUMNS, ROLE_COLUMNS  # noqa: E402
from train_ability_nn import load_split_data  # noqa: E402
from train_composition_lr import build_feature_blocks, fit_with_val_c, metrics  # noqa: E402
from train_semantic_tree import train_frame_for_empirical_scores  # noqa: E402


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"), type=click.Path(path_type=Path), show_default=True)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", default=Path("outputs/ablation_additive_vs_nonadditive.json"), type=click.Path(path_type=Path), show_default=True)
def main(data, score_csv, patch_prefix, out):
    print("[1/4] profiles + split ...", flush=True)
    profiles = build_champion_profiles(
        score_csv=score_csv,
        train_df=train_frame_for_empirical_scores(data, patch_prefix),
        min_games=20, replace_sustain=True,
    )
    splits = load_split_data(data, patch_prefix)
    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    y_tr = np.asarray(splits.train.labels, np.float64)
    y_va = np.asarray(splits.val.labels, np.float64)
    y_te = np.asarray(splits.test.labels, np.float64)

    # additive = pure sums/counts of per-champion attributes; nonadditive = ratios/thresholds/interactions
    additive_names = ["front_count", "front_sum",
                      *[f"sum_{n}" for n in SCORE_COLUMNS],
                      *[f"role_{r.lower()}" for r in ROLE_COLUMNS]]
    nonadditive_linear = ["ad_share", "ad_ap_balance", "true_share", "core_lacks_count", "all_lacks_count",
                          *[f"lack_{n}" for n in SCORE_COLUMNS]]
    interaction_keys = ["ad_front", "wave_engage", "poke_front", "role_ad"]

    def matrices(dataset):
        bp = [team_profile(t, idx_to_cid, profiles) for t in dataset.blue]
        rp = [team_profile(t, idx_to_cid, profiles) for t in dataset.red]
        blocks = build_feature_blocks(bp, rp)
        lin, lin_names = blocks["linear"]
        name_to_col = {n: i for i, n in enumerate(lin_names)}
        add = lin[:, [name_to_col[n] for n in additive_names]]
        nonadd_lin = lin[:, [name_to_col[n] for n in nonadditive_linear]]
        inter = np.concatenate([blocks[k][0] for k in interaction_keys], axis=1)
        nonadd = np.concatenate([nonadd_lin, inter], axis=1)
        champ = champion_matrix(dataset, n_champs)
        return champ, add, nonadd

    print("[2/4] building feature matrices ...", flush=True)
    c_tr, a_tr, na_tr = matrices(splits.train)
    c_va, a_va, na_va = matrices(splits.val)
    c_te, a_te, na_te = matrices(splits.test)
    print(f"      additive dims={a_tr.shape[1]}  nonadditive dims={na_tr.shape[1]}", flush=True)

    configs = {
        "champion_only": (c_tr, c_va, c_te),
        "champion_plus_additive": (np.concatenate([c_tr, a_tr], 1), np.concatenate([c_va, a_va], 1), np.concatenate([c_te, a_te], 1)),
        "champion_plus_nonadditive": (np.concatenate([c_tr, na_tr], 1), np.concatenate([c_va, na_va], 1), np.concatenate([c_te, na_te], 1)),
        "champion_plus_all": (np.concatenate([c_tr, a_tr, na_tr], 1), np.concatenate([c_va, a_va, na_va], 1), np.concatenate([c_te, a_te, na_te], 1)),
    }

    print("[3/4] fitting 4 models ...", flush=True)
    results = {}
    base_acc = None
    for name, (xtr, xva, xte) in configs.items():
        model, _ = fit_with_val_c(xtr, y_tr, xva, y_va)
        p = model.predict_proba(xte)[:, 1]
        m = metrics(y_te, p)
        if base_acc is None:
            base_acc = m["acc"]
        m["delta_acc_pp_vs_champion"] = round((m["acc"] - base_acc) * 100, 3)
        m["n_features"] = int(xtr.shape[1])
        results[name] = m
        print(f"      {name:<28} acc={m['acc']*100:.3f}%  d={m['delta_acc_pp_vs_champion']:+.3f}pp  ll={m['log_loss']:.4f}", flush=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n[4/4] summary")
    print(f"{'config':<28}{'acc%':>9}{'d vs champ':>12}{'log_loss':>11}{'feats':>8}")
    for name, m in results.items():
        print(f"{name:<28}{m['acc']*100:>8.3f}%{m['delta_acc_pp_vs_champion']:>+11.3f}{m['log_loss']:>11.4f}{m['n_features']:>8}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
