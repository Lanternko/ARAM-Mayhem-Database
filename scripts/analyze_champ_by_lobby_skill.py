"""Does the champion meta differ by lobby skill?  (the payoff question)

Splits one patch's games into top-quartile vs bottom-quartile lobby_eff_z and compares each
champion's win rate.  diff>0 = champ wins MORE in high-skill lobbies (skill-scaling / "needs
good play"); diff<0 = "low-skill stomper".  Within-patch so champ balance changes don't leak.

Caveat: in a high-eff lobby every player (incl. the champ's own + the enemy) is more efficient,
and reroll/trade gives mild champ selection — so a win-rate gap is "how this champ fares when
everyone plays better", not a clean isolation of the champion. Descriptive, not causal.

Inputs: data/ratings/game_skill_by_id.parquet, data/lcu/games.db, ddragon_champion_byid.json
"""
from __future__ import annotations
import json
import math
import sqlite3
import numpy as np
import polars as pl

PATCH_PREFIX = "16.12"
MIN_GAMES = 300


def parse_champs(s):
    try:
        v = json.loads(s)
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return [int(x) for x in str(s).replace("[", "").replace("]", "").split(",") if x.strip().lstrip("-").isdigit()]


def main():
    gs = pl.read_parquet("data/ratings/game_skill_by_id.parquet").filter(
        pl.col("patch").str.starts_with(PATCH_PREFIX) & (pl.col("n_known") >= 8))
    eff = {gid: e for gid, e in zip(gs["game_id"].to_list(), gs["lobby_eff_z"].to_list())}
    vals = gs["lobby_eff_z"].to_numpy()
    q25, q75 = np.quantile(vals, [0.25, 0.75])
    print(f"[data] patch {PATCH_PREFIX}: {gs.height:,} scored games  q25={q25:.2f}  q75={q75:.2f}")

    champ_map = {int(k): v for k, v in json.load(open("data/cache/ddragon_champion_byid.json", encoding="utf-8")).items()}
    con = sqlite3.connect("file:data/lcu/games.db?mode=ro", uri=True)
    cur = con.execute("SELECT game_id, blue_champs, red_champs, blue_wins FROM games "
                      "WHERE queue_id=2400 AND patch LIKE ?", [PATCH_PREFIX + "%"])
    # per (champ, group): [games, wins]
    stat = {}  # champ -> {'hi':[g,w], 'lo':[g,w]}
    n_hi = n_lo = 0
    while True:
        rows = cur.fetchmany(20_000)
        if not rows:
            break
        for game_id, bc, rc, bw in rows:
            e = eff.get(game_id)
            if e is None:
                continue
            grp = "hi" if e >= q75 else "lo" if e <= q25 else None
            if grp is None:
                continue
            if grp == "hi":
                n_hi += 1
            else:
                n_lo += 1
            for champs, won in ((parse_champs(bc), int(bw)), (parse_champs(rc), 1 - int(bw))):
                for c in champs:
                    d = stat.setdefault(c, {"hi": [0, 0], "lo": [0, 0]})
                    d[grp][0] += 1
                    d[grp][1] += won
    con.close()
    print(f"[split] high-skill games={n_hi:,}  low-skill games={n_lo:,}")

    rows = []
    for c, d in stat.items():
        gh, wh = d["hi"]; gl, wl = d["lo"]
        if gh < MIN_GAMES or gl < MIN_GAMES:
            continue
        ph, pl_ = wh / gh, wl / gl
        pooled = (wh + wl) / (gh + gl)
        se = math.sqrt(pooled * (1 - pooled) * (1 / gh + 1 / gl)) or 1e-9
        z = (ph - pl_) / se
        rows.append((c, gl, gh, pl_, ph, ph - pl_, z))
    rows.sort(key=lambda r: r[5])
    nm = lambda c: champ_map.get(c, {}).get("alias", str(c))
    print(f"\n{len(rows)} champions with >= {MIN_GAMES} games in BOTH skill groups\n")
    print(f"  {'champ':13s} {'lowWR':>7s} {'highWR':>7s} {'diff':>7s} {'z':>6s}")
    print("  --- strongest in HIGH-skill lobbies (skill-scaling) ---")
    for c, gl, gh, pl_, ph, diff, z in rows[-12:][::-1]:
        print(f"  {nm(c):13s} {100*pl_:6.1f}% {100*ph:6.1f}% {100*diff:+6.1f}% {z:+6.1f}")
    print("  --- strongest in LOW-skill lobbies (low-skill stompers) ---")
    for c, gl, gh, pl_, ph, diff, z in rows[:12]:
        print(f"  {nm(c):13s} {100*pl_:6.1f}% {100*ph:6.1f}% {100*diff:+6.1f}% {z:+6.1f}")
    diffs = np.array([r[5] for r in rows])
    sig = np.array([abs(r[6]) > 3 for r in rows])
    print(f"\n[summary] diff spread: sd={100*diffs.std():.1f}pp  max=|{100*np.abs(diffs).max():.1f}|pp  "
          f"significant(|z|>3): {int(sig.sum())}/{len(rows)} champs")


if __name__ == "__main__":
    main()
