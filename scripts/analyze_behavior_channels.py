"""Corrected behaviour-channel analysis: which player behaviour is the best free elo-proxy?

Fixes two things the user corrected:
  1. "single-minded build" != fewer items.  It means SCHOOL FIXATION (always the same items,
     never situational COUNTERS like anti-heal / anti-shield).  -> measure counter-item ADOPTION
     and item-build ENTROPY, not item count.
  2. ARAM champ choice is NOT fully random: ~12-champ pool (bench + rerolls + trades), field 5.
     -> measure realised champ CONCENTRATION (do they 'main' / comfort-pick).

For each channel we report, reusing the machinery validated in analyze_meta_axis.py:
  STABILITY   split-half (even/odd games) corr  -- is it a real per-player trait?
  ASSORT z    per-game-mean variance vs shuffle null -- do lobbies sort by it at all?
  loo_r       own vs lobbymates corr (raw and DAY-DEMEANED to kill patch drift)
              -- effect SIZE of lobby tiering; compare to raw build-style baseline loo_r=0.077.

The winner (most stable + most assortative, outcome-orthogonal) is the channel to validate
against real SR rank once player_ranks is populated.

Input: data/ratings/participants__q2400__*.parquet, data/cache/counter_item_ids.json
"""
from __future__ import annotations

import glob
import json
import numpy as np
import polars as pl

MS_PER_DAY = 86_400_000


