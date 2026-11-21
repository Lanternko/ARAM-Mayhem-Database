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

import os

# Before polars loads: its allocator otherwise keeps freed frame memory
# committed (1.4GB at 4.4M games) while the matrices are built beside it.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import json
import pickle
import sys
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
from aram_nn.lr_matrix import (
    RowMask, TeamRows, chunked_csr, masked_weights, patch_rows, time_split,
)
from aram_nn.parquet_batches import iter_parquet_rows, read_team_arrays
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def raw_wr(blue: np.ndarray, red: np.ndarray, blue_wins: np.ndarray) -> dict[int, tuple[float, int]]:
    """Champion id -> (win rate, games) over the given games, either side."""
    games = np.bincount(np.concatenate([blue.ravel(), red.ravel()]))
    wins = (np.bincount(blue.ravel(), weights=np.repeat(blue_wins, 5), minlength=len(games))
            + np.bincount(red.ravel(), weights=np.repeat(~blue_wins, 5), minlength=len(games)))
    return {int(c): (int(wins[c]) / int(games[c]), int(games[c])) for c in np.flatnonzero(games)}


def feature_chunk(teams: TeamRows, idx_to_cid, profiles, feature_set, n_champs):
    """Dense features for one slice of games: champion identity + composition blocks."""
    champ = champion_matrix(teams, n_champs)
    blue, red = build_team_profiles(teams, idx_to_cid, profiles)
    blocks = build_feature_blocks(blue, red)
    comp_blocks, names = select_blocks(blocks, feature_set)
    return np.concatenate([champ, *comp_blocks], axis=1), names


def build_x(blue: np.ndarray, red: np.ndarray, idx_to_cid, profiles, feature_set, n_champs):
    """CSR features for every game, built a slice at a time.  *blue*/*red* are
    ``(games, 5)`` champion columns.

    The dense matrix and the per-team profile objects only ever exist for one
    slice, so peak memory follows the sparse result, not the pool size.
    """
    names: list[str] = []

    def chunk(start: int, stop: int) -> np.ndarray:
        x, chunk_names = feature_chunk(
            TeamRows(blue[start:stop].tolist(), red[start:stop].tolist()),
            idx_to_cid, profiles, feature_set, n_champs)
        names[:] = chunk_names
        return x

    return chunked_csr(len(blue), chunk), names


