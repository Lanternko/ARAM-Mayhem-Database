from __future__ import annotations

import pytest

from aram_nn.lcu import poller


class FakeWriter:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.messages: list[dict] = []

    def submit(self, message: dict) -> dict:
        self.messages.append(dict(message))
        if not self.responses:
            raise AssertionError("unexpected writer request")
        return self.responses.pop(0)


def test_rpc_game_writer_threads_claim_token_through_commit_and_release() -> None:
    fake = FakeWriter(
        [
            {"ok": True, "status": "CLAIMED", "token": "lease-token", "generation": 7},
            {"ok": True, "status": "COMMITTED", "inserted": True},
            {"ok": True, "status": "CLAIMED", "token": "lease-two", "generation": 8},
            {"ok": True, "status": "RELEASED"},
        ]
    )
    writer = poller._RPCGameWriter(fake)

    first = writer.claim("g-1")
    assert first.status == "CLAIMED"
    assert first.claim is not None
    writer.commit(
        first.claim,
        {
            "game_id": "g-1",
            "queue_id": 2400,
            "patch": "16.16",
            "blue_champs": [1, 2, 3, 4, 5],
            "red_champs": [6, 7, 8, 9, 10],
            "blue_wins": 1,
            "duration_sec": 900,
            "created_ms": 1,
            "captured_at": "2026-08-12T00:00:00+00:00",
        },
    )
    second = writer.claim("g-2")
    assert second.claim is not None
    writer.release(second.claim)

    assert fake.messages[1]["token"] == "lease-token"
    assert fake.messages[1]["generation"] == 7
    assert fake.messages[3]["token"] == "lease-two"
    assert fake.messages[3]["generation"] == 8


def test_rpc_game_writer_done_and_busy_do_not_create_claims() -> None:
    fake = FakeWriter(
        [
            {"ok": True, "status": "DONE", "mutated": False},
            {"ok": True, "status": "BUSY", "mutated": False},
        ]
    )
    writer = poller._RPCGameWriter(fake)
    assert writer.claim("done").claim is None
    assert writer.claim("busy").claim is None


def test_rpc_writer_failure_is_fail_closed() -> None:
    class BrokenWriter:
        def submit(self, message: dict) -> dict:
            raise EOFError("writer exited")

    with pytest.raises(poller._RPCWriterError, match="writer game_claim failed"):
        poller._RPCGameWriter(BrokenWriter()).claim("g-1")


def test_rpc_collector_startup_never_opens_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        poller.sqlite3,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("RPC producer opened SQLite")),
    )
    monkeypatch.setattr(
        poller,
        "get_credentials",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    poller.run_collector(writer_client=FakeWriter())
