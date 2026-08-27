"""LCU poller — EoG-first Mayhem capture.

Primary path: poll /lol-end-of-game/v1/eog-stats-block every 5 s.
The EoG block contains all 10 champion IDs (integers) + isWinningTeam,
so no champion name mapping or in-game port-2999 polling is needed.

Fallback: during InProgress, remember the game_id from gameflow session
in case the user dismisses the EoG screen before we catch it.

LCU WebSocket events are used as wake-up signals only.  The main loop still
does the REST reads and SQLite writes, so a stuck event stream falls back to
normal polling.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue
from collections.abc import Mapping
from typing import Any, Protocol

from .client import (
    LCUClient,
    get_current_summoner,
    get_eog_stats,
    get_game_detail,
    get_gameflow_phase,
    get_gameflow_session,
    get_match_history,
)
from .events import LCUApiEvent, LCUEventListener
from .process import get_credentials
from .db_state import ensure_runtime_state_schema, update_capture_watermark

DEFAULT_QUEUES = {450, 2400}

# Fallback only: consulted when LCU history omits queueId (see
# _queue_id_from_meta).  JADE is the 經典 mode (queue 4310, map 453) and was
# missing, so those games resolved to -1 and went invisible to both the
# classifier and _extract_target_game_ids.
#
# KIWI is ambiguous and cannot be fixed here: queue 2400 (大混戰) and 2450
# (大混戰經典風) both report gameMode=KIWI, so a row missing queueId can only
# be guessed at.  It maps to 2400 as the overwhelmingly more common case.
_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450, "JADE": 4310}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    queue_id     INTEGER NOT NULL,
    patch        TEXT NOT NULL,
    blue_champs  TEXT NOT NULL,
    red_champs   TEXT NOT NULL,
    blue_wins    INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_ms   INTEGER NOT NULL,
    captured_at  TEXT NOT NULL,
    participants_json TEXT,
    participants_private_json TEXT
);
"""

_GAMEFLOW_PHASE_URI = "/lol-gameflow/v1/gameflow-phase"
_GAMEFLOW_SESSION_URI = "/lol-gameflow/v1/session"
_EOG_EVENT_PREFIXES = ("/lol-end-of-game/", "/lol-pre-end-of-game/")

_ITEM_KEYS = tuple(f"item{idx}" for idx in range(7))

_PARTICIPANT_STAT_ALIASES: dict[str, tuple[str, ...]] = {
    "gold_earned": ("goldEarned",),
    "gold_spent": ("goldSpent",),
    "champ_level": ("champLevel",),
    "kills": ("kills",),
    "deaths": ("deaths",),
    "assists": ("assists",),
    "largest_killing_spree": ("largestKillingSpree",),
    "largest_multi_kill": ("largestMultiKill",),
    "first_blood_kill": ("firstBloodKill",),
    "first_blood_assist": ("firstBloodAssist",),
    "total_minions_killed": ("totalMinionsKilled",),
    "neutral_minions_killed": ("neutralMinionsKilled",),
    "total_damage_dealt_to_champions": ("totalDamageDealtToChampions",),
    "physical_damage_dealt_to_champions": ("physicalDamageDealtToChampions",),
    "magic_damage_dealt_to_champions": ("magicDamageDealtToChampions",),
    "true_damage_dealt_to_champions": ("trueDamageDealtToChampions",),
    "total_damage_dealt": ("totalDamageDealt",),
    "physical_damage_dealt": ("physicalDamageDealt",),
    "magic_damage_dealt": ("magicDamageDealt",),
    "true_damage_dealt": ("trueDamageDealt",),
    "largest_critical_strike": ("largestCriticalStrike",),
    "damage_dealt_to_turrets": ("damageDealtToTurrets",),
    "damage_dealt_to_objectives": ("damageDealtToObjectives",),
    "total_damage_taken": ("totalDamageTaken",),
    "physical_damage_taken": ("physicalDamageTaken",),
    "magic_damage_taken": ("magicDamageTaken", "magicalDamageTaken"),
    "true_damage_taken": ("trueDamageTaken",),
    "damage_self_mitigated": ("damageSelfMitigated",),
    "crowd_control_score": ("crowdControlScore",),
    "time_ccing_others": ("timeCCingOthers",),
    "total_time_cc_dealt": ("totalTimeCCDealt", "totalTimeCrowdControlDealt"),
    "total_heal": ("totalHeal",),
    "total_heals_on_teammates": ("totalHealsOnTeammates", "totalHealOnTeammates"),
    "total_units_healed": ("totalUnitsHealed",),
    "total_damage_shielded_on_teammates": (
        "totalDamageShieldedOnTeammates",
        "damageShieldedOnTeammates",
    ),
    "effective_heal_and_shielding": ("effectiveHealAndShielding",),
    "turret_kills": ("turretKills",),
    "inhibitor_kills": ("inhibitorKills", "inhibKills"),
    # --- Added once 經典 (4310) collection started ---
    # Multi-kill / spree counts.  largest_killing_spree above is the LONGEST spree;
    # killing_sprees is HOW MANY sprees, a different signal (one 5-spree vs five
    # 1-sprees) that the snowball axis work could not distinguish before.
    "double_kills": ("doubleKills",),
    "triple_kills": ("tripleKills",),
    "quadra_kills": ("quadraKills",),
    "penta_kills": ("pentaKills",),
    "killing_sprees": ("killingSprees",),
    "longest_time_spent_living": ("longestTimeSpentLiving",),
    # First-objective flags.  Zero on the ARAM map (no lanes to take early), real
    # on map 453.
    "first_tower_kill": ("firstTowerKill",),
    "first_tower_assist": ("firstTowerAssist",),
    "first_inhibitor_kill": ("firstInhibitorKill",),
    "first_inhibitor_assist": ("firstInhibitorAssist",),
    # Vision and jungle.  Dead weight for ARAM/Mayhem (map 12 has neither), but
    # populated in 經典 -- sampled games showed visionScore up to 55 and wardsPlaced
    # up to 44.
    "vision_score": ("visionScore",),
    "wards_placed": ("wardsPlaced",),
    "wards_killed": ("wardsKilled",),
    "vision_wards_bought": ("visionWardsBoughtInGame",),
    "sight_wards_bought": ("sightWardsBoughtInGame",),
    "neutral_minions_enemy_jungle": ("neutralMinionsKilledEnemyJungle",),
    "neutral_minions_team_jungle": ("neutralMinionsKilledTeamJungle",),
}

