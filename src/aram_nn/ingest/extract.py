"""Extract a single match-v5 detail dict into our flat schema row.

Caller is responsible for queue filtering — this function only validates
structural integrity (10 participants, two teams, win flag present).
"""
from __future__ import annotations

from typing import Any

# Reference constants (caller filters; extract_row does not enforce a specific queue)
ARAM_QUEUE_ID = 450
MAYHEM_QUEUE_ID = 2400


def extract_row(match: dict[str, Any]) -> dict | None:
    info = match.get("info") or {}
    metadata = match.get("metadata") or {}
    participants = info.get("participants") or []
    if len(participants) != 10:
        return None

    blue, red = [], []
    blue_won = None
    for p in participants:
        team_id = p.get("teamId")
        cid = p.get("championId")
        if cid is None:
            return None
        if team_id == 100:
            blue.append(int(cid))
        elif team_id == 200:
            red.append(int(cid))
        else:
            return None
    if len(blue) != 5 or len(red) != 5:
        return None

    for t in info.get("teams") or []:
        if t.get("teamId") == 100:
            blue_won = bool(t.get("win"))
            break
    if blue_won is None:
        return None

    return {
        "match_id": metadata.get("matchId") or info.get("gameId"),
        "patch": str(info.get("gameVersion", "")),
        "queue_id": int(info.get("queueId", 0)),
        "platform": str(info.get("platformId", "")),
        "duration_sec": int(info.get("gameDuration", 0)),
        "blue_champions": sorted(blue),
        "red_champions": sorted(red),
        "blue_wins": blue_won,
        "game_creation_ms": int(info.get("gameCreation", 0)),
        "game_end_ms": int(info.get("gameEndTimestamp", 0) or 0),
        # Crude leaver flag: any participant with timePlayed << gameDuration is suspicious
        "max_leaver_gap_sec": max(
            (info.get("gameDuration", 0) or 0) - (p.get("timePlayed", 0) or 0)
            for p in participants
        ),
    }
