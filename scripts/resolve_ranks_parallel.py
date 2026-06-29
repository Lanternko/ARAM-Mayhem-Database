"""Parallel rank resolver: N Riot dev keys -> N threads -> ~Nx throughput.

Reads keys from a gitignored file (default data/.riot_keys, one RGAPI-... per line).
Bridge: stored riotId -> account-v1 by-riot-id -> public puuid -> league-v4 by-puuid -> tier.
Writes to a SEPARATE db (data/ratings/player_ranks.db) so it never contends with the live
collector on games.db.  Idempotent (skips status='ok').  Samples RANDOMLY among >=min-games
players (top-by-games over-selects unranked ARAM-mains).
"""
from __future__ import annotations
import glob
import sqlite3
import threading
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
@click.option("--refresh", is_flag=True)
def main(games_db, ranks_db, players_dir, keys_file, region, min_games, limit, seed, refresh):
    keys = [l.strip() for l in Path(keys_file).read_text().splitlines() if l.strip().startswith("RGAPI-")]
    if not keys:
        raise SystemExit(f"no RGAPI- keys in {keys_file}")
    click.echo(f"[keys] {len(keys)} keys")

    pf = sorted(glob.glob(str(Path(players_dir) / "players__q2400__*.parquet")))[-1]
    df = (pl.read_parquet(pf)
          .filter((pl.col("games") >= min_games) & pl.col("game_name").is_not_null() & pl.col("tag_line").is_not_null()))
    df = df.sample(n=min(limit * 2, df.height), seed=seed, shuffle=True)  # random, not top-by-games

    Path(ranks_db).parent.mkdir(parents=True, exist_ok=True)
    rc0 = sqlite3.connect(str(ranks_db))
    rc0.execute(_DDL); rc0.execute(_BRIDGE_DDL); rc0.execute("PRAGMA journal_mode=WAL"); rc0.commit()
    done = set() if refresh else {r[0] for r in rc0.execute("SELECT lcu_puuid FROM player_ranks WHERE status='ok'")}
    bridge = {r[0]: r[1] for r in rc0.execute("SELECT lcu_puuid, public_puuid FROM bridge WHERE public_puuid IS NOT NULL")}
    rc0.close()
    # seed bridge from the existing games.db riot_id_bridge (read-only, no lock contention)
    try:
        g = sqlite3.connect(f"file:{games_db}?mode=ro", uri=True)
        for lcu, pub in g.execute("SELECT lcu_puuid, public_puuid FROM riot_id_bridge WHERE public_puuid IS NOT NULL"):
            bridge.setdefault(lcu, pub)
        g.close()
    except sqlite3.OperationalError:
        pass

    todo = [r for r in df.iter_rows(named=True) if r["puuid"] not in done][:limit]
    click.echo(f"[plan] eligible(min_games>={min_games})={df.height:,}  bridge_known={len(bridge):,}  to_resolve={len(todo):,}")

    counters = {"ok": 0, "unranked": 0, "fail": 0}
    lock = threading.Lock()
    dead = []

    def worker(idx: int, key: str, jobs: list[dict]):
        wcon = sqlite3.connect(str(ranks_db), timeout=60)
        wcon.execute("PRAGMA busy_timeout=60000")
        try:
            rc = RiotClient(region, api_key=key)
        except Exception as e:
            with lock:
                dead.append((idx, str(e)[:80]))
            wcon.close()
            return
        with rc:
            for n, r in enumerate(jobs):
                lcu, rid = r["puuid"], f"{r['game_name']}#{r['tag_line']}"
                try:
                    pub = bridge.get(lcu)
                    if not pub:
                        acc = rc.account_by_riot_id(r["game_name"], r["tag_line"])
                        pub = acc.get("puuid")
                        if pub:
                            wcon.execute("INSERT OR REPLACE INTO bridge VALUES(?,?,?)", (lcu, pub, rid))
                    if not pub:
                        wcon.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (lcu, None, rid, None, None, None, None, None, None, r["games"], "no_account", _now()))
                        with lock:
                            counters["fail"] += 1
                    else:
                        ent = {e.get("queueType"): e for e in rc.league_entries_by_puuid(pub)}
                        s, f = ent.get(SOLO, {}), ent.get(FLEX, {})
                        status = "ok" if (s or f) else "unranked"
                        wcon.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (lcu, pub, rid, s.get("tier"), s.get("rank"), s.get("leaguePoints"),
                                      f.get("tier"), f.get("rank"), f.get("leaguePoints"), r["games"], status, _now()))
                        with lock:
                            counters["ok"] += status == "ok"
                            counters["unranked"] += status == "unranked"
                except RiotKeyExpired:
                    with lock:
                        dead.append((idx, "key expired/invalid"))
                    break
                except Exception as e:
                    wcon.execute("INSERT OR REPLACE INTO player_ranks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (lcu, None, rid, None, None, None, None, None, None, r["games"], "err", _now()))
                    with lock:
                        counters["fail"] += 1
                if (n + 1) % 25 == 0:
                    wcon.commit()
                    click.echo(f"  thread{idx}: {n+1}/{len(jobs)}  global ok={counters['ok']} unranked={counters['unranked']} fail={counters['fail']}")
            wcon.commit()
        wcon.close()

    threads = []
    for i, key in enumerate(keys):
        t = threading.Thread(target=worker, args=(i, key, todo[i::len(keys)]), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    if dead:
        click.echo(f"[warn] dead keys/threads: {dead}")
    con = sqlite3.connect(str(ranks_db))
    rows = con.execute("SELECT solo_tier, COUNT(*) FROM player_ranks WHERE status='ok' AND solo_tier IS NOT NULL"
                       " GROUP BY solo_tier ORDER BY COUNT(*) DESC").fetchall()
    fl = con.execute("SELECT COUNT(*) FROM player_ranks WHERE status='ok' AND solo_tier IS NULL AND flex_tier IS NOT NULL").fetchone()[0]
    tot = con.execute("SELECT COUNT(*) FROM player_ranks").fetchone()[0]
    click.echo(f"\n[done] total_resolved={tot:,}  ranked={counters['ok']}  unranked={counters['unranked']}  fail={counters['fail']}")
    click.echo(f"  solo-only-null-but-flex-ranked: {fl}")
    click.echo("  solo tiers: " + (", ".join(f"{t}:{c}" for t, c in rows) or "(none)"))


if __name__ == "__main__":
    main()
