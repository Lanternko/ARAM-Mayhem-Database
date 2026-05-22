#!/usr/bin/env python3
"""macOS crawl watchdog entrypoint.

This is a focused wrapper around scripts/crawler_mac.py for the common Mayhem
crawl failure mode on macOS:

1. keep exactly 1-2 detached snowball workers alive
2. target a minimum Mayhem capture rate
3. restart workers when throughput stays low
4. escalate to a League client restart when worker restarts do not recover rate
5. restart League client immediately if memory crosses the configured threshold
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRAWLER_MAC = ROOT / "scripts/crawler_mac.py"
DEFAULT_TARGET_RATE_PER_MIN = 12.0
DEFAULT_CLAIM_TIMEOUT_SEC = 60


def _run_crawler(args: list[str]) -> int:
    return subprocess.call([sys.executable, str(CRAWLER_MAC), *args], cwd=str(ROOT))


def _common_watchdog_args(args: argparse.Namespace) -> list[str]:
    return [
        "--workers",
        str(args.workers),
        "--target-games",
        str(args.target_games),
        "--max-players",
        str(args.max_players),
        "--claim-timeout-sec",
        str(args.claim_timeout_sec),
        "--target-rate",
        str(args.target_rate),
        "--rate-window-sec",
        str(args.rate_window_sec),
        "--check-sec",
        str(args.check_sec),
        "--total-mem-mb",
        str(args.total_mem_mb),
        "--single-mem-mb",
        str(args.single_mem_mb),
        "--low-rate-client-strikes",
        str(args.low_rate_client_strikes),
        "--low-rate-grace-sec",
        str(args.low_rate_grace_sec),
    ] + [
        item
        for seed_file in args.seed_riot_id_file
        for item in ("--seed-riot-id-file", str(seed_file))
    ]


def cmd_start(args: argparse.Namespace) -> int:
    cmd = ["start"]
    if args.replace:
        cmd.append("--replace")
    cmd.extend(_common_watchdog_args(args))
    return _run_crawler(cmd)


def cmd_foreground(args: argparse.Namespace) -> int:
    cmd = ["watchdog", "--low-rate-strikes", str(args.low_rate_strikes)]
    cmd.extend(_common_watchdog_args(args))
    return _run_crawler(cmd)


def cmd_status(args: argparse.Namespace) -> int:
    return _run_crawler(["status", "--rate-window-sec", str(args.rate_window_sec)])


def cmd_stop(args: argparse.Namespace) -> int:
    cmd = ["stop"]
    if not args.workers:
        cmd.append("--no-workers")
    if args.league:
        cmd.append("--league")
    return _run_crawler(cmd)


def cmd_restart_client(args: argparse.Namespace) -> int:
    cmd = [
        "restart-client",
        "--workers",
        str(args.workers),
        "--target-games",
        str(args.target_games),
        "--max-players",
        str(args.max_players),
        "--claim-timeout-sec",
        str(args.claim_timeout_sec),
        "--restart-wait-sec",
        str(args.restart_wait_sec),
    ]
    for seed_file in args.seed_riot_id_file:
        cmd.extend(["--seed-riot-id-file", str(seed_file)])
    if args.dry_run:
        cmd.append("--dry-run")
    return _run_crawler(cmd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="macOS Mayhem crawler watchdog")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workers", type=int, default=2, help="Keep this many snowball workers alive")
    common.add_argument("--target-games", type=int, default=5000)
    common.add_argument("--max-players", type=int, default=5000)
    common.add_argument(
        "--claim-timeout-sec",
        type=int,
        default=DEFAULT_CLAIM_TIMEOUT_SEC,
        help="Reclaim a worker claim only after this many seconds",
    )
    common.add_argument(
        "--seed-riot-id-file",
        action="append",
        default=[],
        type=Path,
        help="Seed file passed to the first worker on launch/relaunch",
    )

    watchdog_common = argparse.ArgumentParser(add_help=False)
    watchdog_common.add_argument(
        "--target-rate",
        type=float,
        default=DEFAULT_TARGET_RATE_PER_MIN,
        help="Minimum Mayhem games per minute",
    )
    watchdog_common.add_argument("--rate-window-sec", type=int, default=120)
    watchdog_common.add_argument("--check-sec", type=int, default=60)
    watchdog_common.add_argument("--total-mem-mb", type=float, default=10240.0)
    watchdog_common.add_argument("--single-mem-mb", type=float, default=6144.0)
    watchdog_common.add_argument(
        "--low-rate-client-strikes",
        type=int,
        default=3,
        help="Escalate to League client restart after this many low-rate worker restarts",
    )
    watchdog_common.add_argument(
        "--low-rate-grace-sec",
        type=int,
        default=300,
        help="Do not judge low throughput for this many seconds after worker/client restart",
    )

    start = sub.add_parser("start", parents=[common, watchdog_common], help="Start detached watchdog")
    start.add_argument("--replace", action="store_true", default=False, help="Replace existing watchdog")
    start.set_defaults(func=cmd_start)

    foreground = sub.add_parser(
        "foreground",
        parents=[common, watchdog_common],
        help="Run watchdog in foreground for debugging",
    )
    foreground.add_argument("--low-rate-strikes", type=int, default=2)
    foreground.set_defaults(func=cmd_foreground)

    status = sub.add_parser("status", help="Print crawler/watchdog status")
    status.add_argument("--rate-window-sec", type=int, default=120)
    status.set_defaults(func=cmd_status)

    stop = sub.add_parser("stop", help="Stop watchdog and, by default, workers")
    stop.add_argument("--workers/--no-workers", dest="workers", action=argparse.BooleanOptionalAction, default=True)
    stop.add_argument("--league", action="store_true", help="Also stop League client")
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart-client", parents=[common], help="Manually test the client restart path")
    restart.add_argument("--dry-run", action="store_true")
    restart.add_argument("--restart-wait-sec", type=int, default=180)
    restart.set_defaults(func=cmd_restart_client)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
