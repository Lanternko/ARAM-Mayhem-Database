"""One-command Mayhem data collector.

This is the user-facing convenience wrapper around the newer
`scripts/lcu_collector.py` stack. It runs the tuned `snowball-workers`
strategy, waits for the frontier to drain, then exports a parquet file.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


DB_PATH = Path("data/lcu/games.db")


def _frontier_active() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(str(DB_PATH))
        row = con.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status IN ('pending', 'in_progress')"
        ).fetchone()
        con.close()
        return (row[0] if row else 0) > 0
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Mayhem game data via LCU and export to parquet.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel crawl workers (default: 4)")
    parser.add_argument("--out", default="my_games.parquet", help="Output parquet filename (default: my_games.parquet)")
    parser.add_argument("--platform", default="", help="Your server tag, e.g. TW2, KR, EUW1 (optional, metadata only)")
    parser.add_argument("--seed-file", default="data/seeds/opgg_tw.txt", help="Optional Riot ID seed file")
    args = parser.parse_args()

    out = Path(args.out)
    seed_file = Path(args.seed_file)
    platform_args = ["--platform", args.platform] if args.platform else []

    print(f"[collect] Starting {args.workers}-worker snowball crawl. Make sure League client is open.")
    print(f"[collect] Output will be saved to: {out}")
    if seed_file.exists():
        print(f"[collect] Using seed file: {seed_file}")
    else:
        print(f"[collect] Seed file not found -> fallback to self/friends only: {seed_file}")
    print()

    cmd = [
        sys.executable, "lcu_collector.py", "snowball-workers",
        "--workers", str(args.workers),
        "--target-games", "50000",
        "--max-players", "50000",
        "--games-per-player", "4",
        "--max-depth", "3",
        "--manual-seed-pending-cap", "40",
        "--log-dir", ".codex\\logs\\live_prefilter_cap40_main",
        "--seed-self", "--seed-friends",
        "--no-seed-ladder", "--no-seed-apex", "--no-seed-riot-tier",
    ]
    if seed_file.exists():
        cmd += ["--seed-riot-id-file", str(seed_file)]

    subprocess.run(cmd, check=True)

    print("[collect] Workers running in background, waiting for frontier to drain...")
    last_total = -1
    while _frontier_active():
        try:
            con = sqlite3.connect(str(DB_PATH))
            total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            pending = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='pending'"
            ).fetchone()[0]
            in_prog = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='in_progress'"
            ).fetchone()[0]
            con.close()
            if total != last_total:
                print(f"  games={total}  pending={pending}  in_progress={in_prog}")
                last_total = total
        except Exception:
            pass
        time.sleep(10)

    con = sqlite3.connect(str(DB_PATH))
    final_total = con.execute(
        "SELECT COUNT(*) FROM games WHERE queue_id=2400"
    ).fetchone()[0]
    con.close()
    print(f"[collect] Crawl complete. {final_total} Mayhem games in database.")

    subprocess.run(
        [
            sys.executable, "lcu_collector.py", "export",
            "--db", str(DB_PATH),
            "--queue", "2400",
            "--out", str(out),
            *platform_args,
        ],
        check=True,
    )

    print()
    print(f"[collect] Done! -> {out} ({final_total} Mayhem games)")
    print("  GitHub: https://github.com/Lanternko/ARAM-mayhem-collector")
    print(f"  platform: {args.platform or 'TW2'}  mayhem_games={final_total}")


if __name__ == "__main__":
    main()
