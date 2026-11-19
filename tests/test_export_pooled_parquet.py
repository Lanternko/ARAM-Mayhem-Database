from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import polars as pl
import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "export_pooled_parquet_under_test",
    ROOT / "scripts" / "export_pooled_parquet.py",
)
assert SPEC and SPEC.loader
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


def _make_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE games (game_id INTEGER, patch TEXT, queue_id INTEGER, "
        "duration_sec INTEGER, blue_champs TEXT, red_champs TEXT, blue_wins INTEGER, "
        "created_ms INTEGER, participants_json TEXT)"
    )
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _game(game_id: int, patch: str, created_ms: int, *, queue_id: int = 2400,
          blue: str | None = None, red: str | None = None) -> tuple:
    return (
        game_id, patch, queue_id, 1200,
        blue if blue is not None else json.dumps([1, 2, 3, 4, 5]),
        red if red is not None else json.dumps([6, 7, 8, 9, 10]),
        1, created_ms, json.dumps([{"championId": 1}]),
    )


def test_batching_writes_every_game_exactly_once(tmp_path: Path) -> None:
    """A row group boundary must not drop or duplicate games."""
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, [_game(i, "16.16.1", 1000 + i) for i in range(25)])

    # 7 does not divide 25: the last flush carries a partial batch.
    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.16",
        "--cap-oldest", "0", "--batch-rows", "7",
    ])
    assert result.exit_code == 0, result.output

    frame = pl.read_parquet(out)
    assert frame.height == 25
    assert sorted(frame["match_id"].to_list()) == sorted(str(i) for i in range(25))
    assert frame["blue_champions"].to_list()[0] == [1, 2, 3, 4, 5]
    assert set(frame.columns) == set(EXPORT.SCHEMA)


def test_pool_order_cap_and_filters_survive_streaming(tmp_path: Path) -> None:
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    rows = [_game(i, "16.15.1", 1000 + i) for i in range(5)]
    rows += [_game(100 + i, "16.16.1", 5000 + i) for i in range(3)]
    # Neither of these is a usable training row.
    rows.append(_game(900, "16.16.1", 6000, queue_id=450))
    rows.append(_game(901, "16.16.1", 6001, blue=json.dumps([1, 2, 3])))
    _make_db(db, rows)

    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.15,16.16",
        "--cap-oldest", "2", "--batch-rows", "4",
    ])
    assert result.exit_code == 0, result.output

    frame = pl.read_parquet(out)
    # cap-oldest keeps the most recent 2 of 16.15; the short-team and non-Mayhem
    # rows are dropped, leaving all 3 of 16.16.
    assert frame.height == 5
    assert sorted(frame.filter(pl.col("patch") == "16.15.1")["match_id"].to_list()) == ["3", "4"]
    assert "900" not in frame["match_id"].to_list()
    assert "901" not in frame["match_id"].to_list()


def test_empty_pool_still_leaves_a_readable_file(tmp_path: Path) -> None:
    """Downstream steps open this path unconditionally."""
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, [_game(1, "16.16.1", 1000)])

    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.99",
        "--cap-oldest", "0",
    ])
    assert result.exit_code == 0, result.output

    frame = pl.read_parquet(out)
    assert frame.height == 0
    assert set(frame.columns) == set(EXPORT.SCHEMA)


def test_a_failed_export_leaves_the_previous_parquet_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """A row-group writer closes a valid footer even when it dies partway."""
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, [_game(i, "16.16.1", 1000 + i) for i in range(30)])

    pl.DataFrame({"match_id": ["previous"]}).write_parquet(out)

    real = EXPORT._record
    calls = {"n": 0}

    def explode(row):
        calls["n"] += 1
        if calls["n"] > 12:
            raise MemoryError("simulated OOM mid-export")
        return real(row)

    monkeypatch.setattr(EXPORT, "_record", explode)
    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.16",
        "--cap-oldest", "0", "--batch-rows", "5",
    ])
    assert result.exit_code != 0

    # The half-written pool must not be published, and must not be left behind.
    assert pl.read_parquet(out)["match_id"].to_list() == ["previous"]
    assert not (tmp_path / "pooled.parquet.partial").exists()


def test_cap_keeps_the_newest_games_and_never_splits_a_timestamp(
    tmp_path: Path,
) -> None:
    """The cutoff replaces ORDER BY ... LIMIT, so ties must not be split."""
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    rows = [_game(i, "16.15.1", 1000 + i) for i in range(4)]
    # Three games share the timestamp that the cap lands on.
    rows += [_game(50 + j, "16.15.1", 2000) for j in range(3)]
    rows += [_game(60 + j, "16.15.1", 3000 + j) for j in range(2)]
    _make_db(db, rows)

    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.15",
        "--cap-oldest", "4", "--batch-rows", "3",
    ])
    assert result.exit_code == 0, result.output

    kept = sorted(int(x) for x in pl.read_parquet(out)["match_id"].to_list())
    # Newest 4 would cut through the 2000-tie; all three of those stay instead.
    assert kept == [50, 51, 52, 60, 61]


