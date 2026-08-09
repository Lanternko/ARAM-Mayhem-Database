"""Build the 經典 (queue 4310, gameMode JADE) champion win-rate page.

A deliberately standalone, *unlisted* preview page — it is not linked from the
main site nav and carries `noindex`, because 4310 has only a few thousand games
and every number on it is still noise-dominated.  Written as its own script (no
import from build_tier_list / tierlist_engine, per CLAUDE.md) so the production
tier-list pipeline cannot be broken by anything here; the only shared code is
`aram_nn.gamedata`.

Why it is not just "the tier list with queue_id=4310":
  * 經典 ships every champion as a separate ``Jade_*`` id (60000 + base), so all
    metadata joins must go through ``base_champion_id()``.
  * The pool is 60 champions, not 173, and the observed win-rate spread is far
    wider (36%-61%) than Mayhem's, because the sample is small — so the page
    leads with the uncertainty instead of burying it.

Usage:
    python scripts/build_classic_page.py
    python scripts/build_classic_page.py --out docs/classic.html
"""

from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import click
import httpx

from aram_nn.gamedata import base_champion_id, iter_games

CLASSIC_QUEUE_ID = 4310

# Same tier cuts and palette as the main site so the two pages stay readable as
# one product.  They are applied to the *shrunk* win rate, never the raw one.
TIER_ORDER = ["OP", "T1", "T2", "T3", "T4", "T5"]
TIER_CUTS = [("OP", 0.55), ("T1", 0.52), ("T2", 0.50), ("T3", 0.48), ("T4", 0.46)]
TIER_COLOR = {
    "OP": "#d8b8ff",
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}
TIER_LABEL_BG = dict(TIER_COLOR)
TIER_LABEL_BG["OP"] = (
    "linear-gradient(135deg,"
    "#ffffff 0%,#e7d5ff 18%,#bcd6ff 36%,"
    "#ffd5ec 58%,#fff1c8 78%,#ffffff 100%)"
)

POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT"]
POSITION_LABELS = {
    "TOP": "上路",
    "JUNGLE": "打野",
    "MIDDLE": "中路",
    "BOTTOM": "下路",
    "SUPPORT": "輔助",
}

# Flat Beta prior toward 50%, in pseudo-games.  The main site uses k=200 against
# ~40k games per champion, where it is a rounding error; here the median champion
# has ~300 games, so the same k is doing real work — which is the point.  A raw
# 61% off 600 games and a raw 61% off 60 games are not the same claim, and the
# tier a champion lands in should reflect that.
PRIOR_WR = 0.5
PRIOR_GAMES = 200

# Below this the sample cannot support a tier at all; these are listed in the
# table but kept out of the tier board rather than shown as a confident cut.
TIER_BOARD_MIN_GAMES = 50

# 經典 ships its own champion art and its own (old) titles under the Jade_* ids,
# so this page must NOT use the normal Data Dragon portraits — Jade_Kayle is
# 「審判天使」 with the pre-rework icon, not the modern 「正義天使」.  Only
# CommunityDragon exposes those entries, keyed by the Jade id (60000 + base).
CDRAGON_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global"
)
CHAMPION_SUMMARY = "/v1/champion-summary.json"
# CommunityDragon is unreliable to hotlink from the live site (see the augment
# icons, which are self-hosted for the same reason), so the portraits are copied
# into docs/assets/ at build time and served from our own origin.
ICON_DIR = Path("docs/assets/icons/classic")
ICON_URL_PREFIX = "assets/icons/classic"
ITEM_ICON_DIR = Path("docs/assets/icons/classic-items")
ITEM_ICON_URL_PREFIX = "assets/icons/classic-items"
ITEM_ICON_VERSION = "16.15.1"

# The client reports JADE inventory ids as ``77`` + the ordinary item id, e.g.
# 773006 = Berserker's Greaves (3006).  Metadata and Data Dragon art are keyed
# by the ordinary id, so convert only this documented Classic namespace.
CLASSIC_ITEM_ID_PREFIX = "77"
ITEM_BOARD_MIN_GAMES = 50
HERO_ITEM_MIN_GAMES = 30
HERO_COMPLETE_ITEM_LIMIT = 6
HERO_BOOTS_ITEM_LIMIT = 3
HERO_STARTER_ITEM_LIMIT = 3
HERO_FIRST_COMPLETE_ITEM_LIMIT = 3
RELATION_MIN_GAMES = 100
RELATION_PRIOR_GAMES = 100
RELATION_LIMIT = 4
# LCU timeline lane/role is an inferred signal.  Keep one strong secondary
# position, but require enough observations to avoid promoting random noise.
SECONDARY_POSITION_MIN_GAMES = 50
SECONDARY_POSITION_MIN_SHARE = 0.15
TRINKET_ITEM_IDS = frozenset({3340, 3341, 3342, 3363})
BOOT_ITEM_IDS = frozenset({3172})  # Gunmetal Greaves lacks the Boots category.

# Only show items that are genuinely bought as a starting choice.  The broad
# metadata-based ``starter`` kind also contains every cheap component, which is
# useful on the item table but misleading in a champion build summary.
CLASSIC_STARTER_ITEM_IDS = frozenset({
    1039,  # Hailblade
    1054,  # Doran's Shield
    1055,  # Doran's Blade
    1056,  # Doran's Ring
    2049,  # Guardian's Amulet
    2050,  # Guardian's Shroud
})


