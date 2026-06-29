"""Extract per-participant in-game performance stats for the champ-controlled ARAM-skill probe.

The participants__ parquet kept items/augments/spells but not the box-score.  Matchmaking
balances WIN RATE but NOT individual KDA / damage-share / gold-share, so champ-controlled
performance is the most promising recoverable ARAM-skill signal.  This pulls the raw stats.

Output: data/ratings/performance__q2400__{mtime}.parquet
  gidx, created_ms, pid, champ, team, win, kills, deaths, assists,
  gold, dmg_champ, dmg_taken, heal, cc, level
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

import click
import polars as pl

STAT_KEYS = {
    "kills": "kills", "deaths": "deaths", "assists": "assists",
    "gold": "gold_earned", "dmg_champ": "total_damage_dealt_to_champions",
    "dmg_taken": "total_damage_taken", "heal": "total_heal",
    "cc": "time_ccing_others", "level": "champ_level",
}


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True, path_type=Path))
@click.option("--queue", default=2400, type=int)
@click.option("--out-dir", default="data/ratings", type=click.Path(path_type=Path))
@click.option("--rebuild", is_flag=True)
def main(db, queue, out_dir, rebuild):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    mtime = int(db.stat().st_mtime)
    out = out_dir / f"performance__q{queue}__{mtime}.parquet"
    # reuse the same pid space as players__ by interning puuids in created order
    players_pf = sorted(Path(out_dir).glob(f"players__q{queue}__*.parquet"))
    id_of: dict[str, int] = {}
    if players_pf:
        pdf = pl.read_parquet(players_pf[-1])
        for pid, puuid in zip(pdf["pid"].to_list(), pdf["puuid"].to_list()):
            id_of[puuid] = pid
    if out.exists() and not rebuild:
        click.echo(f"[cache] {out.name} exists"); return

    con = sqlite3.connect(str(db))
    cur = con.execute("SELECT created_ms, blue_wins, participants_private_json FROM games WHERE queue_id=? ORDER BY created_ms ASC", [queue])
    cols = {k: [] for k in ("gidx", "created_ms", "pid", "champ", "team", "win", *STAT_KEYS)}
    gidx = seen = 0
    t0 = time.time()
    next_pid = (max(id_of.values()) + 1) if id_of else 0
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
            roster = [p for p in parts if isinstance(p, dict) and p.get("puuid") and p.get("championId") is not None and p.get("teamId") in (100, 200)]
            if len(roster) < 2:
                continue
            for p in roster:
                puuid = p["puuid"]
                pid = id_of.get(puuid)
                if pid is None:
                    pid = next_pid; id_of[puuid] = pid; next_pid += 1
                st = p.get("stats") or {}
                team = p["teamId"]
                cols["gidx"].append(gidx); cols["created_ms"].append(int(created_ms or 0))
                cols["pid"].append(pid); cols["champ"].append(int(p["championId"]))
                cols["team"].append(team); cols["win"].append(int(blue_wins) if team == 100 else int(1 - blue_wins))
                for out_k, raw_k in STAT_KEYS.items():
                    v = st.get(raw_k)
                    cols[out_k].append(int(v) if isinstance(v, (int, float)) else None)
            gidx += 1
        if seen % 200_000 == 0:
            click.echo(f"[perf] {seen:,} scanned, {gidx:,} kept ({time.time()-t0:.0f}s)")
    con.close()
    pl.DataFrame(cols).write_parquet(out)
    click.echo(f"[done] {out.name}  ({len(cols['pid']):,} slots)  {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