# Keys omitted from the stored payload when their value is 0, instead of written
# out as an explicit zero like every field above them.
#
# These are all mode-specific: vision, jungle and lane-objective mechanics that do
# not exist on the ARAM map, plus rare multi-kill tiers.  Writing them as zeros
# measured +3,770 bytes per Mayhem game (+35.6%), which at ~1,600 games/hour is
# +0.14 GB/day of pure zeros -- and Mayhem is >99% of collection volume, so
# essentially all of that cost buys nothing.
#
# Consumers must read these with ``.get(key, 0)``.  That is already required for
# every field here: _extract_selected_stats has always omitted keys the LCU did
# not report, so absence never meant anything other than "treat as zero".
_SPARSE_ZERO_STAT_KEYS = frozenset({
    "double_kills", "triple_kills", "quadra_kills", "penta_kills",
    "killing_sprees", "longest_time_spent_living",
    "first_tower_kill", "first_tower_assist",
    "first_inhibitor_kill", "first_inhibitor_assist",
    "vision_score", "wards_placed", "wards_killed",
    "vision_wards_bought", "sight_wards_bought",
    "neutral_minions_enemy_jungle", "neutral_minions_team_jungle",
})


class WriterClientLike(Protocol):
    """Small producer-side surface required by RPC collection mode.

    ``WriterClient`` implements this protocol, while tests and embedding
    callers can inject a deterministic fake without importing the transport.
    The poller deliberately knows nothing about SQLite in this mode.
    """

    def submit(self, message: Mapping[str, Any]) -> dict[str, Any]:
        ...


class _RPCWriterError(RuntimeError):
    """A writer boundary failure; callers must not fall back to direct writes."""


@dataclass(frozen=True)
class _GameClaim:
    game_id: str
    token: str
    generation: int


@dataclass(frozen=True)
class _GameClaimResult:
    """Result of one atomic writer claim attempt.

    ``claim`` is populated only for ``CLAIMED``.  ``DONE`` is distinct from
    ``BUSY`` so the producer can suppress detail fetches for already durable
    games while still retrying a live claim held by another producer later.
    """

    status: str
    claim: _GameClaim | None = None


