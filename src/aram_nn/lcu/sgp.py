"""Direct SGP match-history reads that bypass the LeagueClient cache.

Why this exists
---------------
Every LCU ``/lol-match-history/v1/products/lol/{puuid}/matches`` call makes
LeagueClient fetch that puuid's match summaries from Riot's SGP service and
keep them in a process cache that is never evicted: +8-10MB per visited
player, which is what forces a planned client restart every ~40 minutes (see
``leagueclient_matchhistory_cache_leak`` in the operations notes).  The client
itself just calls

    GET https://<sgp-host>/match-history-query/v1/products/lol/player/{puuid}/SUMMARY

with the access token LCU hands out at ``/entitlements/v1/token``.  Calling it
directly returns the same games -- newest first, all ten participants, match-v5
shape -- in one ~135KB gzip response for a 20-game window, and LeagueClient
never sees the data, so nothing accumulates.  One call also replaces the 1-20
per-game ``/lol-match-history/v1/games/{id}`` reads that followed each history
read, and it does not queue behind the LCU match-history plugin, which serves
one request at a time.

``sgp_game_to_lcu_detail`` turns a v5 game into the legacy LCU ``games/{id}``
shape so every existing parser and history helper runs unchanged.  Verified
2026-11-09 against 31 games already stored from LCU (queues 450/2400/2450/4310,
incl. 經典 lanes, runes and vision): ``_parse_game_detail`` output matched
field for field once the two LCU-absent stats below were dropped.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

DEFAULT_SGP_HOST = "https://apse1-red.pp.sgp.pvp.net"
DEFAULT_PLATFORM_PREFIX = "TW2"

# v5 reports these, LCU's legacy games/{id} never did.  Dropped so a game's
# stored payload does not depend on which source captured it: during a mixed
# LCU/SGP fleet, rows without them would read as 0 under ``.get(key, 0)`` and
# bias any per-champion average toward SGP-captured games.  Revisit once every
# producer reads from SGP.
_LCU_ABSENT_STATS = frozenset({"totalHealsOnTeammates", "totalDamageShieldedOnTeammates"})

# Refresh the access token this long before its JWT ``exp``.
_TOKEN_REFRESH_MARGIN_SEC = 300.0
# Used when the token's ``exp`` cannot be read; LCU tokens live 60 minutes.
_TOKEN_FALLBACK_LIFETIME_SEC = 45 * 60.0
_DEFAULT_RATE_LIMIT_COOLDOWN_SEC = 60.0


class SGPError(RuntimeError):
    """SGP did not produce a usable answer; callers fall back to LCU."""


class SGPAuthError(SGPError):
    """No access token from LCU, or SGP rejected a freshly fetched one."""


class SGPRateLimited(SGPError):
    def __init__(self, retry_after_sec: float) -> None:
        super().__init__(f"SGP rate limited; retry after {retry_after_sec:.0f}s")
        self.retry_after_sec = float(retry_after_sec)


def _jwt_exp(token: str) -> float | None:
    """Return the unverified ``exp`` claim of a JWT, or None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


