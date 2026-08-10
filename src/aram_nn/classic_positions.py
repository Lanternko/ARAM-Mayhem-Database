"""Conservative position inference for Classic (queue 4310) teams.

The LCU match-history payload only exposes Riot's legacy ``lane``/``role``
hints.  Those hints can duplicate or omit positions, so they are treated as
features rather than labels.  We solve a five-player/ five-position assignment
and separately report whether each assignment is strong enough for statistics.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from typing import Iterable


POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT")
SMITE_SPELL_ID = 711
JUNGLE_ITEM_IDS = frozenset({1039})
SUPPORT_ITEM_IDS = frozenset({2049, 2050})


def base_classic_item_id(item_id: int) -> int:
    """Map the 77-prefixed Jade inventory id back to the ordinary item id."""
    raw = str(int(item_id))
    if raw.startswith("77") and len(raw) > 2:
        return int(raw[2:])
    return int(item_id)


@dataclass(frozen=True)
class PositionInference:
    participant_index: int
    position: str
    confidence: str
    score: float
    margin: float
    evidence_families: tuple[str, ...]

    @property
    def stat_eligible(self) -> bool:
        return self.confidence in {"HIGH", "MEDIUM"}


def _signals(participant: dict, team: list[dict]) -> tuple[dict[str, float], dict[str, set[str]]]:
    scores = {position: 0.0 for position in POSITIONS}
    families = {position: set() for position in POSITIONS}
    lane = str(participant.get("lane") or "").upper()
    role = str(participant.get("role") or "").upper()
    spells = {int(value) for value in participant.get("spells") or []}
    items = {
        base_classic_item_id(int(value))
        for value in participant.get("items") or []
        if int(value) > 0
    }
    stats = participant.get("stats") or {}
    neutral = float(stats.get("neutral_minions_killed") or 0)
    lane_cs = float(stats.get("total_minions_killed") or 0)
    wards = float(stats.get("wards_placed") or 0)

    def add(position: str, points: float, family: str) -> None:
        scores[position] += points
        families[position].add(family)

    if SMITE_SPELL_ID in spells:
        add("JUNGLE", 8, "spell")
    if lane == "JUNGLE":
        add("JUNGLE", 5, "lane_role")
    elif lane == "TOP":
        add("TOP", 5 if role in {"SOLO", ""} else 3, "lane_role")
    elif lane in {"MIDDLE", "MID"}:
        add("MIDDLE", 5 if role in {"SOLO", ""} else 3, "lane_role")
    elif lane == "BOTTOM":
        if role in {"SUPPORT", "DUO_SUPPORT"}:
            add("SUPPORT", 6, "lane_role")
        elif role in {"CARRY", "DUO_CARRY"}:
            add("BOTTOM", 6, "lane_role")
        else:
            add("BOTTOM", 3, "lane_role")
    elif role in {"SUPPORT", "DUO_SUPPORT"}:
        add("SUPPORT", 3, "lane_role")
    elif role in {"CARRY", "DUO_CARRY"}:
        add("BOTTOM", 3, "lane_role")

    if items & JUNGLE_ITEM_IDS:
        add("JUNGLE", 3, "item")
    if items & SUPPORT_ITEM_IDS:
        add("SUPPORT", 5, "item")

    team_neutral = [
        float((member.get("stats") or {}).get("neutral_minions_killed") or 0)
        for member in team
    ]
    if neutral >= 20:
        add("JUNGLE", 2, "cs")
    if neutral >= 10 and neutral == max(team_neutral, default=0):
        add("JUNGLE", 2, "cs")

    # Weak tie-breakers: they can stabilize matching, but never establish a
    # position by themselves because lanes can swap and short games are noisy.
    team_lane_cs = [
        float((member.get("stats") or {}).get("total_minions_killed") or 0)
        for member in team
    ]
    if lane_cs == min(team_lane_cs, default=lane_cs) and lane_cs <= 40:
        add("SUPPORT", 1, "weak")
    team_wards = [
        float((member.get("stats") or {}).get("wards_placed") or 0)
        for member in team
    ]
    if wards >= 2 and wards == max(team_wards, default=0):
        add("SUPPORT", 1, "weak")
    return scores, families


def infer_team_positions(participants: Iterable[dict]) -> list[PositionInference]:
    """Return one candidate per standard position plus honest confidence.

    Exactly five participants are required.  The assignment always yields five
    candidates, but only HIGH/MEDIUM rows are eligible for public aggregates.
    """
    team = list(participants)
    if len(team) != 5:
        return []
    signal_rows = [_signals(participant, team) for participant in team]

    ranked: list[tuple[float, tuple[str, ...]]] = []
    for assignment in permutations(POSITIONS):
        total = sum(signal_rows[idx][0][position] for idx, position in enumerate(assignment))
        ranked.append((total, assignment))
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_total, best = ranked[0]

    results: list[PositionInference] = []
    for idx, position in enumerate(best):
        alternative = max(
            (total for total, assignment in ranked if assignment[idx] != position),
            default=best_total,
        )
        margin = best_total - alternative
        score = signal_rows[idx][0][position]
        evidence = tuple(sorted(signal_rows[idx][1][position] - {"weak"}))
        direct_evidence = bool(set(evidence) & {"spell", "lane_role", "item"})
        conflicting_jungle = (
            (SMITE_SPELL_ID in {int(v) for v in team[idx].get("spells") or []})
            != (str(team[idx].get("lane") or "").upper() == "JUNGLE")
        )
        if len(evidence) >= 2 and score >= 5 and margin >= 4 and not conflicting_jungle:
            confidence = "HIGH"
        elif direct_evidence and score >= 3 and margin >= 2:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        results.append(
            PositionInference(idx, position, confidence, score, margin, evidence)
        )

    eligible = [result for result in results if result.stat_eligible]
    if len(eligible) == 4:
        low_index = next(index for index, result in enumerate(results) if not result.stat_eligible)
        low = results[low_index]
        results[low_index] = PositionInference(
            low.participant_index,
            low.position,
            "DERIVED",
            low.score,
            low.margin,
            low.evidence_families,
        )
    return results
