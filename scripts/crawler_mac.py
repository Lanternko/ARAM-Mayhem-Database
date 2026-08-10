#!/usr/bin/env python3
"""macOS launcher/watchdog for the LCU Mayhem crawler.

This wraps scripts/lcu_collector.py for macOS:
- starts snowball workers in detached process sessions
- uses pgrep/pkill instead of PowerShell
- monitors crawl rate and League client memory
- restarts workers on low throughput
- restarts League client when memory crosses a defensive threshold
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data/lcu/games.db"
CODEX_DIR = ROOT / ".codex"
# Some environments (notably sandboxed runners) can read/write inside the repo
# but still fail to create new files under Documents/Desktop due to macOS privacy
# controls, producing: PermissionError: [Errno 1] Operation not permitted.
# Use a fallback lock in /private/tmp so restart-client / watchdog can still work.
MAINTENANCE_LOCK_FALLBACK = Path("/private/tmp") / "aram_crawler_mac_maintenance.lock"
FALLBACK_LOG_DIR = Path("/private/tmp") / "aram_crawler_mac_logs"
WORKER_PID_DIR = FALLBACK_LOG_DIR
LEAGUE_APP = "/Applications/League of Legends.app"
DEFAULT_TARGET_RATE_PER_MIN = 12.0
DEFAULT_CLAIM_TIMEOUT_SEC = 60


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:
        return False
    return True


# Prefer repo-local `.codex` for logs/pidfiles, but fall back to /private/tmp when
# the environment denies writes (common on macOS with iCloud/Files-on-Demand +
# privacy controls).
RUNTIME_DIR = CODEX_DIR if _is_writable_dir(CODEX_DIR) else FALLBACK_LOG_DIR
LOG_DIR = RUNTIME_DIR
WATCHDOG_PID = RUNTIME_DIR / "crawler_mac_watchdog.pid"
WATCHDOG_LOG = LOG_DIR / "crawler_mac_watchdog.log"
MAINTENANCE_LOCK_PRIMARY = RUNTIME_DIR / "crawler_mac_maintenance.lock"


def _worker_pid_path(worker_id: str) -> Path:
    return WORKER_PID_DIR / f"snowball_{worker_id}.pid"


def _run(cmd: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (PermissionError, FileNotFoundError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", f"{type(exc).__name__}: {exc}")


def _log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Some sandboxed environments can read repo files but refuse writes.
        # Logging should never crash control commands (restart-client/status).
        pass
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        with WATCHDOG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except PermissionError:
        pass


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_watchdog_pid() -> int | None:
    try:
        pid = int(WATCHDOG_PID.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if _process_exists(pid):
        # Best-effort validate the PID is "ours". In some environments we can't
        # inspect process cmdline (ps/psutil) and os.kill(pid, 0) may return
        # PermissionError for unrelated PIDs. If we can't read /proc or inspect,
        # fall back to the watchdog log heartbeat: if the log hasn't advanced
        # recently, treat the pidfile as stale so start/restart can recover.
        try:
            log_age_sec = time.time() - (LOG_DIR / "crawler_mac_watchdog.log").stat().st_mtime
        except Exception:
            log_age_sec = 0.0
        if log_age_sec > 10 * 60:
            return None
        return pid
    return None


def _python_process_pids(fragment: str, *, exclude_self: bool = True) -> list[int]:
    result = _run(["ps", "-axo", "pid,args"])
    own_pid = os.getpid()
    pids: list[int] = []
    if result.returncode != 0 or not result.stdout.strip():
        return pids
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        pid_raw, _, args = line.partition(" ")
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        if exclude_self and pid == own_pid:
            continue
        parts = args.split()
        if not parts or "python" not in Path(parts[0]).name:
            continue
        if fragment in args:
            pids.append(pid)
    return sorted(set(pids))


def _watchdog_pids() -> list[int]:
    pids = _python_process_pids("scripts/crawler_mac.py watchdog")
    pidfile_pid = _read_watchdog_pid()
    if pidfile_pid:
        pids.append(pidfile_pid)
    return sorted(set(pids))


def _snowball_pids() -> list[int]:
    pids = _python_process_pids("scripts/lcu_collector.py snowball")
    if pids:
        return pids

    # Fallback: use recorded worker pidfiles when pgrep is blocked/unavailable.
    for worker_id in ("W01", "W02", "W03", "W04"):
        path = _worker_pid_path(worker_id)
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if _process_exists(pid):
            pids.append(pid)
    return sorted(set(pids))


def _stop_pids(pids: list[int], *, timeout_sec: float = 10.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _process_exists(pid)]
        if not alive:
            return
        time.sleep(0.5)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass


def _stop_snowball_workers() -> None:
    pids = _snowball_pids()
    if pids:
        _log(f"stopping snowball workers pids={pids}")
        _stop_pids(pids)


def _open_logfile(path: Path):
    candidates = (path, FALLBACK_LOG_DIR / path.name)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate.open("ab")
        except PermissionError as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return (FALLBACK_LOG_DIR / path.name).open("ab")


def _launch_worker(
    worker_id: str,
    *,
    seed: bool,
    target_games: int,
    max_players: int,
    claim_timeout_sec: int,
    seed_riot_id_files: tuple[Path, ...] = (),
) -> int:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass
    cmd = [
        sys.executable,
        "-u",
        "scripts/lcu_collector.py",
        "snowball",
        "--worker-id",
        worker_id,
        "--target-games",
        str(target_games),
        "--max-players",
        str(max_players),
        "--games-per-player",
        "0",
        "--claim-timeout-sec",
        str(max(1, claim_timeout_sec)),
    ]
    if not seed:
        cmd.extend(["--no-seed-self", "--no-seed-friends"])
    elif seed_riot_id_files:
        for seed_file in seed_riot_id_files:
            cmd.extend(["--seed-riot-id-file", str(seed_file)])

    _log(
        "launch_cmd"
        f" worker={worker_id}"
        f" seed={int(seed)}"
        f" claim_timeout_sec={max(1, claim_timeout_sec)}"
        f" seed_files={[str(p) for p in seed_riot_id_files] if seed else []}"
        f" cmd={' '.join(map(str, cmd))}"
    )
    stdout = _open_logfile(LOG_DIR / f"snowball_{worker_id}.log")
    stderr = _open_logfile(LOG_DIR / f"snowball_{worker_id}.err")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
    )
    try:
        WORKER_PID_DIR.mkdir(parents=True, exist_ok=True)
        _worker_pid_path(worker_id).write_text(str(proc.pid), encoding="utf-8")
    except PermissionError:
        pass
    _log(f"launched {worker_id} pid={proc.pid}")
    return int(proc.pid)


def _active_worker_ids() -> set[str]:
    result = _run(["ps", "-axo", "args"])
    ids: set[str] = set()
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.splitlines():
            if "scripts/lcu_collector.py snowball" not in line:
                continue
            for worker_id in ("W01", "W02", "W03", "W04"):
                if f"--worker-id {worker_id}" in line:
                    ids.add(worker_id)
        return ids

    # Fallback: ps may be blocked by macOS privacy controls in sandboxed runners.
    for worker_id in ("W01", "W02", "W03", "W04"):
        path = _worker_pid_path(worker_id)
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if _process_exists(pid):
            ids.add(worker_id)
    return ids


def _ensure_workers(
    workers: int,
    *,
    target_games: int,
    max_players: int,
    claim_timeout_sec: int,
    seed_riot_id_files: tuple[Path, ...] = (),
) -> None:
    active = _active_worker_ids()
    if len(active) >= workers:
        return
    for idx in range(1, workers + 1):
        worker_id = f"W{idx:02d}"
        if worker_id in active:
            continue
        _launch_worker(
            worker_id,
            seed=(idx == 1),
            target_games=target_games,
            max_players=max_players,
            claim_timeout_sec=claim_timeout_sec,
            seed_riot_id_files=seed_riot_id_files,
        )
        active.add(worker_id)
        if len(active) >= workers:
            return


def _mayhem_rate_per_min(window_sec: int) -> float:
    if not DB_PATH.exists():
        return 0.0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=window_sec)
    with sqlite3.connect(str(DB_PATH)) as con:
        count = con.execute(
            """
            SELECT COUNT(*)
            FROM games
            WHERE queue_id = 2400
              AND julianday(captured_at) >= julianday(?)
            """,
            (cutoff.isoformat(),),
        ).fetchone()[0]
    return float(count) * 60.0 / float(window_sec)


def _league_memory_mb() -> tuple[float, float, list[tuple[int, float, str]]]:
    try:
        result = _run(["ps", "-axo", "pid,rss,args"])
    except PermissionError:
        return 0.0, 0.0, []
    except FileNotFoundError:
        return 0.0, 0.0, []
    rows: list[tuple[int, float, str]] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_raw, rss_raw, args = parts
        if not any(name in args for name in ("LeagueClient", "League of Legends", "RiotClient")):
            continue
        try:
            rows.append((int(pid_raw), int(rss_raw) / 1024.0, args))
        except ValueError:
            continue
    total_mb = sum(row[1] for row in rows)
    max_mb = max((row[1] for row in rows), default=0.0)
    return total_mb, max_mb, rows


def _lcu_ready() -> bool:
    code = (
        "from aram_nn.lcu.process import get_credentials; "
        "raise SystemExit(0 if get_credentials() else 1)"
    )
    return _run([sys.executable, "-c", code]).returncode == 0


def _maintenance_lock_exists() -> bool:
    return MAINTENANCE_LOCK_PRIMARY.exists() or MAINTENANCE_LOCK_FALLBACK.exists()


def _write_maintenance_lock() -> Path:
    try:
        CODEX_DIR.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_LOCK_PRIMARY.write_text(str(os.getpid()), encoding="utf-8")
        return MAINTENANCE_LOCK_PRIMARY
    except PermissionError:
        MAINTENANCE_LOCK_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_LOCK_FALLBACK.write_text(str(os.getpid()), encoding="utf-8")
        return MAINTENANCE_LOCK_FALLBACK


def _clear_maintenance_lock() -> None:
    for path in (MAINTENANCE_LOCK_PRIMARY, MAINTENANCE_LOCK_FALLBACK):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            pass


def _restart_league_client(*, wait_sec: int) -> None:
    lock_path = _write_maintenance_lock()
    try:
        if lock_path != MAINTENANCE_LOCK_PRIMARY:
            _log(f"maintenance lock fallback active path={lock_path}")
        _log("restart requested: stopping crawler before League client restart")
        _stop_snowball_workers()
        for pattern in ("LeagueClient", "League of Legends"):
            _run(["pkill", "-TERM", "-f", pattern])
        time.sleep(8)
        for pattern in ("LeagueClient", "League of Legends"):
            _run(["pkill", "-KILL", "-f", pattern])
        _log(f"opening {LEAGUE_APP}")
        _run(["open", LEAGUE_APP])

        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            if _lcu_ready():
                _log("LCU credentials ready after restart")
                return
            time.sleep(5)
        _log("LCU credentials not ready before timeout")
    finally:
        _clear_maintenance_lock()


def cmd_start(args: argparse.Namespace) -> int:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    existing = _watchdog_pids()
    if existing and not args.replace:
        print(f"[crawler-mac] watchdog already running pids={existing}")
        return 0
    if existing and args.replace:
        print(f"[crawler-mac] replacing watchdog pids={existing}")
        _stop_pids(existing)

    cmd = [
        sys.executable,
        "scripts/crawler_mac.py",
        "watchdog",
        "--target-rate",
        str(args.target_rate),
        "--rate-window-sec",
        str(args.rate_window_sec),
        "--check-sec",
        str(args.check_sec),
        "--workers",
        str(args.workers),
        "--target-games",
        str(args.target_games),
        "--max-players",
        str(args.max_players),
        "--claim-timeout-sec",
        str(args.claim_timeout_sec),
        "--total-mem-mb",
        str(args.total_mem_mb),
        "--single-mem-mb",
        str(args.single_mem_mb),
        "--low-rate-client-strikes",
        str(args.low_rate_client_strikes),
        "--low-rate-grace-sec",
        str(args.low_rate_grace_sec),
    ]
    for seed_file in args.seed_riot_id_file:
        cmd.extend(["--seed-riot-id-file", str(seed_file)])
    stdout = _open_logfile(LOG_DIR / "crawler_mac.stdout")
    stderr = _open_logfile(LOG_DIR / "crawler_mac.stderr")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
    )
    try:
        WATCHDOG_PID.write_text(str(proc.pid), encoding="utf-8")
    except PermissionError:
        pass
    print(f"[crawler-mac] started watchdog pid={proc.pid}")
    print("  monitor: python scripts/crawler_mac.py status")
    print(f"  logs:    tail -f {WATCHDOG_LOG}")
    print("  stop:    python scripts/crawler_mac.py stop")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    watchdogs = _watchdog_pids()
    if watchdogs:
        print(f"[crawler-mac] stopping watchdog pids={watchdogs}")
        _stop_pids(watchdogs)
    elif WATCHDOG_PID.exists():
        try:
            WATCHDOG_PID.unlink()
        except PermissionError:
            _log(f"PermissionError unlinking pidfile path={WATCHDOG_PID} (continuing)")
    if args.workers:
        _stop_snowball_workers()
    if args.league:
        print("[crawler-mac] stopping League client")
        for pattern in ("LeagueClient", "League of Legends"):
            _run(["pkill", "-TERM", "-f", pattern])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    watchdogs = _watchdog_pids()
    workers = _snowball_pids()
    rate = _mayhem_rate_per_min(args.rate_window_sec)
    total_mb, max_mb, _ = _league_memory_mb()
    watchdog_label: int | list[int] | str
    if not watchdogs:
        watchdog_label = "stopped"
    elif len(watchdogs) == 1:
        watchdog_label = watchdogs[0]
    else:
        watchdog_label = watchdogs
    print(f"[crawler-mac] watchdog={watchdog_label} workers={workers}")
    print(
        f"  mayhem_rate={rate:.2f}/min window={args.rate_window_sec}s  "
        f"league_mem_total={total_mb:.0f}MB league_mem_max={max_mb:.0f}MB"
    )
    result = _run([sys.executable, "scripts/lcu_collector.py", "metrics"])
    print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def cmd_workers(args: argparse.Namespace) -> int:
    if args.replace:
        _stop_snowball_workers()
    _ensure_workers(
        args.workers,
        target_games=args.target_games,
        max_players=args.max_players,
        claim_timeout_sec=args.claim_timeout_sec,
        seed_riot_id_files=tuple(args.seed_riot_id_file),
    )
    print(f"[crawler-mac] workers={_snowball_pids()}")
    return 0


def cmd_restart_client(args: argparse.Namespace) -> int:
    if args.dry_run:
        workers = _snowball_pids()
        total_mb, max_mb, rows = _league_memory_mb()
        print("[crawler-mac] dry-run restart-client")
        print(f"  would stop workers={workers}")
        print(f"  would stop LeagueClient / League of Legends processes={[(pid, round(mb, 1)) for pid, mb, _ in rows]}")
        print(f"  memory total={total_mb:.0f}MB max={max_mb:.0f}MB")
        print(f"  would open: {LEAGUE_APP}")
        return 0

    _restart_league_client(wait_sec=args.restart_wait_sec)
    _ensure_workers(
        args.workers,
        target_games=args.target_games,
        max_players=args.max_players,
        claim_timeout_sec=args.claim_timeout_sec,
        seed_riot_id_files=tuple(args.seed_riot_id_file),
    )
    print(f"[crawler-mac] restart-client complete workers={_snowball_pids()}")
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    try:
        WATCHDOG_PID.write_text(str(os.getpid()), encoding="utf-8")
    except PermissionError:
        _log(f"PermissionError writing pidfile path={WATCHDOG_PID} (continuing without pidfile)")
    _log(
        "watchdog started "
        f"target_rate={args.target_rate:.1f}/min workers={args.workers} "
        f"claim_timeout_sec={args.claim_timeout_sec} "
        f"mem_total={args.total_mem_mb:.0f}MB mem_single={args.single_mem_mb:.0f}MB"
    )
    last_restart_at = 0.0
    low_rate_streak = 0
    low_rate_worker_restarts = 0
    low_rate_grace_until = time.monotonic() + args.low_rate_grace_sec
    while True:
        try:
            if _maintenance_lock_exists():
                _log("maintenance lock active; skipping watchdog check")
                time.sleep(max(10, args.check_sec))
                continue
            total_mb, max_mb, rows = _league_memory_mb()
            cooldown_ok = time.monotonic() - last_restart_at >= args.restart_cooldown_sec
            if not _lcu_ready():
                workers = _snowball_pids()
                _log(
                    f"state=lcu-unavailable rate=0.00/min workers={len(workers)} "
                    f"league_mem_total={total_mb:.0f}MB league_mem_max={max_mb:.0f}MB"
                )
                if workers:
                    _stop_snowball_workers()
                if cooldown_ok:
                    _log("LCU unavailable; restarting League client")
                    _restart_league_client(wait_sec=args.restart_wait_sec)
                    last_restart_at = time.monotonic()
                    low_rate_grace_until = time.monotonic() + args.low_rate_grace_sec
                else:
                    _log("LCU unavailable but restart cooldown is active")
                low_rate_streak = 0
                time.sleep(max(10, args.check_sec))
                continue
            _ensure_workers(
                args.workers,
                target_games=args.target_games,
                max_players=args.max_players,
                claim_timeout_sec=args.claim_timeout_sec,
                seed_riot_id_files=tuple(args.seed_riot_id_file),
            )
            rate = _mayhem_rate_per_min(args.rate_window_sec)
            workers = _snowball_pids()
            grace_active = time.monotonic() < low_rate_grace_until
            if grace_active:
                low_rate_streak = 0
            elif rate >= args.target_rate:
                low_rate_streak = 0
                low_rate_worker_restarts = 0
            else:
                low_rate_streak += 1
            state = "warmup" if grace_active else ("ok" if rate >= args.target_rate else "below-goal")
            _log(
                f"state={state} rate={rate:.2f}/min workers={len(workers)} "
                f"low_rate_streak={low_rate_streak} "
                f"league_mem_total={total_mb:.0f}MB league_mem_max={max_mb:.0f}MB"
            )

            memory_hot = total_mb >= args.total_mem_mb or max_mb >= args.single_mem_mb
            if memory_hot and cooldown_ok:
                top = sorted(rows, key=lambda row: row[1], reverse=True)[:3]
                _log(f"memory threshold crossed top={[(pid, round(mb, 1)) for pid, mb, _ in top]}")
                _restart_league_client(wait_sec=args.restart_wait_sec)
                last_restart_at = time.monotonic()
                low_rate_grace_until = time.monotonic() + args.low_rate_grace_sec
                _ensure_workers(
                    args.workers,
                    target_games=args.target_games,
                    max_players=args.max_players,
                    claim_timeout_sec=args.claim_timeout_sec,
                    seed_riot_id_files=tuple(args.seed_riot_id_file),
                )
                low_rate_streak = 0
            elif memory_hot:
                _log("memory threshold crossed but restart cooldown is active")
            elif low_rate_streak >= args.low_rate_strikes:
                low_rate_worker_restarts += 1
                if low_rate_worker_restarts >= args.low_rate_client_strikes and cooldown_ok:
                    _log(
                        f"rate below target after {low_rate_worker_restarts} worker restarts; "
                        "restarting League client"
                    )
                    _restart_league_client(wait_sec=args.restart_wait_sec)
                    last_restart_at = time.monotonic()
                    low_rate_grace_until = time.monotonic() + args.low_rate_grace_sec
                    _ensure_workers(
                        args.workers,
                        target_games=args.target_games,
                        max_players=args.max_players,
                        claim_timeout_sec=args.claim_timeout_sec,
                        seed_riot_id_files=tuple(args.seed_riot_id_file),
                    )
                    low_rate_streak = 0
                    low_rate_worker_restarts = 0
                    continue
                _log(
                    f"rate below target for {low_rate_streak} checks; "
                    f"restarting snowball workers only low_rate_worker_restarts={low_rate_worker_restarts}"
                )
                _stop_snowball_workers()
                low_rate_grace_until = time.monotonic() + args.low_rate_grace_sec
                _ensure_workers(
                    args.workers,
                    target_games=args.target_games,
                    max_players=args.max_players,
                    claim_timeout_sec=args.claim_timeout_sec,
                    seed_riot_id_files=tuple(args.seed_riot_id_file),
                )
                low_rate_streak = 0
        except Exception as exc:
            _log(f"watchdog error: {exc!r}")
        time.sleep(max(10, args.check_sec))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="macOS LCU crawler launcher/watchdog")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workers", type=int, default=2)
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

    start = sub.add_parser("start", parents=[common], help="start detached watchdog + workers")
    start.add_argument("--replace", action="store_true", help="replace an existing watchdog")
    start.add_argument("--target-rate", type=float, default=DEFAULT_TARGET_RATE_PER_MIN)
    start.add_argument("--rate-window-sec", type=int, default=120)
    start.add_argument("--check-sec", type=int, default=60)
    start.add_argument("--total-mem-mb", type=float, default=10240.0)
    start.add_argument("--single-mem-mb", type=float, default=6144.0)
    start.add_argument("--low-rate-client-strikes", type=int, default=3)
    start.add_argument("--low-rate-grace-sec", type=int, default=300)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop watchdog and workers")
    stop.add_argument("--workers/--no-workers", dest="workers", action=argparse.BooleanOptionalAction, default=True)
    stop.add_argument("--league", action="store_true", help="also stop League client")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="print mac crawler status and lcu metrics")
    status.add_argument("--rate-window-sec", type=int, default=120)
    status.set_defaults(func=cmd_status)

    workers = sub.add_parser("workers", parents=[common], help="start detached workers without watchdog")
    workers.add_argument("--replace", action="store_true")
    workers.set_defaults(func=cmd_workers)

    restart_client = sub.add_parser(
        "restart-client",
        parents=[common],
        help="restart League client using the same macOS path as the watchdog",
    )
    restart_client.add_argument("--dry-run", action="store_true")
    restart_client.add_argument("--restart-wait-sec", type=int, default=180)
    restart_client.set_defaults(func=cmd_restart_client)

    watchdog = sub.add_parser("watchdog", parents=[common], help="run watchdog in foreground")
    watchdog.add_argument("--target-rate", type=float, default=DEFAULT_TARGET_RATE_PER_MIN)
    watchdog.add_argument("--rate-window-sec", type=int, default=120)
    watchdog.add_argument("--check-sec", type=int, default=60)
    watchdog.add_argument("--total-mem-mb", type=float, default=10240.0)
    watchdog.add_argument("--single-mem-mb", type=float, default=6144.0)
    watchdog.add_argument("--restart-cooldown-sec", type=int, default=1800)
    watchdog.add_argument("--restart-wait-sec", type=int, default=180)
    watchdog.add_argument("--low-rate-strikes", type=int, default=2)
    watchdog.add_argument("--low-rate-client-strikes", type=int, default=3)
    watchdog.add_argument("--low-rate-grace-sec", type=int, default=300)
    watchdog.set_defaults(func=cmd_watchdog)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