def _retry_after_sec(response: httpx.Response) -> float:
    try:
        return max(1.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return _DEFAULT_RATE_LIMIT_COOLDOWN_SEC


class SGPMatchHistoryClient:
    """Paced SGP reads authenticated with the LCU entitlements access token.

    Never logs or raises the token.  Token refresh and pacing are locked, so
    one instance can serve a thread pool (the id-gap sweeper probes 3-8 IDs at
    once and paces them itself with ``min_interval_sec=0``).
    """

    def __init__(
        self,
        lcu: Any,
        *,
        host: str = DEFAULT_SGP_HOST,
        platform_prefix: str = DEFAULT_PLATFORM_PREFIX,
        min_interval_sec: float = 1.0,
        timeout_sec: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lcu = lcu
        self._platform_prefix = platform_prefix
        self._min_interval_sec = max(0.0, float(min_interval_sec))
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=host.rstrip("/"),
            timeout=httpx.Timeout(timeout_sec, connect=5.0),
            headers={"Accept": "application/json"},
            transport=transport,
        )
        self._token: str | None = None
        self._token_refresh_at = 0.0
        self._last_request_at: float | None = None
        self._rate_limited_until = 0.0
        self._token_lock = threading.Lock()
        self._pace_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def _access_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            return self._access_token_locked(force=force)

    def _access_token_locked(self, *, force: bool) -> str:
        now = self._clock()
        if not force and self._token and now < self._token_refresh_at:
            return self._token
        data = self._lcu.get("/entitlements/v1/token")
        token = data.get("accessToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            self._token = None
            raise SGPAuthError("LCU returned no entitlements access token")
        exp = _jwt_exp(token)
        expires_at = exp if exp is not None else now + _TOKEN_FALLBACK_LIFETIME_SEC
        self._token = token
        self._token_refresh_at = max(now + 60.0, expires_at - _TOKEN_REFRESH_MARGIN_SEC)
        return token

    def _pace(self) -> None:
        with self._pace_lock:
            self._pace_locked()

    def _pace_locked(self) -> None:
        now = self._monotonic()
        if now < self._rate_limited_until:
            raise SGPRateLimited(self._rate_limited_until - now)
        if self._last_request_at is not None:
            wait = self._last_request_at + self._min_interval_sec - now
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._monotonic()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET ``path``; None on 404, JSON on 200, SGPError otherwise."""
        for attempt in (0, 1):
            token = self._access_token(force=attempt == 1)
            self._pace()
            try:
                response = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise SGPError(f"SGP request failed: {type(exc).__name__}") from None
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise SGPError("SGP returned invalid JSON") from None
            if status == 404:
                return None
            if status in (401, 403) and attempt == 0:
                continue  # token rotated (client restart); fetch a new one once
            if status in (401, 403):
                raise SGPAuthError(f"SGP rejected a fresh access token: status={status}")
            if status == 429:
                retry_after = _retry_after_sec(response)
                self._rate_limited_until = self._monotonic() + retry_after
                raise SGPRateLimited(retry_after)
            raise SGPError(f"SGP returned status={status}")
        raise SGPError("unreachable")  # pragma: no cover

    def player_games(self, puuid: str, *, count: int = 20, start: int = 0) -> list[dict]:
        """Newest-first v5 ``json`` blocks for ``puuid`` (all queues)."""
        data = self._get_json(
            f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY",
            {"startIndex": max(0, int(start)), "count": max(1, int(count))},
        )
        if data is None:
            return []
        games = data.get("games") if isinstance(data, dict) else None
        if not isinstance(games, list):
            raise SGPError("SGP player SUMMARY response has no games list")
        return [g["json"] for g in games if isinstance(g, dict) and isinstance(g.get("json"), dict)]

    def game(self, game_id: str | int) -> dict | None:
        """One v5 ``json`` block by platform game ID, or None when unknown."""
        data = self._get_json(f"/match-history-query/v1/products/lol/{self._platform_prefix}_{int(game_id)}/SUMMARY")
        if data is None:
            return None
        block = data.get("json") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            # A 200 without a game is not a verdict that the ID is unused.
            raise SGPError("SGP game SUMMARY response has no json block")
        return block


def _perk_fields(perks: object) -> dict[str, int]:
    """v5 ``perks.styles`` -> legacy ``perk0..5`` + ``perkPrimaryStyle``/``perkSubStyle``."""
    styles = perks.get("styles") if isinstance(perks, dict) else None
    styles = [s for s in styles if isinstance(s, dict)] if isinstance(styles, list) else []
    styles.sort(key=lambda s: 0 if s.get("description") == "primaryStyle" else 1)
    ids: list[int] = []
    for style in styles[:2]:
        for selection in style.get("selections") or []:
            ids.append(int((selection or {}).get("perk") or 0))
    ids = (ids + [0] * 6)[:6]
    out = {f"perk{idx}": value for idx, value in enumerate(ids)}
    out["perkPrimaryStyle"] = int(styles[0].get("style") or 0) if styles else 0
    out["perkSubStyle"] = int(styles[1].get("style") or 0) if len(styles) > 1 else 0
    return out


def sgp_game_to_lcu_detail(game: dict) -> dict:
    """Convert one v5 game to the legacy LCU ``games/{id}`` shape.

    The result also carries every field the history helpers read (gameId,
    queueId, gameMode, gameCreation, gameVersion), so it doubles as a history
    row.
    """
    participants: list[dict] = []
    identities: list[dict] = []
    for p in game.get("participants") or []:
        if not isinstance(p, dict):
            continue
        stats = {
            key: value
            for key, value in p.items()
            if not isinstance(value, (dict, list)) and key not in _LCU_ABSENT_STATS
        }
        stats.update(_perk_fields(p.get("perks")))
        participants.append(
            {
                "participantId": p.get("participantId"),
                "teamId": p.get("teamId"),
                "championId": p.get("championId"),
                "spell1Id": p.get("spell1Id"),
                "spell2Id": p.get("spell2Id"),
                "stats": stats,
                "timeline": {"lane": p.get("lane"), "role": p.get("role")},
            }
        )
        identities.append(
            {
                "participantId": p.get("participantId"),
                "player": {
                    "puuid": p.get("puuid"),
                    "gameName": p.get("riotIdGameName"),
                    "tagLine": p.get("riotIdTagline"),
                    "summonerName": p.get("summonerName"),
                    "summonerId": p.get("summonerId"),
                    # LCU's legacy identity always reports accountId 0 now.
                    "accountId": 0,
                    "platformId": game.get("platformId"),
                    "currentPlatformId": game.get("platformId"),
                    "profileIcon": p.get("profileIcon"),
                },
            }
        )
    return {
        "gameId": game.get("gameId"),
        "queueId": game.get("queueId"),
        "gameMode": game.get("gameMode"),
        "gameType": game.get("gameType"),
        "mapId": game.get("mapId"),
        "platformId": game.get("platformId"),
        "seasonId": game.get("seasonId"),
        "gameCreation": game.get("gameCreation"),
        "gameDuration": game.get("gameDuration"),
        "gameVersion": game.get("gameVersion"),
        "teams": [
            {"teamId": t.get("teamId"), "win": "Win" if t.get("win") else "Fail"}
            for t in game.get("teams") or []
            if isinstance(t, dict)
        ],
        "participants": participants,
        "participantIdentities": identities,
    }


@dataclass
class SGPSourceStats:
    visits: int = 0
    fallbacks: int = 0
    rate_limited: int = 0
    single_game_fetches: int = 0
    elapsed_sec: float = 0.0

    def summary(self) -> str:
        avg_ms = 1000.0 * self.elapsed_sec / self.visits if self.visits else 0.0
        return (
            f"visits={self.visits} fallbacks={self.fallbacks} rate_limited={self.rate_limited} "
            f"single_game={self.single_game_fetches} avg_ms={avg_ms:.0f}"
        )


class SGPHistorySource:
    """Per-producer history source with a circuit breaker back to LCU.

    ``history`` returns legacy-shaped rows (each one a full ``games/{id}``
    detail), or None when the caller should read LCU for this visit instead.
    After ``failure_threshold`` consecutive failures -- or any 429 -- SGP is
    skipped for ``cooldown_sec`` (or the server's Retry-After, if longer), so
    a broken SGP costs the old leak rate, never throughput.
    """

    def __init__(
        self,
        client: SGPMatchHistoryClient,
        *,
        failure_threshold: int = 3,
        cooldown_sec: float = 300.0,
        log: Callable[[str], None] = lambda line: print(line, flush=True),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.stats = SGPSourceStats()
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_sec = max(1.0, float(cooldown_sec))
        self._log = log
        self._monotonic = monotonic
        self._consecutive_failures = 0
        self._disabled_until = 0.0

    def _trip(self, seconds: float, reason: str) -> None:
        self._disabled_until = self._monotonic() + seconds
        self._consecutive_failures = 0
        self._log(f"[sgp] circuit open for {seconds:.0f}s; using LCU history  reason={reason}")

    def history(self, puuid: str, count: int) -> list[dict] | None:
        now = self._monotonic()
        if now < self._disabled_until:
            self.stats.fallbacks += 1
            return None
        if self._disabled_until:
            self._disabled_until = 0.0
            self._log("[sgp] circuit closed; retrying SGP history")
        started = self._monotonic()
        try:
            games = self.client.player_games(puuid, count=count)
        except SGPRateLimited as exc:
            self.stats.fallbacks += 1
            self.stats.rate_limited += 1
            self._trip(max(self._cooldown_sec, exc.retry_after_sec), "rate_limited")
            return None
        except SGPError as exc:
            self.stats.fallbacks += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._trip(self._cooldown_sec, str(exc))
            return None
        self._consecutive_failures = 0
        self.stats.visits += 1
        self.stats.elapsed_sec += self._monotonic() - started
        return [sgp_game_to_lcu_detail(game) for game in games]

    def game_detail(self, game_id: str) -> dict | None:
        """Single-game read for a batch row that came back without ten players."""
        self.stats.single_game_fetches += 1
        try:
            game = self.client.game(game_id)
        except SGPError:
            return None
        return sgp_game_to_lcu_detail(game) if game else None

    def close(self) -> None:
        self.client.close()
