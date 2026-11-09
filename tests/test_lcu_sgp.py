"""SGP direct match-history: v5 -> legacy conversion, client auth/pacing, fallback."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from aram_nn.lcu import snowball
from aram_nn.lcu.poller import _parse_game_detail
from aram_nn.lcu.sgp import (
    SGPError,
    SGPHistorySource,
    SGPMatchHistoryClient,
    SGPRateLimited,
    sgp_game_to_lcu_detail,
)
from aram_nn.lcu.writer_service import WriterService


def _jwt(exp: float) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'iat': exp - 3600})}.sig"


def _v5_participant(index: int, *, map_lane: str = "NONE") -> dict:
    blue = index < 5
    return {
        "participantId": index + 1,
        "teamId": 100 if blue else 200,
        "championId": 10 + index,
        "puuid": f"puuid-{index}",
        "riotIdGameName": f"name{index}",
        "riotIdTagline": "TW2",
        "summonerName": "",
        "summonerId": 1000 + index,
        "profileIcon": 7,
        "spell1Id": 32,
        "spell2Id": 4,
        "win": blue,
        "kills": index,
        "deaths": 2,
        "assists": 3,
        "goldEarned": 9000,
        "item0": 3089,
        "item1": 0,
        "item6": 3364,
        "playerAugment1": 101,
        "playerAugment2": 0,
        "playerAugment3": 205,
        "totalHealsOnTeammates": 500,
        "totalDamageShieldedOnTeammates": 700,
        "lane": map_lane,
        "role": "NONE" if map_lane == "NONE" else "SOLO",
        "perks": {
            "statPerks": {"defense": 0, "flex": 0, "offense": 0},
            "styles": [
                {"description": "subStyle", "style": 8300, "selections": [{"perk": 8304}, {"perk": 8345}]},
                {
                    "description": "primaryStyle",
                    "style": 8100,
                    "selections": [{"perk": 8112}, {"perk": 8139}, {"perk": 8138}, {"perk": 8135}],
                },
            ],
        },
        "challenges": {"effectiveHealAndShielding": 1234.5, "kda": 3.0},
        "missions": {"x": 1},
    }


def _v5_game(game_id: int = 4242, queue_id: int = 2400, *, players: int = 10, created: int = 1_794_000_000_000) -> dict:
    return {
        "gameId": game_id,
        "queueId": queue_id,
        "gameMode": "KIWI",
        "gameType": "MATCHED_GAME",
        "mapId": 12,
        "platformId": "TW2",
        "gameCreation": created,
        "gameDuration": 1071,
        "gameVersion": "16.22.834.8910",
        "teams": [{"teamId": 100, "win": True}, {"teamId": 200, "win": False}],
        "participants": [_v5_participant(i) for i in range(players)],
    }


def test_converted_game_parses_like_lcu_detail() -> None:
    record = _parse_game_detail(sgp_game_to_lcu_detail(_v5_game()), {2400})
    assert record is not None
    assert record["game_id"] == "4242"
    assert record["queue_id"] == 2400
    assert record["patch"] == "16.22.834"
    assert record["blue_champs"] == [10, 11, 12, 13, 14]
    assert record["red_champs"] == [15, 16, 17, 18, 19]
    assert record["blue_wins"] == 1
    assert record["duration_sec"] == 1071
    assert record["created_ms"] == 1_794_000_000_000
    first = record["participants"][0]
    assert first["augments"] == [101, 205]
    assert first["spells"] == [4, 32]
    assert first["items"] == [3089, 3364]
    # Primary style first even when SGP lists the sub style first.
    assert first["perks"] == {"ids": [8112, 8139, 8138, 8135, 8304, 8345], "styles": [8100, 8300]}
    # Map 12 lane/role are NONE and are not stored.
    assert "lane" not in first and "role" not in first
    # Parity with LCU-sourced rows: LCU never reported these stats, and
    # challenges must not leak v5-only keys into the payload.
    assert "total_heals_on_teammates" not in first["stats"]
    assert "total_damage_shielded_on_teammates" not in first["stats"]
    assert "effective_heal_and_shielding" not in first["stats"]
    private = record["participants_private"][0]
    assert private["puuid"] == "puuid-0"
    assert private["riotId"] == "name0#TW2"
    assert private["accountId"] == 0
    assert snowball._extract_participant_puuids(sgp_game_to_lcu_detail(_v5_game())) == [
        f"puuid-{i}" for i in range(10)
    ]


def test_converted_rows_drive_history_helpers() -> None:
    rows = [sgp_game_to_lcu_detail(_v5_game(1, 2400, created=300)), sgp_game_to_lcu_detail(_v5_game(2, 3270, created=200))]
    assert snowball._extract_target_game_ids(rows, {2400}) == ["1"]
    assert snowball._latest_any_match_created_ms(rows) == 300
    assert snowball._adaptive_target_game_ids(rows, {2400}, current_patch="16.22") == ["1"]


class _FakeLCU:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.calls = 0

    def get(self, path: str):
        assert path == "/entitlements/v1/token"
        self.calls += 1
        return {"accessToken": self.tokens[min(self.calls - 1, len(self.tokens) - 1)]}


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _client(handler, lcu: _FakeLCU, clock: _Clock, *, min_interval: float = 1.0) -> SGPMatchHistoryClient:
    return SGPMatchHistoryClient(
        lcu,
        host="https://sgp.test",
        min_interval_sec=min_interval,
        transport=httpx.MockTransport(handler),
        clock=clock,
        monotonic=clock,
        sleep=clock.sleep,
    )


def test_player_games_sends_bearer_and_returns_v5_blocks() -> None:
    clock = _Clock()
    token = _jwt(clock.now + 3600)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"games": [{"metadata": {}, "json": _v5_game()}, {"json": None}]})

    client = _client(handler, _FakeLCU([token]), clock)
    games = client.player_games("abc", count=20)
    assert [g["gameId"] for g in games] == [4242]
    assert seen[0].url.path == "/match-history-query/v1/products/lol/player/abc/SUMMARY"
    assert seen[0].url.params["count"] == "20"
    assert seen[0].headers["Authorization"] == f"Bearer {token}"


def test_token_is_cached_until_near_expiry_and_paced() -> None:
    clock = _Clock()
    lcu = _FakeLCU([_jwt(clock.now + 3600), _jwt(clock.now + 7200)])
    client = _client(lambda _r: httpx.Response(200, json={"games": []}), lcu, clock, min_interval=1.5)
    client.player_games("a")
    client.player_games("b")
    assert lcu.calls == 1
    assert clock.slept == [pytest.approx(1.5)]
    clock.now += 3600 - 200  # inside the refresh margin
    client.player_games("c")
    assert lcu.calls == 2


def test_401_refreshes_token_once() -> None:
    clock = _Clock()
    old, new = _jwt(clock.now + 3600), _jwt(clock.now + 3601)
    lcu = _FakeLCU([old, new])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {old}":
            return httpx.Response(401)
        return httpx.Response(200, json={"games": []})

    assert _client(handler, lcu, clock).player_games("a") == []
    assert lcu.calls == 2


def test_404_is_empty_and_429_raises_with_retry_after() -> None:
    clock = _Clock()
    lcu = _FakeLCU([_jwt(clock.now + 3600)])
    assert _client(lambda _r: httpx.Response(404), lcu, clock).player_games("a") == []
    limited = _client(lambda _r: httpx.Response(429, headers={"Retry-After": "120"}), lcu, clock)
    with pytest.raises(SGPRateLimited) as info:
        limited.player_games("a")
    assert info.value.retry_after_sec == 120
    with pytest.raises(SGPRateLimited):
        limited.player_games("a")  # still inside the server's window: no request sent


def test_errors_never_carry_the_token() -> None:
    clock = _Clock()
    token = _jwt(clock.now + 3600)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SGPError) as info:
        _client(handler, _FakeLCU([token]), clock).player_games("a")
    assert token not in str(info.value)
    assert info.value.__cause__ is None


class _ScriptedClient:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def player_games(self, _puuid: str, *, count: int = 20):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def game(self, _game_id):
        return _v5_game()

    def close(self) -> None:
        pass


def test_history_source_circuit_breaker() -> None:
    clock = _Clock()
    logs: list[str] = []
    scripted = _ScriptedClient([SGPError("x"), SGPError("x"), [_v5_game()]])
    source = SGPHistorySource(scripted, failure_threshold=2, cooldown_sec=300, log=logs.append, monotonic=clock)
    assert source.history("p", 20) is None
    assert source.history("p", 20) is None  # second failure trips the breaker
    assert source.history("p", 20) is None  # open: SGP not called
    assert scripted.calls == 2
    clock.now += 301
    rows = source.history("p", 20)
    assert rows is not None and rows[0]["gameId"] == 4242
    assert source.stats.visits == 1 and source.stats.fallbacks == 3
    assert any("circuit open" in line for line in logs) and any("circuit closed" in line for line in logs)


def test_rate_limit_opens_breaker_for_retry_after() -> None:
    clock = _Clock()
    scripted = _ScriptedClient([SGPRateLimited(900), [_v5_game()]])
    source = SGPHistorySource(scripted, cooldown_sec=300, log=lambda _l: None, monotonic=clock)
    assert source.history("p", 20) is None
    clock.now += 600
    assert source.history("p", 20) is None and scripted.calls == 1
    clock.now += 301
    assert source.history("p", 20) is not None


class _FakeLCUContext:
    def __init__(self, _credentials: object) -> None:
        pass

    def __enter__(self) -> "_FakeLCUContext":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _run_rpc_snowball(tmp_path: Path, monkeypatch, sgp_outcomes: list) -> tuple:
    service = WriterService(tmp_path / "writer.db")
    lcu_calls: list[str] = []

    class _Client:
        def submit(self, message):
            return service.handle(message, channel_id="rpc-test")

    created = 1_794_000_000_000
    monkeypatch.setattr(snowball, "get_credentials", lambda: object())
    monkeypatch.setattr(snowball, "LCUClient", _FakeLCUContext)
    monkeypatch.setattr(snowball, "get_game_version", lambda _lcu: "16.22.834.8910")
    monkeypatch.setattr(snowball, "get_current_summoner", lambda _lcu: {"puuid": "puuid-0", "gameName": "me"})

    def lcu_history(_lcu, _puuid, begin=0, end=20):
        lcu_calls.append("history")
        return [{"gameId": 4242, "queueId": 2400, "gameCreation": created, "gameVersion": "16.22.834.8910"}]

    def lcu_detail(_lcu, _game_id):
        lcu_calls.append("detail")
        return sgp_game_to_lcu_detail(_v5_game(created=created))

    monkeypatch.setattr(snowball, "get_match_history", lcu_history)
    monkeypatch.setattr(snowball, "get_game_detail", lcu_detail)
    scripted = _ScriptedClient(sgp_outcomes)
    monkeypatch.setattr(
        snowball,
        "_build_sgp_history_source",
        lambda _lcu, **_kw: SGPHistorySource(scripted, log=lambda _l: None),
    )
    try:
        stats = snowball.run_snowball(
            tmp_path / "unused.db",
            target_games=1,
            max_players=1,
            history_window=20,
            games_per_player=0,
            include_self=True,
            include_friends=False,
            suggested_cap=0,
            target_queues={2400},
            writer_client=_Client(),
            history_source="sgp",
        )
        stored = [tuple(row) for row in service.con.execute("SELECT game_id, queue_id FROM games").fetchall()]
    finally:
        service.close()
    return stats, stored, lcu_calls, scripted


def test_sgp_producer_saves_without_any_lcu_history_or_detail(tmp_path: Path, monkeypatch) -> None:
    stats, stored, lcu_calls, scripted = _run_rpc_snowball(
        tmp_path, monkeypatch, [[_v5_game(created=1_794_000_000_000)]]
    )
    assert stats.saved_games == 1
    assert stored == [("4242", 2400)]
    assert lcu_calls == []
    assert scripted.calls == 1


def test_sgp_failure_falls_back_to_lcu_for_that_visit(tmp_path: Path, monkeypatch) -> None:
    stats, stored, lcu_calls, _ = _run_rpc_snowball(tmp_path, monkeypatch, [SGPError("down")])
    assert stats.saved_games == 1
    assert stored == [("4242", 2400)]
    assert lcu_calls == ["history", "detail"]


def test_unknown_history_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        snowball.run_snowball(tmp_path / "x.db", history_source="riot")
