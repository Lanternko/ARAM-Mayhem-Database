from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import time

import pytest

from aram_nn.lcu.game_participants import extract_puuids, participant_timeline
from aram_nn.lcu.writer_service import WriterService

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_game_participants.py"
_spec = importlib.util.spec_from_file_location("backfill_game_participants", _SCRIPT)
backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_mod)


def _game(game_id="g1", participants=None, private=None, created_ms=123, queue_id=2400) -> dict:
    return {
        "game_id": game_id,
        "queue_id": queue_id,
        "patch": "16.1",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": 1,
        "duration_sec": 900,
        "created_ms": created_ms,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "participants": participants if participants is not None else [{"puuid": "p1"}],
        "participants_private": private if private is not None else [],
    }


def _claim(service: WriterService, game_id: str, request_id: str) -> dict:
    return service.handle({"version": 1, "command": "game_claim", "request_id": request_id, "game_id": game_id, "now_ms": 1000}, channel_id="w")


def _commit(service: WriterService, record: dict, request_id: str, claim: dict | None = None, message_puuids=None) -> dict:
    claim = claim or _claim(service, record["game_id"], f"c-{request_id}")
    message = {"version": 1, "command": "commit_game", "request_id": request_id, "game_id": record["game_id"], "token": claim["token"], "generation": claim["generation"], "record": record, "now_ms": 1001}
    if message_puuids is not None:
        message["participant_puuids"] = message_puuids
    return service.handle(message, channel_id="w")


def _store_directly(service: WriterService, record: dict) -> None:
    # Another path (poller) stores the game after our claim was granted, so the
    # writer takes its "existing game" branch at commit time.
    service.con.execute(
        "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, blue_wins, duration_sec, created_ms, captured_at, participants_json, participants_private_json, seed_family) "
        "VALUES (?, ?, '16.1', '[]', '[]', 1, 900, ?, '2026-01-01', ?, '[]', 'match')",
        (record["game_id"], record["queue_id"], record["created_ms"], json.dumps(record["participants"])),
    )
    service.con.commit()


def _index(con: sqlite3.Connection) -> list[tuple]:
    rows = con.execute("SELECT puuid, created_ms, game_id, queue_id FROM game_participants ORDER BY 1, 2, 3").fetchall()
    return [tuple(row) for row in rows]