def assortativity(slot_T, gidx, ms, n_games, perm=100, seed=0):
    """z (variance vs shuffle null), loo_r (raw), loo_r (day-demeaned)."""
    order = np.argsort(gidx, kind="stable")
    g = gidx[order]; t = slot_T[order]
    bounds = np.searchsorted(g, np.arange(n_games + 1))
    starts = bounds[:-1]; counts = np.diff(bounds); valid = counts > 0

    def segvar(tv):
        s = np.add.reduceat(tv, starts)
        return float(np.nanvar(s[valid] / counts[valid]))

    real = segvar(t)
    rng = np.random.default_rng(seed); nulls = []
    tc = t.copy()
    for _ in range(perm):
        rng.shuffle(tc); nulls.append(segvar(tc))
    nm, ns = float(np.mean(nulls)), float(np.std(nulls))
    z = (real - nm) / ns if ns > 0 else float("nan")

    gsum = np.zeros(n_games); gcnt = np.zeros(n_games)
    s = np.add.reduceat(t, starts); gsum[valid] = s[valid]; gcnt[valid] = counts[valid]
    loo = (gsum[gidx] - slot_T) / np.maximum(gcnt[gidx] - 1, 1)
    m = (gcnt[gidx] > 1) & ~np.isnan(slot_T)
    loo_r = float(np.corrcoef(slot_T[m], loo[m])[0, 1])

    day = (ms // MS_PER_DAY).astype(np.int64)
    dsum = np.bincount(day, weights=slot_T); dcnt = np.bincount(day).astype(float)
    dT = slot_T - (dsum / np.maximum(dcnt, 1))[day]
    gsum2 = np.bincount(gidx, weights=dT, minlength=n_games)
    loo2 = (gsum2[gidx] - dT) / np.maximum(gcnt[gidx] - 1, 1)
    loo_dr = float(np.corrcoef(dT[m], loo2[m])[0, 1])
    return z, loo_r, loo_dr


def player_metrics(df: pl.DataFrame) -> pl.DataFrame:
    """Per-player behaviour traits from a (subset of) participant rows."""
    base = df.group_by("pid").agg(
        pl.len().alias("games"),
        pl.col("has_counter").mean().alias("counter_adopt"),
        pl.col("hc_resid").mean().alias("counter_resid"),  # champ-controlled
    )
    ic = df.select("pid", "items").explode("items").drop_nulls()
    ic = ic.group_by("pid", "items").len()
    tot = ic.group_by("pid").agg(pl.col("len").sum().alias("tot"))
    ic = ic.join(tot, on="pid").with_columns((pl.col("len") / pl.col("tot")).alias("p"))
    ent = ic.group_by("pid").agg((-(pl.col("p") * pl.col("p").log()).sum()).alias("item_entropy"))
    cc = df.group_by("pid", "champ").len()
    tt = cc.group_by("pid").agg(pl.col("len").sum().alias("tt"))
    cc = cc.join(tt, on="pid").with_columns((pl.col("len") / pl.col("tt")).alias("p"))
    herf = cc.group_by("pid").agg((pl.col("p") * pl.col("p")).sum().alias("champ_herf"))
    return base.join(ent, on="pid", how="left").join(herf, on="pid", how="left")


def main():
    pf = sorted(glob.glob("data/ratings/participants__q2400__*.parquet"))[-1]
    print(f"[load] {pf}")
    part = pl.read_parquet(pf)
    ids = json.load(open("data/cache/counter_item_ids.json", encoding="utf-8"))
    counter_ids = list(set(ids["anti_heal"] + ids["anti_shield"]))

    part = part.with_columns(
        pl.col("items").list.eval(pl.element().is_in(counter_ids)).list.any().fill_null(False).cast(pl.Int8).alias("has_counter")
    )
    cm = part.group_by("champ").agg(pl.col("has_counter").mean().alias("cm"))
    part = part.join(cm, on="champ").with_columns((pl.col("has_counter") - pl.col("cm")).alias("hc_resid"))

    gidx = part["gidx"].to_numpy(); pid = part["pid"].to_numpy()
    ms = part["created_ms"].to_numpy(); win = part["win"].to_numpy().astype(float)
    n_players = int(pid.max()) + 1; n_games = int(gidx.max()) + 1
    print(f"[data] {part.height:,} slots  {n_players:,} players  {n_games:,} games")
    print(f"[counter-item] overall slot adoption rate = {100*part['has_counter'].mean():.1f}%")

    full = player_metrics(part)
    # numpy arrays indexed by pid for each metric
    def to_arr(dfm, col):
        a = np.full(n_players, np.nan)
        a[dfm["pid"].to_numpy()] = dfm[col].to_numpy()
        return a
    arrs = {c: to_arr(full, c) for c in ("counter_adopt", "counter_resid", "item_entropy", "champ_herf")}

    # ---- split-half stability (recompute metrics on even vs odd games) ----
    ev = player_metrics(part.filter((pl.col("gidx") % 2) == 0))
    od = player_metrics(part.filter((pl.col("gidx") % 2) == 1))
    j = ev.join(od, on="pid", suffix="_o")
    j = j.filter((pl.col("games") >= 5) & (pl.col("games_o") >= 5))
    print(f"\n=== STABILITY  split-half corr (n={j.height:,} players, >=5 games each half) ===")
    print(f"  {'win_rate (reference)':22s} computed separately below")
    for col in ("counter_adopt", "counter_resid", "item_entropy", "champ_herf"):
        a = j[col].to_numpy(); b = j[col + "_o"].to_numpy()
        msk = ~np.isnan(a) & ~np.isnan(b)
        r = float(np.corrcoef(a[msk], b[msk])[0, 1]) if msk.sum() > 2 else float("nan")
        print(f"  {col:22s} r = {r:+.3f}")
    # win-rate stability for contrast
    wr_e = part.filter((pl.col('gidx') % 2) == 0).group_by('pid').agg(pl.col('win').mean().alias('w'), pl.len().alias('g'))
    wr_o = part.filter((pl.col('gidx') % 2) == 1).group_by('pid').agg(pl.col('win').mean().alias('w'), pl.len().alias('g'))
    wj = wr_e.join(wr_o, on='pid', suffix='_o').filter((pl.col('g') >= 5) & (pl.col('g_o') >= 5))
    print(f"  {'win_rate':22s} r = {float(np.corrcoef(wj['w'].to_numpy(), wj['w_o'].to_numpy())[0,1]):+.3f}   <- noise reference")

    # ---- assortativity per channel ----
    print(f"\n=== ASSORTATIVITY  (baseline: raw build-style loo_r=0.077; bigger = stronger lobby tiers) ===")
    print(f"  {'channel':22s} {'z':>8s} {'loo_r':>9s} {'loo_r(day)':>11s}")
    for col in ("counter_adopt", "counter_resid", "item_entropy", "champ_herf"):
        slot_T = arrs[col][pid]
        ok = ~np.isnan(slot_T)
        # fill nan slots with global mean so reduceat stays aligned
        st = slot_T.copy(); st[~ok] = np.nanmean(slot_T)
        z, lr, ldr = assortativity(st, gidx, ms, n_games)
        print(f"  {col:22s} {z:8.1f} {lr:+9.3f} {ldr:+11.3f}")

    print("\n[note] counter_resid = champ-controlled (did you build counters MORE than typical for your champ).")
    print("[note] champ_herf = how concentrated your realised champ pool is (high = you main/comfort-pick).")


if __name__ == "__main__":
    main()
