from __future__ import annotations

import sqlite3

from aram_nn.lcu.db_state import (
    capture_watermark_key,
    ensure_runtime_state_schema,
    update_capture_watermark,
)
from aram_nn.lcu.poller import _ensure_games_schema, _save
from aram_nn.lcu.snowball import _ensure_schema, _insert_game


def _record(game_id: str = "1", captured_at: str = "2026-08-11T15:00:00+00:00") -> dict:
    return {
        "game_id": game_id,
        "queue_id": 2400,
        "patch": "16.15.802",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": 1,
        "duration_sec": 900,
        "created_ms": 1,
        "captured_at": captured_at,
        "participants": [],
        "participants_private": [],
    }


def test_capture_watermark_uses_queue_specific_runtime_state() -> None:
    con = sqlite3.connect(":memory:")
    ensure_runtime_state_schema(con)

    update_capture_watermark(
        con,
        queue_id=2400,
        captured_at="2026-08-11T15:00:00+00:00",
    )

    row = con.execute(
        "SELECT state_value, updated_at FROM crawl_runtime_state WHERE state_key = ?",
        (capture_watermark_key(2400),),
    ).fetchone()
    assert row is not None
    assert row[0] == "2026-08-11T15:00:00+00:00"
    assert row[1]


def test_snowball_insert_updates_watermark_only_for_new_game() -> None:
    con = sqlite3.connect(":memory:")
    _ensure_schema(con)

    assert _insert_game(con, _record()) is True
    assert _insert_game(
        con,
        _record(captured_at="2026-08-11T16:00:00+00:00"),
    ) is False

    row = con.execute(
        "SELECT state_value FROM crawl_runtime_state WHERE state_key = ?",
        (capture_watermark_key(2400),),
    ).fetchone()
    assert row == ("2026-08-11T15:00:00+00:00",)


def test_poller_save_creates_and_updates_capture_watermark() -> None:
    con = sqlite3.connect(":memory:")
    _ensure_games_schema(con)
    seen_ids: set[str] = set()

    assert _save(con, _record(), seen_ids) is True

    row = con.execute(
        "SELECT state_value FROM crawl_runtime_state WHERE state_key = ?",
        (capture_watermark_key(2400),),
    ).fetchone()
    assert row == ("2026-08-11T15:00:00+00:00",)
    assert seen_ids == {"1"}