class _RPCGameWriter:
    """Game claim/commit/release adapter for the single SQLite writer.

    This class is intentionally a pure RPC producer.  It never opens a
    database, keeps a database connection, or implements a local save fallback.
    Every accepted claim carries the writer-issued token and generation through
    the corresponding commit/release request.
    """

    def __init__(self, client: WriterClientLike, *, lease_ms: int = 60_000) -> None:
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or lease_ms <= 0:
            raise ValueError("lease_ms must be a positive integer")
        self._client = client
        self._lease_ms = lease_ms
        self._request_counter = 0

    def _request_id(self, operation: str, game_id: str) -> str:
        self._request_counter += 1
        return f"lcu-poller-{operation}-{self._request_counter}-{game_id}"

    def _submit(self, message: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.submit(dict(message))
        except Exception as exc:  # transport implementations expose varied errors
            raise _RPCWriterError(f"writer {message.get('command')} failed") from exc
        if not isinstance(response, dict):
            raise _RPCWriterError("writer returned an invalid response")
        if response.get("ok") is not True:
            status = str(response.get("status") or "WRITER_REJECTED")
            raise _RPCWriterError(f"writer rejected {message.get('command')}: {status}")
        return response

    def claim(self, game_id: str) -> _GameClaimResult:
        game_id = str(game_id)
        response = self._submit(
            {
                "version": 1,
                "command": "game_claim",
                "request_id": self._request_id("claim", game_id),
                "game_id": game_id,
                "lease_ms": self._lease_ms,
            }
        )
        status = str(response.get("status") or "")
        if status in {"DONE", "BUSY"}:
            return _GameClaimResult(status=status)
        if status != "CLAIMED":
            raise _RPCWriterError(f"writer returned unexpected game_claim status: {status or 'empty'}")
        token = response.get("token")
        generation = response.get("generation")
        if not isinstance(token, str) or not token:
            raise _RPCWriterError("writer CLAIMED response omitted token")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise _RPCWriterError("writer CLAIMED response omitted generation")
        return _GameClaimResult(
            status=status,
            claim=_GameClaim(game_id=game_id, token=token, generation=generation),
        )

    def commit(self, claim: _GameClaim, record: Mapping[str, Any]) -> dict[str, Any]:
        response = self._submit(
            {
                "version": 1,
                "command": "commit_game",
                "request_id": self._request_id("commit", claim.game_id),
                "game_id": claim.game_id,
                "token": claim.token,
                "generation": claim.generation,
                "record": dict(record),
            }
        )
        status = str(response.get("status") or "")
        if status not in {"COMMITTED", "DUPLICATE"}:
            raise _RPCWriterError(f"writer returned unexpected commit_game status: {status or 'empty'}")
        return response

    def release(self, claim: _GameClaim) -> dict[str, Any]:
        response = self._submit(
            {
                "version": 1,
                "command": "release_game",
                "request_id": self._request_id("release", claim.game_id),
                "game_id": claim.game_id,
                "token": claim.token,
                "generation": claim.generation,
            }
        )
        if str(response.get("status") or "") != "RELEASED":
            raise _RPCWriterError(
                f"writer returned unexpected release_game status: {response.get('status') or 'empty'}"
            )
        return response


@dataclass
class _CollectorSignals:
    phase: str | None = None
    game_id: str | None = None
    should_fetch_eog: bool = False
    should_fetch_session: bool = False


# ---------- Parsing ----------

def _extract_augments(stats: dict) -> list[int]:
    augments: list[int] = []
    for idx in range(1, 7):
        value = _to_int(stats.get(f"playerAugment{idx}", 0)) or 0
        if value > 0:
            augments.append(value)
    return augments


def _extract_summoner_spells(participant: dict, stats: dict) -> list[int]:
    """Return the player's summoner spell IDs, sorted ascending.

    ARAM/Mayhem forces Mark/Dash (id 32) plus one chosen spell.  Slot order
    (spell1 vs spell2) is meaningless here — sort by id like champions so the
    model never learns a spurious slot feature.
    """
    spells: list[int] = []
    for aliases in (("spell1Id", "summoner1Id"), ("spell2Id", "summoner2Id")):
        value = _to_int(_lookup_raw_value([participant, stats], aliases))
        if value is not None and value > 0:
            spells.append(value)
    return sorted(spells)


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _lookup_raw_value(sources: list[dict], aliases: tuple[str, ...]) -> object | None:
    normalized_aliases = {_norm_key(alias) for alias in aliases}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for alias in aliases:
            if alias in source and source[alias] is not None:
                return source[alias]
        normalized_source = {_norm_key(str(key)): value for key, value in source.items()}
        for alias in normalized_aliases:
            if alias in normalized_source and normalized_source[alias] is not None:
                return normalized_source[alias]
    return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_item_slots(participant: dict, stats: dict) -> list[int]:
    slots: list[int] = []
    for key in _ITEM_KEYS:
        item_id = _to_int(_lookup_raw_value([stats, participant], (key,)))
        slots.append(item_id if item_id is not None and item_id > 0 else 0)
    return slots


def _extract_selected_stats(participant: dict, stats: dict, challenges: dict) -> dict[str, int]:
    selected: dict[str, int] = {}
    for out_key, aliases in _PARTICIPANT_STAT_ALIASES.items():
        value = _to_int(_lookup_raw_value([stats, challenges, participant], aliases))
        if value is None:
            continue
        # Mode-specific keys are stored only when non-zero; see the comment on
        # _SPARSE_ZERO_STAT_KEYS for why (Mayhem is >99% of volume and would carry
        # ~3.8 KB of meaningless zeros per game otherwise).
        if not value and out_key in _SPARSE_ZERO_STAT_KEYS:
            continue
        selected[out_key] = value
    return selected


def _extract_perks(stats: dict) -> dict:
    """Pull the rune page (perk0-5 + primary/sub style) when the mode has one.

    Returns {} when every field is zero, which is the case for every queue we
    currently collect -- Mayhem (2400), 大混戰經典風 (2450) and 經典 (4310) all
    report zeros, verified against live games -- so stored payloads for those
    modes are byte-identical to before this existed.  Only ARAM (450) and any
    Summoner's Rift game that lands in the net can produce a non-empty result.

    Captured despite currently yielding nothing for 4310 because the LCU keeps
    only ~20 games per player: if Riot later wires runes up for that mode, the
    games played in between cannot be re-fetched.  Storing the field now is the
    only way to not lose them.
    """
    ids = [_to_int(stats.get(f"perk{idx}")) or 0 for idx in range(6)]
    primary = _to_int(stats.get("perkPrimaryStyle")) or 0
    sub = _to_int(stats.get("perkSubStyle")) or 0
    if not any(ids) and not primary and not sub:
        return {}
    return {"perks": {"ids": ids, "styles": [primary, sub]}}


def _extract_lane_role(raw: dict) -> dict:
    """Pull timeline.lane / timeline.role when the mode actually has lanes.

    Returns {} for ARAM / Mayhem, where the fields are missing or "NONE", so the
    stored payload for those modes is byte-identical to before this existed.
    """
    timeline = raw.get("timeline")
    if not isinstance(timeline, dict):
        return {}
    out: dict = {}
    for src, dst in (("lane", "lane"), ("role", "role")):
        value = timeline.get(src)
        if isinstance(value, str):
            value = value.strip().upper()
            if value and value != "NONE":
                out[dst] = value
    return out


def _build_participant_record(team_id: int, champion_id: int, raw: dict) -> dict:
    stats_raw = raw.get("stats") or {}
    stats = stats_raw if isinstance(stats_raw, dict) else {}
    challenges_raw = raw.get("challenges") or {}
    challenges = challenges_raw if isinstance(challenges_raw, dict) else {}
    item_slots = _extract_item_slots(raw, stats)
    record = {
        "teamId": int(team_id),
        "championId": int(champion_id),
        "augments": _extract_augments(stats),
    }

    spells = _extract_summoner_spells(raw, stats)
    if spells:
        record["spells"] = spells

    # Lane / role for the laned modes (queue 4310 "經典" on map 453, and any
    # Summoner's Rift game that lands in the net).  ARAM and Mayhem are played on
    # map 12 where these are absent or NONE, so nothing is stored for them.
    #
    # These come from Riot's OLD lane/role inference and are demonstrably wrong --
    # a sampled 4310 game had three JUNGLEs on one team and no TOP or MIDDLE.  The
    # accurate field (teamPosition) is match-v5 only and the LCU endpoint does not
    # carry it.  They are stored raw anyway, unadjusted, because position has to be
    # re-derived later from spells (11 = Smite) + items + these hints, and the LCU
    # keeps only ~20 games per player: whatever is not captured now is gone for good.
    # Anything reading these must treat them as a weak signal, never ground truth.
    lane_role = _extract_lane_role(raw)
    if lane_role:
        record.update(lane_role)

    record.update(_extract_perks(stats))

    items = [item_id for item_id in item_slots if item_id > 0]
    if items:
        record["items"] = items
        record["itemSlots"] = item_slots

    selected_stats = _extract_selected_stats(raw, stats, challenges)
    if selected_stats:
        record["stats"] = selected_stats

    return record


def _build_private_participant_record(
    team_id: int,
    champion_id: int,
    raw: dict,
    identity: dict | None = None,
) -> dict:
    """Build the local-only participant payload with player identity fields."""
    record = _build_participant_record(team_id, champion_id, raw)
    participant_id = _to_int(raw.get("participantId"))
    if participant_id is not None:
        record["participantId"] = participant_id

    player = identity or {}
    if player:
        private_fields = {
            "puuid": player.get("puuid"),
            "gameName": player.get("gameName"),
            "tagLine": player.get("tagLine"),
            "summonerName": player.get("summonerName"),
            "summonerId": player.get("summonerId"),
            "accountId": player.get("accountId"),
            "platformId": player.get("platformId"),
            "currentPlatformId": player.get("currentPlatformId"),
            "profileIcon": player.get("profileIcon"),
        }
        for key, value in private_fields.items():
            if value not in (None, ""):
                record[key] = value
        if player.get("gameName") and player.get("tagLine"):
            record["riotId"] = f"{player['gameName']}#{player['tagLine']}"

    return record


def _participants_payload_has_postgame_stats(payload: object) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "[]")
        except Exception:
            return False
    if not isinstance(payload, list):
        return False
    for participant in payload:
        if not isinstance(participant, dict):
            continue
        if participant.get("stats"):
            return True
    return False


