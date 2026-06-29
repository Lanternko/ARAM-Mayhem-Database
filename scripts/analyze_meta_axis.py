"""Test whether a behavioural 'lobby tier' (elo-like meta) axis exists in Mayhem.

Premise the user is right about: matchmaking balances WIN RATE, not BEHAVIOUR.  So a
high-elo and a low-elo lobby can both be ~50% internally yet pick/build very differently.
Earlier finding (player_skill_unrecoverable_mayhem) only killed *outcome*-derived skill;
behaviour is a separate, untested channel.

Three falsifiable tests, none of which use win/loss:

  T1  STABILITY   per-player behaviour traits split-half corr (even vs odd games).
                  If win-rate replicates at r=0 but Heartsteel-rate replicates high,
                  behaviour carries the recoverable signal.
  T2  ASSORTATIVITY  do high-trait players share lobbies?  per-game mean(trait) variance
                  vs a permutation null that reshuffles players across games.  Real >> null
                  == tiered lobbies genuinely exist.  This is THE test for "high/low elo games".
  T3  GRADIENT    bin games by mean Heartsteel-rate; show how champion picks (Sion/Lillia/Sett
                  vs Xin Zhao/Ambessa) and build shift from the low end to the high end.

Inputs:  data/lcu/games.db, data/cache/ddragon_champion_byid.json
Outputs (local only): data/ratings/meta_axis_roster.parquet (cache), console report.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import click
import numpy as np
import polars as pl

HEARTSTEEL = {3084, 223084}
MS_PER_DAY = 86_400_000


def load_roster(db: Path, queue: int, out_dir: Path, rebuild: bool, limit: int) -> pl.DataFrame:
    """One row per participant: gidx, created_ms, pid, champ, win, has_hs, n_items."""
    mtime = int(db.stat().st_mtime)
    cache = out_dir / f"meta_axis_roster__q{queue}__{mtime}.parquet"
    if cache.exists() and not rebuild and not limit:
        click.echo(f"[load] cache hit {cache.name}")
        return pl.read_parquet(cache)

    con = sqlite3.connect(str(db))
    sql = "SELECT created_ms, blue_wins, participants_private_json FROM games WHERE queue_id=? ORDER BY created_ms ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = con.execute(sql, [queue])

    id_of: dict[str, int] = {}
    gidx_c: list[int] = []
    ms_c: list[int] = []
    pid_c: list[int] = []
    champ_c: list[int] = []
    win_c: list[int] = []
    hs_c: list[int] = []
    ni_c: list[int] = []
    gidx = seen = 0
    t0 = time.time()
    while True:
        rows = cur.fetchmany(20_000)
        if not rows:
            break
        for created_ms, blue_wins, pj in rows:
            seen += 1
            if not pj:
                continue
            try:
                parts = json.loads(pj)
            except (ValueError, TypeError):
                continue
            roster = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                puuid = p.get("puuid")
                champ = p.get("championId")
                team = p.get("teamId")
                if not puuid or champ is None or team not in (100, 200):
                    continue
                items = p.get("items") or []
                roster.append((puuid, int(champ), team, items))
            if len(roster) < 2:
                continue
            for puuid, champ, team, items in roster:
                pid = id_of.get(puuid)
                if pid is None:
                    pid = len(id_of)
                    id_of[puuid] = pid
                gidx_c.append(gidx)
                ms_c.append(int(created_ms or 0))
                pid_c.append(pid)
                champ_c.append(champ)
                win_c.append(int(blue_wins) if team == 100 else int(1 - blue_wins))
                hs_c.append(1 if any(it in HEARTSTEEL for it in items) else 0)
                ni_c.append(len(items))
            gidx += 1
        if seen % 200_000 == 0:
            click.echo(f"[load] scanned {seen:,}  games kept {gidx:,}  ({time.time()-t0:.0f}s)")
    con.close()

    df = pl.DataFrame(
        {
            "gidx": gidx_c,
            "created_ms": ms_c,
            "pid": pid_c,
            "champ": champ_c,
            "win": win_c,
            "has_hs": hs_c,
            "n_items": ni_c,
        }
    )
    click.echo(f"[load] {seen:,} games scanned, {gidx:,} kept, {len(id_of):,} players, {df.height:,} slots ({time.time()-t0:.0f}s)")
    if not limit:
        out_dir.mkdir(parents=True, exist_ok=True)
        df.write_parquet(cache)
    return df


def _split_half_corr(pid, val, parity, n_players, min_games):
    """Per-player mean(val) on even vs odd slots; corr for players with >=min_games each half."""
    sum_e = np.zeros(n_players); cnt_e = np.zeros(n_players)
    sum_o = np.zeros(n_players); cnt_o = np.zeros(n_players)
    np.add.at(sum_e, pid[parity == 0], val[parity == 0])
    np.add.at(cnt_e, pid[parity == 0], 1.0)
    np.add.at(sum_o, pid[parity == 1], val[parity == 1])
    np.add.at(cnt_o, pid[parity == 1], 1.0)
    mask = (cnt_e >= min_games) & (cnt_o >= min_games)
    if mask.sum() < 3:
        return float("nan"), 0
    a = sum_e[mask] / cnt_e[mask]
    b = sum_o[mask] / cnt_o[mask]
    if a.std() == 0 or b.std() == 0:
        return float("nan"), int(mask.sum())
    return float(np.corrcoef(a, b)[0, 1]), int(mask.sum())


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True, path_type=Path))
@click.option("--queue", default=2400, type=int)
@click.option("--out-dir", default="data/ratings", type=click.Path(path_type=Path))
@click.option("--min-games", default=10, type=int, help="Min games per half for stability test.")
@click.option("--perm", default=200, type=int, help="Permutations for assortativity null.")
@click.option("--rebuild", is_flag=True)
@click.option("--limit", default=0, type=int)
def main(db, queue, out_dir, min_games, perm, rebuild, limit):
    out_dir = Path(out_dir)
    df = load_roster(db, queue, out_dir, rebuild, limit)
    champ_map = {int(k): v for k, v in json.load(open("data/cache/ddragon_champion_byid.json", encoding="utf-8")).items()}

    gidx = df["gidx"].to_numpy()
    pid = df["pid"].to_numpy()
    champ = df["champ"].to_numpy()
    win = df["win"].to_numpy().astype(float)
    has_hs = df["has_hs"].to_numpy().astype(float)
    n_items = df["n_items"].to_numpy().astype(float)
    n_players = int(pid.max()) + 1
    n_games = int(gidx.max()) + 1
    parity = (np.arange(df.height) % 2)  # alternate slots; per-player splits average out

    # ---- T1 STABILITY: does behaviour replicate where win-rate did not? ----
    click.echo("\n=== T1  split-half stability (players with >= %d games each half) ===" % min_games)
    gpar = (gidx % 2)  # split by GAME parity so each player's games fall into both halves
    for name, val in [("win_rate", win), ("heartsteel_rate", has_hs), ("n_items", n_items)]:
        r, n = _split_half_corr(pid, val, gpar, n_players, min_games)
        click.echo(f"  {name:16s} split-half r = {r:+.3f}   (n={n:,} players)")

    # ---- champion-CONTROLLED build trait (strips random champ assignment) ----
    # Heartsteel-rate is confounded by which champ you were handed (tanks build it).
    # Residualise vs the champ's population norm: did you build HS MORE than typical FOR THIS CHAMP?
    uc, inv = np.unique(champ, return_inverse=True)
    ccnt = np.bincount(inv).astype(float)
    cmean_hs = np.bincount(inv, weights=has_hs) / ccnt
    cmean_it = np.bincount(inv, weights=n_items) / ccnt
    hs_res_slot = has_hs - cmean_hs[inv]      # build CHOICE beyond champ norm
    it_res_slot = n_items - cmean_it[inv]

    def player_mean(slotval):
        s = np.bincount(pid, weights=slotval, minlength=n_players)
        c = np.bincount(pid, minlength=n_players).astype(float)
        return np.divide(s, c, out=np.full(n_players, np.nan), where=c > 0)

    p_hs_raw = player_mean(has_hs)          # confounded by champ roll
    p_hs_res = player_mean(hs_res_slot)     # champ-controlled choice
    p_cnt = np.bincount(pid, minlength=n_players).astype(float)

    # stability of the champ-controlled trait too
    r, n = _split_half_corr(pid, hs_res_slot, gpar, n_players, min_games)
    click.echo(f"  {'heartsteel_resid':16s} split-half r = {r:+.3f}   (n={n:,} players)  [champ-controlled]")

    # ---- T2 ASSORTATIVITY: do similar-building players cluster into the same lobbies? ----
    order = np.argsort(gidx, kind="stable")
    g_sorted = gidx[order]
    bounds = np.searchsorted(g_sorted, np.arange(n_games + 1))
    seg_starts = bounds[:-1]
    seg_counts = np.diff(bounds)
    valid = seg_counts > 0
    rng = np.random.default_rng(0)

    def assortativity(slot_T, label):
        t_sorted = slot_T[order]
        def seg_means(tv):
            s = np.add.reduceat(tv, seg_starts)
            return s[valid] / seg_counts[valid]
        real_var = float(np.nanvar(seg_means(t_sorted)))
        null_vars = []
        t_copy = t_sorted.copy()
        for _ in range(perm):
            rng.shuffle(t_copy)
            null_vars.append(float(np.nanvar(seg_means(t_copy))))
        nm_, ns_ = float(np.mean(null_vars)), float(np.std(null_vars))
        z = (real_var - nm_) / ns_ if ns_ > 0 else float("nan")
        g_sum = np.add.reduceat(t_sorted, seg_starts)
        g_sum_full = np.zeros(n_games); g_cnt_full = np.zeros(n_games)
        g_sum_full[valid] = g_sum[valid]; g_cnt_full[valid] = seg_counts[valid]
        loo = (g_sum_full[gidx] - slot_T) / np.maximum(g_cnt_full[gidx] - 1, 1)
        mm = ~np.isnan(slot_T) & ~np.isnan(loo) & (g_cnt_full[gidx] > 1)
        loo_r = float(np.corrcoef(slot_T[mm], loo[mm])[0, 1])
        excess = 100 * ((real_var / nm_) ** 0.5 - 1) if nm_ > 0 else float("nan")
        click.echo(f"  [{label}]")
        click.echo(f"    game-mean variance real={real_var:.5f} null={nm_:.5f}+-{ns_:.5f}  z={z:+.1f}  excess_std={excess:+.1f}%  loo_r={loo_r:+.3f}")
        return g_sum_full, g_cnt_full

    click.echo("\n=== T2  lobby assortativity (z>>0 / loo_r>0 == tiered lobbies exist) ===")
    assortativity(p_hs_raw[pid], "raw Heartsteel-rate  (champ-confounded baseline)")
    g_sum_full, g_cnt_full = assortativity(p_hs_res[pid], "champ-controlled build choice  <-- the real elo test")
    slot_T = p_hs_res[pid]

    # ---- T3 GRADIENT: champion + build meta from low-HS to high-HS lobbies ----
    gmean = g_sum_full / np.maximum(g_cnt_full, 1)
    gmean_games = gmean[np.arange(n_games)]
    gvalid = g_cnt_full >= 8  # well-populated games only
    gm = gmean_games.copy(); gm[~gvalid] = np.nan
    qs = np.nanquantile(gm, [0.1, 0.9])
    lo_games = set(np.where(gm <= qs[0])[0].tolist())
    hi_games = set(np.where(gm >= qs[1])[0].tolist())
    click.echo(f"\n=== T3  meta gradient: bottom vs top decile lobbies by champ-controlled build choice ===")
    click.echo(f"  low-end games={len(lo_games):,}  high-end games={len(hi_games):,}")
    in_lo = np.array([g in lo_games for g in gidx])
    in_hi = np.array([g in hi_games for g in gidx])
    n_lo_slots = in_lo.sum(); n_hi_slots = in_hi.sum()
    # champ pick rates per end
    def pickrate(maskslots):
        c, cnt = np.unique(champ[maskslots], return_counts=True)
        return dict(zip(c.tolist(), cnt.tolist())), int(maskslots.sum())
    lo_counts, lo_tot = pickrate(in_lo)
    hi_counts, hi_tot = pickrate(in_hi)
    anchors = {14: "Sion", 876: "Lillia", 875: "Sett", 5: "XinZhao", 799: "Ambessa"}
    click.echo("  --- your named anchors (pick rate low-end -> high-end) ---")
    for cid, nm in anchors.items():
        lo = 100 * lo_counts.get(cid, 0) / max(lo_tot, 1)
        hi = 100 * hi_counts.get(cid, 0) / max(hi_tot, 1)
        arrow = "UP" if hi > lo else "dn"
        click.echo(f"    {nm:9s}: {lo:5.2f}%  ->  {hi:5.2f}%   ({arrow} x{hi/max(lo,0.01):.2f})")
    # biggest movers overall
    allc = set(lo_counts) | set(hi_counts)
    rows = []
    for cid in allc:
        lo = lo_counts.get(cid, 0) / max(lo_tot, 1)
        hi = hi_counts.get(cid, 0) / max(hi_tot, 1)
        if lo + hi < 0.001:
            continue
        rows.append((cid, lo, hi, (hi + 1e-4) / (lo + 1e-4)))
    rows.sort(key=lambda x: x[3])
    nm = lambda cid: champ_map.get(cid, {}).get("alias", str(cid))
    click.echo("  --- most LOW-end-skewed champions (rate hi/lo) ---")
    for cid, lo, hi, ratio in rows[:8]:
        click.echo(f"    {nm(cid):12s} {100*lo:5.2f}% -> {100*hi:5.2f}%   x{ratio:.2f}")
    click.echo("  --- most HIGH-end-skewed champions ---")
    for cid, lo, hi, ratio in rows[-8:][::-1]:
        click.echo(f"    {nm(cid):12s} {100*lo:5.2f}% -> {100*hi:5.2f}%   x{ratio:.2f}")
    click.echo(f"\n  win-rate (sanity, expect ~50/50)  low={100*win[in_lo].mean():.1f}%  high={100*win[in_hi].mean():.1f}%")
    click.echo(f"  build: avg items  low={n_items[in_lo].mean():.2f}  high={n_items[in_hi].mean():.2f}")
    click.echo(f"  raw Heartsteel slot-rate  low={100*has_hs[in_lo].mean():.1f}%  high={100*has_hs[in_hi].mean():.1f}%")


if __name__ == "__main__":
    main()
