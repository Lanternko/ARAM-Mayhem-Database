"""Recommender backtest on the held-out test split + paired model CI.

Part A (Q1): bootstrap CIs on Champion-LR vs Composition-LR test accuracy and,
crucially, on their PAIRED difference (same test rows), since CI overlap is the
wrong way to judge a same-data model gap.

Part B (#2): for each sampled test team, hide one champion, rank every eligible
candidate for the empty slot with the full Composition-LR recommender, and record
where the actually-played champion lands. If the ranking is valid out of sample,
teams whose real pick the model rated highly should win more. A strength-only
recommender (rank candidates by champion coefficient, context-free) is the control:
the gap between the two decile spreads is what team composition adds to the advice.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix, team_profile, build_champion_profiles, C_GRID  # noqa: E402
from train_ability_nn import load_split_data, build_vocab, TeamDataset  # noqa: E402
from train_composition_lr import build_feature_blocks, select_blocks, fit_with_val_c  # noqa: E402
from train_semantic_tree import train_frame_for_empirical_scores  # noqa: E402
from build_role_synergy import load_role_by_champ  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.recommend import _combine_synergy  # noqa: E402
from aram_nn.role_synergy import RoleSynergyStats, build_role_rows_from_team_rows  # noqa: E402

RNG = np.random.default_rng(42)


def acc(y, p):
    return float(((p > 0.5) == (y > 0.5)).mean())


def decile_winrate(rec_scores, labels, n_bins=10):
    order = np.argsort(rec_scores)
    labels = np.asarray(labels)[order]
    out = []
    for b in range(n_bins):
        lo = b * len(labels) // n_bins
        hi = (b + 1) * len(labels) // n_bins
        seg = labels[lo:hi]
        out.append({"bin": b + 1, "n": int(len(seg)), "winrate_pct": round(float(seg.mean()) * 100, 2)})
    return out


def load_walkforward_split(
    data: Path,
    score_csv: Path,
    train_patches: list[str],
    test_patch: str,
    *,
    min_duration: int = 300,
    val_frac: float = 0.15,
    min_games: int = 20,
    replace_sustain: bool = True,
):
    """Patch-partitioned split: train on `train_patches`, rank-test on `test_patch`.

    Mirrors load_split_data's vocab + known-filter contract but partitions by
    patch instead of a within-patch time fraction, so the rank-backtest measures
    the real deployment condition (a model trained on PAST patches scoring the
    NEXT one). Vocab, profiles, and synergy all come from train patches only; the
    test patch is fully disjoint, so there is no train/test leakage by construction.
    Returns (profiles, splits, patches_present) where splits exposes the same
    .train/.val/.test/.champ_to_idx attributes the backtest reads off SplitData.
    """
    import polars as pl

    df = pl.read_parquet(data)
    df = df.filter(pl.col("duration_sec") >= min_duration)
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
    )
    present = sorted(df["patch_prefix"].unique().to_list())
    df_train_all = df.filter(pl.col("patch_prefix").is_in(train_patches)).sort("game_creation_ms")
    df_test = df.filter(pl.col("patch_prefix") == test_patch).sort("game_creation_ms")
    if df_train_all.height == 0 or df_test.height == 0:
        raise click.ClickException(
            f"walk-forward needs both folds non-empty: train={df_train_all.height} "
            f"test={df_test.height}; patches present={present}"
        )

    # Carve a same-patch time-tail val out of train for C-grid selection.
    n = df_train_all.height
    n_val = max(1, int(n * val_frac))
    df_train = df_train_all.slice(0, n - n_val)
    df_val = df_train_all.slice(n - n_val, n_val)

    champ_to_idx = build_vocab(df_train)
    known = list(champ_to_idx)

    def filter_known(d):
        mask = (
            d["blue_champions"].list.eval(pl.element().is_in(known)).list.all()
            & d["red_champions"].list.eval(pl.element().is_in(known)).list.all()
        )
        return d.filter(mask)

    df_val = filter_known(df_val)
    df_test = filter_known(df_test)

    profiles = build_champion_profiles(
        score_csv=score_csv, train_df=df_train, min_games=min_games, replace_sustain=replace_sustain
    )
    splits = SimpleNamespace(
        train=TeamDataset(df_train, champ_to_idx),
        val=TeamDataset(df_val, champ_to_idx),
        test=TeamDataset(df_test, champ_to_idx),
        champ_to_idx=champ_to_idx,
    )
    return profiles, splits, present


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"), type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"), type=click.Path(path_type=Path), show_default=True)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--feature-set", default="all_composition", show_default=True)
@click.option("--sample-teams", default=6000, show_default=True)
@click.option("--n-boot", default=2000, show_default=True)
@click.option("--kept-extreme", default=0.0, show_default=True, help="keep only teams whose kept-4 |ad_share-0.5| >= this (0 = no filter)")
@click.option("--lambdas", default="0,0.5,1.0,1.5,2.0,3.0,5.0", show_default=True,
              help="role-synergy blend weights to sweep: score = p_comp + lambda*synergy")
@click.option("--syn-min-cell", default=80, show_default=True,
              help="min present-bucket games for a role-synergy cell in the TRAIN fold")
@click.option("--syn-shrink-k", default=150.0, show_default=True)
@click.option("--syn-persistence", default=0.5, show_default=True)
@click.option("--walk-forward/--in-split", default=False, show_default=True,
              help="cross-patch mode: train on --train-patches, rank-test on --test-patch (needs a multi-patch --data)")
@click.option("--train-patches", default="16.10,16.11", show_default=True,
              help="comma-separated patch prefixes to train on (walk-forward mode)")
@click.option("--test-patch", default="16.12", show_default=True,
              help="held-out future patch prefix to rank-test on (walk-forward mode)")
@click.option("--out", default=Path("outputs/ablation_recommender_backtest.json"), type=click.Path(path_type=Path), show_default=True)
def main(data, score_csv, patch_prefix, feature_set, sample_teams, n_boot, kept_extreme,
         lambdas, syn_min_cell, syn_shrink_k, syn_persistence,
         walk_forward, train_patches, test_patch, out):
    print("[1/6] profiles + split ...", flush=True)
    train_patch_list = None
    if walk_forward:
        train_patch_list = [p.strip() for p in str(train_patches).split(",") if p.strip()]
        profiles, splits, patches_present = load_walkforward_split(
            data, score_csv, train_patch_list, str(test_patch).strip(),
        )
        print(
            f"    WALK-FORWARD  train={train_patch_list} -> test={test_patch}  "
            f"present={patches_present}  rows train/val/test="
            f"{len(splits.train)}/{len(splits.val)}/{len(splits.test)}",
            flush=True,
        )
    else:
        profiles = build_champion_profiles(
            score_csv=score_csv,
            train_df=train_frame_for_empirical_scores(data, patch_prefix),
            min_games=20, replace_sustain=True,
        )
        splits = load_split_data(data, patch_prefix)
    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    has_profile = {idx for idx, cid in idx_to_cid.items() if cid in profiles}

    y_tr = np.asarray(splits.train.labels, np.float64)
    y_va = np.asarray(splits.val.labels, np.float64)
    y_te = np.asarray(splits.test.labels, np.float64)

    print("[*] building role-synergy from TRAIN fold only (leak-free) ...", flush=True)
    role_by_champ = load_role_by_champ(score_csv)
    train_team_rows: list[tuple[list[int], int]] = []
    for blue_idx, red_idx, won in zip(splits.train.blue, splits.train.red, y_tr):
        w = int(won)
        train_team_rows.append(([idx_to_cid[i] for i in blue_idx], w))
        train_team_rows.append(([idx_to_cid[i] for i in red_idx], 1 - w))
    syn_rows = build_role_rows_from_team_rows(
        train_team_rows, role_by_champ=role_by_champ,
        min_cell=syn_min_cell, shrink_k=syn_shrink_k, persistence_factor=syn_persistence,
    )
    syn = RoleSynergyStats(
        rows=syn_rows, role_by_champ=role_by_champ,
        min_pair=syn_min_cell, shrink_k=syn_shrink_k, persistence_factor=syn_persistence,
    )
    lam_list = [float(x) for x in str(lambdas).split(",") if x.strip() != ""]
    print(f"    role-synergy cells={len(syn_rows)}  champs_with_role={len(role_by_champ)}  lambdas={lam_list}")

    def comp_matrix(dataset):
        bp = [team_profile(t, idx_to_cid, profiles) for t in dataset.blue]
        rp = [team_profile(t, idx_to_cid, profiles) for t in dataset.red]
        blocks, _ = select_blocks(build_feature_blocks(bp, rp), feature_set)
        return np.concatenate([champion_matrix(dataset, n_champs), *blocks], axis=1)

    print("[2/6] building feature matrices ...", flush=True)
    x_tr, x_va, x_te = comp_matrix(splits.train), comp_matrix(splits.val), comp_matrix(splits.test)
    champ_tr = champion_matrix(splits.train, n_champs)
    champ_va = champion_matrix(splits.val, n_champs)
    champ_te = champion_matrix(splits.test, n_champs)

    print("[3/6] fitting Champion LR + Composition LR ...", flush=True)
    champ_model, _ = fit_with_val_c(champ_tr, y_tr, champ_va, y_va)
    comp_model, _ = fit_with_val_c(x_tr, y_tr, x_va, y_va)
    p_champ = champ_model.predict_proba(champ_te)[:, 1]
    p_comp = comp_model.predict_proba(x_te)[:, 1]
    champ_coef = champ_model.coef_[0]

    print("[4/6] paired bootstrap CIs (Q1) ...", flush=True)
    n = len(y_te)
    correct_champ = (p_champ > 0.5) == (y_te > 0.5)
    correct_comp = (p_comp > 0.5) == (y_te > 0.5)
    boot_champ, boot_comp, boot_diff = [], [], []
    for _ in range(n_boot):
        idx = RNG.integers(0, n, n)
        bc, bp = correct_champ[idx].mean(), correct_comp[idx].mean()
        boot_champ.append(bc); boot_comp.append(bp); boot_diff.append(bp - bc)
    def ci(a):
        return [round(float(np.percentile(a, 2.5)) * 100, 3), round(float(np.percentile(a, 97.5)) * 100, 3)]
    q1 = {
        "champion_lr_acc_pct": round(acc(y_te, p_champ) * 100, 3),
        "champion_lr_acc_CI95": ci(boot_champ),
        "composition_lr_acc_pct": round(acc(y_te, p_comp) * 100, 3),
        "composition_lr_acc_CI95": ci(boot_comp),
        "paired_diff_pp": round(float(np.mean(boot_diff)) * 100, 3),
        "paired_diff_CI95_pp": [round(float(np.percentile(boot_diff, 2.5)) * 100, 3), round(float(np.percentile(boot_diff, 97.5)) * 100, 3)],
        "paired_diff_p_gt_0": round(float(np.mean(np.array(boot_diff) > 0)), 4),
        "champ_lr_ece_note": "see report: Composition LR ECE 0.0026 vs DeepSets+scores 0.0192",
    }

    print(f"[5/6] recommender backtest on up to {sample_teams} test teams (kept_extreme={kept_extreme}) ...", flush=True)
    n_matches = len(splits.test.blue)
    order = RNG.permutation(n_matches)
    cand_pool = np.array(sorted(has_profile))
    full_scores, strength_scores, labels = [], [], []
    syn_scores: dict[float, list[float]] = {lam: [] for lam in lam_list}
    nonzero_syn = 0
    total_syn_slots = 0
    skipped = 0
    filtered = 0
    for mi in order:
        if len(labels) >= sample_teams:
            break
        blue = list(splits.test.blue[mi]); red = list(splits.test.red[mi]); y = y_te[mi]
        if any(c not in has_profile for c in blue + red):
            skipped += 1; continue
        hide_pos = int(RNG.integers(0, len(blue)))
        c_star = blue[hide_pos]
        kept = [c for j, c in enumerate(blue) if j != hide_pos]
        if kept_extreme > 0.0:
            kept_ad = team_profile(kept, idx_to_cid, profiles).ad_share
            if abs(kept_ad - 0.5) < kept_extreme:
                filtered += 1; continue
        taken = set(kept) | set(red)
        cands = [c for c in cand_pool if c not in taken or c == c_star]
        if c_star not in cands:
            cands.append(c_star)
        red_tp = team_profile(red, idx_to_cid, profiles)
        blue_tps = [team_profile(kept + [c], idx_to_cid, profiles) for c in cands]
        blocks, _ = select_blocks(build_feature_blocks(blue_tps, [red_tp] * len(cands)), feature_set)
        champ_oh = np.zeros((len(cands), n_champs), np.float32)
        for c in red:
            champ_oh[:, c] = -1.0
        for c in kept:
            champ_oh[:, c] = 1.0
        for r, c in enumerate(cands):
            champ_oh[r, c] = 1.0
        X = np.concatenate([champ_oh, *blocks], axis=1)
        p = comp_model.predict_proba(X)[:, 1]
        # clean decomposition: same comp_model, champion-only sub-logit vs full prediction
        s_strength = champ_oh @ comp_model.coef_[0][:n_champs]
        ci_star = cands.index(c_star)
        full_scores.append(float((p[ci_star] >= p).mean()))                      # 1.0 = c* IS the top recommendation
        strength_scores.append(float((s_strength[ci_star] >= s_strength).mean()))
        # role-synergy of each candidate with the kept-4 anchors, via the exact
        # shipped combiner (recommend._combine_synergy), so the blend the backtest
        # scores is the one suggest_for_cell would apply live.
        kept_cids = [idx_to_cid[c] for c in kept]
        syn_vec = np.array(
            [_combine_synergy(kept_cids, idx_to_cid[c], syn)[0] for c in cands],
            dtype=np.float64,
        )
        nonzero_syn += int(np.count_nonzero(syn_vec))
        total_syn_slots += int(syn_vec.size)
        for lam in lam_list:
            blended = p + lam * syn_vec
            syn_scores[lam].append(float((blended[ci_star] >= blended).mean()))
        labels.append(float(y))

    full_deciles = decile_winrate(np.array(full_scores), labels)
    strength_deciles = decile_winrate(np.array(strength_scores), labels)

    def spread(dec):
        return round(dec[-1]["winrate_pct"] - dec[0]["winrate_pct"], 2)

    base_spread = spread(full_deciles)
    synergy_sweep = []
    for lam in lam_list:
        dec = decile_winrate(np.array(syn_scores[lam]), labels)
        sp = spread(dec)
        synergy_sweep.append({
            "lambda": lam,
            "top_minus_bottom_pp": sp,
            "added_vs_composition_pp": round(sp - base_spread, 2),
        })
    best_syn = max(synergy_sweep, key=lambda r: r["top_minus_bottom_pp"]) if synergy_sweep else None
    syn_coverage_pct = round(100.0 * nonzero_syn / max(total_syn_slots, 1), 2)

    result = {
        "mode": ("walk_forward" if walk_forward else "in_split"),
        "train_patches": train_patch_list,
        "test_patch": (str(test_patch).strip() if walk_forward else patch_prefix),
        "split": [len(splits.train), len(splits.val), len(splits.test)],
        "Q1_model_CIs": q1,
        "backtest": {
            "teams_used": len(labels), "skipped_missing_profile": skipped,
            "kept_extreme_threshold": kept_extreme, "filtered_not_extreme": filtered,
            "candidate_pool_size": int(len(cand_pool)),
            "full_recommender_decile_winrate": full_deciles,
            "strength_only_decile_winrate": strength_deciles,
            "full_top_minus_bottom_pp": spread(full_deciles),
            "strength_only_top_minus_bottom_pp": spread(strength_deciles),
            "composition_added_spread_pp": round(spread(full_deciles) - spread(strength_deciles), 2),
            "synergy_train_cells": len(syn_rows),
            "synergy_coverage_pct": syn_coverage_pct,
            "synergy_sweep": synergy_sweep,
            "synergy_best_lambda": (best_syn["lambda"] if best_syn else None),
            "synergy_best_added_vs_composition_pp": (best_syn["added_vs_composition_pp"] if best_syn else None),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n===== Q1: model accuracy CIs (test) =====")
    print(f"Champion LR    {q1['champion_lr_acc_pct']:.2f}%  CI95 {q1['champion_lr_acc_CI95']}")
    print(f"Composition LR {q1['composition_lr_acc_pct']:.2f}%  CI95 {q1['composition_lr_acc_CI95']}")
    print(f"paired diff    {q1['paired_diff_pp']:+.3f}pp  CI95 {q1['paired_diff_CI95_pp']}  P(diff>0)={q1['paired_diff_p_gt_0']}")
    print("\n===== #2: recommender backtest =====")
    print(f"teams used {len(labels)} (skipped {skipped})")
    print(f"{'decile':>7}{'full WR%':>11}{'strength WR%':>15}")
    for fd, sd in zip(full_deciles, strength_deciles):
        print(f"{fd['bin']:>7}{fd['winrate_pct']:>11}{sd['winrate_pct']:>15}")
    print(f"\nfull recommender top-vs-bottom spread:     {spread(full_deciles):+.2f} pp")
    print(f"strength-only      top-vs-bottom spread:     {spread(strength_deciles):+.2f} pp")
    print(f"composition's added recommender value:       {result['backtest']['composition_added_spread_pp']:+.2f} pp")

    print("\n===== #3: role-synergy blend sweep (score = p_comp + lambda*synergy) =====")
    print(f"synergy train cells {len(syn_rows)}  |  coverage {syn_coverage_pct}% of candidate slots got a nonzero adj")
    print(f"{'lambda':>8}{'spread pp':>12}{'vs comp pp':>13}")
    for r in synergy_sweep:
        print(f"{r['lambda']:>8.2f}{r['top_minus_bottom_pp']:>12.2f}{r['added_vs_composition_pp']:>+13.2f}")
    if best_syn:
        print(f"\nbest lambda={best_syn['lambda']:.2f}  spread={best_syn['top_minus_bottom_pp']:+.2f}pp  "
              f"(composition base {base_spread:+.2f}pp, added {best_syn['added_vs_composition_pp']:+.2f}pp)")

    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