def _build_participant_payload(participants: list[dict]) -> list[dict]:
    payload: list[dict] = []
    for participant in participants:
        team_id = participant.get("teamId")
        champion_id = participant.get("championId")
        if team_id not in (100, 200) or champion_id is None:
            continue
        payload.append(_build_participant_record(int(team_id), int(champion_id), participant))
    payload.sort(key=lambda row: (row["teamId"], row["championId"]))
    return payload


def _build_private_participant_payload(game: dict) -> list[dict]:
    participants = game.get("participants") or []
    identities = {
        ident.get("participantId"): (ident.get("player") or {})
        for ident in game.get("participantIdentities") or []
        if isinstance(ident, dict)
    }
    payload: list[dict] = []
    for participant in participants:
        team_id = participant.get("teamId")
        champion_id = participant.get("championId")
        if team_id not in (100, 200) or champion_id is None:
            continue
        participant_id = participant.get("participantId")
        payload.append(
            _build_private_participant_record(
                int(team_id),
                int(champion_id),
                participant,
                identities.get(participant_id),
            )
        )
    payload.sort(key=lambda row: (row["teamId"], row.get("participantId", 0), row["championId"]))
    return payload


def _ensure_games_schema(con: sqlite3.Connection) -> None:
    con.execute(_CREATE_SQL)
    ensure_runtime_state_schema(con)
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(games)").fetchall()}
    if "participants_json" not in columns:
        con.execute("ALTER TABLE games ADD COLUMN participants_json TEXT")
    if "participants_private_json" not in columns:
        con.execute("ALTER TABLE games ADD COLUMN participants_private_json TEXT")
    con.commit()