def test_commit_game_indexes_each_participant_once(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    record = _game(participants=[{"puuid": "a"}, {"puuid": "b"}], private=[{"puuid": "b"}, {"puuid": "c"}], created_ms=555)
    claim = _claim(service, "g1", "c1")
    _commit(service, record, "r1", claim, message_puuids=["d", "a"])
    expected = [(p, 555, "g1", 2400) for p in ("a", "b", "c", "d")]
    assert _index(service.con) == expected
    # Re-committing the same game (claim granted earlier, game stored since): no duplicates.
    claim2 = _claim(service, "g2", "c2")
    _store_directly(service, _game("g2", participants=[{"puuid": "a"}], created_ms=555))
    assert _commit(service, _game("g2", participants=[{"puuid": "a"}], created_ms=555), "r2", claim2)["status"] == "DUPLICATE"
    assert [row for row in _index(service.con) if row[2] == "g2"] == [("a", 555, "g2", 2400)]
    assert [row for row in _index(service.con) if row[2] == "g1"] == expected


def test_commit_game_rollback_drops_index_rows(tmp_path: pathlib.Path) -> None:
    def fail(point: str) -> None:
        if point == "participant_enqueue":
            raise RuntimeError("injected")

    service = WriterService(tmp_path / "games.db", failure_injector=fail)
    claim = _claim(service, "g1", "c1")
    with pytest.raises(RuntimeError):
        _commit(service, _game(participants=[{"puuid": "a"}, {"puuid": "b"}]), "r1", claim)
    assert service.con.execute("SELECT COUNT(*) FROM game_participants").fetchone()[0] == 0
    service._failure_injector = None
    _commit(service, _game(participants=[{"puuid": "a"}, {"puuid": "b"}]), "r2", claim)
    assert service.con.execute("SELECT COUNT(*) FROM game_participants").fetchone()[0] == 2


def test_richer_participants_for_existing_game_are_indexed(tmp_path: pathlib.Path) -> None:
    service = WriterService(tmp_path / "games.db")
    claim = _claim(service, "g1", "c1")
    _store_directly(service, _game(participants=[{"puuid": "a"}]))
    result = _commit(service, _game(participants=[{"puuid": "a"}, {"puuid": "b"}], private=[{"puuid": "c"}]), "r1", claim)
    assert result["status"] == "DUPLICATE"
    assert [row[0] for row in _index(service.con)] == ["a", "b", "c"]
    assert service.con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1


def test_extract_puuids_dedupes_and_tolerates_bad_input() -> None:
    public = json.dumps([{"puuid": "a"}, {"player": {"puuid": "b"}}, "junk", {"puuid": ""}])
    private = json.dumps([{"puuid": "a"}, {"puuid": "c"}, {"puuid": 5}])
    assert extract_puuids(public, private) == ["a", "b", "c"]
    assert extract_puuids("", "not json", None, "{}", "[1,2]") == []


def _make_db(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "games.db"
    WriterService(path).con.close()
    con = sqlite3.connect(path)
    rows = [
        ("1", 2400, 300, [{"puuid": "a"}, {"puuid": "b"}], [{"puuid": "a"}, {"puuid": "c"}]),
        ("2", 450, 100, [{"stats": {}}], [{"puuid": "a"}]),
        ("3", 2400, 200, "{bad json", "[]"),
        ("4", 2400, 400, [], ""),
        ("5", 2400, 500, [{"puuid": "b"}], [{"puuid": "b"}]),
    ]
    for game_id, queue_id, created_ms, public, private in rows:
        con.execute(
            "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, blue_wins, duration_sec, created_ms, captured_at, participants_json, participants_private_json, seed_family) "
            "VALUES (?, ?, '16.1', '[]', '[]', 1, 900, ?, '2026-01-01', ?, ?, 'match')",
            (game_id, queue_id, created_ms, public if isinstance(public, str) else json.dumps(public), private if isinstance(private, str) else json.dumps(private)),
        )
    con.commit()
    con.close()
    return path


def _count(db: pathlib.Path) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM game_participants").fetchone()[0]
    finally:
        con.close()


def test_backfill_is_idempotent_and_dry_run_writes_nothing(tmp_path: pathlib.Path) -> None:
    db = _make_db(tmp_path)
    ckpt = tmp_path / "ckpt.json"
    dry = backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0, dry_run=True)
    assert dry["puuid_rows"] == 5 and dry["games_without_puuid"] == 2
    assert _count(db) == 0 and not ckpt.exists()

    first = backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0)
    assert first["rows_inserted"] == 5 and first["batches"] == 3
    con = sqlite3.connect(db)
    assert participant_timeline(con, "a") == [(100, "2", 450), (300, "1", 2400)]
    assert participant_timeline(con, "b") == [(300, "1", 2400), (500, "5", 2400)]
    con.close()

    # Re-run from scratch (checkpoint removed) adds nothing; so does resuming.
    ckpt.unlink()
    assert backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0)["rows_inserted"] == 0
    assert backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0)["games"] == 0
    assert _count(db) == 5


def test_backfill_limit_and_resume_from_checkpoint(tmp_path: pathlib.Path) -> None:
    db = _make_db(tmp_path)
    ckpt = tmp_path / "ckpt.json"
    part = backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0, limit_games=3)
    assert part["games"] == 3
    saved = json.loads(ckpt.read_text(encoding="utf-8"))
    assert saved["last_rowid"] == 3 and saved["rows_inserted"] == part["rows_inserted"] == 4
    rest = backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0)
    assert rest["games"] == 2 and rest["rows_inserted"] == 1
    assert json.loads(ckpt.read_text(encoding="utf-8"))["rows_inserted"] == 5
    assert _count(db) == 5
    # A game stored after the run (the pre-deploy gap) is picked up on re-run.
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, blue_wins, duration_sec, created_ms, captured_at, participants_json, participants_private_json, seed_family) "
        "VALUES ('6', 2400, '16.1', '[]', '[]', 1, 900, 600, '2026-01-01', '[]', '[{\"puuid\": \"z\"}]', 'match')"
    )
    con.commit()
    con.close()
    assert backfill_mod.backfill(db, ckpt, batch_games=2, sleep_sec=0)["rows_inserted"] == 1
    assert _count(db) == 6


