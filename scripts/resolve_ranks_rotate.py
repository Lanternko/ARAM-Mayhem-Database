"""Single-thread, key-ROTATING rank resolver (gentle on Riot's rate penalty).

The 4-thread version tripped Riot's punitive 429 back-off (concurrent bursts from one IP).
This runs ONE thread and rotates the N keys round-robin, so each key sees only ~1/N of the
traffic and each RiotClient's own 88/120s throttle paces it safely.  ~same total throughput,
no bursts.  Writes to data/ratings/player_ranks.db, idempotent (skips status='ok').
"""
from __future__ import annotations
import glob
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import polars as pl

from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired

SOLO = "RANKED_SOLO_5x5"
FLEX = "RANKED_FLEX_SR"
_DDL = """CREATE TABLE IF NOT EXISTS player_ranks(
  lcu_puuid TEXT PRIMARY KEY, public_puuid TEXT, riot_id TEXT,
  solo_tier TEXT, solo_div TEXT, solo_lp INTEGER,
  flex_tier TEXT, flex_div TEXT, flex_lp INTEGER,
  games INTEGER, status TEXT, resolved_at TEXT)"""
_BRIDGE_DDL = "CREATE TABLE IF NOT EXISTS bridge(lcu_puuid TEXT PRIMARY KEY, public_puuid TEXT, riot_id TEXT)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@click.command()
@click.option("--games-db", default="data/lcu/games.db", type=click.Path(path_type=Path))
@click.option("--ranks-db", default="data/ratings/player_ranks.db", type=click.Path(path_type=Path))
@click.option("--players-dir", default="data/ratings", type=click.Path(path_type=Path))
@click.option("--keys-file", default="data/.riot_keys", type=click.Path(path_type=Path))
@click.option("--region", default="tw")
@click.option("--min-games", default=20, type=int)
@click.option("--limit", default=4000, type=int)
@click.option("--seed", default=7, type=int)
def main(games_db, ranks_db, players_dir, keys_file, region, min_games, limit, seed):
    keys = [l.strip() for l in Path(keys_file).read_text().splitlines() if l.strip().startswith("RGAPI-")]
    if not keys:
        raise SystemExit("no keys")
    clients = [RiotClient(region, api_key=k) for k in keys]
    click.echo(f"[keys] {len(clients)} clients (rotating)")

    pf = sorted(glob.glob(str(Path(players_dir) / "players__q2400__*.parquet")))[-1]
    df = (pl.read_parquet(pf)
          .filter((pl.col("games") >= min_games) & pl.col("game_name").is_not_null() & pl.col("tag_line").is_not_null())
          .sample(n=min(limit * 3, pl.read_parquet(pf).height), seed=seed, shuffle=True))

    Path(ranks_db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ranks_db))
    con.execute(_DDL); con.execute(_BRIDGE_DDL); con.commit()
    done = {r[0] for r in con.execute("SELECT lcu_puuid FROM player_ranks WHERE status='ok'")}
    bridge = {r[0]: r[1] for r in con.execute("SELECT lcu_puuid, public_puuid FROM bridge WHERE public_puuid IS NOT NULL")}
    try:
        g = sqlite3.connect(f"file:{games_db}?mode=ro", uri=True)
        for lcu, pub in g.execute("SELECT lcu_puuid, public_puuid FROM riot_id_bridge WHERE public_puuid IS NOT NULL"):
            bridge.setdefault(lcu, pub)
        g.close()
    except sqlite3.OperationalError:
        pass

    todo = [r for r in df.iter_rows(named=True) if r["puuid"] not in done][:limit]
    click.echo(f"[plan] eligible={df.height:,}  to_resolve={len(todo):,}  bridge_known={len(bridge):,}")
    ok = unranked = fail = 0
    for i, r in enumerate(todo):
        rc = clients[i % len(clients)]
        lcu, rid = r["puuid"], f"{r['game_name']}#{r['tag_line']}"
        try:
            pub = bridge.get(lcu)
            if not pub:
                acc = rc.account_by_riot_id(r["game_name"], r["tag_line"])
                pub = acc.get("puuid")
                if pub:
                    con.execute("INSERT OR REPLACE INTO bridge VALUES(?,?,?)", (lcu, pub, rid))
            if not pub:
                con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lcu, None, rid, None, None, None, None, None, None, r["games"], "no_account", _now()))
                fail += 1
            else:
                ent = {e.get("queueType"): e for e in rc.league_entries_by_puuid(pub)}
                s, f = ent.get(SOLO, {}), ent.get(FLEX, {})
                status = "ok" if (s or f) else "unranked"
                con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lcu, pub, rid, s.get("tier"), s.get("rank"), s.get("leaguePoints"),
                             f.get("tier"), f.get("rank"), f.get("leaguePoints"), r["games"], status, _now()))
                ok += status == "ok"; unranked += status == "unranked"
        except RiotKeyExpired:
            click.echo(f"[stop] key#{i % len(clients) + 1} expired; committing partial."); break
        except Exception:
            con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (lcu, None, rid, None, None, None, None, None, None, r["games"], "err", _now()))
            fail += 1
        if (i + 1) % 25 == 0:
            con.commit()
            click.echo(f"  {i+1}/{len(todo)}  ranked={ok} unranked={unranked} fail={fail}")
            sys.stdout.flush()
    con.commit()
    for c in clients:
        c.close()
    rows = con.execute("SELECT solo_tier, COUNT(*) FROM player_ranks WHERE status='ok' AND solo_tier IS NOT NULL"
                       " GROUP BY solo_tier ORDER BY COUNT(*) DESC").fetchall()
    click.echo(f"[done] ranked={ok} unranked={unranked} fail={fail}  coverage={100*ok/max(ok+unranked+fail,1):.0f}%")
    click.echo("solo tiers: " + (", ".join(f"{t}:{c}" for t, c in rows) or "(none)"))


if __name__ == "__main__":
    main()
