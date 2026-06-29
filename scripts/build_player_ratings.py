"""Build per-player skill ratings (Glicko) and per-game quality scores from the
local LCU Mayhem database.

Inputs:
  data/lcu/games.db   (games.participants_private_json carries a 36-char puuid per slot)

Outputs (LOCAL ONLY — data/ is .gitignored; player_ratings contains raw puuids):
  data/ratings/player_ratings.parquet   puuid, rating, rd, games, last_ms
  data/ratings/game_quality.parquet     game_id, created_ms, n_known,
                                         avg_rating, min_rating, avg_rd,
                                         blue_avg_rating, red_avg_rating, rating_gap

Why Glicko (not raw recent win-rate, not Glicko-2):
  - Raw "last-20-games win-rate" ignores opponent strength and is +-11% noisy at 20 games.
  - Glicko pools EVERY game a puuid appears in across the whole match graph, credited by
    opponent strength, and reports an RD (uncertainty) that flags the 1-2-game long tail
    instead of pretending to know them.
  - Glicko-1 (rating + RD, no volatility term) is sufficient for *relative* strength on a
    closed graph and far faster than Glicko-2 for multi-pass over ~455k games.

Two distinct notions of "match quality" are emitted per game:
  - LEVEL   : avg_rating / min_rating   -> are these strong players? (the tier-list filter you want)
  - BALANCE : rating_gap = |blue_avg - red_avg|  -> was it a fair, close game? (TrueSkill-style)

Team adaptation: each player is scored once per game against the *aggregate* of the enemy
team (mean rating, mean RD); outcome = did their team win.  One game = one result (NOT five),
so a single stomp cannot fake five games' worth of confidence.

Caveat: this is RELATIVE strength within the players you captured, not Riot's absolute MMR.
That is exactly right for "filter the higher-level games" and wrong for absolute claims.

Example:
  python scripts/build_player_ratings.py --queue 2400 --passes 3 --validate
  python scripts/build_player_ratings.py --queue 2400 --patch-prefix 16.13
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

import click
import numpy as np
import polars as pl

# --- Glicko-1 constants (Glickman's standard scale) -------------------------------------
INITIAL_RATING = 1500.0
INITIAL_RD = 350.0
MIN_RD = 30.0
MAX_RD = 350.0
Q = math.log(10.0) / 400.0
MS_PER_DAY = 86_400_000


def _g(rd: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * Q * Q * rd * rd / (math.pi * math.pi))


def _expected(r: float, r_opp: float, rd_opp: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-_g(rd_opp) * (r - r_opp) / 400.0))


# --- Data loading -----------------------------------------------------------------------
def _cache_path(out_dir: Path, db: Path, queue: int, patch_prefix: str) -> Path:
    mtime = int(db.stat().st_mtime)
    tag = patch_prefix.replace(".", "_") or "all"
    return out_dir / f"_games_min__q{queue}__{tag}__{mtime}.parquet"


def load_games(
    db: Path, queue: int, patch_prefix: str, out_dir: Path, rebuild_cache: bool, limit: int
) -> pl.DataFrame:
    """Return one row per puuid-bearing game: game_id, created_ms, blue_wins, blue_ids, red_ids."""
    cache = _cache_path(out_dir, db, queue, patch_prefix)
    if cache.exists() and not rebuild_cache and not limit:
        click.echo(f"[load] cache hit {cache.name}")
        return pl.read_parquet(cache)

    con = sqlite3.connect(str(db))
    sql = "SELECT game_id, created_ms, blue_wins, participants_private_json FROM games WHERE queue_id=?"
    params: list = [queue]
    if patch_prefix:
        sql += " AND patch LIKE ?"
        params.append(patch_prefix + "%")
    sql += " ORDER BY created_ms ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = con.execute(sql, params)

    gid_col: list[str] = []
    ms_col: list[int] = []
    bw_col: list[int] = []
    blue_col: list[list[str]] = []
    red_col: list[list[str]] = []
    seen = parsed = 0
    t0 = time.time()
    while True:
        rows = cur.fetchmany(20_000)
        if not rows:
            break
        for game_id, created_ms, blue_wins, pj in rows:
            seen += 1
            if not pj:
                continue
            try:
                parts = json.loads(pj)
            except (ValueError, TypeError):
                continue
            blue: list[str] = []
            red: list[str] = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                puuid = p.get("puuid")
                if not puuid:
                    continue
                team = p.get("teamId")
                if team == 100:
                    blue.append(puuid)
                elif team == 200:
                    red.append(puuid)
            if not blue and not red:
                continue
            gid_col.append(game_id)
            ms_col.append(int(created_ms or 0))
            bw_col.append(int(blue_wins or 0))
            blue_col.append(blue)
            red_col.append(red)
            parsed += 1
        if seen % 200_000 == 0:
            click.echo(f"[load] scanned {seen:,}  puuid-bearing {parsed:,}  ({time.time()-t0:.0f}s)")
    con.close()

    df = pl.DataFrame(
        {
            "game_id": gid_col,
            "created_ms": ms_col,
            "blue_wins": bw_col,
            "blue_ids": blue_col,
            "red_ids": red_col,
        }
    )
    click.echo(f"[load] {seen:,} games scanned, {df.height:,} carry puuid ({time.time()-t0:.0f}s)")
    if not limit:
        out_dir.mkdir(parents=True, exist_ok=True)
        df.write_parquet(cache)
        click.echo(f"[load] cached -> {cache.name}")
    return df


# --- Glicko forward filter --------------------------------------------------------------
class _P:
    __slots__ = ("r", "rd", "last_period", "games")

    def __init__(self, r: float, rd: float) -> None:
        self.r = r
        self.rd = rd
        self.last_period = None  # period index of last activity (for RD inflation)
        self.games = 0


def glicko_pass(
    games: list[tuple[int, int, list[int], list[int]]],
    n_players: int,
    period_days: int,
    inflate_per_day: float,
    init_r: np.ndarray | None,
    reseed_rd: float,
) -> list[_P]:
    """One forward pass over time-sorted games. games = (period_idx, blue_wins, blue_ids, red_ids).

    Glicko batch semantics: all games inside one rating period are treated as simultaneous —
    opponents are read at the period-start snapshot, every player's update is applied at period end.
    """
    if init_r is None:
        players = [_P(INITIAL_RATING, INITIAL_RD) for _ in range(n_players)]
    else:
        players = [_P(float(init_r[i]), reseed_rd) for i in range(n_players)]

    def flush(buf: list[tuple[int, list[int], list[int]]], period: int) -> None:
        # 1) inflate RD once for everyone participating this period (lazy time-decay)
        touched: set[int] = set()
        for _bw, blue, red in buf:
            touched.update(blue)
            touched.update(red)
        for pid in touched:
            p = players[pid]
            if p.last_period is not None and inflate_per_day > 0:
                gap = (period - p.last_period) * period_days
                if gap > 0:
                    p.rd = min(math.sqrt(p.rd * p.rd + inflate_per_day * inflate_per_day * gap), MAX_RD)
        # 2) accumulate results against the enemy-team aggregate (read pre-update snapshot)
        acc: dict[int, list[float]] = {pid: [0.0, 0.0] for pid in touched}  # [sum g*(s-E), sum g^2 E(1-E)]
        for bw, blue, red in buf:
            if not blue or not red:
                continue
            blue_r = sum(players[i].r for i in blue) / len(blue)
            blue_rd = sum(players[i].rd for i in blue) / len(blue)
            red_r = sum(players[i].r for i in red) / len(red)
            red_rd = sum(players[i].rd for i in red) / len(red)
            for side, opp_r, opp_rd, s in (
                (blue, red_r, red_rd, float(bw)),
                (red, blue_r, blue_rd, float(1 - bw)),
            ):
                g_opp = _g(opp_rd)
                for pid in side:
                    e = _expected(players[pid].r, opp_r, opp_rd)
                    a = acc[pid]
                    a[0] += g_opp * (s - e)
                    a[1] += g_opp * g_opp * e * (1.0 - e)
                    players[pid].games += 1
        # 3) apply updates
        for pid, (sum_gse, sum_info) in acc.items():
            p = players[pid]
            if sum_info <= 0:
                p.last_period = period
                continue
            d2_inv = Q * Q * sum_info
            denom = 1.0 / (p.rd * p.rd) + d2_inv
            p.r = p.r + (Q / denom) * sum_gse
            p.rd = min(max(math.sqrt(1.0 / denom), MIN_RD), MAX_RD)
            p.last_period = period

    buf: list[tuple[int, list[int], list[int]]] = []
    cur_period = None
    for period, bw, blue, red in games:
        if cur_period is None:
            cur_period = period
        if period != cur_period:
            flush(buf, cur_period)
            buf = []
            cur_period = period
        buf.append((bw, blue, red))
    if buf:
        flush(buf, cur_period)
    return players


def _percentiles(values: np.ndarray, qs: list[int]) -> dict[int, float]:
    if values.size == 0:
        return {q: float("nan") for q in qs}
    return {q: float(np.percentile(values, q)) for q in qs}


# --- CLI --------------------------------------------------------------------------------
@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True, path_type=Path))
@click.option("--queue", default=2400, type=int, help="queueId (2400 = Mayhem).")
@click.option("--patch-prefix", default="", help="e.g. 16.13 to restrict to one patch; empty = all.")
@click.option("--passes", default=3, type=int, help="Forward passes; >1 propagates strength backward through the graph.")
@click.option("--period-days", default=1, type=int, help="Rating-period length in days.")
@click.option("--reseed-rd", default=200.0, type=float, help="RD each player restarts with on passes 2+.")
@click.option("--rd-inflate-per-day", default=10.0, type=float, help="RD growth per idle day (confidence decay).")
@click.option("--out-dir", default="data/ratings", type=click.Path(path_type=Path))
@click.option("--validate", is_flag=True, help="Split-half reliability check (2 extra half-data passes).")
@click.option("--rebuild-cache", is_flag=True)
@click.option("--limit", default=0, type=int, help="Debug: cap number of games loaded.")
def main(
    db: Path,
    queue: int,
    patch_prefix: str,
    passes: int,
    period_days: int,
    reseed_rd: float,
    rd_inflate_per_day: float,
    out_dir: Path,
    validate: bool,
    rebuild_cache: bool,
    limit: int,
) -> None:
    out_dir = Path(out_dir)
    df = load_games(db, queue, patch_prefix, out_dir, rebuild_cache, limit)
    if df.height == 0:
        click.echo("[abort] no puuid-bearing games matched.")
        return

    # Intern puuids -> contiguous int ids.
    id_of: dict[str, int] = {}

    def intern(lst: list[str]) -> list[int]:
        out = []
        for u in lst:
            i = id_of.get(u)
            if i is None:
                i = len(id_of)
                id_of[u] = i
            out.append(i)
        return out

    ms = df["created_ms"].to_list()
    bw = df["blue_wins"].to_list()
    blue_raw = df["blue_ids"].to_list()
    red_raw = df["red_ids"].to_list()

    games: list[tuple[int, int, list[int], list[int]]] = []
    for i in range(df.height):
        period = (ms[i] // MS_PER_DAY) // period_days
        games.append((period, bw[i], intern(blue_raw[i]), intern(red_raw[i])))
    n_players = len(id_of)
    click.echo(f"[ids] {n_players:,} unique players across {len(games):,} games")

    # --- multi-pass Glicko ---
    init_r = None
    prev_r = None
    players: list[_P] = []
    for p in range(passes):
        t0 = time.time()
        players = glicko_pass(games, n_players, period_days, rd_inflate_per_day, init_r, reseed_rd)
        cur_r = np.array([pl_.r for pl_ in players])
        shift = "" if prev_r is None else f"  mean|dR|={np.abs(cur_r - prev_r).mean():.2f}"
        click.echo(f"[pass {p+1}/{passes}] {time.time()-t0:.1f}s{shift}")
        prev_r = cur_r
        init_r = cur_r  # next pass starts from learned means

    ratings = np.array([p.r for p in players])
    rds = np.array([p.rd for p in players])
    gcount = np.array([p.games for p in players])

    # --- report: does the 1-2-game worry actually bite? ---
    click.echo("\n=== player coverage ===")
    gp = _percentiles(gcount, [50, 75, 90, 99])
    click.echo(f"games/player  median={gp[50]:.0f}  p75={gp[75]:.0f}  p90={gp[90]:.0f}  p99={gp[99]:.0f}  max={gcount.max()}")
    for k in (1, 3, 5, 10, 20):
        click.echo(f"  players with >= {k:>2} games: {int((gcount >= k).sum()):>8,}  ({100*(gcount>=k).mean():.1f}%)")
    rp = _percentiles(ratings, [1, 10, 50, 90, 99])
    click.echo(f"rating spread  p1={rp[1]:.0f}  p10={rp[10]:.0f}  p50={rp[50]:.0f}  p90={rp[90]:.0f}  p99={rp[99]:.0f}")

    # --- split-half reliability (optional) ---
    if validate:
        even = [g for i, g in enumerate(games) if i % 2 == 0]
        odd = [g for i, g in enumerate(games) if i % 2 == 1]
        pa = glicko_pass(even, n_players, period_days, rd_inflate_per_day, None, reseed_rd)
        pb = glicko_pass(odd, n_players, period_days, rd_inflate_per_day, None, reseed_rd)
        ra = np.array([p.r for p in pa])
        rb = np.array([p.r for p in pb])
        ga = np.array([p.games for p in pa])
        gb = np.array([p.games for p in pb])
        for kmin in (5, 10):
            mask = (ga >= kmin) & (gb >= kmin)
            n = int(mask.sum())
            if n >= 2:
                corr = float(np.corrcoef(ra[mask], rb[mask])[0, 1])
                click.echo(f"[validate] split-half rating corr (>= {kmin} games each half): r={corr:.3f}  n={n:,}")
            else:
                click.echo(f"[validate] not enough players with >= {kmin} games in both halves")

    # --- per-game quality using FINAL ratings ---
    rows = []
    for i in range(df.height):
        blue = intern_lookup(blue_raw[i], id_of)
        red = intern_lookup(red_raw[i], id_of)
        known = blue + red
        if not known:
            continue
        kr = ratings[known]
        b_avg = float(ratings[blue].mean()) if blue else None
        r_avg = float(ratings[red].mean()) if red else None
        gap = abs(b_avg - r_avg) if (b_avg is not None and r_avg is not None) else None
        rows.append(
            {
                "game_id": df["game_id"][i],
                "created_ms": ms[i],
                "n_known": len(known),
                "avg_rating": float(kr.mean()),
                "min_rating": float(kr.min()),
                "avg_rd": float(rds[known].mean()),
                "blue_avg_rating": b_avg,
                "red_avg_rating": r_avg,
                "rating_gap": gap,
            }
        )
    gq = pl.DataFrame(rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    pr = pl.DataFrame(
        {
            "puuid": list(id_of.keys()),
            "rating": ratings.tolist(),
            "rd": rds.tolist(),
            "games": gcount.tolist(),
        }
    ).sort("rating", descending=True)
    pr.write_parquet(out_dir / "player_ratings.parquet")
    gq.write_parquet(out_dir / "game_quality.parquet")

    # --- show the LEVEL filter knob the user actually wanted ---
    click.echo("\n=== game LEVEL filter preview (avg_rating, well-known games) ===")
    av = gq["avg_rating"].to_numpy()
    nk = gq["n_known"].to_numpy()
    qa = _percentiles(av, [50, 75, 90, 95])
    click.echo(f"avg_rating  p50={qa[50]:.0f}  p75={qa[75]:.0f}  p90={qa[90]:.0f}  p95={qa[95]:.0f}")
    for thr in (qa[50], qa[75], qa[90]):
        keep = int(((av >= thr) & (nk >= 8)).sum())
        click.echo(f"  avg_rating>={thr:.0f} & n_known>=8 : {keep:,} games ({100*keep/gq.height:.1f}%)")

    click.echo(f"\n[done] {out_dir/'player_ratings.parquet'}  ({pr.height:,} players)")
    click.echo(f"[done] {out_dir/'game_quality.parquet'}  ({gq.height:,} games scored)")


def intern_lookup(lst: list[str], id_of: dict[str, int]) -> list[int]:
    return [id_of[u] for u in lst if u in id_of]


if __name__ == "__main__":
    main()
