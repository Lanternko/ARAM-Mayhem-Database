"""Recognize an ongoing, explicitly time-bounded Riot maintenance notice.

Queue availability and current-summoner can stay healthy during maintenance.
Only the service-status ticker's present-tense shutdown notice is authoritative
here; unknown wording, planned notices and expired deadlines fail open.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

import httpx

from .process import get_credentials


def active_notice(messages: Any, now: datetime) -> dict[str, str] | None:
    if not isinstance(messages, list):
        return None
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("message") or "")
        if not all(word in text for word in ("正在進行", "維護", "期間遊戲暫不開放遊玩")):
            continue
        match = re.search(
            r"預計\s*(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*GMT([+-]\d{2})(?::?(\d{2}))?\s*結束",
            text,
        )
        if not match:
            continue
        try:
            year, month, day, hour, minute = map(int, match.groups()[:5])
            offset_hour = int(match[6])
            offset_minute = int(match[7] or 0) * (-1 if match[6].startswith("-") else 1)
            zone = timezone(timedelta(hours=offset_hour, minutes=offset_minute))
            end = datetime(year, month, day, hour, minute, tzinfo=zone)
        except ValueError:
            continue
        # A malformed or recycled notice must never disable recovery indefinitely.
        if not now < end <= now + timedelta(hours=8):
            continue
        return {"ends_at": end.isoformat(), "message": text,
                "observed_at": now.isoformat(), "source": "LCU service-status ticker"}
    return None


def probe_maintenance(cache_path: Path, now: datetime | None = None) -> dict[str, str] | None:
    """Keep a confirmed deadline through an LCU outage, never beyond its expiry."""
    now = now or datetime.now(timezone.utc)
    cached = None
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
        end = datetime.fromisoformat(value["ends_at"])
        observed = datetime.fromisoformat(value["observed_at"])
        if observed <= now < end <= observed + timedelta(hours=8):
            cached = value
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        creds = get_credentials()
        if creds is None:
            return cached
        with httpx.Client(base_url=f"https://127.0.0.1:{creds.port}",
                          auth=("riot", creds.token), verify=False,
                          trust_env=False, timeout=2.0) as client:
            response = client.get("/lol-service-status/v1/ticker-messages")
            response.raise_for_status()
            messages = response.json()
        if not isinstance(messages, list):
            return cached
    except (httpx.HTTPError, ValueError):
        return cached
    active = active_notice(messages, now)
    # Cache only public notice text, never credentials or player responses.
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache_path)
    except OSError:
        pass  # The live observation remains valid even if caching is unavailable.
    return active