def test_cap_larger_than_the_patch_keeps_everything(tmp_path: Path) -> None:
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, [_game(i, "16.15.1", 1000 + i) for i in range(3)])

    result = CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", "16.15",
        "--cap-oldest", "500",
    ])
    assert result.exit_code == 0, result.output
    assert pl.read_parquet(out).height == 3


def _pool_rows() -> list[tuple]:
    rows = [_game(i, "16.15.1", 1000 + i) for i in range(40)]
    rows += [_game(100 + i, "16.16.1", 5000 + i) for i in range(33)]
    rows.append(_game(900, "16.16.1", 5010, blue="not json"))
    rows += [_game(200 + i, "16.16.2", 4000 + i) for i in range(21)]
    return rows


def _export(db: Path, out: Path, *extra: str, patches: str = "16.15,16.16"):
    return CliRunner().invoke(EXPORT.main, [
        "--db", str(db), "--out", str(out), "--patches", patches,
        "--cap-oldest", "25", "--batch-rows", "4", "--part-rows", "8", *extra,
    ])


def _stop_after(monkeypatch, n: int) -> dict:
    real = EXPORT._record
    calls = {"n": 0}

    def record(row):
        calls["n"] += 1
        if calls["n"] > n:
            raise MemoryError("stopped by the memory guard")
        return real(row)

    monkeypatch.setattr(EXPORT, "_record", record)
    return calls


def test_a_stopped_export_continues_where_it_stopped(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "games.db"
    _make_db(db, _pool_rows())
    whole = tmp_path / "whole.parquet"
    assert _export(db, whole).exit_code == 0
    expected = pl.read_parquet(whole)
    assert expected.height == 25 + 33 + 21

    out = tmp_path / "pooled.parquet"
    for stop in (30, 20):  # stopped twice, in two different patches
        _stop_after(monkeypatch, stop)
        assert _export(db, out).exit_code != 0
        assert not out.exists()
        monkeypatch.undo()

    calls = _stop_after(monkeypatch, 10_000)
    result = _export(db, out)
    assert result.exit_code == 0, result.output
    assert "resuming" in result.output
    assert f"rows={expected.height}" in result.output
    assert pl.read_parquet(out).equals(expected)
    # Only what the stopped runs had not yet made durable is read again.
    assert calls["n"] < expected.height - 30
    assert not (tmp_path / "pooled.parquet.parts").exists()


def test_cap_boundary_does_not_move_when_a_stopped_export_continues(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, _pool_rows())
    _stop_after(monkeypatch, 12)
    assert _export(db, out).exit_code != 0
    monkeypatch.undo()

    con = sqlite3.connect(db)
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                    [_game(700 + i, "16.15.1", 2000 + i) for i in range(10)])
    con.commit()
    con.close()

    assert _export(db, out).exit_code == 0
    oldest = pl.read_parquet(out).filter(pl.col("patch") == "16.15.1")
    # The 25 kept before the stop's cap, plus the 10 that arrived after it.
    assert oldest.height == 35
    assert oldest["match_id"].n_unique() == 35


@pytest.mark.parametrize("change", ["other_pool", "too_old", "part_damaged"])
def test_parts_that_cannot_be_trusted_are_thrown_away(
    tmp_path: Path, monkeypatch, change: str
) -> None:
    db = tmp_path / "games.db"
    out = tmp_path / "pooled.parquet"
    _make_db(db, _pool_rows())
    _stop_after(monkeypatch, 30)
    assert _export(db, out).exit_code != 0
    monkeypatch.undo()

    parts = tmp_path / "pooled.parquet.parts"
    patches, extra = "16.15,16.16", ()
    if change == "other_pool":
        patches = "16.16"
    elif change == "too_old":
        extra = ("--resume-max-age-hours", "0")
    else:
        first = sorted(parts.glob("part-*.parquet"))[0]
        first.write_bytes(first.read_bytes()[:-1])

    result = _export(db, out, *extra, patches=patches)
    assert result.exit_code == 0, result.output
    assert "resuming" not in result.output
    whole = tmp_path / "whole.parquet"
    assert _export(db, whole, patches=patches).exit_code == 0
    assert pl.read_parquet(out).equals(pl.read_parquet(whole))
    assert not parts.exists()