def test_backfill_main_cli_and_foreign_checkpoint_is_ignored(tmp_path: pathlib.Path) -> None:
    db = _make_db(tmp_path)
    ckpt = tmp_path / "ckpt.json"
    ckpt.write_text(json.dumps({"db": "elsewhere.db", "last_rowid": 99, "rows_inserted": 7}), encoding="utf-8")
    assert backfill_mod.main(["--db", str(db), "--checkpoint", str(ckpt), "--sleep", "0", "--batch-games", "10"]) == 0
    assert _count(db) == 5


def test_participant_timeline_orders_and_filters(tmp_path: pathlib.Path) -> None:
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute(backfill_mod.CREATE_GAME_PARTICIPANTS_SQL)
    con.executemany(
        "INSERT INTO game_participants VALUES (?, ?, ?, ?)",
        [("p", 300, "c", 2400), ("p", 100, "a", 450), ("p", 200, "b", 2400), ("q", 150, "z", 2400)],
    )
    assert participant_timeline(con, "p") == [(100, "a", 450), (200, "b", 2400), (300, "c", 2400)]
    assert participant_timeline(con, "p", since_ms=200) == [(200, "b", 2400), (300, "c", 2400)]
    assert participant_timeline(con, "nobody") == []


def test_backfill_batch_of_500_games_commits_quickly(tmp_path: pathlib.Path, capsys) -> None:
    db = tmp_path / "games.db"
    WriterService(db).con.close()
    con = sqlite3.connect(db)
    payload = json.dumps([{"puuid": f"pu-{i}"} for i in range(10)])
    con.executemany(
        "INSERT INTO games(game_id, queue_id, patch, blue_champs, red_champs, blue_wins, duration_sec, created_ms, captured_at, participants_json, participants_private_json, seed_family) "
        "VALUES (?, 2400, '16.1', '[]', '[]', 1, 900, ?, '2026-01-01', '[]', ?, 'match')",
        [(str(i), 1000 + i, payload.replace("pu-", f"pu{i}-")) for i in range(500)],
    )
    con.commit()
    con.close()
    writer = sqlite3.connect(db, isolation_level=None)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    parsed = [backfill_mod._parse_row(row) for row in con.execute(backfill_mod._SELECT_BATCH_SQL, (0, 500))]
    started = time.perf_counter()
    added = backfill_mod._write_batch(writer, parsed)
    elapsed = time.perf_counter() - started
    print(f"BATCH500 rows={added} commit_sec={elapsed:.4f}")
    assert added == 5000 and elapsed < 5


def test_snowball_non_rpc_insert_and_backfill_index_participants(tmp_path: pathlib.Path) -> None:
    from aram_nn.lcu import snowball

    con = sqlite3.connect(tmp_path / "games.db")
    snowball._ensure_schema(con)
    record = _game("s1", participants=[{"stats": {}}], private=[{"puuid": "a"}, {"puuid": "b"}], created_ms=777)
    record["seed_family"] = "match"
    assert snowball._insert_game(con, record) is True
    assert _index(con) == [("a", 777, "s1", 2400), ("b", 777, "s1", 2400)]
    # Re-saving the same game is a no-op for the index.
    assert snowball._insert_game(con, record) is False
    richer = dict(record, participants_private=[{"puuid": "a"}, {"puuid": "b"}, {"puuid": "c"}])
    snowball._backfill_participants_json(con, richer)
    assert [row[0] for row in _index(con)] == ["a", "b", "c"]
