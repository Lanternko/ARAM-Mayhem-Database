"""Extract per-champion tier list from LR-solo (single-team) model.

Trains LR on mirrored solo rows (combined 16.9+16.10), then reads off
each champion's logit weight w_i. Converts to:
  - solo_logit_contrib = w_i              (raw weight; sum across 5 picks ≈ logit)
  - per_pick_winrate_delta ≈ sigmoid'(0) * w_i ≈ 0.25 * w_i   (small-w approx)
  - bayes_wr from raw counts (sanity check, prior strength = 30)
  - sample size for confidence

Output: terminal table + CSV.
"""
from __future__ import annotations

from pathlib import Path

import click
import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss


def _load_champion_name_map() -> dict[int, str]:
    try:
        from aram_nn.lcu.client import LCUClient, get_champion_summary
        from aram_nn.lcu.process import get_credentials
        creds = get_credentials()
        if creds is None:
            return {}
        with LCUClient(creds) as lcu:
            summary = get_champion_summary(lcu)
        m = {}
        for row in summary:
            cid = row.get("id"); name = row.get("alias") or row.get("name")
            if cid is None or not name: continue
            m[int(cid)] = str(name)
        return m
    except Exception:
        return {}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patches", multiple=True, default=("16.9", "16.10"), show_default=True)
@click.option("--min-games", default=30, show_default=True, type=int, help="Drop champs with fewer team appearances")
@click.option("--out", default=Path("models/tier_list_solo.csv"), type=click.Path(path_type=Path))
@click.option("--C", "Cval", default=0.01, show_default=True, type=float)
@click.option("--prior-strength", default=30, show_default=True, type=int)
def main(data, patches, min_games, out, Cval, prior_strength):
    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p")
    ).filter(pl.col("p").is_in(list(patches)))
    click.echo(f"[data] n_matches={df.height}  patches={list(patches)}")

    # Build per-match rows -> mirror to solo
    blue_lists = df["blue_champions"].to_list()
    red_lists  = df["red_champions"].to_list()
    wins       = df["blue_wins"].to_list()

    champ_set = set()
    for r in blue_lists: champ_set.update(r)
    for r in red_lists:  champ_set.update(r)
    champs = sorted(champ_set)
    c2i = {c: i for i, c in enumerate(champs)}
    n = len(champs)
    click.echo(f"[vocab] {n} champions")

    # Solo rows
    teams, labels = [], []
    for bl, rd, w in zip(blue_lists, red_lists, wins):
        teams.append(bl);  labels.append(int(w))
        teams.append(rd);  labels.append(int(1 - w))
    click.echo(f"[rows] {len(teams)} solo rows (label mean={np.mean(labels):.4f})")

    # Per-champ raw counts (for bayes + min_games filter)
    games = np.zeros(n, dtype=np.int64)
    wins_n = np.zeros(n, dtype=np.int64)
    for team, lbl in zip(teams, labels):
        for c in team:
            i = c2i[c]
            games[i] += 1
            wins_n[i] += lbl
    raw_wr = np.where(games > 0, wins_n / np.maximum(games, 1), 0.5)
    global_wr = 0.5  # by construction
    bayes_wr = (wins_n + global_wr * prior_strength) / (games + prior_strength)

    # Build X, y for LR
    X = np.zeros((len(teams), n), dtype=np.float32)
    y = np.array(labels, dtype=np.float32)
    for r, team in enumerate(teams):
        for c in team:
            X[r, c2i[c]] = 1.0

    # Fit
    clf = LogisticRegression(C=Cval, max_iter=2000, solver="lbfgs", fit_intercept=True)
    clf.fit(X, y)
    train_ll = log_loss(y, clf.predict_proba(X)[:, 1])
    train_acc = ((clf.predict_proba(X)[:, 1] >= 0.5) == y.astype(bool)).mean()
    click.echo(f"[LR fit] C={Cval}  intercept={clf.intercept_[0]:+.4f}  train log_loss={train_ll:.4f}  train acc={train_acc:.4f}")

    weights = clf.coef_[0]   # shape (n,)
    # Each pick's contribution to logit ≈ w_i.
    # P(win) lift from picking champ i, holding rest equal:
    #   logit -> logit + w_i
    # Around logit=0, dP/dlogit = 0.25, so per_pick_winrate_delta ≈ 0.25 * w_i

    name_map = _load_champion_name_map()
    if not name_map:
        click.echo("[warn] no LCU running — names not resolved, showing champion_id only")

    rows = []
    for cid in champs:
        i = c2i[cid]
        rows.append({
            "champion_id": cid,
            "champion_name": name_map.get(cid, f"id_{cid}"),
            "games": int(games[i]),
            "wins": int(wins_n[i]),
            "raw_wr": float(raw_wr[i]),
            "bayes_wr": float(bayes_wr[i]),
            "lr_weight": float(weights[i]),
            "wr_delta_pp": float(0.25 * weights[i] * 100),  # percentage points
        })

    # Filter by min_games AND sort by lr_weight desc
    rows_kept = [r for r in rows if r["games"] >= min_games]
    rows_kept.sort(key=lambda r: -r["lr_weight"])
    click.echo(f"\n[tier list] {len(rows_kept)} champs >= {min_games} games (of {len(rows)} total)")

    # Print top + bottom
    def fmt(r, rank):
        return (f"  {rank:>3}  id={r['champion_id']:<3}  {r['champion_name']:<14.14}  "
                f"games={r['games']:>5}  wr={r['raw_wr']*100:5.1f}%  "
                f"bayes={r['bayes_wr']*100:5.1f}%  "
                f"lr_w={r['lr_weight']:+.4f}  dwr~{r['wr_delta_pp']:+5.2f}pp")

    click.echo("\n=== TOP 25 (LR-solo strongest) ===")
    for rank, r in enumerate(rows_kept[:25], 1): click.echo(fmt(r, rank))

    click.echo("\n=== BOTTOM 25 (LR-solo weakest) ===")
    for rank, r in enumerate(rows_kept[-25:], len(rows_kept) - 24): click.echo(fmt(r, rank))

    # CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows_kept).write_csv(out)
    click.echo(f"\n[saved] {out}  ({len(rows_kept)} rows)")

    # Self-consistency check: predict on a few synthetic "all top-5" comps
    click.echo("\n[sanity] picking the LR-top-5 vs bottom-5 (single-team predictions):")
    top5 = [r["champion_id"] for r in rows_kept[:5]]
    bot5 = [r["champion_id"] for r in rows_kept[-5:]]
    def _p(comp):
        x = np.zeros((1, n), dtype=np.float32)
        for c in comp: x[0, c2i[c]] = 1.0
        return float(clf.predict_proba(x)[0, 1])
    click.echo(f"  top5  comp = {[r['champion_name'] for r in rows_kept[:5]]}: P(win) = {_p(top5)*100:.1f}%")
    click.echo(f"  bot5  comp = {[r['champion_name'] for r in rows_kept[-5:]]}: P(win) = {_p(bot5)*100:.1f}%")


if __name__ == "__main__":
    main()
