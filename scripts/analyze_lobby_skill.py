"""Can we filter 'high-level' ARAM games?  -> does performance-skill ASSORT into lobbies?

If Mayhem matchmaking sorts players by a performance-linked MMR, then per-game mean skill has
real spread (some lobbies genuinely higher level) and filtering works.  If not, every lobby
averages to ~the same level and there is nothing to filter.  Tested on the strongest stable
performance trait (champ-controlled dmg_share) AND the SR-calibrated composite (aram_skill),
with the games-count + day-demean controls that killed the earlier false positives.

Also writes the per-game lobby score for the user to filter on.
Inputs: performance__*.parquet, data/ratings/aram_skill.parquet
Output: data/ratings/game_skill.parquet (gidx, created_ms, n, lobby_skill_mean, lobby_skill_min)
"""
from __future__ import annotations
import glob
import numpy as np
import polars as pl

MS_PER_DAY = 86_400_000


def assortativity(slot_T, gidx, ms, n_games, perm=100, seed=0):
    st = slot_T.copy()
    st[np.isnan(st)] = np.nanmean(slot_T)
    order = np.argsort(gidx, kind="stable")
    g = gidx[order]
    bounds = np.searchsorted(g, np.arange(n_games + 1))
    starts = bounds[:-1]; counts = np.diff(bounds); valid = counts > 0
    ts = st[order]

    def segvar(tv):
        s = np.add.reduceat(tv, starts)
        return float(np.nanvar(s[valid] / counts[valid]))

    real = segvar(ts)
    rng = np.random.default_rng(seed); nulls = []
    tc = ts.copy()
    for _ in range(perm):
        rng.shuffle(tc); nulls.append(segvar(tc))
    nm, ns = float(np.mean(nulls)), float(np.std(nulls))
    z = (real - nm) / ns if ns > 0 else float("nan")
    excess = 100 * ((real / nm) ** 0.5 - 1) if nm > 0 else float("nan")

    gsum = np.zeros(n_games); gcnt = np.zeros(n_games)
    s = np.add.reduceat(ts, starts); gsum[valid] = s[valid]; gcnt[valid] = counts[valid]
    loo = (gsum[gidx] - st) / np.maximum(gcnt[gidx] - 1, 1)
    m = (gcnt[gidx] > 1) & ~np.isnan(slot_T)
    loo_r = float(np.corrcoef(st[m], loo[m])[0, 1])
    day = (ms // MS_PER_DAY).astype(np.int64)
    dsum = np.bincount(day, weights=st); dcnt = np.bincount(day).astype(float)
    dT = st - (dsum / np.maximum(dcnt, 1))[day]
    g2 = np.bincount(gidx, weights=dT, minlength=n_games)
    loo2 = (g2[gidx] - dT) / np.maximum(gcnt[gidx] - 1, 1)
    loo_dr = float(np.corrcoef(dT[m], loo2[m])[0, 1])
    return z, excess, loo_r, loo_dr


def main():
    perf = pl.read_parquet(sorted(glob.glob("data/ratings/performance__q2400__*.parquet"))[-1])
    team = perf.group_by("gidx", "team").agg(pl.col("dmg_champ").sum().alias("tdmg"))
    perf = perf.join(team, on=["gidx", "team"]).with_columns(
        (pl.col("dmg_champ") / pl.max_horizontal(pl.col("tdmg"), pl.lit(1))).alias("dmg_share"))
    perf = perf.with_columns((pl.col("dmg_champ") / pl.max_horizontal(pl.col("gold"), pl.lit(1))).alias("dpg"))
    cm = perf.group_by("champ").agg(pl.col("dmg_share").mean().alias("cm"), pl.col("dpg").mean().alias("cm_dpg"))
    perf = perf.join(cm, on="champ").with_columns(
        (pl.col("dmg_share") - pl.col("cm")).alias("ds_res"),
        (pl.col("dpg") - pl.col("cm_dpg")).alias("dpg_res"))

    pid = perf["pid"].to_numpy(); gidx = perf["gidx"].to_numpy(); ms = perf["created_ms"].to_numpy()
    n_players = int(pid.max()) + 1; n_games = int(gidx.max()) + 1
    gc = np.bincount(pid, minlength=n_players).astype(float)
    lg = np.log(np.maximum(gc, 1))

    # per-player traits
    ds_trait = np.divide(np.bincount(pid, weights=perf["ds_res"].to_numpy(), minlength=n_players),
                         np.maximum(gc, 1), out=np.full(n_players, np.nan), where=gc > 0)
    dpg_trait = np.divide(np.bincount(pid, weights=perf["dpg_res"].to_numpy(), minlength=n_players),
                          np.maximum(gc, 1), out=np.full(n_players, np.nan), where=gc > 0)
    sk = pl.read_parquet("data/ratings/aram_skill.parquet")
    skill = np.full(n_players, np.nan)
    skill[sk["pid"].to_numpy()] = sk["aram_skill"].to_numpy()

    print("=== does performance-skill assort into lobbies? (vs games-count baseline) ===")
    for name, trait in [("dpg/efficiency (non-share)", dpg_trait), ("dmg_share (share-deflated)", ds_trait),
                        ("aram_skill (SR-calib)", skill), ("games_count (confound ref)", gc)]:
        z, exc, lr, ldr = assortativity(trait[pid], gidx, ms, n_games)
        mb = ~np.isnan(trait) & (gc >= 10)
        gb = float(np.corrcoef(trait[mb], lg[mb])[0, 1])
        print(f"  {name:26s} z={z:7.1f}  excess_std={exc:+5.1f}%  loo_r={lr:+.3f}  day={ldr:+.3f}  (games-bias {gb:+.2f})")

    # per-game lobby score on BOTH the SR-calibrated composite and the clean efficiency signal
    order = np.argsort(gidx, kind="stable")
    g = gidx[order]
    bounds = np.searchsorted(g, np.arange(n_games + 1)); starts = bounds[:-1]; counts = np.diff(bounds)
    valid = counts > 0
    gms = np.zeros(n_games); gms[g[starts[valid]]] = ms[order][starts[valid]]

    def lobby_mean(trait):
        ts = trait[pid][order]
        s = np.add.reduceat(np.nan_to_num(ts, nan=0.0), starts)
        c = np.add.reduceat((~np.isnan(ts)).astype(float), starts)
        out = np.full(n_games, np.nan); out[valid] = np.divide(s[valid], np.maximum(c[valid], 1))
        return out, c

    eff_z = (dpg_trait - np.nanmean(dpg_trait)) / np.nanstd(dpg_trait)  # cleanest signal, sd units
    mean_sk, cnt_known = lobby_mean(skill)
    mean_eff, _ = lobby_mean(eff_z)
    out = pl.DataFrame({
        "gidx": np.arange(n_games)[valid],
        "created_ms": gms[valid].astype(np.int64),
        "n_known": cnt_known[valid].astype(int),
        "lobby_skill_tier": mean_sk[valid],     # SR-tier units (interpretable, share-deflated)
        "lobby_eff_z": mean_eff[valid],         # efficiency sd units (cleanest filter signal)
    }).filter(pl.col("n_known") >= 8)
    out.write_parquet("data/ratings/game_skill.parquet")
    e = out["lobby_eff_z"].to_numpy()
    print(f"\n[game_skill] {out.height:,} games (>=8 known players)  -> data/ratings/game_skill.parquet")
    print("  lobby_eff_z (efficiency) percentiles:", {p: round(float(np.percentile(e, p)), 2) for p in (5, 25, 50, 75, 95)})
    print(f"  top-decile vs bottom-decile lobby gap = {np.percentile(e,90)-np.percentile(e,10):.2f} sd of player efficiency")


if __name__ == "__main__":
    main()
