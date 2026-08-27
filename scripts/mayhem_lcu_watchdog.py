from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

import psutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "lcu" / "games.db"
DEFAULT_SEED_FILE = ROOT / "data" / "seeds" / "opgg_tw.txt"
DEFAULT_LOG_DIR = ROOT / ".codex" / "logs" / "mayhem_lcu_watchdog"
DEFAULT_STATE_FILE = ROOT / "data" / "monitor" / "mayhem_lcu_watchdog.jsonl"
DEFAULT_STATIC_PUBLISH_STATE = ROOT / "data" / "site" / "static_publish_state.json"
DEFAULT_STATIC_PUBLISH_LOG_DIR = ROOT / "data" / "site"
DEFAULT_MODEL_REFRESH_STATE = ROOT / "data" / "site" / "model_refresh_state.json"
DEFAULT_MODEL_REFRESH_LOG_DIR = ROOT / "data" / "site"
LEAGUE_LOCKFILES = (
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\?\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
)
DEFAULT_RIOT_CLIENTS = (
    Path(r"D:\遊戲\Riot Games\Riot Client\RiotClientServices.exe"),
    Path(r"C:\Riot Games\Riot Client\RiotClientServices.exe"),
    Path(r"D:\Riot Games\Riot Client\RiotClientServices.exe"),
    Path(r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe"),
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def background_python_executable() -> str:
    if os.name != "nt":
        return sys.executable
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw) if pythonw.exists() else sys.executable


def mb(rss: int | float) -> float:
    return round(float(rss) / 1024 / 1024, 1)


def iter_processes() -> list[psutil.Process]:
    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "memory_info"]):
        try:
            # Touch info now so later access is less likely to race.
            _ = proc.info
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_snowball_worker(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or "").lower()
        cmdline = proc.info.get("cmdline") or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name not in ("python.exe", "pythonw.exe") or len(cmdline) < 2:
        return False
    normalized = [str(part).replace("/", "\\") for part in cmdline]
    joined = " ".join(normalized)
    return r"scripts\lcu_collector.py" in joined and "snowball-workers" in normalized


def is_static_site_publisher(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or "").lower()
        cmdline = proc.info.get("cmdline") or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name not in ("python.exe", "pythonw.exe") or len(cmdline) < 2:
        return False
    joined = " ".join(str(part) for part in cmdline).replace("/", "\\")
    return (
        r"scripts\publish_static_site.py" in joined
        and "--watch" in [str(part) for part in cmdline]
    )


def static_site_publishers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        if not is_static_site_publisher(proc):
            continue
        try:
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "rss_mb": mb(rss),
                    "cmdline": proc.info.get("cmdline") or [],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def model_refresh_health(state_path: Path) -> dict[str, Any]:
    """Summarise the refresher's own state file.

    A live PID says nothing about whether the refresh actually works: the
    refresher crash-looped 436 times (2026-08-12..26) while looking perfectly
    healthy here.  Surface the failure streak so the watchdog record shows it.
    """
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    if not isinstance(state, dict):
        return {"available": False}
    return {
        "available": True,
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "last_result": state.get("last_result"),
        "last_error": state.get("last_error"),
        "last_error_at_unix": state.get("last_error_at_unix"),
        "last_refresh_at_unix": state.get("last_refresh_at_unix"),
    }


def is_model_refresher(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or "").lower()
        cmdline = proc.info.get("cmdline") or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name not in ("python.exe", "pythonw.exe") or len(cmdline) < 2:
        return False
    joined = " ".join(str(part) for part in cmdline).replace("/", "\\")
    return (
        r"scripts\refresh_recommender_models.py" in joined
        and "--watch" in [str(part) for part in cmdline]
    )


