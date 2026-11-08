from __future__ import annotations

from pathlib import Path

import pytest

from aram_nn.lcu.id_gap import CURSOR_KEY, IdGapConfig, IdGapSweeper, ProbeResult
from aram_nn.lcu.snowball import SnowballWriterFacade
from aram_nn.lcu.writer_protocol import ProtocolError, validate_message
from aram_nn.lcu.writer_service import WriterService

HOUR_MS = 3600_000
NOW_MS = 1_800_000_000_000


def _detail(game_id: int, *, queue: int = 2400, created_ms: int = NOW_MS - 10 * HOUR_MS) -> dict:
    participants = [
        {"teamId": 100 if i < 5 else 200, "championId": i + 1, "participantId": i + 1, "stats": {"win": i < 5}}
        for i in range(10)
    ]
    return {
        "gameId": game_id,
        "queueId": queue,
        "gameDuration": 900,
        "gameVersion": "16.22.1",
        "gameCreation": created_ms,
        "participants": participants,
        "teams": [{"teamId": 100, "win": True}, {"teamId": 200, "win": False}],
        "participantIdentities": [
            {"participantId": i + 1, "player": {"puuid": f"p{game_id}-{i}"}} for i in range(10)
        ],
    }


def _stored_game(game_id: str, created_ms: int) -> dict:
    return {
        "game_id": game_id, "queue_id": 2400, "patch": "16.22", "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10], "blue_wins": 1, "duration_sec": 900,
        "created_ms": created_ms, "captured_at": "2026-11-09T00:00:00+00:00", "participants": [],
    }


class _Channel:
    def __init__(self, service: WriterService) -> None:
        self.service = service

    def submit(self, message):
        return self.service.handle(message, channel_id="G01")


def _store(service: WriterService, game_id: str, created_ms: int) -> None:
    claim = service.handle({"version": 1, "command": "game_claim", "request_id": f"c{game_id}", "game_id": game_id}, channel_id="seed")
    service.handle(
        {"version": 1, "command": "commit_game", "request_id": f"k{game_id}", "game_id": game_id,
         "token": claim["token"], "generation": claim["generation"], "record": _stored_game(game_id, created_ms)},
        channel_id="seed",
    )


class _Probe:
    def __init__(self, answers: dict[int, ProbeResult]) -> None:
        self.answers = answers
        self.calls: list[int] = []

    def __call__(self, game_id: int) -> ProbeResult:
        self.calls.append(game_id)
        answer = self.answers.get(game_id, ProbeResult(404, None))
        if callable(answer):
            return answer()
        return answer


def _sweeper(service: WriterService, probe, **config) -> IdGapSweeper:
    config.setdefault("rps", 0)
    config.setdefault("batch_size", 10)
    return IdGapSweeper(
        SnowballWriterFacade(_Channel(service)), probe, IdGapConfig(**config),
        clock=lambda: NOW_MS / 1000, sleep=lambda _s: None, log=lambda _line: None,
    )