def wilson_interval(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval — the honest error bar on a win rate.

    Preferred over the normal approximation because it stays inside [0,1] and
    keeps its nominal coverage at the sample sizes this page actually has
    (n as low as 50), where Wald intervals are visibly too narrow.
    """
    if games <= 0:
        return (0.0, 1.0)
    p = wins / games
    denom = 1 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    margin = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def assign_tier(wr: float) -> str:
    for tier, cut in TIER_CUTS:
        if wr >= cut:
            return tier
    return "T5"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def logit(probability: float) -> float:
    probability = clamp(probability, 1e-6, 1 - 1e-6)
    return math.log(probability / (1 - probability))


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-value))


def wr_tone_class(wr: float) -> str:
    """Five-step semantic tone shared by initial tiles and client rendering."""
    if wr >= 0.55:
        return "rate-wr-5"
    if wr >= 0.52:
        return "rate-wr-4"
    if wr > 0.48:
        return "rate-wr-3"
    if wr > 0.45:
        return "rate-wr-2"
    return "rate-wr-1"


def load_classic_champion_metadata() -> dict[int, dict]:
    """base champion_id -> the 經典 (Jade_*) name, old title, and portrait id.

    Keyed by the NORMAL champion id so callers can look up straight from
    ``base_champion_id()``, but every value here comes from the Jade entry —
    including ``title_zh``, which is the mode's period-correct title.
    """
    r_en = httpx.get(f"{CDRAGON_BASE}/default{CHAMPION_SUMMARY}", timeout=40)
    r_en.raise_for_status()
    r_zh = httpx.get(f"{CDRAGON_BASE}/zh_tw{CHAMPION_SUMMARY}", timeout=40)

    zh_by_id: dict[int, dict] = {}
    if r_zh.status_code == 200:
        zh_by_id = {int(c["id"]): c for c in r_zh.json() if int(c.get("id", 0)) >= 60000}

    by_id: dict[int, dict] = {}
    for c in r_en.json():
        jade_id = int(c.get("id", 0))
        if jade_id < 60000:
            continue
        base = base_champion_id(jade_id)
        zh = zh_by_id.get(jade_id, {})
        by_id[base] = {
            "jade_id": jade_id,
            "alias": c.get("alias", f"Jade_{base}"),
            "name_zh": zh.get("name") or c.get("name") or str(base),
            "title_zh": zh.get("description") or "",
            "name_en": c.get("name") or str(base),
            "title_en": c.get("description") or "",
            "image": f"{ICON_URL_PREFIX}/{jade_id}.png",
        }
    return by_id


def download_icons(meta: dict[int, dict], icon_dir: Path, refresh: bool) -> int:
    """Copy the Jade portraits into docs/assets/ so the page serves its own icons.

    Already-present files are skipped, so a rebuild costs no network; pass
    ``refresh`` to re-pull them after a patch changes the art.
    """
    icon_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    with httpx.Client(timeout=40) as client:
        for entry in sorted(meta.values(), key=lambda e: e["jade_id"]):
            jade_id = entry["jade_id"]
            dest = icon_dir / f"{jade_id}.png"
            if dest.exists() and not refresh:
                continue
            url = f"{CDRAGON_BASE}/default/v1/champion-icons/{jade_id}.png"
            r = client.get(url)
            if r.status_code != 200 or not r.content:
                click.echo(f"[classic] WARNING: icon {jade_id} -> HTTP {r.status_code}")
                continue
            dest.write_bytes(r.content)
            fetched += 1
    return fetched


def base_item_id(item_id: int) -> int:
    """Map a JADE item id back to its ordinary catalogue id when applicable."""
    text = str(int(item_id))
    if text.startswith(CLASSIC_ITEM_ID_PREFIX) and len(text) >= 6:
        return int(text[len(CLASSIC_ITEM_ID_PREFIX):])
    return int(item_id)


def load_classic_item_metadata() -> dict[int, dict]:
    """Load translated standard-item metadata for JADE's prefixed item ids.

    The mode's client inventory uses a namespace prefix, but its items share
    normal names and art.  Keep the result local to this standalone builder;
    importing the main tier-list engine here would couple the two pipelines.
    """
    rows_en = httpx.get(f"{CDRAGON_BASE}/default/v1/items.json", timeout=40).json()
    response_zh = httpx.get(f"{CDRAGON_BASE}/zh_tw/v1/items.json", timeout=40)
    rows_zh = response_zh.json() if response_zh.status_code == 200 else []
    zh_by_id = {
        int(row["id"]): row
        for row in rows_zh
        if isinstance(row, dict) and row.get("id") is not None
    }
    meta: dict[int, dict] = {}
    for row in rows_en:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        item_id = int(row["id"])
        zh = zh_by_id.get(item_id, {})
        price = row.get("priceTotal")
        price_total = int(price.get("amount") or 0) if isinstance(price, dict) else int(price or 0)
        meta[item_id] = {
            "item_id": item_id,
            "name_zh": zh.get("name") or row.get("name") or f"#{item_id}",
            "name_en": row.get("name") or zh.get("name") or f"#{item_id}",
            "categories": list(row.get("categories") or zh.get("categories") or []),
            "price_total": price_total,
            "upgrades": bool(row.get("to")),
            "image": f"{ITEM_ICON_URL_PREFIX}/{item_id}.png",
        }
    return meta


def download_item_icons(meta: dict[int, dict], item_ids: set[int], icon_dir: Path) -> int:
    """Self-host observed item icons so the preview has no hotlink dependency."""
    icon_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    with httpx.Client(timeout=40) as client:
        for item_id in sorted(item_ids):
            if item_id not in meta:
                continue
            dest = icon_dir / f"{item_id}.png"
            if dest.exists():
                continue
            url = f"https://ddragon.leagueoflegends.com/cdn/{ITEM_ICON_VERSION}/img/item/{item_id}.png"
            response = client.get(url)
            if response.status_code != 200 or not response.content:
                click.echo(f"[classic] WARNING: item icon {item_id} -> HTTP {response.status_code}")
                continue
            dest.write_bytes(response.content)
            fetched += 1
    return fetched


def classify_item(meta: dict) -> str:
    """Return a UI filter category, not a gameplay recommendation."""
    item_id = int(meta.get("item_id") or 0)
    categories = {str(c) for c in meta.get("categories") or []}
    if item_id in TRINKET_ITEM_IDS or "Trinket" in categories:
        return "trinket"
    if item_id in BOOT_ITEM_IDS or "Boots" in categories:
        return "boots"
    if bool(meta.get("upgrades")) or int(meta.get("price_total") or 0) < 1000:
        return "starter"
    return "complete"


def collect_stats(db: Path, patch_prefix: str | None) -> dict:
    """Aggregate heroes plus final-inventory item association for queue 4310."""
    games: dict[int, int] = defaultdict(int)
    wins: dict[int, int] = defaultdict(int)
    per_patch: dict[str, int] = defaultdict(int)
    item_games: dict[int, int] = defaultdict(int)
    item_wins: dict[int, int] = defaultdict(int)
    hero_item_games: dict[tuple[int, int], int] = defaultdict(int)
    hero_item_wins: dict[tuple[int, int], int] = defaultdict(int)
    hero_first_slots_games: dict[tuple[int, int, int], int] = defaultdict(int)
    hero_first_slots_wins: dict[tuple[int, int, int], int] = defaultdict(int)
    hero_position_games: dict[tuple[int, str], int] = defaultdict(int)
    ally_pair_games: dict[tuple[int, int], int] = defaultdict(int)
    ally_pair_wins: dict[tuple[int, int], int] = defaultdict(int)
    matchup_games: dict[tuple[int, int], int] = defaultdict(int)
    matchup_wins: dict[tuple[int, int], int] = defaultdict(int)
    hero_combat_sums: dict[int, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    position_observations = 0
    total = 0
    for g in iter_games(
        db,
        queue_id=CLASSIC_QUEUE_ID,
        patch_prefix=patch_prefix,
        parse_participants=True,
    ):
        total += 1
        per_patch[g["patch"] or "?"] += 1
        blue_won = int(g["blue_wins"])
        duration_minutes = max(float(g.get("duration_sec") or 0) / 60, 1.0)
        team_champions: dict[int, list[int]] = {100: [], 200: []}
        for cid in g["blue_champs"]:
            base = base_champion_id(int(cid))
            games[base] += 1
            wins[base] += blue_won
        for cid in g["red_champs"]:
            base = base_champion_id(int(cid))
            games[base] += 1
            wins[base] += 1 - blue_won
        for participant in g["participants"]:
            champion_id = participant.get("championId")
            if champion_id is None:
                continue
            champion_id = base_champion_id(int(champion_id))
            team_id = int(participant.get("teamId") or 0)
            if team_id in team_champions:
                team_champions[team_id].append(champion_id)
            lane = str(participant.get("lane") or "").upper()
            role = str(participant.get("role") or "").upper()
            position = ""
            if lane in {"TOP", "JUNGLE", "MIDDLE"}:
                position = lane
            elif lane == "BOTTOM":
                position = "SUPPORT" if role == "SUPPORT" else "BOTTOM"
            if position:
                hero_position_games[(champion_id, position)] += 1
                position_observations += 1
            held_items = {
                base_item_id(int(item_id))
                for item_id in participant.get("items") or []
                if int(item_id) > 0
            }
            won = (participant.get("teamId") == 100) == bool(blue_won)
            item_slots = participant.get("itemSlots") or []
            if len(item_slots) >= 2:
                first_slot = base_item_id(int(item_slots[0] or 0))
                second_slot = base_item_id(int(item_slots[1] or 0))
                slot_key = (champion_id, first_slot, second_slot)
                hero_first_slots_games[slot_key] += 1
                hero_first_slots_wins[slot_key] += int(won)
            combat = hero_combat_sums[champion_id]
            combat["games"] += 1
            combat["minutes"] += duration_minutes
            participant_stats = participant.get("stats") or {}
            for stat_name in (
                "kills",
                "deaths",
                "assists",
                "total_damage_dealt_to_champions",
                "gold_earned",
                "total_minions_killed",
                "neutral_minions_killed",
            ):
                combat[stat_name] += float(participant_stats.get(stat_name) or 0)
            for item_id in held_items:
                item_games[item_id] += 1
                item_wins[item_id] += int(won)
                hero_item_games[(champion_id, item_id)] += 1
                hero_item_wins[(champion_id, item_id)] += int(won)
        for team_id, champion_ids in team_champions.items():
            team_won = int((team_id == 100) == bool(blue_won))
            for first, second in combinations(sorted(set(champion_ids)), 2):
                key = (first, second)
                ally_pair_games[key] += 1
                ally_pair_wins[key] += team_won
        blue_ids = sorted(set(team_champions[100]))
        red_ids = sorted(set(team_champions[200]))
        for blue_id in blue_ids:
            for red_id in red_ids:
                if blue_id == red_id:
                    continue
                matchup_games[(blue_id, red_id)] += 1
                matchup_wins[(blue_id, red_id)] += blue_won
                matchup_games[(red_id, blue_id)] += 1
                matchup_wins[(red_id, blue_id)] += 1 - blue_won
    return {
        "hero_games": games,
        "hero_wins": wins,
        "total_games": total,
        "per_patch": dict(per_patch),
        "item_games": item_games,
        "item_wins": item_wins,
        "hero_item_games": hero_item_games,
        "hero_item_wins": hero_item_wins,
        "hero_first_slots_games": hero_first_slots_games,
        "hero_first_slots_wins": hero_first_slots_wins,
        "hero_position_games": hero_position_games,
        "ally_pair_games": ally_pair_games,
        "ally_pair_wins": ally_pair_wins,
        "matchup_games": matchup_games,
        "matchup_wins": matchup_wins,
        "hero_combat_sums": hero_combat_sums,
        "position_observations": position_observations,
    }


def build_rows(
    games: dict,
    wins: dict,
    total_games: int,
    meta: dict,
    position_games: dict[tuple[int, str], int] | None = None,
) -> list[dict]:
    rows = []
    position_games = position_games or {}
    for cid, g in games.items():
        w = wins[cid]
        raw = w / g if g else 0.0
        shrunk = (w + PRIOR_WR * PRIOR_GAMES) / (g + PRIOR_GAMES)
        lo, hi = wilson_interval(w, g)
        m = meta.get(cid, {})
        observed_positions = {
            position: int(position_games.get((cid, position), 0))
            for position in POSITION_ORDER
        }
        position_total = sum(observed_positions.values())
        ranked_positions = sorted(
            observed_positions.items(),
            key=lambda pair: (-pair[1], POSITION_ORDER.index(pair[0])),
        )
        position, position_count = ranked_positions[0]
        positions = [position] if position_count else []
        if position_total and len(ranked_positions) > 1:
            secondary, secondary_count = ranked_positions[1]
            if (
                secondary_count >= SECONDARY_POSITION_MIN_GAMES
                and secondary_count / position_total >= SECONDARY_POSITION_MIN_SHARE
            ):
                positions.append(secondary)
        position_labels = [POSITION_LABELS[item] for item in positions]
        rows.append({
            "champion_id": cid,
            "alias": m.get("alias", f"#{cid}"),
            "name_zh": m.get("name_zh", f"#{cid}"),
            "name_en": m.get("name_en", f"#{cid}"),
            "title_zh": m.get("title_zh", ""),
            "image": m.get("image", ""),
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "shrunk_wr": shrunk,
            "ci_lo": lo,
            "ci_hi": hi,
            # Presence rate per team: 10 slots per game, 2 teams -> a champion
            # appearing in x% of *teams* is games/total_games/2... but ids are
            # unique per team here, so team-presence = picks / (games * 2).
            "pick_rate": (g / (total_games * 2)) if total_games else 0.0,
            "tier": assign_tier(shrunk),
            "position": position if position_count else "",
            "positions": positions,
            "position_label": "／".join(position_labels) if positions else "未分類",
            "position_games": position_count,
            "position_share": position_count / position_total if position_total else 0.0,
        })
    rows.sort(key=lambda r: -r["shrunk_wr"])
    return rows


def attach_combat_profiles(
    heroes: list[dict],
    combat_sums: dict[int, dict[str, float]],
) -> None:
    """Attach stable per-game and per-minute combat context to hero details."""
    for hero in heroes:
        totals = combat_sums.get(hero["champion_id"], {})
        games = max(float(totals.get("games") or 0), 1.0)
        minutes = max(float(totals.get("minutes") or 0), 1.0)
        deaths = float(totals.get("deaths") or 0)
        hero["combat"] = {
            "kills_per_game": float(totals.get("kills") or 0) / games,
            "deaths_per_game": deaths / games,
            "assists_per_game": float(totals.get("assists") or 0) / games,
            "kda": (
                float(totals.get("kills") or 0)
                + float(totals.get("assists") or 0)
            ) / max(deaths, 1.0),
            "damage_per_minute": (
                float(totals.get("total_damage_dealt_to_champions") or 0)
                / minutes
            ),
            "gold_per_minute": float(totals.get("gold_earned") or 0) / minutes,
            "cs_per_minute": (
                float(totals.get("total_minions_killed") or 0)
                + float(totals.get("neutral_minions_killed") or 0)
            ) / minutes,
        }


def attach_relationships(
    heroes: list[dict],
    ally_pair_games: dict[tuple[int, int], int],
    ally_pair_wins: dict[tuple[int, int], int],
    matchup_games: dict[tuple[int, int], int],
    matchup_wins: dict[tuple[int, int], int],
) -> None:
    """Attach conservative 5v5 teammate and opponent associations.

    Both signals are shrunk toward an expectation based on the two champions'
    individual adjusted win rates.  This prevents a 100-game cell from
    outranking a 1,000-game cell on noise alone, while keeping the UI honest:
    these remain team-level associations, never lane-duel or causal claims.
    """
    by_id = {int(hero["champion_id"]): hero for hero in heroes}
    teammates: dict[int, list[dict]] = defaultdict(list)
    opponents: dict[int, list[dict]] = defaultdict(list)

    for (first, second), games in ally_pair_games.items():
        if games < RELATION_MIN_GAMES or first not in by_id or second not in by_id:
            continue
        wins = int(ally_pair_wins[(first, second)])
        expected = clamp(
            sigmoid(logit(by_id[first]["shrunk_wr"]) + logit(by_id[second]["shrunk_wr"])),
            0.35,
            0.65,
        )
        adjusted = (wins + expected * RELATION_PRIOR_GAMES) / (
            games + RELATION_PRIOR_GAMES
        )
        lift = adjusted - expected
        standard_error = math.sqrt(
            max(adjusted * (1 - adjusted), 1e-6)
            / (games + RELATION_PRIOR_GAMES)
        )
        for champion_id, other_id in ((first, second), (second, first)):
            other = by_id[other_id]
            teammates[champion_id].append({
                "champion_id": other_id,
                "name_zh": other["name_zh"],
                "name_en": other["name_en"],
                "image": other["image"],
                "games": games,
                "adjusted_wr": adjusted,
                "lift": lift,
                "rank_score": lift - standard_error,
            })

    for (champion_id, opponent_id), games in matchup_games.items():
        if (
            games < RELATION_MIN_GAMES
            or champion_id not in by_id
            or opponent_id not in by_id
        ):
            continue
        wins = int(matchup_wins[(champion_id, opponent_id)])
        expected = clamp(
            sigmoid(
                logit(by_id[champion_id]["shrunk_wr"])
                - logit(by_id[opponent_id]["shrunk_wr"])
            ),
            0.35,
            0.65,
        )
        adjusted = (wins + expected * RELATION_PRIOR_GAMES) / (
            games + RELATION_PRIOR_GAMES
        )
        edge = adjusted - expected
        standard_error = math.sqrt(
            max(adjusted * (1 - adjusted), 1e-6)
            / (games + RELATION_PRIOR_GAMES)
        )
        opponent = by_id[opponent_id]
        opponents[champion_id].append({
            "champion_id": opponent_id,
            "name_zh": opponent["name_zh"],
            "name_en": opponent["name_en"],
            "image": opponent["image"],
            "games": games,
            "adjusted_wr": adjusted,
            "lift": edge,
            "rank_score": edge + standard_error,
        })

    for hero in heroes:
        champion_id = int(hero["champion_id"])
        ally_rows = teammates.get(champion_id, [])
        enemy_rows = opponents.get(champion_id, [])
        ally_rows.sort(key=lambda row: (-row["rank_score"], -row["games"]))
        enemy_rows.sort(key=lambda row: (row["rank_score"], -row["games"]))
        hero["teammates"] = ally_rows[:RELATION_LIMIT]
        hero["tough_matchups"] = enemy_rows[:RELATION_LIMIT]


def build_item_rows(
    item_games: dict,
    item_wins: dict,
    total_games: int,
    meta: dict[int, dict],
) -> list[dict]:
    """Build global final-inventory association rows with honest uncertainty."""
    rows = []
    for item_id, games in item_games.items():
        item = meta.get(item_id)
        if not item:
            continue
        wins = item_wins[item_id]
        raw_wr = wins / games if games else 0.0
        ci_lo, ci_hi = wilson_interval(wins, games)
        rows.append({
            **item,
            "kind": classify_item(item),
            "games": games,
            "wins": wins,
            "raw_wr": raw_wr,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
            "hold_rate": games / (total_games * 10) if total_games else 0.0,
        })
    rows.sort(key=lambda row: (-row["games"], row["name_zh"]))
    return rows


def attach_hero_items(
    heroes: list[dict],
    hero_item_games: dict,
    hero_item_wins: dict,
    hero_first_slots_games: dict,
    hero_first_slots_wins: dict,
    item_meta: dict[int, dict],
) -> None:
    """Attach held items plus a clearly labelled slot-based first-item proxy."""
    by_champion: dict[int, list[dict]] = defaultdict(list)
    for (champion_id, item_id), games in hero_item_games.items():
        item = item_meta.get(item_id)
        if not item or games < HERO_ITEM_MIN_GAMES or classify_item(item) == "trinket":
            continue
        wins = hero_item_wins[(champion_id, item_id)]
        raw_wr = wins / games
        ci_lo, ci_hi = wilson_interval(wins, games)
        by_champion[champion_id].append({
            "item_id": item_id,
            "name_zh": item["name_zh"],
            "name_en": item["name_en"],
            "image": item["image"],
            "kind": classify_item(item),
            "games": games,
            "raw_wr": raw_wr,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
        })

    # LCU does not preserve a purchase timeline.  The first non-boot completed
    # item found in terminal inventory slot 0, then slot 1, is therefore only a
    # proxy for the first completed item.  Aggregate the ordered slot pairs here
    # after item metadata is available so we can classify candidates correctly.
    first_complete_games: dict[tuple[int, int], int] = defaultdict(int)
    first_complete_wins: dict[tuple[int, int], int] = defaultdict(int)
    for (champion_id, first_slot, second_slot), games in hero_first_slots_games.items():
        inferred_item_id = 0
        for item_id in (first_slot, second_slot):
            item = item_meta.get(item_id)
            if item and classify_item(item) == "complete":
                inferred_item_id = item_id
                break
        if not inferred_item_id:
            continue
        key = (champion_id, inferred_item_id)
        first_complete_games[key] += games
        first_complete_wins[key] += hero_first_slots_wins[
            (champion_id, first_slot, second_slot)
        ]

    first_complete_by_champion: dict[int, list[dict]] = defaultdict(list)
    for (champion_id, item_id), games in first_complete_games.items():
        item = item_meta.get(item_id)
        if not item or games < HERO_ITEM_MIN_GAMES:
            continue
        wins = first_complete_wins[(champion_id, item_id)]
        raw_wr = wins / games
        ci_lo, ci_hi = wilson_interval(wins, games)
        first_complete_by_champion[champion_id].append({
            "item_id": item_id,
            "name_zh": item["name_zh"],
            "name_en": item["name_en"],
            "image": item["image"],
            "kind": "complete",
            "games": games,
            "raw_wr": raw_wr,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
        })
    for hero in heroes:
        hero_items = by_champion.get(hero["champion_id"], [])
        for item in hero_items:
            item["lift"] = item["raw_wr"] - hero["raw_wr"]
        hero_items.sort(key=lambda row: (-row["games"], -row["raw_wr"]))
        complete_items = [item for item in hero_items if item["kind"] == "complete"]
        boots_items = [item for item in hero_items if item["kind"] == "boots"]
        starter_items = [
            item for item in hero_items
            if item["item_id"] in CLASSIC_STARTER_ITEM_IDS
        ]
        first_complete_items = first_complete_by_champion.get(
            hero["champion_id"], []
        )
        for item in first_complete_items:
            item["lift"] = item["raw_wr"] - hero["raw_wr"]
        first_complete_items.sort(key=lambda row: (-row["games"], -row["raw_wr"]))
        hero["items"] = (
            complete_items[:HERO_COMPLETE_ITEM_LIMIT]
            + boots_items[:HERO_BOOTS_ITEM_LIMIT]
        )
        hero["starter_items"] = starter_items[:HERO_STARTER_ITEM_LIMIT]
        hero["first_complete_items"] = first_complete_items[
            :HERO_FIRST_COMPLETE_ITEM_LIMIT
        ]
        if hero["games"] < 100:
            hero["credibility"] = "探索"
        elif hero["ci_lo"] <= 0.5 <= hero["ci_hi"]:
            hero["credibility"] = "未定"
        elif hero["games"] < 250:
            hero["credibility"] = "中"
        else:
            hero["credibility"] = "高"


CSS = """
:root{color-scheme:dark;--bg:#0a0b0d;--surface:#101114;--surface-2:#161a20;
--chip-bg:#1a1d21;--text:#e8eaed;--text-muted:#9aa0a6;--text-dim:#6b7280;
--border:rgba(255,255,255,.09);--border-strong:rgba(255,255,255,.15);
--accent:#f5c518;--r-sm:8px;--r-md:12px;--container:1320px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:"Noto Sans TC",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:var(--container);margin:0 auto;padding:24px 16px 80px}
/* Sticky top bar cloned from the main site header (site.css .site-header). */
.site-header{position:sticky;top:0;z-index:45;
background:color-mix(in srgb,var(--bg) 72%,transparent);
-webkit-backdrop-filter:saturate(180%) blur(14px);
backdrop-filter:saturate(180%) blur(14px);
border-bottom:1px solid var(--border)}
.site-header-inner{display:flex;align-items:center;gap:12px;height:56px;
max-width:var(--container);margin:0 auto;padding:0 16px}
/* Wordmark: identical treatment to the main site (site.css .brand-title) —
   Outfit, weight split across the two halves, no gradient / texture. */
.brand-title{font-family:"Outfit","Noto Sans TC",-apple-system,"Segoe UI",sans-serif;
font-size:26px;font-weight:600;letter-spacing:-.035em;line-height:1;
white-space:nowrap;color:var(--text)}
.brand-aram{font-weight:500;color:var(--text-muted)}
.brand-meta{font-weight:700;color:var(--text)}
.brand-div{color:var(--text-muted);font-size:13px;font-weight:500;white-space:nowrap}
.brand-div::before{content:"";display:inline-block;width:1px;height:14px;
margin-right:11px;vertical-align:-2px;background:var(--border-strong)}
.unlisted{font-size:11px;font-weight:700;letter-spacing:.5px;padding:3px 8px;
border-radius:999px;background:rgba(245,197,24,.14);color:var(--accent);
border:1px solid rgba(245,197,24,.35);white-space:nowrap}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 18px}
#q{flex:1;min-width:220px;max-width:360px;background:var(--surface);color:var(--text);
border:1px solid var(--border);border-radius:var(--r-sm);padding:9px 12px;font-size:14px}
#q::placeholder{color:var(--text-dim)}
#q:focus{outline:none;border-color:rgba(245,197,24,.55)}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:var(--r-sm);
overflow:hidden}
.seg button{background:transparent;border:0;color:var(--text-muted);padding:8px 14px;
font-size:13px;cursor:pointer;font-family:inherit}
.seg button.on{background:var(--accent);color:#14110a;font-weight:700}
/* Sample-size caveat: quiet, inline, right of the view switch.  Only the two
   numbers take the warn colour so the line reads as a caption, not an alarm. */
.caveat{color:var(--text-dim);font-size:12px;line-height:1.5;white-space:nowrap}
.caveat b{color:#c9846f;font-weight:600;font-variant-numeric:tabular-nums}
/* Every tier block carries the SAME padding, even though only OP/T1 paint a
   background wash. .tier-grid derives its column COUNT from container width via
   auto-fill, so padding on only some blocks silently changes how many columns
   fit -- 6px each side was enough to drop OP/T1 from 15 columns to 14 at 1500px,
   making their icons render ~7% larger than every other tier's. */
.tier-block{margin-bottom:22px;position:relative;border-radius:12px;
padding:2px 6px 8px}
.tier-block[data-tier="OP"]{background:radial-gradient(ellipse 70% 60% at 50% 60%,
rgba(216,184,255,.045) 0%,transparent 75%)}
.tier-block[data-tier="T1"]{background:radial-gradient(ellipse 70% 60% at 50% 60%,
rgba(255,90,60,.035) 0%,transparent 75%)}
.tier-heading{display:flex;align-items:center;gap:10px;margin:16px 0 10px;
padding-bottom:8px;font-size:14px;font-weight:600;
border-bottom:1px solid color-mix(in oklab,var(--tier-color,#555) 30%,transparent)}
.tier-pill{position:relative;overflow:hidden;display:inline-flex;align-items:center;
justify-content:center;padding:4px 16px;border-radius:6px;color:#0e1116;
background:var(--tier-bg);font-size:16px;font-weight:700;
text-shadow:0 1px 0 rgba(255,255,255,.25);letter-spacing:.3px}
.tier-pill>span{position:relative;z-index:2}
.tier-count{color:var(--text-muted);font-size:12px;font-weight:400}
.tier-block[data-tier="OP"] .tier-pill{background-size:200% 200%;
animation:prismShift 6s ease-in-out infinite;color:#2a1a4a;
box-shadow:0 0 12px rgba(220,180,255,.55),0 0 28px rgba(170,210,255,.30),
inset 0 0 0 1px rgba(255,255,255,.55);text-shadow:0 1px 0 rgba(255,255,255,.8)}
.tier-block[data-tier="OP"] .tier-pill::before{content:"";position:absolute;inset:0;
background:linear-gradient(115deg,transparent 35%,rgba(255,255,255,.75) 50%,transparent 65%);
background-size:220% 100%;animation:shineSweep 3.2s linear infinite;z-index:1}
@keyframes prismShift{0%{background-position:0% 50%}50%{background-position:100% 50%}
100%{background-position:0% 50%}}
@keyframes shineSweep{from{background-position:220% 0}to{background-position:-120% 0}}
.tier-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));gap:10px}
.champ{position:relative;aspect-ratio:1/1;border-radius:8px;background:var(--chip-bg);
border:2px solid var(--tier-color,#555);cursor:default;
transition:transform .08s,box-shadow .08s}
.champ img{width:100%;height:100%;object-fit:cover;display:block;border-radius:6px}
@media (hover:hover){.champ:hover{transform:translateY(-3px) scale(1.015);
box-shadow:0 8px 24px -8px rgba(245,197,24,.35);z-index:1}
.champ:hover .name{opacity:1}}
.champ .wr{position:absolute;left:2px;bottom:2px;font-size:10px;font-weight:700;
font-variant-numeric:tabular-nums lining-nums;padding:1px 4px;border-radius:6px;
color:#e6e8eb;background:rgba(14,17,22,.9)}
.champ .n{position:absolute;right:2px;top:2px;font-size:9px;padding:1px 4px;
border-radius:6px;color:#aab0b8;background:rgba(14,17,22,.82);
font-variant-numeric:tabular-nums}
.champ .name{position:absolute;left:0;right:0;bottom:0;padding:2px 4px;font-size:10px;
text-align:center;background:linear-gradient(to top,rgba(0,0,0,.88),rgba(0,0,0,0));
color:#e6e8eb;border-radius:0 0 6px 6px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;pointer-events:none;opacity:0;transition:opacity .15s}
.champ.thin{opacity:.55}
.champ.thin::after{content:"?";position:absolute;left:3px;top:2px;font-size:10px;
font-weight:800;color:#0e1116;background:#9aa0a6;border-radius:999px;
width:14px;height:14px;display:flex;align-items:center;justify-content:center}
h2.sec{font-size:16px;margin:36px 0 4px;letter-spacing:.3px}
.sec-note{color:var(--text-muted);font-size:12.5px;margin:0 0 12px;line-height:1.7}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--r-md);
background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:720px}
th,td{padding:9px 12px;text-align:right;white-space:nowrap;
border-bottom:1px solid rgba(255,255,255,.05)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
thead th{position:sticky;top:0;background:var(--surface-2);color:var(--text-muted);
font-weight:600;font-size:12px;cursor:pointer;user-select:none;z-index:2}
thead th:hover{color:var(--text)}
thead th.sorted{color:var(--accent)}
tbody tr:hover{background:rgba(255,255,255,.03)}
td.num{font-variant-numeric:tabular-nums lining-nums}
.tchip{display:inline-block;min-width:26px;text-align:center;padding:1px 6px;
border-radius:4px;font-size:11px;font-weight:700;color:#0e1116}
.cname{display:flex;align-items:center;gap:8px}
.cname img{width:28px;height:28px;border-radius:5px;display:block;flex:none}
.cname small{color:var(--text-dim);font-weight:400}
/* The Jade entries ship the mode's period-correct titles (凱爾 is 審判天使
   here, not 正義天使), which is half the point of a nostalgia mode. */
.cname em{display:block;font-style:normal;font-size:11px;color:var(--text-dim);
margin-top:1px}
/* Error bar: the whole point of the table. Shows the 95% Wilson interval as a
   span against a 30-70% axis, with a hairline at the 50% coin-flip mark. */
.bar{position:relative;width:190px;height:16px;background:rgba(255,255,255,.05);
border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle}
.bar::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;
background:rgba(255,255,255,.28)}
.bar i{position:absolute;top:5px;height:6px;border-radius:3px;background:var(--bc,#9aa0a6);
opacity:.85}
.bar b{position:absolute;top:2px;width:2px;height:12px;background:#fff;border-radius:1px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--border);
color:var(--text-dim);font-size:12px;line-height:1.9}
footer code{color:var(--text-muted)}
@media (max-width:640px){
.wrap{padding:16px 10px 60px}
.site-header-inner{height:52px;padding:0 10px;gap:8px}
.brand-title{font-size:21px}.brand-div{font-size:12.5px}
.unlisted{display:none}
/* Toolbar wraps here, so the caveat drops to its own line and may wrap. */
.caveat{white-space:normal;font-size:11.5px}
.tier-grid{grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}
.bar{width:110px}}

/* Research-page extension.  The old board remains readable while this layer
   adds real controls, inline details, and final-inventory item evidence. */
:root{--focus:oklch(82% .16 86);--surface-3:oklch(18% .012 255);--positive:oklch(73% .15 142);--negative:oklch(68% .18 30);--warning:oklch(73% .12 63);
--wr-5:oklch(84% .17 145);--wr-4:oklch(78% .10 150);--wr-3:oklch(78% .025 255);--wr-2:oklch(77% .10 25);--wr-1:oklch(72% .19 25);
--pick-5:oklch(72% .22 350);--pick-4:oklch(64% .15 350);--pick-3:oklch(72% .07 345);--pick-2:oklch(82% .14 175);--pick-1:oklch(66% .02 250)}
.skip-link{position:fixed;left:12px;top:-64px;z-index:80;padding:10px 14px;border-radius:8px;background:var(--focus);color:#14110a;font-weight:700;text-decoration:none}.skip-link:focus{top:12px}
.site-header-inner{height:64px;gap:16px}.site-header-meta{display:flex;gap:12px;align-items:center;color:var(--text-dim);font-size:12px;white-space:nowrap}.site-header-meta span+span{border-left:1px solid var(--border);padding-left:12px}.site-header-meta b{color:var(--warning);font-weight:600}.unlisted{order:3;margin-left:auto;flex:0 0 auto}.site-header-meta{order:4}
.research-context{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin:30px 0 0;padding-top:16px;border-top:1px solid var(--border)}
.research-context strong{font-size:15px}.research-context p{margin:0;color:var(--text-muted);font-size:13px;line-height:1.6}.research-context .data-note{color:var(--warning);font-variant-numeric:tabular-nums}
.classic-tabs{position:relative;order:2;display:flex;align-items:center;gap:2px;min-width:0;margin-left:8px;overflow-x:auto;overflow-y:hidden;scrollbar-width:none}.classic-tabs::-webkit-scrollbar{display:none}.classic-tab{position:relative;min-height:42px;padding:7px 14px 9px;border:0;border-radius:10px 10px 4px 4px;background:transparent;color:var(--text-muted);font:600 16px inherit;white-space:nowrap;cursor:pointer;transition:color .14s,background-color .14s}.classic-tab:hover{color:var(--text);background:color-mix(in srgb,var(--text) 7%,transparent)}.classic-tab[aria-selected="true"]{color:var(--text);background:color-mix(in oklab,var(--surface-2) 78%,transparent)}.classic-tab[aria-selected="true"]::after{content:"";position:absolute;right:0;bottom:0;left:0;height:3px;border-radius:3px 3px 0 0;background:var(--focus);box-shadow:0 0 8px color-mix(in srgb,var(--focus) 45%,transparent)}.classic-tab:focus-visible{outline:2px solid color-mix(in oklab,var(--focus) 65%,transparent);outline-offset:2px}
.research-bar{display:flex;align-items:center;gap:12px;min-height:40px;margin:0 0 18px;padding:0;background:transparent}.filter-chips{display:flex;align-items:center;gap:6px;min-width:0}.filter-chip{min-height:30px;padding:5px 12px;border:1px solid transparent;border-radius:18px;background:var(--chip-bg);color:var(--text-muted);font:500 12px inherit;white-space:nowrap;cursor:pointer;transition:background .1s,border-color .1s,color .1s}.filter-chip:hover{background:var(--overlay)}.filter-chip.active{border-color:var(--focus);background:var(--focus);color:#0e1116}.filter-count{flex:0 0 auto;padding:0 2px;color:var(--text-muted);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}.filter-search{position:relative;display:block;width:min(280px,32vw);min-width:180px;margin-left:auto}.filter-search svg{position:absolute;top:50%;left:11px;transform:translateY(-50%);color:var(--text-dim);pointer-events:none}.filter-search:focus-within svg{color:var(--text-muted)}.filter-search input{width:100%;height:40px;padding:0 12px 0 32px;border:1px solid var(--border);border-radius:12px;background:color-mix(in oklab,var(--surface-2) 88%,transparent);color:var(--text);font:14px inherit;outline:none;transition:border-color .12s,box-shadow .12s}.filter-search input::placeholder{color:var(--text-dim)}.filter-search input:focus{border-color:rgba(148,163,184,.4);box-shadow:0 0 0 3px rgba(88,96,107,.16)}.filter-source{display:none!important}
.research-tip{margin:0;color:var(--text-dim);font-size:12px;line-height:1.55}.research-tip b{color:var(--text-muted)}.research-tip[hidden],.view[hidden],.empty-state[hidden],.hero-detail[hidden],.filter-chips[hidden],.hero-tile[hidden],.tier-group[hidden]{display:none!important}
.tier-groups{display:grid;gap:20px}.tier-group{padding:0 0 12px;border-bottom:1px solid var(--border)}.tier-group:last-child{border-bottom:0}.tier-group-heading{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid color-mix(in oklab,var(--tier-color,#555) 30%,transparent)}.tier-group-heading h2{margin:0;line-height:1}.tier-group-heading p{margin:0;color:var(--text-muted);font-size:12px}.tier-group-heading p span{color:var(--text);font-weight:700}.tier-group[data-tier-group="OP"] .tier-pill{background-size:200% 200%;animation:prismShift 6s ease-in-out infinite;color:#2a1a4a;box-shadow:0 0 12px rgba(220,180,255,.55),0 0 28px rgba(170,210,255,.30),inset 0 0 0 1px rgba(255,255,255,.55);text-shadow:0 1px 0 rgba(255,255,255,.8)}.tier-group[data-tier-group="OP"] .tier-pill::before{content:"";position:absolute;inset:0;background:linear-gradient(115deg,transparent 35%,rgba(255,255,255,.75) 50%,transparent 65%);background-size:220% 100%;animation:shineSweep 3.2s linear infinite;z-index:1}
.hero-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px 8px}.hero-tile{display:grid;gap:5px;min-width:0;border:0;background:transparent;padding:0;cursor:pointer;color:var(--text);text-align:left}.tile-icon{position:relative;aspect-ratio:1;border:2px solid var(--tier-color,#555);border-radius:8px;background:var(--chip-bg);overflow:hidden;transition:transform .08s,box-shadow .08s,border-color .08s}.hero-tile img{width:100%;height:100%;object-fit:cover;display:block;border-radius:6px}.tile-icon::after{content:"";position:absolute;inset:2px;border-radius:6px;background:rgba(10,11,13,.08);pointer-events:none}.tile-name,.tile-stat{font-variant-numeric:tabular-nums;pointer-events:none}.tile-name{padding:0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;font-weight:600;line-height:1.25}.tile-stat{position:absolute;z-index:1;left:0;bottom:0;padding:2px 5px;border-radius:0 6px 0 0;background:rgba(10,12,16,.92);font-size:10px;font-weight:700;line-height:1.2}.tier-group[data-tier-group="OP"] .tile-icon{border-color:transparent;background:linear-gradient(#1f2530,#1f2530) padding-box,linear-gradient(135deg,#f7f7fb 0%,#e7d5ff 18%,#bcd6ff 36%,#ffd5ec 58%,#fff1c8 78%,#f7f7fb 100%) border-box;box-shadow:0 0 8px rgba(220,180,255,.45)}.tier-group[data-tier-group="T1"] .tile-icon{border-color:transparent;background:linear-gradient(#1f2530,#1f2530) padding-box,linear-gradient(135deg,#ffb380 0%,#ff5a3c 32%,#c8262c 62%,#ff8050 100%) border-box;box-shadow:0 0 6px rgba(255,90,60,.42)}.hero-tile[aria-pressed="true"] .tile-icon{filter:brightness(1.08);box-shadow:0 0 0 1px #f7f7fb,0 6px 16px rgba(0,0,0,.6)}
.rate-wr-5{color:var(--wr-5);font-weight:800;text-shadow:0 0 10px color-mix(in oklab,var(--wr-5) 20%,transparent)}.rate-wr-4{color:var(--wr-4);font-weight:750}.rate-wr-3{color:var(--wr-3);font-weight:650}.rate-wr-2{color:var(--wr-2);font-weight:700}.rate-wr-1{color:var(--wr-1);font-weight:800;text-shadow:0 0 10px color-mix(in oklab,var(--wr-1) 22%,transparent)}.rate-pick-5{color:var(--pick-5);font-weight:800;text-shadow:0 0 10px color-mix(in oklab,var(--pick-5) 20%,transparent)}.rate-pick-4{color:var(--pick-4);font-weight:750}.rate-pick-3{color:var(--pick-3);font-weight:700}.rate-pick-2{color:var(--pick-2);font-weight:650}.rate-pick-1{color:var(--pick-1);font-weight:600}
.hero-detail{display:grid;gap:16px;margin:12px 0 4px;padding:16px;border:1px solid var(--focus);border-radius:12px;background:var(--surface-2)}.detail-head{display:flex;gap:12px;align-items:flex-start}.detail-head>img{width:56px;height:56px;border-radius:8px;border:1px solid var(--border)}.detail-identity{min-width:0;flex:1}.detail-head h2{margin:0;font-size:20px}.detail-head p{margin:3px 0 0;color:var(--text-muted);font-size:13px}.combat-profile{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px}.combat-profile span{display:grid;gap:1px}.combat-profile b{color:var(--text);font-size:12px;font-variant-numeric:tabular-nums}.combat-profile small{color:var(--text-dim);font-size:10px}.detail-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.detail-stat{min-width:0;padding-top:10px;border-top:1px solid var(--border)}.detail-stat dt{color:var(--text-dim);font-size:11px;font-weight:600;letter-spacing:.04em}.detail-stat dd{margin:4px 0 0;color:var(--text);font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}.detail-loadout{min-width:0;padding-top:10px;border-top:1px solid var(--border)}.detail-loadout h3{margin:0 0 9px;color:var(--text-dim);font-size:11px;font-weight:600;letter-spacing:.04em}.detail-loadout-items{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.detail-loadout-item{min-width:0;color:var(--text-muted);text-align:center}.detail-loadout-item img{display:block;width:44px;height:44px;margin:0 auto 5px;border:1px solid var(--border);border-radius:7px}.detail-loadout-item span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px}.ci-line{display:grid;gap:5px}.ci-line span{display:flex;justify-content:space-between;color:var(--text-muted);font-size:12px;font-variant-numeric:tabular-nums}.ci-track{position:relative;height:12px;background:var(--surface-3);border-radius:99px}.ci-track::before{content:"";position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--border-strong)}.ci-track i{position:absolute;top:4px;height:4px;border-radius:99px;background:var(--focus)}.ci-track b{position:absolute;top:1px;width:2px;height:10px;background:var(--text)}
.detail-items{border-top:1px solid var(--border);padding-top:14px}.detail-items-head{display:flex;gap:8px;justify-content:space-between;align-items:baseline;margin-bottom:12px}.detail-items h3{margin:0;font-size:14px}.detail-items p{margin:0;color:var(--text-dim);font-size:11px}.detail-equipment-columns{display:grid;grid-template-columns:minmax(0,.82fr) minmax(0,1.18fr);gap:26px}.detail-equipment-column+ .detail-equipment-column{padding-left:26px;border-left:1px solid var(--border)}.detail-item-list{display:grid;gap:8px}.detail-item{display:grid;grid-template-columns:34px minmax(0,1fr) auto;gap:9px;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.06)}.detail-item:last-child{border-bottom:0}.detail-item img{width:32px;height:32px;border-radius:5px}.detail-item strong{display:block;font-size:13px}.detail-item small{display:block;margin-top:2px;color:var(--text-dim);font-size:11px}.detail-item .item-stat{text-align:right;color:var(--text-muted);font-size:12px;font-variant-numeric:tabular-nums}.item-stat .item-rate{font-size:13px;font-weight:700;white-space:nowrap}.item-stat .lift-up{color:var(--positive)}.item-stat .lift-down{color:var(--negative)}
.detail-item-group{margin-top:16px}.detail-item-group:first-child{margin-top:0}.detail-item-group h4{display:flex;align-items:baseline;gap:7px;margin:0 0 3px;color:var(--text-muted);font-size:12px;font-weight:600}.detail-item-group h4 span{color:var(--text-dim);font-size:11px;font-variant-numeric:tabular-nums}.detail-item-group>p{margin:0 0 4px;color:var(--text-dim);font-size:10px;line-height:1.45}
.detail-relationships{grid-column:1/-1;border-top:1px solid var(--border);padding-top:14px}.relationship-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}.relationship-group h3{margin:0;font-size:14px}.relationship-group>p{margin:3px 0 7px;color:var(--text-dim);font-size:11px;line-height:1.45}.relationship-list{display:grid}.relationship-row{display:grid;grid-template-columns:30px minmax(0,1fr) auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.06)}.relationship-row:last-child{border-bottom:0}.relationship-row img{width:28px;height:28px;border-radius:5px}.relationship-row strong{display:block;font-size:12px}.relationship-row small{display:block;margin-top:1px;color:var(--text-dim);font-size:10px}.relationship-stat{text-align:right;font-size:11px;font-variant-numeric:tabular-nums}.relationship-stat b{display:block;color:var(--text);font-size:12px}.relationship-stat .lift-up{color:var(--positive)}.relationship-stat .lift-down{color:var(--negative)}
.data-section-head{display:flex;gap:12px;align-items:baseline;justify-content:space-between;margin:4px 0 12px}.data-section-head h2{margin:0;font-size:18px}.data-section-head p{margin:0;color:var(--text-muted);font-size:12px;line-height:1.5;text-align:right}.table-scroller{overflow:auto;border:1px solid var(--border);border-radius:12px;background:var(--surface)}.research-table{width:100%;min-width:760px;border-collapse:collapse;font-size:13px}.research-table th,.research-table td{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.06);text-align:right;white-space:nowrap}.research-table th:first-child,.research-table td:first-child,.research-table th:nth-child(2),.research-table td:nth-child(2){text-align:left}.research-table th{position:sticky;top:0;z-index:1;background:var(--surface-2);color:var(--text-muted);font-size:11px;letter-spacing:.03em}.research-table th button{display:inline-flex;gap:4px;align-items:center;border:0;background:transparent;color:inherit;font:inherit;cursor:pointer}.research-table th button[aria-sort="descending"],.research-table th button[aria-sort="ascending"]{color:var(--focus)}.research-table tbody tr:hover{background:rgba(255,255,255,.025)}.research-table tbody tr:last-child td{border-bottom:0}.table-hero{display:flex;gap:11px;align-items:center;min-width:180px;border:0;background:transparent;color:var(--text);font:inherit;cursor:pointer;padding:0;text-align:left}.table-hero img{width:42px;height:42px;border:1px solid var(--border-strong);border-radius:7px;object-fit:cover}.table-hero strong{display:block;font-size:15px;font-weight:700;line-height:1.2}.table-item img{width:30px;height:30px;border-radius:5px;object-fit:cover}.table-item small{display:block;margin-top:2px;color:var(--text-dim);font-size:11px}.table-item{display:flex;gap:8px;align-items:center;min-width:180px}.rate-positive{color:var(--positive);font-weight:700}.rate-negative{color:var(--negative);font-weight:700}.rate-neutral{color:var(--warning);font-weight:700}.mono{font-variant-numeric:tabular-nums}.item-kind{display:inline-block;padding:2px 6px;border:1px solid var(--border);border-radius:99px;color:var(--text-muted);font-size:11px}.item-note{display:flex;gap:8px;align-items:flex-start;margin:0 0 12px;color:var(--text-muted);font-size:12px;line-height:1.6}.item-note b{color:var(--warning);font-weight:600}.empty-state{padding:28px 14px;border:1px dashed var(--border-strong);border-radius:12px;color:var(--text-muted);text-align:center;font-size:14px}.method{margin-top:32px;padding-top:14px;border-top:1px solid var(--border);color:var(--text-muted);font-size:13px;line-height:1.7}.method summary{color:var(--text);font-weight:600;cursor:pointer}.method p{max-width:75ch}.method code{color:var(--text-muted)}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--focus);outline-offset:2px}@media (pointer:fine){.hero-tile:hover{transform:translateY(-3px) scale(1.015)}.hero-tile:hover .tile-icon{border-color:color-mix(in srgb,var(--focus) 55%,var(--tier-color,#555));box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 8px 24px -8px rgba(245,197,24,.35)}.classic-tab:hover{color:var(--text)}}@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
@media (min-width:700px){.wrap{padding-top:28px}.hero-grid{grid-template-columns:repeat(auto-fill,minmax(76px,1fr));gap:10px}.hero-detail{grid-template-columns:minmax(220px,.72fr) minmax(320px,1fr) minmax(190px,.52fr)}.detail-head{grid-column:1;grid-row:1/span 2;align-items:flex-start}.detail-stats{grid-column:2;grid-row:1}.detail-loadout{grid-column:3;grid-row:1}.detail-items{grid-column:2/4;grid-row:2}.site-header-meta{display:flex}}
@media (max-width:960px){.site-header-meta{display:none}}
@media (max-width:699px){.site-header-inner{height:auto;min-height:52px;flex-wrap:wrap;padding:8px 10px 0;gap:0 10px}.brand-title{font-size:22px}.unlisted{margin-left:auto}.site-header-meta{display:none}.classic-tabs{order:10;flex-basis:calc(100% + 20px);margin:4px -10px 0;padding:0 10px}.classic-tab{min-height:42px;padding:8px 12px 10px;font-size:15px}.research-context{margin-top:24px}.research-context strong{width:100%}.research-bar{align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:14px}.filter-chips{width:100%;max-width:100%;flex-wrap:wrap;gap:4px}.filter-chip{min-height:34px;padding:4px 10px;font-size:11px}.filter-count{display:none}.filter-search{width:100%;min-width:0;margin-left:0}.data-section-head{align-items:flex-start;flex-direction:column}.data-section-head p{text-align:left}.table-scroller{margin-right:-2px;margin-left:-2px}.hero-detail{padding:12px}.detail-head h2{font-size:18px}.combat-profile{gap:10px}.detail-loadout-item img{width:40px;height:40px}.relationship-columns{grid-template-columns:1fr;gap:18px}.detail-items-head{align-items:flex-start;flex-direction:column}.detail-equipment-columns{grid-template-columns:1fr;gap:18px}.detail-equipment-column+ .detail-equipment-column{padding:18px 0 0;border-top:1px solid var(--border);border-left:0}.item-note{align-items:flex-start}}
"""

JS = """
(function(){
  var dataEl=document.getElementById('classic-data');
  if(!dataEl) return;
  var data=JSON.parse(dataEl.textContent);
  var state={tab:'tier',heroSort:'shrunk_wr',itemSort:'games',desc:true,heroId:null};
  var search=document.getElementById('research-search');
  var positionFilter=document.getElementById('position-filter');
  var itemKindFilter=document.getElementById('item-kind-filter');
  var minGames=document.getElementById('min-games');
  var heroSort=document.getElementById('hero-sort');
  var itemSort=document.getElementById('item-sort');
  var detail=document.getElementById('hero-detail');
  var positionChips=document.getElementById('position-chips');
  var itemKindChips=document.getElementById('item-kind-chips');
  var shownN=document.getElementById('shown-n');
  var shownTotal=document.getElementById('shown-total');
  var shownUnit=document.getElementById('shown-unit');
  var tabs=[].slice.call(document.querySelectorAll('[role="tab"]'));
  var views=[].slice.call(document.querySelectorAll('[data-view]'));
  function esc(value){return String(value==null?'':value).replace(/[&<>'"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c];});}
  function pct(value){return (Number(value)*100).toFixed(1)+'%';}
  function num(value){return Number(value).toLocaleString('zh-TW');}
  function wrToneClass(value){var v=Number(value)||0;return v>=.55?'rate-wr-5':v>=.52?'rate-wr-4':v>.48?'rate-wr-3':v>.45?'rate-wr-2':'rate-wr-1';}
  function pickToneClass(value){var v=Number(value)||0;return v>=.18?'rate-pick-5':v>=.12?'rate-pick-4':v>=.07?'rate-pick-3':v>=.04?'rate-pick-2':'rate-pick-1';}
  function clearToneClass(element){element.className=element.className.replace(/\\brate-(?:wr|pick)-[1-5]\\b/g,'').trim();}
  function ciMarkup(row){var lo=Math.max(0,Math.min(100,(row.ci_lo-.3)/.4*100)),hi=Math.max(0,Math.min(100,(row.ci_hi-.3)/.4*100)),raw=Math.max(0,Math.min(100,(row.raw_wr-.3)/.4*100));return '<div class="ci-line"><span><b>'+pct(row.ci_lo)+'</b><b>'+pct(row.ci_hi)+'</b></span><div class="ci-track"><i style="left:'+lo.toFixed(2)+'%;width:'+Math.max(hi-lo,1).toFixed(2)+'%"></i><b style="left:'+raw.toFixed(2)+'%"></b></div></div>';}
  function activeHeroes(){var query=(search.value||'').trim().toLowerCase(),position=positionFilter.value,minimum=Number(minGames.value||0);return data.heroes.filter(function(hero){var positions=hero.positions||[hero.position];return (!query||hero.search.indexOf(query)>=0)&&(!position||positions.indexOf(position)>=0)&&hero.games>=minimum;});}
  function activeItems(){var query=(search.value||'').trim().toLowerCase(),kind=itemKindFilter.value,minimum=Number(minGames.value||0);return data.items.filter(function(item){return (!query||item.search.indexOf(query)>=0)&&(!kind||item.kind===kind)&&item.games>=minimum;});}
  function compare(field){return function(a,b){var av=a[field],bv=b[field],result=typeof av==='string'?String(av).localeCompare(String(bv),'zh-Hant'):Number(av)-Number(bv);return state.desc?-result:result;};}
  function updateTiles(){var allowed={};activeHeroes().forEach(function(hero){allowed[hero.champion_id]=hero;});var field=heroSort.value;[ ].slice.call(document.querySelectorAll('.hero-tile')).forEach(function(tile){var hero=allowed[tile.dataset.heroId];tile.hidden=!hero;if(hero){var stat=tile.querySelector('.tile-stat');clearToneClass(stat);if(field==='raw_wr'){stat.textContent=pct(hero.raw_wr);stat.classList.add(wrToneClass(hero.raw_wr));}else if(field==='pick_rate'){stat.textContent=pct(hero.pick_rate);stat.classList.add(pickToneClass(hero.pick_rate));}else if(field==='games')stat.textContent=num(hero.games);else{stat.textContent=pct(hero.shrunk_wr);stat.classList.add(wrToneClass(hero.shrunk_wr));}}});[ ].slice.call(document.querySelectorAll('[data-tier-group]')).forEach(function(group){var visible=group.querySelectorAll('.hero-tile:not([hidden])').length;group.hidden=!visible;var count=group.querySelector('[data-group-count]');if(count)count.textContent=num(visible);});document.getElementById('tier-empty').hidden=Object.keys(allowed).length>0;if(state.heroId&&!allowed[state.heroId]) closeDetail();}
  function renderHeroes(){var heroes=activeHeroes().sort(compare(heroSort.value));document.getElementById('hero-tbody').innerHTML=heroes.map(function(hero){return '<tr><td><span class="item-kind">'+esc(hero.tier)+'</span></td><td><button type="button" class="table-hero" data-open-hero="'+hero.champion_id+'" aria-label="'+esc(hero.name_zh+' '+hero.name_en)+'"><img src="'+esc(hero.image)+'" alt=""><strong>'+esc(hero.name_zh)+'</strong></button></td><td class="mono">'+num(hero.games)+'</td><td class="mono '+wrToneClass(hero.shrunk_wr)+'">'+pct(hero.shrunk_wr)+'</td><td class="mono '+pickToneClass(hero.pick_rate)+'">'+pct(hero.pick_rate)+'</td></tr>';}).join('');document.getElementById('heroes-empty').hidden=heroes.length>0;}
  function renderItems(){var items=activeItems().sort(compare(itemSort.value));document.getElementById('item-tbody').innerHTML=items.map(function(item){return '<tr><td><span class="item-kind">'+esc(item.kind_label)+'</span></td><td><span class="table-item"><img src="'+esc(item.image)+'" alt=""><span>'+esc(item.name_zh)+' <small>'+esc(item.name_en)+'</small></span></span></td><td class="mono '+pickToneClass(item.hold_rate)+'">'+pct(item.hold_rate)+'</td><td class="mono">'+num(item.games)+'</td><td class="mono '+wrToneClass(item.raw_wr)+'">'+pct(item.raw_wr)+'</td><td>'+ciMarkup(item)+'</td></tr>';}).join('');document.getElementById('items-empty').hidden=items.length>0;}
  function itemRows(items,sampleLabel){sampleLabel=sampleLabel||'持有';return '<div class="detail-item-list">'+items.map(function(item){var lift=item.lift||0,liftClass=lift>0?'lift-up':lift<0?'lift-down':'';return '<div class="detail-item"><img src="'+esc(item.image)+'" alt=""><div><strong>'+esc(item.name_zh)+'</strong><small>'+esc(sampleLabel)+' '+num(item.games)+' 場</small></div><div class="item-stat"><span class="item-rate '+liftClass+'">'+pct(item.raw_wr)+'（'+(lift>=0?'+':'')+(lift*100).toFixed(1)+'%）</span></div></div>';}).join('')+'</div>';}
  function itemGroup(title,items,sampleLabel,note){if(!items.length)return '';return '<section class="detail-item-group"><h4>'+esc(title)+' <span>'+num(items.length)+'</span></h4>'+(note?'<p>'+esc(note)+'</p>':'')+itemRows(items,sampleLabel)+'</section>';}
  function renderItemsForHero(hero){var complete=(hero.items||[]).filter(function(item){return item.kind==='complete';}),boots=(hero.items||[]).filter(function(item){return item.kind==='boots';}),starters=hero.starter_items||[],firstComplete=hero.first_complete_items||[];var left=itemGroup('鞋子',boots,'持有')+itemGroup('常見出門裝',starters,'終局仍持有','只統計多蘭系列、打野與守護者起手裝；賣掉後無法觀察。')+itemGroup('推定首件大裝',firstComplete,'前兩格出現','依終局背包第 1–2 格推估，並非實際購買時間線。');var right=itemGroup('常見終局完整裝備',complete,'持有');return (left||right)?'<div class="detail-equipment-columns"><div class="detail-equipment-column">'+(left||'<p class="research-tip">左欄目前沒有達到樣本門檻的資料。</p>')+'</div><div class="detail-equipment-column">'+(right||'<p class="research-tip">沒有足夠樣本的終局完整裝備。</p>')+'</div></div>':'<p class="research-tip">沒有足夠樣本的裝備資料可呈現。</p>';}
  function renderCompactLoadout(hero){var items=(hero.items||[]).filter(function(item){return item.kind==='complete';}).slice(0,3);if(!items.length)return '';return '<section class="detail-loadout"><h3>最常見完整裝備</h3><div class="detail-loadout-items">'+items.map(function(item){return '<div class="detail-loadout-item" title="'+esc(item.name_zh)+' · 持有 '+num(item.games)+' 場"><img src="'+esc(item.image)+'" alt=""><span>'+esc(item.name_zh)+'</span></div>';}).join('')+'</div></section>';}
  function relationshipRows(rows){if(!rows.length)return '<p class="research-tip">目前沒有達到 100 場門檻的組合。</p>';return '<div class="relationship-list">'+rows.map(function(row){var lift=Number(row.lift)||0,liftClass=lift>0?'lift-up':lift<0?'lift-down':'';return '<div class="relationship-row"><img src="'+esc(row.image)+'" alt=""><div><strong>'+esc(row.name_zh)+'</strong><small>'+num(row.games)+' 場共同樣本</small></div><div class="relationship-stat"><b class="'+wrToneClass(row.adjusted_wr)+'">'+pct(row.adjusted_wr)+'</b><span class="'+liftClass+'">相對預期 '+(lift>=0?'+':'')+(lift*100).toFixed(1)+'pp</span></div></div>';}).join('')+'</div>';}
  function renderRelationships(hero){return '<section class="detail-relationships"><div class="relationship-columns"><section class="relationship-group"><h3>最佳搭檔</h3><p>同隊勝率經收縮；差值已扣除兩位英雄本身強度。</p>'+relationshipRows(hero.teammates||[])+'</section><section class="relationship-group"><h3>棘手對手</h3><p>面對該英雄的勝率經收縮；不是單線對決或因果結論。</p>'+relationshipRows(hero.tough_matchups||[])+'</section></div></section>';}
  function openHero(heroId,shouldScroll){var hero=data.heroes.find(function(row){return Number(row.champion_id)===Number(heroId);});if(!hero)return;state.heroId=hero.champion_id;[ ].slice.call(document.querySelectorAll('.hero-tile')).forEach(function(tile){tile.setAttribute('aria-pressed',String(Number(tile.dataset.heroId)===hero.champion_id));});var adjustedWrCls=wrToneClass(hero.shrunk_wr),combat=hero.combat||{},combatMarkup='<div class="combat-profile"><span><b>'+Number(combat.kills_per_game||0).toFixed(1)+' / '+Number(combat.deaths_per_game||0).toFixed(1)+' / '+Number(combat.assists_per_game||0).toFixed(1)+'</b><small>平均 K / D / A</small></span><span><b>'+num(Math.round(combat.damage_per_minute||0))+'</b><small>英雄傷害／分</small></span><span><b>'+num(Math.round(combat.gold_per_minute||0))+'</b><small>金錢／分</small></span><span><b>'+Number(combat.cs_per_minute||0).toFixed(1)+'</b><small>CS／分</small></span></div>';detail.innerHTML='<div class="detail-head"><img src="'+esc(hero.image)+'" alt=""><div class="detail-identity"><h2>'+esc(hero.name_zh)+'</h2><p>'+esc(hero.name_en)+' · '+esc(hero.title_zh)+' · 常見分路 '+esc(hero.position_label)+'</p>'+combatMarkup+'</div></div><dl class="detail-stats"><div class="detail-stat"><dt>調整後勝率</dt><dd class="'+adjustedWrCls+'">'+pct(hero.shrunk_wr)+'</dd></div><div class="detail-stat"><dt>樣本數（選用率）</dt><dd>'+num(hero.games)+'（'+pct(hero.pick_rate)+'）</dd></div></dl>'+renderCompactLoadout(hero)+'<section class="detail-items"><div class="detail-items-head"><h3>裝備習慣</h3><p>終局持有資料；首件僅依背包前兩格推估，並非購買順序或因果推薦</p></div>'+renderItemsForHero(hero)+'</section>'+renderRelationships(hero);var group=document.querySelector('[data-tier-group="'+hero.tier+'"]');if(group)group.appendChild(detail);detail.hidden=false;if(shouldScroll)detail.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'nearest'});}
  function closeDetail(){state.heroId=null;detail.hidden=true;[ ].slice.call(document.querySelectorAll('.hero-tile')).forEach(function(tile){tile.setAttribute('aria-pressed','false');});}
  function setTab(tab,focus){state.tab=tab;tabs.forEach(function(button){var selected=button.dataset.tab===tab;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;if(selected&&focus)button.focus();});views.forEach(function(view){view.hidden=view.dataset.view!==tab;});var isItems=tab==='items';positionChips.hidden=isItems;itemKindChips.hidden=!isItems;search.placeholder=isItems?'搜尋裝備（中 / 英）':'搜尋英雄（中 / 英）';search.setAttribute('aria-label',isItems?'搜尋裝備':'搜尋英雄');if(isItems)closeDetail();refresh();}
  function updateCount(){var isItems=state.tab==='items',rows=isItems?activeItems():activeHeroes(),total=isItems?data.items.length:data.heroes.length;shownN.textContent=num(rows.length);shownTotal.textContent=num(total);shownUnit.textContent=isItems?'件':'隻';}
  function refresh(){updateTiles();renderHeroes();renderItems();updateCount();}
  tabs.forEach(function(tab,index){tab.addEventListener('click',function(){setTab(tab.dataset.tab,false);});tab.addEventListener('keydown',function(event){if(event.key!=='ArrowLeft'&&event.key!=='ArrowRight')return;event.preventDefault();var next=(index+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length;setTab(tabs[next].dataset.tab,true);});});
  [ ].slice.call(document.querySelectorAll('[data-position-filter]')).forEach(function(button){button.addEventListener('click',function(){positionFilter.value=button.dataset.positionFilter;[ ].slice.call(document.querySelectorAll('[data-position-filter]')).forEach(function(chip){var active=chip===button;chip.classList.toggle('active',active);chip.setAttribute('aria-pressed',String(active));});refresh();});});
  [ ].slice.call(document.querySelectorAll('[data-item-kind-filter]')).forEach(function(button){button.addEventListener('click',function(){itemKindFilter.value=button.dataset.itemKindFilter;[ ].slice.call(document.querySelectorAll('[data-item-kind-filter]')).forEach(function(chip){var active=chip===button;chip.classList.toggle('active',active);chip.setAttribute('aria-pressed',String(active));});refresh();});});
  [search,positionFilter,itemKindFilter,minGames,heroSort,itemSort].forEach(function(control){control.addEventListener(control===search?'input':'change',refresh);});
  document.addEventListener('click',function(event){var opener=event.target.closest('[data-open-hero]');if(opener){setTab('tier',false);openHero(opener.dataset.openHero,true);return;}var tile=event.target.closest('.hero-tile');if(tile){if(Number(state.heroId)===Number(tile.dataset.heroId)&&!detail.hidden)closeDetail();else openHero(tile.dataset.heroId,false);return;}var sorter=event.target.closest('[data-sort]');if(sorter){var target=sorter.dataset.sortTarget;var field=sorter.dataset.sort;if(target==='hero'){state.desc=heroSort.value===field?!state.desc:true;heroSort.value=field;}else{state.desc=itemSort.value===field?!state.desc:true;itemSort.value=field;}document.querySelectorAll('[data-sort-target="'+target+'"]').forEach(function(button){button.setAttribute('aria-sort',button===sorter?(state.desc?'descending':'ascending'):'none');});refresh();}});
  setTab('tier',false);
})();
"""


def render(rows: list[dict], total_games: int, per_patch: dict) -> str:
    board_rows = [r for r in rows if r["games"] >= TIER_BOARD_MIN_GAMES]
    thin = len(rows) - len(board_rows)
    by_tier: dict[str, list] = {t: [] for t in TIER_ORDER}
    for r in board_rows:
        by_tier[r["tier"]].append(r)

    patch_str = "、".join(
        f"{p} ({n:,})" for p, n in sorted(per_patch.items(), key=lambda kv: -kv[1])
    )
    built = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    # Mean Wilson half-width across champions — the headline "how wrong is this
    # number, typically" figure, so the banner never has to be re-tuned by hand.
    mean_err = (
        sum((r["ci_hi"] - r["ci_lo"]) / 2 for r in rows) / len(rows) if rows else 0.0
    )

    p: list[str] = []
    p.append("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>")
    p.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    # Unlisted: keep it out of search results and out of the sitemap.
    p.append("<meta name='robots' content='noindex,nofollow'>")
    p.append("<title>經典模式 英雄勝率（內部預覽）· classicmeta</title>")
    # Same brand face as the main site: Outfit for the Latin wordmark, Noto Sans TC
    # body.  Noto Serif TC is the extra here — the 經典模式 label is set in serif
    # to sell the nostalgia framing.
    p.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2"
        "?family=Outfit:wght@500;600;700"
        "&family=Noto+Sans+TC:wght@400;500;600;700"
        "&family=Noto+Serif+TC:wght@400;500&display=swap'>"
    )
    p.append(f"<style>{CSS}</style></head><body>")

    # Sticky top chrome mirrors the main site header (the "劉海"): blurred bar,
    # bottom hairline, same wordmark treatment.
    p.append("<header class='site-header'><div class='site-header-inner'>")
    p.append(
        "<span class='brand-title'>"
        "<span class='brand-aram'>classic</span><span class='brand-meta'>meta</span>"
        "</span>"
    )
    p.append("<span class='brand-div'>經典模式</span>")
    p.append("<span class='unlisted'>UNLISTED · 內部預覽</span>")
    p.append("</div></header>")
    # No subtitle line here: queue / gameMode / champion count / build time all
    # already live in the footer, and the main site goes straight from header to
    # content too. Repeating them under the wordmark was pure noise.
    p.append("<div class='wrap'>")

    p.append("<div class='toolbar'>")
    p.append(
        "<input id='q' type='search' placeholder='搜尋英雄（中 / 英）' "
        "autocomplete='off' spellcheck='false'>"
    )
    # Two views only. 「兩者」 was dropped: it made the control a three-way
    # choice where the honest default (the tier board) already answers the
    # question, and stacking both views just doubled the scroll.
    p.append(
        "<div class='seg'>"
        "<button data-view='board' class='on'>Tier 榜</button>"
        "<button data-view='table'>明細表</button>"
        "</div>"
    )
    # The sample-size caveat rides in the toolbar next to the view switch
    # rather than as a full-width banner: it is a standing property of the
    # data, not an alert, and a banner-sized warning above the fold was
    # shouting a footnote.  Numbers stay computed, so it self-updates.
    p.append(
        "<span class='caveat'>"
        f"僅 <b>{total_games:,}</b> 場 · 平均誤差 <b>±{mean_err * 100:.1f}%</b>"
        "</span>"
    )
    p.append("</div>")

    # ---- Tier board -------------------------------------------------------
    p.append("<div id='board'>")
    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        p.append(
            f"<div class='tier-block' data-tier='{tier}' "
            f"style='--tier-color:{TIER_COLOR[tier]}; --tier-bg:{TIER_LABEL_BG[tier]};'>"
        )
        p.append("<h2 class='tier-heading'>")
        p.append(f"<span class='tier-pill'><span>{tier}</span></span>")
        p.append(f"<span class='tier-count'>{len(entries)} 隻</span>")
        p.append("</h2>")
        p.append("<div class='tier-grid'>")
        for r in entries:
            wr = f"{r['shrunk_wr'] * 100:.1f}%"
            search = html.escape(
                f"{r['name_zh']} {r['title_zh']} {r['name_en']} {r['alias']}".lower(),
                quote=True,
            )
            title = (
                f"{r['name_zh']}　{r['title_zh']}\n"
                f"調整後 {wr} · 原始 {r['raw_wr'] * 100:.1f}% · "
                f"{r['games']:,} 場 · 95% CI "
                f"{r['ci_lo'] * 100:.1f}–{r['ci_hi'] * 100:.1f}%"
            )
            p.append(
                f"<div class='champ' data-search=\"{search}\" "
                f"title=\"{html.escape(title, quote=True)}\">"
                f"<img loading='lazy' src='{r['image']}' alt=''>"
                f"<span class='n'>{r['games']}</span>"
                f"<span class='wr'>{wr}</span>"
                f"<span class='name'>{html.escape(r['name_zh'])}</span>"
                f"</div>"
            )
        p.append("</div></div>")
    p.append("</div>")

    # ---- Detail table -----------------------------------------------------
    # Hidden on first paint: the board is the default view now that 「兩者」 is
    # gone.  Inline rather than JS-on-load so it never flashes open.
    p.append("<section id='table-sec' style='display:none'>")
    p.append("<h2 class='sec'>完整明細（含誤差範圍）</h2>")
    p.append(
        "<p class='sec-note'>"
        "<b>原始勝率</b>是直接觀察值；<b>調整後</b>把每隻英雄往 50% 收縮 "
        f"{PRIOR_GAMES} 場的先驗，樣本越小拉得越兇 — Tier 榜用的是調整後的值。"
        "<b>誤差條</b>是 95% Wilson 信賴區間（軸 30–70%，白線為觀察值，"
        "中央細線是 50% 硬幣線）：<b>只要色條跨過中央線，就代表這隻英雄"
        "和「五五波」在統計上分不出來</b>。點欄位標題可排序。"
        f"{f' 場次未達 {TIER_BOARD_MIN_GAMES} 的 {thin} 隻不進 Tier 榜。' if thin else ''}"
        "</p>"
    )
    p.append("<div class='table-wrap'><table><thead><tr>")
    for label in [
        "Tier", "英雄", "場次", "原始勝率", "調整後", "95% CI", "誤差範圍", "選用率",
    ]:
        p.append(f"<th>{label}</th>")
    p.append("</tr></thead><tbody>")
    for r in rows:
        search = html.escape(
            f"{r['name_zh']} {r['title_zh']} {r['name_en']} {r['alias']}".lower(),
            quote=True,
        )
        tier = r["tier"] if r["games"] >= TIER_BOARD_MIN_GAMES else "—"
        chip_bg = TIER_COLOR.get(tier, "#3a3f47")
        chip_fg = "#0e1116" if tier != "—" else "#9aa0a6"
        # Error bar geometry, mapped onto a fixed 30%-70% axis so every row is
        # comparable at a glance.
        axis_lo, axis_hi = 0.30, 0.70
        span = axis_hi - axis_lo

        def pos(v: float) -> float:
            return max(0.0, min(100.0, (v - axis_lo) / span * 100))

        left, right = pos(r["ci_lo"]), pos(r["ci_hi"])
        crosses = r["ci_lo"] <= 0.5 <= r["ci_hi"]
        bar_color = "#9aa0a6" if crosses else (
            "#8ec441" if r["raw_wr"] > 0.5 else "#ff5a3c"
        )
        p.append(f"<tr data-search=\"{search}\">")
        p.append(
            f"<td data-v='{r['shrunk_wr']:.6f}'>"
            f"<span class='tchip' style='background:{chip_bg};color:{chip_fg}'>"
            f"{tier}</span></td>"
        )
        p.append(
            f"<td><span class='cname'>"
            f"<img loading='lazy' src='{r['image']}' alt=''>"
            f"<span>{html.escape(r['name_zh'])} "
            f"<small>{html.escape(r['name_en'])}</small>"
            f"<em>{html.escape(r['title_zh'])}</em></span></span></td>"
        )
        p.append(f"<td class='num' data-v='{r['games']}'>{r['games']:,}</td>")
        p.append(
            f"<td class='num' data-v='{r['raw_wr']:.6f}'>{r['raw_wr'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td class='num' data-v='{r['shrunk_wr']:.6f}'>"
            f"{r['shrunk_wr'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td class='num' data-v='{r['ci_lo']:.6f}'>"
            f"{r['ci_lo'] * 100:.1f}–{r['ci_hi'] * 100:.1f}%</td>"
        )
        p.append(
            f"<td data-v='{(r['ci_hi'] - r['ci_lo']):.6f}'>"
            f"<span class='bar' style='--bc:{bar_color}'>"
            f"<i style='left:{left:.2f}%;width:{max(right - left, 1):.2f}%'></i>"
            f"<b style='left:{pos(r['raw_wr']):.2f}%'></b>"
            f"</span></td>"
        )
        p.append(
            f"<td class='num' data-v='{r['pick_rate']:.6f}'>"
            f"{r['pick_rate'] * 100:.1f}%</td>"
        )
        p.append("</tr>")
    p.append("</tbody></table></div></section>")

    p.append("<footer>")
    p.append(
        f"queue 4310（經典 / JADE）· {total_games:,} 場 · "
        f"{len(rows)} 隻英雄 · 頭像與稱號取自 Jade_* 條目（CommunityDragon），"
        f"非現行版本美術<br>"
        f"版本分佈：{html.escape(patch_str)}<br>"
        f"Tier 切點（調整後勝率）：OP ≥55% · T1 ≥52% · T2 ≥50% · "
        f"T3 ≥48% · T4 ≥46% · T5 &lt;46%<br>"
        f"由 <code>scripts/build_classic_page.py</code> 產生 · {built}<br>"
        "未公開頁面（noindex），不在主站導覽列。"
    )
    p.append("</footer>")

    p.append(f"</div><script>{JS}</script></body></html>")
    return "\n".join(p)


def render_research_preview(
    heroes: list[dict],
    items: list[dict],
    total_games: int,
    per_patch: dict,
    position_observations: int,
) -> str:
    """Render the interactive Classic research preview as a self-contained page."""
    built = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    mean_err = (
        sum((row["ci_hi"] - row["ci_lo"]) / 2 for row in heroes) / len(heroes)
        if heroes else 0.0
    )
    patch_str = "、".join(
        f"{patch} ({count:,})"
        for patch, count in sorted(per_patch.items(), key=lambda pair: -pair[1])
    )
    kind_label = {
        "complete": "完整裝備",
        "boots": "鞋子",
        "starter": "起手／組件",
        "trinket": "飾品",
    }
    payload_heroes = []
    for hero in heroes:
        payload_hero = dict(hero)
        payload_hero["search"] = " ".join(
            str(hero.get(key) or "").lower()
            for key in ("name_zh", "name_en", "title_zh", "alias")
        )
        for item_key in ("items", "starter_items", "first_complete_items"):
            for item in payload_hero.get(item_key) or []:
                item["kind_label"] = kind_label.get(item["kind"], item["kind"])
        payload_heroes.append(payload_hero)
    payload_items = []
    for item in items:
        payload_item = dict(item)
        payload_item["kind_label"] = kind_label.get(item["kind"], item["kind"])
        payload_item["search"] = " ".join(
            str(item.get(key) or "").lower()
            for key in ("name_zh", "name_en", "kind")
        )
        payload_items.append(payload_item)
    payload = json.dumps(
        {"heroes": payload_heroes, "items": payload_items},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    page: list[str] = []
    page.append("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>")
    page.append("<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>")
    page.append("<meta name='robots' content='noindex,nofollow'>")
    page.append("<title>經典模式資料研究（預覽）· classicmeta</title>")
    page.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2"
        "?family=Outfit:wght@500;600;700&family=Noto+Sans+TC:wght@400;500;600;700&display=swap'>"
    )
    page.append(f"<style>{CSS}</style></head><body>")
    page.append("<a class='skip-link' href='#main-content'>跳至主要內容</a>")
    page.append("<header class='site-header'><div class='site-header-inner'>")
    page.append("<span class='brand-title'><span class='brand-aram'>classic</span><span class='brand-meta'>meta</span></span>")
    page.append("<div class='classic-tabs' role='tablist' aria-label='經典模式資料視圖'>")
    for tab, label in (("tier", "英雄"), ("heroes", "明細"), ("items", "裝備")):
        selected = "true" if tab == "tier" else "false"
        tabindex = "0" if tab == "tier" else "-1"
        page.append(
            f"<button type='button' class='classic-tab' role='tab' data-tab='{tab}' "
            f"aria-selected='{selected}' aria-controls='view-{tab}' tabindex='{tabindex}'>{label}</button>"
        )
    page.append("</div>")
    page.append("<span class='unlisted'>經典模式 · 預覽</span>")
    page.append(
        "<div class='site-header-meta'><span>queue 4310</span>"
        f"<span>更新 {built}</span><span><b>初步可用</b></span></div>"
    )
    page.append("</div></header><main id='main-content' class='wrap'>")
    page.append("<section class='research-bar' aria-label='篩選與搜尋'>")
    page.append("<div class='filter-chips' id='position-chips' aria-label='常見分路篩選'>")
    page.append("<button type='button' class='filter-chip active' data-position-filter='' aria-pressed='true'>★ 全部</button>")
    page.extend(
        f"<button type='button' class='filter-chip' data-position-filter='{position}' aria-pressed='false'>{POSITION_LABELS[position]}</button>"
        for position in POSITION_ORDER
    )
    page.append("</div>")
    page.append("<div class='filter-chips' id='item-kind-chips' aria-label='裝備類型篩選' hidden>")
    for value, label in (("", "★ All"), ("complete", "完整裝備"), ("boots", "鞋子"), ("starter", "起手／組件"), ("trinket", "飾品")):
        active = " active" if not value else ""
        pressed = "true" if not value else "false"
        page.append(f"<button type='button' class='filter-chip{active}' data-item-kind-filter='{value}' aria-pressed='{pressed}'>{label}</button>")
    page.append("</div>")
    page.append(f"<span class='filter-count'><span id='shown-n'>{len(heroes)}</span> / <span id='shown-total'>{len(heroes)}</span> <span id='shown-unit'>隻</span></span>")
    page.append("<label class='filter-search'><svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><circle cx='11' cy='11' r='7'></circle><line x1='21' y1='21' x2='16.5' y2='16.5'></line></svg><input id='research-search' type='search' placeholder='搜尋英雄（中 / 英）' autocomplete='off' spellcheck='false' aria-label='搜尋英雄'></label>")
    page.append("<select class='filter-source' id='position-filter' tabindex='-1' aria-hidden='true'><option value=''>全部</option>" + "".join(f"<option value='{position}'>{POSITION_LABELS[position]}</option>" for position in POSITION_ORDER) + "</select>")
    page.append("<select class='filter-source' id='item-kind-filter' tabindex='-1' aria-hidden='true'><option value=''>全部</option><option value='complete'>完整裝備</option><option value='boots'>鞋子</option><option value='starter'>起手／組件</option><option value='trinket'>飾品</option></select>")
    page.append("<input class='filter-source' id='min-games' type='hidden' value='50'>")
    page.append("<select class='filter-source' id='hero-sort' tabindex='-1' aria-hidden='true'><option value='shrunk_wr'>調整後勝率</option><option value='raw_wr'>原始勝率</option><option value='pick_rate'>選用率</option><option value='games'>樣本數</option></select>")
    page.append("<select class='filter-source' id='item-sort' tabindex='-1' aria-hidden='true'><option value='games'>持有場次</option><option value='raw_wr'>持有者勝率</option><option value='hold_rate'>終局持有率</option></select></section>")

    page.append("<section class='view' id='view-tier' data-view='tier' role='tabpanel'>")
    page.append("<div class='tier-groups'>")
    by_tier = {tier: [hero for hero in heroes if hero["tier"] == tier and hero["games"] >= TIER_BOARD_MIN_GAMES] for tier in TIER_ORDER}
    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        page.append(f"<section class='tier-group' data-tier-group='{tier}' style='--tier-color:{TIER_COLOR[tier]};--tier-bg:{TIER_LABEL_BG[tier]}'><div class='tier-group-heading'><h2><span class='tier-pill'><span>{tier}</span></span></h2><p><span data-group-count>{len(entries)}</span> 隻 · 依調整後勝率</p></div><div class='hero-grid'>")
        for hero in entries:
            search_text = html.escape(payload_heroes[heroes.index(hero)]["search"], quote=True)
            label = html.escape(f"查看 {hero['name_zh']} 詳情，調整後勝率 {hero['shrunk_wr'] * 100:.1f}%", quote=True)
            page.append(
                f"<button type='button' class='hero-tile' data-hero-id='{hero['champion_id']}' "
                f"data-search='{search_text}' aria-pressed='false' aria-label='{label}'>"
                f"<span class='tile-icon'><img loading='lazy' src='{html.escape(hero['image'], quote=True)}' alt=''>"
                f"<span class='tile-stat {wr_tone_class(hero['shrunk_wr'])}'>{hero['shrunk_wr'] * 100:.1f}%</span></span>"
                f"<span class='tile-name'>{html.escape(hero['name_zh'])}</span></button>"
            )
        page.append("</div></section>")
    page.append("</div><section id='hero-detail' class='hero-detail' hidden aria-live='polite'></section><p id='tier-empty' class='empty-state' hidden>沒有符合這組篩選的英雄。請降低最低樣本或清除搜尋。</p></section>")

    page.append("<section class='view' id='view-heroes' data-view='heroes' role='tabpanel' hidden><div class='data-section-head'><h2>完整英雄明細</h2><p>Tier 固定以調整後勝率分級；欄位可排序。</p></div><div class='table-scroller'><table class='research-table'><thead><tr>")
    for field, label in (("tier", "Tier"), ("name_zh", "英雄"), ("games", "場次"), ("shrunk_wr", "調整後勝率"), ("pick_rate", "選用率")):
        page.append(f"<th><button type='button' data-sort-target='hero' data-sort='{field}' aria-sort='none'>{label}</button></th>")
    page.append("</tr></thead><tbody id='hero-tbody'></tbody></table></div><p id='heroes-empty' class='empty-state' hidden>沒有符合這組篩選的英雄。請降低最低樣本或清除搜尋。</p></section>")

    page.append("<section class='view' id='view-items' data-view='items' role='tabpanel' hidden><div class='data-section-head'><h2>裝備表現</h2><p>依終局背包計算，不代表購買順序或造成勝利。</p></div><p class='item-note'><b>終局持有者勝率</b><span>只描述在對局結束時持有該裝備的玩家勝率。請搭配樣本、95% CI 與英雄本身強度閱讀。</span></p><div class='table-scroller'><table class='research-table'><thead><tr>")
    for field, label in (("kind", "類型"), ("name_zh", "裝備"), ("hold_rate", "終局持有率"), ("games", "持有場次"), ("raw_wr", "持有者勝率"), ("ci_lo", "95% CI")):
        page.append(f"<th><button type='button' data-sort-target='item' data-sort='{field}' aria-sort='none'>{label}</button></th>")
    page.append("</tr></thead><tbody id='item-tbody'></tbody></table></div><p id='items-empty' class='empty-state' hidden>沒有符合這組篩選的裝備。請降低最低樣本或改變類型。</p></section>")
    page.append("<details class='method'><summary>資料與限制</summary><p>英雄 Tier 使用 Beta 收縮後的勝率（先驗 50%、強度 " + str(PRIOR_GAMES) + " 場），避免小樣本被偶然高勝率放大。95% CI 使用 Wilson 區間。常見分路以每位英雄在 LCU timeline.lane／role 中最多的分類為主要分路；次高分類至少要有 " + str(SECONDARY_POSITION_MIN_GAMES) + " 筆、且占該英雄有效分路紀錄 " + str(round(SECONDARY_POSITION_MIN_SHARE * 100)) + "%，才列為次要分路，最多顯示兩個。這是舊版客戶端推估訊號，不是精確的 teamPosition，適合做整體篩選，不應當成單場位置真值。裝備資料是終局背包中的持有關聯，沒有購買時間線，因此不應解讀為出裝順序、因果效果或直接推薦。</p><p>戰鬥輪廓是每場或每分鐘的描述統計。最佳搭檔與棘手對手只納入至少 " + str(RELATION_MIN_GAMES) + " 場的組合，並以 " + str(RELATION_PRIOR_GAMES) + " 場虛擬樣本向雙方英雄強度所推得的預期勝率收縮；它仍是完整 5v5 對局中的關聯，不是單線對決、因果效果或勝率保證。現有資料沒有符文與裝備購買時間線，因此本頁不顯示或推測這兩項。</p><p>版本分佈：" + html.escape(patch_str) + "。由 <code>scripts/build_classic_page.py</code> 產生於 " + built + "。</p></details>")
    page.append(
        "<footer class='research-context' aria-label='資料範圍'>"
        "<strong>經典模式資料研究</strong>"
        f"<p><span class='data-note'>{total_games:,} 場 · 平均 95% CI 半寬 ±{mean_err * 100:.1f}pp</span>"
        f"　常見分路由 {position_observations:,} 筆 LCU lane／role 紀錄推估；點英雄查看完整數據與終局裝備關聯。</p>"
        "</footer>"
    )
    page.append("</main><script id='classic-data' type='application/json'>" + payload + "</script><script>" + JS + "</script></body></html>")
    return "\n".join(page)


@click.command()
@click.option("--db", default="data/lcu/games.db", type=click.Path(exists=True))
@click.option("--out", default="docs/classic.html", type=click.Path())
@click.option("--patch", "patch_prefix", default="", help="版本前綴過濾；省略＝全收")
@click.option("--icon-dir", default=str(ICON_DIR), type=click.Path())
@click.option("--item-icon-dir", default=str(ITEM_ICON_DIR), type=click.Path())
@click.option("--refresh-icons", is_flag=True, help="重新下載頭像（改版換美術時用）")
def main(
    db: str,
    out: str,
    patch_prefix: str,
    icon_dir: str,
    item_icon_dir: str,
    refresh_icons: bool,
) -> None:
    db_path = Path(db)
    prefix = patch_prefix or None

    click.echo(f"[classic] scanning queue {CLASSIC_QUEUE_ID} from {db_path} ...")
    stats = collect_stats(db_path, prefix)
    games = stats["hero_games"]
    wins = stats["hero_wins"]
    total = stats["total_games"]
    per_patch = stats["per_patch"]
    if not total:
        raise click.ClickException(f"no queue {CLASSIC_QUEUE_ID} games found in {db}")
    click.echo(f"[classic] {total:,} games, {len(games)} champions")

    click.echo("[classic] fetching Jade_* champion metadata from CommunityDragon ...")
    meta = load_classic_champion_metadata()
    click.echo(f"[classic] {len(meta)} Jade entries")
    unknown = [c for c in games if c not in meta]
    if unknown:
        # base_champion_id() should make this impossible; if it fires, the Jade
        # offset assumption has drifted and the page would silently drop champs.
        click.echo(f"[classic] WARNING: {len(unknown)} unmapped champion ids: {unknown}")

    fetched = download_icons(meta, Path(icon_dir), refresh_icons)
    click.echo(
        f"[classic] icons: {fetched} downloaded, "
        f"{len(list(Path(icon_dir).glob('*.png')))} on disk -> {icon_dir}"
    )

    rows = build_rows(games, wins, total, meta, stats["hero_position_games"])
    attach_combat_profiles(rows, stats["hero_combat_sums"])
    attach_relationships(
        rows,
        stats["ally_pair_games"],
        stats["ally_pair_wins"],
        stats["matchup_games"],
        stats["matchup_wins"],
    )
    click.echo("[classic] fetching item metadata for final-inventory association ...")
    item_meta = load_classic_item_metadata()
    observed_item_ids = set(stats["item_games"])
    fetched_items = download_item_icons(item_meta, observed_item_ids, Path(item_icon_dir))
    click.echo(
        f"[classic] item icons: {fetched_items} downloaded, "
        f"{len(list(Path(item_icon_dir).glob('*.png')))} on disk -> {item_icon_dir}"
    )
    item_rows = build_item_rows(stats["item_games"], stats["item_wins"], total, item_meta)
    attach_hero_items(
        rows,
        stats["hero_item_games"],
        stats["hero_item_wins"],
        stats["hero_first_slots_games"],
        stats["hero_first_slots_wins"],
        item_meta,
    )
    if not item_rows:
        raise click.ClickException("no Classic final-inventory items could be resolved")

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_research_preview(
            rows,
            item_rows,
            total,
            per_patch,
            stats["position_observations"],
        ),
        encoding="utf-8",
    )

    size_kb = out_path.stat().st_size / 1024
    click.echo(f"[classic] wrote {out_path} ({size_kb:.0f} KB, {len(item_rows)} items)")
    top = rows[0]
    click.echo(
        f"[classic] top: {top['name_zh']} {top['shrunk_wr'] * 100:.1f}% "
        f"(raw {top['raw_wr'] * 100:.1f}%, {top['games']} games)"
    )


if __name__ == "__main__":
    main()
