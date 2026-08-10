"""Report unique-game yield by the actual time between crawler visits.

The source table is prospective telemetry: crawl_visit_events only contains
visits completed after the table was introduced. It deliberately reports
globally new games, not merely target games visible in a player's history.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "lcu" / "games.db"


@dataclass(frozen=True)
class Bucket:
    label: str
    low_ms: int
    high_ms: int | None


HOUR = 3_600_000
DAY = 24 * HOUR
BUCKETS = (
    Bucket("<6h", 0, 6 * HOUR),
    Bucket("6–24h", 6 * HOUR, DAY),
    Bucket("1–3d", DAY, 3 * DAY),
    Bucket("3–7d", 3 * DAY, 7 * DAY),
    Bucket("7–21d", 7 * DAY, 21 * DAY),
    Bucket(">21d", 21 * DAY, None),
)


def _bucket(interval_ms: int) -> str:
    for bucket in BUCKETS:
        if interval_ms >= bucket.low_ms and (
            bucket.high_ms is None or interval_ms < bucket.high_ms
        ):
            return bucket.label
    raise AssertionError(interval_ms)


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    weight = pos - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def _print_group(label: str, values: list[int], targets: list[int]) -> None:
    count = len(values)
    total = sum(values)
    avg = total / count if count else 0.0
    zero = sum(value == 0 for value in values) / count if count else 0.0
    at_15 = sum(value >= 15 for value in values) / count if count else 0.0
    at_20 = sum(value >= 20 for value in values) / count if count else 0.0
    visible = sum(targets) / count if count else 0.0
    print(
        f"{label:<18}{count:>9,}{avg:>10.2f}{median(values) if values else 0:>9.1f}"
        f"{_percentile(values, 0.90):>9.1f}{zero:>9.1%}{at_15:>9.1%}"
        f"{at_20:>9.1%}{visible:>11.2f}"
    )


def report(db: Path, since_hours: float) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    con.execute("PRAGMA query_only=ON")
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawl_visit_events'"
    ).fetchone()
    if not exists:
        print("尚未建立 crawl_visit_events；請先重啟新版 crawler worker。")
        return 1

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    rollout = con.execute(
        "SELECT state_value FROM crawl_runtime_state "
        "WHERE state_key = 'revisit_ab_v2_started_at'"
    ).fetchone()
    if rollout:
        try:
            rollout_dt = datetime.fromisoformat(str(rollout[0]))
            cutoff_dt = max(cutoff_dt, rollout_dt)
        except ValueError:
            pass
    cutoff = cutoff_dt.isoformat()
    rows = con.execute(
        """
        SELECT revisit_arm, revisit_interval_ms, new_games_found,
               target_game_count
        FROM crawl_visit_events
        WHERE is_revisit = 1
          AND revisit_interval_ms IS NOT NULL
          AND visited_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    con.close()

    print(
        f"逐次 revisit 產出（有效起點 {cutoff_dt.isoformat()}，共 {len(rows):,} 次）"
    )
    print("產出＝這次造訪實際加入資料庫的全域唯一新場次。")
    print(
        f"{'arm / 實際間隔':<18}{'次數':>9}{'平均新增':>10}{'中位數':>9}"
        f"{'P90':>9}{'零新增':>9}{'>=15':>9}{'>=20':>9}{'平均可見':>11}"
    )
    if not rows:
        print("尚無新版 telemetry；等待 worker 完成 revisit 後再執行。")
        return 0

    for arm in ("control", "treatment"):
        arm_rows = [row for row in rows if row[0] == arm]
        _print_group(
            arm,
            [int(row[2]) for row in arm_rows],
            [int(row[3]) for row in arm_rows],
        )
        for bucket in BUCKETS:
            grouped = [row for row in arm_rows if _bucket(int(row[1])) == bucket.label]
            if grouped:
                _print_group(
                    f"  {bucket.label}",
                    [int(row[2]) for row in grouped],
                    [int(row[3]) for row in grouped],
                )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-hours", type=float, default=24 * 7)
    args = parser.parse_args()
    return report(args.db.resolve(), max(0.0, args.since_hours))


if __name__ == "__main__":
    raise SystemExit(main())