def fit_c(x_tr, y_tr, x_va, y_va, w_tr=None):
    """Tune C on val, then fit.  ``w_tr`` may be zero on rows of ``x_tr`` that
    are not part of this fit (see ``masked_weights``)."""
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
    # The pool as arrays in file order; the splits are row-index arrays over
    # them.  As polars frames, every sort/filter/gather copied all champion
    # lists and the freed copies stayed committed (most of this step's 8GB at
    # 4.4M games on the refresher's polars).
    teams = read_team_arrays(data, min_duration_sec=300)
    created = teams.created
    is_cur = patch_rows(teams.patch, current_patch)
    is_non_cur = teams.patch.is_not_null().to_numpy() & ~is_cur
    n_cur, n_non_cur = int(is_cur.sum()), int(is_non_cur.sum())
    if n_cur <= holdout + 100:
        raise click.ClickException(f"{current_patch} has {n_cur} games (<= holdout)")
    train_idx, val_idx, test_idx = time_split(created, is_cur, is_non_cur,
                                              holdout=holdout, val_size=val_size)
    del is_non_cur

    # Champion id -> column, ids ascending; val/test keep only games whose
    # champions all appear in train.
    vocab = np.union1d(np.unique(teams.blue[train_idx]), np.unique(teams.red[train_idx]))
    champ_to_idx = {int(cid): i for i, cid in enumerate(vocab)}
    idx_to_cid = {i: c for c, i in champ_to_idx.items()}
    n_champs = len(champ_to_idx)

    def all_known(rows: np.ndarray) -> np.ndarray:
        return rows[np.isin(teams.blue[rows], vocab).all(axis=1)
                    & np.isin(teams.red[rows], vocab).all(axis=1)]

    val_idx, test_idx = all_known(val_idx), all_known(test_idx)
    n_tr, n_va, n_te = len(train_idx), len(val_idx), len(test_idx)

    click.echo(f"train={n_tr} (pool {n_non_cur - val_size} + early-{current_patch} {n_cur - holdout})  "
               f"val={n_va}  test={n_te}  champs={n_champs}")

    # movers (raw WR drift prev->current) for the diagnostic subset
    wr0, wr1 = (raw_wr(teams.blue[rows], teams.red[rows], teams.blue_wins[rows])
                for rows in (patch_rows(teams.patch, prev_patch), is_cur))
    movers = {c for c in wr1 if c in wr0 and wr0[c][1] >= 300 and wr1[c][1] >= 300
              and abs((wr1[c][0] - wr0[c][0]) * 100) >= mover_min_drift}
    mover_ids = np.fromiter(movers, dtype=np.int64, count=len(movers))
    test_mask = (np.isin(teams.blue[test_idx], mover_ids).any(axis=1)
                 | np.isin(teams.red[test_idx], mover_ids).any(axis=1))
    click.echo(f"movers(|dWR|>={mover_min_drift}pp)={len(movers)}  test mover-game coverage={test_mask.mean()*100:.0f}%\n")

    # Everything the fits need, in one [train, val, test] block: the fits pick
    # their rows with zero sample weights instead of holding a copy per split.
    order = np.concatenate([train_idx, val_idx, test_idx])
    blue_cols = np.searchsorted(vocab, teams.blue[order]).astype(np.int32)
    red_cols = np.searchsorted(vocab, teams.red[order]).astype(np.int32)
    y_all = teams.blue_wins[order].astype(np.float64)
    t = created[train_idx].astype(np.float64)
    t_all = created[order].astype(np.float64)
    base_rows = np.flatnonzero(patch_rows(teams.patch, baseline_patch)[train_idx])
    train_rows = RowMask(teams.source_row[train_idx])
    del teams, created, is_cur, order, train_idx, val_idx, test_idx

    # Empirical champion profiles from TRAIN ONLY (no leakage; includes early current patch).
    profiles = build_champion_profiles(score_csv=score_csv, train_df=None,
                                       min_games=empirical_min_games, replace_sustain=True,
                                       empirical_rows=iter_parquet_rows(
                                           data, ["blue_wins", "duration_sec", "participants_json"],
                                           selected_rows=train_rows,
                                       ))

    x_all, feat_names = build_x(blue_cols, red_cols, idx_to_cid, profiles, feature_set, n_champs)
    del blue_cols, red_cols
    feature_names = [f"champion:{idx_to_cid[i]}" for i in range(n_champs)] + feat_names
    y_tr = y_all[:n_tr]
    x_va, y_va = x_all[n_tr:n_tr + n_va], y_all[n_tr:n_tr + n_va]
    x_te, y_te = x_all[n_tr + n_va:], y_all[n_tr + n_va:]
    is_train = np.arange(len(y_all)) < n_tr

    # recency weights on train
    t_ref = t.max()
    w = np.exp(-(t_ref - t) / (half_life_days * DAY_MS))
    w *= len(w) / w.sum()

    # baseline: train on baseline_patch rows only (single-patch regime), uniform
    m_base, c_base = fit_c(x_all[base_rows], y_tr[base_rows], x_va, y_va)
    m_flat, c_flat = fit_c(x_all, y_all, x_va, y_va, w_tr=masked_weights(is_train))
    m_rec, c_rec = fit_c(x_all, y_all, x_va, y_va, w_tr=masked_weights(is_train, w))

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
    # value most).  Same matrix as above with every row weighted, so it's just one fit.
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
        "train_rows": n_tr, "val_rows": n_va, "test_rows": n_te,
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
