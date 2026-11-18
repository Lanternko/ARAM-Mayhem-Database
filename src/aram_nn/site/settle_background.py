"""Settle closed patches in the background, in resumable time slices.

The tier-list build no longer scans a closed patch that has no usable snapshot
(see ``tierlist_engine.set_settle_deferral``): it publishes without it and asks
this module to settle the patch afterwards.  The work is split in two layers:

* a detached **worker** (``settle_patch.py --background``), one per machine,
  that loops until every requested patch is settled;
* short **slices** (``settle_patch.py --resumable --max-seconds N``) the worker
  runs one at a time under the shared heavy-job lock.

A slice checkpoints every folded window (``chunk_cache.windowed_fold``) and
exits with ``EXIT_MORE_WORK`` when its time is up.  Between slices the worker
releases the lock and sleeps longer than the heavy-job retry interval, so the
publisher always gets its turn; a slice killed for memory pressure loses at
most the window it was folding, and the next one continues from the store.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from aram_nn import patch_snapshot
from aram_nn.heavy_jobs import (
    SITE_BUILD_LIMITS,
    HeavyJob,
    ResourcePressure,
    exclusive_lock,
    run_command,
)

# Slice exit code for "checkpointed, more to do" (EX_TEMPFAIL).
EXIT_MORE_WORK = 75
SLICE_SEC = 300
# Longer than Limits.retry_sec (30s): a queued publisher re-polls the heavy-job
# lock at least twice while the worker is out of it.
PAUSE_SEC = 90
PRESSURE_PAUSE_SEC = 300
MAX_RUNTIME_SEC = 24 * 3600
MAX_FAILURES = 3

Log = Callable[[str], Any]


def state_dir(snapshot_dir: Path | None = None) -> Path:
    return Path(snapshot_dir) if snapshot_dir is not None else patch_snapshot.DEFAULT_SNAPSHOT_DIR


def resume_store_path(queue_id: int, patch: str, snapshot_dir: Path | None = None) -> Path:
    return state_dir(snapshot_dir) / "resume" / f"q{int(queue_id)}-{patch}.sqlite"


def remove_resume_store(queue_id: int, patch: str, snapshot_dir: Path | None = None) -> None:
    store = resume_store_path(queue_id, patch, snapshot_dir)
    for path in (store, store.with_name(store.name + "-wal"), store.with_name(store.name + "-shm")):
        try:
            path.unlink()
        except OSError:
            pass


def lock_path(snapshot_dir: Path | None = None) -> Path:
    return state_dir(snapshot_dir) / "settle.lock"


def log_path(snapshot_dir: Path | None = None) -> Path:
    return state_dir(snapshot_dir) / "settle-background.log"


def worker_python(executable: str | None = None) -> str:
    """The windowless interpreter next to *executable* when there is one."""
    exe = Path(executable or sys.executable)
    if os.name == "nt":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def worker_command(
    script: Path | str,
    *,
    db: Path | str,
    queue_id: int,
    patches: Iterable[str],
    executable: str | None = None,
) -> list[str]:
    command = [worker_python(executable), str(script), "--db", str(db),
               "--queue", str(int(queue_id)), "--background"]
    for patch in patches:
        command += ["--patch", str(patch)]
    return command


def slice_command(
    script: Path | str,
    *,
    db: Path | str,
    queue_id: int,
    patches: Iterable[str],
    slice_sec: float = SLICE_SEC,
    executable: str | None = None,
) -> list[str]:
    command = [str(executable or sys.executable), "-u", str(script), "--db", str(db),
               "--queue", str(int(queue_id)), "--resumable", "--max-seconds", f"{slice_sec:g}"]
    for patch in patches:
        command += ["--patch", str(patch)]
    return command


def spawn_background_settle(
    script: Path | str,
    *,
    db: Path | str,
    queue_id: int,
    patches: Sequence[str],
    cwd: Path | str | None = None,
    snapshot_dir: Path | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    log: Log | None = None,
) -> list[str] | None:
    """Start the detached worker for *patches*; returns its command line.

    Safe to call on every build: a second worker finds the lock taken and exits.
    Never raises -- a failed spawn only means the next build asks again.
    """
    patches = [str(p) for p in dict.fromkeys(patches) if p]
    if not patches:
        return None
    command = worker_command(script, db=db, queue_id=queue_id, patches=patches)
    try:
        destination = log_path(snapshot_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with destination.open("ab") as stream:
            popen(command, cwd=str(cwd) if cwd is not None else None,
                  stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                  creationflags=flags, close_fds=True)
    except Exception as exc:  # noqa: BLE001 - the build must not fail over this
        if log:
            log(f"[settle] WARN: could not start the background settle ({exc!r})")
        return None
    if log:
        log(f"[settle] background settle started for {', '.join(patches)} "
            f"(log: {log_path(snapshot_dir)})")
    return command


def run_worker(
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    snapshot_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
    admit: Callable[[], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Log = print,
    pause_sec: float = PAUSE_SEC,
    pressure_pause_sec: float = PRESSURE_PAUSE_SEC,
    max_runtime_sec: float = MAX_RUNTIME_SEC,
    max_failures: int = MAX_FAILURES,
) -> int:
    """Run *command* (one slice) until it reports done.  Returns an exit code.

    0 = settled, or another worker already owns the job; 1 = gave up.
    *runner* / *admit* are injectable for tests; the defaults run the slice as a
    memory-monitored child under the shared heavy-job lock.
    """
    runner = runner or run_command
    if admit is None:
        def admit():
            return HeavyJob("settle_patch", limits=SITE_BUILD_LIMITS).admitted()

    with exclusive_lock(lock_path(snapshot_dir)) as acquired:
        if not acquired:
            log("[settle-bg] another background settle is running; nothing to do")
            return 0
        started = clock()
        failures = slices = 0
        while True:
            if clock() - started > max_runtime_sec:
                log(f"[settle-bg] giving up after {max_runtime_sec / 3600:.0f}h; "
                    "progress is checkpointed, the next build restarts the worker")
                return 1
            try:
                with admit():
                    result = runner(list(command), cwd=cwd)
            except ResourcePressure as exc:
                log(f"[settle-bg] slice stopped for memory pressure ({exc}); "
                    f"retrying in {pressure_pause_sec:g}s")
                sleep(pressure_pause_sec)
                continue
            slices += 1
            for text in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
                if text and text.strip():
                    log(text.rstrip())
            code = int(result.returncode)
            if code == 0:
                log(f"[settle-bg] done after {slices} slice(s)")
                return 0
            if code == EXIT_MORE_WORK:
                failures = 0
                sleep(pause_sec)
                continue
            failures += 1
            log(f"[settle-bg] slice failed (exit {code}), attempt {failures}/{max_failures}")
            if failures >= max_failures:
                return 1
            sleep(pause_sec)