def _extract_session_game_id(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    game_data = data.get("gameData")
    if not isinstance(game_data, dict):
        return None
    game_id = game_data.get("gameId")
    if game_id is None:
        return None
    value = str(game_id)
    return value if value else None


def _drain_lcu_events(event_queue: SimpleQueue[LCUApiEvent]) -> _CollectorSignals:
    signals = _CollectorSignals()
    while True:
        try:
            event = event_queue.get_nowait()
        except Empty:
            break

        if event.uri == _GAMEFLOW_PHASE_URI and isinstance(event.data, str):
            signals.phase = event.data
            if event.data == "InProgress":
                signals.should_fetch_session = True
        elif event.uri == _GAMEFLOW_SESSION_URI:
            signals.should_fetch_session = True
            game_id = _extract_session_game_id(event.data)
            if game_id:
                signals.game_id = game_id
            if isinstance(event.data, dict):
                phase = event.data.get("phase")
                if isinstance(phase, str):
                    signals.phase = phase
        elif event.uri.startswith(_EOG_EVENT_PREFIXES):
            signals.should_fetch_eog = True

    return signals


def _parse_eog_block(eog: dict, target_queues: set[int], patch: str) -> dict | None:
    """Parse the EoG stats block into a saveable record.

    EoG gives us integer championIds directly — no name mapping needed.
    """
    game_id = str(eog.get("gameId", ""))
    if not game_id:
        return None

    mode = eog.get("gameMode", "")
    queue_id = _MODE_TO_QUEUE.get(mode, -1)
    if queue_id not in target_queues:
        return None

    duration = int(eog.get("gameLength", 0))
    if duration < 300:
        return None

    teams = eog.get("teams") or []
    if len(teams) != 2:
        return None

    blue_champs: list[int] = []
    red_champs:  list[int] = []
    blue_wins: int | None = None
    payload: list[dict] = []

    for team in teams:
        tid     = team.get("teamId")
        winning = bool(team.get("isWinningTeam", False))
        players = team.get("players") or []
        if len(players) != 5 or tid not in (100, 200):
            return None
        champs = sorted(int(p["championId"]) for p in players if p.get("championId") is not None)
        if len(champs) != 5:
            return None
        for player in players:
            champion_id = player.get("championId")
            if champion_id is None:
                continue
            payload.append(_build_participant_record(int(tid), int(champion_id), player))
        if tid == 100:
            blue_champs = champs
            blue_wins   = 1 if winning else 0
        else:
            red_champs = champs

    if not blue_champs or not red_champs or blue_wins is None:
        return None

    created_ms = int(eog.get("endOfGameTimestamp", 0)) - duration * 1000

    return {
        "game_id":     game_id,
        "queue_id":    queue_id,
        "patch":       patch,
        "blue_champs": blue_champs,
        "red_champs":  red_champs,
        "blue_wins":   blue_wins,
        "duration_sec": duration,
        "created_ms":  created_ms,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "participants": sorted(payload, key=lambda row: (row["teamId"], row["championId"])),
        "participants_private": sorted(payload, key=lambda row: (row["teamId"], row["championId"])),
    }


def _parse_game_detail(game: dict, target_queues: set[int]) -> dict | None:
    """Parse a /lol-match-history/v1/games/{id} response (all 10 participants)."""
    game_id = str(game.get("gameId", ""))
    if not game_id:
        return None

    queue_id = game.get("queueId", -1)
    if queue_id not in target_queues:
        queue_id = _MODE_TO_QUEUE.get(game.get("gameMode", ""), -1)
    if queue_id not in target_queues:
        return None

    duration = int(game.get("gameDuration", 0))
    if duration < 300:
        return None

    participants = game.get("participants") or []
    if len(participants) != 10:
        return None

    blue_champs = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 100)
    red_champs  = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 200)
    if len(blue_champs) != 5 or len(red_champs) != 5:
        return None

    blue_wins: int | None = None
    for team in (game.get("teams") or []):
        if team.get("teamId") == 100:
            w = team.get("win")
            if isinstance(w, bool):
                blue_wins = 1 if w else 0
            elif isinstance(w, str):
                blue_wins = 1 if w.lower() == "win" else 0
            break
    if blue_wins is None:
        for p in participants:
            if p.get("teamId") == 100:
                w = (p.get("stats") or {}).get("win")
                if w is not None:
                    blue_wins = 1 if w else 0
                    break
    if blue_wins is None:
        return None

    ver = game.get("gameVersion", "")
    vparts = ver.split(".")
    patch = ".".join(vparts[:3]) if len(vparts) >= 3 else (ver or "unknown")

    return {
        "game_id":      game_id,
        "queue_id":     queue_id,
        "patch":        patch,
        "blue_champs":  blue_champs,
        "red_champs":   red_champs,
        "blue_wins":    blue_wins,
        "duration_sec": duration,
        "created_ms":   int(game.get("gameCreation", 0)),
        "captured_at":  datetime.now(timezone.utc).isoformat(),
        "participants": _build_participant_payload(participants),
        "participants_private": _build_private_participant_payload(game),
    }


