"""Riot API client with built-in rate limiting and 429/key-expiry handling.

Personal Development Key limits:
  - 20 req / 1 sec
  - 100 req / 2 min  (the binding constraint for long scrapes)
Dev keys expire every 24h — set RIOT_API_KEY env var, regenerate daily.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote

import httpx


REGION_TO_PLATFORM = {
    "kr": "kr",
    "tw": "tw2",
    "euw": "euw1",
    "na": "na1",
    "jp": "jp1",
}
# Match-V5 routing: asia/europe/americas/sea (SEA cluster split out ~2022)
REGION_TO_MATCH_ROUTING = {
    "kr": "asia",
    "tw": "sea",
    "jp": "asia",
    "euw": "europe",
    "na": "americas",
}
# Account-V1 routing: asia/europe/americas only (no sea)
REGION_TO_ACCOUNT_ROUTING = {
    "kr": "asia",
    "tw": "asia",
    "jp": "asia",
    "euw": "europe",
    "na": "americas",
}


class RiotKeyExpired(RuntimeError):
    pass


@dataclass
class _Bucket:
    limit: int
    window_sec: float
    timestamps: deque

    def wait_slot(self) -> None:
        now = time.monotonic()
        while self.timestamps and now - self.timestamps[0] > self.window_sec:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.limit:
            sleep_for = self.window_sec - (now - self.timestamps[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            self.wait_slot()
            return
        self.timestamps.append(time.monotonic())


class RiotClient:
    def __init__(self, region: str, api_key: str | None = None, timeout: float = 30.0):
        if region not in REGION_TO_PLATFORM:
            raise ValueError(f"Unknown region {region!r}; pick one of {list(REGION_TO_PLATFORM)}")
        self.region = region
        self.platform_host = f"https://{REGION_TO_PLATFORM[region]}.api.riotgames.com"
        self.routing_host = f"https://{REGION_TO_MATCH_ROUTING[region]}.api.riotgames.com"
        self.account_host = f"https://{REGION_TO_ACCOUNT_ROUTING[region]}.api.riotgames.com"
        key = api_key or os.environ.get("RIOT_API_KEY")
        if not key:
            raise RuntimeError("Set RIOT_API_KEY env var or pass api_key=")
        key = key.strip().strip('"').strip("'")
        if not key.startswith("RGAPI-"):
            raise RuntimeError(
                f"RIOT_API_KEY does not start with 'RGAPI-' (got {key[:8]!r}...). "
                "Check the value — maybe quotes or whitespace got in."
            )
        self._client = httpx.Client(headers={"X-Riot-Token": key}, timeout=timeout)
        # Conservative limits: official is 20/1s + 100/2min.
        # Using 15/1s + 88/2min leaves enough headroom that a single jitter spike
        # never triggers the severe Retry-After:7200 penalty.
        self._buckets = [_Bucket(15, 1.0, deque()), _Bucket(88, 120.0, deque())]

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _wait(self) -> None:
        for b in self._buckets:
            b.wait_slot()

    def _get(self, host: str, path: str, params: dict | None = None) -> dict | list:
        url = f"{host}{path}"
        for attempt in range(6):
            self._wait()
            r = self._client.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                retry = float(r.headers.get("Retry-After", "2"))
                time.sleep(retry + 1.0)
                # Clear bucket state so we don't treat the failed attempt as a
                # used slot — avoids compounding double-counts across retries.
                for b in self._buckets:
                    b.timestamps.clear()
                continue
            if r.status_code in (401, 403):
                raise RiotKeyExpired(
                    f"Riot API returned {r.status_code} — dev key invalid or expired. "
                    "Regenerate at https://developer.riotgames.com and update RIOT_API_KEY "
                    "(remember $env:RIOT_API_KEY=... in PowerShell, NOT `set`)."
                )
            if r.status_code == 404:
                return {} if path.endswith("/ids") is False else []
            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
        raise RuntimeError(f"Exhausted retries for {url}")

    # ---- Account v1 (Riot ID -> PUUID) ----
    def account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        name_q = quote(game_name, safe="")
        tag_q = quote(tag_line, safe="")
        path = f"/riot/account/v1/accounts/by-riot-id/{name_q}/{tag_q}"
        data = self._get(self.account_host, path)
        return data if isinstance(data, dict) else {}

    def account_by_puuid(self, puuid: str) -> dict:
        path = f"/riot/account/v1/accounts/by-puuid/{puuid}"
        data = self._get(self.account_host, path)
        return data if isinstance(data, dict) else {}

    # ---- League-Exp v4 ----
    def league_entries(
        self,
        tier: str,
        division: str = "I",
        queue: str = "RANKED_SOLO_5x5",
        page: int = 1,
    ) -> list[dict]:
        """Apex tiers (CHALLENGER/GRANDMASTER/MASTER) only support division=I.
        Diamond/Plat/Gold/Silver/Bronze/Iron take I~IV."""
        path = f"/lol/league-exp/v4/entries/{queue}/{tier}/{division}"
        data = self._get(self.platform_host, path, params={"page": page})
        return data if isinstance(data, list) else []

    # ---- Match v5 (routing host) ----
    def match_ids_by_puuid(
        self, puuid: str, queue: int | None = None, count: int = 100, start: int = 0
    ) -> list[str]:
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        params: dict = {"count": count, "start": start}
        if queue is not None:
            params["queue"] = queue
        data = self._get(self.routing_host, path, params=params)
        return data if isinstance(data, list) else []

    def match_detail(self, match_id: str) -> dict:
        path = f"/lol/match/v5/matches/{match_id}"
        data = self._get(self.routing_host, path)
        return data if isinstance(data, dict) else {}
