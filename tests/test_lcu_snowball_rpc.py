from __future__ import annotations

from pathlib import Path

from aram_nn.lcu import snowball
from aram_nn.lcu.writer_service import WriterService


class _FakeLCU:
    def __init__(self, _credentials: object) -> None:
        pass

    def __enter__(self) -> "_FakeLCU":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _detail() -> dict[str, object]:
    participants = [
        {
            "teamId": 100 if index < 5 else 200,
            "championId": index + 1,
            "participantId": index + 1,
            "stats": {"win": index < 5},
        }
        for index in range(10)
    ]
    return {
        "gameId": "g1",
        "queueId": 2400,
        "gameDuration": 900,
        "gameVersion": "16.1.1",
        "gameCreation": 123,
        "participants": participants,
        "teams": [{"teamId": 100, "win": True}, {"teamId": 200, "win": False}],
        "participantIdentities": [
            {"participantId": index + 1, "player": {"puuid": f"p{index}"}}
            for index in range(10)
        ],
    }


def test_rpc_snowball_bounded_flow_and_no_producer_db_open(tmp_path: Path, monkeypatch) -> None:
    service = WriterService(tmp_path / "writer.db")

    class _Client:
        def submit(self, message):
            return service.handle(message, channel_id="rpc-test")

    monkeypatch.setattr(snowball, "get_credentials", lambda: object())
    monkeypatch.setattr(snowball, "LCUClient", _FakeLCU)
    monkeypatch.setattr(snowball, "get_game_version", lambda _lcu: "16.1.1")
    monkeypatch.setattr(snowball, "get_current_summoner", lambda _lcu: {"puuid": "p0", "gameName": "me"})
    monkeypatch.setattr(
        snowball,
        "get_match_history",
        lambda _lcu, _puuid, begin=0, end=20: [
            {"gameId": "g1", "queueId": 2400, "gameCreation": 123, "gameVersion": "16.1.1"}
        ],
    )
    monkeypatch.setattr(snowball, "get_game_detail", lambda _lcu, _game_id: _detail())
    monkeypatch.setattr(
        snowball.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RPC producer opened SQLite")),
    )

    try:
        stats = snowball.run_snowball(
            tmp_path / "producer-must-not-open.db",
            target_games=1,
            max_players=1,
            history_window=20,
            games_per_player=1,
            include_self=True,
            include_friends=False,
            suggested_cap=0,
            target_queues={2400},
            writer_client=_Client(),
        )
    finally:
        service.close()

    assert stats.saved_games == 1
    assert service.con is not None

