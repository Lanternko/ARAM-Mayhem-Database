"""Build the tier2 champion-strength LR (GUI z-score + fallback win-prob) from
pooled, recency-weighted data — the champion-only companion to the pooled
composition model.

Champion-only (signed +1 blue / -1 red identity), so no empirical profiles
needed — fast.  Same pool + exp recency weighting as the composition candidate,
so the displayed champion strength is on the same patch-current model as the
win probability.  Emits lr_weights.json + champ_to_idx.json that recommend_gui
loads via aram_nn.recommend.load_lr.

  python scripts/build_pooled_champ_lr.py \
      --data data/raw/mayhem_pooled_16_10_12.parquet \
      --current-patch 16.12 --half-life-days 7 \
      --out-dir models/composition_lr_pooled_recency_7d
"""
from __future__ import annotations

import os

# Before polars loads: its allocator otherwise keeps freed frame memory
# committed (1.4GB at 4.4M games) while the matrices are built beside it.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import json
import sys
from pathlib import Path

import click
import numpy as np
import polars as pl
from aram_nn.lr_matrix import champion_vocab, chunked_csr, masked_weights, patch_rows
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_ability_nn import TeamDataset  # noqa: E402
from analyze_composition_signals import champion_matrix  # noqa: E402
from train_composition_lr import C_GRID  # noqa: E402

DAY_MS = 86_400_000


def identity(df, champ_to_idx, n):
    """Signed champion identity as CSR (10 non-zeros a game), built a slice at
    a time -- dense, this was ~3GB at 4M games and was built twice."""
    return chunked_csr(
        df.height,
        lambda start, stop: champion_matrix(
            TeamDataset(df.slice(start, stop - start), champ_to_idx), n))


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--current-patch", default="16.12", show_default=True)
@click.option("--val-size", default=15000, show_default=True)
@click.option("--half-life-days", default=7.0, show_default=True)
@click.option("--out-dir", required=True, type=click.Path(path_type=Path))
def main(data, current_patch, val_size, half_life_days, out_dir):
    # participants_json is ~8.5GB of the pooled parquet in memory and nothing
    # here reads it; leaving it out keeps this step off the OOM edge that the
    # export used to fall over.
    #
    # Rows stay in file order.  Nothing below depends on their order except
    # "the latest pre-current games", which is picked by timestamp; sorting the
    # frame instead copied every champion list (≈5GB at 4.4M games on the
    # refresher's polars, most of this step's peak).
    df = (
        pl.scan_parquet(data)
        .filter(pl.col("duration_sec") >= 300)
        .select("patch", "game_creation_ms", "blue_champions", "red_champions", "blue_wins")
        .collect()
    )

    champ_to_idx = champion_vocab(df)
    n = len(champ_to_idx)
    y_all = df["blue_wins"].cast(pl.Float32).to_numpy().astype(np.float64)
    t_all = df["game_creation_ms"].to_numpy().astype(np.float64)
    # One matrix over every game.  The C-tuning fits leave the held-out late
    # pre-current games out with zero sample weights rather than a second copy.
    earlier = np.flatnonzero(df["patch"].is_not_null().to_numpy()
                             & ~patch_rows(df["patch"], current_patch))
    is_val = np.zeros(df.height, dtype=bool)
    is_val[earlier[np.argsort(t_all[earlier], kind="stable")[max(len(earlier) - val_size, 0):]]] = True
    x_all = identity(df.select("blue_champions", "red_champions", "blue_wins"), champ_to_idx, n)
    del df

    # Tune C on the held-out late pre-current val, training weighted.
    x_va, y_va = x_all[is_val], y_all[is_val]
    w_tc = np.exp(-(t_all.max() - t_all[~is_val]) / (half_life_days * DAY_MS))
    w_tc *= len(w_tc) / w_tc.sum()
    w_tune = masked_weights(~is_val, w_tc)
    best_c, best_ll = C_GRID[0], float("inf")
    for c in C_GRID:
        m = LogisticRegression(C=c, max_iter=2000, solver="lbfgs")
        m.fit(x_all, y_all, sample_weight=w_tune)
        ll = log_loss(y_va, m.predict_proba(x_va)[:, 1])
        if ll < best_ll:
            best_ll, best_c = ll, c

    # Final fit on ALL rows, recency-weighted.
    w_all = np.exp(-(t_all.max() - t_all) / (half_life_days * DAY_MS))
    w_all *= len(w_all) / w_all.sum()
    final = LogisticRegression(C=best_c, max_iter=2000, solver="lbfgs")
    final.fit(x_all, y_all, sample_weight=w_all)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    coef = final.coef_[0].astype(float).tolist()
    (out_dir / "lr_weights.json").write_text(
        json.dumps({"coef": coef, "intercept": float(final.intercept_[0])}), encoding="utf-8")
    (out_dir / "champ_to_idx.json").write_text(
        json.dumps({str(cid): int(idx) for cid, idx in champ_to_idx.items()}), encoding="utf-8")
    click.echo(f"champ-LR: C={best_c}  n_champs={n}  rows={len(y_all)}  val_ll={best_ll:.4f}  "
               f"coef[min/mean/max]={min(coef):+.3f}/{np.mean(coef):+.3f}/{max(coef):+.3f}")
    click.echo(f"wrote {out_dir}/lr_weights.json + champ_to_idx.json")


if __name__ == "__main__":
    main()
