"""Produce a game_id-keyed lobby-skill table that joins to the tier-list / training dataset.

Keys everything by game_id + puuid (stable) instead of the internal gidx, so there is no
positional-alignment risk.  Per-player efficiency trait (champ-controlled dmg/gold, the one
signal that survived every lobby confound) is averaged over each game's players.

Self-verifies: the game_id version must reproduce the ~0.50 sd top/bottom-decile gap that
analyze_lobby_skill.py found on the gidx version.

Inputs: performance__*.parquet, players__*.parquet, data/lcu/games.db
Output: data/ratings/game_skill_by_id.parquet
        game_id, patch, created_ms, blue_wins, n_known, lobby_eff_z
"""
from __future__ import annotations
import glob
import json
import sqlite3
import time
import numpy as np
import polars as pl


def main():
    # 1) per-player efficiency trait (all players), champ-controlled, z-scored
    perf = pl.read_parquet(sorted(glob.glob("data/ratings/performance__q2400__*.parquet"))[-1])
    perf = perf.with_columns((pl.col("dmg_champ") / pl.max_horizontal(pl.col("gold"), pl.lit(1))).alias("dpg"))
    cm = perf.group_by("champ").agg(pl.col("dpg").mean().alias("cm"))
    perf = perf.join(cm, on="champ").with_columns((pl.col("dpg") - pl.col("cm")).alias("dpg_res"))
    n_players = int(perf["pid"].max()) + 1
    gc = np.bincount(perf["pid"].to_numpy(), minlength=n_players).astype(float)
    trait = np.divide(np.bincount(perf["pid"].to_numpy(), weights=perf["dpg_res"].to_numpy(), minlength=n_players),
                      np.maximum(gc, 1), out=np.full(n_players, np.nan), where=gc > 0)
    trait_z = (trait - np.nanmean(trait)) / np.nanstd(trait)

    players = pl.read_parquet(sorted(glob.glob("data/ratings/players__q2400__*.parquet"))[-1])
    eff_by_puuid = {}
    for pid, puuid in zip(players["pid"].to_list(), players["puuid"].to_list()):
        if pid < n_players and not np.isnan(trait_z[pid]):
            eff_by_puuid[puuid] = float(trait_z[pid])
    print(f"[trait] efficiency-z for {len(eff_by_puuid):,} players")

    # 2) scan games.db once: game_id -> puuids ; compute lobby mean
    con = sqlite3.connect("data/lcu/games.db")
    cur = con.execute("SELECT game_id, patch, created_ms, blue_wins, participants_private_json FROM games WHERE queue_id=2400")
    gid_c, patch_c, ms_c, bw_c, nk_c, eff_c = ([] for _ in range(6))
    seen = 0; t0 = time.time()
    while True:
        rows = cur.fetchmany(20_000)
        if not rows:
            break
        for game_id, patch, created_ms, blue_wins, pj in rows:
            seen += 1
            if not pj:
                continue
            try:
                parts = json.loads(pj)
            except (ValueError, TypeError):
                continue
            vals = []
            for p in parts:
                if isinstance(p, dict) and p.get("puuid") in eff_by_puuid:
                    vals.append(eff_by_puuid[p["puuid"]])
            if len(vals) < 2:
                continue
            gid_c.append(game_id); patch_c.append(patch); ms_c.append(int(created_ms or 0))
            bw_c.append(int(blue_wins or 0)); nk_c.append(len(vals)); eff_c.append(float(np.mean(vals)))
        if seen % 300_000 == 0:
            print(f"  scanned {seen:,} ({time.time()-t0:.0f}s)")
    con.close()
    out = pl.DataFrame({"game_id": gid_c, "patch": patch_c, "created_ms": ms_c,
                        "blue_wins": bw_c, "n_known": nk_c, "lobby_eff_z": eff_c})
    out.write_parquet("data/ratings/game_skill_by_id.parquet")
    print(f"[done] data/ratings/game_skill_by_id.parquet  ({out.height:,} games, {time.time()-t0:.0f}s)")

    # 3) VERIFY: reproduce the gradient on well-known games
    wk = out.filter(pl.col("n_known") >= 8)["lobby_eff_z"].to_numpy()
    gap = float(np.percentile(wk, 90) - np.percentile(wk, 10))
    print(f"[verify] {len(wk):,} games with >=8 known players")
    print(f"[verify] top-decile vs bottom-decile gap = {gap:.2f} sd  (gidx version found 0.50 — should match)")
    print("  lobby_eff_z percentiles:", {p: round(float(np.percentile(wk, p)), 2) for p in (5, 25, 50, 75, 95)})
    cov = out.group_by("patch").agg(pl.len().alias("games")).sort("games", descending=True).head(5)
    print("  top patches covered:", {r["patch"]: r["games"] for r in cov.iter_rows(named=True)})


if __name__ == "__main__":
    main()
