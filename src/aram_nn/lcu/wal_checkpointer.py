"""Background WAL checkpointing for the single-writer process.

SQLite's built-in auto-checkpoint runs *inside* the commit that crosses the
page threshold, on the writer's own connection.  On the live database that
meant an RPC handler regularly stopped to copy WAL frames into a 200 GB file
while every producer waited on it, and once long-lived readers had let the WAL
grow to tens of GB every commit paid for another attempt.

:class:`WalCheckpointer` moves that work to a thread with its own connection.
``PRAGMA wal_checkpoint(PASSIVE)`` never blocks the writer or readers: it
copies whatever no open read transaction still needs.  When it has copied
everything, the writer's next commit restarts the WAL at offset zero and
``journal_size_limit`` (set by :class:`WriterService`) truncates the file.

PASSIVE alone never lets the WAL restart under a continuous write load: by the
time it finishes, the writer has appended more frames, so the log is never
fully backfilled at the moment the writer begins a transaction and the file
only grows.  Once the WAL is past ``truncate_pages`` and PASSIVE has left only
a small remainder, the thread follows up with ``wal_checkpoint(TRUNCATE)``.
That holds the write lock for the remainder only (the writer waits on its busy
timeout), then resets the file to zero bytes.

What it cannot fix: a reader that keeps one read transaction open pins the WAL
from that point on.  The periodic ``backlog`` log line is how that shows up,
and TRUNCATE then reports ``busy=1`` and is retried less often.
"""
from __future__ import annotations

from collections.abc import Callable
import sqlite3
import threading
import time


class WalCheckpointer:
    def __init__(
        self,
        db_path: str,
        *,
        interval_sec: float = 30.0,
        log: Callable[[str], None] | None = None,
        slow_ms: float = 5000.0,
        backlog_log_pages: int = 262_144,  # 1 GiB of 4 KiB pages
        backlog_log_every_sec: float = 600.0,
        truncate_pages: int = 131_072,  # 512 MiB of 4 KiB pages; 0 disables
        truncate_max_backlog_pages: int = 65_536,
        truncate_busy_retry_sec: float = 30.0,
        truncate_busy_timeout_ms: int = 250,
    ) -> None:
        self._db_path = str(db_path)
        self._interval = max(0.05, float(interval_sec))
        self._log = log
        self._slow_ms = float(slow_ms)
        self._backlog_log_pages = int(backlog_log_pages)
        self._backlog_log_every = float(backlog_log_every_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_backlog_log = 0.0
        self._truncate_pages = int(truncate_pages)
        self._truncate_max_backlog = int(truncate_max_backlog_pages)
        self._truncate_busy_retry = float(truncate_busy_retry_sec)
        self._truncate_busy_timeout_ms = int(truncate_busy_timeout_ms)
        self._truncate_not_before = 0.0
        self._last_busy_log = 0.0
        self.truncations = 0
        self.runs = 0
        self.last_result: tuple[int, int, int] | None = None  # (busy, wal pages, checkpointed)
        self.last_error: str | None = None
        self._last_backlog: int | None = None
        self._last_backlog_at = 0.0
        self._busy_wal_interval = min(self._interval, 5.0)

    def recent_backlog(self, max_age_sec: float = 10.0) -> int | None:
        """Frames the last PASSIVE pass left behind, or None if that is stale.

        The writer uses this to decide whether finishing the checkpoint itself
        is cheap.  Without it, a reader letting go of a long-pinned WAL would
        make the writer copy the whole backlog inside its request loop.
        """
        backlog = self._last_backlog
        if backlog is None or time.monotonic() - self._last_backlog_at > max_age_sec:
            return None
        return backlog

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wal-checkpointer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, line: str) -> None:
        if self._log is not None:
            try:
                self._log(line)
            except Exception:
                pass

    def checkpoint_once(self, con: sqlite3.Connection) -> tuple[int, int, int]:
        started = time.monotonic()
        row = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        result = (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
        self.runs += 1
        self.last_result = result
        self.last_error = None
        backlog = max(0, result[1] - result[2])
        now = time.monotonic()
        self._last_backlog = backlog
        self._last_backlog_at = now
        slow = elapsed_ms >= self._slow_ms
        pinned = (
            backlog >= self._backlog_log_pages
            and now - self._last_backlog_log >= self._backlog_log_every
        )
        if slow or pinned:
            self._last_backlog_log = now
            self._emit(
                f"[writer] wal checkpoint {time.strftime('%Y-%m-%d %H:%M:%S')} ms={elapsed_ms:.0f} "
                f"wal_pages={result[1]} checkpointed={result[2]} backlog={backlog} busy={result[0]}"
            )
        if (
            self._truncate_pages > 0
            and result[1] >= self._truncate_pages
            and backlog <= self._truncate_max_backlog
            and now >= self._truncate_not_before
        ):
            self._truncate(con, result[1])
        return result

    def _truncate(self, con: sqlite3.Connection, wal_pages: int) -> None:
        started = time.monotonic()
        # While this waits for a reader to leave the WAL it holds the write
        # lock, so keep the wait short and retry on a later pass instead.
        con.execute(f"PRAGMA busy_timeout={self._truncate_busy_timeout_ms}")
        try:
            row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.execute("PRAGMA busy_timeout=1000")
        finished = time.monotonic()
        elapsed_ms = (finished - started) * 1000.0
        busy = int(row[0]) if row else 0
        if busy:
            self._truncate_not_before = finished + self._truncate_busy_retry
            if finished - self._last_busy_log < self._backlog_log_every and self._last_busy_log:
                return
            self._last_busy_log = finished
        else:
            self.truncations += 1
        self._emit(
            f"[writer] wal truncate {time.strftime('%Y-%m-%d %H:%M:%S')} ms={elapsed_ms:.0f} "
            f"wal_pages={wal_pages} busy={busy}"
        )

    def _next_wait(self) -> float:
        # A grown WAL is checked more often so the remainder handed to the
        # writer (or to TRUNCATE) stays a few seconds of writes, not thirty.
        last = self.last_result
        if last is not None and self._truncate_pages > 0 and last[1] >= self._truncate_pages:
            return self._busy_wal_interval
        return self._interval

    def _run(self) -> None:
        con: sqlite3.Connection | None = None
        try:
            while not self._stop.wait(self._next_wait()):
                try:
                    if con is None:
                        con = sqlite3.connect(self._db_path, timeout=1.0)
                        con.execute("PRAGMA busy_timeout=1000")
                    self.checkpoint_once(con)
                except Exception as exc:  # never let the thread die: nothing else checkpoints
                    message = repr(exc)
                    if message != self.last_error:
                        self._emit(f"[writer] wal checkpoint failed {time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
                    self.last_error = message
                    if con is not None:
                        try:
                            con.close()
                        except sqlite3.Error:
                            pass
                        con = None
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass


__all__ = ["WalCheckpointer"]
