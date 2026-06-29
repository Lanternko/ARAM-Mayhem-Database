"""Build an ARAM-skill score for every player, calibrated against SR rank.

Pipeline that the whole investigation converged on:
  - win/loss = noise (unrecoverable);  build/champ PREFERENCE = does not track rank;
  - champ-controlled PERFORMANCE (dmg/gold/kill-participation/efficiency) = stable AND
    correlates with SR tier.
So: fit  SR_tier ~ champ-controlled performance  on the resolved players, cross-validate it,
then apply the model to ALL ~425k players to get a free ARAM-skill score (SR-tier units).

This is the deliverable answer to "approximate a player's rank from their PUUID" without
calling the API for everyone — SR is only the calibration anchor.

Inputs: performance__*.parquet, players__*.parquet, data/ratings/player_ranks.db
Output: data/ratings/aram_skill.parquet  (pid, puuid, n_games, aram_skill, aram_tier_est, traits)
"""
from __future__ import annotations
import glob
import sqlite3
import numpy as np
import polars as pl
from scipy import stats

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIV = {"IV": 0, "III": 1, "II": 2, "I": 3, None: 3}
FEATURES = ["dmg_share", "gold_share", "kp", "dpg", "kda"]
MIN_GAMES = 10


def tier_ordinal(tier, div, lp):
    if tier not in TIERS:
        return None
    base = TIERS.index(tier) * 4 + DIV.get(div, 3)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") and lp:
        base += min(lp, 1500) / 500.0
    return base


def main():
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
    for m in FEATURES:
        cmean = perf.group_by("champ").agg(pl.col(m).mean().alias(f"_cm_{m}"))
        perf = perf.join(cmean, on="champ").with_columns((pl.col(m) - pl.col(f"_cm_{m}")).alias(f"{m}_res"))
    traits = perf.group_by("pid").agg(
        pl.len().alias("n_games"), pl.col("win").mean().alias("win_rate"),
        *[pl.col(f"{m}_res").mean().alias(m) for m in FEATURES])
    traits = traits.filter(pl.col("n_games") >= MIN_GAMES)

    # z-score features over the trait population
    feat = {}
    for m in FEATURES:
        v = traits[m].to_numpy().astype(float)
        mu, sd = np.nanmean(v), np.nanstd(v)
        feat[m] = np.nan_to_num((v - mu) / (sd if sd else 1.0))
    X = np.column_stack([feat[m] for m in FEATURES])
    pid = traits["pid"].to_numpy()

    # SR labels
    rcon = sqlite3.connect("data/ratings/player_ranks.db")
    rmap = {}
    for lcu, st, sd, slp, ft, fd, flp in rcon.execute(
            "SELECT lcu_puuid, solo_tier, solo_div, solo_lp, flex_tier, flex_div, flex_lp FROM player_ranks WHERE status='ok'"):
        tier, div, lp = (st, sd, slp) if st else (ft, fd, flp)
        o = tier_ordinal(tier, div, lp)
        if o is not None:
            rmap[lcu] = o
    pid_to_puuid = dict(zip(players["pid"].to_list(), players["puuid"].to_list()))
    y = np.array([rmap.get(pid_to_puuid.get(p), np.nan) for p in pid])
    lab = ~np.isnan(y)
    print(f"[calib] players with traits={len(pid):,}  SR-labelled={int(lab.sum()):,}")

    Xl, yl = X[lab], y[lab]
    Xl1 = np.column_stack([Xl, np.ones(len(Xl))])

    # 5-fold CV: predict held-out SR, correlate
    rng = np.random.default_rng(0)
    idx = rng.permutation(len(yl))
    preds = np.full(len(yl), np.nan)
    for k in range(5):
        te = idx[k::5]; tr = np.setdiff1d(idx, te)
        w, *_ = np.linalg.lstsq(Xl1[tr], yl[tr], rcond=None)
        preds[te] = Xl1[te] @ w
    pear = stats.pearsonr(preds, yl)
    spear = stats.spearmanr(preds, yl)
    print(f"[validate] 5-fold CV  predicted-vs-actual SR:  pearson r={pear.statistic:+.3f} (p={pear.pvalue:.1e})  spearman rho={spear.statistic:+.3f}")
    print(f"           => the score explains ~{100*pear.statistic**2:.0f}% of SR-tier variance (out-of-sample)")

    # final model on all labelled, weights
    w, *_ = np.linalg.lstsq(Xl1, yl, rcond=None)
    print("[weights] (per +1 sd, in SR-tier-quarters):")
    for m, wi in zip(FEATURES, w[:-1]):
        print(f"    {m:12s} {wi:+.2f}")

    # apply to everyone
    score = np.column_stack([X, np.ones(len(X))]) @ w
    tier_idx = np.clip((np.round(score) // 4).astype(int), 0, 9)
    out = traits.select("pid", "n_games", "win_rate", *FEATURES).with_columns(
        pl.Series("aram_skill", score),
        pl.Series("aram_tier_est", [TIERS[i] for i in tier_idx]),
    )
    out = out.join(players.select("pid", "puuid"), on="pid", how="left")
    out.write_parquet("data/ratings/aram_skill.parquet")

    print(f"\n[done] data/ratings/aram_skill.parquet  ({out.height:,} players scored)")
    print("[dist] aram_skill percentiles:",
          {p: round(float(np.percentile(score, p)), 1) for p in (5, 25, 50, 75, 95)})
    band = out.group_by("aram_tier_est").agg(pl.len().alias("n")).sort("n", descending=True)
    print("[dist] estimated-tier counts:", {r["aram_tier_est"]: r["n"] for r in band.iter_rows(named=True)})


if __name__ == "__main__":
    main()
