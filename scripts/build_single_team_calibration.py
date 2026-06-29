"""Compute single_team_calibration.json for a composition LR model.

The composition LR is trained on both teams (logit = b + Sum_blue - Sum_red).
The recommender's "current win rate" display shows P(blue wins) with the
opponent zeroed (Sum_red = 0), so the both-teams bias `b` is the wrong centering
and the number reads inflated.  This fits a 1-parameter recalibrated intercept
b' so that sigmoid(team_contribution + b') is calibrated on real single teams.

Uses both blue teams (label = blue_wins) and red teams (label = not blue_wins)
from recent games, recency-weighted, and a Newton fit.

  python scripts/build_single_team_calibration.py \
      --data data/raw/mayhem_pooled_16_10_12.parquet \
      --model-dir models/composition_lr_pooled_recency_7d
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.recommend import load_composition_lr  # noqa: E402

DAY_MS = 86_400_000


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--model-dir", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--sample", default=80000, show_default=True, help="most-recent games to calibrate on")
@click.option("--half-life-days", default=7.0, show_default=True)
def main(data, model_dir, sample, half_life_days):
    model = load_composition_lr(Path(model_dir))
    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300).sort("game_creation_ms")
    df = df.tail(sample)

    contribs, labels, weights = [], [], []
    t = df["game_creation_ms"].to_numpy().astype(np.float64)
    t_ref = t.max()
    blue_l = df["blue_champions"].to_list(); red_l = df["red_champions"].to_list()
    win_l = df["blue_wins"].to_list()
    for i in range(df.height):
        w = float(np.exp(-(t_ref - t[i]) / (half_life_days * DAY_MS)))
        cb, ub = model.team_logit_contribution([int(c) for c in blue_l[i]])
        cr, ur = model.team_logit_contribution([int(c) for c in red_l[i]])
        if not ub:
            contribs.append(cb); labels.append(1.0 if win_l[i] else 0.0); weights.append(w)
        if not ur:
            contribs.append(cr); labels.append(0.0 if win_l[i] else 1.0); weights.append(w)

    c = np.array(contribs); y = np.array(labels); w = np.array(weights)
    # Newton fit of b' for sigmoid(c + b').
    b = 0.0
    for _ in range(50):
        p = 1.0 / (1.0 + np.exp(-(c + b)))
        grad = float(np.sum(w * (p - y)))
        hess = float(np.sum(w * p * (1.0 - p))) + 1e-9
        step = grad / hess
        b -= step
        if abs(step) < 1e-8:
            break

    p = 1.0 / (1.0 + np.exp(-(c + b)))
    wmean = float(np.sum(w * p) / np.sum(w))
    base = float(np.sum(w * y) / np.sum(w))
    out = Path(model_dir) / "single_team_calibration.json"
    out.write_text(json.dumps({"single_team_intercept": float(b)}, indent=2), encoding="utf-8")
    click.echo(f"single_team_intercept = {b:+.4f}  (full intercept was {model.intercept:+.4f})")
    click.echo(f"calibrated mean pred {wmean:.4f} vs base rate {base:.4f}  on {len(y)} teams")
    click.echo(f"wrote {out}")


if __name__ == "__main__":
    main()
