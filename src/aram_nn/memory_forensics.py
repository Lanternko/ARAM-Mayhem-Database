"""Per-process memory snapshots taken while the resource guard sees pressure.

The resource guard only records system totals, so a pause says *that* commit
hit its limit but not *who* holds it.  The 2026-09-24 two-hour pause (commit
pinned at ~64/65 GB) could not be attributed afterwards because nothing listed
processes during the window.  This module writes that list while it happens.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import psutil

MB = 1024 * 1024


def _private_bytes(info: Any) -> int:
    # On Windows `private` is the pagefile-backed commit charge, which is the
    # quantity the guard pauses on.  Elsewhere fall back to RSS.
    return int(getattr(info, "private", 0) or info.rss)


def top_memory_consumers(
    limit: int = 15,
    processes: Iterable[psutil.Process] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """Group processes by name and return the largest by private commit.

    Returns (groups, total_private_mb) where the total covers every readable
    process, so callers can compare it with system commit to see how much is
    held outside user processes (kernel, drivers, VMs).
    """

    groups: dict[str, dict[str, Any]] = {}
    total = 0
    source = processes if processes is not None else psutil.process_iter(["name"])
    for proc in source:
        try:
            name = proc.info.get("name") if hasattr(proc, "info") else proc.name()
            info = proc.memory_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        private = _private_bytes(info)
        total += private
        group = groups.setdefault(
            name or "?",
            {"name": name or "?", "count": 0, "private": 0, "rss": 0, "top_pid": None, "top_private": -1},
        )
        group["count"] += 1
        group["private"] += private
        group["rss"] += int(info.rss)
        if private > group["top_private"]:
            group["top_private"] = private
            group["top_pid"] = proc.pid

    ranked = sorted(groups.values(), key=lambda g: g["private"], reverse=True)[:limit]
    return (
        [
            {
                "name": g["name"],
                "count": g["count"],
                "private_mb": round(g["private"] / MB, 1),
                "rss_mb": round(g["rss"] / MB, 1),
                "top_pid": g["top_pid"],
                "top_pid_private_mb": round(g["top_private"] / MB, 1),
            }
            for g in ranked
        ],
        round(total / MB, 1),
    )


class MemoryPressureRecorder:
    """Write throttled process snapshots while system pressure persists."""

    def __init__(
        self,
        out_dir: Path,
        interval_sec: float = 600.0,
        keep_files: int = 300,
        clock: Callable[[], float] = time.time,
        sampler: Callable[[], tuple[list[dict[str, Any]], float]] = top_memory_consumers,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.interval_sec = interval_sec
        self.keep_files = keep_files
        self._clock = clock
        self._sampler = sampler
        self._last_at: float | None = None

    def maybe_capture(self, decision: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any] | None:
        """Snapshot on the first pressured sample, then at most once per interval.

        Only system pressure (commit / available RAM) counts: LeagueClient-only
        pressure already names its culprit in the watchdog record.
        """

        if not decision.get("system_pressure"):
            self._last_at = None
            return None
        now = self._clock()
        if self._last_at is not None and now - self._last_at < self.interval_sec:
            return None
        self._last_at = now

        groups, total_private_mb = self._sampler()
        commit_mb = sample.get("commit_total_mb")
        stamp = dt.datetime.fromtimestamp(now, dt.UTC)
        record = {
            "ts": stamp.isoformat(),
            "guard_state": decision.get("state"),
            "sample": sample,
            "process_private_total_mb": total_private_mb,
            # Commit charged outside user-mode private bytes: kernel pools,
            # drivers, VM memory, shared sections.  A large value points away
            # from any single app.
            "unattributed_commit_mb": (
                round(float(commit_mb) - total_private_mb, 1) if commit_mb is not None else None
            ),
            "top_processes": groups,
        }
        path = self.out_dir / f"memory_pressure_{stamp:%Y%m%dT%H%M%SZ}.json"
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            self._prune()
        except OSError as exc:
            path = None
            record["write_error"] = f"{type(exc).__name__}: {exc}"
        return {
            "action": "memory_pressure_snapshot",
            "path": str(path) if path else None,
            "unattributed_commit_mb": record["unattributed_commit_mb"],
            "top": [f"{g['name']}x{g['count']}={g['private_mb']:.0f}MB" for g in groups[:5]],
            **({"write_error": record["write_error"]} if "write_error" in record else {}),
        }

    def _prune(self) -> None:
        files = sorted(self.out_dir.glob("memory_pressure_*.json"))
        for stale in files[: max(0, len(files) - self.keep_files)]:
            stale.unlink(missing_ok=True)
