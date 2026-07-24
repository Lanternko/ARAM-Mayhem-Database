"""Post Mayhem LCU crawler status to a Discord webhook.

Webhook URL resolution order:
  1. --webhook CLI flag
  2. DISCORD_CRAWLER_WEBHOOK env var
  3. data/monitor/discord_crawler_webhook.txt  (gitignored under data/)

Usage:
  python scripts/crawler_status_discord.py
  python scripts/crawler_status_discord.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "lcu" / "games.db"
DEFAULT_STATE = ROOT / "data" / "monitor" / "mayhem_lcu_watchdog.jsonl"
DEFAULT_WEBHOOK_FILE = ROOT / "data" / "monitor" / "discord_crawler_webhook.txt"
DEFAULT_LOG_DIR = ROOT / ".codex" / "logs" / "mayhem_lcu_watchdog"
# Static-site publisher artifacts.  The state file's last_publish_at_unix only
# advances on a *successful* publish; the err log grows on every crash.
DEFAULT_PUBLISH_STATE = ROOT / "data" / "site" / "static_publish_state.json"
DEFAULT_PUBLISH_ERR_LOG = ROOT / "data" / "site" / "static_publish.err.log"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def resolve_webhook(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("DISCORD_CRAWLER_WEBHOOK", "").strip()
    if env:
        return env
    if DEFAULT_WEBHOOK_FILE.exists():
        text = DEFAULT_WEBHOOK_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise SystemExit(
        "No Discord webhook configured. Pass --webhook, set DISCORD_CRAWLER_WEBHOOK, "
        f"or write the URL to {DEFAULT_WEBHOOK_FILE}"
    )


def is_snowball_worker(proc: psutil.Process) -> bool:
    try:
        name = (proc.name() or "").lower()
        cmdline = proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name not in ("python.exe", "pythonw.exe") or len(cmdline) < 2:
        return False
    joined = " ".join(str(p).replace("/", "\\") for p in cmdline)
    return r"scripts\lcu_collector.py" in joined and "snowball" in cmdline


def is_watchdog(proc: psutil.Process) -> bool:
    try:
        name = (proc.name() or "").lower()
        cmdline = proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name not in ("python.exe", "pythonw.exe"):
        return False
    joined = " ".join(str(p).replace("/", "\\") for p in cmdline)
    return "mayhem_lcu_watchdog.py" in joined


def worker_id(cmdline: list[str]) -> str:
    try:
        return cmdline[cmdline.index("--worker-id") + 1]
    except (ValueError, IndexError):
        return "?"


def live_workers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "memory_info"]):
        try:
            p = psutil.Process(proc.info["pid"])
            if not is_snowball_worker(p):
                continue
            cmdline = list(proc.info.get("cmdline") or [])
            rss = proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0
            uptime_min = (time.time() - float(proc.info.get("create_time") or time.time())) / 60
            rows.append(
                {
                    "pid": proc.info["pid"],
                    "worker_id": worker_id(cmdline),
                    "rss_mb": round(rss / 1024 / 1024, 1),
                    "uptime_min": round(uptime_min, 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["worker_id"])
    return rows


def live_watchdog() -> dict[str, Any] | None:
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            p = psutil.Process(proc.info["pid"])
            if not is_watchdog(p):
                continue
            uptime_min = (time.time() - float(proc.info.get("create_time") or time.time())) / 60
            return {"pid": proc.info["pid"], "uptime_min": round(uptime_min, 1)}
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            continue
    return None


def league_main_mb() -> float:
    vals: list[float] = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            if (proc.info.get("name") or "").lower() != "leagueclient.exe":
                continue
            mem = proc.info.get("memory_info")
            if mem:
                vals.append(mem.rss / 1024 / 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return round(max(vals), 1) if vals else 0.0


def latest_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        # Read last ~64KB and parse the final JSON object.
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            chunk = handle.read().decode("utf-8", "replace")
        lines = [ln for ln in chunk.splitlines() if ln.strip().startswith("{")]
        if not lines:
            return None
        return json.loads(lines[-1])
    except (OSError, json.JSONDecodeError):
        return None


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts
    except ValueError:
        return None


def db_stats(db: Path, window_hours: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "total_mayhem": None,
        "window_saves": None,
        "latest_captured_at": None,
        "latest_age_min": None,
        "window_hours": window_hours,
    }
    if not db.exists():
        out["error"] = "db missing"
        return out
    try:
        # Read-only URI avoids interfering with live writers when possible.
        uri = db.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            con.execute("pragma query_only=on")
        except sqlite3.Error:
            pass
        total = con.execute("select count(*) from games where queue_id=2400").fetchone()[0]
        latest = con.execute(
            "select max(captured_at) from games where queue_id=2400"
        ).fetchone()[0]
        # captured_at is ISO text; lexical compare works for zero-padded ISO-8601.
        cutoff = (utc_now() - dt.timedelta(hours=window_hours)).isoformat()
        window = con.execute(
            "select count(*) from games where queue_id=2400 and captured_at >= ?",
            (cutoff,),
        ).fetchone()[0]
        con.close()
    except Exception as exc:  # noqa: BLE001 — status probe must never crash the report
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["ok"] = True
    out["total_mayhem"] = int(total)
    out["window_saves"] = int(window)
    out["latest_captured_at"] = latest
    latest_ts = parse_iso(str(latest) if latest else None)
    if latest_ts:
        out["latest_age_min"] = round((utc_now() - latest_ts).total_seconds() / 60, 2)
    return out


def publish_status(repo_root: Path, stale_hours: float) -> dict[str, Any]:
    """Local commits that never reached origin — the silent auto-publish failure.

    Deliberately offline: it compares HEAD against the *cached* origin/main ref,
    which only advances on a successful push.  A live `git fetch` would be worse
    than useless here — the failure mode this detects (watchdog push blocked on a
    credential prompt) would hang the fetch on that same prompt.

    Symptom this catches: commits pile up locally, the site never updates, and
    nothing else in this report looks wrong (the crawler keeps saving games fine).
    """
    out: dict[str, Any] = {
        "ok": False,
        "ahead": None,
        "oldest_unpushed_age_h": None,
        "stale": False,
        "stale_hours": stale_hours,
    }

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
        )

    try:
        head = git("rev-list", "--count", "origin/main..HEAD")
        if head.returncode != 0:
            out["error"] = (head.stderr or "").strip()[:200] or "rev-list failed"
            return out
        ahead = int((head.stdout or "0").strip() or 0)
        out["ahead"] = ahead
        out["ok"] = True
        if ahead > 0:
            # Committer timestamps of the unpushed commits; the oldest one is how
            # long the publish leg has actually been broken.
            log = git("log", "--format=%ct", f"-{ahead}", "HEAD")
            stamps = [int(x) for x in (log.stdout or "").split() if x.strip().isdigit()]
            if stamps:
                age_h = (time.time() - min(stamps)) / 3600.0
                out["oldest_unpushed_age_h"] = round(age_h, 1)
                out["stale"] = age_h >= stale_hours
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def site_publish_status(
    state_file: Path,
    err_log: Path,
    stale_hours: float,
    crash_window_min: float = 15.0,
) -> dict[str, Any]:
    """Health of the static-site auto-publisher — the leg that turns fresh DB
    games into a rebuilt site.

    publish_status() (above) watches for commits that don't *push*.  This is the
    complementary failure the 2026-07-25 outage exposed: the publisher
    crash-looped on a dirty docs/api/tier-list.json and never reached the commit
    step at all, so publish_status stayed green while the site rotted 3 days.

    Two independent signals, both cheap (no DB scan, no git):
      * last_publish_age_h — from ``last_publish_at_unix`` in the state file,
        which advances ONLY on a successful publish.  Stale while the crawler is
        still producing games == publishing is stuck.
      * crashing — the err log was written within ``crash_window_min`` AND its
        tail carries a traceback.  Catches an active crash-loop immediately,
        before the staleness threshold trips.
    """
    out: dict[str, Any] = {
        "ok": False,
        "last_publish_age_h": None,
        "last_published_total": None,
        "stale": False,
        "crashing": False,
        "stale_hours": stale_hours,
    }
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            ts = state.get("last_publish_at_unix")
            if isinstance(ts, (int, float)) and ts > 0:
                out["ok"] = True
                age_h = (time.time() - float(ts)) / 3600.0
                out["last_publish_age_h"] = round(age_h, 1)
                out["last_published_total"] = state.get("last_published_total")
                out["stale"] = age_h >= stale_hours
        # An active crash-loop: err log touched very recently with a traceback in
        # its tail.  Stale crashes (old mtime) are ignored — the publisher may
        # have recovered since.
        if err_log.exists():
            age_min = (time.time() - err_log.stat().st_mtime) / 60.0
            if age_min <= crash_window_min:
                tail = read_text_shared(err_log, max_bytes=4000)
                if "Traceback" in tail or "Error" in tail:
                    out["crashing"] = True
                    last = [ln for ln in tail.splitlines() if ln.strip()]
                    if last:
                        out["last_error"] = last[-1][:200]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def current_run_logs(log_dir: Path) -> list[Path]:
    if not log_dir.exists():
        return []
    # One newest log per worker id (W01, W02, ...).
    best: dict[str, Path] = {}
    for path in log_dir.glob("snowball_W*_*.out.log"):
        m = re.match(r"snowball_(W\d+)_", path.name)
        if not m:
            continue
        wid = m.group(1)
        prev = best.get(wid)
        if prev is None or path.stat().st_mtime > prev.stat().st_mtime:
            best[wid] = path
    return [best[k] for k in sorted(best)]


def read_text_shared(path: Path, max_bytes: int = 512_000) -> str:
    """Best-effort read of a log still open by a worker (Windows share mode)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def worker_log_stats(log_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in current_run_logs(log_dir):
        text = read_text_shared(path)
        saves = len(re.findall(r"\[saved\] Mayhem", text))
        totals = re.findall(r"total_saved=(\d+)", text)
        players = re.findall(r"\[snowball\] player\s+(\d+)/", text)
        patches = re.findall(r"\[saved\] Mayhem.*?patch=([\d.]+)", text)
        patch_counts: dict[str, int] = {}
        for patch in patches[-200:]:
            patch_counts[patch] = patch_counts.get(patch, 0) + 1
        m = re.match(r"snowball_(W\d+)_", path.name)
        rows.append(
            {
                "worker_id": m.group(1) if m else path.name,
                "log": path.name,
                "saves_in_log_tail": saves,
                "total_saved": int(totals[-1]) if totals else None,
                "player_step": int(players[-1]) if players else None,
                "patch_mix_recent": patch_counts,
                "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat(),
            }
        )
    return rows


def health_color(
    workers: int,
    lcu_ok: bool | None,
    age_min: float | None,
    publish_stale: bool = False,
    site_publish_bad: bool = False,
) -> int:
    # Discord embed colors
    green = 0x57F287
    yellow = 0xFEE75C
    red = 0xED4245
    if not lcu_ok or workers <= 0:
        return red
    if age_min is not None and age_min > 30:
        return yellow
    # Crawling can be perfectly healthy while the publish leg is dead — that is
    # how both the 2026-07-21 (push blocked) and 2026-07-25 (publisher
    # crash-loop) outages looked, so neither must stay green.
    if publish_stale or site_publish_bad:
        return yellow
    return green


def build_status(
    db: Path,
    state_file: Path,
    log_dir: Path,
    window_hours: int,
    publish_stale_hours: float = 3.0,
    site_publish_stale_hours: float = 12.0,
    repo_root: Path = ROOT,
    publish_state_file: Path = DEFAULT_PUBLISH_STATE,
    publish_err_log: Path = DEFAULT_PUBLISH_ERR_LOG,
) -> dict[str, Any]:
    workers = live_workers()
    watchdog = live_watchdog()
    league_mb = league_main_mb()
    state = latest_state(state_file)
    stats = db_stats(db, window_hours=window_hours)
    logs = worker_log_stats(log_dir)
    publish = publish_status(repo_root, stale_hours=publish_stale_hours)
    site_publish = site_publish_status(
        publish_state_file, publish_err_log, stale_hours=site_publish_stale_hours
    )

    lcu = (state or {}).get("lcu") or {}
    lcu_ok = lcu.get("ok") if state else None
    phase = lcu.get("phase") if state else None
    state_age = None
    state_ts = parse_iso((state or {}).get("ts"))
    if state_ts:
        state_age = round((utc_now() - state_ts).total_seconds() / 60, 1)

    age = stats.get("latest_age_min")
    if age is None and state is not None:
        age = state.get("latest_capture_age_min")

    # Merge patch mix across workers
    patch_mix: dict[str, int] = {}
    for row in logs:
        for patch, n in (row.get("patch_mix_recent") or {}).items():
            patch_mix[patch] = patch_mix.get(patch, 0) + int(n)

    return {
        "ts": utc_now().isoformat(),
        "watchdog": watchdog,
        "workers_live": workers,
        "worker_count": len(workers),
        "league_main_mb": league_mb,
        "lcu_ok": lcu_ok,
        "phase": phase,
        "state_ts": (state or {}).get("ts"),
        "state_age_min": state_age,
        "capture_age_min": age,
        "db": stats,
        "publish": publish,
        "site_publish": site_publish,
        "worker_logs": logs,
        "patch_mix_recent": dict(sorted(patch_mix.items(), key=lambda kv: (-kv[1], kv[0]))),
        "window_hours": window_hours,
    }


def format_message(status: dict[str, Any]) -> dict[str, Any]:
    workers = status["worker_count"]
    lcu_ok = status.get("lcu_ok")
    age = status.get("capture_age_min")
    publish = status.get("publish") or {}
    publish_stale = bool(publish.get("stale"))
    site_publish = status.get("site_publish") or {}
    # Crawler must be producing for staleness to mean "stuck" vs "legitimately
    # idle" (a quiet patch can sit below the +10% publish gate for a while).
    crawler_producing = isinstance(window_saves_probe := (status.get("db") or {}).get("window_saves"), int) and window_saves_probe > 0
    site_crashing = bool(site_publish.get("crashing"))
    site_stale = bool(site_publish.get("stale")) and crawler_producing
    site_publish_bad = site_crashing or site_stale
    color = health_color(
        workers, lcu_ok if isinstance(lcu_ok, bool) else None, age,
        publish_stale, site_publish_bad,
    )

    if site_crashing:
        headline = "爬蟲正常但發布器崩潰中"
    elif publish_stale:
        headline = "爬蟲正常但發布卡住"
    elif site_stale:
        headline = "爬蟲正常但網站太久沒更新"
    elif workers >= 2 and lcu_ok and (age is None or age < 5):
        headline = "運作正常"
    elif workers >= 1 and lcu_ok:
        headline = "爬蟲有在跑（請留意收場間隔）"
    elif workers >= 1:
        headline = "Worker 還在，LCU 不健康"
    else:
        headline = "爬蟲已停止"

    db = status.get("db") or {}
    window_h = status.get("window_hours") or 6
    window_saves = db.get("window_saves")
    rate = None
    if isinstance(window_saves, int) and window_h > 0:
        rate = round(window_saves / float(window_h), 1)

    lcu_label = "正常" if lcu_ok is True else ("異常" if lcu_ok is False else "未知")
    phase = status.get("phase")
    phase_label = "閒置" if phase in (None, "None") else str(phase)

    worker_lines = []
    for w in status.get("workers_live") or []:
        worker_lines.append(
            f"`{w['worker_id']}` pid={w['pid']} 已跑 {w['uptime_min']} 分 · {w['rss_mb']} MB"
        )
    if not worker_lines:
        worker_lines = ["_（無）_"]

    log_lines = []
    for row in status.get("worker_logs") or []:
        total = row.get("total_saved")
        step = row.get("player_step")
        log_lines.append(
            f"`{row['worker_id']}` 本輪已存 {total if total is not None else '?'} 場"
            f" · 掃到第 {step if step is not None else '?'} 人"
        )

    # Prefer short patch label: 16.14.794 -> 16.14
    def short_patch(patch: str) -> str:
        parts = str(patch).split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else str(patch)

    raw_mix = list((status.get("patch_mix_recent") or {}).items())
    # Collapse full builds into major.patch (sum counts)
    collapsed: dict[str, int] = {}
    for patch, n in raw_mix:
        key = short_patch(patch)
        collapsed[key] = collapsed.get(key, 0) + int(n)
    ordered = sorted(collapsed.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    total_mix = sum(n for _, n in ordered) or 0
    if ordered:
        patch_lines = []
        for patch, n in ordered:
            pct = round(100.0 * n / total_mix) if total_mix else 0
            patch_lines.append(f"**{patch}**　{n} 場（{pct}%）")
        top = ordered[0][0]
        patch_text = (
            f"近端 log 抽樣，依遊戲版本分類（不是累積總量）\n"
            f"主力版本：**{top}**\n" + "\n".join(patch_lines)
        )
    else:
        patch_text = "—"

    wd = status.get("watchdog")
    wd_text = (
        f"pid={wd['pid']} · 已跑 {wd['uptime_min']} 分" if wd else "未運行"
    )

    ahead = publish.get("ahead")
    unpushed_age = publish.get("oldest_unpushed_age_h")
    if publish.get("error"):
        publish_text = f"無法判斷（{str(publish['error'])[:80]}）"
    elif not publish.get("ok"):
        publish_text = "無法判斷"
    elif not ahead:
        publish_text = "已同步 ✅"
    elif publish_stale:
        publish_text = (
            f"⚠️ **{ahead}** 個 commit 未上線\n卡了 **{unpushed_age}** 小時"
            f"（門檻 {publish.get('stale_hours')}h）\n多半是 push 卡在認證視窗"
        )
    else:
        publish_text = f"**{ahead}** 個 commit 待推\n最舊 {unpushed_age} 小時"

    # Static-site publisher (rebuild leg) — separate from the git push leg above.
    sp_age = site_publish.get("last_publish_age_h")
    sp_total = site_publish.get("last_published_total")
    if site_publish.get("error"):
        site_publish_text = f"無法判斷（{str(site_publish['error'])[:80]}）"
    elif site_crashing:
        err = site_publish.get("last_error") or ""
        site_publish_text = (
            f"🔴 發布器崩潰中\n上次成功發布 **{sp_age}** 小時前\n`{err[:90]}`"
        )
    elif site_stale:
        site_publish_text = (
            f"⚠️ 已 **{sp_age}** 小時沒發布（門檻 {site_publish.get('stale_hours')}h）\n"
            f"crawler 仍在收場 → 發布器可能卡住"
        )
    elif not site_publish.get("ok"):
        site_publish_text = "無發布記錄"
    else:
        total_txt = f"（上次 {sp_total} 場）" if sp_total else ""
        site_publish_text = f"上次發布 **{sp_age}** 小時前 {total_txt}"

    fields = [
        {
            "name": "LCU / 客戶端",
            "value": (
                f"LCU：`{lcu_label}` · 階段：`{phase_label}`\n"
                f"LeagueClient：`{status.get('league_main_mb')} MB`"
            ),
            "inline": True,
        },
        {
            "name": "Workers",
            "value": f"**{workers}** 個在線\n" + "\n".join(worker_lines),
            "inline": True,
        },
        {
            "name": "Watchdog",
            "value": wd_text,
            "inline": True,
        },
        {
            "name": f"近 {window_h} 小時 Mayhem 新增",
            "value": (
                f"**{window_saves if window_saves is not None else '?'}** 場"
                + (f"  （約 {rate} 場/時）" if rate is not None else "")
            ),
            "inline": True,
        },
        {
            "name": "距上次收場",
            "value": f"**{age if age is not None else '?'}** 分鐘",
            "inline": True,
        },
        {
            "name": "資料庫 Mayhem 總場",
            "value": f"{db.get('total_mayhem') if db.get('total_mayhem') is not None else '?'}",
            "inline": True,
        },
        {
            "name": "Git 推送",
            "value": publish_text,
            "inline": True,
        },
        {
            "name": "網站重建",
            "value": site_publish_text,
            "inline": True,
        },
        {
            "name": "本輪計數",
            "value": "\n".join(log_lines) if log_lines else "—",
            "inline": False,
        },
        {
            "name": "版本分佈（抽樣）",
            "value": patch_text,
            "inline": False,
        },
    ]

    if db.get("error"):
        fields.append(
            {"name": "資料庫探測錯誤", "value": str(db["error"])[:500], "inline": False}
        )

    embed = {
        "title": f"ARAM 大亂鬥爬蟲 · {headline}",
        "color": color,
        "timestamp": status.get("ts"),
        "fields": fields,
        "footer": {"text": "arammeta 爬蟲狀態（每 6 小時）"},
    }
    return {
        "username": "arammeta 爬蟲",
        "embeds": [embed],
    }


def post_webhook(url: str, payload: dict[str, Any], timeout_sec: float = 20.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "arammeta-crawler-status/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        return exc.code, body


def main(argv: list[str] | None = None) -> int:
    # The report text is Chinese + a couple of status emoji; a cp950 console
    # (Windows default here) raises UnicodeEncodeError on the emoji and takes
    # --dry-run down with it.  The webhook payload itself is always UTF-8 JSON.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook", default=None, help="Discord webhook URL (prefer file/env)")
    parser.add_argument("--webhook-file", type=Path, default=DEFAULT_WEBHOOK_FILE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--window-hours", type=int, default=6)
    parser.add_argument(
        "--publish-stale-hours",
        type=float,
        default=3.0,
        help="Unpushed commits older than this flag the report yellow",
    )
    parser.add_argument(
        "--site-publish-stale-hours",
        type=float,
        default=12.0,
        help="Hours since last successful site publish before flagging yellow "
        "(only while the crawler is still producing games)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print payload, do not POST")
    parser.add_argument("--save-webhook", action="store_true", help="Write --webhook to webhook file")
    args = parser.parse_args(argv)

    if args.save_webhook:
        if not args.webhook:
            raise SystemExit("--save-webhook requires --webhook")
        args.webhook_file.parent.mkdir(parents=True, exist_ok=True)
        args.webhook_file.write_text(args.webhook.strip() + "\n", encoding="utf-8")
        print(f"saved webhook to {args.webhook_file}")

    # Prefer file path if user passed a custom webhook-file via env resolution.
    if args.webhook_file != DEFAULT_WEBHOOK_FILE and args.webhook_file.exists() and not args.webhook:
        os.environ.setdefault(
            "DISCORD_CRAWLER_WEBHOOK",
            args.webhook_file.read_text(encoding="utf-8").strip(),
        )

    status = build_status(
        db=args.db,
        state_file=args.state_file,
        log_dir=args.log_dir,
        window_hours=args.window_hours,
        publish_stale_hours=args.publish_stale_hours,
        site_publish_stale_hours=args.site_publish_stale_hours,
    )
    payload = format_message(status)

    if args.dry_run:
        print(json.dumps({"status": status, "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    webhook = resolve_webhook(args.webhook)
    code, body = post_webhook(webhook, payload)
    if 200 <= code < 300:
        print(f"ok discord_status={code} workers={status['worker_count']} capture_age={status.get('capture_age_min')}")
        return 0
    print(f"discord POST failed status={code} body={body[:500]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
