"""Does SR rank correlate with champ-controlled ARAM performance? (prong 2 x prong 1)

The performance traits (dmg_share, gold_share, KDA, ... champ-controlled) are stable, games-
count-unbiased per-player axes.  This adjudicates SKILL vs STYLE: if they rise with SR tier,
they are a genuine elo signal we can apply to all ~1M games for free.  If flat vs tier, they
are playstyle, not skill (or SR-skill simply doesn't transfer to ARAM).

Inputs: performance__*.parquet, players__*.parquet, data/ratings/player_ranks.db
"""
from __future__ import annotations
import glob
import sqlite3
import numpy as np
import polars as pl
from scipy import stats

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIV = {"IV": 0, "III": 1, "II": 2, "I": 3, None: 3}
METRICS = ["kda", "kp", "dmg_share", "gold_share", "dpg"]


def tier_ordinal(tier, div, lp):
    if tier not in TIERS:
        return None
    base = TIERS.index(tier) * 4 + DIV.get(div, 3)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") and lp:
        base += min(lp, 1500) / 500.0
    return base


def main():
    rcon = sqlite3.connect("data/ratings/player_ranks.db")
    try:
        rows = rcon.execute("SELECT lcu_puuid, solo_tier, solo_div, solo_lp, flex_tier, flex_div, flex_lp"
                            " FROM player_ranks WHERE status='ok'").fetchall()
    except sqlite3.OperationalError:
        print("[abort] no player_ranks yet."); return
    rr = []
    for lcu, st, sd, slp, ft, fd, flp in rows:
        tier, div, lp = (st, sd, slp) if st else (ft, fd, flp)
        o = tier_ordinal(tier, div, lp)
        if o is not None:
            rr.append({"puuid": lcu, "tier": tier, "ord": o})
    if len(rr) < 30:
        print(f"[wait] only {len(rr)} ranked players so far — rerun when resolution finishes."); return
    rank_df = pl.DataFrame(rr)
    print(f"[ranks] {rank_df.height:,} ranked players")

    perf = pl.read_parquet(sorted(glob.glob("data/ratings/performance__q2400__*.parquet"))[-1])
    players = pl.read_parquet(sorted(glob.glob("data/ratings/players__q2400__*.parquet"))[-1])
    team = perf.group_by("gidx", "team").agg(
        pl.col("kills").sum().alias("tk"), pl.col("dmg_champ").sum().alias("tdmg"), pl.col("gold").sum().alias("tgold"))
    perf = perf.join(team, on=["gidx", "team"]).with_columns(
        ((pl.col("kills") + pl.col("assists")) / pl.max_horizontal(pl.col("deaths"), pl.lit(1))).alias("kda"),
        ((pl.col("kills") + pl.col("assists")) / pl.max_horizontal(pl.col("tk"), pl.lit(1))).alias("kp"),
        (pl.col("dmg_champ") / pl.max_horizontal(pl.col("tdmg"), pl.lit(1))).alias("dmg_share"),
        (pl.col("gold") / pl.max_horizontal(pl.col("tgold"), pl.lit(1))).alias("gold_share"),
        (pl.col("dmg_champ") / pl.max_horizontal(pl.col("gold"), pl.lit(1))).alias("dpg"),
    )
    for m in METRICS:
        cmean = perf.group_by("champ").agg(pl.col(m).mean().alias(f"_cm_{m}"))
        perf = perf.join(cmean, on="champ").with_columns((pl.col(m) - pl.col(f"_cm_{m}")).alias(f"{m}_res"))
    traits = perf.group_by("pid").agg(
        pl.col("win").mean().alias("win_rate"),
        *[pl.col(f"{m}_res").mean().alias(m) for m in METRICS],
    )
    j = players.select("pid", "puuid").join(traits, on="pid", how="inner").join(rank_df, on="puuid", how="inner")
    print(f"[join] ranked players with performance traits = {j.height:,}\n")

    ordv = j["ord"].to_numpy()
    print("=== Spearman(SR tier, trait) — does rank predict ARAM performance? ===")
    for m in ["win_rate"] + METRICS:
        v = j[m].to_numpy(); mk = ~np.isnan(v) & ~np.isnan(ordv)
        rho, pval = stats.spearmanr(ordv[mk], v[mk])
        flag = "  <-- skill signal" if (m != "win_rate" and pval < 0.01 and abs(rho) > 0.1) else ""
        print(f"  {m:12s} rho={rho:+.3f}  p={pval:.1e}{flag}")

    def band(o):
        return "IRON-BRZ" if o < 8 else "SLV-GLD" if o < 16 else "PLT-EMR" if o < 24 else "DIA+"
    j = j.with_columns(pl.col("ord").map_elements(band, return_dtype=pl.Utf8).alias("band"))
    print("\n=== mean champ-controlled trait by SR band ===")
    order = {"IRON-BRZ": 0, "SLV-GLD": 1, "PLT-EMR": 2, "DIA+": 3}
    agg = j.group_by("band").agg(pl.len().alias("n"), *[pl.col(m).mean() for m in METRICS])
    agg = agg.sort(pl.col("band").replace_strict(order, default=9))
    print(f"  {'band':10s} {'n':>5s} " + " ".join(f"{m:>11s}" for m in METRICS))
    for r in agg.iter_rows(named=True):
        print(f"  {r['band']:10s} {r['n']:>5d} " + " ".join(f"{r[m]:>+11.4f}" for m in METRICS))
    print("\n[note] residuals are vs champ mean, so 0 = average-for-champ; positive = over-performs that champ.")


if __name__ == "__main__":
    main()