def _get_patch(lcu: LCUClient, puuid: str, game_id: str) -> str:
    """Look up patch string from match history for the given gameId."""
    for g in get_match_history(lcu, puuid, begin=0, end=5):
        ver = g.get("gameVersion", "")
        if ver:
            parts = ver.split(".")
            patch = ".".join(parts[:3]) if len(parts) >= 3 else ver
            if str(g.get("gameId", "")) == game_id:
                return patch  # exact match
            # keep this as fallback; loop may find exact match later
    # Return whatever we found as fallback
    for g in get_match_history(lcu, puuid, begin=0, end=1):
        ver = g.get("gameVersion", "")
        if ver:
            parts = ver.split(".")
            return ".".join(parts[:3]) if len(parts) >= 3 else ver
    return "unknown"


def _save(con: sqlite3.Connection, record: dict, seen_ids: set[str]) -> bool:
    """INSERT record into DB. Returns True on success. Only updates seen_ids on success."""
    try:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO games (
                game_id, queue_id, patch, blue_champs, red_champs,
                blue_wins, duration_sec, created_ms, captured_at,
                participants_json, participants_private_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record["game_id"], record["queue_id"], record["patch"],
                json.dumps(record["blue_champs"]), json.dumps(record["red_champs"]),
                record["blue_wins"], record["duration_sec"],
                record["created_ms"], record["captured_at"],
                json.dumps(record.get("participants", []), separators=(",", ":")),
                json.dumps(record.get("participants_private", []), ensure_ascii=False, separators=(",", ":")),
            ),
        )
        if cursor.rowcount > 0:
            update_capture_watermark(
                con,
                queue_id=int(record["queue_id"]),
                captured_at=str(record["captured_at"]),
            )
        con.commit()
        seen_ids.add(record["game_id"])
        return True
    except sqlite3.Error as e:
        print(f"[lcu] db error (will retry): {e}")
        return False


# ---------- Main loop ----------

