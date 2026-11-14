from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tierlist_engine as engine  # noqa: E402
from aram_nn.site import patch_extract as px  # noqa: E402
from aram_nn.site.db import ensure_public_schema  # noqa: E402

QUEUE = 2400
PATCH = "16.22"
ITEMS = (3031, 3072, 3153, 6672, 3046, 3094)
AUGMENTS = (1116, 1348, 1046, 3081)


def _participants(seed: int, *, with_stats: bool = True, size: int = 10) -> list[dict]:
    out = []
    for slot in range(size):
        held = [ITEMS[(seed + slot + k) % len(ITEMS)] for k in range(1 + (seed + slot) % 3)]
        participant = {
            "championId": 1 + (seed * 7 + slot * 3) % 23,
            "teamId": 100 if slot < size // 2 else 200,
            "augments": [AUGMENTS[(seed + slot) % len(AUGMENTS)], AUGMENTS[(seed + 1) % len(AUGMENTS)]],
            "items": held,
            "itemSlots": held + [0] * (7 - len(held)),
            "spells": [4, 6 + slot % 2],
            "role": "SUPPORT",
        }
        if with_stats:
            participant["stats"] = {
                "kills": slot,
                "deaths": seed % 5,
                "total_damage_dealt_to_champions": 1000 * (slot + 1),
                "total_damage_taken": 500 + seed,
                "gold_earned": 9000,
            }
        out.append(participant)
    return out


def _insert(con: sqlite3.Connection, game_id: str, *, seed: int, patch: str = "16.22.1",
            queue_id: int = QUEUE, created_ms: int | None = None, participants=...) -> None:
    if participants is ...:
        participants = _participants(seed)
    blue = [p["championId"] for p in (participants or []) if p.get("teamId") == 100] or [1, 2, 3, 4, 5]
    red = [p["championId"] for p in (participants or []) if p.get("teamId") == 200] or [6, 7, 8, 9, 10]
    con.execute(
        "INSERT INTO games (game_id, queue_id, patch, blue_champs, red_champs, blue_wins, "
        "duration_sec, created_ms, captured_at, participants_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            game_id, queue_id, patch, json.dumps(blue), json.dumps(red), seed % 2, 900 + seed,
            created_ms if created_ms is not None else 1_000_000 + seed, "2026-11-14T00:00:00Z",
            None if participants is None else json.dumps(participants),
        ),
    )


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    db = tmp_path / "games.db"
    con = sqlite3.connect(str(db))
    ensure_public_schema(con)
    for i in range(60):
        _insert(con, f"TW_{i}", seed=i)
    for i in range(8):  # other scopes must never leak into the replica
        _insert(con, f"TW_old_{i}", seed=i, patch="16.21.1")
        _insert(con, f"TW_q_{i}", seed=i, queue_id=450)
        _insert(con, f"TW_next_{i}", seed=i, patch="16.2.1" if i % 2 else "16.23.1")
    con.commit()
    con.close()
    yield db
    px.clear_scan_dbs()


def _sync(source: Path, tmp_path: Path, **kwargs) -> px.PatchExtract:
    return px.sync_patch_extract(
        source, queue_id=QUEUE, patch_prefix=PATCH, extract_dir=tmp_path / "extract",
        log=lambda _msg: None, **kwargs,
    )


def _ids(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {str(r[0]) for r in con.execute("SELECT game_id FROM games")}
    finally:
        con.close()


def test_patch_range_matches_like_prefix(source: Path) -> None:
    con = sqlite3.connect(str(source))
    for prefix in ("16.22", "16.2", "16.22.", "16.21", "16"):
        like = {r[0] for r in con.execute("SELECT game_id FROM games WHERE patch LIKE ?", (f"{prefix}%",))}
        lo, hi = px.patch_range(prefix)
        ranged = {r[0] for r in con.execute("SELECT game_id FROM games WHERE patch>=? AND patch<?", (lo, hi))}
        assert ranged == like, prefix
    con.close()


def test_range_predicate_seeks_the_patch_index(source: Path) -> None:
    con = sqlite3.connect(str(source))
    plan = " ".join(
        str(r[-1])
        for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch >= ? AND patch < ?",
            (QUEUE, *px.patch_range(PATCH)),
        )
    )
    con.close()
    assert "patch>?" in plan and "patch<?" in plan


