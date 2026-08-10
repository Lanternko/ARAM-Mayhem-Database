"""Extract a full per-participant table from the LCU Mayhem DB for behaviour + rank analysis.

The cached roster used by analyze_meta_axis.py only kept has_hs/n_items.  The corrected
build analysis (situational/counter-item ADOPTION, build-school fixation) and the rank join
need the raw item / augment / spell lists plus the riotId, so this dumps them once.

Outputs (local only — data/ is .gitignored):
  data/ratings/participants__q{queue}__{mtime}.parquet
      gidx, created_ms, pid, champ, win, items(list[int]), augments(list[int]), spells(list[int])
  data/ratings/players__q{queue}__{mtime}.parquet
      pid, puuid(36-char LCU), game_name, tag_line, games
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import click
import polars as pl


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True, path_type=Path))
@click.option("--queue", default=2400, type=int)
@click.option("--out-dir", default="data/ratings", type=click.Path(path_type=Path))
@click.option("--rebuild", is_flag=True)
@click.option(
    "--ordered/--unordered",
    default=True,
    show_default=True,
    help="Sort by created_ms for reproducible chronology; unordered avoids a large SQLite temp sort.",
)
def main(db, queue, out_dir, rebuild, ordered):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mtime = int(db.stat().st_mtime)
    part_path = out_dir / f"participants__q{queue}__{mtime}.parquet"
    play_path = out_dir / f"players__q{queue}__{mtime}.parquet"
    if part_path.exists() and play_path.exists() and not rebuild:
        click.echo(f"[cache] {part_path.name} + {play_path.name} already exist")
        return

    con = sqlite3.connect(str(db))
    order_sql = " ORDER BY created_ms ASC" if ordered else ""
    cur = con.execute(
        "SELECT created_ms, blue_wins, participants_private_json FROM games WHERE queue_id=?" + order_sql,
        [queue],
    )
    id_of: dict[str, int] = {}
    gname: dict[str, str] = {}
    tline: dict[str, str] = {}
    gcount: dict[int, int] = {}
    gidx_c, ms_c, pid_c, champ_c, win_c, items_c, aug_c, sp_c = ([] for _ in range(8))
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
            roster = [
                p
                for p in parts
                if isinstance(p, dict) and p.get("puuid") and p.get("championId") is not None and p.get("teamId") in (100, 200)
            ]
            if len(roster) < 2:
                continue
            for p in roster:
                puuid = p["puuid"]
                pid = id_of.get(puuid)
                if pid is None:
                    pid = len(id_of)
                    id_of[puuid] = pid
                if puuid not in gname and p.get("gameName"):
                    gname[puuid] = p.get("gameName")
                    tline[puuid] = p.get("tagLine")
                gcount[pid] = gcount.get(pid, 0) + 1
                team = p["teamId"]
                gidx_c.append(gidx)
                ms_c.append(int(created_ms or 0))
                pid_c.append(pid)
                champ_c.append(int(p["championId"]))
                win_c.append(int(blue_wins) if team == 100 else int(1 - blue_wins))
                items_c.append([int(x) for x in (p.get("items") or [])])
                aug_c.append([int(x) for x in (p.get("augments") or [])])
                sp_c.append([int(x) for x in (p.get("spells") or [])])
            gidx += 1
        if seen % 200_000 == 0:
            click.echo(f"[extract] {seen:,} scanned, {gidx:,} kept ({time.time()-t0:.0f}s)")
    con.close()

    pl.DataFrame(
        {"gidx": gidx_c, "created_ms": ms_c, "pid": pid_c, "champ": champ_c, "win": win_c,
         "items": items_c, "augments": aug_c, "spells": sp_c}
    ).write_parquet(part_path)
    inv = {v: k for k, v in id_of.items()}
    pids = sorted(inv)
    pl.DataFrame(
        {"pid": pids, "puuid": [inv[i] for i in pids],
         "game_name": [gname.get(inv[i]) for i in pids],
         "tag_line": [tline.get(inv[i]) for i in pids],
         "games": [gcount.get(i, 0) for i in pids]}
    ).write_parquet(play_path)
    click.echo(f"[done] {part_path.name} ({len(gidx_c):,} slots)  {play_path.name} ({len(pids):,} players)  {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
