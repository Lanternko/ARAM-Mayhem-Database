from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from aram_nn.lcu import snowball
from aram_nn.lcu.snowball import (
    _connect_db,
    _enqueue_player,
    _ensure_schema,
    _pending_player_count,
    _retry_on_locked,
)


class RetryOnLockedTests(unittest.TestCase):
    """Losing the write lock must cost a retry, not the whole worker process."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "games.db"
        self.con = _connect_db(self.db_path)
        _ensure_schema(self.con)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.con.close)

    def test_write_retries_until_the_other_writer_commits(self) -> None:
        blocker = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        self.addCleanup(blocker.close)
        blocker.execute("PRAGMA busy_timeout=30000")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO crawl_seen (puuid, source, priority, min_depth, first_seen_at) "
            "VALUES ('blocker', 'match', 1, 0, '2026-01-01T00:00:00+00:00')"
        )

        released = threading.Event()

        def release_after_a_moment() -> None:
            time.sleep(1.0)
            blocker.commit()
            released.set()

        # The decorated call below must survive the window where the lock is
        # held; _connect_db's busy_timeout would normally cover this, so shorten
        # it to force the "database is locked" path the retry exists for.
        self.con.execute("PRAGMA busy_timeout=1")
        releaser = threading.Thread(target=release_after_a_moment)
        releaser.start()
        self.addCleanup(releaser.join)

        with unittest.mock.patch.object(snowball, "_DB_RETRY_BASE_SLEEP_SEC", 0.1):
            result = _enqueue_player(self.con, "player-1", depth=0, source="match")

        self.assertTrue(released.is_set())
        self.assertEqual(result, "new")
        self.assertEqual(_pending_player_count(self.con), 1)

    def test_non_lock_errors_are_not_retried(self) -> None:
        attempts: list[int] = []

        @_retry_on_locked
        def boom(con: sqlite3.Connection) -> None:
            attempts.append(1)
            raise sqlite3.OperationalError("no such table: nope")

        with self.assertRaises(sqlite3.OperationalError):
            boom(self.con)
        self.assertEqual(len(attempts), 1)

    def test_retry_gives_up_and_reraises(self) -> None:
        attempts: list[int] = []

        @_retry_on_locked
        def always_locked(con: sqlite3.Connection) -> None:
            attempts.append(1)
            raise sqlite3.OperationalError("database is locked")

        with unittest.mock.patch.object(snowball, "_DB_RETRY_ATTEMPTS", 3), unittest.mock.patch.object(
            snowball, "_DB_RETRY_BASE_SLEEP_SEC", 0.0
        ):
            with self.assertRaises(sqlite3.OperationalError):
                always_locked(self.con)
        self.assertEqual(len(attempts), 3)

    def test_nested_calls_do_not_roll_back_the_caller(self) -> None:
        """Only the outermost decorated call may retry.

        An inner retry would roll back a transaction its caller still owns, so
        the guard has to keep nested units running inline.
        """
        seen_depth: list[int] = []

        @_retry_on_locked
        def inner(con: sqlite3.Connection) -> None:
            seen_depth.append(snowball._db_retry_depth)

        @_retry_on_locked
        def outer(con: sqlite3.Connection) -> None:
            inner(con)

        outer(self.con)
        self.assertEqual(seen_depth, [1])
        self.assertEqual(snowball._db_retry_depth, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
