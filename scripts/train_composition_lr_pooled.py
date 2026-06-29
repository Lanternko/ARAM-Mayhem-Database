"""Candidate: pooled cross-patch + recency-weighted composition LR.

Reuses the EXACT production feature pipeline (champion identity + empirical/
semantic/role composition blocks from train_composition_lr) but:
  * pools all patches in the parquet instead of filtering to one,
  * holds out the most-recent `--holdout` games of the current patch as test
    (early current-patch games stay in TRAIN, so recency weighting has current
    signal to use — unlike a global time split which buries all of it in test),
  * weights each train game by recency  w = exp(-(t_ref - t)/tau).

Verification: fits three models on the SAME held-out current-patch test and
prints metrics (overall + on games containing a "mover" champion):
  baseline_16_10    feature pipeline, trained on oldest-patch rows only (= the
                    current pinned model's regime)
  pooled_flat       pooled, uniform weights
  pooled_recency    pooled, exp recency weights @ tau   <- the candidate

Saves the candidate as a drop-in model.pkl (same schema as
train_composition_lr) so recommend_gui can load it unchanged.

  python scripts/train_composition_lr_pooled.py \
      --data data/raw/mayhem_pooled_16_10_12.parquet \
      --current-patch 16.12 --half-life-days 7 \
      --out models/composition_lr_pooled_recency_7d
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_ability_nn import TeamDataset, build_vocab  # noqa: E402
from train_semantic_tree import train_frame_for_empirical_scores  # noqa: E402
from analyze_composition_signals import build_champion_profiles, champion_matrix  # noqa: E402
from train_composition_lr import (  # noqa: E402
    C_GRID, build_feature_blocks, build_team_profiles, metrics, select_blocks,
    write_coefficients, write_feature_names,
)

DAY_MS = 86_400_000


def patch_prefix_col(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("pp")
    )


def filter_known(d: pl.DataFrame, known: set[int]) -> pl.DataFrame:
    mask = (
        d["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        & d["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
    )
    return d.filter(mask)


def raw_wr(df: pl.DataFrame, pp: str) -> dict[int, tuple[float, int]]:
    sub = df.filter(pl.col("pp") == pp)
    g = {}
    for blue, red, bw in zip(sub["blue_champions"].to_list(), sub["red_champions"].to_list(),
                             sub["blue_wins"].to_list()):
        for c in blue:
            a = g.setdefault(int(c), [0, 0]); a[0] += 1; a[1] += int(bw)
        for c in red:
            a = g.setdefault(int(c), [0, 0]); a[0] += 1; a[1] += int(not bw)
    return {c: (w / n, n) for c, (n, w) in g.items()}


def build_x(df: pl.DataFrame, champ_to_idx, idx_to_cid, profiles, feature_set, n_champs):
    ds = TeamDataset(df, champ_to_idx)
    champ = champion_matrix(ds, n_champs)
    blue, red = build_team_profiles(ds, idx_to_cid, profiles)
    blocks = build_feature_blocks(blue, red)
    comp_blocks, names = select_blocks(blocks, feature_set)
    x = np.concatenate([champ, *comp_blocks], axis=1)
    y = ds.labels.astype(np.float64)
    return x, y, names


def fit_c(x_tr, y_tr, x_va, y_va, w_tr=None):
    best_c, best_ll = C_GRID[0], float("inf")
    for c in C_GRID:
        m = LogisticRegression(C=c, max_iter=2000, solver="lbfgs")
        m.fit(x_tr, y_tr, sample_weight=w_tr)
        ll = log_loss(y_va, m.predict_proba(x_va)[:, 1])
        if ll < best_ll:
            best_ll, best_c = ll, c
    m = LogisticRegression(C=best_c, max_iter=2000, solver="lbfgs")
    m.fit(x_tr, y_tr, sample_weight=w_tr)
    return m, best_c


def ev(model, x, y, mask=None):
    p = model.predict_proba(x)[:, 1]
    if mask is not None:
        if mask.sum() == 0:
            return None
        return {**metrics(y[mask], p[mask]), "n": int(mask.sum())}
    return {**metrics(y, p), "n": int(len(y))}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"),
              type=click.Path(exists=True, path_type=Path))
@click.option("--current-patch", default="16.12", show_default=True)
@click.option("--prev-patch", default="16.11", show_default=True)
@click.option("--baseline-patch", default="16.10", show_default=True, help="single-patch baseline trains on this only")
@click.option("--holdout", default=12000, show_default=True, help="recent current-patch games held out as test")
@click.option("--val-size", default=15000, show_default=True, help="late pre-current games used for C-tuning")
@click.option("--half-life-days", default=7.0, show_default=True)
@click.option("--feature-set", default="all_composition", show_default=True)
@click.option("--empirical-min-games", default=20, show_default=True)
@click.option("--mover-min-drift", default=4.0, show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--seed", default=42, show_default=True)
def main(data, score_csv, current_patch, prev_patch, baseline_patch, holdout, val_size,
         half_life_days, feature_set, empirical_min_games, mover_min_drift, out, seed):
    np.random.seed(seed)
    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = patch_prefix_col(df).sort("game_creation_ms")

    cur = df.filter(pl.col("pp") == current_patch)
    non_cur = df.filter(pl.col("pp") != current_patch)
    if cur.height <= holdout + 100:
        raise click.ClickException(f"{current_patch} has {cur.height} games (<= holdout)")

    test_df = cur.tail(holdout)
    early_cur = cur.head(cur.height - holdout)
    val_df = non_cur.tail(val_size)
    train_df = pl.concat([non_cur.head(non_cur.height - val_size), early_cur]).sort("game_creation_ms")

    champ_to_idx = build_vocab(train_df)
    known = set(champ_to_idx)
    idx_to_cid = {i: c for c, i in champ_to_idx.items()}
    n_champs = len(champ_to_idx)
    val_df = filter_known(val_df, known)
    test_df = filter_known(test_df, known)

    click.echo(f"train={train_df.height} (pool {non_cur.height - val_size} + early-{current_patch} {early_cur.height})  "
               f"val={val_df.height}  test={test_df.height}  champs={n_champs}")

    # Empirical champion profiles from TRAIN ONLY (no leakage; includes early current patch).
    profiles = build_champion_profiles(score_csv=score_csv, train_df=train_df,
                                       min_games=empirical_min_games, replace_sustain=True)

    x_tr, y_tr, feat_names = build_x(train_df, champ_to_idx, idx_to_cid, profiles, feature_set, n_champs)
    x_va, y_va, _ = build_x(val_df, champ_to_idx, idx_to_cid, profiles, feature_set, n_champs)
    x_te, y_te, _ = build_x(test_df, champ_to_idx, idx_to_cid, profiles, feature_set, n_champs)
    feature_names = [f"champion:{idx_to_cid[i]}" for i in range(n_champs)] + feat_names

    # recency weights on train
    t = train_df["game_creation_ms"].to_numpy().astype(np.float64)
    t_ref = t.max()
    w = np.exp(-(t_ref - t) / (half_life_days * DAY_MS))
    w *= len(w) / w.sum()

    # movers (raw WR drift prev->current) for the diagnostic subset
    wr0, wr1 = raw_wr(df, prev_patch), raw_wr(df, current_patch)
    movers = {c for c in wr1 if c in wr0 and wr0[c][1] >= 300 and wr1[c][1] >= 300
              and abs((wr1[c][0] - wr0[c][0]) * 100) >= mover_min_drift}
    test_mask = np.array([any(int(c) in movers for c in (b + r))
                          for b, r in zip(test_df["blue_champions"].to_list(),
                                          test_df["red_champions"].to_list())])
    click.echo(f"movers(|dWR|>={mover_min_drift}pp)={len(movers)}  test mover-game coverage={test_mask.mean()*100:.0f}%\n")

    # baseline: train on baseline_patch rows only (single-patch regime), uniform
    base_mask = (train_df["pp"] == baseline_patch).to_numpy()
    m_base, c_base = fit_c(x_tr[base_mask], y_tr[base_mask], x_va, y_va)
    m_flat, c_flat = fit_c(x_tr, y_tr, x_va, y_va)
    m_rec, c_rec = fit_c(x_tr, y_tr, x_va, y_va, w_tr=w)

    rows = []
    for name, m, c in [(f"baseline_{baseline_patch}", m_base, c_base),
                       ("pooled_flat", m_flat, c_flat),
                       (f"pooled_recency_{half_life_days:g}d", m_rec, c_rec)]:
        all_m = ev(m, x_te, y_te)
        mv_m = ev(m, x_te, y_te, test_mask)
        rows.append((name, c, all_m, mv_m))

    click.echo(f"{'model':>24} | {'C':>5} | {'test ll/acc':>16} | {'mover-game ll/acc':>18}")
    click.echo("-" * 74)
    for name, c, a, mv in rows:
        mvs = f"{mv['log_loss']:.4f}/{mv['acc']*100:4.1f}%" if mv else "n/a"
        click.echo(f"{name:>24} | {c:>5g} | {a['log_loss']:.4f}/{a['acc']*100:4.1f}% | {mvs:>18}")

    base_ll = rows[0][2]["log_loss"]; rec_ll = rows[2][2]["log_loss"]
    click.echo(f"\ncandidate vs baseline: test logloss {base_ll:.4f} -> {rec_ll:.4f}  "
               f"({(base_ll-rec_ll)*1000:+.1f} millinats)  "
               f"acc {rows[0][2]['acc']*100:.1f}% -> {rows[2][2]['acc']*100:.1f}%")

    # Production artifact: refit on ALL rows (train+val+test) with recency weights,
    # reusing the val-tuned C.  The metrics above are the held-out verification; a
    # shipped model must not waste the most-recent ~27k games (which recency weights
    # value most).  Reuses the already-built feature matrices, so it's just one fit.
    t_va = val_df["game_creation_ms"].to_numpy().astype(np.float64)
    t_te = test_df["game_creation_ms"].to_numpy().astype(np.float64)
    x_all = np.vstack([x_tr, x_va, x_te])
    y_all = np.concatenate([y_tr, y_va, y_te])
    t_all = np.concatenate([t, t_va, t_te])
    t_ref_all = t_all.max()
    w_all = np.exp(-(t_ref_all - t_all) / (half_life_days * DAY_MS))
    w_all *= len(w_all) / w_all.sum()
    ship_model = LogisticRegression(C=c_rec, max_iter=2000, solver="lbfgs")
    ship_model.fit(x_all, y_all, sample_weight=w_all)
    click.echo(f"shipped model: refit on all {len(y_all)} rows (C={c_rec}, half-life {half_life_days:g}d)")

    # Save production model (drop-in for recommend_gui), same schema as train_composition_lr.
    out.mkdir(parents=True, exist_ok=True)
    profiles_payload = {
        int(cid): {"cid": int(p.cid), "scores": p.scores, "roles": p.roles,
                   "physical_dpm": p.physical_dpm, "magic_dpm": p.magic_dpm, "true_dpm": p.true_dpm}
        for cid, p in profiles.items()
    }
    summary = {
        "data": str(data), "current_patch": current_patch, "baseline_patch": baseline_patch,
        "half_life_days": half_life_days, "feature_set": feature_set,
        "train_rows": train_df.height, "val_rows": val_df.height, "test_rows": test_df.height,
        "n_champs": n_champs, "best_C": c_rec, "movers": len(movers),
        "results": {name: {"all": a, "mover_games": mv} for name, c, a, mv in rows},
        "candidate_minus_baseline_logloss": rec_ll - base_ll,
    }
    summary["shipped_model_rows"] = int(len(y_all))
    summary["shipped_model_note"] = "model.pkl refit on all rows; results{} are held-out verification"
    write_feature_names(out / "feature_names.csv", feature_names)
    write_coefficients(out / "coefficients.csv", ship_model, feature_names)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "model.pkl").open("wb") as f:
        pickle.dump({
            "model": ship_model, "champion_baseline_model": m_base, "feature_set": feature_set,
            "feature_names": feature_names, "champ_to_idx": champ_to_idx, "idx_to_cid": idx_to_cid,
            "champion_profiles": profiles_payload,
            "config": {"data": str(data), "score_csv": str(score_csv), "current_patch": current_patch,
                       "half_life_days": half_life_days, "empirical_min_games": empirical_min_games, "seed": seed},
        }, f)
    click.echo(f"\nwrote candidate -> {out}/model.pkl")


if __name__ == "__main__":
    main()
