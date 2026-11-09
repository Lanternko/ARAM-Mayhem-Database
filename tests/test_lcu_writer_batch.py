"""Group commit, housekeeping and WAL checkpointing of the single writer."""
from __future__ import annotations

import pathlib
import sqlite3
import time

import pytest

from aram_nn.lcu.wal_checkpointer import WalCheckpointer
from aram_nn.lcu.writer_service import WriterService


def _game(game_id: str = "g1") -> dict[str, object]:
    return {
        "game_id": game_id,
        "queue_id": 2400,
        "patch": "16.1",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": 1,
        "duration_sec": 900,
        "created_ms": 123,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "participants": [{"puuid": "p1"}],
    }


def _claim(service: WriterService, game_id: str, request_id: str) -> dict[str, object]:
    return service.handle(
        {"version": 1, "command": "game_claim", "request_id": request_id, "game_id": game_id, "now_ms": 1000},
        channel_id="w",
    )


def _commit(service: WriterService, claim: dict[str, object], game_id: str, request_id: str) -> dict[str, object]:
    return service.handle(
        {
            "version": 1, "command": "commit_game", "request_id": request_id, "game_id": game_id,
            "token": claim["token"], "generation": claim["generation"], "record": _game(game_id), "now_ms": 1001,
        },
        channel_id="w",
    )


