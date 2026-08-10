"""Observed Mayhem augment-order features and conservative smoothing helpers.

The local LCU payload stores ``playerAugment1`` ... ``playerAugment6`` as an
ordered list.  This module deliberately models *observed association* only:
it does not claim that a later pick caused a win, because the offered pool and
the player's hidden state are not recorded in ``games`` yet.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


ROLE_ORDER = ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank")


@dataclass(frozen=True)
class AugmentObservation:
    """One selected augment, joined to its match-level context."""

    champion_id: int
    augment_id: int
    slot: int
    won: int
    team_id: int
    context_key: str


def load_role_map(raw: Mapping[str, Any]) -> dict[int, str]:
    """Convert a Data-Dragon champion map into one stable primary role."""
    roles: dict[int, str] = {}
    for raw_id, meta in raw.items():
        try:
            champion_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        tags = meta.get("tags") if isinstance(meta, Mapping) else None
        if isinstance(tags, list) and tags:
            role = str(tags[0])
        else:
            role = "Unknown"
        roles[champion_id] = role if role in ROLE_ORDER else "Unknown"
    return roles


def _shape(champion_ids: Iterable[int], role_map: Mapping[int, str]) -> str:
    counts = {role: 0 for role in ROLE_ORDER}
    for raw_id in champion_ids:
        try:
            champion_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        role = role_map.get(champion_id, "Unknown")
        if role in counts:
            counts[role] += 1
    return ",".join(str(counts[role]) for role in ROLE_ORDER)


def composition_context_key(
    ally_champions: Iterable[int],
    enemy_champions: Iterable[int],
    role_map: Mapping[int, str],
) -> str:
    """Return a compact, position-free team-shape key.

    ARAM has no meaningful lane slots.  Role-count shape is therefore a safer
    first context than inventing top/mid/jungle positions.  Ally and enemy
    shapes are kept separate so a support-heavy opponent is not confused with
    a support-heavy allied team.
    """
    return f"a:{_shape(ally_champions, role_map)}|e:{_shape(enemy_champions, role_map)}"


def iter_observations(
    participants: Iterable[Mapping[str, Any]],
    *,
    blue_wins: int | bool,
    blue_champions: Iterable[int],
    red_champions: Iterable[int],
    role_map: Mapping[int, str],
) -> Iterable[AugmentObservation]:
    """Yield one observation per selected augment, preserving its slot."""
    blue = tuple(int(cid) for cid in blue_champions)
    red = tuple(int(cid) for cid in red_champions)
    for participant in participants:
        try:
            team_id = int(participant.get("teamId", 0) or 0)
            champion_id = int(participant.get("championId", 0) or 0)
        except (TypeError, ValueError):
            continue
        if team_id not in (100, 200) or champion_id <= 0:
            continue
        augments = participant.get("augments") or []
        if not isinstance(augments, (list, tuple)):
            continue
        ally = blue if team_id == 100 else red
        enemy = red if team_id == 100 else blue
        context_key = composition_context_key(ally, enemy, role_map)
        won = int((team_id == 100) == bool(blue_wins))
        for slot, raw_augment_id in enumerate(augments, start=1):
            try:
                augment_id = int(raw_augment_id)
            except (TypeError, ValueError):
                continue
            if augment_id <= 0:
                continue
            yield AugmentObservation(
                champion_id=champion_id,
                augment_id=augment_id,
                slot=slot,
                won=won,
                team_id=team_id,
                context_key=context_key,
            )


def smoothed_rate(
    wins: int,
    games: int,
    *,
    prior_rate: float = 0.5,
    prior_games: float = 100.0,
) -> float:
    """Empirical-Bayes Bernoulli rate used for sparse augment cells."""
    n = max(0, int(games))
    w = min(max(0, int(wins)), n)
    k = max(0.0, float(prior_games))
    p0 = min(1.0 - 1e-9, max(1e-9, float(prior_rate)))
    return (w + k * p0) / (n + k) if n + k > 0 else p0


def logit_probability(probability: float) -> float:
    p = min(1.0 - 1e-9, max(1e-9, float(probability)))
    return math.log(p / (1.0 - p))