def test_slim_participants_drops_stats_only() -> None:
    raw = json.dumps(_participants(3))
    lean_json, stats_json, poor = px.slim_participants(raw)
    original = json.loads(raw)
    lean = json.loads(lean_json)
    assert not poor
    assert lean == [{k: v for k, v in p.items() if k != "stats"} for p in original]
    stats = json.loads(stats_json)
    assert [s["championId"] for s in stats] == [p["championId"] for p in original]
    assert stats[2]["stats"] == {
        "total_damage_dealt_to_champions": 3000,
        "total_damage_taken": 503,
    }


@pytest.mark.parametrize("raw", [None, "", "[]", "not json", '{"a": 1}', "[1, 2]"])
def test_slim_participants_passes_odd_payloads_through(raw) -> None:
    lean_json, stats_json, _poor = px.slim_participants(raw)
    if raw == "[]":
        assert json.loads(lean_json) == [] and json.loads(stats_json) == []
    else:
        assert lean_json == raw and stats_json == raw


def test_full_then_incremental_sync(source: Path, tmp_path: Path) -> None:
    first = _sync(source, tmp_path)
    assert first.rebuilt and first.games == 60 and first.added == 60
    assert _ids(first.lean_db) == _ids(first.stats_db) == {f"TW_{i}" for i in range(60)}

    con = sqlite3.connect(str(source))
    # Back-filled games are older than everything already synced: a created_ms
    # watermark would miss them, the rowid tail does not.
    _insert(con, "TW_back", seed=100, created_ms=5)
    _insert(con, "TW_new", seed=101)
    _insert(con, "TW_other_patch", seed=102, patch="16.21.1")
    con.commit()
    con.close()

    second = _sync(source, tmp_path)
    assert not second.rebuilt and second.added == 2 and second.games == 62
    assert {"TW_back", "TW_new"} <= _ids(second.lean_db)
    assert "TW_other_patch" not in _ids(second.lean_db)

    third = _sync(source, tmp_path)
    assert not third.rebuilt and third.added == 0 and third.games == 62


def test_in_place_payload_upgrade_is_picked_up(source: Path, tmp_path: Path) -> None:
    con = sqlite3.connect(str(source))
    _insert(con, "TW_poor", seed=7, participants=_participants(7, with_stats=False))
    _insert(con, "TW_null", seed=8, participants=None)
    con.commit()
    _sync(source, tmp_path)

    rich = json.dumps(_participants(7))
    con.execute("UPDATE games SET participants_json=? WHERE game_id='TW_poor'", (rich,))
    con.execute("UPDATE games SET participants_json=? WHERE game_id='TW_null'", (rich,))
    con.commit()
    con.close()

    result = _sync(source, tmp_path)
    assert not result.rebuilt and result.added == 0 and result.rechecked == 2
    stats = sqlite3.connect(str(result.stats_db))
    for game_id in ("TW_poor", "TW_null"):
        payload = json.loads(
            stats.execute("SELECT participants_json FROM games WHERE game_id=?", (game_id,)).fetchone()[0]
        )
        assert payload[0]["stats"]["total_damage_dealt_to_champions"] == 1000
    stats.close()
    # Upgraded rows stop being re-read.
    assert _sync(source, tmp_path).rechecked == 0


def test_source_shrink_triggers_one_full_rebuild(source: Path, tmp_path: Path) -> None:
    _sync(source, tmp_path)
    con = sqlite3.connect(str(source))
    con.execute("DELETE FROM games WHERE game_id='TW_3'")
    con.commit()
    con.close()
    result = _sync(source, tmp_path)
    assert result.rebuilt and result.games == 59
    assert "TW_3" not in _ids(result.lean_db)


