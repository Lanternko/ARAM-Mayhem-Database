"""Resolve approximate ranked tier per captured Mayhem player via the public Riot API.

THE BRIDGE (this is the answer to "how do I get rank from a PUUID"):
  our games.db stores 36-char LCU-LOCAL puuids that the public API does NOT accept.
  The usable handle is the stored riotId (gameName#tagLine):
      riotId --account-v1 by-riot-id--> PUBLIC puuid --league-v4 by-puuid--> tier/div/LP
  Already-resolved public puuids are reused from the riot_id_bridge table.
  Results are upserted idempotently into player_ranks.

Routing (TW): account-v1 -> asia, league-v4 -> tw2  (handled by RiotClient).
Needs RIOT_API_KEY (dev key, regenerate every 24h).  --dry-run validates the plan + coverage
with ZERO API calls, so you can confirm everything before spending rate limit.

Examples:
  python scripts/resolve_player_ranks.py --dry-run
  python scripts/resolve_player_ranks.py --min-games 20 --limit 3000
"""
from __future__ import annotations

import glob
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click
import polars as pl

from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired

SOLO = "RANKED_SOLO_5x5"
FLEX = "RANKED_FLEX_SR"

_DDL = """CREATE TABLE IF NOT EXISTS player_ranks(
  lcu_puuid    TEXT PRIMARY KEY,
  public_puuid TEXT,
  riot_id      TEXT,
  solo_tier    TEXT, solo_div TEXT, solo_lp INTEGER,
  flex_tier    TEXT, flex_div TEXT, flex_lp INTEGER,
  games        INTEGER,
  status       TEXT,
  resolved_at  TEXT
)"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(path_type=Path))
@click.option("--players-dir", default="data/ratings", type=click.Path(path_type=Path),
              help="dir holding players__*.parquet from extract_participants.py")
@click.option("--region", default="tw")
@click.option("--min-games", default=20, type=int, help="only resolve players with >= this many captured games")
@click.option("--limit", default=3000, type=int, help="cap API resolutions this run (rate-limit budget)")
@click.option("--dry-run", is_flag=True, help="plan + coverage only, no API calls")
@click.option("--refresh", is_flag=True, help="re-resolve players already marked ok")
def main(db, players_dir, region, min_games, limit, dry_run, refresh):
    pf = sorted(glob.glob(str(Path(players_dir) / "players__*.parquet")))
    if not pf:
        raise SystemExit("no players__*.parquet — run scripts/extract_participants.py first")
    df = (
        pl.read_parquet(pf[-1])
        .filter((pl.col("games") >= min_games) & pl.col("game_name").is_not_null() & pl.col("tag_line").is_not_null())
        .sort("games", descending=True)
    )
    con = sqlite3.connect(str(db))
    con.execute(_DDL)
    done = set() if refresh else {r[0] for r in con.execute("SELECT lcu_puuid FROM player_ranks WHERE status='ok'")}
    bridge = {r[0]: r[1] for r in con.execute("SELECT lcu_puuid, public_puuid FROM riot_id_bridge WHERE public_puuid IS NOT NULL")}
    todo = [r for r in df.iter_rows(named=True) if r["puuid"] not in done][:limit]
    click.echo(f"[plan] candidates(min_games>={min_games})={df.height:,}  already_ok={len(done):,}  "
               f"bridge_known={len(bridge):,}  to_resolve={len(todo):,}")
    if dry_run:
        n_bridged = sum(1 for r in todo if r["puuid"] in bridge)
        click.echo(f"[dry-run] {n_bridged}/{len(todo)} already have a public puuid (skip account-v1). examples:")
        for r in todo[:6]:
            click.echo(f"    {r['game_name']}#{r['tag_line']}  games={r['games']}  bridge={'Y' if r['puuid'] in bridge else 'N'}")
        return

    if not os.environ.get("RIOT_API_KEY"):
        raise SystemExit("RIOT_API_KEY not set (PowerShell: $env:RIOT_API_KEY='RGAPI-...')")
    rc = RiotClient(region)
    ok = unranked = fail = 0
    with rc:
        for i, r in enumerate(todo):
            lcu, rid = r["puuid"], f"{r['game_name']}#{r['tag_line']}"
            try:
                pub = bridge.get(lcu)
                if not pub:
                    acc = rc.account_by_riot_id(r["game_name"], r["tag_line"])
                    pub = acc.get("puuid")
                    if pub:
                        con.execute(
                            "INSERT OR REPLACE INTO riot_id_bridge(public_puuid,riot_id,lcu_puuid,resolved_at,resolve_status)"
                            " VALUES(?,?,?,?,'resolved')", (pub, rid, lcu, _now()))
                if not pub:
                    con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (lcu, None, rid, None, None, None, None, None, None, r["games"], "no_account", _now()))
                    fail += 1
                    continue
                ent = {e.get("queueType"): e for e in rc.league_entries_by_puuid(pub)}
                s, f = ent.get(SOLO, {}), ent.get(FLEX, {})
                status = "ok" if (s or f) else "unranked"
                con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lcu, pub, rid, s.get("tier"), s.get("rank"), s.get("leaguePoints"),
                             f.get("tier"), f.get("rank"), f.get("leaguePoints"), r["games"], status, _now()))
                ok += status == "ok"
                unranked += status == "unranked"
            except RiotKeyExpired as e:
                con.commit()
                raise SystemExit(str(e))
            except Exception:
                con.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lcu, None, rid, None, None, None, None, None, None, r["games"], "err", _now()))
                fail += 1
            if (i + 1) % 50 == 0:
                con.commit()
                click.echo(f"  {i+1}/{len(todo)}  ranked={ok} unranked={unranked} fail={fail}")
    con.commit()
    rows = con.execute("SELECT solo_tier, COUNT(*) FROM player_ranks WHERE status='ok' AND solo_tier IS NOT NULL"
                       " GROUP BY solo_tier ORDER BY COUNT(*) DESC").fetchall()
    click.echo(f"[done] ranked={ok} unranked={unranked} fail={fail}")
    click.echo("solo-tier distribution: " + (", ".join(f"{t}:{c}" for t, c in rows) or "(none)"))


if __name__ == "__main__":
    main()