def run_collector(
    db_path: Path | None = None,
    poll_interval: int = 30,
    target_queues: set[int] | None = None,
    *,
    writer_client: WriterClientLike | None = None,
) -> None:
    """Run the LCU collector in direct-maintenance or single-writer RPC mode.

    With ``writer_client`` supplied, ``db_path`` is ignored and the poller is a
    pure producer: it claims each candidate through ``game_claim``, fetches and
    parses detail, then commits via ``commit_game``.  No SQLite connection,
    schema setup, local save, or startup ``game_id`` scan is performed.  The
    legacy direct mode remains available when no client is injected.
    """
    if target_queues is None:
        target_queues = DEFAULT_QUEUES

    rpc_writer = _RPCGameWriter(writer_client) if writer_client is not None else None
    con: sqlite3.Connection | None = None
    # Direct mode preloads the legacy local cache.  RPC mode intentionally
    # starts empty and learns only from writer claim responses during runtime.
    seen_ids: set[str] = set()
    if rpc_writer is None:
        if db_path is None:
            raise ValueError("db_path is required when writer_client is not supplied")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        _ensure_games_schema(con)
        seen_ids = {row[0] for row in con.execute("SELECT game_id FROM games").fetchall()}
        print(f"[lcu] db={db_path}  already_saved={len(seen_ids)}  queues={sorted(target_queues)}")
    else:
        print(f"[lcu] writer=rpc  queues={sorted(target_queues)}")
    print("[lcu] waiting for League client …  (Ctrl-C to stop)")
    print("[lcu] TIP: keep this running — it captures at the post-game screen")

    puuid: str | None = None
    summoner_fail_streak = 0
    # Fallback: if user dismisses EoG before poller catches it, we know the game_id
    # from InProgress and can fetch full detail afterwards via get_game_detail.
    pending_game_id: str | None = None
    last_in_progress_at: float = 0.0   # time.time() of last InProgress poll
    event_queue: SimpleQueue[LCUApiEvent] = SimpleQueue()
    wake_event = threading.Event()
    event_listener: LCUEventListener | None = None
    event_listener_key: tuple[int, str] | None = None

    def _on_lcu_event(event: LCUApiEvent) -> None:
        event_queue.put(event)
        wake_event.set()

    def _stop_event_listener() -> None:
        nonlocal event_listener, event_listener_key
        if event_listener is not None:
            event_listener.stop()
        event_listener = None
        event_listener_key = None

    def _ensure_event_listener(creds) -> None:
        nonlocal event_listener, event_listener_key
        key = (creds.port, creds.token)
        if event_listener_key == key and event_listener is not None and event_listener.is_alive():
            return
        _stop_event_listener()
        event_listener = LCUEventListener(
            creds,
            on_event=_on_lcu_event,
            on_status=lambda status: print(f"[lcu:ws] {status}"),
        )
        event_listener_key = key
        event_listener.start()

    def _sleep(seconds: float) -> None:
        wake_event.wait(max(0.0, seconds))
        wake_event.clear()

    def _claim_game(game_id: str) -> _GameClaimResult:
        """Claim one candidate before any RPC-mode detail fetch."""
        if rpc_writer is None:
            return _GameClaimResult(status="DIRECT")
        if game_id in seen_ids:
            return _GameClaimResult(status="SEEN")
        result = rpc_writer.claim(game_id)
        if result.status == "DONE":
            # This is a runtime cache only; unlike direct mode it is never
            # preloaded from SQLite and therefore cannot become a second source
            # of truth.
            seen_ids.add(game_id)
        return result

    def _release_claim(claim: _GameClaim, *, mark_seen: bool = False) -> None:
        if rpc_writer is None:
            return
        rpc_writer.release(claim)
        if mark_seen:
            seen_ids.add(claim.game_id)

    def _commit_game(claim: _GameClaim | None, record: dict) -> dict[str, Any] | bool:
        if rpc_writer is None:
            if con is None:
                raise RuntimeError("direct collector connection is unavailable")
            return _save(con, record, seen_ids)
        if claim is None:
            raise RuntimeError("RPC commit requires a game claim")
        try:
            response = rpc_writer.commit(claim, record)
        except _RPCWriterError:
            # The writer may have accepted the commit before transport failure;
            # a best-effort release is safe but never replaces fail-closed
            # behavior when the writer is unavailable.
            try:
                rpc_writer.release(claim)
            except _RPCWriterError:
                pass
            raise
        seen_ids.add(claim.game_id)
        return response

    try:
        while True:
            creds = get_credentials()
            if creds is None:
                if puuid is not None:
                    print("[lcu] League client not found — waiting …")
                puuid = None
                summoner_fail_streak = 0
                _stop_event_listener()
                _sleep(poll_interval)
                continue

            _ensure_event_listener(creds)

            try:
                with LCUClient(creds) as lcu:
                    if puuid is None:
                        summoner = get_current_summoner(lcu)
                        if summoner:
                            puuid = summoner.get("puuid")
                            summoner_fail_streak = 0
                            print("[lcu] connected; summoner identity redacted")
                        else:
                            summoner_fail_streak += 1
                            if summoner_fail_streak >= 3:
                                print("[lcu] WARNING: cannot resolve summoner — credentials may be stale")
                            _sleep(poll_interval)
                            continue

                    if not puuid:
                        _sleep(poll_interval)
                        continue

                    # ── Primary: EoG stats block ─────────────────────────────
                    signals = _drain_lcu_events(event_queue)
                    if signals.phase == "InProgress" or signals.should_fetch_eog:
                        last_in_progress_at = time.time()
                    if signals.game_id and signals.game_id not in seen_ids:
                        if pending_game_id != signals.game_id:
                            pending_game_id = signals.game_id
                            print(f"[lcu] event: tracking game {pending_game_id}")

                    eog = get_eog_stats(lcu)
                    if eog:
                        game_id = str(eog.get("gameId", ""))
                        if game_id and game_id not in seen_ids:
                            claim_result = _claim_game(game_id)
                            claim = claim_result.claim
                            if claim_result.status in {"DONE", "SEEN", "BUSY"}:
                                if claim_result.status == "DONE":
                                    pending_game_id = None
                            else:
                                try:
                                    patch = _get_patch(lcu, puuid, game_id)
                                    record = _parse_eog_block(eog, target_queues, patch)
                                    if record:
                                        if _commit_game(claim, record):
                                            total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0] if con is not None else "writer"
                                            q_tag = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                                            print(
                                                f"[lcu] SAVED {q_tag}  game_id={game_id}  "
                                                f"patch={patch}  blue_wins={bool(record['blue_wins'])}  "
                                                f"total={total}"
                                            )
                                            pending_game_id = None
                                    else:
                                        if claim is not None:
                                            _release_claim(claim, mark_seen=True)
                                        else:
                                            seen_ids.add(game_id)
                                except Exception:
                                    if claim is not None:
                                        try:
                                            _release_claim(claim)
                                        except _RPCWriterError:
                                            pass
                                    raise
                        _sleep(5)
                        continue

                    # ── Record game_id during InProgress for fallback ─────────
                    phase = signals.phase or get_gameflow_phase(lcu)
                    if phase == "InProgress" or signals.should_fetch_session:
                        last_in_progress_at = time.time()
                        if pending_game_id is None:
                            session = get_gameflow_session(lcu)
                            gid = str(
                                ((session or {}).get("gameData") or {}).get("gameId", "")
                            )
                            if gid and gid not in seen_ids:
                                pending_game_id = gid
                                print(f"[lcu] fallback: tracking game {gid}")
                        if phase == "InProgress":
                            _sleep(5)
                            continue

                    # ── Fallback: fetch full detail after game ends ───────────
                    if pending_game_id and pending_game_id not in seen_ids:
                        game_id = pending_game_id
                        claim_result = _claim_game(game_id)
                        claim = claim_result.claim
                        if claim_result.status in {"DONE", "SEEN"}:
                            pending_game_id = None
                        elif claim_result.status != "BUSY":
                            try:
                                detail = get_game_detail(lcu, game_id)
                                if detail:
                                    record = _parse_game_detail(detail, target_queues)
                                    if record:
                                        if _commit_game(claim, record):
                                            total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0] if con is not None else "writer"
                                            q_tag = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
                                            print(f"[lcu] SAVED (fallback)  {q_tag}  "
                                                  f"game_id={game_id}  total={total}")
                                    else:
                                        if claim is not None:
                                            _release_claim(claim, mark_seen=True)
                                        else:
                                            seen_ids.add(game_id)
                                    pending_game_id = None
                                elif claim is not None:
                                    _release_claim(claim)
                            except Exception:
                                if claim is not None:
                                    try:
                                        _release_claim(claim)
                                    except _RPCWriterError:
                                        pass
                                raise

            except _RPCWriterError:
                # A producer cannot establish whether an acknowledgement was
                # committed after the writer/pipe fails.  Stop immediately;
                # never retry through SQLite or spin on a dead channel.
                raise
            except Exception as exc:
                print(f"[lcu] error: {exc}")
                puuid = None

            # Poll fast for 2 min after a game ends (so we catch the EoG screen).
            since_game = time.time() - last_in_progress_at
            _sleep(5 if since_game < 120 else poll_interval)

    except KeyboardInterrupt:
        print("\n[lcu] stopped by user")
    finally:
        _stop_event_listener()
        if con is not None:
            con.close()
