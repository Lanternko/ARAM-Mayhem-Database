"""Read-only acceptance check for the writer rediscovery-cooldown fix (4cd99d93c).

Before the fix, a crawled player seen in a newer game was requeued with a flat
45s cooldown regardless of revisit arm, so ~40% of revisits landed within 6h of
the previous visit and almost all of them found nothing.  After it, the
treatment arm should honour its 24h minimum while control keeps the shipped
yield tiers, so the two arms finally differ.

The fix is live only once the snowball fleet (and its writer) restarted after
the commit; the cutoff is the first fleet log started after --deployed-after.

Usage:
    python scripts/check_rediscovery_cooldown.py [--min-hours 24]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lcu" / "games.db"
FLEET_LOGS = ROOT / ".codex" / "logs" / "mayhem_lcu_watchdog"
REPORT = ROOT / "data" / "monitor" / "rediscovery_cooldown_check.json"
COMMIT_AT = "2026-11-02T20:26:19+00:00"
SHORT_MS = 6 * 3600_000
# ~1k visits/h; generous enough to cover a multi-day before/after window.
VISIT_SCAN = 600_000


def _fleet_cutoff(after: datetime) -> datetime | None:
    starts = []
    for path in FLEET_LOGS.glob("snowball_fleet_*.out.log"):
        m = re.search(r"(\d{8}_\d{6})", path.name)
        if m:
            ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            if ts > after:
                starts.append(ts)
    return min(starts) if starts else None


def _window(con: sqlite3.Connection, lo: str, hi: str, min_id: int) -> dict:
    hours = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).total_seconds() / 3600
    arms = {}
    for arm, visits, short, short_new, new_games in con.execute(
        """SELECT revisit_arm, COUNT(*),
                  SUM(revisit_interval_ms < ?),
                  SUM(CASE WHEN revisit_interval_ms < ? THEN new_games_found ELSE 0 END),
                  SUM(new_games_found)
           FROM crawl_visit_events
           WHERE visit_id > ? AND visited_at >= ? AND visited_at < ?
             AND is_revisit = 1 AND source = 'match'
           GROUP BY revisit_arm""",
        (SHORT_MS, SHORT_MS, min_id, lo, hi),
    ):
        arms[arm] = {
            "revisits": visits,
            "revisits_per_hour": round(visits / hours, 1),
            "short_revisit_share": round(short / visits, 3) if visits else None,
            "short_revisit_new_per_visit": round(short_new / short, 2) if short else None,
            "new_per_revisit": round(new_games / visits, 2) if visits else None,
            "new_games_per_hour": round(new_games / hours, 1),
        }
    total_visits, total_new = con.execute(
        """SELECT COUNT(*), SUM(new_games_found) FROM crawl_visit_events
           WHERE visit_id > ? AND visited_at >= ? AND visited_at < ?""",
        (min_id, lo, hi),
    ).fetchone()
    return {
        "from": lo, "to": hi, "hours": round(hours, 1), "arms": arms,
        "visits_per_hour": round((total_visits or 0) / hours, 1),
        "new_games_per_hour": round((total_new or 0) / hours, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployed-after", default=COMMIT_AT)
    parser.add_argument("--min-hours", type=float, default=24.0,
                        help="Minimum post-deploy hours before a verdict is given.")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = _fleet_cutoff(datetime.fromisoformat(args.deployed_after))
    report: dict = {"checked_at": now.isoformat(), "commit_at": args.deployed_after}
    if cutoff is None:
        report["verdict"] = "NOT_DEPLOYED"
        report["reason"] = "no snowball fleet restart since the commit; writer still runs old code"
    else:
        report["deployed_at"] = cutoff.isoformat()
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=30)
        min_id = con.execute("SELECT MAX(visit_id) FROM crawl_visit_events").fetchone()[0] - VISIT_SCAN
        after_hours = (now - cutoff).total_seconds() / 3600
        # Same-length baseline right before deploy so weekday/patch drift is comparable.
        span = timedelta(hours=min(after_hours, 72))
        before = _window(con, (cutoff - span).isoformat(), cutoff.isoformat(), min_id)
        # Skip the first 24h: rows queued before deploy still carry the old 45s eligibility.
        settle = cutoff + timedelta(hours=min(24, after_hours / 2))
        after = _window(con, settle.isoformat(), now.isoformat(), min_id)
        con.close()
        report.update(before=before, after=after)
        t_before = before["arms"].get("treatment", {})
        t_after = after["arms"].get("treatment", {})
        c_after = after["arms"].get("control", {})
        checks = {
            "treatment_short_share_below_5pct": (t_after.get("short_revisit_share") or 1) < 0.05,
            "treatment_short_share_dropped": (t_after.get("short_revisit_share") or 1)
            < (t_before.get("short_revisit_share") or 0),
            "treatment_yield_per_revisit_above_control": (t_after.get("new_per_revisit") or 0)
            > (c_after.get("new_per_revisit") or 0),
        }
        report["checks"] = checks
        if after_hours < args.min_hours:
            report["verdict"] = "TOO_EARLY"
        else:
            report["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