def test_version_bump_rebuilds(source: Path, tmp_path: Path, monkeypatch) -> None:
    _sync(source, tmp_path)
    monkeypatch.setattr(px, "EXTRACT_VERSION", px.EXTRACT_VERSION + 1)
    assert _sync(source, tmp_path).rebuilt


def test_prune_removes_only_other_patches_of_the_queue(source: Path, tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    stale = [extract_dir / "q2400-16.21.lean.db", extract_dir / "q2400-16.21.stats.db"]
    keep = extract_dir / "q450-16.21.lean.db"
    for path in (*stale, keep):
        path.write_bytes(b"")
    result = _sync(source, tmp_path)
    assert not any(path.exists() for path in stale)
    assert keep.exists() and result.lean_db.exists() and result.stats_db.exists()


def test_scan_routing_is_exact_scope(source: Path, tmp_path: Path) -> None:
    result = _sync(source, tmp_path)
    px.register_scan_db(source, QUEUE, PATCH, result.lean_db)
    assert px.scan_db(source, QUEUE, PATCH) == result.lean_db
    assert px.scan_db(source, QUEUE, "16.21") == source
    assert px.scan_db(source, 450, PATCH) == source
    assert px.scan_db(source, QUEUE, None) == source
    # The closed patch still counts from the source even while routing is on.
    assert engine.count_patch_games(source, QUEUE, "16.21") == 8
    assert engine.count_patch_games(source, QUEUE, PATCH) == 60


def test_engine_scans_agree_between_source_and_replica(source: Path, tmp_path: Path) -> None:
    con = sqlite3.connect(str(source))
    _insert(con, "TW_nopj", seed=9, participants=None)
    con.commit()
    con.close()
    items_ok = set(ITEMS)

    def run() -> tuple:
        return (
            engine.count_patch_games(source, QUEUE, PATCH),
            engine.count_participant_games(source, QUEUE, PATCH),
            engine._scan_patch_counters(source, QUEUE, PATCH),
            engine._scan_destiny_counters(source, QUEUE, PATCH, items_ok),
        )

    direct = run()
    result = _sync(source, tmp_path)
    px.register_scan_db(source, QUEUE, PATCH, result.lean_db)
    assert run() == direct
    assert direct[0] == 61 and direct[1] == 60


def test_settled_destiny_items_reuse_frozen_counters(source: Path, tmp_path: Path, monkeypatch) -> None:
    items_ok = set(ITEMS)
    kwargs = {"min_side_games": 1, "min_destiny_games": 1}
    snapshot_dir = tmp_path / "snapshots"
    direct = engine.compute_champ_destiny_items(source, QUEUE, PATCH, items_ok)
    first = engine.settled_champ_destiny_items(source, QUEUE, PATCH, items_ok, snapshot_dir=snapshot_dir)
    assert first == direct

    counters = engine._scan_destiny_counters(source, QUEUE, PATCH, items_ok)
    assert engine.compute_champ_destiny_items(
        source, QUEUE, PATCH, items_ok, counters=counters, **kwargs
    ) == engine.compute_champ_destiny_items(source, QUEUE, PATCH, items_ok, **kwargs)

    def _no_scan(*_args, **_kwargs):
        raise AssertionError("closed patch was rescanned")

    monkeypatch.setattr(engine, "_scan_destiny_counters", _no_scan)
    assert engine.settled_champ_destiny_items(
        source, QUEUE, PATCH, items_ok, snapshot_dir=snapshot_dir
    ) == direct
    # A different legendary catalogue invalidates the frozen tallies.
    with pytest.raises(AssertionError):
        engine.settled_champ_destiny_items(
            source, QUEUE, PATCH, items_ok | {9999}, snapshot_dir=snapshot_dir
        )
