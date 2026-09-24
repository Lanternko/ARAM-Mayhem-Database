"""Check the augment pools against what Mayhem games actually hand out.

Pool membership (``augment_pools``) is necessary but not sufficient: in 16.18,
no pick or random grant fell outside a champion's pools, yet two kinds of
reachable augment never showed up.

- **Dead augments.** These were removed or re-coloured in an earlier patch, but
  ``augmentgroups.bin`` still lists them, some in a dozen pools.  No champion
  gets them.
- **Blocked pairs.** The server applies a per-augment champion filter that is
  not in any file (Terrain'd only reaches terrain champions, BONK! about a
  dozen).  The filter is per augment, not per pool: a champion still misses the
  augment when another of its pools holds it.

Zero observations alone prove nothing for a rare pair.  A (champion, augment)
pair is only called blocked when the evidence is strong: at the augment's
10th-percentile rate among champions that do get it, the expected count must
reach ``MIN_EXPECTED``.  At 10, P(0 | not blocked) is about 5e-5.  An augment
nobody gets is tested the same way, using the 10th-percentile rate over all
observed pairs.  Everything below that bar is left in the pools.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

MIN_EXPECTED = 10.0
# Champions with fewer games give noisy rates; they still count as exposure.
MIN_RATE_GAMES = 200
# Per-augment floor needs this many champions with a nonzero rate, else the
# global floor is used (a gated augment reaches only a handful of champions).
MIN_FLOOR_CHAMPS = 10
FLOOR_QUANTILE = 0.10


class PatchCounts:
    """Games per champion and (champion, augment) occurrences in one patch."""

    def __init__(self) -> None:
        self.games: Counter[int] = Counter()
        self.pairs: Counter[tuple[int, int]] = Counter()

    def add_participant(self, champ: int, augments: Iterable[int]) -> None:
        self.games[champ] += 1
        # Any slot counts: random grants obey the same champion filter.
        for aid in set(augments):
            self.pairs[champ, aid] += 1


def count_patch(db: Path | str, queue_id: int, patch: str) -> PatchCounts:
    """Scan one patch (``"16.19"``) of ``games.db`` read-only."""
    out = PatchCounts()
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT participants_json FROM games WHERE queue_id=? AND patch LIKE ? "
            "AND participants_json IS NOT NULL AND participants_json != ''",
            (queue_id, f"{patch}.%"),
        )
        for (raw,) in cur:
            for p in json.loads(raw):
                champ = int(p.get("championId") or 0)
                if champ <= 0:
                    continue
                out.add_participant(champ, (int(a) for a in p.get("augments") or [] if a))
    finally:
        con.close()
    return out


def _quantile(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def classify(
    patches: Iterable[tuple[PatchCounts, Mapping[int, set[int]]]],
    *,
    min_expected: float = MIN_EXPECTED,
) -> dict:
    """Find dead augments and blocked (champion, augment) pairs.

    ``patches`` pairs each patch's counts with that patch's reachable sets
    (champion id -> augment ids from its pools).  A pair only gathers exposure
    in patches where it was reachable, so an augment added to a pool this patch
    is not judged by last patch's games.
    """
    exposure: Counter[tuple[int, int]] = Counter()
    seen: Counter[tuple[int, int]] = Counter()
    for counts, reach in patches:
        for champ, augs in reach.items():
            games = counts.games.get(champ, 0)
            if not games:
                continue
            for aid in augs:
                exposure[champ, aid] += games
                seen[champ, aid] += counts.pairs.get((champ, aid), 0)

    rates: dict[int, list[float]] = {}
    for (champ, aid), games in exposure.items():
        if games >= MIN_RATE_GAMES and seen[champ, aid]:
            rates.setdefault(aid, []).append(seen[champ, aid] / games)
    all_rates = [r for rs in rates.values() for r in rs]
    if not all_rates:
        return {"dead": [], "blocked": {}, "global_floor": None}
    global_floor = _quantile(all_rates, FLOOR_QUANTILE)
    floor = {
        aid: _quantile(rs, FLOOR_QUANTILE) if len(rs) >= MIN_FLOOR_CHAMPS else global_floor
        for aid, rs in rates.items()
    }

    by_aug_exposure: Counter[int] = Counter()
    by_aug_seen: Counter[int] = Counter()
    for (champ, aid), games in exposure.items():
        by_aug_exposure[aid] += games
        by_aug_seen[aid] += seen[champ, aid]
    dead = sorted(
        aid for aid, games in by_aug_exposure.items()
        if not by_aug_seen[aid] and games * global_floor >= min_expected
    )
    dead_set = set(dead)
    blocked: dict[int, list[int]] = {}
    for (champ, aid), games in sorted(exposure.items()):
        if aid in dead_set or seen[champ, aid] or aid not in floor:
            continue
        if games * floor[aid] >= min_expected:
            blocked.setdefault(champ, []).append(aid)
    return {"dead": dead, "blocked": blocked, "global_floor": global_floor}
