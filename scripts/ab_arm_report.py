"""Per-player A/B snapshot + arm report for the crawler experiments.

crawl_seen only carries lifetime counters, so any arm comparison has to diff
against a snapshot taken at rollout. The earlier revisit baseline stored arm
totals only, which gives a point estimate but no confidence interval -- with
totals there is nothing left to resample. This keeps one row per player, so the
report can cluster-bootstrap over players and say whether a gap is real.

    python scripts/ab_arm_report.py snapshot                 # take the baseline
    python scripts/ab_arm_report.py report --split history   # 3-arm classifier
    python scripts/ab_arm_report.py report --split revisit   # 2-arm interval

The snapshot serves both splits: arms are derived from the puuid at report
time, so one snapshot covers every experiment that hashes the same players.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.lcu.snowball import history_arm, revisit_arm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lcu" / "games.db"
SNAP = ROOT / "data" / "monitor" / "ab_player_baseline.sqlite"

SPLITS = {
    "revisit": (revisit_arm, ("control", "treatment")),
    "history": (history_arm, ("control", "probe", "full")),
}


def _read_only(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    con.execute("pragma query_only=on")
    return con


def take_snapshot(force: bool) -> int:
    if SNAP.exists() and not force:
        con = _read_only(SNAP)
        taken_at, rows = con.execute("SELECT taken_at, rows FROM meta").fetchone()
        con.close()
        print(f"快照已存在：{taken_at}（{rows:,} 位玩家）")
        print("覆寫等於丟掉整個實驗的比較基準；真要重拍加 --force。")
        return 1

    src = _read_only(DB)
    # Per-queue counters only start accruing once migrated workers run. A snapshot
    # taken before that gives the two windows different t0s, which silently
    # deflates every per-queue rate; record it so the report can say so.
    queue_col = any(
        r[1] == "new_games_by_queue_json" for r in src.execute("PRAGMA table_info(crawl_seen)")
    )
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAP.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    dst.execute("pragma journal_mode=off")
    dst.execute(
        "CREATE TABLE baseline ("
        "puuid TEXT PRIMARY KEY, process_count INTEGER, new_games_found INTEGER)"
    )
    dst.execute(
        "CREATE TABLE meta ("
        "taken_at TEXT, source_db TEXT, rows INTEGER, queue_col_present INTEGER)"
    )

    rows = 0
    batch: list[tuple[str, int, int]] = []
    for puuid, pc, ng in src.execute(
        "SELECT puuid, process_count, new_games_found FROM crawl_seen"
    ):
        batch.append((puuid, pc or 0, ng or 0))
        if len(batch) >= 20_000:
            dst.executemany("INSERT INTO baseline VALUES (?,?,?)", batch)
            rows += len(batch)
            batch.clear()
    if batch:
        dst.executemany("INSERT INTO baseline VALUES (?,?,?)", batch)
        rows += len(batch)
    src.close()

    dst.execute("INSERT INTO meta VALUES (?,?,?,?)", (taken_at, str(DB), rows, int(queue_col)))
    dst.commit()
    dst.close()
    tmp.replace(SNAP)
    print(f"快照完成：{rows:,} 位玩家 @ {taken_at}")
    if not queue_col:
        print("警告：crawl_seen 還沒有 per-queue 欄位，這份快照的 per-queue 比率會偏低。")
    print(f"寫到 {SNAP.relative_to(ROOT)}（含 puuid，data/ 已 gitignore，不要外流）")
    return 0


def _load_deltas() -> tuple[str, dict[str, np.ndarray], list[dict[str, int]]]:
    """Per-player (visits, games) accumulated since the snapshot.

    Per-queue counts need no baseline: the column starts NULL for every player
    and is only written by post-migration workers, so whatever is in there is by
    construction the experiment window.
    """
    snap = _read_only(SNAP)
    meta_cols = [r[1] for r in snap.execute("PRAGMA table_info(meta)")]
    taken_at = snap.execute("SELECT taken_at FROM meta").fetchone()[0]
    aligned = (
        bool(snap.execute("SELECT queue_col_present FROM meta").fetchone()[0])
        if "queue_col_present" in meta_cols
        else False
    )
    base = {pu: (pc, ng) for pu, pc, ng in snap.execute("SELECT * FROM baseline")}
    snap.close()

    con = _read_only(DB)
    has_queue_col = any(
        r[1] == "new_games_by_queue_json" for r in con.execute("PRAGMA table_info(crawl_seen)")
    )
    cols = "puuid, process_count, new_games_found"
    cols += ", new_games_by_queue_json" if has_queue_col else ", NULL"
    puuids: list[str] = []
    dv: list[int] = []
    dg: list[int] = []
    by_queue: list[dict[str, int]] = []
    rewound = 0
    for puuid, pc, ng, qjson in con.execute(f"SELECT {cols} FROM crawl_seen"):
        b_pc, b_ng = base.get(puuid, (0, 0))
        d_v = (pc or 0) - b_pc
        d_g = (ng or 0) - b_ng
        if d_v < 0 or d_g < 0:
            rewound += 1
            continue
        if d_v == 0:
            continue
        puuids.append(puuid)
        dv.append(d_v)
        dg.append(d_g)
        by_queue.append(json.loads(qjson) if qjson else {})
    con.close()
    if rewound:
        print(f"注意：{rewound:,} 位玩家計數比快照低（DB 被重置或合併過），已排除。\n")
    if not has_queue_col:
        print("注意：crawl_seen 還沒有 new_games_by_queue_json 欄位，跳過 per-queue 歸因。\n")
    elif not aligned:
        print(
            "注意：快照拍攝時 per-queue 欄位還不存在，per-queue 計數的起點晚於造訪起點，"
            "\n下面的每千次造訪產出是低估值，只能比 arm 之間的相對大小。\n"
        )
    return (
        taken_at,
        {
            "puuid": np.array(puuids, dtype=object),
            "visits": np.array(dv, dtype=np.int64),
            "games": np.array(dg, dtype=np.int64),
        },
        by_queue,
    )


def _bootstrap_ratio(
    visits: np.ndarray, games: np.ndarray, iters: int, rng: np.random.Generator
) -> np.ndarray:
    """Cluster bootstrap over players of the ratio-of-totals games/visit.

    Resampling players (not visits) is the point: one player's visits share a
    cooldown schedule and a history, so treating them as independent draws
    would understate the spread.
    """
    n = visits.size
    idx = rng.integers(0, n, size=(iters, n))
    return games[idx].sum(axis=1) / np.maximum(visits[idx].sum(axis=1), 1)


QUEUE_LABEL = {"2400": "Mayhem", "450": "ARAM", "2450": "大混戰經典風", "4310": "經典"}


def _print_queue_table(
    labels: np.ndarray, arms: tuple[str, ...], by_queue: list[dict[str, int]], visits: np.ndarray
) -> None:
    """Per-arm yield split by queue -- what the aggregate number hides."""
    queues = sorted({q for row in by_queue for q in row}, key=lambda q: -sum(
        row.get(q, 0) for row in by_queue
    ))
    if not queues:
        print("\n（尚無 per-queue 資料：worker 還沒帶新欄位重啟，或窗內沒有新場。）")
        return
    print("\n每千次造訪的產出，依 queue 拆分")
    head = "".join(f"{QUEUE_LABEL.get(q, q):>14}" for q in queues)
    print(f"{'arm':<11}{head}")
    for arm in arms:
        m = labels == arm
        if not m.any():
            continue
        v = max(int(visits[m].sum()), 1)
        rows = [by_queue[i] for i in np.flatnonzero(m)]
        cells = "".join(
            f"{sum(r.get(q, 0) for r in rows) / v * 1000:>14.1f}" for q in queues
        )
        print(f"  {arm:<9}{cells}")


def report(split: str, iters: int, seed: int) -> int:
    if not SNAP.exists():
        print(f"沒有快照：{SNAP}。先跑 `python scripts/ab_arm_report.py snapshot`。")
        return 1
    arm_of, arms = SPLITS[split]
    taken_at, d, by_queue = _load_deltas()
    started = dt.datetime.fromisoformat(taken_at)
    hours = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 3600
    print(f"分流 {split}　快照 {taken_at}　已進行 {hours:.1f} 小時\n")

    labels = np.array([arm_of(p) for p in d["puuid"]])
    rng = np.random.default_rng(seed)
    stat: dict[str, dict[str, float]] = {}
    boot: dict[str, np.ndarray] = {}

    print(f"{'arm':<11}{'活躍玩家':>10}{'造訪':>10}{'產出':>10}{'每訪產出':>12}{'零產出訪':>10}")
    for arm in arms:
        m = labels == arm
        v, g = d["visits"][m], d["games"][m]
        if v.size == 0:
            print(f"  {arm:<9}{'—':>10}")
            continue
        per = g.sum() / max(v.sum(), 1)
        zero = float((g == 0).mean())
        stat[arm] = {"players": v.size, "visits": int(v.sum()), "games": int(g.sum()),
                     "per": per, "zero": zero}
        boot[arm] = _bootstrap_ratio(v, g, iters, rng)
        print(f"  {arm:<9}{v.size:>10,}{v.sum():>10,}{g.sum():>10,}{per:>12.3f}{zero:>9.1%}")

    if not stat:
        print("\n窗內沒有任何造訪，crawler 沒在跑？")
        return 0
    thin = [a for a, s in stat.items() if s["visits"] < 500]
    if thin:
        _print_queue_table(labels, arms, by_queue, d["visits"])
        print(f"\n樣本仍太少（{', '.join(thin)} 造訪 < 500），先讓它多跑一陣子再看。")
        return 0

    print(f"\n每訪產出 95% CI（cluster bootstrap over players, {iters:,} 次）")
    for arm in stat:
        lo, hi = np.percentile(boot[arm], [2.5, 97.5])
        print(f"  {arm:<11}{stat[arm]['per']:.3f}  [{lo:.3f}, {hi:.3f}]")

    ref = arms[0]
    print(f"\n對照 {ref} 的差值（>0 = 該 arm 更好，CI 不跨 0 才算有結論）")
    for arm in list(stat)[1:]:
        diff = boot[arm] - boot[ref]
        lo, hi = np.percentile(diff, [2.5, 97.5])
        point = stat[arm]["per"] - stat[ref]["per"]
        verdict = "顯著" if lo > 0 or hi < 0 else "尚無結論"
        print(f"  {arm:<11}{point:+.3f}  [{lo:+.3f}, {hi:+.3f}]  {verdict}")

    _print_queue_table(labels, arms, by_queue, d["visits"])

    if split == "history":
        print(
            "\n零產出訪：control 會整個跳過沒有 Mayhem 的玩家，probe / full 讓他們可見，"
            "\n所以零產出比例應該下降。per-queue 那張表才看得到錢花在哪個模式上 --"
            "\nfull 比 probe 多付一次 20 場展開，要換到的就是那些非 2400 的量。"
        )
    else:
        print(
            "\n注意：treatment 刻意拉長間隔，造訪數本來就會較少。"
            "\n要看的是每訪產出是否提升，以及總產出有沒有因此下降。"
        )
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="拍 per-player baseline")
    s.add_argument("--force", action="store_true", help="覆寫現有快照（會丟掉比較基準）")
    r = sub.add_parser("report", help="讀快照算 arm 差異")
    r.add_argument("--split", choices=sorted(SPLITS), default="history")
    r.add_argument("--iters", type=int, default=2000)
    r.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        return take_snapshot(a.force)
    return report(a.split, a.iters, a.seed)


if __name__ == "__main__":
    raise SystemExit(main())
