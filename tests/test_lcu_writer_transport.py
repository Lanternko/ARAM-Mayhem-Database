from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import pathlib
import sqlite3
import time

import pytest

from aram_nn.lcu.writer_protocol import MAX_FRAME_BYTES, encode_frame
from aram_nn.lcu.writer_supervisor import WriterSupervisor
from aram_nn.lcu.writer_transport import (
    WriterLifecycleError,
    WriterUnavailableError,
)


def _ping(request_id: str) -> dict[str, object]:
    return {"version": 1, "command": "ping", "request_id": request_id}


def test_two_channels_match_responses_and_sparse_client_is_not_starved(
    tmp_path: pathlib.Path,
) -> None:
    supervisor = WriterSupervisor(tmp_path / "games.db", 2)
    clients = supervisor.start()
    try:
        def busy() -> list[str]:
            return [clients[0].submit(_ping(f"busy-{index}"))["request_id"] for index in range(30)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            busy_result = pool.submit(busy)
            sparse_result = pool.submit(clients[1].submit, _ping("sparse"))
            assert sparse_result.result(timeout=5)["request_id"] == "sparse"
            assert busy_result.result(timeout=10) == [f"busy-{index}" for index in range(30)]
        assert clients[0].channel_id != clients[1].channel_id
        metrics = supervisor.metrics()
        assert metrics.frames_succeeded == 31
        assert metrics.frames_rejected == 0
    finally:
        supervisor.shutdown()


def test_invalid_and_oversized_frames_do_not_mutate_and_writer_survives(
    tmp_path: pathlib.Path,
) -> None:
    db_path = tmp_path / "games.db"
    supervisor = WriterSupervisor(db_path, 2)
    clients = supervisor.start()
    try:
        # Exercise the hostile producer boundary directly.  The normal client
        # refuses oversized frames before they reach this point.
        clients[0]._connection.send_bytes(b"not-json")
        invalid = json.loads(clients[0]._connection.recv_bytes(MAX_FRAME_BYTES).decode("utf-8"))
        assert invalid == {"ok": False, "status": "INVALID_FRAME"}
        assert clients[0].submit(_ping("after-invalid"))["status"] == "PONG"

        with pytest.raises((EOFError, OSError)):
            clients[0]._connection.send_bytes(b"x" * (MAX_FRAME_BYTES + 1))
            clients[0]._connection.recv_bytes(MAX_FRAME_BYTES)
        assert clients[1].submit(_ping("other-channel"))["status"] == "PONG"

        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM writer_requests").fetchone()[0] == 2
    finally:
        supervisor.shutdown()


def test_writer_death_is_fail_closed_without_fallback(tmp_path: pathlib.Path) -> None:
    supervisor = WriterSupervisor(tmp_path / "games.db", 1)
    client = supervisor.start()[0]
    assert client.submit(_ping("alive"))["status"] == "PONG"
    supervisor.abort()
    with pytest.raises(WriterUnavailableError, match="WRITER_UNAVAILABLE"):
        client.submit(_ping("after-death"))


def test_stop_claims_allows_control_work_then_shutdown_drains(
    tmp_path: pathlib.Path,
) -> None:
    supervisor = WriterSupervisor(tmp_path / "games.db", 2, drain_timeout=2.0)
    clients = supervisor.start()
    supervisor.stop_claims()
    assert supervisor.state == "STOP_CLAIMS"
    with pytest.raises(WriterLifecycleError, match="CLAIMS_STOPPED"):
        clients[0].submit(
            {
                "version": 1,
                "command": "snowball_queue",
                "operation": "claim_next",
                "request_id": "late-claim",
                "classic_claim_percent": 0,
                "claim_timeout_ms": 300_000,
            }
        )
    with pytest.raises(WriterLifecycleError, match="CLAIMS_STOPPED"):
        clients[0].submit(
            {
                "version": 1,
                "command": "player_claim",
                "request_id": "late-player",
                "puuid": "p",
            }
        )
    # A producer that already holds a player must still be able to claim
    # that player's games and finish control work.
    held = clients[0].submit(
        {
            "version": 1,
            "command": "game_claim",
            "request_id": "held-game",
            "game_id": "g",
        }
    )
    assert held["status"] in {"CLAIMED", "BUSY", "DONE"}
    assert clients[1].submit(_ping("during-stop"))["status"] == "PONG"
    supervisor.shutdown()
    assert supervisor.state == "CLOSED"
    with pytest.raises(WriterUnavailableError):
        clients[1].submit(_ping("closed"))


def test_duplicate_request_replays_on_same_transport_channel(tmp_path: pathlib.Path) -> None:
    supervisor = WriterSupervisor(tmp_path / "games.db", 1)
    client = supervisor.start()[0]
    try:
        first = client.submit(_ping("same"))
        assert client.submit(_ping("same")) == first
    finally:
        supervisor.shutdown()


def test_transport_sources_use_only_byte_connection_api() -> None:
    package = pathlib.Path(__file__).parents[1] / "src" / "aram_nn" / "lcu"
    source = (package / "writer_transport.py").read_text(encoding="utf-8") + (
        package / "writer_supervisor.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        ".send(",
        ".recv(",
        "multiprocessing.connection.Listener",
        "multiprocessing.connection.Client",
        "import pickle",
        "import socket",
    )
    assert not [token for token in forbidden if token in source]
    assert ".send_bytes(" in source
    assert ".recv_bytes(" in source
