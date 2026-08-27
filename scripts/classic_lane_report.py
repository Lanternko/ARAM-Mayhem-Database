"""Report the Classic-lane ordering A/B from the per-visit event log.

Unlike the revisit / history experiments, this one needs no baseline snapshot:
crawl_visit_events already stores one row per visit with the per-queue counts
that visit produced, so the arms can be diffed directly and bootstrapped over
players.

    python scripts/classic_lane_report.py
    python scripts/classic_lane_report.py --since 2026-08-27T00:00:00+00:00

Only the reserved slots ('classic_score' / 'classic_due') are compared. Fallback
claims are excluded: they fire when the general frontier is empty, which is not
a state both arms see equally, so including them would confound capacity with
ordering.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

# The Windows console here is cp950; the report is written in Chinese.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lcu" / "games.db"
CLASSIC_QUEUE = "4310"
ARMS = ("due", "score")
LANES = {"due": "classic_due", "score": "classic_score"}


def _read_only(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    con.execute("pragma query_only=on")
    return con


def _load(con: sqlite3.Connection, since: str | None):
    sql = (
        "SELECT puuid, claim_lane, new_games_by_queue_json, new_games_found, visited_at "
        "FROM crawl_visit_events WHERE claim_lane IN (?, ?)"
    )
    params: list[object] = [LANES["due"], LANES["score"]]
    if since:
        sql += " AND visited_at >= ?"
        params.append(since)
    per_player: dict[str, dict[str, list[tuple[int, int]]]] = collections.defaultdict(
        lambda: {arm: [] for arm in ARMS}
    )
    span = [None, None]
    for puuid, lane, queue_json, total, visited_at in con.execute(sql, params):
        arm = "score" if lane == LANES["score"] else "due"
        classic = 0
        if queue_json:
            try:
                classic = int(json.loads(queue_json).get(CLASSIC_QUEUE, 0))
            except (ValueError, TypeError):
                classic = 0
        per_player[puuid][arm].append((classic, int(total or 0)))
        span[0] = visited_at if span[0] is None else min(span[0], visited_at)
        span[1] = visited_at if span[1] is None else max(span[1], visited_at)
    return per_player, span


def _rate(players: list[dict[str, list[tuple[int, int]]]], arm: str) -> float:
    classic = sum(row[0] for p in players for row in p[arm])
    visits = sum(len(p[arm]) for p in players)
    return 1000.0 * classic / visits if visits else 0.0


def _bootstrap(players, arm: str, draws: int, rng) -> tuple[float, float]:
    if not players:
        return (0.0, 0.0)
    index = np.arange(len(players))
    samples = []
    for _ in range(draws):
        pick = [players[i] for i in rng.choice(index, size=len(index), replace=True)]
        samples.append(_rate(pick, arm))
    return float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--since", default=None, help="ISO timestamp lower bound")
    parser.add_argument("--draws", type=int, default=2000)
    args = parser.parse_args()

    con = _read_only(args.db)
    per_player, span = _load(con, args.since)
    con.close()
    players = list(per_player.values())
    if not players:
        print("尚無 classic_score / classic_due 造訪紀錄。")
        print("這兩個 lane 只在保留配額的 slot 上出現，worker 要跑過才有資料。")
        return 1

    print(f"視窗 {span[0]} → {span[1]}")
    print()
    print(f"{'arm':10}{'玩家':>9}{'造訪':>9}{'經典場':>9}{'每千訪經典':>12}{'每訪總產出':>12}")
    for arm in ARMS:
        rows = [row for p in players for row in p[arm]]
        n_players = sum(1 for p in players if p[arm])
        visits = len(rows)
        classic = sum(r[0] for r in rows)
        total = sum(r[1] for r in rows)
        per_k = 1000.0 * classic / visits if visits else 0.0
        per_v = total / visits if visits else 0.0
        print(f"{arm:10}{n_players:>9,}{visits:>9,}{classic:>9,}{per_k:>12.1f}{per_v:>12.3f}")

    rng = np.random.default_rng(20260827)
    # Cluster over players: one player can be claimed many times, so visits are
    # not independent draws.
    both = [p for p in players if p["due"] and p["score"]]
    print()
    print(f"每千訪經典 95% CI（cluster bootstrap over players, {args.draws:,} 次）")
    lo_hi = {}
    for arm in ARMS:
        arm_players = [p for p in players if p[arm]]
        lo, hi = _bootstrap(arm_players, arm, args.draws, rng)
        lo_hi[arm] = (lo, hi)
        print(f"  {arm:8}{_rate(arm_players, arm):>9.1f}  [{lo:.1f}, {hi:.1f}]")

    diffs = []
    all_players = [p for p in players if p["due"] or p["score"]]
    index = np.arange(len(all_players))
    for _ in range(args.draws):
        pick = [all_players[i] for i in rng.choice(index, size=len(index), replace=True)]
        diffs.append(_rate([p for p in pick if p["score"]], "score")
                     - _rate([p for p in pick if p["due"]], "due"))
    lo, hi = float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))
    delta = _rate([p for p in players if p["score"]], "score") - _rate(
        [p for p in players if p["due"]], "due"
    )
    verdict = "score 勝" if lo > 0 else ("due 勝" if hi < 0 else "尚無結論")
    print()
    print(f"score - due：{delta:+.1f} 每千訪經典  [{lo:+.1f}, {hi:+.1f}]  {verdict}")
    print()
    print("兩臂玩家池不相交（lane_arm 是 puuid 的穩定 hash），所以這是玩家層級的")
    print("平行比較，不是同一批人的前後對照。經典場數以外的產出照樣入庫，")
    print("排序只決定先看誰。")
    if both:
        print(f"注意：{len(both):,} 位玩家在兩臂都有紀錄，代表 lane_arm 不穩定，要查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