def model_refreshers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        if not is_model_refresher(proc):
            continue
        try:
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "rss_mb": mb(rss),
                    "cmdline": proc.info.get("cmdline") or [],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def snowball_workers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        if not is_snowball_worker(proc):
            continue
        try:
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "rss_mb": mb(rss),
                    "cmdline": proc.info.get("cmdline") or [],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def worker_number_from_cmdline(cmdline: Sequence[Any]) -> int | None:
    try:
        parts = [str(part) for part in cmdline]
        worker_id = parts[parts.index("--worker-id") + 1]
        if len(worker_id) > 1 and worker_id[0].upper() == "W":
            return int(worker_id[1:])
    except (ValueError, IndexError, TypeError):
        return None
    return None


def fleet_control_file_from_cmdline(cmdline: Sequence[Any]) -> Path | None:
    parts = [str(part) for part in cmdline]
    try:
        value = parts[parts.index("--control-file") + 1]
    except (ValueError, IndexError):
        return None
    return Path(value) if value else None


def stop_snowball_workers(grace_sec: int = 45) -> list[int]:
    stopped: list[int] = []
    procs = [proc for proc in iter_processes() if is_snowball_worker(proc)]
    for proc in procs:
        try:
            control_file = fleet_control_file_from_cmdline(proc.info.get("cmdline") or [])
            if control_file is None:
                raise RuntimeError(f"fleet pid={proc.pid} has no control file")
            control_file.parent.mkdir(parents=True, exist_ok=True)
            control_file.write_text("stop\n", encoding="utf-8")
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(procs, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stopped


def stop_extra_snowball_workers(keep: int, grace_sec: int = 5) -> list[int]:
    workers: list[tuple[psutil.Process, int]] = []
    for proc in iter_processes():
        if not is_snowball_worker(proc):
            continue
        worker_num = 999
        try:
            worker_num = worker_number_from_cmdline(proc.info.get("cmdline") or []) or 999
        except (TypeError, ValueError):
            worker_num = 999
        workers.append((proc, worker_num))

    workers.sort(key=lambda item: item[1])
    to_stop = [proc for proc, _ in workers[keep:]]
    stopped: list[int] = []
    for proc in to_stop:
        try:
            proc.terminate()
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(to_stop, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stopped


def league_processes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in iter_processes():
        try:
            name = proc.info.get("name") or ""
            if not name.lower().startswith("leagueclient"):
                continue
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "name": name,
                    "exe": proc.info.get("exe"),
                    "rss_mb": mb(rss),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def league_main_mb() -> float:
    vals = [row["rss_mb"] for row in league_processes() if row["name"].lower() == "leagueclient.exe"]
    return max(vals, default=0.0)


def read_lockfile() -> tuple[str, str] | None:
    for path in LEAGUE_LOCKFILES:
        try:
            if not path.exists():
                continue
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 5:
                return parts[2], parts[3]
        except OSError:
            continue
    return None


def lcu_get(path: str, timeout_sec: float = 5.0) -> tuple[int | str, str]:
    lock = read_lockfile()
    if not lock:
        return "ERR", "lockfile missing"
    port, password = lock
    auth = base64.b64encode(("riot:" + password).encode("utf-8")).decode("ascii")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{path}",
        headers={"Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_sec) as resp:
            body = resp.read(512).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return "ERR", type(exc).__name__ + ": " + str(exc)


def lcu_health() -> dict[str, Any]:
    summoner_status, summoner_body = lcu_get("/lol-summoner/v1/current-summoner")
    phase_status, phase_body = lcu_get("/lol-gameflow/v1/gameflow-phase")
    phase = None
    if phase_status == 200:
        try:
            phase = json.loads(phase_body)
        except json.JSONDecodeError:
            phase = phase_body.strip('"')
    return {
        "ok": summoner_status == 200 and phase_status == 200,
        "current_summoner_status": summoner_status,
        "gameflow_status": phase_status,
        "phase": phase,
        "summoner_body_bytes": len(summoner_body.encode("utf-8", "replace")),
        "phase_body_bytes": len(phase_body.encode("utf-8", "replace")),
    }


def find_riot_client() -> Path | None:
    for proc in iter_processes():
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe")
            if name == "riotclientservices.exe" and exe:
                return Path(exe)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for path in DEFAULT_RIOT_CLIENTS:
        if path.exists():
            return path
    return None


def riot_remoting() -> tuple[str, str] | None:
    for proc in iter_processes():
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "--app-port=" not in cmd or "--remoting-auth-token=" not in cmd:
            continue
        port = re.search(r"--app-port=(\d+)", cmd)
        token = re.search(r"--remoting-auth-token=([^\s]+)", cmd)
        if port and token:
            return port.group(1), token.group(1)
    return None


def remoting_request(path: str, method: str = "GET", data: bytes | None = None) -> tuple[int | str, str]:
    remoting = riot_remoting()
    if not remoting:
        return "ERR", "Riot remoting port/token not found"
    port, token = remoting
    auth = base64.b64encode(("riot:" + token).encode("utf-8")).decode("ascii")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            body = resp.read(512).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return "ERR", type(exc).__name__ + ": " + str(exc)


def wait_for_riot_remoting(timeout_sec: int) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if riot_remoting():
            return True
        time.sleep(2)
    return False


def close_league_client(grace_sec: int = 20) -> list[int]:
    targets = []
    for proc in iter_processes():
        try:
            name = (proc.info.get("name") or "").lower()
            if name.startswith("leagueclient"):
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    closed: list[int] = []
    for proc in targets:
        try:
            proc.terminate()
            closed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(targets, timeout=grace_sec)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return closed


def start_league_client() -> dict[str, Any]:
    status, body = remoting_request(
        "/product-launcher/v1/products/league_of_legends/patchlines/live",
        method="POST",
        data=b"{}",
    )
    if status == 200:
        return {"started": True, "method": "riot_remoting", "response": "<redacted>"}

    # A Riot Client process with stale remoting args is also a zombie: the
    # advertised port can refuse connections while fresh launches keep forwarding
    # their app-command to that dead instance. A transport failure proves the
    # existing remoting endpoint is unusable. HTTP 424 means remoting is
    # reachable but the product launcher cannot launch League; forwarding
    # --launch-product to that same instance does not recover. Recycle every
    # Riot Client process before cold-starting in both cases. Match both
    # "RiotClientServices.exe" and the space-form "Riot Client.exe" (the UX
    # host); stray copies wedge a fresh boot pre-LCU.
    killed_zombie_pids: list[int] = []
    if status in ("ERR", 424):
        zombies = []
        for proc in iter_processes():
            try:
                name = (proc.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name.replace(" ", "").startswith("riotclient"):
                zombies.append(proc)
        for proc in zombies:
            try:
                proc.kill()
                killed_zombie_pids.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed_zombie_pids:
            psutil.wait_procs(zombies, timeout=10)

    riot = find_riot_client()
    if not riot:
        return {
            "started": False,
            "method": "riot_remoting",
            "error": f"launch failed: {status} {body}; RiotClientServices.exe not found",
            "killed_zombie_pids": killed_zombie_pids,
        }
    # Cold start (no live Riot Client): "--app-command" only forwards to an
    # existing instance and exits when there is none, so remoting never comes
    # up. "--launch-product" boots the full Riot Client AND launches League
    # directly; wait_for_lcu_ready in the caller confirms actual health.
    args = [
        str(riot),
        "--launch-product=league_of_legends",
        "--launch-patchline=live",
    ]
    subprocess.Popen(args, cwd=str(riot.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # After a recycle, the cold-start CLI intent can arrive before Riot's
    # product launcher is ready and be dropped even though the UX later becomes
    # healthy. Once remoting appears, repeat the authoritative POST; 424 and
    # transport errors are transient only during that fresh startup, so retry
    # them briefly. Do not treat a pre-recycle 424 as transient.
    remoting_launch_status: int | str = "ERR"
    remoting_launch_body = "Riot remoting did not become ready after cold start"
    remoting_launch_attempts = 0
    if wait_for_riot_remoting(timeout_sec=60):
        for remoting_launch_attempts in range(1, 6):
            remoting_launch_status, remoting_launch_body = remoting_request(
                "/product-launcher/v1/products/league_of_legends/patchlines/live",
                method="POST",
                data=b"{}",
            )
            if remoting_launch_status == 200:
                break
            if remoting_launch_status not in ("ERR", 424):
                break
            time.sleep(2)
    return {
        "started": remoting_launch_status == 200,
        "method": "direct_launch_product_then_riot_remoting",
        "initial_launch_requested": True,
        "exe": str(riot),
        "killed_zombie_pids": killed_zombie_pids,
        "remoting_status_before_launch": status,
        "remoting_error_before_launch": body,
        "remoting_launch_status": remoting_launch_status,
        "remoting_launch_attempts": remoting_launch_attempts,
        "remoting_launch_error": (
            None if remoting_launch_status == 200 else remoting_launch_body
        ),
    }


def start_snowball_fleet(args: argparse.Namespace, worker_count: int) -> dict[str, Any]:
    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    out_path = args.log_dir / f"snowball_fleet_{stamp}.out.log"
    err_path = args.log_dir / f"snowball_fleet_{stamp}.err.log"
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "lcu_collector.py"),
        "snowball-workers",
        "--db",
        str(args.db),
        "--workers",
        str(max(1, int(worker_count))),
        "--control-file",
        str(args.db.parent / f".{args.db.name}.snowball.stop"),
        "--log-dir",
        str(args.log_dir),
        "--target-games",
        str(args.target_games),
        "--max-players",
        str(args.max_players),
        "--history-window",
        str(args.history_window),
        "--games-per-player",
        str(args.games_per_player),
        "--claim-timeout-sec",
        str(args.claim_timeout_sec),
        "--player-requeue-cooldown-sec",
        str(args.player_requeue_cooldown_sec),
        "--manual-seed-pending-cap",
        str(args.manual_seed_pending_cap),
        "--max-depth",
        str(args.max_depth),
        "--classic-claim-percent",
        str(args.classic_claim_percent),
        "--classic-revisit-min-hours",
        str(args.classic_revisit_min_hours),
        "--classic-revisit-max-hours",
        str(args.classic_revisit_max_hours),
        "--queue",
        "450",
        "--queue",
        "2400",
        # 2450 隨機單中大混戰：經典風 (KIWI, map 12) and 4310 經典 (JADE_RANKED_SOLO_5x5,
        # map 453) both launched 2026-07-30 ~02:15.  Collected opportunistically:
        # match history comes back unfiltered, so DISCOVERING them is free and only
        # the per-game detail fetch costs anything -- measured at ~1.7% of sampled
        # history (26 + 10 games across 99 players), so a negligible throughput hit
        # on Mayhem.  Worth taking now because the LCU keeps only ~20 games per
        # player: games not captured today cannot be back-filled later.
        "--queue",
        "2450",
        "--queue",
        "4310",
        "--seed-riot-id-file",
        str(args.seed_riot_id_file),
        "--seed-self",
        "--seed-friends",
        "--no-seed-ladder",
        "--no-seed-apex",
        "--no-seed-riot-tier",
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    with out_path.open("ab") as out, err_path.open("ab") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=out,
            stderr=err,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    return {
        "pid": proc.pid,
        "workers": max(1, int(worker_count)),
        "cmd": cmd,
        "stdout": str(out_path),
        "stderr": str(err_path),
    }


def ensure_static_site_publisher(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.site_publisher or args.once:
        return None
    active = static_site_publishers()
    if active:
        return {"action": "static_site_publisher_already_running", "publishers": active}

    args.static_publisher_log_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.static_publisher_log_dir / "static_publish.out.log"
    err_path = args.static_publisher_log_dir / "static_publish.err.log"
    cmd = [
        background_python_executable(),
        str(ROOT / "scripts" / "publish_static_site.py"),
        "--watch",
        "--threshold",
        str(args.static_publish_threshold),
        "--growth-ratio",
        str(args.static_publish_growth_ratio),
        "--max-age-hours",
        str(args.static_publish_max_age_hours),
        "--interval-sec",
        str(args.static_publish_interval_sec),
        "--db",
        str(args.db),
        "--state",
        str(args.static_publish_state),
        "--patch-prefix",
        args.static_publish_patch_prefix,
        "--auto-patch-min-games",
        str(args.static_publish_auto_patch_min_games),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with out_path.open("ab") as out, err_path.open("ab") as err:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=err, creationflags=creationflags)
    return {
        "action": "start_static_site_publisher",
        "pid": proc.pid,
        "cmd": cmd,
        "stdout": str(out_path),
        "stderr": str(err_path),
    }


def ensure_model_refresher(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.model_refresher or args.once:
        return None
    active = model_refreshers()
    if active:
        return {"action": "model_refresher_already_running", "refreshers": active}

    args.model_refresh_log_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.model_refresh_log_dir / "model_refresh.out.log"
    err_path = args.model_refresh_log_dir / "model_refresh.err.log"
    cmd = [
        background_python_executable(),
        # -u: stdout redirected to a file is block-buffered, which would let the
        # skip/BLOCKED lines sit in an 8KB buffer for hours.  An operator log
        # nobody can read in time is the failure mode this whole path exists to
        # prevent.
        "-u",
        str(ROOT / "scripts" / "refresh_recommender_models.py"),
        "--watch",
        "--threshold",
        str(args.model_refresh_threshold),
        "--growth-ratio",
        str(args.model_refresh_growth_ratio),
        "--interval-sec",
        str(args.model_refresh_interval_sec),
        "--min-current-games",
        str(args.model_refresh_min_current_games),
        "--db",
        str(args.db),
        "--state",
        str(args.model_refresh_state),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with out_path.open("ab") as out, err_path.open("ab") as err:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=err, creationflags=creationflags)
    return {
        "action": "start_model_refresher",
        "pid": proc.pid,
        "cmd": cmd,
        "stdout": str(out_path),
        "stderr": str(err_path),
    }


def latest_capture_age_min(db: Path) -> float | None:
    captured_at = latest_capture_at(db)
    if not captured_at:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return round((utc_now() - ts).total_seconds() / 60, 2)
    except ValueError:
        return None


def latest_capture_at(db: Path, queue_id: int = 2400) -> str | None:
    """Return the latest successful capture without scanning the wide games table."""
    if not db.exists():
        return None
    try:
        import sqlite3

        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            row = con.execute(
                "SELECT updated_at FROM crawl_runtime_state WHERE state_key = ?",
                (f"latest_capture:{int(queue_id)}",),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            # games is append-only and captured_at is assigned at insert time. Force
            # a reverse rowid walk so SQLite stops at the first matching recent row;
            # otherwise its low-cardinality queue index scans nearly every Mayhem
            # entry and touches the table's ~25 KB participant payloads.
            row = con.execute(
                "SELECT captured_at FROM games NOT INDEXED "
                "WHERE queue_id = ? ORDER BY rowid DESC LIMIT 1",
                (int(queue_id),),
            ).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def min_worker_uptime_min(workers: list[dict[str, Any]]) -> float:
    """Uptime (minutes) of the youngest snowball worker; 0.0 when unknown."""
    ages: list[float] = []
    now = time.time()
    for worker in workers:
        try:
            ages.append((now - psutil.Process(int(worker["pid"])).create_time()) / 60)
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, ValueError):
            continue
    return min(ages) if ages else 0.0


# In-memory stall tracker for watch mode; resets when the watchdog restarts.
_STALL_STATE = {"last_restart_monotonic": 0.0, "consecutive_restarts": 0}


def append_state(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def should_restart_client(args: argparse.Namespace, health: dict[str, Any], main_mb: float) -> tuple[bool, str]:
    # When LCU is down, gameflow has no phase; treat that like the idle "None" phase.
    phase = health.get("phase") or "None"
    if phase not in args.safe_restart_phase:
        return False, f"phase {phase!r} is not safe to restart"
    if main_mb >= args.client_restart_mb:
        return True, f"LeagueClient memory {main_mb:.1f}MB >= {args.client_restart_mb:.1f}MB"
    if not health["ok"]:
        return True, "LCU health check failed"
    return False, "client healthy enough"


def action_context(args: argparse.Namespace, health: dict[str, Any], main_mb: float) -> dict[str, Any]:
    return {
        "league_main_mb_at_action": main_mb,
        "degrade_client_mb": args.degrade_client_mb,
        "client_restart_mb": args.client_restart_mb,
        "worker_start_max_client_mb": args.worker_start_max_client_mb,
        "lcu_ok_at_action": health.get("ok"),
        "gameflow_phase_at_action": health.get("phase"),
        "current_summoner_status_at_action": health.get("current_summoner_status"),
        "gameflow_status_at_action": health.get("gameflow_status"),
    }


def wait_for_lcu_ready(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.client_ready_timeout_sec
    last_health: dict[str, Any] = {}
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        last_health = lcu_health()
        main_mb = league_main_mb()
        if last_health["ok"] and main_mb and main_mb <= args.worker_start_max_client_mb:
            return {"ready": True, "health": last_health, "league_main_mb": main_mb}
        append_state(
            args.state_file,
            {
                "ts": iso_now(),
                "league_main_mb": main_mb,
                "lcu": last_health,
                "workers": snowball_workers(),
                "latest_capture_age_min": latest_capture_age_min(args.db),
                "actions": [
                    {
                        "action": "wait_for_lcu_ready_progress",
                        "attempt": attempt,
                        "ready": False,
                    }
                ],
            },
        )
        time.sleep(args.check_interval_sec)
    return {"ready": False, "health": last_health, "league_main_mb": league_main_mb()}


def check_once(args: argparse.Namespace) -> dict[str, Any]:
    workers = snowball_workers()
    target_workers = max(1, int(args.workers))
    main_mb = league_main_mb()
    health = lcu_health()
    latest_age = latest_capture_age_min(args.db)
    actions: list[dict[str, Any]] = []
    publisher_action = ensure_static_site_publisher(args)
    if publisher_action:
        actions.append(publisher_action)
    refresher_action = ensure_model_refresher(args)
    if refresher_action:
        actions.append(refresher_action)
    refresh_health = model_refresh_health(args.model_refresh_state)
    if refresh_health.get("consecutive_failures"):
        # Report only -- the refresher keeps its own retry loop and the fix is
        # always an operator restoring an input, never a process restart.
        actions.append(
            {
                "action": "model_refresh_failing",
                "consecutive_failures": refresh_health["consecutive_failures"],
                "last_result": refresh_health.get("last_result"),
                "last_error": refresh_health.get("last_error"),
            }
        )

    if workers and not health["ok"]:
        stopped = stop_snowball_workers()
        workers = snowball_workers()
        actions.append(
            {
                "action": "stop_workers_lcu_unhealthy",
                "pids": stopped,
                **action_context(args, health, main_mb),
            }
        )
    elif (
        workers
        and target_workers > args.degraded_workers
        and main_mb >= args.degrade_client_mb
        and main_mb < args.client_restart_mb
        and health["ok"]
    ):
        target_workers = max(1, int(args.degraded_workers))
        stopped = stop_snowball_workers(grace_sec=10)
        workers = snowball_workers()
        actions.append(
            {
                "action": "degrade_workers",
                "pids": stopped,
                "reason": f"LeagueClient memory {main_mb:.1f}MB >= {args.degrade_client_mb:.1f}MB",
                "target_workers": args.degraded_workers,
                **action_context(args, health, main_mb),
            }
        )

    # Zero-throughput stall: workers alive and LCU "healthy", yet no new 2400
    # capture for worker-stall-min while the youngest worker has been up at
    # least that long (fake-healthy hang: PID alive + LCU 200 but blocked call).
    stall_force_client_restart = False
    if args.worker_stall_min > 0 and latest_age is not None:
        if latest_age < args.worker_stall_min:
            _STALL_STATE["consecutive_restarts"] = 0
        elif (
            workers
            and health["ok"]
            and min_worker_uptime_min(workers) >= args.worker_stall_min
            and time.monotonic() - _STALL_STATE["last_restart_monotonic"]
            >= args.worker_stall_cooldown_sec
        ):
            stopped = stop_snowball_workers()
            workers = snowball_workers()
            _STALL_STATE["last_restart_monotonic"] = time.monotonic()
            _STALL_STATE["consecutive_restarts"] += 1
            actions.append(
                {
                    "action": "restart_workers_capture_stalled",
                    "pids": stopped,
                    "latest_capture_age_min": latest_age,
                    "consecutive_stall_restarts": _STALL_STATE["consecutive_restarts"],
                    **action_context(args, health, main_mb),
                }
            )
            if _STALL_STATE["consecutive_restarts"] >= args.worker_stall_client_restart_after:
                stall_force_client_restart = True

    restart, restart_reason = should_restart_client(args, health, main_mb)
    if not restart and stall_force_client_restart:
        phase = health.get("phase") or "None"
        if phase in args.safe_restart_phase:
            restart = True
            restart_reason = (
                f"capture stalled {latest_age:.0f}min despite "
                f"{_STALL_STATE['consecutive_restarts']} worker restarts"
            )
    if args.restart_client and restart:
        _STALL_STATE["consecutive_restarts"] = 0
        if workers:
            stopped = stop_snowball_workers()
            workers = snowball_workers()
            actions.append(
                {
                    "action": "stop_workers_before_client_restart",
                    "pids": stopped,
                    "reason": restart_reason,
                    **action_context(args, health, main_mb),
                }
            )
        closed = close_league_client()
        started = start_league_client()
        actions.append(
            {
                "action": "restart_league_client",
                "reason": restart_reason,
                "closed_pids": closed,
                "start": started,
                **action_context(args, health, main_mb),
            }
        )
        append_state(
            args.state_file,
            {
                "ts": iso_now(),
                "league_main_mb": main_mb,
                "lcu": health,
                "workers": snowball_workers(),
                "latest_capture_age_min": latest_age,
                "actions": actions,
            },
        )
        ready = wait_for_lcu_ready(args)
        health = ready.get("health") or lcu_health()
        main_mb = float(ready.get("league_main_mb") or league_main_mb())
        actions.append({"action": "wait_for_lcu_ready", **ready})

    workers = snowball_workers()
    if (
        not workers
        and health["ok"]
        and 0 < main_mb <= args.worker_start_max_client_mb
    ):
        started_fleet = start_snowball_fleet(args, target_workers)
        actions.append({"action": "start_fleet", **started_fleet})
        workers = snowball_workers()
    elif not workers and health["ok"] and main_mb > args.worker_start_max_client_mb:
        actions.append(
            {
                "action": "keep_worker_stopped",
                "reason": f"LeagueClient memory {main_mb:.1f}MB > start max {args.worker_start_max_client_mb:.1f}MB",
                **action_context(args, health, main_mb),
            }
        )
    elif len(workers) > 1:
        stopped = stop_snowball_workers(grace_sec=10)
        actions.append(
            {
                "action": "stop_duplicate_fleets",
                "pids": stopped,
                "reason": f"active fleet supervisors {len(workers)} > 1",
            }
        )

    record = {
        "ts": iso_now(),
        "league_main_mb": main_mb,
        "lcu": health,
        "workers": snowball_workers(),
        "static_site_publishers": static_site_publishers(),
        "model_refreshers": model_refreshers(),
        "model_refresh_health": refresh_health,
        "latest_capture_age_min": latest_age,
        "actions": actions,
    }
    append_state(args.state_file, record)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch and recover the local Mayhem LCU crawler.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed-riot-id-file", type=Path, default=DEFAULT_SEED_FILE)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--check-interval-sec", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--restart-client", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--degraded-workers", type=int, default=1)
    parser.add_argument("--degrade-client-mb", type=float, default=4500.0)
    parser.add_argument("--client-restart-mb", type=float, default=6000.0)
    parser.add_argument("--worker-stop-client-mb", type=float, default=5000.0)
    parser.add_argument("--worker-start-max-client-mb", type=float, default=3500.0)
    # Zero-throughput stall recovery: restart workers when no new 2400 capture
    # for this many minutes while workers look alive (0 disables).
    parser.add_argument("--worker-stall-min", type=float, default=30.0)
    parser.add_argument("--worker-stall-cooldown-sec", type=int, default=900)
    # After this many consecutive stall restarts without recovery, restart the
    # League client too (safe phases only).
    parser.add_argument("--worker-stall-client-restart-after", type=int, default=3)
    parser.add_argument("--client-ready-timeout-sec", type=int, default=600)
    parser.add_argument("--safe-restart-phase", action="append", default=["None", "EndOfGame"])
    parser.add_argument("--target-games", type=int, default=50000)
    parser.add_argument("--max-players", type=int, default=50000)
    parser.add_argument("--history-window", type=int, default=20)
    parser.add_argument(
        "--games-per-player",
        type=int,
        default=0,
        help="Target-queue games per player; 0 = adaptive 4-row probe (3+ Mayhem expands full window)",
    )
    parser.add_argument("--claim-timeout-sec", type=int, default=300)
    parser.add_argument("--player-requeue-cooldown-sec", type=int, default=45)
    parser.add_argument("--manual-seed-pending-cap", type=int, default=40)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--classic-claim-percent", type=int, default=10)
    parser.add_argument("--classic-revisit-min-hours", type=float, default=10.0)
    parser.add_argument("--classic-revisit-max-hours", type=float, default=168.0)
    parser.add_argument("--site-publisher", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--static-publish-threshold", type=int, default=0)
    parser.add_argument("--static-publish-growth-ratio", type=float, default=0.10)
    parser.add_argument("--static-publish-max-age-hours", type=float, default=12.0)
    parser.add_argument("--static-publish-interval-sec", type=int, default=300)
    parser.add_argument("--static-publish-patch-prefix", default="auto")
    # 10,000: gated by teammate-synergy coverage, not by win-rate accuracy.  See the
    # measured coverage table in watchdog_keepalive.ps1 before changing this.
    parser.add_argument("--static-publish-auto-patch-min-games", type=int, default=10_000)
    parser.add_argument("--static-publish-state", type=Path, default=DEFAULT_STATIC_PUBLISH_STATE)
    parser.add_argument("--static-publisher-log-dir", type=Path, default=DEFAULT_STATIC_PUBLISH_LOG_DIR)
    parser.add_argument("--model-refresher", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model-refresh-threshold", type=int, default=0)
    parser.add_argument("--model-refresh-growth-ratio", type=float, default=0.25)
    parser.add_argument("--model-refresh-interval-sec", type=int, default=600)
    parser.add_argument("--model-refresh-min-current-games", type=int, default=15000)
    parser.add_argument("--model-refresh-state", type=Path, default=DEFAULT_MODEL_REFRESH_STATE)
    parser.add_argument("--model-refresh-log-dir", type=Path, default=DEFAULT_MODEL_REFRESH_LOG_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.db = args.db.resolve()
    args.seed_riot_id_file = args.seed_riot_id_file.resolve()
    args.log_dir = args.log_dir.resolve()
    args.state_file = args.state_file.resolve()
    args.static_publish_state = args.static_publish_state.resolve()
    args.static_publisher_log_dir = args.static_publisher_log_dir.resolve()
    args.model_refresh_state = args.model_refresh_state.resolve()
    args.model_refresh_log_dir = args.model_refresh_log_dir.resolve()

    while True:
        record = check_once(args)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(args.check_interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