def _count(db: pathlib.Path, table: str) -> int:
    with sqlite3.connect(db) as con:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_batch_is_invisible_until_end_batch_then_durable(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    service = WriterService(db)
    service.begin_batch()
    claim = _claim(service, "g1", "c1")
    assert _commit(service, claim, "g1", "k1")["status"] == "COMMITTED"
    # A no-op answer (already stored) is journaled without dropping the batch.
    assert _claim(service, "g1", "c2")["status"] == "DONE"

    assert _count(db, "games") == 0  # another connection: nothing committed yet
    assert _count(db, "writer_requests") == 0

    assert service.end_batch() is True
    assert _count(db, "games") == 1
    assert _count(db, "writer_requests") == 3
    assert not service.con.in_transaction


def test_failed_request_in_batch_is_undone_alone(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    armed = [False]

    def fail(point: str) -> None:
        if armed[0] and point == "participant_enqueue":
            raise RuntimeError("injected")

    service = WriterService(db, failure_injector=fail)
    service.begin_batch()
    first = _claim(service, "g1", "c1")
    assert _commit(service, first, "g1", "k1")["status"] == "COMMITTED"
    second = _claim(service, "g2", "c2")
    armed[0] = True
    with pytest.raises(RuntimeError):
        _commit(service, second, "g2", "k2")
    armed[0] = False
    assert service.end_batch() is True

    with sqlite3.connect(db) as con:
        assert [r[0] for r in con.execute("SELECT game_id FROM games")] == ["g1"]
        journal = {r[0] for r in con.execute("SELECT request_id FROM writer_requests")}
    assert journal == {"c1", "k1", "c2"}
    # The failed request's claim survived, so the producer can retry it.
    assert _commit(service, second, "g2", "k2-retry")["status"] == "COMMITTED"
    assert _count(db, "games") == 2


def test_replay_inside_and_after_batch_returns_the_stored_response(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    service.begin_batch()
    first = _claim(service, "g1", "c1")
    assert _claim(service, "g1", "c1") == first
    assert service.end_batch() is True
    assert _claim(service, "g1", "c1") == first


def test_lost_transaction_marks_the_whole_batch_failed(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"

    def wipe(point: str) -> None:
        if point == "participant_enqueue":
            # What SQLite does on its own for disk-full / I/O errors.
            service.con.rollback()
            raise sqlite3.OperationalError("disk I/O error")

    service = WriterService(db, failure_injector=wipe)
    service.begin_batch()
    claim = _claim(service, "g1", "c1")
    with pytest.raises(sqlite3.OperationalError):
        _commit(service, claim, "g1", "k1")
    with pytest.raises(RuntimeError):
        _claim(service, "g2", "c2")
    assert service.end_batch() is False
    assert _count(db, "writer_requests") == 0
    assert _count(db, "writer_game_claims") == 0
    # The service is usable again after the failed batch.
    assert _claim(service, "g1", "c1")["status"] == "CLAIMED"


def test_queue_reads_are_not_journaled_and_pending_count_is_cached(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    service = WriterService(db, pending_count_ttl_sec=60.0)

    def pending(request_id: str) -> int:
        response = service.handle(
            {"version": 1, "command": "snowball_queue", "request_id": request_id, "operation": "pending_count"},
            channel_id="w",
        )
        assert response["status"] == "COUNT"
        return int(response["count"])

    assert pending("p1") == 0
    service.enqueue_player("player-1")
    assert pending("p2") == 0  # served from the cache
    service._pending_count_cache = None
    assert pending("p3") == 1
    wait = service.handle(
        {"version": 1, "command": "snowball_queue", "request_id": "n1", "operation": "next_wait"},
        channel_id="w",
    )
    assert wait["status"] == "WAIT"
    assert _count(db, "writer_requests") == 0

    exact = WriterService(tmp_path / "exact.db")  # default: no cache
    exact.enqueue_player("player-1")
    response = exact.handle(
        {"version": 1, "command": "snowball_queue", "request_id": "p", "operation": "pending_count"},
        channel_id="w",
    )
    assert response["count"] == 1


def test_maintain_prunes_old_journal_rows_and_finished_claims(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    now = [10 * 3600_000]
    service = WriterService(db, clock_ms=lambda: now[0], request_retention_sec=3600.0)
    old_claim = _claim(service, "old", "c-old")
    assert _commit(service, old_claim, "old", "k-old")["status"] == "COMMITTED"  # claim row -> done
    _claim(service, "open", "c-open")  # stays in_progress
    service.con.execute("UPDATE writer_requests SET created_at='2020-01-01T00:00:00+00:00'")
    service.con.execute("UPDATE writer_game_claims SET claimed_at_ms=0")
    service.con.commit()
    fresh = service.handle({"version": 1, "command": "ping", "request_id": "fresh"}, channel_id="w")

    swept = service.maintain()
    assert swept == {"requests": 3, "game_claims": 1, "player_claims": 0}
    with sqlite3.connect(db) as con:
        assert [r[0] for r in con.execute("SELECT request_id FROM writer_requests")] == ["fresh"]
        assert [tuple(r) for r in con.execute("SELECT game_id, status FROM writer_game_claims")] == [("open", "in_progress")]
        assert con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    # Paced: an immediate second call does nothing, and a retained row still replays.
    assert service.maintain() == {}
    assert service.handle({"version": 1, "command": "ping", "request_id": "fresh"}, channel_id="w") == fresh
    # A stored game is still reported DONE after its claim row is gone.
    assert _claim(service, "old", "c-again")["status"] == "DONE"


def test_maintain_is_skipped_inside_a_batch_and_when_disabled(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db", request_retention_sec=3600.0)
    service.handle({"version": 1, "command": "ping", "request_id": "a"}, channel_id="w")
    service.con.execute("UPDATE writer_requests SET created_at='2020-01-01T00:00:00+00:00'")
    service.con.commit()
    service.begin_batch()
    assert service.maintain() == {}
    assert service.end_batch() is True
    off = WriterService(tmp_path / "off.db", request_retention_sec=0)
    assert off.maintain() == {}
    assert service.maintain()["requests"] == 1


def test_wal_checkpointer_backfills_without_the_writer(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    service = WriterService(db)
    service.con.execute("PRAGMA wal_autocheckpoint=0")
    for index in range(50):
        _claim(service, f"g{index}", f"c{index}")
    lines: list[str] = []
    checkpointer = WalCheckpointer(str(db), interval_sec=0.05, log=lines.append, slow_ms=0.0)
    checkpointer.start()
    try:
        deadline = time.monotonic() + 5.0
        while checkpointer.runs == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        checkpointer.stop()
    assert not checkpointer.is_alive()
    assert checkpointer.last_error is None
    busy, wal_pages, checkpointed = checkpointer.last_result
    assert wal_pages > 0 and checkpointed == wal_pages and busy == 0
    assert lines and "wal checkpoint" in lines[0]
    # The writer keeps working against the checkpointed database.
    assert _claim(service, "after", "c-after")["status"] == "CLAIMED"


def test_wal_checkpointer_truncates_a_grown_wal(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "games.db"
    service = WriterService(db)
    service.con.execute("PRAGMA wal_autocheckpoint=0")
    for index in range(50):
        _claim(service, f"g{index}", f"c{index}")
    wal = pathlib.Path(str(db) + "-wal")
    assert wal.stat().st_size > 0
    lines: list[str] = []
    checkpointer = WalCheckpointer(str(db), log=lines.append, truncate_pages=1)
    con = sqlite3.connect(db)
    try:
        checkpointer.checkpoint_once(con)
        assert checkpointer.truncations == 1 and wal.stat().st_size == 0
        assert any("wal truncate" in line and "busy=0" in line for line in lines)
        # The writer carries on into the fresh log.
        assert _claim(service, "after", "c-after")["status"] == "CLAIMED"

        # A reader inside the WAL blocks the reset: reported busy, then backed off.
        reader = sqlite3.connect(db)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM writer_game_claims").fetchone()
        _claim(service, "more", "c-more")
        con.execute("PRAGMA busy_timeout=50")
        checkpointer.checkpoint_once(con)
        assert checkpointer.truncations == 1 and wal.stat().st_size > 0
        assert "busy=1" in lines[-1]
        before = len(lines)
        checkpointer.checkpoint_once(con)
        assert len(lines) == before
        reader.close()
    finally:
        con.close()


@pytest.mark.parametrize("group_commit", ["1", "0"])
def test_supervisor_acks_only_durable_writes_under_concurrency(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, group_commit: str
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from aram_nn.lcu.writer_supervisor import WriterSupervisor

    # The writer is a spawned process: it reads these at import time.
    monkeypatch.setenv("ARAM_WRITER_GROUP_COMMIT", group_commit)
    monkeypatch.setenv("ARAM_WRITER_CHECKPOINT_SEC", "0.05")
    db = tmp_path / "games.db"
    supervisor = WriterSupervisor(db, 4)
    clients = supervisor.start()
    try:
        def work(index: int) -> list[str]:
            statuses = []
            for item in range(25):
                game_id = f"g{index}-{item}"
                response = clients[index].submit(
                    {"version": 1, "command": "game_claim", "request_id": f"c-{game_id}", "game_id": game_id}
                )
                statuses.append(str(response["status"]))
                # Acknowledged means committed: a reader sees it right away.
                with sqlite3.connect(db) as con:
                    assert con.execute(
                        "SELECT 1 FROM writer_game_claims WHERE game_id=?", (game_id,)
                    ).fetchone() is not None
            return statuses

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [future.result(timeout=60) for future in [pool.submit(work, i) for i in range(4)]]
        assert all(status == "CLAIMED" for statuses in results for status in statuses)
        metrics = supervisor.metrics()
        assert metrics.frames_succeeded == 100 and metrics.frames_rejected == 0
    finally:
        supervisor.shutdown()
    assert _count(db, "writer_game_claims") == 100
    assert _count(db, "writer_requests") == 100