# ------------------------------------------------------------------ writer side
def test_game_index_known_splits_by_digit_length_and_skips_journal(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    for gid in ("99998", "99999", "100000", "100002", "1000001", "abc"):
        _store(service, gid, NOW_MS - 10 * HOUR_MS)
    journal_before = service.con.execute("SELECT COUNT(*) FROM writer_requests").fetchone()[0]
    response = service.handle(
        {"version": 1, "command": "game_index", "request_id": "k", "operation": "known", "id_lo": 99999, "id_hi": 100005},
        channel_id="G01",
    )
    # "1000001" sorts between "100000" and "100005" as text but is outside the range.
    assert response["known"] == [99999, 100000, 100002]
    assert service.con.execute("SELECT COUNT(*) FROM writer_requests").fetchone()[0] == journal_before


def test_game_index_known_rejects_oversized_range(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    response = service.handle(
        {"version": 1, "command": "game_index", "request_id": "k", "operation": "known", "id_lo": 1, "id_hi": 1_000_000},
        channel_id="G01",
    )
    assert response["ok"] is False and response["status"] == "INVALID_RANGE"


def test_game_index_latest_returns_newest_id_created_before(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    _store(service, "461000000", NOW_MS - 30 * HOUR_MS)
    _store(service, "461500000", NOW_MS - 20 * HOUR_MS)
    _store(service, "462000000", NOW_MS - 1 * HOUR_MS)
    _store(service, "99999999", NOW_MS - 50 * HOUR_MS)  # shorter ID, larger as text
    facade = SnowballWriterFacade(_Channel(service))
    assert facade.latest_game_id(created_before_ms=NOW_MS - 10 * HOUR_MS) == 461500000
    assert facade.latest_game_id(created_before_ms=NOW_MS - 40 * HOUR_MS) == 99999999
    assert facade.latest_game_id(created_before_ms=0) is None


def test_game_index_protocol_rejects_unknown_fields_and_negative_ids() -> None:
    base = {"version": 1, "command": "game_index", "request_id": "r", "operation": "known"}
    validate_message({**base, "id_lo": 1, "id_hi": 2})
    with pytest.raises(ProtocolError):
        validate_message({**base, "id_lo": -1, "id_hi": 2})
    with pytest.raises(ProtocolError):
        validate_message({**base, "sql": "select 1"})


# ----------------------------------------------------------------- sweeper side
def test_sweep_commits_only_unknown_target_games_with_id_gap_family(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    _store(service, "1003", NOW_MS - 10 * HOUR_MS)
    probe = _Probe({
        1001: ProbeResult(200, _detail(1001)),
        1002: ProbeResult(200, _detail(1002, queue=420)),
        1004: ProbeResult(200, _detail(1004, queue=450)),
    })
    sweeper = _sweeper(service, probe)
    next_cursor, state = sweeper.sweep_batch(1000)
    assert (next_cursor, state) == (1010, "progress")
    assert 1003 not in probe.calls  # already stored: never probed
    rows = dict(service.con.execute("SELECT game_id, seed_family FROM games").fetchall())
    assert rows == {"1003": "match", "1001": "id_gap", "1004": "id_gap"}
    # 404s and other-queue games leave no claim rows behind.
    claimed = {r[0] for r in service.con.execute("SELECT game_id FROM writer_game_claims")}
    assert claimed == {"1003", "1001", "1004"}
    # Participants are not pushed into the snowball frontier by default.
    assert service.con.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0] == 0
    assert sweeper.stats.saved == 2 and sweeper.stats.other_queue == 1


def test_sweep_stops_at_lag_frontier_without_skipping(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    probe = _Probe({
        1001: ProbeResult(200, _detail(1001)),
        1003: ProbeResult(200, _detail(1003, created_ms=NOW_MS - HOUR_MS)),  # younger than the 4h lag
        1004: ProbeResult(200, _detail(1004)),
    })
    next_cursor, state = _sweeper(service, probe).sweep_batch(1000)
    assert (next_cursor, state) == (1003, "caught_up")
    # 1004 may have been prefetched by a concurrent probe, but is never judged
    # or stored past the frontier.
    assert {r[0] for r in service.con.execute("SELECT game_id FROM games")} == {"1001"}


def test_sweep_never_skips_an_id_without_an_lcu_verdict(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    probe = _Probe({1002: ProbeResult(-1, None)})
    sweeper = _sweeper(service, probe, max_transport_errors=2)
    assert sweeper.sweep_batch(1000) == (1002, "retry")
    assert sweeper.sweep_batch(1002) == (1002, "lcu_down")


def test_run_persists_cursor_and_cold_starts_from_lookback_anchor(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    _store(service, "500000", NOW_MS - 30 * HOUR_MS)
    _store(service, "600000", NOW_MS - 2 * HOUR_MS)
    probe = _Probe({500001: ProbeResult(200, _detail(500001))})
    sweeper = _sweeper(service, probe, start_lookback_hours=24)
    sweeper.run(max_batches=2)
    assert probe.calls[0] == 500001
    facade = SnowballWriterFacade(_Channel(service))
    assert facade.runtime_get(CURSOR_KEY) == "500021"
    # A restarted sweeper resumes from the stored cursor, not the anchor.
    probe2 = _Probe({})
    _sweeper(service, probe2).run(max_batches=1)
    assert probe2.calls[0] == 500021


def test_long_404_run_is_treated_as_frontier_then_skipped_when_dead(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    facade = SnowballWriterFacade(_Channel(service))
    facade.runtime_set(CURSOR_KEY, "1000")
    probe = _Probe({})  # everything 404
    sweeper = _sweeper(service, probe, batch_size=50, max_not_found_run=25, dead_block_after_pauses=2, caught_up_sleep_sec=0)
    # Pauses 1..2 hold the cursor at the start of the run; pause 3 skips it.
    sweeper.run(max_batches=2)
    assert facade.runtime_get(CURSOR_KEY) == "1000"
    sweeper.run(max_batches=1)
    assert facade.runtime_get(CURSOR_KEY) == "1025"


def test_busy_or_done_claim_is_left_alone(tmp_path: Path) -> None:
    service = WriterService(tmp_path / "games.db")
    service.handle({"version": 1, "command": "game_claim", "request_id": "other", "game_id": "1001", "lease_ms": 600_000}, channel_id="W01")
    probe = _Probe({1001: ProbeResult(200, _detail(1001))})
    sweeper = _sweeper(service, probe)
    sweeper.sweep_batch(1000)
    assert sweeper.stats.already_claimed == 1 and sweeper.stats.saved == 0
    assert service.con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
