"""Champ-controlled in-game performance: is THIS the recoverable ARAM-skill signal?

Matchmaking balances win rate (-> noise, split-half r=0) but NOT individual performance.
For each metric we residualise against the champion's population norm (removes the role
confound: tanks tank, ADCs damage), average per player, and split-half test (even vs odd
games) to separate real skill (persists) from single-game luck/stomps (does not).

Metrics (champ-controlled): KDA, kill-participation, damage-share, gold-share, damage/gold.
A metric that is STABLE here AND later correlates with SR rank is a genuine ARAM-skill axis.

Input: data/ratings/performance__q2400__*.parquet
"""
from __future__ import annotations
import glob
import numpy as np
import polars as pl

METRICS = ["kda", "kp", "dmg_share", "gold_share", "dpg"]


def main():
    pf = sorted(glob.glob("data/ratings/performance__q2400__*.parquet"))
    if not pf:
        print("[abort] no performance parquet — run extract_performance.py first.")
        return
    perf = pl.read_parquet(pf[-1])
    print(f"[data] {perf.height:,} slots  {perf['pid'].n_unique():,} players")

    team = perf.group_by("gidx", "team").agg(
        pl.col("kills").sum().alias("tk"), pl.col("dmg_champ").sum().alias("tdmg"), pl.col("gold").sum().alias("tgold"))
    perf = perf.join(team, on=["gidx", "team"]).with_columns(
        ((pl.col("kills") + pl.col("assists")) / pl.max_horizontal(pl.col("deaths"), pl.lit(1))).alias("kda"),
        ((pl.col("kills") + pl.col("assists")) / pl.max_horizontal(pl.col("tk"), pl.lit(1))).alias("kp"),
        (pl.col("dmg_champ") / pl.max_horizontal(pl.col("tdmg"), pl.lit(1))).alias("dmg_share"),
        (pl.col("gold") / pl.max_horizontal(pl.col("tgold"), pl.lit(1))).alias("gold_share"),
        (pl.col("dmg_champ") / pl.max_horizontal(pl.col("gold"), pl.lit(1))).alias("dpg"),
    )
    # champ-control: residual vs champ population mean of each metric
    for m in METRICS:
        cmean = perf.group_by("champ").agg(pl.col(m).mean().alias(f"_cm_{m}"))
        perf = perf.join(cmean, on="champ").with_columns((pl.col(m) - pl.col(f"_cm_{m}")).alias(f"{m}_res"))

    pid = perf["pid"].to_numpy()
    gpar = (perf["gidx"].to_numpy() % 2)
    win = perf["win"].to_numpy().astype(float)
    n_players = int(pid.max()) + 1

    def split_half(val, min_g=10):
        se = np.zeros(n_players); ce = np.zeros(n_players); so = np.zeros(n_players); co = np.zeros(n_players)
        e = gpar == 0; o = ~e
        np.add.at(se, pid[e], val[e]); np.add.at(ce, pid[e], 1.0)
        np.add.at(so, pid[o], val[o]); np.add.at(co, pid[o], 1.0)
        mask = (ce >= min_g) & (co >= min_g)
        a = se[mask] / ce[mask]; b = so[mask] / co[mask]
        keep = ~np.isnan(a) & ~np.isnan(b)
        return float(np.corrcoef(a[keep], b[keep])[0, 1]), int(keep.sum())

    print("\n=== split-half stability of champ-controlled performance (>=10 games each half) ===")
    r, n = split_half(win)
    print(f"  {'win_rate':14s} r = {r:+.3f}   (noise reference, n={n:,})")
    gc = np.bincount(pid, minlength=n_players).astype(float)
    lg = np.log(np.maximum(gc, 1))
    for m in METRICS:
        v = perf[f"{m}_res"].to_numpy()
        ok = ~np.isnan(v)
        vv = v.copy(); vv[~ok] = 0.0
        r, n = split_half(vv)
        # games-count bias check on the per-player trait
        ps = np.bincount(pid, weights=np.where(ok, v, 0.0), minlength=n_players)
        pc = np.bincount(pid, weights=ok.astype(float), minlength=n_players)
        trait = np.divide(ps, pc, out=np.full(n_players, np.nan), where=pc > 0)
        mb = ~np.isnan(trait) & (gc >= 10)
        gb = float(np.corrcoef(trait[mb], lg[mb])[0, 1])
        print(f"  {m:14s} r = {r:+.3f}   (n={n:,},  corr w/ log-games = {gb:+.3f})")

    print("\n[note] high stability + ~0 games-bias = a real per-player axis; SR correlation decides if it's elo.")


if __name__ == "__main__":
    main()
