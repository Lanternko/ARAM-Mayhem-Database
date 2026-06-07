"""Generate a tier-list HTML from Mayhem (or ARAM) winrates.

Reads winrates from data/lcu/games.db, fetches champion id->name mapping from
Riot's Data Dragon CDN, applies Bayesian smoothing, and renders an HTML grid
where each champion icon carries a tier badge (OP / T1..T5) in the top-right.

Clicking a champion expands an inline panel below its tier-row showing the
top-5 best and bottom-5 worst augments (by empirical-Bayes lower-bound lift;
peer-relative pick-rate is kept as diagnostics), plus best/worst same-team
teammate synergies.  A right-side panel also lets users pick 1-4 champions and
        rank recommended teammates by aggregated anchor-conditional synergy.

Usage:
    python scripts/build_tier_list.py
    python scripts/build_tier_list.py --queue 2400 --patch-prefix 16.10 --out tier_list.html
    python scripts/build_tier_list.py --queue 450  --patch-prefix 16.9
"""
from __future__ import annotations

import datetime as _dt
import csv
import html
from io import BytesIO
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import click
import httpx

try:
    from champion_roles import (
        MARKSMAN_ITEM_STYLES,
        RANGED_ATTACK_RANGE_MIN,
        ROLE_FROM_ITEM_STYLE,
        ROLE_LABELS,
        ROLE_ORDER,
        ROLE_RANGED_ALIAS_OVERRIDES,
        ROLE_SORT_PRIORITY,
        role_definitions_payload,
        role_tags_for_alias,
    )
except ImportError:  # pragma: no cover - supports importing as scripts.build_tier_list.
    from scripts.champion_roles import (
        MARKSMAN_ITEM_STYLES,
        RANGED_ATTACK_RANGE_MIN,
        ROLE_FROM_ITEM_STYLE,
        ROLE_LABELS,
        ROLE_ORDER,
        ROLE_RANGED_ALIAS_OVERRIDES,
        ROLE_SORT_PRIORITY,
        role_definitions_payload,
        role_tags_for_alias,
    )

try:
    from scipy.optimize import minimize_scalar
    from scipy.special import betaln, betaincinv
except Exception:  # pragma: no cover - scipy is installed through sklearn locally.
    minimize_scalar = None
    betaln = None
    betaincinv = None

TIER_ORDER = ["OP", "T1", "T2", "T3", "T4", "T5"]
TIER_COLOR = {
    "OP": "#d8b8ff",
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}
# OP gets a prismatic/iridescent look with shine + glow (see CSS below).
# Other tiers stay solid.
TIER_LABEL_BG = {
    "OP": (
        "linear-gradient(135deg,"
        "#ffffff 0%,#e7d5ff 18%,#bcd6ff 36%,"
        "#ffd5ec 58%,#fff1c8 78%,#ffffff 100%)"
    ),
    "T1": "#ff5a3c",
    "T2": "#f5c518",
    "T3": "#8ec441",
    "T4": "#3aa0ff",
    "T5": "#7a7f8a",
}

CDRAGON_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"
SITE_ICON_SOURCE = Path("docs/favicon-source.png")
AUGMENT_PRIOR_DEFAULT = 350.0
AUGMENT_POSTERIOR_Q = 0.10
AUGMENT_LCB_Z = 1.2815515655446004
AUGMENT_PICK_LIFT_WEIGHT = 0.003
AUGMENT_PICK_LIFT_CAP = 3.0
EMPIRICAL_CHAMPION_SCORES = Path("data/cache/champion_scores_empirical_merged.csv")
SEMANTIC_CHAMPION_SCORES = Path("data/cache/champion_semantic_scores.csv")
ITEM_MIN_TOTAL_GOLD = 1800
ITEM_BOOT_MIN_TOTAL_GOLD = 900
GUARDIAN_STARTER_ITEM_IDS = frozenset({2051, 3112, 3177, 3184})
ANTIHEAL_COMPONENT_NAME_KEYWORDS = (
    "oblivion orb",
    "bramble vest",
    "executioner's calling",
)
ANTIHEAL_ITEM_NAME_KEYWORDS = (
    "oblivion orb",
    "bramble vest",
    "executioner's calling",
    "morellonomicon",
    "thornmail",
    "mortal reminder",
    "chempunk chainsword",
)
CATEGORY_PRIOR_DEFAULT = AUGMENT_PRIOR_DEFAULT
ITEM_STYLE_MIN_GAMES = 150
ITEM_STYLE_FALLBACK_MIN_GAMES = 100
ITEM_PAIR_MIN_GAMES = 30
ITEM_PAIR_FALLBACK_MIN_GAMES = 20
ITEM_PAIR_TOP_MIN_LIFT = -0.02
ITEM_PAIR_PICK_LIFT_WEIGHT = 0.0
ITEM_PAIR_PICK_LIFT_CAP = AUGMENT_PICK_LIFT_CAP
ITEM_PAIR_PICK_RATE_WEIGHT = 0.012
ITEM_PAIR_PICK_RATE_REF = 0.005
ITEM_PAIR_PICK_RATE_CAP = 0.045
ITEM_PAIR_ORDER_PRIOR_GAMES = 20
SINGLE_ITEM_MIN_GAMES = 30
SINGLE_ITEM_FALLBACK_MIN_GAMES = 20
SINGLE_ITEM_TOP_MIN_LIFT = -0.02
SINGLE_ITEM_PICK_LIFT_WEIGHT = ITEM_PAIR_PICK_LIFT_WEIGHT
SINGLE_ITEM_PICK_LIFT_CAP = ITEM_PAIR_PICK_LIFT_CAP
SINGLE_ITEM_PICK_RATE_WEIGHT = ITEM_PAIR_PICK_RATE_WEIGHT
SINGLE_ITEM_PICK_RATE_REF = ITEM_PAIR_PICK_RATE_REF
SINGLE_ITEM_PICK_RATE_CAP = ITEM_PAIR_PICK_RATE_CAP
SINGLE_ITEM_COMMON_TRAP_N = 6
SINGLE_ITEM_COMMON_TRAP_MIN_LIFT = -0.01
BOOT_ITEM_MIN_GAMES = 30
BOOT_ITEM_FALLBACK_MIN_GAMES = 20
BOOT_ITEM_TOP_MIN_LIFT = -0.04
ITEM_CLUSTER_MIN_PAIR_GAMES = 20
ITEM_CLUSTER_MIN_COSINE = 0.10
ITEM_CLUSTER_MIN_GAMES = 20
ITEM_CLUSTER_MIN_EXACT_GAMES = 3
ITEM_CLUSTER_ITEM_EVIDENCE_MIN_GAMES = 50
ITEM_CLUSTER_ITEM_FALLBACK_MIN_GAMES = 20
ITEM_CLUSTER_ITEM_FALLBACK_MIN_LIFT = 0.02
ITEM_CLUSTER_TOP_MIN_LIFT = -0.02
ITEM_CLUSTER_TOP_N = 4
ITEM_CLUSTER_MAX_ITEMS = 6
ITEM_CLUSTER_MAX_EXACT_ROUTES_PER_CHAMP = 100
ITEM_CLUSTER_PAIR_WEIGHT = 0.45
ITEM_CLUSTER_SINGLE_WEIGHT = 0.35
ITEM_CLUSTER_GLOBAL_WEIGHT = 0.20
ITEM_CLUSTER_CORE_ITEM_COUNT = 3
ITEM_CLUSTER_ROUTE_LIFT_WEIGHT = 0.55
ITEM_CLUSTER_CORE_PAIR_WEIGHT = 0.45
ITEM_CLUSTER_CORE_SINGLE_WEIGHT = 0.40
ITEM_CLUSTER_CORE_GLOBAL_WEIGHT = 0.12
ITEM_CLUSTER_FLEX_SINGLE_WEIGHT = 0.06
ITEM_CLUSTER_FLEX_GLOBAL_WEIGHT = 0.06
ITEM_CLUSTER_FLEX_STABILITY_PICK_REF = 0.15
ITEM_CLUSTER_FLEX_STABILITY_WEIGHT = 1.25
ITEM_CLUSTER_EXACT_GAMES_WEIGHT = 0.014
ITEM_CLUSTER_PICK_RATE_WEIGHT = 0.012
ITEM_CLUSTER_PICK_RATE_REF = 0.01
ITEM_CLUSTER_PICK_RATE_CAP = 0.035
ITEM_CLUSTER_DIVERSITY_MAX_JACCARD = 0.66
ITEM_CLUSTER_DIVERSITY_HARD_MAX_JACCARD = 0.80
PATCH_CHANGE_TOP_N = 10
PATCH_CHANGE_HERO_MIN_GAMES = 500
PATCH_CHANGE_ITEM_CURRENT_MIN_GAMES = 500
PATCH_CHANGE_ITEM_BASELINE_MIN_GAMES = 800
PATCH_CHANGE_CHAMP_ITEM_CURRENT_MIN_GAMES = 80
PATCH_CHANGE_CHAMP_ITEM_BASELINE_MIN_GAMES = 120
PATCH_CHANGE_ITEM_PRIOR_GAMES = 200
PATCH_CHANGE_CHAMP_ITEM_PRIOR_GAMES = 30
AUGMENT_TYPE_MIN_GAMES = 100

ITEM_STYLE_LABELS = {
    "ap_burn": {"zh": "AP燃燒", "en": "AP burn"},
    "ap_burst": {"zh": "AP爆發", "en": "AP burst"},
    "ap_bruiser": {"zh": "法坦", "en": "AP bruiser"},
    "ap_onhit": {"zh": "混傷命中", "en": "Hybrid on-hit"},
    "ad_bruiser": {"zh": "AD鬥士", "en": "AD bruiser"},
    "ad_assassin": {"zh": "物穿刺客", "en": "Lethality assassin"},
    "ad_poke": {"zh": "AD poke", "en": "AD poke"},
    "crit": {"zh": "暴擊", "en": "Crit"},
    "onhit": {"zh": "攻速命中", "en": "AS / on-hit"},
    "heartsteel": {"zh": "心之鋼", "en": "Heartsteel"},
    "tank": {"zh": "坦克", "en": "Tank"},
    "support": {"zh": "輔助", "en": "Support"},
}

AP_BURN_ITEM_KEYWORDS = (
    "liandry",
    "blackfire torch",
    "demonic embrace",
    "malignance",
    "pyromancer",
)

AP_BRUISER_ITEM_KEYWORDS = (
    "abyssal mask",
    "banshee",
    "bloodletter",
    "cosmic drive",
    "crown of the shattered queen",
    "cruelty",
    "demon king",
    "everfrost",
    "innervating locket",
    "lightning braid",
    "moonflair",
    "morellonomicon",
    "riftmaker",
    "rod of ages",
    "rylai",
    "sanguine gift",
    "twin mask",
    "twilight's edge",
    "zhonya",
)

AP_BURST_ITEM_KEYWORDS = (
    "actualizer",
    "archangel",
    "cryptbloom",
    "deathfire",
    "detonation orb",
    "flesheater",
    "hextech gunblade",
    "hextech rocketbelt",
    "horizon focus",
    "luden",
    "night harvester",
    "perplexity",
    "rabadon",
    "runecarver",
    "seraph",
    "shadowflame",
    "stormsurge",
    "void staff",
    "wooglet",
    "wordless promise",
)

AP_ONHIT_ITEM_KEYWORDS = (
    "dusk and dawn",
    "guinsoo",
    "lich bane",
    "nashor",
    "reality fracture",
    "reaper's toll",
    "statikk",
)

SUPPORT_ITEM_KEYWORDS = (
    "ardent",
    "chemtech putrifier",
    "dawncore",
    "echoes of helia",
    "empirean promise",
    "imperial mandate",
    "locket",
    "mikael",
    "moonstone",
    "puppeteer",
    "redemption",
    "shurelya",
    "staff of flowing",
    "sword of blossoming dawn",
)

AD_POKE_ITEM_KEYWORDS = (
    "bastionbreaker",
    "diamond-tipped spear",
    "hellfire hatchet",
    "manamune",
    "muramana",
    "serylda",
)

AD_ASSASSIN_ITEM_KEYWORDS = (
    "axiom arc",
    "duskblade",
    "edge of night",
    "gambler's blade",
    "hubris",
    "opportunity",
    "profane hydra",
    "prowler",
    "regicide",
    "serpent",
    "spectral cutlass",
    "umbral glaive",
    "voltaic",
    "youmuu",
)

AD_BRUISER_ITEM_KEYWORDS = (
    "black cleaver",
    "bloodthirster",
    "blade of the ruined king",
    "chempunk",
    "death's dance",
    "divine sunderer",
    "eclipse",
    "endless hunger",
    "experimental hexplate",
    "frozen mallet",
    "goredrinker",
    "guardian angel",
    "hemomancer",
    "hullbreaker",
    "innervating locket",
    "maw of malmortius",
    "mercurial scimitar",
    "overlord",
    "ravenous hydra",
    "sanguine blade",
    "shield of the rakkor",
    "silvermere dawn",
    "spear of shojin",
    "sterak",
    "stridebreaker",
    "sundered sky",
    "titanic hydra",
    "trinity force",
)

HEARTSTEEL_ITEM_IDS = {3084, 223084, 323084}
# Quest: Icathia's Fall combines Sunfire Aegis + Hollow Radiance into this reward item.
AUGMENT_GATED_ITEM_IDS = {223069}
ROLE_RANGED_ALIAS_OVERRIDES = {"Kayle"}
HEARTSTEEL_TANK_FOLLOWUP_STYLES = {"tank"}
HEARTSTEEL_BRUISER_FOLLOWUP_STYLES = {
    "ad_bruiser",
    "ap_bruiser",
    "ap_burn",
    "ap_onhit",
    "onhit",
}

AUGMENT_TYPE_LABELS = {
    "damage": {"zh": "傷害", "en": "Damage"},
    "spell": {"zh": "技能 / AP", "en": "Spell / AP"},
    "attack": {"zh": "普攻 / AD", "en": "Attack / AD"},
    "crit": {"zh": "暴擊", "en": "Crit"},
    "tank": {"zh": "坦克", "en": "Tank"},
    "sustain": {"zh": "治療護盾", "en": "Heal / Shield"},
    "mobility": {"zh": "機動進場", "en": "Mobility"},
    "snowball": {"zh": "雪球", "en": "Snowball"},
    "economy": {"zh": "經濟", "en": "Economy"},
    "stacking": {"zh": "疊層成長", "en": "Stacking"},
    "utility": {"zh": "控制輔助", "en": "Utility"},
    "auto": {"zh": "自動觸發", "en": "Automated"},
}

AUGMENT_DISPLAY_TAG_LABELS = {
    0: {"zh": "隊友", "en": "Ally"},
    1: {"zh": "傷害", "en": "Damage"},
    2: {"zh": "一般", "en": "General"},
    3: {"zh": "韌性", "en": "Tenacity"},
    4: {"zh": "速度", "en": "Speed"},
    5: {"zh": "輔助", "en": "Support"},
    # Only Red Envelopes currently uses 7 in the local Kiwi data. Treat it as
    # economy until a live screenshot shows Riot's exact zh-TW label.
    7: {"zh": "金幣", "en": "Gold"},
}

COMPOSITION_SCORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
    "poke_score",
    "sustain_score",
    "frontline_score",
)
COMPOSITION_LACK_THRESHOLDS = {
    "wave": 3.0,
    "cc": 3.0,
    "engage": 2.2,
    "damage": 5.5,
    "poke": 2.0,
    "sustain": 1.5,
    "front": 1.8,
}
RECOMMENDATION_COMPOSITION_WEIGHT = 0.25
RECOMMENDATION_COMPOSITION_CLAMP = 0.05
RECOMMENDATION_DAMAGE_MIX_TARGET_AD = 0.40
RECOMMENDATION_DAMAGE_MIX_WEIGHT = 0.18
RECOMMENDATION_DAMAGE_MIX_CLAMP = 0.025
RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS = {
    "ad_front": 0.55,
    "poke_front": 0.30,
    "wave_engage": 0.15,
    "all_lacks": 0.15,
    "mage_ad": 0.20,
    "marksman_ad": 0.20,
}
RECOMMENDATION_COMPOSITION_TABLES = {
    "ad_front": {
        "0 front|<35% AD": -0.0393,
        "0 front|35-45% AD": 0.0181,
        "0 front|45-55% AD": -0.0189,
        "0 front|55-65% AD": -0.0201,
        "0 front|>=65% AD": -0.0383,
        "1 front|<35% AD": -0.0019,
        "1 front|35-45% AD": 0.0187,
        "1 front|45-55% AD": 0.0149,
        "1 front|55-65% AD": 0.0185,
        "1 front|>=65% AD": -0.0037,
        "2+ front|<35% AD": -0.0146,
        "2+ front|35-45% AD": 0.0160,
        "2+ front|45-55% AD": 0.0097,
        "2+ front|55-65% AD": -0.0148,
        "2+ front|>=65% AD": -0.0388,
    },
    "poke_front": {
        "0 front|poke lack": -0.0447,
        "0 front|poke ok": -0.0164,
        "1 front|poke lack": 0.0228,
        "1 front|poke ok": 0.0102,
        "2+ front|poke lack": -0.0497,
        "2+ front|poke ok": -0.0006,
    },
    "wave_engage": {
        "wave lack|engage lack": -0.0160,
        "wave lack|engage ok": -0.0105,
        "wave ok|engage lack": 0.0073,
        "wave ok|engage ok": 0.0002,
    },
    "all_lacks": {
        "0": 0.0026,
        "1": -0.0053,
        "2+": -0.0119,
    },
    "mage_ad": {
        "0|>=65% AD": -0.0499,
        "1|35-45% AD": 0.0006,
        "1|45-55% AD": -0.0118,
        "1|55-65% AD": -0.0042,
        "1|>=65% AD": -0.0285,
        "2+|<35% AD": -0.0135,
        "2+|35-45% AD": 0.0187,
        "2+|45-55% AD": 0.0110,
        "2+|55-65% AD": 0.0013,
        "2+|>=65% AD": -0.0099,
    },
    "marksman_ad": {
        "0|<35% AD": -0.0266,
        "0|35-45% AD": -0.0141,
        "0|45-55% AD": -0.0037,
        "0|55-65% AD": -0.0354,
        "1|<35% AD": -0.0030,
        "1|35-45% AD": 0.0267,
        "1|45-55% AD": 0.0176,
        "1|55-65% AD": -0.0063,
        "1|>=65% AD": -0.0153,
        "2+|<35% AD": -0.0318,
        "2+|35-45% AD": 0.0188,
        "2+|45-55% AD": -0.0016,
        "2+|55-65% AD": 0.0071,
        "2+|>=65% AD": -0.0299,
    },
}

def _mean(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0

_FRONTLINE_ROLE_DELTA = (
    RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS["ad_front"] * _mean([
        RECOMMENDATION_COMPOSITION_TABLES["ad_front"][f"1 front|{ad_bin}"]
        - RECOMMENDATION_COMPOSITION_TABLES["ad_front"][f"0 front|{ad_bin}"]
        for ad_bin in ("<35% AD", "35-45% AD", "45-55% AD", "55-65% AD", ">=65% AD")
    ])
    + RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS["poke_front"] * _mean([
        RECOMMENDATION_COMPOSITION_TABLES["poke_front"][f"1 front|{state}"]
        - RECOMMENDATION_COMPOSITION_TABLES["poke_front"][f"0 front|{state}"]
        for state in ("poke lack", "poke ok")
    ])
)
_MARKSMAN_ROLE_DELTA = RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS["marksman_ad"] * _mean([
    RECOMMENDATION_COMPOSITION_TABLES["marksman_ad"][f"1|{ad_bin}"]
    - RECOMMENDATION_COMPOSITION_TABLES["marksman_ad"][f"0|{ad_bin}"]
    for ad_bin in ("<35% AD", "35-45% AD", "45-55% AD", "55-65% AD")
])
_MAGE_ROLE_DELTA = RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS["mage_ad"] * _mean([
    RECOMMENDATION_COMPOSITION_TABLES["mage_ad"][f"1|{ad_bin}"]
    - RECOMMENDATION_COMPOSITION_TABLES["mage_ad"].get(
        f"0|{ad_bin}",
        RECOMMENDATION_COMPOSITION_TABLES["mage_ad"].get("0|>=65% AD", 0.0),
    )
    for ad_bin in ("35-45% AD", "45-55% AD", "55-65% AD", ">=65% AD")
])
ROLE_NEED_CREDITS = {
    "Tank": RECOMMENDATION_COMPOSITION_WEIGHT * _FRONTLINE_ROLE_DELTA,
    "Marksman": RECOMMENDATION_COMPOSITION_WEIGHT * _MARKSMAN_ROLE_DELTA,
    "Mage": RECOMMENDATION_COMPOSITION_WEIGHT * _MAGE_ROLE_DELTA,
    "Support": 0.0,
}

MAYHEM_AUGMENT_SETS = {
    "Archmage": [
        "Buff Buddies",
        "Juiced",
        "Mind to Matter",
        "Ocean Soul",
        "Overflow",
    ],
    "Dive Bomb": [
        "Clown College",
        "Dive Bomber",
        "Final City Transit",
        "Self Destruct",
    ],
    "Firecracker": [
        "Critical Missile",
        "Fan the Hammer",
        "Light 'em Up!",
        "Magic Missile",
        "Twin Fire",
        "Typhoon",
    ],
    "Fully Automated": [
        "Divine Intervention",
        "Firefox",
        "Frost Wraith",
        "OK Boomerang",
        "Prom Queen",
        "Quantum Computing",
        "Self Destruct",
        "Sonata",
    ],
    "High Roller": [
        "Pandora's Box",
        "Stats!",
        "Stats on Stats!",
        "Stats on Stats on Stats!",
        "Transmute: Chaos",
        "Transmute: Gold",
        "Transmute: Prismatic",
    ],
    "Make it Rain": [
        "Donation",
        "From Beginning to End",
        "Goldrend",
        "Heads Up Cupcake!",
        "Red Envelopes",
        "Upgrade: Collector",
        "Upgrade: Immolate",
    ],
    "Snowday": [
        "Biggest Snowball Ever",
        "Holy Snowball",
        "Pinball",
        "Snowball Roulette",
        "Snowball Upgrade",
    ],
    "Stackosaurus Rex": [
        "Infinite Recursion",
        "Master of Duality",
        "Phenomenal Evil",
        "Quest: Steel Your Heart",
        "Shrink Engine",
        "Slap Around",
        "Soul Eater",
        "Tap Dancer",
        "Upgrade: Hubris",
    ],
    "Wee Woo Wee Woo": [
        "All For You",
        "Critical Healing",
        "First-Aid Kit",
        "I'm a Baby Kitty Where is Mama",
        "Sonata",
        "Upgrade Mikael's Blessing",
        "Windspeaker's Blessing",
    ],
}

MAYHEM_AUGMENT_SET_LABELS = {
    "Archmage": {"zh": "大法師", "en": "Archmage"},
    "Dive Bomb": {"zh": "俯衝轟炸", "en": "Dive Bomb"},
    "Firecracker": {"zh": "爆竹", "en": "Firecracker"},
    "Fully Automated": {"zh": "全自動", "en": "Fully Automated"},
    "High Roller": {"zh": "豪賭", "en": "High Roller"},
    "Make it Rain": {"zh": "天降財雨", "en": "Make it Rain"},
    "Snowday": {"zh": "雪球日", "en": "Snowday"},
    "Stackosaurus Rex": {"zh": "疊疊暴龍", "en": "Stackosaurus Rex"},
    "Wee Woo Wee Woo": {"zh": "警笛大響", "en": "Wee Woo Wee Woo"},
}

def render_analytics_tags(
    *,
    cloudflare_token: str = "",
    ga_measurement_id: str = "",
) -> list[str]:
    tags: list[str] = []
    cloudflare_token = cloudflare_token.strip()
    ga_measurement_id = ga_measurement_id.strip()

    if cloudflare_token:
        cf_config = html.escape(json.dumps({"token": cloudflare_token}), quote=True)
        tags.append(
            "<script defer src='https://static.cloudflareinsights.com/beacon.min.js' "
            f"data-cf-beacon='{cf_config}'></script>"
        )

    if ga_measurement_id:
        ga_id = html.escape(ga_measurement_id, quote=True)
        ga_id_js = json.dumps(ga_measurement_id)
        tags.append(
            f"<script async src='https://www.googletagmanager.com/gtag/js?id={ga_id}'></script>"
            "<script>"
            "window.dataLayer=window.dataLayer||[];"
            "function gtag(){dataLayer.push(arguments);}"
            "gtag('js',new Date());"
            f"gtag('config',{ga_id_js});"
            "</script>"
        )

    return tags

def _slugify_set_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def _normalize_augment_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())

def _augment_set_lookup() -> dict[str, list[dict[str, str]]]:
    lookup: dict[str, list[dict[str, str]]] = {}
    for set_name, aug_names in MAYHEM_AUGMENT_SETS.items():
        slug = _slugify_set_name(set_name)
        labels = MAYHEM_AUGMENT_SET_LABELS.get(
            set_name,
            {"zh": set_name, "en": set_name},
        )
        for aug_name in aug_names:
            info = {
                "name": set_name,
                "name_zh": labels["zh"],
                "name_en": labels["en"],
                "slug": slug,
            }
            lookup.setdefault(_normalize_augment_name(aug_name), []).append(info)
            if aug_name.startswith("Upgrade: "):
                lookup.setdefault(
                    _normalize_augment_name(aug_name.replace("Upgrade: ", "Upgrade ")),
                    [],
                ).append(info)
    return lookup

def _queue_copy(queue_id: int) -> tuple[str, str]:
    # queue 2400 was Mayhem's queueId during the 16.x cycle.
    if queue_id == 2400:
        return "ARAM 大亂鬥", "ARAM Mayhem (queueId 2400)"
    if queue_id == 450:
        return "ARAM 勝率 Tier List", "ARAM (queueId 450)"
    return f"Tier List (queueId {queue_id})", f"queueId {queue_id}"

def _load_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msjhbd.ttc" if bold else "C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/NotoSansTC-Bold.otf" if bold else "C:/Windows/Fonts/NotoSansTC-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()

def _draw_text_fit(draw, xy: tuple[int, int], text: str, font, fill: str, max_width: int) -> None:
    # Pillow can hang measuring some CJK fonts on Windows, so keep this
    # deliberately simple for the fixed-size OG canvas.
    char_budget = max(8, max_width // 20)
    if len(text) > char_budget:
        text = text[: char_budget - 3].rstrip() + "..."
    draw.text(xy, text, font=font, fill=fill)

def _draw_prismatic_frame(img, box: tuple[int, int, int, int], radius: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    x1, y1, x2, y2 = box
    border_w = 14
    stops = [
        (0.00, (216, 184, 255)),
        (0.28, (188, 214, 255)),
        (0.56, (255, 213, 236)),
        (0.82, (231, 213, 255)),
        (1.00, (216, 184, 255)),
    ]

    def sample(t: float) -> tuple[int, int, int, int]:
        for idx in range(len(stops) - 1):
            left_t, left = stops[idx]
            right_t, right = stops[idx + 1]
            if t <= right_t:
                local = 0.0 if right_t == left_t else (t - left_t) / (right_t - left_t)
                rgb = tuple(int(left[c] + (right[c] - left[c]) * local) for c in range(3))
                return (*rgb, 255)
        return (*stops[-1][1], 255)

    ring_mask = Image.new("L", img.size, 0)
    ring_draw = ImageDraw.Draw(ring_mask)
    ring_draw.rounded_rectangle(box, radius=radius, fill=255)
    ring_draw.rounded_rectangle(
        (x1 + border_w, y1 + border_w, x2 - border_w, y2 - border_w),
        radius=radius - border_w,
        fill=0,
    )

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (x1 - 5, y1 - 5, x2 + 5, y2 + 5),
        radius=radius + 5,
        outline=(216, 184, 255, 140),
        width=9,
    )
    glow_draw.rounded_rectangle(
        (x1 - 10, y1 - 10, x2 + 10, y2 + 10),
        radius=radius + 10,
        outline=(188, 214, 255, 80),
        width=7,
    )
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7)))

    gradient = Image.new("RGBA", img.size, (0, 0, 0, 0))
    px = gradient.load()
    denom = max(1, (x2 - x1) + (y2 - y1))
    for y in range(y1, y2 + 1):
        for x in range(x1, x2 + 1):
            if ring_mask.getpixel((x, y)):
                px[x, y] = sample(((x - x1) + (y - y1)) / denom)
    img.alpha_composite(gradient)

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (x1 + border_w + 2, y1 + border_w + 2, x2 - border_w - 2, y2 - border_w - 2),
        radius=radius - border_w - 2,
        outline="#090c12",
        width=4,
    )

def write_og_image(
    out_path: Path,
    records: list[dict],
    champ_meta: dict[int, dict],
    *,
    queue_id: int,
    patch_prefix: str | None,
    total_games: int,
) -> None:
    """Write a square top-champion thumbnail for Open Graph cards."""
    from PIL import Image, ImageDraw

    top_record = records[0] if records else None
    top_meta = champ_meta.get(top_record["champion_id"]) if top_record else None
    top_wr = float(top_record.get("bayes_wr", 0.0)) if top_record else 0.0

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    badge_font = _load_font(58, bold=True)

    card_x, card_y, card_size = 58, 58, 396
    frame_box = (card_x - 24, card_y - 24, card_x + card_size + 24, card_y + card_size + 24)
    draw.rounded_rectangle(frame_box, radius=36, fill="#080a10")
    _draw_prismatic_frame(img, frame_box, 36)
    if top_meta and top_meta.get("image"):
        try:
            resp = httpx.get(top_meta["image"], timeout=5)
            resp.raise_for_status()
            icon = Image.open(BytesIO(resp.content)).convert("RGB").resize((card_size, card_size))
            mask = Image.new("L", (card_size, card_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, card_size, card_size), radius=24, fill=255)
            img.paste(icon, (card_x, card_y), mask)
        except Exception:
            draw.rounded_rectangle((card_x, card_y, card_x + card_size, card_y + card_size), radius=24, fill="#242b3a")
    else:
        draw.rounded_rectangle(
            (card_x, card_y, card_x + card_size, card_y + card_size),
            radius=24,
            fill="#242b3a",
        )
    badge_text = f"{top_wr * 100:.1f}%"
    draw.rounded_rectangle((card_x, card_y + card_size - 102, card_x + 190, card_y + card_size), radius=22, fill="#0d111a")
    draw.text((card_x + 22, card_y + card_size - 86), badge_text, font=badge_font, fill="#f8fbff")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)

def write_favicon_svg(out_path: Path) -> None:
    """Write a compact site favicon inspired by the Mayhem prismatic dice mark."""
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>
  <defs>
    <linearGradient id='bg' x1='32' y1='24' x2='224' y2='232' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#0d1122'/>
      <stop offset='0.55' stop-color='#090d1d'/>
      <stop offset='1' stop-color='#05070f'/>
    </linearGradient>
    <linearGradient id='sheen' x1='58' y1='62' x2='194' y2='192' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#fbf7ff'/>
      <stop offset='0.22' stop-color='#8ef2ff'/>
      <stop offset='0.48' stop-color='#f5b6ff'/>
      <stop offset='0.72' stop-color='#ffe8ad'/>
      <stop offset='1' stop-color='#7ddfff'/>
    </linearGradient>
    <linearGradient id='orbit' x1='30' y1='188' x2='228' y2='110' gradientUnits='userSpaceOnUse'>
      <stop offset='0' stop-color='#f180ff'/>
      <stop offset='0.45' stop-color='#fff7ef'/>
      <stop offset='1' stop-color='#9f78ff'/>
    </linearGradient>
    <filter id='softGlow' x='-40%' y='-40%' width='180%' height='180%'>
      <feGaussianBlur stdDeviation='4' result='blur'/>
      <feMerge>
        <feMergeNode in='blur'/>
        <feMergeNode in='SourceGraphic'/>
      </feMerge>
    </filter>
  </defs>
  <rect x='8' y='8' width='240' height='240' rx='34' fill='url(#bg)'/>
  <rect x='8' y='8' width='240' height='240' rx='34' fill='none' stroke='rgba(255,255,255,0.18)' stroke-width='3'/>
  <g filter='url(#softGlow)' stroke='url(#sheen)' stroke-width='3.5' stroke-linejoin='round'>
    <path d='M128 56 69 94l59 34 59-34-59-38Z' fill='rgba(255,248,255,0.88)'/>
    <path d='M69 94v69l59 35v-70L69 94Z' fill='rgba(232,220,255,0.78)'/>
    <path d='M187 94v69l-59 35v-70l59-34Z' fill='rgba(244,205,255,0.8)'/>
  </g>
  <g fill='#090d1d'>
    <ellipse cx='128' cy='101' rx='11' ry='8'/>
    <ellipse cx='91' cy='122' rx='10' ry='14' transform='rotate(-24 91 122)'/>
    <ellipse cx='108' cy='164' rx='10' ry='14' transform='rotate(-24 108 164)'/>
    <ellipse cx='153' cy='142' rx='10' ry='14' transform='rotate(24 153 142)'/>
    <ellipse cx='171' cy='122' rx='10' ry='14' transform='rotate(24 171 122)'/>
  </g>
  <path d='M31 181c26 21 59 29 95 27 38-2 72-15 101-49' fill='none' stroke='#05070f' stroke-width='18' stroke-linecap='round'/>
  <path d='M27 177c26 21 59 29 95 27 38-2 72-15 101-49' fill='none' stroke='url(#orbit)' stroke-width='11' stroke-linecap='round' filter='url(#softGlow)'/>
  <path d='M191 64l5 14 14 5-14 5-5 14-5-14-14-5 14-5 5-14Z' fill='#fff3d5' filter='url(#softGlow)'/>
</svg>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")

def favicon_asset_version() -> str:
    """Use icon-source or generator mtime so browser cache updates on asset tweaks."""
    candidates = [Path(__file__)]
    if SITE_ICON_SOURCE.exists():
        candidates.append(SITE_ICON_SOURCE)
    existing = [path for path in candidates if path.exists()]
    if existing:
        latest = max(path.stat().st_mtime for path in existing)
        stamp = _dt.datetime.fromtimestamp(latest)
        return stamp.strftime("%Y%m%d%H%M%S")
    return (_dt.date.today().isoformat()).replace("-", "")

def write_favicon_assets(out_dir: Path, source_path: Path = SITE_ICON_SOURCE) -> list[Path]:
    """Generate favicon PNG/ICO assets by directly downscaling the checked-in icon."""
    from PIL import Image, ImageChops, ImageDraw

    if not source_path.exists():
        return []

    img_master = Image.open(source_path).convert("RGBA")
    source_has_alpha = img_master.getchannel("A").getextrema()[0] < 255

    def _resized(img_rgba: "Image.Image", size: tuple[int, int]) -> "Image.Image":
        resized = img_rgba.resize(size, Image.LANCZOS)
        if source_has_alpha:
            return resized
        radius = max(4, round(min(size) * 0.22))
        mask = Image.new("L", size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
        alpha = resized.getchannel("A")
        resized.putalpha(ImageChops.multiply(alpha, mask))
        return resized

    out_dir.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    raster_targets = {
        "mayhem-single-die-icon.png": (180, 180),
        "mayhem-tab-icon.png": (180, 180),
        "favicon-32.png": (32, 32),
        "apple-touch-icon.png": (180, 180),
    }
    for name, size in raster_targets.items():
        target = out_dir / name
        resized = _resized(img_master, size)
        resized.save(target, "PNG", optimize=True)
        outputs.append(target)

    ico_path = out_dir / "favicon.ico"
    ico_master = _resized(img_master, (256, 256))
    ico_master.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    outputs.append(ico_path)
    return outputs

def assign_tier(bayes_wr: float) -> str:
    if bayes_wr >= 0.55:
        return "OP"
    if bayes_wr >= 0.52:
        return "T1"
    if bayes_wr >= 0.50:
        return "T2"
    if bayes_wr >= 0.48:
        return "T3"
    if bayes_wr >= 0.46:
        return "T4"
    return "T5"

# Role definitions live in scripts/champion_roles.py so every generated page
# and public role spec shares one site-wide source of truth.
ROLE_SCORE_CLOSE_GAP = 0.012
ROLE_MIN_PICK_RATE = 0.06
SECONDARY_ROLE_MIN_PICK_RATE = 0.08
ROLE_PICK_LIFT_WEIGHT = AUGMENT_PICK_LIFT_WEIGHT
ROLE_PICK_LIFT_CAP = AUGMENT_PICK_LIFT_CAP
CHAMPION_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "Renata": {"name_zh": "睿娜妲", "name_en": "Renata"},
}

def load_champion_metadata(version: str | None) -> tuple[str, dict[int, dict]]:
    if version is None:
        r = httpx.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=15)
        r.raise_for_status()
        version = r.json()[0]
    url_zh = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_TW/champion.json"
    r_zh = httpx.get(url_zh, timeout=30)
    url_en = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    r_en = httpx.get(url_en, timeout=30)
    if r_zh.status_code != 200 and r_en.status_code != 200:
        r_zh.raise_for_status()
        r_en.raise_for_status()
    raw_zh = r_zh.json()["data"] if r_zh.status_code == 200 else {}
    raw_en = r_en.json()["data"] if r_en.status_code == 200 else {}
    by_id: dict[int, dict] = {}
    applied: list[tuple[str, list[str], list[str]]] = []
    source = raw_en or raw_zh
    for alias, base_entry in source.items():
        entry_en = raw_en.get(alias, base_entry)
        entry_zh = raw_zh.get(alias, base_entry)
        tags = entry_en.get("tags") or entry_zh.get("tags") or []
        original_tags = list(tags)
        tags = role_tags_for_alias(alias, tags)
        primary_role = tags[0] if tags else ""
        if tags != original_tags:
            applied.append((alias, original_tags, tags))
        name_zh = entry_zh.get("name") or entry_en.get("name") or alias
        name_en = entry_en.get("name") or alias
        if alias in CHAMPION_NAME_OVERRIDES:
            name_override = CHAMPION_NAME_OVERRIDES[alias]
            name_zh = name_override.get("name_zh", name_zh)
            name_en = name_override.get("name_en", name_en)
        by_id[int(base_entry["key"])] = {
            "name": name_zh,
            "name_zh": name_zh,
            "name_en": name_en,
            "alias": alias,
            "primary_role": primary_role,
            "tags": tags,
            "original_tags": original_tags,
            "attack_range": int((entry_en.get("stats") or entry_zh.get("stats") or {}).get("attackrange") or 0),
            "image": f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alias}.png",
        }
    if applied:
        click.echo(f"[tierlist] applied {len(applied)} fixed Mayhem primary roles (DDragon -> site role):")
        for alias, before, after in applied:
            click.echo(f"  {alias:14s} {before} -> {after}")
    return version, by_id


def write_role_definitions_json(
    out_path: Path,
    *,
    champ_meta: dict[int, dict] | None = None,
    data_dragon_version: str | None = None,
    patch_prefix: str | None = None,
) -> None:
    payload = role_definitions_payload()
    payload["generated_at"] = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload["data_dragon_version"] = data_dragon_version
    payload["patch_prefix"] = patch_prefix
    if champ_meta:
        current_roles: dict[str, dict[str, object]] = {}
        secondary_roles: dict[str, dict[str, object]] = {}
        for meta in champ_meta.values():
            alias = str(meta.get("alias") or "")
            if not alias:
                continue
            tags = list(meta.get("tags") or [])
            primary = str(tags[0]) if tags else ""
            secondary = str(tags[1]) if len(tags) > 1 else ""
            role_meta = meta.get("role_meta") or {}
            current_roles[alias] = {
                "primary": primary,
                "secondary": secondary,
                "tags": tags,
            }
            if secondary:
                secondary_roles[alias] = {
                    "role": secondary,
                    "meta": role_meta.get(secondary, {}),
                }
        payload["current_roles"] = dict(sorted(current_roles.items()))
        payload["secondary_roles"] = dict(sorted(secondary_roles.items()))
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

def _icon_url(lcu_path: str) -> str:
    """Convert an LCU asset path to a CommunityDragon URL."""
    stripped = lcu_path.replace("/lol-game-data/assets/", "", 1).lower()
    return f"{CDRAGON_BASE}/{stripped}"

def _cached_get_json(url: str, cache_path: Path, timeout: float = 60) -> dict | list:
    """Fetch JSON with on-disk caching (the kiwi.bin.json + stringtable are large)."""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(r.text, encoding="utf-8")
    return r.json()

# Strips Riot's inline markup so an augment description can be shown as plain
# text in a hover tooltip:
#   * `<speed>跑速</speed>`     -> `跑速`            (keep inner text)
#   * `<br>` / `<br />`         -> ` ` / newline
#   * `@MovespeedMod*100@%`     -> `[數值]`          (numeric placeholders)
#   * `%i:scaleCrit%`           -> ``                (inline UI icons)
_TAG_RE = re.compile(r"<[^>]+>")
_PLACEHOLDER_RE = re.compile(r"@[A-Za-z0-9_*+\-./]+@%?")
_ICON_REF_RE = re.compile(r"%i:[A-Za-z0-9_]+%")

def _clean_desc(text: str) -> str:
    if not text:
        return ""
    s = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    s = _PLACEHOLDER_RE.sub("[數值]", s)
    s = _ICON_REF_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def load_augment_descriptions(
    cache_dir: Path,
    *,
    locale: str,
    cache_name: str,
) -> dict[int, str]:
    """Resolve Mayhem augment descriptions via:

        kiwi.bin.json (AugmentPlatformId -> DescriptionTra)
        +  lol.stringtable.json (lowercase key -> localized text)

    Returns dict mapping augment ID (matches our DB) -> cleaned localized summary.
    """
    kiwi = _cached_get_json(
        "https://raw.communitydragon.org/latest/game/maps/modespecificdata/kiwi.bin.json",
        cache_dir / "kiwi.bin.json",
    )
    plat: dict[int, tuple[str | None, str | None]] = {}
    for entry in kiwi.values() if isinstance(kiwi, dict) else []:
        if not isinstance(entry, dict) or entry.get("__type") != "AugmentData":
            continue
        pid = entry.get("AugmentPlatformId")
        if pid is None:
            continue
        desc_key = (entry.get("DescriptionTra") or "").lower() or None
        tip_key = (entry.get("AugmentTooltipTra") or "").lower() or None
        plat[int(pid)] = (desc_key, tip_key)

    st = _cached_get_json(
        f"https://raw.communitydragon.org/latest/game/{locale}/data/menu/en_us/lol.stringtable.json",
        cache_dir / cache_name,
    )
    entries = st["entries"] if isinstance(st, dict) and "entries" in st else {}

    out: dict[int, str] = {}
    for pid, (desc_key, tip_key) in plat.items():
        # Prefer the *Summary (DescriptionTra) — it tends to be a short clean
        # blurb with no @placeholders.  Fall back to Tooltip if missing.
        raw = ""
        if desc_key and desc_key in entries:
            raw = entries[desc_key]
        if not raw and tip_key and tip_key in entries:
            raw = entries[tip_key]
        cleaned = _clean_desc(raw)
        if cleaned:
            out[pid] = cleaned
    return out

def load_augment_display_tags(cache_dir: Path) -> dict[int, list[int]]:
    kiwi = _cached_get_json(
        "https://raw.communitydragon.org/latest/game/maps/modespecificdata/kiwi.bin.json",
        cache_dir / "kiwi.bin.json",
    )
    out: dict[int, list[int]] = {}
    for entry in kiwi.values() if isinstance(kiwi, dict) else []:
        if not isinstance(entry, dict) or entry.get("__type") != "AugmentData":
            continue
        pid = entry.get("AugmentPlatformId")
        if pid is None:
            continue
        tags: list[int] = []
        for raw_tag in entry.get("AugmentDisplayTags") or []:
            try:
                tag = int(raw_tag)
            except (TypeError, ValueError):
                continue
            if tag in AUGMENT_DISPLAY_TAG_LABELS:
                tags.append(tag)
        out[int(pid)] = tags
    return out

# CommunityDragon `zh_tw` augment names don't always match Garena's live
# Traditional Chinese client.  Drop manual TW overrides here as users
# report mistranslations.  Key = augment ID (== `AugmentPlatformId`).
#
# Format: aid -> TW name as it actually appears in the game client.
AUGMENT_NAME_OVERRIDES: dict[int, str] = {
    # Internal: Kiwi_UltimateAwakening; icon ZeroHour_small.png.
    # CommunityDragon zh_tw: 「大絕覺醒」, Garena TW client ships 「最終型態」
    # (型 not 形 — Garena consistently picks 型態 over 形態 for "form" in
    # game context).  Verified against live client screenshot 2026-05-15.
    1349: "最終型態",
}

# Some tooltips contain spell-slot placeholders like "your @SpellName@ gains
# @Value@ ability haste".  Our generic cleaner intentionally collapses opaque
# numeric tokens to `[數值]`, but for Bread-and-* augments that also erases the
# Q/W/E slot and makes the tooltip misleading.  Override only the affected
# descriptions with the actual spell slot wording shown in-game.
AUGMENT_DESC_OVERRIDES: dict[int, str] = {
    1103: "你的第一個基礎技能（Q）獲得[數值]技能加速。",
    1150: "你的第二個基礎技能（W）獲得[數值]技能加速。",
    1151: "你的第三個基礎技能（E）獲得[數值]技能加速。",
}

def load_augment_metadata(cache_dir: Path | None = None) -> dict[int, dict]:
    display_tags_by_id = load_augment_display_tags(cache_dir or Path("data/cache"))
    # Try zh-TW first; fall back to default (English) if the field is empty.
    try:
        r_tw = httpx.get(f"{CDRAGON_BASE.replace('/default', '/zh_tw')}/v1/cherry-augments.json", timeout=20)
        r_tw.raise_for_status()
        tw_rows = r_tw.json()
    except Exception:
        tw_rows = []
    tw_by_id = {int(r["id"]): r for r in tw_rows if "id" in r}

    r = httpx.get(f"{CDRAGON_BASE}/v1/cherry-augments.json", timeout=20)
    r.raise_for_status()
    rows = r.json()

    by_id: dict[int, dict] = {}
    set_by_augment = _augment_set_lookup()
    name_overrides_applied: list[tuple[int, str, str]] = []
    for entry in rows:
        aug_id = entry.get("id")
        if aug_id is None:
            continue
        aug_id = int(aug_id)
        tw_entry = tw_by_id.get(aug_id, {})
        tw_name = tw_entry.get("nameTRA") or tw_entry.get("name")
        en_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA")
        name_zh = tw_name if tw_name and tw_name.strip() else en_name
        name_en = en_name or tw_name
        name = name_zh
        # Apply manual TW translation override if we have one.
        if aug_id in AUGMENT_NAME_OVERRIDES:
            override = AUGMENT_NAME_OVERRIDES[aug_id]
            if name != override:
                name_overrides_applied.append((aug_id, name or "?", override))
                name = override
                name_zh = override
        icon_path = (
            entry.get("augmentSmallIconPath")
            or entry.get("augmentLargeIconPath")
        )
        en_lookup_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA") or ""
        set_infos = set_by_augment.get(_normalize_augment_name(en_lookup_name), [])
        by_id[aug_id] = {
            "name": name or f"#{aug_id}",
            "name_zh": name_zh or name or f"#{aug_id}",
            "name_en": name_en or name or f"#{aug_id}",
            "icon": _icon_url(icon_path) if icon_path else "",
            "rarity": entry.get("rarity", ""),
            "desc": "",
            "desc_zh": "",
            "desc_en": "",
            "set": " / ".join(info["name"] for info in set_infos),
            "set_zh": " / ".join(info["name_zh"] for info in set_infos),
            "set_en": " / ".join(info["name_en"] for info in set_infos),
            "setSlug": " ".join(info["slug"] for info in set_infos),
            "sets": set_infos,
            "displayTags": display_tags_by_id.get(aug_id, []),
        }
    if name_overrides_applied:
        click.echo(
            f"[tierlist] applied {len(name_overrides_applied)} "
            "AUGMENT_NAME_OVERRIDES (CDragon zh_tw -> Garena TW):"
        )
        for aid, before, after in name_overrides_applied:
            click.echo(f"  {aid:5d}  {before}  ->  {after}")

    if cache_dir is not None:
        try:
            descs_zh = load_augment_descriptions(
                cache_dir,
                locale="zh_tw",
                cache_name="lol_stringtable_zh_tw.json",
            )
            for aid, txt in descs_zh.items():
                if aid in by_id:
                    by_id[aid]["desc"] = txt
                    by_id[aid]["desc_zh"] = txt
        except Exception as exc:
            click.echo(f"[tierlist] WARN: zh-TW augment description fetch failed: {exc}")
        try:
            descs_en = load_augment_descriptions(
                cache_dir,
                locale="en_us",
                cache_name="lol_stringtable_en_us.json",
            )
            for aid, txt in descs_en.items():
                if aid in by_id:
                    by_id[aid]["desc_en"] = txt
        except Exception as exc:
            click.echo(f"[tierlist] WARN: en-US augment description fetch failed: {exc}")

    for aid, txt in AUGMENT_DESC_OVERRIDES.items():
        if aid in by_id:
            by_id[aid]["desc"] = txt
            by_id[aid]["desc_zh"] = txt

    return by_id

def load_item_metadata(cache_dir: Path | None = None) -> dict[int, dict]:
    rows_default = _cached_get_json(
        f"{CDRAGON_BASE}/v1/items.json",
        (cache_dir or Path("data/cache")) / "cdragon_items_en_us.json",
    )
    rows_zh = _cached_get_json(
        f"{CDRAGON_BASE.replace('/default', '/zh_tw')}/v1/items.json",
        (cache_dir or Path("data/cache")) / "cdragon_items_zh_tw.json",
    )
    zh_by_id = {
        int(row["id"]): row
        for row in rows_zh
        if isinstance(row, dict) and row.get("id") is not None
    }
    out: dict[int, dict] = {}
    for row in rows_default:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        item_id = int(row["id"])
        zh_row = zh_by_id.get(item_id, {})
        icon_path = row.get("iconPath") or zh_row.get("iconPath") or ""
        price_raw = row.get("priceTotal")
        if isinstance(price_raw, dict):
            price_total = int(price_raw.get("amount") or 0)
        else:
            price_total = int(price_raw or 0)
        out[item_id] = {
            "id": item_id,
            "name": zh_row.get("name") or row.get("name") or f"#{item_id}",
            "name_zh": zh_row.get("name") or row.get("name") or f"#{item_id}",
            "name_en": row.get("name") or zh_row.get("name") or f"#{item_id}",
            "categories": list(row.get("categories") or zh_row.get("categories") or []),
            "price_total": price_total,
            "icon": _icon_url(icon_path) if icon_path else "",
        }
    return out

def compute_winrates(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    prior: float = 0.5,
    k: int = 200,
):
    """Compute champion winrates + per-(champion, augment) winrates.

    Returns: (champ_records, champ_aug_records, champ_pair_records)
      champ_records: list of dicts with champion_id, games, wins, raw_wr, bayes_wr
      champ_aug_records: list of dicts with champion_id, augment_id, games, wins,
                        raw_wr, smoothed_wr, lift (smoothed_wr - champ_baseline_wr)
      champ_pair_records: list of dicts with champion_id, teammate_id, games,
                        wins, expected_wr, lift, delta_vs_rest, z_score
    """
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_champs, red_champs, blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ?",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_champs, red_champs, blue_wins, participants_json FROM games "
            "WHERE queue_id=?",
            (queue_id,),
        )

    games: Counter[int] = Counter()
    wins: Counter[int] = Counter()
    ca_games: Counter[tuple[int, int]] = Counter()
    ca_wins: Counter[tuple[int, int]] = Counter()
    cp_games: Counter[tuple[int, int]] = Counter()
    cp_wins: Counter[tuple[int, int]] = Counter()

    try:
        for blue, red, bw, pj in rows:
            bw_bool = bool(bw)
            blue_team = json.loads(blue)
            red_team = json.loads(red)
            for team, team_won in ((blue_team, bw_bool), (red_team, not bw_bool)):
                for c in team:
                    games[c] += 1
                    if team_won:
                        wins[c] += 1
                # Ordered anchor -> teammate rows: recommendation is conditioned on
                # the already-picked champions, so we preserve "given anchor A,
                # how much does teammate B help?" rather than collapsing to an
                # undirected pair too early.
                for c in team:
                    for teammate in team:
                        if teammate == c:
                            continue
                        cp_games[(c, teammate)] += 1
                        if team_won:
                            cp_wins[(c, teammate)] += 1
            if not pj:
                continue
            for p in json.loads(pj):
                cid = int(p.get("championId", 0))
                if cid <= 0:
                    continue
                player_won = 1 if (int(p.get("teamId", 0)) == 100) == bw_bool else 0
                for a in p.get("augments") or []:
                    a = int(a)
                    if a <= 0:
                        continue
                    ca_games[(cid, a)] += 1
                    ca_wins[(cid, a)] += player_won
    finally:
        con.close()

    champ_records = []
    for cid, g in games.items():
        w = wins[cid]
        raw = w / g if g else 0.0
        bayes = (w + prior * k) / (g + k)
        champ_records.append({
            "champion_id": cid,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "bayes_wr": bayes,
        })
    champ_records.sort(key=lambda d: -d["bayes_wr"])

    # Per-pair smoothing uses *that champion's* baseline winrate as the prior.
    # This way the comparison is "does this augment lift the champ above its
    # own baseline?", which is what we actually want for best/worst-fit picks.
    raw_wr_by_champ = {cid: (wins[cid] / games[cid]) if games[cid] else 0.5 for cid in games}
    pair_k = 20
    champ_aug_records = []
    for (cid, aid), g in ca_games.items():
        w = ca_wins[(cid, aid)]
        raw = w / g if g else 0.0
        baseline = raw_wr_by_champ.get(cid, 0.5)
        smoothed = (w + baseline * pair_k) / (g + pair_k)
        champ_aug_records.append({
            "champion_id": cid,
            "augment_id": aid,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": smoothed - baseline,
        })

    # Same-team pair synergy is a residual over each champion's marginal
    # strength.  This avoids recommending "T2+ good-stuff piles" as synergy:
    # the pair has to beat the winrate expected from anchor + teammate strength.
    team_rows = sum(games.values())
    global_wr = (sum(wins.values()) / team_rows) if team_rows else 0.5
    eps = 1e-4

    def _logit(p: float) -> float:
        p = min(max(p, eps), 1.0 - eps)
        return math.log(p / (1.0 - p))

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    champ_pair_records = []
    for (cid, teammate_id), g in cp_games.items():
        w = cp_wins[(cid, teammate_id)]
        raw = w / g if g else 0.0
        anchor_wr = raw_wr_by_champ.get(cid, global_wr)
        teammate_wr = raw_wr_by_champ.get(teammate_id, global_wr)
        expected_wr = _sigmoid(_logit(anchor_wr) + _logit(teammate_wr) - _logit(global_wr))

        rest_games = games[cid] - g
        rest_wins = wins[cid] - w
        delta_vs_expected = raw - expected_wr
        var_pair = raw * (1 - raw) / max(g, 1)
        var_anchor = anchor_wr * (1 - anchor_wr) / max(games[cid], 1)
        var_teammate = teammate_wr * (1 - teammate_wr) / max(games[teammate_id], 1)
        se = (var_pair + var_anchor + var_teammate) ** 0.5
        z_score = (delta_vs_expected / se) if se > 0 else 0.0

        if rest_games > 0:
            rest_wr = rest_wins / rest_games
            delta_vs_rest = raw - rest_wr
        else:
            rest_wr = anchor_wr
            delta_vs_rest = raw - rest_wr

        champ_pair_records.append({
            "champion_id": cid,
            "teammate_id": teammate_id,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "expected_wr": expected_wr,
            "baseline_wr": anchor_wr,
            "teammate_wr": teammate_wr,
            "rest_wr": rest_wr,
            "lift": delta_vs_expected,
            "delta_vs_rest": delta_vs_rest,
            "z_score": z_score,
        })

    return champ_records, champ_aug_records, champ_pair_records

RARITY_ORDER = ["kPrismatic", "kGold", "kSilver"]

def estimate_augment_prior_strength(champ_aug: list[dict]) -> float:
    """Estimate beta-binomial prior strength for champ x augment WRs.

    Each pair is centered on that champion's baseline winrate.  The fitted
    concentration controls how aggressively low-sample pairs shrink back to the
    champion baseline, and avoids a hand-picked `games / (games + k)` scale.
    """
    rows: list[tuple[int, int, float]] = []
    for row in champ_aug:
        games = int(row.get("games", 0))
        wins = int(row.get("wins", 0))
        baseline = float(row.get("baseline_wr", 0.5))
        if games <= 0:
            continue
        rows.append((wins, games, min(max(baseline, 1e-4), 1.0 - 1e-4)))

    if len(rows) < 20:
        return AUGMENT_PRIOR_DEFAULT

    if minimize_scalar is not None and betaln is not None:
        def nll(log_k: float) -> float:
            k = math.exp(log_k)
            loss = 0.0
            for wins, games, baseline in rows:
                alpha = baseline * k
                beta = (1.0 - baseline) * k
                # The combinatorial term is constant in k, so it is omitted.
                loss -= float(betaln(wins + alpha, games - wins + beta) - betaln(alpha, beta))
            return loss

        try:
            result = minimize_scalar(
                nll,
                bounds=(math.log(5.0), math.log(5000.0)),
                method="bounded",
                options={"xatol": 1e-3},
            )
            if result.success:
                return max(5.0, min(5000.0, math.exp(float(result.x))))
        except Exception:
            pass

    # Fallback: moment estimate from over-dispersion beyond binomial noise.
    rhos: list[float] = []
    for wins, games, baseline in rows:
        observed = wins / games
        denom = max(baseline * (1.0 - baseline), 1e-6)
        extra_var = max(0.0, (observed - baseline) ** 2 - denom / games)
        if extra_var > 0:
            rhos.append(extra_var / denom)
    if not rhos:
        return AUGMENT_PRIOR_DEFAULT
    rhos.sort()
    rho = rhos[len(rhos) // 2]
    if rho <= 0:
        return AUGMENT_PRIOR_DEFAULT
    return max(5.0, min(5000.0, (1.0 / rho) - 1.0))

def beta_posterior_quantile(q: float, alpha: float, beta: float) -> float:
    if betaincinv is not None:
        try:
            return float(betaincinv(alpha, beta, q))
        except Exception:
            pass
    mean = alpha / (alpha + beta)
    var = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
    direction = -1.0 if q <= 0.5 else 1.0
    return min(max(mean + direction * AUGMENT_LCB_Z * math.sqrt(max(var, 0.0)), 0.0), 1.0)

def posterior_wr_summary(wins: int, games: int, baseline: float, prior_strength: float) -> tuple[float, float]:
    baseline = min(max(baseline, 1e-4), 1.0 - 1e-4)
    alpha = baseline * prior_strength + wins
    beta = (1.0 - baseline) * prior_strength + games - wins
    mean = alpha / (alpha + beta)
    lower = beta_posterior_quantile(AUGMENT_POSTERIOR_Q, alpha, beta)
    return mean, lower

def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _damage_bucket(row: dict[str, str] | None, role: str) -> str:
    if row:
        physical = _safe_float(row.get("empirical_physical_damage_ratio"))
        magic = _safe_float(row.get("empirical_magic_damage_ratio"))
        if physical >= 0.55 and physical >= magic + 0.15:
            return "physical"
        if magic >= 0.55 and magic >= physical + 0.15:
            return "magic"
    if role in {"Marksman", "Fighter", "Assassin"}:
        return "physical"
    if role in {"Mage", "Support"}:
        return "magic"
    return "mixed"

def load_champion_pick_profiles(
    champ_meta: dict[int, dict],
    scores_path: Path = EMPIRICAL_CHAMPION_SCORES,
) -> dict[int, dict[str, object]]:
    score_rows: dict[int, dict[str, str]] = {}
    source_path = scores_path if scores_path.exists() else SEMANTIC_CHAMPION_SCORES
    if source_path.exists():
        with source_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    score_rows[int(row["champion_id"])] = row
                except (KeyError, TypeError, ValueError):
                    continue

    profiles: dict[int, dict[str, object]] = {}
    for cid, meta in champ_meta.items():
        tags = list(meta.get("tags") or [])
        role = tags[0] if tags else "Unknown"
        row = score_rows.get(int(cid))
        damage_per_min = _safe_float(row.get("empirical_damage_per_min")) if row else 0.0
        physical_ratio = _safe_float(row.get("empirical_physical_damage_ratio")) if row else 0.0
        magic_ratio = _safe_float(row.get("empirical_magic_damage_ratio")) if row else 0.0
        true_ratio = _safe_float(row.get("empirical_true_damage_ratio")) if row else 0.0
        physical_dpm = damage_per_min * physical_ratio
        magic_dpm = damage_per_min * magic_ratio
        true_dpm = damage_per_min * true_ratio
        damage_bucket = _damage_bucket(row, role)
        if physical_dpm + magic_dpm <= 0:
            if damage_bucket == "physical":
                physical_dpm = 1.0
            elif damage_bucket == "magic":
                magic_dpm = 1.0
            else:
                physical_dpm = magic_dpm = 0.5
        profiles[int(cid)] = {
            "role": role,
            "damage": damage_bucket,
            "physical_dpm": physical_dpm,
            "magic_dpm": magic_dpm,
            "true_dpm": true_dpm,
            "wave": _safe_float(row.get("wave_clear_score")) if row else 0.0,
            "cc": _safe_float(row.get("cc_score")) if row else 0.0,
            "engage": _safe_float(row.get("engage_score")) if row else 0.0,
            "damage_score": _safe_float(row.get("damage_score")) if row else 0.0,
            "poke": _safe_float(row.get("poke_score")) if row else 0.0,
            "sustain": _safe_float(row.get("sustain_score")) if row else 0.0,
            "front": _safe_float(row.get("frontline_score")) if row else 0.0,
        }
    return profiles

_DAMAGE_PROFILE_KEYWORDS = (
    "ability power",
    "adaptive force",
    "attack damage",
    "attack speed",
    "basic attack",
    "basic attacks",
    "critical",
    "crit",
    "magic damage",
    "magic penetration",
    "on-hit",
    "physical damage",
    "armor penetration",
    "lethality",
    "spell damage",
    "true damage",
    "convert",
)

def augment_peer_scope(meta: dict | None) -> str:
    if not meta:
        return "role"
    text = " ".join(
        str(meta.get(key) or "")
        for key in ("name", "name_en", "desc", "desc_en", "set", "set_en")
    ).lower()
    if any(keyword in text for keyword in _DAMAGE_PROFILE_KEYWORDS):
        return "role_damage"
    return "role"

def _profile_group(cid: int, profiles: dict[int, dict[str, object]], scope: str) -> str:
    profile = profiles.get(cid, {})
    role = profile.get("role") or "Unknown"
    if scope == "role_damage":
        return f"{role}|{profile.get('damage') or 'mixed'}"
    return role

def build_pick_lift_index(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    profiles: dict[int, dict[str, object]],
) -> dict[tuple[int, int], dict[str, float | str]]:
    champ_rarity_totals: Counter[tuple[int, str]] = Counter()
    global_totals: Counter[str] = Counter()
    global_counts: Counter[tuple[str, int]] = Counter()
    group_totals: Counter[tuple[str, str, str]] = Counter()
    group_counts: Counter[tuple[str, str, str, int]] = Counter()
    rarity_aug_ids: dict[str, set[int]] = defaultdict(set)

    for row in champ_aug:
        aid = int(row["augment_id"])
        cid = int(row["champion_id"])
        games = int(row["games"])
        meta = aug_meta.get(aid)
        rarity = str(meta.get("rarity") or "") if meta else ""
        if not rarity:
            continue
        rarity_aug_ids[rarity].add(aid)
        champ_rarity_totals[(cid, rarity)] += games
        global_totals[rarity] += games
        global_counts[(rarity, aid)] += games
        for scope in ("role", "role_damage"):
            group = _profile_group(cid, profiles, scope)
            group_totals[(scope, group, rarity)] += games
            group_counts[(scope, group, rarity, aid)] += games

    out: dict[tuple[int, int], dict[str, float | str]] = {}
    for row in champ_aug:
        aid = int(row["augment_id"])
        cid = int(row["champion_id"])
        games = int(row["games"])
        meta = aug_meta.get(aid)
        rarity = str(meta.get("rarity") or "") if meta else ""
        if not rarity or global_totals[rarity] <= 0:
            continue
        scope = augment_peer_scope(meta)
        group = _profile_group(cid, profiles, scope)
        group_key = (scope, group, rarity)
        champ_total = champ_rarity_totals[(cid, rarity)]
        peer_total = group_totals[group_key] - champ_total
        peer_count = group_counts[(scope, group, rarity, aid)] - games

        # If role+damage is too thin after leave-one-out, fall back to role-only
        # before falling all the way back to the same-rarity global baseline.
        min_peer_total = max(50.0, 2.0 * len(rarity_aug_ids[rarity]))
        if scope == "role_damage" and peer_total < min_peer_total:
            scope = "role"
            group = _profile_group(cid, profiles, scope)
            group_key = (scope, group, rarity)
            peer_total = group_totals[group_key] - champ_total
            peer_count = group_counts[(scope, group, rarity, aid)] - games

        m = max(1, len(rarity_aug_ids[rarity]))
        global_rate = (global_counts[(rarity, aid)] + 0.5) / (global_totals[rarity] + 0.5 * m)
        if peer_total > 0:
            peer_rate = (peer_count + 0.5 * m * global_rate) / (peer_total + 0.5 * m)
        else:
            peer_rate = global_rate
            group = "global"
        champ_rate = (games + 0.5 * m * peer_rate) / (champ_total + 0.5 * m) if champ_total > 0 else peer_rate
        pick_lift = math.log(max(champ_rate, 1e-9) / max(peer_rate, 1e-9))
        out[(cid, aid)] = {
            "pick_rate": champ_rate,
            "peer_pick_rate": peer_rate,
            "pick_lift": pick_lift,
            "peer_scope": scope,
            "peer_group": group,
        }
    return out

def _label_entry(labels: dict[str, dict[str, str]], slug: str) -> dict[str, str]:
    info = labels.get(slug, {})
    name_en = info.get("en") or slug
    name_zh = info.get("zh") or name_en
    return {
        "name": name_zh,
        "name_zh": name_zh,
        "name_en": name_en,
        "slug": slug,
    }

def item_style_infos(item: dict | None) -> list[dict[str, str]]:
    if not item:
        return []
    if int(item.get("price_total") or 0) < ITEM_MIN_TOTAL_GOLD:
        return []
    if int(item.get("id") or 0) in HEARTSTEEL_ITEM_IDS:
        return [_label_entry(ITEM_STYLE_LABELS, "heartsteel")]
    categories = set(str(c) for c in item.get("categories") or [])
    name = f"{item.get('name_en', '')} {item.get('name', '')}".lower()
    is_spell_item = "SpellDamage" in categories or "ability power" in name
    is_support = (
        "HealAndShieldPower" in categories
        or any(word in name for word in SUPPORT_ITEM_KEYWORDS)
    )
    # Use one primary style per completed item.  Multi-tag CDragon items such as
    # crit+AP Mayhem items otherwise make marksmen look like AP builders just
    # because their best crit item also carries spell-damage tags.
    if is_support:
        slug = "support"
    elif "CriticalStrike" in categories:
        slug = "crit"
    elif is_spell_item:
        if any(word in name for word in AP_ONHIT_ITEM_KEYWORDS) or (
            {"OnHit", "AttackSpeed"} & categories and "Damage" not in categories
        ):
            slug = "ap_onhit"
        elif any(word in name for word in AP_BURN_ITEM_KEYWORDS):
            slug = "ap_burn"
        elif any(word in name for word in AP_BRUISER_ITEM_KEYWORDS):
            slug = "ap_bruiser"
        elif any(word in name for word in AP_BURST_ITEM_KEYWORDS):
            slug = "ap_burst"
        elif {"Health", "Armor", "SpellBlock", "MagicResist"} & categories and "MagicPenetration" not in categories:
            slug = "ap_bruiser"
        else:
            slug = "ap_burst"
    elif {"Damage", "ArmorPenetration", "Lethality"} & categories and (
        "manamune" in name or "muramana" in name
    ):
        slug = "ad_poke"
    elif {"OnHit", "AttackSpeed"} & categories:
        slug = "onhit"
    elif {"Damage", "ArmorPenetration", "Lethality"} & categories:
        if any(word in name for word in AD_POKE_ITEM_KEYWORDS):
            slug = "ad_poke"
        elif any(word in name for word in AD_ASSASSIN_ITEM_KEYWORDS):
            slug = "ad_assassin"
        elif any(word in name for word in AD_BRUISER_ITEM_KEYWORDS):
            slug = "ad_bruiser"
        elif {"Health", "Armor", "SpellBlock", "MagicResist", "LifeSteal", "SpellVamp", "Tenacity"} & categories:
            slug = "ad_bruiser"
        elif {"ArmorPenetration", "Lethality"} & categories and {"Active", "NonbootsMovement", "Slow", "Stealth"} & categories:
            slug = "ad_assassin"
        elif {"ArmorPenetration", "Lethality"} & categories and {"AbilityHaste", "CooldownReduction", "Mana"} & categories:
            slug = "ad_poke"
        elif {"ArmorPenetration", "Lethality"} & categories:
            slug = "ad_assassin"
        else:
            slug = "ad_bruiser"
    elif {"Health", "Armor", "SpellBlock", "MagicResist"} & categories:
        slug = "tank"
    else:
        return []
    return [_label_entry(ITEM_STYLE_LABELS, slug)]

def _dominant_style_slug(style_weights: Counter[str]) -> str | None:
    if not style_weights:
        return None
    return sorted(style_weights, key=lambda slug: (-style_weights[slug], slug))[0]

def _heartsteel_followup_slug(non_heartsteel_weights: Counter[str]) -> str | None:
    if not non_heartsteel_weights:
        return None
    tank_weight = sum(
        non_heartsteel_weights.get(slug, 0)
        for slug in HEARTSTEEL_TANK_FOLLOWUP_STYLES
    )
    bruiser_weight = sum(
        non_heartsteel_weights.get(slug, 0)
        for slug in HEARTSTEEL_BRUISER_FOLLOWUP_STYLES
    )
    if tank_weight >= max(bruiser_weight, ITEM_MIN_TOTAL_GOLD):
        return "tank"
    leader = _dominant_style_slug(non_heartsteel_weights)
    if not leader:
        return None
    if leader == "ap_burn":
        return "ap_bruiser"
    if leader in HEARTSTEEL_BRUISER_FOLLOWUP_STYLES:
        return leader
    return None

def _participant_item_infos(item_ids: list[int], item_meta: dict[int, dict]) -> list[dict[str, str]]:
    item_style_weights: Counter[str] = Counter()
    item_style_by_slug: dict[str, dict[str, str]] = {}
    for item_id in item_ids:
        item = item_meta.get(int(item_id))
        for info in item_style_infos(item):
            slug = str(info.get("slug") or "")
            if not slug:
                continue
            item_style_weights[slug] += max(int((item or {}).get("price_total") or 0), 1)
            item_style_by_slug[slug] = info
    if not item_style_weights:
        return []

    non_heartsteel_weights = Counter({
        slug: weight
        for slug, weight in item_style_weights.items()
        if slug != "heartsteel"
    })
    primary_slug = _dominant_style_slug(item_style_weights)
    if primary_slug is None:
        return []
    heartsteel_followup = _heartsteel_followup_slug(non_heartsteel_weights)
    if primary_slug == "heartsteel" and non_heartsteel_weights:
        primary_slug = heartsteel_followup or _dominant_style_slug(non_heartsteel_weights) or primary_slug

    item_infos: list[dict[str, str]] = []
    primary_info = item_style_by_slug.get(primary_slug)
    if primary_info:
        item_infos.append(primary_info)
    if (
        "heartsteel" in item_style_by_slug
        and heartsteel_followup == "tank"
        and primary_slug != "heartsteel"
    ):
        item_infos.append(item_style_by_slug["heartsteel"])
    return item_infos

def _is_recommendable_core_item(item: dict | None) -> bool:
    if not item:
        return False
    if int(item.get("id") or 0) in AUGMENT_GATED_ITEM_IDS:
        return False
    if int(item.get("price_total") or 0) < ITEM_MIN_TOTAL_GOLD:
        return False
    categories = set(str(c) for c in item.get("categories") or [])
    return "Boots" not in categories

def _is_guardian_starter_item(item: dict | None) -> bool:
    if not item:
        return False
    if int(item.get("id") or 0) not in GUARDIAN_STARTER_ITEM_IDS:
        return False
    if int(item.get("price_total") or 0) != 950:
        return False
    categories = set(str(c) for c in item.get("categories") or [])
    return "Boots" not in categories

def _is_antiheal_component(item: dict | None) -> bool:
    if not item:
        return False
    if int(item.get("id") or 0) in AUGMENT_GATED_ITEM_IDS:
        return False
    if int(item.get("price_total") or 0) >= ITEM_MIN_TOTAL_GOLD:
        return False
    name_en = str(item.get("name_en") or item.get("name") or "").strip().lower()
    return any(keyword in name_en for keyword in ANTIHEAL_COMPONENT_NAME_KEYWORDS)


def _is_antiheal_item_name(name: str | None) -> bool:
    value = str(name or "").strip().lower()
    return any(keyword in value for keyword in ANTIHEAL_ITEM_NAME_KEYWORDS)

def _is_recommendable_single_item(item: dict | None) -> bool:
    return (
        _is_recommendable_core_item(item)
        or _is_guardian_starter_item(item)
        or _is_antiheal_component(item)
    )

def _is_boot_item(item: dict | None) -> bool:
    if not item:
        return False
    categories = set(str(c) for c in item.get("categories") or [])
    return "Boots" in categories

def _is_recommendable_route_item(item: dict | None) -> bool:
    if not item:
        return False
    if int(item.get("id") or 0) in AUGMENT_GATED_ITEM_IDS:
        return False
    if _is_boot_item(item):
        return int(item.get("price_total") or 0) >= ITEM_BOOT_MIN_TOTAL_GOLD
    return _is_recommendable_core_item(item)

def _item_pair_payload(item_ids: list[int], item_meta: dict[int, dict]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for item_id in item_ids:
        item = item_meta.get(int(item_id))
        if not item:
            continue
        payload.append({
            "id": int(item_id),
            "name": str(item.get("name") or f"#{item_id}"),
            "name_zh": str(item.get("name_zh") or item.get("name") or f"#{item_id}"),
            "name_en": str(item.get("name_en") or item.get("name") or f"#{item_id}"),
            "icon": str(item.get("icon") or ""),
        })
    return payload

def _participant_core_item_ids(item_ids: list[int], item_meta: dict[int, dict]) -> list[int]:
    core_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in item_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        if not _is_recommendable_core_item(item_meta.get(item_id)):
            continue
        core_ids.append(item_id)
        seen.add(item_id)
        if len(core_ids) >= 2:
            break
    return core_ids

def _participant_recommendable_item_ids(item_ids: list[int], item_meta: dict[int, dict]) -> list[int]:
    core_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in item_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        if not _is_recommendable_single_item(item_meta.get(item_id)):
            continue
        core_ids.append(item_id)
        seen.add(item_id)
    return core_ids

def _participant_route_item_ids(item_ids: list[int], item_meta: dict[int, dict]) -> list[int]:
    route_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in item_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        if not _is_recommendable_route_item(item_meta.get(item_id)):
            continue
        route_ids.append(item_id)
        seen.add(item_id)
    return route_ids

def _participant_boot_item_ids(item_ids: list[int], item_meta: dict[int, dict]) -> list[int]:
    boot_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in item_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        item = item_meta.get(item_id)
        if not _is_recommendable_route_item(item) or not _is_boot_item(item):
            continue
        boot_ids.append(item_id)
        seen.add(item_id)
    return boot_ids

def _item_pair_name(item_payload: list[dict[str, object]], key: str) -> str:
    return " + ".join(str(item.get(key) or item.get("name") or item.get("id")) for item in item_payload)

def _item_pair_slug(item_ids: list[int], *, ordered: bool) -> str:
    ids = item_ids if ordered else sorted(item_ids)
    return "+".join(str(item_id) for item_id in ids)

_AUGMENT_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "damage": (
        "damage", "burn", "missile", "fire", "lightning", "execute", "explosion",
        "goldrend", "boomerang", "blade", "laser", "bomb",
    ),
    "spell": (
        "ability power", "spell", "magic damage", "mana", "ultimate", "cooldown",
        "ability haste", "phenomenal evil", "mind to matter", "bread and",
    ),
    "attack": (
        "attack damage", "basic attack", "basic attacks", "attack speed", "on-hit",
        "physical damage", "fan the hammer", "light 'em up", "typhoon",
    ),
    "crit": ("critical", "crit", "jeweled", "infinity"),
    "tank": (
        "health", "armor", "magic resist", "damage reduction", "shield", "steel your heart",
        "immolate", "goliath", "perseverance",
    ),
    "sustain": (
        "heal", "healing", "shield", "omnivamp", "lifesteal", "first-aid",
        "windspeaker", "mikael", "all for you", "critical healing",
    ),
    "mobility": (
        "dash", "blink", "movement speed", "move speed", "speed", "haste",
        "transit", "dive bomber", "clown college",
    ),
    "snowball": ("snowball", "snowday", "pinball"),
    "economy": (
        "gold", "transmute", "pandora", "donation", "red envelope", "collector",
        "stats!", "make it rain",
    ),
    "stacking": (
        "stack", "quest", "infinite", "duality", "phenomenal", "hubris",
        "slap around", "soul eater", "tap dancer", "shrink engine",
    ),
    "utility": (
        "slow", "stun", "root", "crowd control", "ally", "allies", "intervention",
        "sonata", "polymorph", "buff buddies", "ocean soul",
    ),
    "auto": (
        "automated", "fully automated", "firefox", "frost wraith", "quantum",
        "self destruct", "prom queen", "ok boomerang",
    ),
}

_SET_TO_AUGMENT_TYPES = {
    "archmage": {"spell", "utility"},
    "dive-bomb": {"mobility", "damage"},
    "firecracker": {"damage", "attack", "crit"},
    "fully-automated": {"auto", "damage"},
    "high-roller": {"economy"},
    "make-it-rain": {"economy", "damage"},
    "snowday": {"snowball", "mobility"},
    "stackosaurus-rex": {"stacking", "tank"},
    "wee-woo-wee-woo": {"sustain", "utility"},
}

_DISPLAY_TAG_TO_AUGMENT_TYPES = {
    0: {"official_ally"},
    # Do not add official damage/support for every matching augment: those
    # buckets are intentionally broad and would drown out AP/AD/crit/snowball
    # style chips. More specific official tags still add useful signal.
    2: {"official_general"},
    3: {"official_tenacity"},
    4: {"official_speed"},
    7: {"economy"},
}

_OFFICIAL_AUGMENT_TYPE_LABELS = {
    "official_ally": {"zh": "隊友", "en": "Ally"},
    "official_general": {"zh": "一般 / 質變", "en": "General / Transmute"},
    "official_tenacity": {"zh": "韌性", "en": "Tenacity"},
    "official_speed": {"zh": "速度", "en": "Speed"},
}

def augment_type_infos(meta: dict | None) -> list[dict[str, str]]:
    if not meta:
        return []
    text = " ".join(
        str(meta.get(key) or "")
        for key in ("name", "name_en", "desc", "desc_en", "set", "set_en", "setSlug")
    ).lower()
    slugs: set[str] = set()
    for info in meta.get("sets") or []:
        slugs.update(_SET_TO_AUGMENT_TYPES.get(str(info.get("slug") or ""), set()))
    for slug, keywords in _AUGMENT_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            slugs.add(slug)
    for tag in meta.get("displayTags") or []:
        try:
            slugs.update(_DISPLAY_TAG_TO_AUGMENT_TYPES.get(int(tag), set()))
        except (TypeError, ValueError):
            continue
    labels = {**AUGMENT_TYPE_LABELS, **_OFFICIAL_AUGMENT_TYPE_LABELS}
    return [_label_entry(labels, slug) for slug in sorted(slugs)]

def estimate_category_prior_strength(rows: list[dict]) -> float:
    usable = [
        (
            int(row["wins"]),
            int(row["games"]),
            min(max(float(row["prior_wr"]), 1e-4), 1.0 - 1e-4),
        )
        for row in rows
        if int(row.get("games", 0)) > 0
    ]
    if len(usable) < 8 or minimize_scalar is None or betaln is None:
        return CATEGORY_PRIOR_DEFAULT

    def nll(log_k: float) -> float:
        k = math.exp(log_k)
        loss = 0.0
        for wins, games, prior_wr in usable:
            alpha = prior_wr * k
            beta = (1.0 - prior_wr) * k
            loss -= float(betaln(wins + alpha, games - wins + beta) - betaln(alpha, beta))
        return loss

    try:
        result = minimize_scalar(
            nll,
            bounds=(math.log(5.0), math.log(5000.0)),
            method="bounded",
            options={"xatol": 1e-3},
        )
        if result.success:
            return max(5.0, min(5000.0, math.exp(float(result.x))))
    except Exception:
        pass
    return CATEGORY_PRIOR_DEFAULT

def _finalize_category_affinity(
    cs_games: Counter[tuple[int, str]],
    cs_wins: Counter[tuple[int, str]],
    cs_baseline_games: Counter[tuple[int, str]],
    champ_total_games: Counter[int],
    category_games: Counter[str],
    category_wins: Counter[str],
    category_baseline_games: Counter[str],
    category_names: dict[str, dict[str, object]],
    *,
    min_games: int,
    champ_groups: dict[int, str] | None = None,
    group_min_games: int | None = None,
    group_scope: str = "global",
    fallback_min_games: int | None = None,
    top_n: int = 4,
    bot_n: int = 4,
    pick_lift_weight: float = 0.0,
    pick_lift_cap: float = AUGMENT_PICK_LIFT_CAP,
    pick_rate_weight: float = 0.0,
    pick_rate_ref: float = 0.002,
    pick_rate_cap: float | None = None,
    rank_mode: str = "residual",
    top_min_lift: float | None = None,
    top_min_pick_rate: float = 0.0,
    top_pick_guarantee: bool = False,
    popular_bad_n: int = 0,
) -> dict[int, dict]:
    global_total_games = sum(champ_total_games.values())
    category_avg_lift: dict[str, float] = {}
    for slug, games in category_games.items():
        if games > 0:
            category_avg_lift[slug] = (category_wins[slug] / games) - (category_baseline_games[slug] / games)
    category_group_games: Counter[tuple[str, str]] = Counter()
    category_group_wins: Counter[tuple[str, str]] = Counter()
    category_group_baseline_games: Counter[tuple[str, str]] = Counter()
    if champ_groups:
        for (cid, slug), games in cs_games.items():
            group = champ_groups.get(int(cid), "global")
            key = (slug, group)
            category_group_games[key] += games
            category_group_wins[key] += cs_wins[(cid, slug)]
            category_group_baseline_games[key] += cs_baseline_games[(cid, slug)]
    category_group_avg_lift: dict[tuple[str, str], float] = {}
    for key, games in category_group_games.items():
        if games > 0:
            category_group_avg_lift[key] = (
                category_group_wins[key] / games
            ) - (
                category_group_baseline_games[key] / games
            )

    raw_rows: list[dict] = []
    row_min_games = fallback_min_games or min_games
    group_min = group_min_games or max(min_games * 4, 200)
    for (cid, slug), games in cs_games.items():
        if games < row_min_games:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        peer_group = "global"
        avg_lift = category_avg_lift.get(slug, 0.0)
        if champ_groups:
            candidate_group = champ_groups.get(int(cid), "global")
            group_key = (slug, candidate_group)
            if category_group_games[group_key] >= group_min:
                peer_group = candidate_group
                avg_lift = category_group_avg_lift.get(group_key, avg_lift)
        prior_wr = min(max(baseline + avg_lift, 1e-4), 1.0 - 1e-4)
        raw_rows.append({
            "champion_id": cid,
            "slug": slug,
            "games": games,
            "wins": wins,
            "baseline_wr": baseline,
            "avg_lift": avg_lift,
            "prior_wr": prior_wr,
            "peer_group": peer_group,
            "peer_scope": group_scope if peer_group != "global" else "global",
            "primary_sample": games >= min_games,
        })

    prior_strength = estimate_category_prior_strength(raw_rows)
    by_champ: dict[int, list[dict]] = {}
    for row in raw_rows:
        games = int(row["games"])
        wins = int(row["wins"])
        prior_wr = float(row["prior_wr"])
        champ_total = max(int(champ_total_games.get(int(row["champion_id"]), 0)), 1)
        alpha = wins + prior_wr * prior_strength
        beta = games - wins + (1.0 - prior_wr) * prior_strength
        mean_wr = alpha / (alpha + beta)
        lower_wr = beta_posterior_quantile(AUGMENT_POSTERIOR_Q, alpha, beta)
        upper_wr = beta_posterior_quantile(1.0 - AUGMENT_POSTERIOR_Q, alpha, beta)
        slug = str(row["slug"])
        pick_rate = games / champ_total
        global_pick_rate = (
            category_games[slug] / global_total_games
            if global_total_games > 0 else 0.0
        )
        pick_lift = math.log(max(pick_rate, 1e-9) / max(global_pick_rate, 1e-9))
        clamped_pick_lift = max(-pick_lift_cap, min(pick_lift_cap, pick_lift))
        pick_rate_credit = 0.0
        if pick_rate_weight > 0:
            pick_rate_credit = pick_rate_weight * math.log1p(
                pick_rate / max(pick_rate_ref, 1e-9)
            )
            if pick_rate_cap is not None:
                pick_rate_credit = min(pick_rate_cap, pick_rate_credit)
        name_info = category_names.get(slug, _label_entry({}, slug))
        lift = mean_wr - float(row["baseline_wr"])
        lcb_lift = lower_wr - float(row["baseline_wr"])
        ucb_lift = upper_wr - float(row["baseline_wr"])
        residual = mean_wr - prior_wr
        lcb_residual = lower_wr - prior_wr
        ucb_residual = upper_wr - prior_wr
        if rank_mode == "lift":
            rank_score = lcb_lift + pick_lift_weight * clamped_pick_lift + pick_rate_credit
            rank_bad_score = ucb_lift + pick_lift_weight * clamped_pick_lift
        else:
            rank_score = lcb_residual + pick_lift_weight * clamped_pick_lift + pick_rate_credit
            rank_bad_score = ucb_residual + pick_lift_weight * clamped_pick_lift
        packed_row = {
            "name": name_info["name"],
            "name_zh": name_info["name_zh"],
            "name_en": name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": wins / games if games else prior_wr,
            "smoothed_wr": mean_wr,
            "baseline_wr": float(row["baseline_wr"]),
            "avg_lift": float(row["avg_lift"]),
            "lift": lift,
            "lcb_lift": lcb_lift,
            "ucb_lift": ucb_lift,
            "residual": residual,
            "lcb_residual": lcb_residual,
            "ucb_residual": ucb_residual,
            "rank_score": rank_score,
            "rank_bad_score": rank_bad_score,
            "pick_rate": pick_rate,
            "global_pick_rate": global_pick_rate,
            "pick_lift": pick_lift,
            "pick_rate_credit": pick_rate_credit,
            "prior_strength": prior_strength,
            "peer_group": str(row.get("peer_group") or "global"),
            "peer_scope": str(row.get("peer_scope") or "global"),
            "primary_sample": bool(row.get("primary_sample")),
        }
        if "items" in name_info:
            packed_row["items"] = name_info["items"]
        by_champ.setdefault(int(row["champion_id"]), []).append(packed_row)

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        if rank_mode == "lift":
            rows.sort(
                key=lambda r: (
                    -r["rank_score"],
                    -r["lcb_lift"],
                    -r["lift"],
                    -r["pick_rate"],
                    -r["games"],
                    r["name_en"],
                )
            )
        else:
            rows.sort(key=lambda r: (-r["rank_score"], -r["lcb_residual"], -r["residual"], -r["games"], r["name_en"]))
        eligible = [r for r in rows if r.get("primary_sample")]
        if not eligible:
            eligible = rows
        top_rows = eligible
        if top_min_lift is not None:
            top_rows = [r for r in top_rows if float(r.get("lift", 0.0)) >= top_min_lift]
        if top_min_pick_rate > 0:
            top_rows = [r for r in top_rows if float(r.get("pick_rate", 0.0)) >= top_min_pick_rate]
        if top_pick_guarantee and top_rows and top_n > 0:
            top_pick_row = max(
                top_rows,
                key=lambda r: (
                    float(r.get("pick_rate", 0.0)),
                    int(r.get("games", 0)),
                    float(r.get("rank_score", 0.0)),
                    str(r.get("name_en", "")),
                ),
            )
            selected_top_rows = top_rows[:top_n]
            selected_slugs = {str(r.get("slug") or "") for r in selected_top_rows}
            if str(top_pick_row.get("slug") or "") not in selected_slugs:
                if len(selected_top_rows) < top_n:
                    selected_top_rows.append(top_pick_row)
                elif selected_top_rows:
                    selected_top_rows[-1] = top_pick_row
            top_rows = selected_top_rows
        elif top_n > 0:
            top_rows = top_rows[:top_n]
        if rank_mode == "lift":
            bot_rows = sorted(eligible, key=lambda r: (r["rank_bad_score"], r["ucb_lift"], r["lift"], r["games"], r["name_en"]))
        else:
            bot_rows = sorted(eligible, key=lambda r: (r["rank_bad_score"], r["ucb_residual"], r["residual"], r["games"], r["name_en"]))
        if bot_n > 0:
            bot_rows = bot_rows[:bot_n]
        popular_bad_rows: list[dict] = []
        if popular_bad_n > 0:
            popular_bad_rows = sorted(
                [
                    r for r in eligible
                    if float(r.get("lift", 0.0)) <= SINGLE_ITEM_COMMON_TRAP_MIN_LIFT
                    and not _is_antiheal_item_name(str(r.get("name_en") or r.get("name") or ""))
                ],
                key=lambda r: (
                    -float(r.get("pick_rate", 0.0)),
                    -int(r.get("games", 0)),
                    float(r.get("lift", 0.0)),
                    str(r.get("name_en", "")),
                ),
            )[:popular_bad_n]
        out[cid] = {
            "top": top_rows,
            "bot": bot_rows,
            "popular_bad": popular_bad_rows,
            "prior_strength": prior_strength,
        }
    return out

def compute_champ_category_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    aug_meta: dict[int, dict],
    item_meta: dict[int, dict],
    champ_records: list[dict],
    champ_profiles: dict[int, dict[str, object]] | None = None,
    *,
    min_set_games: int,
    min_item_games: int,
    min_augtype_games: int,
) -> tuple[dict[int, dict], dict[int, dict], dict[int, dict]]:
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL",
            (queue_id,),
        )

    dims = ("sets", "items", "augtypes")
    cs_games = {dim: Counter() for dim in dims}
    cs_wins = {dim: Counter() for dim in dims}
    cs_baseline_games = {dim: Counter() for dim in dims}
    champ_total_games = Counter()
    category_games = {dim: Counter() for dim in dims}
    category_wins = {dim: Counter() for dim in dims}
    category_baseline_games = {dim: Counter() for dim in dims}
    category_names: dict[str, dict[str, dict[str, str]]] = {dim: {} for dim in dims}

    def add(dim: str, cid: int, player_won: int, baseline: float, infos: list[dict[str, str]]) -> None:
        seen = {str(info.get("slug") or ""): info for info in infos if info.get("slug")}
        for slug, info in seen.items():
            key = (cid, slug)
            cs_games[dim][key] += 1
            cs_wins[dim][key] += player_won
            cs_baseline_games[dim][key] += baseline
            category_games[dim][slug] += 1
            category_wins[dim][slug] += player_won
            category_baseline_games[dim][slug] += baseline
            category_names[dim][slug] = {
                "name": str(info.get("name") or slug),
                "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                "name_en": str(info.get("name_en") or info.get("name") or slug),
            }

    try:
        for blue_wins, participants_json in rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                champ_total_games[cid] += 1
                baseline = baseline_by_champ.get(cid, 0.5)
                player_won = 1 if (team_id == 100) == blue_won else 0
                set_infos: list[dict[str, str]] = []
                aug_type_infos: list[dict[str, str]] = []
                for augment_id in participant.get("augments") or []:
                    meta = aug_meta.get(int(augment_id))
                    if not meta:
                        continue
                    set_infos.extend(meta.get("sets") or [])
                    aug_type_infos.extend(augment_type_infos(meta))
                item_infos = _participant_item_infos(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                add("sets", cid, player_won, baseline, set_infos)
                add("items", cid, player_won, baseline, item_infos)
                add("augtypes", cid, player_won, baseline, aug_type_infos)
    finally:
        con.close()

    augtype_groups = None
    if champ_profiles:
        augtype_groups = {
            int(cid): _profile_group(int(cid), champ_profiles, "role_damage")
            for cid in champ_total_games
        }

    return (
        _finalize_category_affinity(
            cs_games["sets"], cs_wins["sets"], cs_baseline_games["sets"], champ_total_games,
            category_games["sets"], category_wins["sets"], category_baseline_games["sets"],
            category_names["sets"], min_games=min_set_games,
        ),
        _finalize_category_affinity(
            cs_games["items"], cs_wins["items"], cs_baseline_games["items"], champ_total_games,
            category_games["items"], category_wins["items"], category_baseline_games["items"],
            category_names["items"], min_games=min_item_games, fallback_min_games=ITEM_STYLE_FALLBACK_MIN_GAMES,
        ),
        _finalize_category_affinity(
            cs_games["augtypes"], cs_wins["augtypes"], cs_baseline_games["augtypes"], champ_total_games,
            category_games["augtypes"], category_wins["augtypes"], category_baseline_games["augtypes"],
            category_names["augtypes"], min_games=min_augtype_games,
            champ_groups=augtype_groups, group_scope="role_damage",
        ),
    )

def compute_champ_item_pair_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games: int,
) -> dict[int, dict]:
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id,),
        )

    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    champ_total_games = Counter()
    category_games: Counter[str] = Counter()
    category_wins: Counter[str] = Counter()
    category_baseline_games: Counter[str] = Counter()
    category_names: dict[str, dict[str, object]] = {}
    ordered_games: Counter[tuple[int, str, str]] = Counter()
    ordered_wins: Counter[tuple[int, str, str]] = Counter()
    ordered_baseline_games: Counter[tuple[int, str, str]] = Counter()

    try:
        for blue_wins, participants_json in rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                champ_total_games[cid] += 1
                core_ids = _participant_core_item_ids(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                if len(core_ids) < 2:
                    continue
                slug = _item_pair_slug(core_ids, ordered=False)
                ordered_slug = _item_pair_slug(core_ids, ordered=True)
                baseline = baseline_by_champ.get(cid, 0.5)
                player_won = 1 if (team_id == 100) == blue_won else 0
                key = (cid, slug)
                cs_games[key] += 1
                cs_wins[key] += player_won
                cs_baseline_games[key] += baseline
                order_key = (cid, slug, ordered_slug)
                ordered_games[order_key] += 1
                ordered_wins[order_key] += player_won
                ordered_baseline_games[order_key] += baseline
                category_games[slug] += 1
                category_wins[slug] += player_won
                category_baseline_games[slug] += baseline
                if slug not in category_names:
                    items = _item_pair_payload(sorted(core_ids), item_meta)
                    category_names[slug] = {
                        "name": _item_pair_name(items, "name_zh"),
                        "name_zh": _item_pair_name(items, "name_zh"),
                        "name_en": _item_pair_name(items, "name_en"),
                        "items": items,
                    }
    finally:
        con.close()

    best_order_by_pair: dict[tuple[int, str], list[int]] = {}
    order_candidates: dict[tuple[int, str], list[tuple[float, int, str]]] = defaultdict(list)
    for (cid, slug, ordered_slug), games in ordered_games.items():
        baseline = ordered_baseline_games[(cid, slug, ordered_slug)] / games
        wins = ordered_wins[(cid, slug, ordered_slug)]
        smoothed_wr = (
            wins + baseline * ITEM_PAIR_ORDER_PRIOR_GAMES
        ) / (games + ITEM_PAIR_ORDER_PRIOR_GAMES)
        order_candidates[(cid, slug)].append((smoothed_wr - baseline, games, ordered_slug))
    for key, candidates in order_candidates.items():
        _, _, ordered_slug = max(candidates, key=lambda item: (item[0], item[1], item[2]))
        best_order_by_pair[key] = [int(part) for part in ordered_slug.split("+")]

    affinity = _finalize_category_affinity(
        cs_games,
        cs_wins,
        cs_baseline_games,
        champ_total_games,
        category_games,
        category_wins,
        category_baseline_games,
        category_names,
        min_games=min_games,
        fallback_min_games=ITEM_PAIR_FALLBACK_MIN_GAMES,
        pick_lift_weight=ITEM_PAIR_PICK_LIFT_WEIGHT,
        pick_lift_cap=ITEM_PAIR_PICK_LIFT_CAP,
        pick_rate_weight=ITEM_PAIR_PICK_RATE_WEIGHT,
        pick_rate_ref=ITEM_PAIR_PICK_RATE_REF,
        pick_rate_cap=ITEM_PAIR_PICK_RATE_CAP,
        rank_mode="lift",
        top_min_lift=ITEM_PAIR_TOP_MIN_LIFT,
        top_n=0,
    )
    for cid, payload in affinity.items():
        for row in [*(payload.get("top") or []), *(payload.get("bot") or [])]:
            item_ids = best_order_by_pair.get((int(cid), str(row.get("slug") or "")))
            if not item_ids:
                continue
            items = _item_pair_payload(item_ids, item_meta)
            row["name"] = _item_pair_name(items, "name_zh")
            row["name_zh"] = _item_pair_name(items, "name_zh")
            row["name_en"] = _item_pair_name(items, "name_en")
            row["items"] = items
    return affinity

def _compute_champ_item_slot_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games: int,
    item_selector,
    fallback_min_games: int,
    top_min_lift: float,
    top_n: int = 0,
) -> dict[int, dict]:
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id,),
        )

    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    champ_total_games = Counter()
    category_games: Counter[str] = Counter()
    category_wins: Counter[str] = Counter()
    category_baseline_games: Counter[str] = Counter()
    category_names: dict[str, dict[str, object]] = {}

    try:
        for blue_wins, participants_json in rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                champ_total_games[cid] += 1
                selected_ids = item_selector(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                if not selected_ids:
                    continue
                baseline = baseline_by_champ.get(cid, 0.5)
                player_won = 1 if (team_id == 100) == blue_won else 0
                for item_id in selected_ids:
                    slug = str(item_id)
                    key = (cid, slug)
                    cs_games[key] += 1
                    cs_wins[key] += player_won
                    cs_baseline_games[key] += baseline
                    category_games[slug] += 1
                    category_wins[slug] += player_won
                    category_baseline_games[slug] += baseline
                    if slug not in category_names:
                        items = _item_pair_payload([item_id], item_meta)
                        if not items:
                            continue
                        category_names[slug] = {
                            "name": _item_pair_name(items, "name_zh"),
                            "name_zh": _item_pair_name(items, "name_zh"),
                            "name_en": _item_pair_name(items, "name_en"),
                            "items": items,
                        }
    finally:
        con.close()

    return _finalize_category_affinity(
        cs_games,
        cs_wins,
        cs_baseline_games,
        champ_total_games,
        category_games,
        category_wins,
        category_baseline_games,
        category_names,
        min_games=min_games,
        fallback_min_games=fallback_min_games,
        pick_lift_weight=SINGLE_ITEM_PICK_LIFT_WEIGHT,
        pick_lift_cap=SINGLE_ITEM_PICK_LIFT_CAP,
        pick_rate_weight=SINGLE_ITEM_PICK_RATE_WEIGHT,
        pick_rate_ref=SINGLE_ITEM_PICK_RATE_REF,
        pick_rate_cap=SINGLE_ITEM_PICK_RATE_CAP,
        rank_mode="lift",
        top_min_lift=top_min_lift,
        top_n=top_n,
        popular_bad_n=SINGLE_ITEM_COMMON_TRAP_N,
    )

def compute_champ_single_item_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games: int,
) -> dict[int, dict]:
    return _compute_champ_item_slot_affinities(
        db_path,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=min_games,
        item_selector=_participant_recommendable_item_ids,
        fallback_min_games=SINGLE_ITEM_FALLBACK_MIN_GAMES,
        top_min_lift=SINGLE_ITEM_TOP_MIN_LIFT,
        top_n=0,
    )

def compute_champ_boot_item_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games: int,
) -> dict[int, dict]:
    return _compute_champ_item_slot_affinities(
        db_path,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=min_games,
        item_selector=_participant_boot_item_ids,
        fallback_min_games=BOOT_ITEM_FALLBACK_MIN_GAMES,
        top_min_lift=BOOT_ITEM_TOP_MIN_LIFT,
        top_n=4,
    )

def previous_patch_prefix(patch_prefix: str | None) -> str | None:
    if not patch_prefix:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", patch_prefix.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    if minor <= 0:
        return None
    return f"{major}.{minor - 1}"

def _champ_record_index(records: list[dict]) -> dict[int, dict]:
    return {int(row["champion_id"]): row for row in records}

def _record_total_games(records: list[dict]) -> int:
    return sum(int(row.get("games", 0) or 0) for row in records) // 10

def _record_global_wr(records: list[dict]) -> float:
    games = sum(int(row.get("games", 0) or 0) for row in records)
    wins = sum(int(row.get("wins", 0) or 0) for row in records)
    return (wins / games) if games else 0.5

def _smoothed_patch_wr(wins: int, games: int, prior: float, prior_games: int) -> float:
    return (wins + prior * prior_games) / (games + prior_games) if games + prior_games else prior

def _patch_champ_payload(cid: int, champ_meta: dict[int, dict]) -> dict[str, object]:
    meta = champ_meta.get(cid, {})
    return {
        "id": cid,
        "name": meta.get("name", str(cid)),
        "name_zh": meta.get("name_zh", meta.get("name", str(cid))),
        "name_en": meta.get("name_en", meta.get("alias", meta.get("name", str(cid)))),
        "alias": meta.get("alias", ""),
        "image": meta.get("image", ""),
    }

def _patch_item_payload(item_id: int, item_meta: dict[int, dict]) -> dict[str, object]:
    item = item_meta.get(item_id, {})
    return {
        "id": item_id,
        "name": item.get("name", str(item_id)),
        "name_zh": item.get("name_zh", item.get("name", str(item_id))),
        "name_en": item.get("name_en", item.get("name", str(item_id))),
        "icon": item.get("icon", ""),
    }

def _compute_core_item_patch_stats(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    item_meta: dict[int, dict],
    champ_records: list[dict],
) -> dict[str, object]:
    champ_baseline = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5) or 0.5)
        for row in champ_records
    }
    item_stats: dict[int, dict[str, float]] = defaultdict(
        lambda: {"games": 0.0, "wins": 0.0}
    )
    champ_item_stats: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: {"games": 0.0, "wins": 0.0, "baseline_sum": 0.0}
    )
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id, f"{patch_prefix}%"),
        )
        for blue_wins, participants_json in rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                selected_ids = _participant_recommendable_item_ids(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                if not selected_ids:
                    continue
                player_won = 1 if (team_id == 100) == blue_won else 0
                baseline = champ_baseline.get(cid, 0.5)
                for item_id in selected_ids:
                    item_bucket = item_stats[item_id]
                    item_bucket["games"] += 1
                    item_bucket["wins"] += player_won
                    champ_bucket = champ_item_stats[(cid, item_id)]
                    champ_bucket["games"] += 1
                    champ_bucket["wins"] += player_won
                    champ_bucket["baseline_sum"] += baseline
    finally:
        con.close()
    return {
        "item": item_stats,
        "champ_item": champ_item_stats,
        "global_wr": _record_global_wr(champ_records),
    }

def compute_patch_changes(
    db_path: Path,
    queue_id: int,
    current_patch: str | None,
    baseline_patch: str | None,
    item_meta: dict[int, dict],
    champ_meta: dict[int, dict],
    current_records: list[dict],
    baseline_records: list[dict],
) -> dict[str, object] | None:
    if not current_patch or not baseline_patch:
        return None

    current_by_champ = _champ_record_index(current_records)
    baseline_by_champ = _champ_record_index(baseline_records)
    hero_rows: list[dict[str, object]] = []
    for cid, current in current_by_champ.items():
        baseline = baseline_by_champ.get(cid)
        if not baseline:
            continue
        current_games = int(current.get("games", 0) or 0)
        baseline_games = int(baseline.get("games", 0) or 0)
        if current_games < PATCH_CHANGE_HERO_MIN_GAMES or baseline_games < PATCH_CHANGE_HERO_MIN_GAMES:
            continue
        current_wr = float(current.get("bayes_wr", 0.0) or 0.0)
        baseline_wr = float(baseline.get("bayes_wr", 0.0) or 0.0)
        hero_rows.append({
            **_patch_champ_payload(cid, champ_meta),
            "current_wr": round(current_wr, 4),
            "baseline_wr": round(baseline_wr, 4),
            "delta": round(current_wr - baseline_wr, 4),
            "current_games": current_games,
            "baseline_games": baseline_games,
            "current_tier": assign_tier(current_wr),
            "baseline_tier": assign_tier(baseline_wr),
        })

    current_item_stats = _compute_core_item_patch_stats(
        db_path, queue_id, current_patch, item_meta, current_records
    )
    baseline_item_stats = _compute_core_item_patch_stats(
        db_path, queue_id, baseline_patch, item_meta, baseline_records
    )
    current_global_wr = float(current_item_stats["global_wr"])
    baseline_global_wr = float(baseline_item_stats["global_wr"])

    item_rows: list[dict[str, object]] = []
    current_items = current_item_stats["item"]
    baseline_items = baseline_item_stats["item"]
    for item_id, current in current_items.items():
        baseline = baseline_items.get(item_id)
        if not baseline or item_id not in item_meta:
            continue
        current_games = int(current["games"])
        baseline_games = int(baseline["games"])
        if (
            current_games < PATCH_CHANGE_ITEM_CURRENT_MIN_GAMES
            or baseline_games < PATCH_CHANGE_ITEM_BASELINE_MIN_GAMES
        ):
            continue
        current_wr = _smoothed_patch_wr(
            int(current["wins"]), current_games, current_global_wr, PATCH_CHANGE_ITEM_PRIOR_GAMES
        )
        baseline_wr = _smoothed_patch_wr(
            int(baseline["wins"]), baseline_games, baseline_global_wr, PATCH_CHANGE_ITEM_PRIOR_GAMES
        )
        item_rows.append({
            **_patch_item_payload(item_id, item_meta),
            "current_wr": round(current_wr, 4),
            "baseline_wr": round(baseline_wr, 4),
            "delta": round(current_wr - baseline_wr, 4),
            "current_games": current_games,
            "baseline_games": baseline_games,
        })

    champ_item_rows: list[dict[str, object]] = []
    current_champ_items = current_item_stats["champ_item"]
    baseline_champ_items = baseline_item_stats["champ_item"]
    for key, current in current_champ_items.items():
        cid, item_id = key
        baseline = baseline_champ_items.get(key)
        if not baseline or cid not in champ_meta or item_id not in item_meta:
            continue
        current_games = int(current["games"])
        baseline_games = int(baseline["games"])
        if (
            current_games < PATCH_CHANGE_CHAMP_ITEM_CURRENT_MIN_GAMES
            or baseline_games < PATCH_CHANGE_CHAMP_ITEM_BASELINE_MIN_GAMES
        ):
            continue
        current_prior = float(current["baseline_sum"]) / current_games if current_games else 0.5
        baseline_prior = float(baseline["baseline_sum"]) / baseline_games if baseline_games else 0.5
        current_wr = _smoothed_patch_wr(
            int(current["wins"]), current_games, current_prior, PATCH_CHANGE_CHAMP_ITEM_PRIOR_GAMES
        )
        baseline_wr = _smoothed_patch_wr(
            int(baseline["wins"]), baseline_games, baseline_prior, PATCH_CHANGE_CHAMP_ITEM_PRIOR_GAMES
        )
        current_lift = current_wr - current_prior
        baseline_lift = baseline_wr - baseline_prior
        champ_item_rows.append({
            "champ": _patch_champ_payload(cid, champ_meta),
            "item": _patch_item_payload(item_id, item_meta),
            "current_wr": round(current_wr, 4),
            "baseline_wr": round(baseline_wr, 4),
            "current_lift": round(current_lift, 4),
            "baseline_lift": round(baseline_lift, 4),
            "delta": round(current_lift - baseline_lift, 4),
            "current_games": current_games,
            "baseline_games": baseline_games,
        })

    return {
        "currentPatch": current_patch,
        "baselinePatch": baseline_patch,
        "currentGames": _record_total_games(current_records),
        "baselineGames": _record_total_games(baseline_records),
        "minHeroGames": PATCH_CHANGE_HERO_MIN_GAMES,
        "minItemGames": PATCH_CHANGE_ITEM_CURRENT_MIN_GAMES,
        "minChampItemGames": PATCH_CHANGE_CHAMP_ITEM_CURRENT_MIN_GAMES,
        "heroRisers": sorted(hero_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "heroFallers": sorted(hero_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "itemRisers": sorted(item_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "itemFallers": sorted(item_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "champItemRisers": sorted(champ_item_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "champItemFallers": sorted(champ_item_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
    }

def _affinity_top_rows_by_slug(affinity: dict[int, dict]) -> dict[int, dict[str, dict]]:
    by_champ: dict[int, dict[str, dict]] = {}
    for cid, payload in affinity.items():
        rows = payload.get("top") or []
        by_champ[int(cid)] = {
            str(row.get("slug") or ""): row
            for row in rows
            if row.get("slug")
        }
    return by_champ

def _item_cluster_names(item_ids: list[int], item_meta: dict[int, dict]) -> dict[str, str]:
    style_weight: Counter[str] = Counter()
    style_info_by_slug: dict[str, dict[str, str]] = {}
    for item_id in item_ids:
        item = item_meta.get(int(item_id))
        if not item:
            continue
        for info in item_style_infos(item):
            slug = str(info.get("slug") or "")
            if not slug:
                continue
            style_weight[slug] += max(int(item.get("price_total") or 0), 1)
            style_info_by_slug[slug] = info

    slugs = sorted(style_weight, key=lambda slug: (-style_weight[slug], slug))[:2]
    if slugs:
        infos = [style_info_by_slug[slug] for slug in slugs]
        return {
            "name": " / ".join(str(info.get("name") or info.get("name_zh") or info.get("name_en") or "") for info in infos),
            "name_zh": " / ".join(str(info.get("name_zh") or info.get("name") or info.get("name_en") or "") for info in infos),
            "name_en": " / ".join(str(info.get("name_en") or info.get("name") or info.get("name_zh") or "") for info in infos),
        }

    items = _item_pair_payload(item_ids[:2], item_meta)
    fallback = _item_pair_name(items, "name_zh") if items else "Item route"
    fallback_en = _item_pair_name(items, "name_en") if items else "Item route"
    return {"name": fallback, "name_zh": fallback, "name_en": fallback_en}

def _connected_item_components(
    items: set[int],
    adjacency: dict[int, set[int]],
) -> list[list[int]]:
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in sorted(items):
        if start in seen or not adjacency.get(start):
            continue
        stack = [start]
        seen.add(start)
        component: list[int] = []
        while stack:
            item_id = stack.pop()
            component.append(item_id)
            for nxt in sorted(adjacency.get(item_id, set())):
                if nxt in seen:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        if len(component) >= 2:
            components.append(sorted(component))
    return components


def _item_cluster_style_key(row: dict) -> str:
    label = str(row.get("name_en") or row.get("name") or row.get("name_zh") or "")
    if not label:
        return ""
    parts = [part.strip().lower() for part in label.split("/") if part.strip()]
    if not parts:
        return ""
    return " / ".join(sorted(dict.fromkeys(parts)))


def _item_cluster_core_item_set(row: dict, item_meta: dict[int, dict]) -> set[int]:
    item_set: set[int] = set()
    for item in row.get("items", []):
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not item_id or _is_boot_item(item_meta.get(item_id)):
            continue
        item_set.add(item_id)
    return item_set


def _item_cluster_core_signature(
    row: dict,
    item_meta: dict[int, dict],
    *,
    core_count: int = ITEM_CLUSTER_CORE_ITEM_COUNT,
) -> tuple[int, ...]:
    core_items: list[int] = []
    for item in row.get("items", []):
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not item_id or _is_boot_item(item_meta.get(item_id)):
            continue
        core_items.append(item_id)
        if len(core_items) >= core_count:
            break
    return tuple(core_items)


def _item_cluster_rows_too_similar(row: dict, other: dict, item_meta: dict[int, dict]) -> bool:
    row_core = _item_cluster_core_signature(row, item_meta)
    other_core = _item_cluster_core_signature(other, item_meta)
    if row_core and row_core == other_core:
        return True
    row_items = _item_cluster_core_item_set(row, item_meta)
    other_items = _item_cluster_core_item_set(other, item_meta)
    if not row_items or not other_items:
        return False
    union = row_items | other_items
    if not union:
        return False
    jaccard = len(row_items & other_items) / len(union)
    if jaccard >= ITEM_CLUSTER_DIVERSITY_HARD_MAX_JACCARD:
        return True
    return (
        jaccard >= ITEM_CLUSTER_DIVERSITY_MAX_JACCARD
        and _item_cluster_style_key(row) == _item_cluster_style_key(other)
    )


def _select_diverse_item_cluster_rows(
    rows: list[dict],
    item_meta: dict[int, dict],
    *,
    top_n: int,
) -> list[dict]:
    selected: list[dict] = []
    for row in rows:
        if any(_item_cluster_rows_too_similar(row, existing, item_meta) for existing in selected):
            continue
        selected.append(row)
        if len(selected) >= top_n:
            break
    return selected


def compute_champ_item_build_clusters(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    single_item_affinity: dict[int, dict],
    *,
    min_pair_games: int = ITEM_CLUSTER_MIN_PAIR_GAMES,
    min_games: int = ITEM_CLUSTER_MIN_GAMES,
    max_items: int = ITEM_CLUSTER_MAX_ITEMS,
    top_n: int = ITEM_CLUSTER_TOP_N,
) -> dict[int, dict]:
    """Cluster each champion's co-built core items into readable build routes."""
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id,),
        )

    champ_total_games = Counter()
    champ_item_games: Counter[tuple[int, int]] = Counter()
    champ_item_wins: Counter[tuple[int, int]] = Counter()
    champ_item_baseline_games: Counter[tuple[int, int]] = Counter()
    champ_pair_games: Counter[tuple[int, int, int]] = Counter()
    champ_pair_wins: Counter[tuple[int, int, int]] = Counter()
    champ_pair_baseline_games: Counter[tuple[int, int, int]] = Counter()
    champ_exact_build_games: Counter[tuple[int, tuple[int, ...]]] = Counter()
    champ_exact_build_wins: Counter[tuple[int, tuple[int, ...]]] = Counter()
    champ_exact_build_baseline_games: Counter[tuple[int, tuple[int, ...]]] = Counter()
    global_item_games: Counter[int] = Counter()
    global_item_wins: Counter[int] = Counter()
    global_item_baseline_games: Counter[int] = Counter()
    champ_builds: dict[int, list[tuple[tuple[int, ...], int, float]]] = defaultdict(list)

    try:
        for blue_wins, participants_json in rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                champ_total_games[cid] += 1
                route_ids = _participant_route_item_ids(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                if not route_ids:
                    continue
                baseline = baseline_by_champ.get(cid, 0.5)
                player_won = 1 if (team_id == 100) == blue_won else 0
                ordered_route = tuple(route_ids)
                champ_builds[cid].append((ordered_route, player_won, baseline))
                sorted_route = tuple(sorted(ordered_route))
                if len(sorted_route) == max_items:
                    exact_key = (cid, sorted_route)
                    champ_exact_build_games[exact_key] += 1
                    champ_exact_build_wins[exact_key] += player_won
                    champ_exact_build_baseline_games[exact_key] += baseline
                for item_id in sorted_route:
                    item_key = (cid, item_id)
                    champ_item_games[item_key] += 1
                    champ_item_wins[item_key] += player_won
                    champ_item_baseline_games[item_key] += baseline
                    global_item_games[item_id] += 1
                    global_item_wins[item_id] += player_won
                    global_item_baseline_games[item_id] += baseline
                for idx, item_a in enumerate(sorted_route):
                    for item_b in sorted_route[idx + 1:]:
                        pair_key = (cid, item_a, item_b)
                        champ_pair_games[pair_key] += 1
                        champ_pair_wins[pair_key] += player_won
                        champ_pair_baseline_games[pair_key] += baseline
    finally:
        con.close()

    single_rows_by_champ = _affinity_top_rows_by_slug(single_item_affinity)
    global_item_lift = {
        item_id: (global_item_wins[item_id] / games) - (global_item_baseline_games[item_id] / games)
        for item_id, games in global_item_games.items()
        if games > 0
    }
    exact_routes_by_champ: dict[int, list[tuple[int, ...]]] = defaultdict(list)
    for (exact_cid, route_key), games in champ_exact_build_games.items():
        if games >= ITEM_CLUSTER_MIN_EXACT_GAMES:
            exact_routes_by_champ[exact_cid].append(route_key)
    for exact_cid, routes in list(exact_routes_by_champ.items()):
        routes.sort(
            key=lambda route_key: (
                -champ_exact_build_games[(exact_cid, route_key)],
                route_key,
            )
        )
        exact_routes_by_champ[exact_cid] = routes[:ITEM_CLUSTER_MAX_EXACT_ROUTES_PER_CHAMP]
    pair_keys_by_champ: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for (pair_cid, item_a, item_b), games in champ_pair_games.items():
        pair_keys_by_champ[pair_cid].append((item_a, item_b, games))

    out: dict[int, dict] = {}
    for cid, single_rows in single_rows_by_champ.items():
        if not single_rows:
            continue
        eligible_items = {
            int(slug)
            for slug, row in single_rows.items()
            if slug.isdigit()
            and champ_item_games[(cid, int(slug))] >= SINGLE_ITEM_FALLBACK_MIN_GAMES
        }
        if len(eligible_items) < 2:
            continue
        exact_routes = exact_routes_by_champ.get(cid, [])
        if not exact_routes:
            continue

        def route_item_lift(item_id: int) -> float:
            games = champ_item_games[(cid, item_id)]
            if games <= 0:
                return 0.0
            baseline = champ_item_baseline_games[(cid, item_id)] / games
            return (champ_item_wins[(cid, item_id)] / games) - baseline

        def route_item_has_evidence(item_id: int) -> bool:
            games = champ_item_games[(cid, item_id)]
            if games >= ITEM_CLUSTER_ITEM_EVIDENCE_MIN_GAMES:
                return True
            return (
                games >= ITEM_CLUSTER_ITEM_FALLBACK_MIN_GAMES
                and route_item_lift(item_id) >= ITEM_CLUSTER_ITEM_FALLBACK_MIN_LIFT
            )

        def item_strength(item_id: int) -> float:
            row = single_rows.get(str(item_id), {})
            item_lift = route_item_lift(item_id)
            return (
                float(row.get("rank_score", 0.0))
                + 0.25 * item_lift
                + 0.20 * global_item_lift.get(item_id, float(row.get("avg_lift", 0.0)))
                + 0.05 * math.log1p(champ_item_games[(cid, item_id)])
            )

        adjacency: dict[int, set[int]] = defaultdict(set)
        pair_metrics: dict[tuple[int, int], dict[str, float]] = {}
        for item_a, item_b, games in pair_keys_by_champ.get(cid, []):
            if games < min_pair_games:
                continue
            if _is_boot_item(item_meta.get(item_a)) or _is_boot_item(item_meta.get(item_b)):
                continue
            if item_a not in eligible_items or item_b not in eligible_items:
                continue
            item_a_games = champ_item_games[(cid, item_a)]
            item_b_games = champ_item_games[(cid, item_b)]
            if item_a_games <= 0 or item_b_games <= 0:
                continue
            cosine = games / math.sqrt(item_a_games * item_b_games)
            if cosine < ITEM_CLUSTER_MIN_COSINE:
                continue
            baseline = champ_pair_baseline_games[(cid, item_a, item_b)] / games
            raw_wr = champ_pair_wins[(cid, item_a, item_b)] / games
            pair_lift = raw_wr - baseline
            if pair_lift < ITEM_CLUSTER_TOP_MIN_LIFT:
                continue
            adjacency[item_a].add(item_b)
            adjacency[item_b].add(item_a)
            pair_metrics[(item_a, item_b)] = {
                "games": float(games),
                "lift": float(pair_lift),
                "cosine": float(cosine),
            }

        rows_by_slug: dict[str, dict] = {}
        for component in _connected_item_components(eligible_items, adjacency):
            cluster_set = set(component)
            cluster_games = 0
            for build_item_ids, player_won, baseline in champ_builds.get(cid, []):
                if len(cluster_set.intersection(build_item_ids)) < 2:
                    continue
                cluster_games += 1
            if cluster_games < min_games:
                continue

            for route_key in exact_routes:
                exact_games = champ_exact_build_games[(cid, route_key)]
                if exact_games < ITEM_CLUSTER_MIN_EXACT_GAMES:
                    continue
                boot_items = [item_id for item_id in route_key if _is_boot_item(item_meta.get(item_id))]
                if len(boot_items) != 1:
                    continue
                core_items = [item_id for item_id in route_key if item_id not in boot_items]
                if len(core_items) < 2 or len(cluster_set.intersection(core_items)) < 2:
                    continue
                if not all(route_item_has_evidence(item_id) for item_id in route_key):
                    continue

                ordered_core_items = sorted(
                    core_items,
                    key=lambda item_id: (
                        -item_strength(item_id),
                        -champ_item_games[(cid, item_id)],
                        str((item_meta.get(item_id) or {}).get("name_en") or item_id),
                    ),
                )[:max(0, max_items - 1)]
                selected_items = ordered_core_items + boot_items[:1]
                if len(selected_items) != max_items:
                    continue
                scoring_items = ordered_core_items

                core_score_items = scoring_items[:ITEM_CLUSTER_CORE_ITEM_COUNT]
                flex_score_items = scoring_items[ITEM_CLUSTER_CORE_ITEM_COUNT:]

                def average(values: list[float]) -> float:
                    return sum(values) / len(values) if values else 0.0

                def item_global_fit(item_id: int) -> float:
                    return global_item_lift.get(
                        item_id,
                        float(single_rows.get(str(item_id), {}).get("avg_lift", 0.0)),
                    )

                def weighted_pair_lift(items: list[int]) -> tuple[float, int]:
                    pair_weight = 0.0
                    pair_weighted_lift = 0.0
                    pair_coverage = 0
                    for idx, item_a in enumerate(items):
                        for item_b in items[idx + 1:]:
                            metric = pair_metrics.get((min(item_a, item_b), max(item_a, item_b)))
                            if not metric:
                                continue
                            weight = metric["games"] * max(metric["cosine"], ITEM_CLUSTER_MIN_COSINE)
                            pair_weight += weight
                            pair_weighted_lift += metric["lift"] * weight
                            pair_coverage += 1
                    return (pair_weighted_lift / pair_weight if pair_weight > 0 else 0.0, pair_coverage)

                core_pair_fit, core_pair_coverage = weighted_pair_lift(core_score_items)
                pair_fit, pair_coverage = weighted_pair_lift(scoring_items)
                core_single_fit = average([route_item_lift(item_id) for item_id in core_score_items])
                flex_single_fit = average([route_item_lift(item_id) for item_id in flex_score_items])
                single_fit = average([route_item_lift(item_id) for item_id in selected_items])
                core_global_fit = average([item_global_fit(item_id) for item_id in core_score_items])
                flex_global_fit = average([item_global_fit(item_id) for item_id in flex_score_items])
                global_fit = average([item_global_fit(item_id) for item_id in selected_items])

                flex_stability_scores: list[float] = []
                for item_id in flex_score_items:
                    item_cluster_games = 0
                    core_pair_games_sum = 0
                    for build_item_ids, _player_won, _baseline in champ_builds.get(cid, []):
                        build_set = set(build_item_ids)
                        if item_id in build_set and len(cluster_set.intersection(build_set)) >= 2:
                            item_cluster_games += 1
                    for core_item_id in core_score_items:
                        core_pair_games_sum += champ_pair_games[
                            (cid, min(item_id, core_item_id), max(item_id, core_item_id))
                        ]
                    cluster_pick = item_cluster_games / max(cluster_games, 1)
                    item_pick = champ_item_games[(cid, item_id)] / max(champ_total_games.get(cid, 0), 1)
                    flex_stability_scores.append(
                        min(0.018, 0.018 * cluster_pick / ITEM_CLUSTER_FLEX_STABILITY_PICK_REF)
                        + min(0.012, 0.012 * math.sqrt(champ_item_games[(cid, item_id)] / 300.0))
                        + min(0.010, 0.010 * math.log1p(core_pair_games_sum) / math.log1p(120.0))
                        + min(0.006, 0.006 * item_pick / ITEM_CLUSTER_PICK_RATE_REF)
                    )
                flex_stability = average(flex_stability_scores)
                exact_wins = champ_exact_build_wins[(cid, route_key)]
                baseline_wr = champ_exact_build_baseline_games[(cid, route_key)] / exact_games
                smoothed_wr = (
                    exact_wins + baseline_wr * ITEM_PAIR_ORDER_PRIOR_GAMES
                ) / (exact_games + ITEM_PAIR_ORDER_PRIOR_GAMES)
                route_lift = smoothed_wr - baseline_wr
                if (
                    route_lift < ITEM_CLUSTER_TOP_MIN_LIFT
                    and single_fit < 0.0
                    and pair_fit < 0.0
                ):
                    continue
                pick_rate = exact_games / max(champ_total_games.get(cid, 0), 1)
                pick_credit = ITEM_CLUSTER_PICK_RATE_WEIGHT * math.log1p(
                    pick_rate / max(ITEM_CLUSTER_PICK_RATE_REF, 1e-9)
                )
                pick_credit = min(ITEM_CLUSTER_PICK_RATE_CAP, pick_credit)
                exact_credit = ITEM_CLUSTER_EXACT_GAMES_WEIGHT * math.log1p(exact_games)
                rank_score = (
                    ITEM_CLUSTER_ROUTE_LIFT_WEIGHT * route_lift
                    + ITEM_CLUSTER_CORE_PAIR_WEIGHT * core_pair_fit
                    + ITEM_CLUSTER_CORE_SINGLE_WEIGHT * core_single_fit
                    + ITEM_CLUSTER_CORE_GLOBAL_WEIGHT * core_global_fit
                    + ITEM_CLUSTER_FLEX_SINGLE_WEIGHT * flex_single_fit
                    + ITEM_CLUSTER_FLEX_GLOBAL_WEIGHT * flex_global_fit
                    + ITEM_CLUSTER_FLEX_STABILITY_WEIGHT * flex_stability
                    + pick_credit
                    + exact_credit
                )
                slug = "cluster:" + "+".join(str(item_id) for item_id in route_key)
                names = _item_cluster_names(selected_items, item_meta)
                row = {
                    **names,
                    "slug": slug,
                    "games": exact_games,
                    "wins": exact_wins,
                    "raw_wr": exact_wins / exact_games,
                    "smoothed_wr": smoothed_wr,
                    "baseline_wr": baseline_wr,
                    "lift": route_lift,
                    "rank_score": rank_score,
                    "pick_rate": pick_rate,
                    "pair_lift": pair_fit,
                    "single_lift": single_fit,
                    "global_lift": global_fit,
                    "core_pair_lift": core_pair_fit,
                    "core_single_lift": core_single_fit,
                    "flex_single_lift": flex_single_fit,
                    "flex_stability": flex_stability,
                    "pair_coverage": pair_coverage,
                    "core_pair_coverage": core_pair_coverage,
                    "cluster_size": len(selected_items),
                    "cluster_games": cluster_games,
                    "exact_games": exact_games,
                    "items": _item_pair_payload(selected_items, item_meta),
                }
                existing = rows_by_slug.get(slug)
                if existing is None or (
                    float(row.get("rank_score", 0.0)),
                    int(row.get("games", 0)),
                ) > (
                    float(existing.get("rank_score", 0.0)),
                    int(existing.get("games", 0)),
                ):
                    rows_by_slug[slug] = row

        rows_out = list(rows_by_slug.values())
        if not rows_out:
            continue
        rows_out.sort(
            key=lambda row: (
                -float(row.get("rank_score", 0.0)),
                -float(row.get("lift", 0.0)),
                -float(row.get("pick_rate", 0.0)),
                -int(row.get("games", 0)),
                str(row.get("name_en", "")),
            )
        )
        rows_out = _select_diverse_item_cluster_rows(rows_out, item_meta, top_n=top_n)
        if not rows_out:
            continue
        out[cid] = {"top": rows_out, "bot": []}
    return out

def _is_ranged_champion(meta: dict) -> bool:
    if str(meta.get("alias") or "") in ROLE_RANGED_ALIAS_OVERRIDES:
        return True
    return int(meta.get("attack_range") or 0) >= RANGED_ATTACK_RANGE_MIN

def _role_from_item_style(slug: str, meta: dict) -> str | None:
    if slug in MARKSMAN_ITEM_STYLES:
        return "Marksman" if _is_ranged_champion(meta) else "Fighter"
    return ROLE_FROM_ITEM_STYLE.get(slug)

def _style_role_need_credit(slug: str, role: str) -> float:
    tank_credit = ROLE_NEED_CREDITS["Tank"]
    marksman_credit = ROLE_NEED_CREDITS["Marksman"]
    mage_credit = ROLE_NEED_CREDITS["Mage"]
    if slug in {"tank", "heartsteel"}:
        return tank_credit
    if slug in {"crit", "ad_poke"}:
        return marksman_credit
    if slug == "ad_bruiser":
        return 0.5 * (tank_credit + marksman_credit)
    if slug == "ap_bruiser":
        return 0.5 * (tank_credit + mage_credit)
    if slug == "onhit":
        return marksman_credit if role == "Marksman" else 0.5 * (tank_credit + marksman_credit)
    if slug == "ap_onhit":
        return marksman_credit if role == "Marksman" else 0.5 * (tank_credit + mage_credit)
    return ROLE_NEED_CREDITS.get(role, 0.0)

def infer_secondary_roles_from_data(
    champ_meta: dict[int, dict],
    champ_records: list[dict],
    item_style_affinity: dict[int, dict],
) -> list[tuple[str, list[str], list[str]]]:
    """Keep base primary roles stable; add only data-backed alternate roles.

    Primary role stays aligned with the site's curated primary-role map.
    Secondary role only appears when a distinct item-style branch earns it
    from real usage + win-rate fit in the current patch.
    """
    games_by_champ = {
        int(row["champion_id"]): max(int(row.get("games", 0) or 0), 1)
        for row in champ_records
    }
    wr_by_champ = {
        int(row["champion_id"]): float(row.get("bayes_wr", row.get("raw_wr", 0.5)) or 0.5)
        for row in champ_records
    }
    changes: list[tuple[str, list[str], list[str]]] = []

    for cid, affinity in item_style_affinity.items():
        meta = champ_meta.get(int(cid))
        if not meta:
            continue
        champ_games = games_by_champ.get(int(cid), 1)
        champ_wr = wr_by_champ.get(int(cid), 0.5)
        role_rows: dict[str, dict[str, object]] = {}
        for row in affinity.get("top", []):
            slug = str(row.get("slug") or "")
            role = _role_from_item_style(slug, meta)
            if not role:
                continue
            pick_rate = float(row.get("pick_rate", 0.0) or 0.0)
            if pick_rate < ROLE_MIN_PICK_RATE:
                continue
            conservative = float(row.get("lcb_residual", row.get("residual", 0.0)) or 0.0)
            residual = float(row.get("residual", 0.0) or 0.0)
            lift = float(row.get("lift", 0.0) or 0.0)
            pick_lift = float(row.get("pick_lift", 0.0) or 0.0)
            clamped_pick_lift = max(-ROLE_PICK_LIFT_CAP, min(ROLE_PICK_LIFT_CAP, pick_lift))
            score = conservative + _style_role_need_credit(slug, role) + ROLE_PICK_LIFT_WEIGHT * clamped_pick_lift
            if score <= 0.0:
                continue
            current = role_rows.get(role)
            if current is None or score > current["score"]:
                role_rows[role] = {
                    "score": score,
                    "residual": residual,
                    "lift": lift,
                    "pick_rate": pick_rate,
                    "pick_lift": pick_lift,
                    "wr": float(row.get("smoothed_wr", 0.0) or 0.0),
                    "games": int(row.get("games", 0) or 0),
                    "style_slug": slug,
                    "style_name": str(row.get("name") or slug),
                    "style_name_zh": str(row.get("name_zh") or row.get("name") or slug),
                    "style_name_en": str(row.get("name_en") or row.get("name") or slug),
                    "source": "data",
                }

        primary_role = str(meta.get("primary_role") or "")
        before = [primary_role] if primary_role else list(meta.get("tags") or [])
        ranked = sorted(
            role_rows.items(),
            key=lambda item: (
                -item[1]["score"],
                -item[1]["pick_rate"],
                ROLE_SORT_PRIORITY.get(item[0], 99),
            ),
        )
        primary_role = primary_role or (before[0] if before else (ranked[0][0] if ranked else ""))
        inferred = [primary_role] if primary_role else []
        secondary_role = ""
        for role, info in ranked:
            if role == primary_role:
                continue
            if info["pick_rate"] < SECONDARY_ROLE_MIN_PICK_RATE:
                continue
            secondary_role = role
            inferred.append(role)
            break

        role_meta: dict[str, dict[str, object]] = {}
        for idx, role in enumerate(inferred[:2]):
            info = dict(role_rows.get(role) or {})
            if not info:
                info = {
                    "score": None,
                    "residual": None,
                    "lift": None,
                    "pick_rate": None,
                    "pick_lift": None,
                    "wr": champ_wr,
                    "games": champ_games,
                    "style_slug": "",
                    "style_name": "",
                    "style_name_zh": "",
                    "style_name_en": "",
                    "source": "base",
                }
            info["role"] = role
            info["slot"] = "primary" if idx == 0 else "secondary"
            info["role_label_zh"] = ROLE_LABELS.get(role, {}).get("zh", role)
            info["role_label_en"] = ROLE_LABELS.get(role, {}).get("en", role)
            role_meta[role] = info
        if role_meta:
            meta["role_meta"] = role_meta
        elif "role_meta" in meta:
            del meta["role_meta"]

        if inferred and inferred != before:
            meta["tags"] = inferred
            changes.append((str(meta.get("alias") or cid), before, inferred))

    return changes

def build_champ_augment_picks(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    profiles: dict[int, dict[str, object]],
    *,
    min_games_per_pair: int,
    top_n: int,
    bot_n: int,
    prior_strength: float,
) -> dict[int, dict]:
    """For each champion, rank augments within each rarity by fit score.

    Displayed WR remains the posterior mean.  Ranking uses a conservative
    posterior lower-bound lift with a small peer-relative pick-rate weight.
    The pick-rate term nudges stable, repeatedly chosen fits without rewriting
    the displayed WR.  A non-positive top/bot limit keeps the full ranked
    bucket, which is what the carousel UI needs.
    """
    pick_lift_index = build_pick_lift_index(champ_aug, aug_meta, profiles)
    by_champ_rarity: dict[int, dict[str, list[dict]]] = {}
    for row in champ_aug:
        if row["games"] < min_games_per_pair:
            continue
        meta = aug_meta.get(row["augment_id"])
        if meta is None:
            continue
        rarity = meta.get("rarity", "")
        if rarity not in RARITY_ORDER:
            continue
        bucket = by_champ_rarity.setdefault(
            row["champion_id"], {r: [] for r in RARITY_ORDER}
        )
        games = int(row["games"])
        wins = int(row["wins"])
        baseline = float(row.get("baseline_wr", 0.5))
        mean_wr, lower_wr = posterior_wr_summary(wins, games, baseline, prior_strength)
        pick_info = pick_lift_index.get((int(row["champion_id"]), int(row["augment_id"])), {})
        pick_lift = float(pick_info.get("pick_lift", 0.0))
        clamped_pick_lift = max(-AUGMENT_PICK_LIFT_CAP, min(AUGMENT_PICK_LIFT_CAP, pick_lift))
        ranked = {
            **row,
            "raw_wr": wins / games if games else baseline,
            "smoothed_wr": mean_wr,
            "lcb_wr": lower_wr,
            "baseline_wr": baseline,
            "lift": mean_wr - baseline,
            "lcb_lift": lower_wr - baseline,
            "rank_score": (lower_wr - baseline) + AUGMENT_PICK_LIFT_WEIGHT * clamped_pick_lift,
            "pick_rate": float(pick_info.get("pick_rate", 0.0)),
            "peer_pick_rate": float(pick_info.get("peer_pick_rate", 0.0)),
            "pick_lift": pick_lift,
            "peer_scope": str(pick_info.get("peer_scope", "")),
            "peer_group": str(pick_info.get("peer_group", "")),
        }
        bucket[rarity].append(ranked)

    def _take_ranked(rows: list[dict], limit: int) -> list[dict]:
        return rows if limit <= 0 else rows[:limit]

    out: dict[int, dict] = {}
    for cid, buckets in by_champ_rarity.items():
        top, bot = {}, {}
        for rarity, rows in buckets.items():
            rows.sort(key=lambda r: (-r["rank_score"], -r["lcb_lift"], -r["games"], r["augment_id"]))
            top[rarity] = _take_ranked(rows, top_n)
            bot_rows = sorted(
                rows,
                key=lambda r: (r["rank_score"], r["lcb_lift"], r["games"], r["augment_id"]),
            )
            bot[rarity] = _take_ranked(bot_rows, bot_n)
        out[cid] = {"top": top, "bot": bot}
    return out

def build_champ_set_affinity(
    champ_aug: list[dict],
    aug_meta: dict[int, dict],
    *,
    min_games_per_set: int,
    top_n: int = 4,
    bot_n: int = 4,
) -> dict[int, dict]:
    """Aggregate per-augment rows into champion x augment-set affinity.

    `lift` asks whether a champion performs better with this set than their
    own baseline. `residual` then subtracts the global set lift, so generally
    strong sets do not automatically look like good champion-specific fits.
    """
    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    set_games: Counter[str] = Counter()
    set_wins: Counter[str] = Counter()
    set_baseline_games: Counter[str] = Counter()
    set_names: dict[str, dict[str, str]] = {}

    for row in champ_aug:
        meta = aug_meta.get(row["augment_id"])
        if not meta:
            continue
        memberships = meta.get("sets") or []
        if not memberships:
            continue
        games = int(row["games"])
        wins = int(row["wins"])
        baseline_games = float(row.get("baseline_wr", 0.5)) * games
        for info in memberships:
            slug = str(info.get("slug") or "")
            if not slug:
                continue
            name_info = {
                "name": str(info.get("name") or slug),
                "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                "name_en": str(info.get("name_en") or info.get("name") or slug),
            }
            key = (int(row["champion_id"]), slug)
            cs_games[key] += games
            cs_wins[key] += wins
            cs_baseline_games[key] += baseline_games
            set_games[slug] += games
            set_wins[slug] += wins
            set_baseline_games[slug] += baseline_games
            set_names[slug] = name_info

    set_avg_lift: dict[str, float] = {}
    for slug, games in set_games.items():
        if games <= 0:
            continue
        set_wr = set_wins[slug] / games
        set_baseline = set_baseline_games[slug] / games
        set_avg_lift[slug] = set_wr - set_baseline

    pair_k = 30.0
    by_champ: dict[int, list[dict]] = {}
    for (cid, slug), games in cs_games.items():
        if games < min_games_per_set:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        raw = wins / games if games else baseline
        smoothed = (wins + baseline * pair_k) / (games + pair_k)
        lift = smoothed - baseline
        avg_lift = set_avg_lift.get(slug, 0.0)
        set_name_info = set_names.get(
            slug,
            {"name": slug, "name_zh": slug, "name_en": slug},
        )
        by_champ.setdefault(cid, []).append({
            "set": set_name_info["name"],
            "set_zh": set_name_info["name_zh"],
            "set_en": set_name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": lift,
            "avg_lift": avg_lift,
            "residual": lift - avg_lift,
        })

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        rows.sort(key=lambda r: (-r["residual"], -abs(r["lift"]), -r["games"], r["set"]))
        out[cid] = {
            "top": rows[:top_n],
            "bot": sorted(rows, key=lambda r: (r["residual"], r["games"], r["set"]))[:bot_n],
        }
    return out

def compute_champ_set_affinity(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    aug_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games_per_set: int,
    top_n: int = 4,
    bot_n: int = 4,
) -> dict[int, dict]:
    """Compute champion x augment-set affinity from player-games.

    A player-game counts once for a set if that participant picked one or more
    augments from the set. This keeps the displayed `games` value literal while
    still capturing the performance of set-oriented builds.
    """
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL",
                (queue_id, f"{patch_prefix}%"),
            )
        )
    else:
        rows = list(
            con.execute(
                "SELECT blue_wins, participants_json FROM games "
                "WHERE queue_id=? AND participants_json IS NOT NULL",
                (queue_id,),
            )
        )
    con.close()

    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    set_games: Counter[str] = Counter()
    set_wins: Counter[str] = Counter()
    set_baseline_games: Counter[str] = Counter()
    set_names: dict[str, dict[str, str]] = {}

    for blue_wins, participants_json in rows:
        if not participants_json:
            continue
        blue_won = bool(blue_wins)
        for participant in json.loads(participants_json):
            cid = int(participant.get("championId", 0) or 0)
            team_id = int(participant.get("teamId", 0) or 0)
            if cid <= 0 or team_id not in (100, 200):
                continue
            seen_sets: dict[str, dict[str, str]] = {}
            for augment_id in participant.get("augments") or []:
                meta = aug_meta.get(int(augment_id))
                if not meta:
                    continue
                for info in meta.get("sets") or []:
                    slug = str(info.get("slug") or "")
                    if slug:
                        seen_sets[slug] = {
                            "name": str(info.get("name") or slug),
                            "name_zh": str(info.get("name_zh") or info.get("name") or slug),
                            "name_en": str(info.get("name_en") or info.get("name") or slug),
                        }
            if not seen_sets:
                continue
            player_won = 1 if (team_id == 100) == blue_won else 0
            baseline = baseline_by_champ.get(cid, 0.5)
            for slug, name_info in seen_sets.items():
                key = (cid, slug)
                cs_games[key] += 1
                cs_wins[key] += player_won
                cs_baseline_games[key] += baseline
                set_games[slug] += 1
                set_wins[slug] += player_won
                set_baseline_games[slug] += baseline
                set_names[slug] = name_info

    set_avg_lift: dict[str, float] = {}
    for slug, games in set_games.items():
        if games <= 0:
            continue
        set_avg_lift[slug] = (set_wins[slug] / games) - (set_baseline_games[slug] / games)

    pair_k = 30.0
    by_champ: dict[int, list[dict]] = {}
    for (cid, slug), games in cs_games.items():
        if games < min_games_per_set:
            continue
        wins = cs_wins[(cid, slug)]
        baseline = cs_baseline_games[(cid, slug)] / games
        raw = wins / games if games else baseline
        smoothed = (wins + baseline * pair_k) / (games + pair_k)
        lift = smoothed - baseline
        avg_lift = set_avg_lift.get(slug, 0.0)
        set_name_info = set_names.get(
            slug,
            {"name": slug, "name_zh": slug, "name_en": slug},
        )
        by_champ.setdefault(cid, []).append({
            "set": set_name_info["name"],
            "set_zh": set_name_info["name_zh"],
            "set_en": set_name_info["name_en"],
            "slug": slug,
            "games": games,
            "wins": wins,
            "raw_wr": raw,
            "smoothed_wr": smoothed,
            "baseline_wr": baseline,
            "lift": lift,
            "avg_lift": avg_lift,
            "residual": lift - avg_lift,
        })

    out: dict[int, dict] = {}
    for cid, rows in by_champ.items():
        rows.sort(key=lambda r: (-r["residual"], -abs(r["lift"]), -r["games"], r["set"]))
        out[cid] = {
            "top": rows[:top_n],
            "bot": sorted(rows, key=lambda r: (r["residual"], r["games"], r["set"]))[:bot_n],
        }
    return out

def build_champ_synergy_index(
    champ_pairs: list[dict],
    *,
    min_games: int,
) -> dict[int, list[dict]]:
    """Per champion, keep same-team teammate rows sorted by synergy lift.

    `lift` is pair WR minus the additive expectation from each champion's
    marginal winrate.  z-score is kept as a confidence tie-breaker, not the
    primary fit metric.
    """
    by_champ: dict[int, list[dict]] = {}
    for row in champ_pairs:
        if row["games"] < min_games:
            continue
        by_champ.setdefault(row["champion_id"], []).append(row)

    for cid, rows in by_champ.items():
        rows.sort(
            key=lambda r: (
                -r["lift"],
                -r["z_score"],
                -r["games"],
                r["teammate_id"],
            )
        )
    return by_champ

def render_html(
    records: list[dict],
    champ_meta: dict[int, dict],
    champ_profiles: dict[int, dict[str, object]],
    champ_picks: dict[int, dict],
    champ_sets: dict[int, dict],
    champ_item_builds: dict[int, dict],
    champ_single_items: dict[int, dict],
    champ_boot_items: dict[int, dict],
    champ_item_clusters: dict[int, dict],
    champ_augment_types: dict[int, dict],
    champ_synergy: dict[int, list[dict]],
    aug_meta: dict[int, dict],
    patch_changes: dict[str, object] | None,
    *,
    queue_id: int,
    patch_prefix: str | None,
    ddragon_version: str,
    total_games: int,
    min_games_per_pair: int,
    min_synergy_games: int,
    site_url: str = "",
    og_image: str = "",
    build_date: str = "",
    cloudflare_analytics_token: str = "",
    ga_measurement_id: str = "",
    payload_out_path: Path | None = None,
    payload_url: str = "",
) -> str:
    # Group champions by tier
    by_tier: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for r in records:
        tier = assign_tier(r["bayes_wr"])
        meta = champ_meta.get(r["champion_id"])
        if meta is None:
            continue
        by_tier[tier].append({**r, **meta})

    header_title, queue_label = _queue_copy(queue_id)
    header_title_en = "ARAM Mayhem Database" if queue_id == 2400 else queue_label
    patch_label = f"patch {patch_prefix}" if patch_prefix else "all patches"

    # Build the JS data payload. Keep it slim: only champs we render + their
    # picked augments / teammate synergy rows + the augment metadata for ids
    # that actually appear.
    used_aug_ids: set[int] = set()
    js_champs: dict[str, dict] = {}

    def _pack(r: dict) -> dict:
        return {
            "id": r["augment_id"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
            "score": round(r.get("rank_score", r["lift"]), 4),
            "lcb": round(r.get("lcb_lift", r["lift"]), 4),
            "pick": round(r.get("pick_rate", 0.0), 4),
            "peerPick": round(r.get("peer_pick_rate", 0.0), 4),
            "pickLift": round(r.get("pick_lift", 0.0), 3),
        }

    def _pack_set(r: dict) -> dict:
        avg_value = float(r.get("avg_lift", r.get("global_lift", 0.0)) or 0.0)
        residual_value = float(r.get("residual", float(r.get("lift", 0.0) or 0.0) - avg_value) or 0.0)
        packed = {
            "name": r.get("set", r.get("name", r["slug"])),
            "name_zh": r.get("set_zh", r.get("name_zh", r.get("set", r.get("name", r["slug"])))),
            "name_en": r.get("set_en", r.get("name_en", r.get("set", r.get("name", r["slug"])))),
            "slug": r["slug"],
            "g": r["games"],
            "wr": round(r["smoothed_wr"], 4),
            "lift": round(r["lift"], 4),
            "avg": round(avg_value, 4),
            "res": round(residual_value, 4),
            "score": round(r.get("rank_score", r.get("lcb_residual", residual_value)), 4),
            "badScore": round(r.get("rank_bad_score", r.get("ucb_residual", residual_value)), 4),
            "pick": round(r.get("pick_rate", 0.0), 4),
            "globalPick": round(r.get("global_pick_rate", 0.0), 4),
            "pickLift": round(r.get("pick_lift", 0.0), 3),
            "pickCredit": round(r.get("pick_rate_credit", 0.0), 4),
            "peerGroup": r.get("peer_group", ""),
            "peerScope": r.get("peer_scope", ""),
        }
        optional_float_fields = {
            "pair_lift": "pairLift",
            "single_lift": "singleLift",
            "global_lift": "globalLift",
            "core_pair_lift": "corePairLift",
            "core_single_lift": "coreSingleLift",
            "flex_single_lift": "flexSingleLift",
            "flex_stability": "flexStability",
        }
        for source_key, dest_key in optional_float_fields.items():
            if source_key in r:
                packed[dest_key] = round(float(r.get(source_key, 0.0)), 4)
        optional_int_fields = {
            "cluster_size": "routeSize",
            "pair_coverage": "pairCoverage",
            "core_pair_coverage": "corePairCoverage",
            "cluster_games": "clusterGames",
            "exact_games": "exactGames",
        }
        for source_key, dest_key in optional_int_fields.items():
            if source_key in r:
                packed[dest_key] = int(r.get(source_key, 0) or 0)
        if r.get("items"):
            packed["items"] = r["items"]
        return packed

    def _pack_comp(profile: dict[str, object]) -> dict:
        return {
            "phys": round(float(profile.get("physical_dpm") or 0.0), 3),
            "magic": round(float(profile.get("magic_dpm") or 0.0), 3),
            "true": round(float(profile.get("true_dpm") or 0.0), 3),
            "wave": round(float(profile.get("wave") or 0.0), 3),
            "cc": round(float(profile.get("cc") or 0.0), 3),
            "engage": round(float(profile.get("engage") or 0.0), 3),
            "damage": round(float(profile.get("damage_score") or 0.0), 3),
            "poke": round(float(profile.get("poke") or 0.0), 3),
            "sustain": round(float(profile.get("sustain") or 0.0), 3),
            "front": round(float(profile.get("front") or 0.0), 3),
        }

    def _pack_role_meta(role_meta: dict[str, dict[str, object]] | None) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for role, info in (role_meta or {}).items():
            out[role] = {
                "role": role,
                "slot": info.get("slot", ""),
                "source": info.get("source", ""),
                "wr": round(float(info.get("wr", 0.0) or 0.0), 4),
                "games": int(info.get("games", 0) or 0),
                "pick": (
                    round(float(info.get("pick_rate", 0.0) or 0.0), 4)
                    if info.get("pick_rate") is not None else None
                ),
                "lift": (
                    round(float(info.get("lift", 0.0) or 0.0), 4)
                    if info.get("lift") is not None else None
                ),
                "score": (
                    round(float(info.get("score", 0.0) or 0.0), 4)
                    if info.get("score") is not None else None
                ),
                "styleSlug": info.get("style_slug", ""),
                "styleName": info.get("style_name", ""),
                "styleNameZh": info.get("style_name_zh", ""),
                "styleNameEn": info.get("style_name_en", ""),
                "roleLabelZh": info.get("role_label_zh", role),
                "roleLabelEn": info.get("role_label_en", role),
            }
        return out

    def _add_search_terms(terms: list[str], *values: object) -> None:
        for value in values:
            if value is None:
                continue
            if isinstance(value, dict):
                _add_search_terms(terms, *value.values())
                continue
            if isinstance(value, (list, tuple, set)):
                _add_search_terms(terms, *value)
                continue
            text = str(value).strip()
            if text:
                terms.append(text)

    def _add_named_rows_for_search(terms: list[str], rows: list[dict]) -> None:
        for row in rows:
            _add_search_terms(
                terms,
                row.get("name"),
                row.get("name_zh"),
                row.get("name_en"),
                row.get("set"),
                row.get("set_zh"),
                row.get("set_en"),
                row.get("slug"),
            )
            for item in row.get("items") or []:
                _add_search_terms(
                    terms,
                    item.get("name"),
                    item.get("name_zh"),
                    item.get("name_en"),
                    item.get("id"),
                )

    def _champ_search_blob(cid: int, display_name: str, meta: dict, tags: list[str]) -> str:
        terms: list[str] = []
        _add_search_terms(
            terms,
            display_name,
            meta.get("name"),
            meta.get("name_zh"),
            meta.get("name_en"),
            meta.get("alias"),
            tags,
        )
        picks_for_champ = champ_picks.get(cid, {"top": {}, "bot": {}})
        for side in ("top", "bot"):
            for rows in picks_for_champ.get(side, {}).values():
                for row in rows:
                    aug = aug_meta.get(int(row.get("augment_id") or 0))
                    if not aug:
                        continue
                    _add_search_terms(
                        terms,
                        aug.get("name"),
                        aug.get("name_zh"),
                        aug.get("name_en"),
                        aug.get("set"),
                        aug.get("set_zh"),
                        aug.get("set_en"),
                    )
                    for aug_set in aug.get("sets") or []:
                        _add_search_terms(
                            terms,
                            aug_set.get("name"),
                            aug_set.get("name_zh"),
                            aug_set.get("name_en"),
                            aug_set.get("slug"),
                        )
            _add_named_rows_for_search(terms, champ_sets.get(cid, {}).get(side, []))
            _add_named_rows_for_search(terms, champ_item_builds.get(cid, {}).get(side, []))
            _add_named_rows_for_search(terms, champ_single_items.get(cid, {}).get(side, []))
            _add_named_rows_for_search(terms, champ_boot_items.get(cid, {}).get(side, []))
            _add_named_rows_for_search(terms, champ_item_clusters.get(cid, {}).get(side, []))
            _add_named_rows_for_search(terms, champ_augment_types.get(cid, {}).get(side, []))
        seen: set[str] = set()
        unique_terms: list[str] = []
        for term in terms:
            normalized = term.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_terms.append(normalized)
        return " ".join(unique_terms)

    visible_cids = [int(r["champion_id"]) for r in records]
    visible_cid_set = set(visible_cids)
    for cid in visible_cids:
        meta = champ_meta.get(cid)
        if meta is None:
            continue
        picks = champ_picks.get(cid, {"top": {}, "bot": {}})
        top_buckets = {}
        bot_buckets = {}
        for rarity in RARITY_ORDER:
            top_rows = picks["top"].get(rarity, [])
            bot_rows = picks["bot"].get(rarity, [])
            for r in top_rows + bot_rows:
                used_aug_ids.add(r["augment_id"])
            top_buckets[rarity] = [_pack(r) for r in top_rows]
            bot_buckets[rarity] = [_pack(r) for r in bot_rows]
        pairs = [
            {
                "id": row["teammate_id"],
                "g": row["games"],
                "wr": round(row["raw_wr"], 4),
                "expected": round(row["expected_wr"], 4),
                "lift": round(row["lift"], 4),
                "z": round(row["z_score"], 3),
            }
            for row in champ_synergy.get(cid, [])
            if row["teammate_id"] in visible_cid_set
        ]
        js_champs[str(cid)] = {
            "name": meta["name"],
            "name_zh": meta.get("name_zh", meta["name"]),
            "name_en": meta.get("name_en", meta.get("alias", meta["name"])),
            "alias": meta.get("alias", ""),
            "image": meta.get("image", ""),
            "tags": meta.get("tags") or [],
            "top": top_buckets,
            "bot": bot_buckets,
            "sets": {
                "top": [_pack_set(r) for r in champ_sets.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_sets.get(cid, {}).get("bot", [])],
            },
            "items": {
                "top": [_pack_set(r) for r in champ_item_builds.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_item_builds.get(cid, {}).get("bot", [])],
            },
            "singleItems": {
                "top": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("bot", [])],
                "popularBad": [_pack_set(r) for r in champ_single_items.get(cid, {}).get("popular_bad", [])],
            },
            "boots": {
                "top": [_pack_set(r) for r in champ_boot_items.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_boot_items.get(cid, {}).get("bot", [])],
            },
            "itemClusters": {
                "top": [_pack_set(r) for r in champ_item_clusters.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_item_clusters.get(cid, {}).get("bot", [])],
            },
            "augTypes": {
                "top": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("top", [])],
                "bot": [_pack_set(r) for r in champ_augment_types.get(cid, {}).get("bot", [])],
            },
            "pairs": pairs,
            "comp": _pack_comp(champ_profiles.get(cid, {})),
            "roleMeta": _pack_role_meta(meta.get("role_meta")),
        }
    js_augs = {
        str(aid): {
            "name": aug_meta[aid]["name"],
            "name_zh": aug_meta[aid].get("name_zh", aug_meta[aid]["name"]),
            "name_en": aug_meta[aid].get("name_en", aug_meta[aid]["name"]),
            "icon": aug_meta[aid]["icon"],
            "rarity": aug_meta[aid].get("rarity", ""),
            "desc": aug_meta[aid].get("desc", ""),
            "desc_zh": aug_meta[aid].get("desc_zh", aug_meta[aid].get("desc", "")),
            "desc_en": aug_meta[aid].get("desc_en", ""),
            "set": aug_meta[aid].get("set", ""),
            "set_zh": aug_meta[aid].get("set_zh", aug_meta[aid].get("set", "")),
            "set_en": aug_meta[aid].get("set_en", aug_meta[aid].get("set", "")),
            "setSlug": aug_meta[aid].get("setSlug", ""),
            "sets": aug_meta[aid].get("sets", []),
            "displayTags": aug_meta[aid].get("displayTags", []),
        }
        for aid in used_aug_ids
        if aid in aug_meta
    }

    css = """
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
        margin: 0;
        background: #0e1116;
        color: #e6e8eb;
        /* Body = Noto Sans TC (modern sans, readable in dense UI).  Serif
           is reserved for small captions — see `.subtitle`,
           `.aug .alift`. */
        font-family: "Noto Sans TC", -apple-system, "Segoe UI",
                     "Microsoft JhengHei", "PingFang TC", sans-serif;
        padding: 32px 24px 64px;
    }
    h1 { margin: 0; font-weight: 600; font-size: 22px; line-height: 1.1; }
    /* Mincho-only captions — opt-in serif for the three small metadata
       lines the user picked out: page subtitle, detail-panel sub-heading,
       and augment card's lift/games row. */
    .subtitle,
    .aug .alift {
        font-family: "Noto Serif TC", "Source Han Serif TC",
                     "PingFang TC", "PMingLiU", "Songti TC", serif;
    }
    .subtitle { color: #9aa0a6; font-size: 13px; }
    .title-patch {
        font-family: "Noto Sans TC", -apple-system, "Segoe UI",
                     "Microsoft JhengHei", "PingFang TC", sans-serif;
        font-size: 14px;
        font-weight: 500;
        line-height: 1;
        white-space: nowrap;
        color: #8d96a0;
    }
    /* Top header row — title on the left, GitHub star CTA on the right. */
    .page-header {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: start;
        gap: 10px 12px;
        margin-bottom: 14px;
    }
    .page-header > div:first-child { min-width: 0; }
    .title-meta {
        display: flex;
        align-items: baseline;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 0;
    }
    .page-actions {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        flex-wrap: nowrap;
        justify-self: end;
        align-self: start;
    }
    .page-actions .icon-btn,
    .page-actions .gh-star {
        min-height: auto;
        padding: 0 0 2px;
        background: transparent;
        border: 0;
        border-radius: 0;
    }
    .page-actions .icon-btn:hover,
    .page-actions .gh-star:hover {
        background: transparent;
        border-color: transparent;
        color: #e6e8eb;
    }
    .tool-btn.header-update-tab {
        min-height: auto;
        padding: 0 0 2px;
        font-size: 11px;
        font-weight: 600;
        background: transparent;
        border: 0;
        border-bottom: 1px solid #f5d780;
        border-radius: 0;
        color: #f5d780;
        line-height: 1.2;
    }
    .app-shell {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 24px;
        align-items: start;
    }
    .app-shell.with-side-panel {
        grid-template-columns: minmax(0, 1fr) minmax(480px, 520px);
    }
    .main-col { min-width: 0; }
    .icon-btn,
    .gh-star {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        background: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        text-decoration: none;
        white-space: nowrap;
        transition: background 0.12s, border-color 0.12s;
    }
    .icon-btn {
        cursor: pointer;
        font: inherit;
    }
    .icon-btn:hover,
    .gh-star:hover { background: #30363d; border-color: #58606b; }
    .icon-btn svg,
    .gh-star svg { flex-shrink: 0; }
    .gh-star {
        width: 36px;
        justify-content: center;
        padding: 6px 0;
    }
    .page-actions .gh-star {
        width: auto;
        justify-content: flex-start;
    }
    .lang-toggle { min-width: 56px; justify-content: center; }
    .lang-toggle span { font-size: 12px; letter-spacing: 0; }
    /* Filter bar: role chips + free-text search + live count. */
    .filter-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        margin: 0 0 20px;
        padding: 10px 12px;
        background: #161a22;
        border-radius: 10px;
    }
    .role-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
    }
    .chip {
        padding: 5px 12px;
        background: #1f2530;
        color: #c5cad3;
        border: 1px solid transparent;
        border-radius: 18px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        font-family: inherit;
        transition: background 0.1s;
    }
    .chip:hover { background: #2a3142; }
    .chip.active {
        background: var(--role-color, #f5c518);
        color: #0e1116;
        border-color: var(--role-color, #f5c518);
    }
    .chip[data-role=""]              { --role-color: #f5c518; }
    .chip[data-role="Assassin"]      { --role-color: #ef4444; }
    .chip[data-role="Fighter"]       { --role-color: #f97316; }
    .chip[data-role="Mage"]          { --role-color: #3b82f6; }
    .chip[data-role="Marksman"]      { --role-color: #22c55e; }
    .chip[data-role="Support"]       { --role-color: #ec4899; }
    .chip[data-role="Tank"]          { --role-color: #a855f7; }
    .filter-tools {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-left: auto;
        flex: 1;
        justify-content: flex-end;
    }
    .tool-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 34px;
        padding: 6px 12px;
        background: #21262d;
        color: #e6e8eb;
        border: 1px solid #30363d;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
        transition: background 0.12s, border-color 0.12s, color 0.12s;
    }
    .tool-btn:hover { background: #2a3142; border-color: #58606b; }
    .tool-btn.active {
        background: #f5d780;
        border-color: #f5d780;
        color: #231802;
    }
    .tool-btn.ghost {
        background: transparent;
        color: #c5cad3;
    }
    .tool-btn.ghost:hover {
        background: rgba(255,255,255,0.04);
    }
    .tool-btn.update-tab {
        position: relative;
        border-color: #f5d780;
        color: #f5d780;
    }
    .tool-btn.update-tab[aria-expanded="true"] {
        background: transparent;
        color: #f8e39f;
    }
    .tool-btn.header-update-tab:hover {
        background: transparent;
        border-color: #f8e39f;
        color: #f8e39f;
    }
    .search-row { display: contents; }
    .updates-panel {
        margin: 0 0 18px;
        padding: 14px 16px 16px;
        background: #11151d;
        border: 1px solid #30363d;
        border-radius: 10px;
        box-shadow: 0 10px 26px rgba(0,0,0,0.18);
    }
    .updates-panel.is-hidden { display: none; }
    .updates-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 10px;
    }
    .updates-kicker {
        display: block;
        margin-bottom: 2px;
        color: #f5d780;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0;
    }
    .updates-title {
        margin: 0;
        color: #e6e8eb;
        font-size: 15px;
        font-weight: 700;
    }
    .updates-close {
        width: 30px;
        height: 30px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        border: 1px solid #30363d;
        border-radius: 999px;
        background: #1b2030;
        color: #c5cad3;
        font: inherit;
        font-size: 16px;
        font-weight: 700;
        line-height: 1;
        cursor: pointer;
    }
    .updates-close:hover {
        background: #2a3142;
        border-color: #58606b;
    }
    .updates-list {
        display: grid;
        gap: 8px;
        margin: 0;
        padding: 0;
        list-style: none;
    }
    .updates-list li {
        position: relative;
        padding-left: 14px;
        color: #c5cad3;
        font-size: 12px;
        line-height: 1.6;
    }
    .updates-list li::before {
        content: "";
        position: absolute;
        left: 0;
        top: 0.72em;
        width: 5px;
        height: 5px;
        border-radius: 999px;
        background: #f5d780;
    }
    .change-summary {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 0 0 12px;
    }
    .change-chip {
        display: inline-flex;
        align-items: center;
        min-height: 24px;
        padding: 3px 8px;
        border: 1px solid rgba(245, 215, 128, 0.28);
        border-radius: 999px;
        color: #d8dce3;
        background: rgba(245, 215, 128, 0.06);
        font-size: 11px;
        line-height: 1.2;
    }
    .change-tabs {
        display: flex;
        gap: 6px;
        margin: 2px 0 12px;
        overflow-x: auto;
        scrollbar-width: none;
    }
    .change-tabs::-webkit-scrollbar { display: none; }
    .change-tab {
        flex: 0 0 auto;
        min-height: 32px;
        padding: 5px 10px;
        border: 1px solid #30363d;
        border-radius: 999px;
        background: #1b2030;
        color: #c5cad3;
        font: inherit;
        font-size: 12px;
        font-weight: 700;
        cursor: pointer;
    }
    .change-tab:hover {
        border-color: #58606b;
        background: #21283a;
    }
    .change-tab.active {
        border-color: #f5d780;
        background: #f5d780;
        color: #231802;
    }
    .change-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 14px;
    }
    .change-column-title {
        margin: 0 0 8px;
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 800;
    }
    .change-list {
        display: grid;
        gap: 6px;
    }
    .change-row {
        width: 100%;
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        gap: 9px;
        align-items: center;
        min-height: 44px;
        padding: 7px 9px;
        border: 1px solid rgba(148, 163, 184, 0.14);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.025);
        color: inherit;
        text-align: left;
        font: inherit;
    }
    button.change-row {
        cursor: pointer;
    }
    button.change-row:hover {
        border-color: rgba(245, 215, 128, 0.38);
        background: rgba(245, 215, 128, 0.045);
    }
    .change-icon,
    .change-duo img {
        width: 34px;
        height: 34px;
        border-radius: 6px;
        object-fit: cover;
        background: #0e1116;
    }
    .change-duo {
        display: inline-flex;
        align-items: center;
    }
    .change-duo img + img {
        margin-left: -9px;
        box-shadow: -2px 0 0 #11151d;
    }
    .change-name {
        min-width: 0;
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 700;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .change-meta {
        display: block;
        margin-top: 2px;
        color: #8d96a0;
        font-size: 10px;
        line-height: 1.35;
    }
    .change-delta {
        justify-self: end;
        min-width: 56px;
        padding: 3px 6px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 800;
        text-align: center;
    }
    .change-delta.up {
        color: #11151d;
        background: #7ddc8a;
    }
    .change-delta.down {
        color: #180f12;
        background: #ff9aa5;
    }
    .change-empty {
        color: #9aa0a6;
        font-size: 12px;
        line-height: 1.6;
    }
    .search-wrap {
        position: relative;
        max-width: none;
        min-width: 0;
    }
    .search-wrap svg {
        position: absolute;
        left: 10px;
        top: 50%;
        transform: translateY(-50%);
        color: #6b7280;
        pointer-events: none;
    }
    .search-wrap:focus-within svg { color: #9aa0a6; }
    .search {
        width: 100%;
        height: 40px;
        padding: 0 12px 0 30px;
        background: rgba(2, 6, 23, 0.72);
        color: #e6e8eb;
        border: 1px solid rgba(148, 163, 184, 0.18);
        border-radius: 12px;
        font-size: 14px;
        font-family: inherit;
        outline: none;
        transition: border-color .12s, box-shadow .12s;
    }
    .search:focus {
        border-color: rgba(148, 163, 184, 0.4);
        box-shadow: 0 0 0 3px rgba(88,96,107,0.16);
    }
    .shown-count {
        color: #9fb3d9;
        font-size: 13px;
        font-weight: 700;
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
    }
    .shown-count #shown-n {
        color: #e6e8eb;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
    }
    .side-panel {
        position: sticky;
        top: 24px;
        max-height: calc(100vh - 48px);
        overflow-y: auto;
        overscroll-behavior: contain;
        scrollbar-gutter: stable;
        background: #11151d;
        border: 1px solid #1f2530;
        border-radius: 12px;
        padding: 14px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.22);
    }
    .side-panel.is-modal-open {
        display: block;
    }
    .side-panel.is-hidden {
        display: none;
    }
    .side-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
    }
    .side-head h2 {
        margin: 0 0 4px;
        font-size: 16px;
        font-weight: 600;
    }
    .side-close,
    .detail-close,
    .rec-fab {
        border: 1px solid #30363d;
        background: #1b2030;
        color: #e6e8eb;
        font-family: inherit;
        font-weight: 700;
        cursor: pointer;
    }
    .side-close,
    .detail-close {
        display: none;
        width: 34px;
        height: 34px;
        border-radius: 999px;
        align-items: center;
        justify-content: center;
        font-size: 18px;
        line-height: 1;
        flex-shrink: 0;
    }
    .rec-fab {
        display: none;
        position: fixed;
        right: 14px;
        bottom: 14px;
        z-index: 40;
        min-height: 46px;
        padding: 0 16px;
        border-radius: 999px;
        box-shadow: 0 12px 30px rgba(0,0,0,0.36);
    }
    .rec-fab:not(.is-hidden) {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
    }
    .side-sub {
        color: #9aa0a6;
        font-size: 12px;
        line-height: 1.55;
    }
    .pick-slots {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 14px 0 10px;
    }
    .pick-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        min-height: 36px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid #30363d;
        background: #1b2030;
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
    }
    .pick-chip img {
        width: 22px;
        height: 22px;
        border-radius: 999px;
        display: block;
        object-fit: cover;
        background: #2a3142;
        border: 1px solid rgba(255,255,255,0.08);
        flex-shrink: 0;
    }
    .pick-chip.empty {
        border-style: dashed;
        color: #6b7280;
        background: transparent;
        cursor: default;
    }
    .pick-chip .ord {
        width: 18px;
        height: 18px;
        border-radius: 999px;
        background: #f5d780;
        color: #231802;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 700;
        flex-shrink: 0;
    }
    .pick-note {
        min-height: 18px;
        color: #9aa0a6;
        font-size: 11px;
        margin-bottom: 10px;
    }
    .rec-list {
        display: grid;
        gap: 8px;
    }
    .panel-empty {
        color: #6b7280;
        font-size: 12px;
        line-height: 1.6;
        padding: 8px 0 4px;
    }
    .rec-row {
        display: grid;
        grid-template-columns: 22px 40px 1fr;
        gap: 8px;
        align-items: center;
        width: 100%;
        padding: 8px;
        border-radius: 10px;
        background: #1b2030;
        border: 1px solid rgba(255,255,255,0.05);
        cursor: pointer;
        font: inherit;
        text-align: left;
        transition: background 0.12s, border-color 0.12s, transform 0.08s;
    }
    .rec-row:hover {
        background: #20263a;
        border-color: rgba(245,215,128,0.28);
        transform: translateY(-1px);
    }
    .rec-row.least-fit {
        background: linear-gradient(135deg, rgba(54, 24, 30, 0.82), #1b2030 72%);
        border-color: rgba(255,138,138,0.22);
    }
    .rec-row.least-fit:hover {
        background: linear-gradient(135deg, rgba(66, 28, 35, 0.9), #20263a 72%);
        border-color: rgba(255,138,138,0.38);
    }
    .rec-rank {
        color: #9aa0a6;
        font-size: 11px;
        font-weight: 700;
        text-align: center;
        font-variant-numeric: tabular-nums;
    }
    .rec-row.least-fit .rec-rank { color: #ff8a8a; }
    .rec-row img {
        width: 40px;
        height: 40px;
        border-radius: 8px;
        display: block;
        background: #2a3142;
    }
    .rec-main {
        display: grid;
        gap: 4px;
        min-width: 0;
    }
    .rec-titleline {
        display: grid;
        grid-template-columns: minmax(96px, 1fr) auto;
        align-items: center;
        gap: 8px;
        min-width: 0;
    }
    .rec-name {
        display: block;
        color: #e6e8eb;
        font-size: 13px;
        font-weight: 600;
        line-height: 1.25;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .rec-meta {
        display: grid;
        gap: 3px;
        margin-top: 2px;
        color: #9aa0a6;
        font-size: 11px;
        line-height: 1.35;
        font-variant-numeric: tabular-nums;
        min-width: 0;
    }
    .rec-scoreline {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 5px;
    }
    .rec-score {
        color: oklch(0.76 0.09 92);
        font-weight: 700;
        justify-self: end;
        white-space: nowrap;
    }
    .rec-score.fit-top {
        color: oklch(0.91 0.13 92);
        text-shadow: 0 0 12px rgba(245, 215, 128, 0.24);
    }
    .rec-score.fit-strong { color: oklch(0.84 0.12 92); }
    .rec-score.fit-solid { color: oklch(0.76 0.09 92); }
    .rec-score.fit-soft { color: oklch(0.68 0.06 92); }
    .rec-score.fit-floor { color: #9aa0a6; }
    .rec-score.fit-worst { color: #ff8a8a; }
    .rec-detail {
        display: grid;
        grid-template-columns: auto auto auto;
        justify-content: start;
        gap: 8px;
        align-items: center;
        min-width: 0;
        white-space: nowrap;
    }
    .rec-detail .good,
    .rec-meta .z {
        color: #6bd16b;
        font-weight: 700;
    }
    .rec-detail .bad {
        color: #ff8a8a;
        font-weight: 700;
    }
    .rec-detail .muted {
        color: #9aa0a6;
    }
    /* Empty filter state — surfaces when role × search yields zero champs.
       Mincho italic to match the caption typography elsewhere, deliberately
       gentle (not an error) since nothing actually broke. */
    .empty-state {
        display: none;
        margin: 32px auto;
        max-width: 480px;
        padding: 24px;
        text-align: center;
        color: #9aa0a6;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
        font-size: 14px;
        font-style: italic;
        line-height: 1.6;
    }
    .empty-state.visible { display: block; }
    .empty-state strong {
        display: block;
        margin-bottom: 4px;
        color: #c5cad3;
        font-style: normal;
        font-weight: 600;
        font-size: 16px;
    }
    .tier-block { margin-bottom: 22px; position: relative; }
    .tier-block.hidden { display: none; }
    /* Tier name on its own line above the grid (replaces the old left-side
       full-height ornament bar).  A hairline rule tinted with the tier's
       colour trails the heading, visually anchoring the grid to the pill. */
    .tier-heading {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 16px 0 10px;
        padding-bottom: 8px;
        font-size: 14px;
        font-weight: 600;
        border-bottom: 1px solid color-mix(in oklab, var(--tier-color, #555) 30%, transparent);
    }
    /* OP block: faint radial wash behind the grid to elevate the apex tier
       without resorting to a full coloured backdrop.  Same trick on T1 with
       warmer hue and lower alpha. */
    .tier-block[data-tier="OP"] {
        background:
            radial-gradient(ellipse 70% 60% at 50% 60%,
                rgba(216,184,255,0.045) 0%, transparent 75%);
        border-radius: 12px;
        padding: 2px 6px 8px;
    }
    .tier-block[data-tier="T1"] {
        background:
            radial-gradient(ellipse 70% 60% at 50% 60%,
                rgba(255,120,80,0.028) 0%, transparent 75%);
        border-radius: 12px;
        padding: 2px 6px 8px;
    }
    .tier-pill {
        position: relative;
        overflow: hidden;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px 16px;
        border-radius: 6px;
        color: #0e1116;
        background: var(--tier-bg);
        font-size: 16px;
        font-weight: 700;
        text-shadow: 0 1px 0 rgba(255,255,255,0.25);
        letter-spacing: 0.3px;
    }
    .tier-pill > span { position: relative; z-index: 2; }
    .tier-count { color: #9aa0a6; font-size: 12px; font-weight: 400; }
    /* Prismatic / pearl shine for the OP tier — animated highlight sweep +
       outer halo glow, matching the iridescent augment-card look. */
    .tier-block[data-tier="OP"] .tier-pill {
        background-size: 200% 200%;
        animation: prismShift 6s ease-in-out infinite;
        box-shadow:
            0 0 12px rgba(220,180,255,0.55),
            0 0 28px rgba(170,210,255,0.30),
            inset 0 0 0 1px rgba(255,255,255,0.55);
        color: #2a1a4a;
        text-shadow: 0 1px 0 rgba(255,255,255,0.8);
    }
    .tier-block[data-tier="OP"] .tier-pill::before {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(115deg,
            transparent 35%,
            rgba(255,255,255,0.75) 50%,
            transparent 65%);
        background-size: 220% 100%;
        animation: shineSweep 3.2s linear infinite;
        z-index: 1;
        pointer-events: none;
    }
    @keyframes prismShift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    @keyframes shineSweep {
        from { background-position: 220% 0; }
        to   { background-position: -120% 0; }
    }
    .tier-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
        gap: 10px;
    }
    .champ {
        position: relative;
        aspect-ratio: 1 / 1;
        border-radius: 8px;
        overflow: visible;
        background: #1f2530;
        /* Champion thumbnail wears its tier's colour as a 2px frame.
           Non-OP tiers use a solid border; OP gets a prismatic gradient
           via the .tier-block[data-tier="OP"] .champ rule below. */
        border: 2px solid var(--tier-color, #555);
        cursor: pointer;
        transition: transform .08s, box-shadow .08s, filter .08s;
    }
    .champ:hover { transform: translateY(-1px); }
    .champ.detail-selected {
        transform: translateY(-2px);
        filter: brightness(1.08);
        box-shadow: 0 0 0 1px #fff, 0 6px 16px rgba(0,0,0,0.6);
    }
    .champ.pick-selected {
        box-shadow:
            inset 0 0 0 2px rgba(245,215,128,0.95),
            0 0 0 1px rgba(245,215,128,0.35),
            0 6px 16px rgba(0,0,0,0.38);
    }
    .champ.pick-selected::before {
        content: attr(data-pick-rank);
        position: absolute;
        top: 4px;
        left: 4px;
        width: 18px;
        height: 18px;
        border-radius: 999px;
        background: #f5d780;
        color: #231802;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 800;
        z-index: 4;
        box-shadow: 0 1px 6px rgba(0,0,0,0.35);
    }
    /* OP-tier champions get the "棱彩飾框" — Prismatic decorative frame —
       so they're as visually distinct from T1 as Prismatic augments are
       from Gold ones.  Double-background trick: inner dark colour clips to
       padding-box, iridescent gradient renders on border-box, the transparent
       2px border lets the gradient show.  prismShift animates the hue. */
    .tier-block[data-tier="OP"] .champ {
        border-color: transparent;
        background:
            linear-gradient(#1f2530, #1f2530) padding-box,
            linear-gradient(135deg,
                #ffffff 0%, #e7d5ff 18%, #bcd6ff 36%,
                #ffd5ec 58%, #fff1c8 78%, #ffffff 100%) border-box;
        background-size: auto, 220% 220%;
        animation: prismShift 6s ease-in-out infinite;
        box-shadow: 0 0 8px rgba(220,180,255,0.45);
    }
    /* T1 = "premium red" — solid red would just look like a flat tier band,
       so promote it with a hot-coal gradient (orange-red → deep crimson →
       warm highlight), a slow shimmer (slower than OP so the hierarchy is
       legible), and a subtle red halo.  Reads as "valuable but not OP". */
    .tier-block[data-tier="T1"] .champ {
        border-color: transparent;
        background:
            linear-gradient(#1f2530, #1f2530) padding-box,
            linear-gradient(135deg,
                #ffb380 0%,   /* hot orange highlight */
                #ff5a3c 32%,  /* main red-orange */
                #c8262c 62%,  /* deep crimson */
                #ff8050 100%  /* warm trailing highlight */
            ) border-box;
        background-size: auto, 220% 220%;
        animation: prismShift 9s ease-in-out infinite;
        box-shadow: 0 0 6px rgba(255,90,60,0.42);
    }
    .champ img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
        border-radius: 6px;
    }
    .alt-role-badge {
        --badge-color: #f5d780;
        position: absolute;
        top: 4px;
        right: 4px;
        width: 18px;
        height: 18px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
        border: 1px solid var(--badge-color);
        background: rgba(9, 14, 22, 0.9);
        box-shadow:
            0 0 0 1px rgba(0,0,0,0.18),
            0 2px 8px rgba(0,0,0,0.34);
        opacity: 0;
        transform: translateY(-1px) scale(0.96);
        transition: opacity 0.12s ease-out, transform 0.12s ease-out, box-shadow 0.12s ease-out, border-color 0.12s ease-out;
        pointer-events: auto;
        z-index: 4;
        cursor: help;
    }
    .alt-role-badge svg {
        width: 10px;
        height: 10px;
        display: block;
        color: var(--badge-color);
        filter: drop-shadow(0 1px 2px rgba(0,0,0,0.22));
    }
    .alt-role-badge[data-alt-role="Assassin"] { --badge-color: #D94A5F; }
    .alt-role-badge[data-alt-role="Fighter"] { --badge-color: #D9822B; }
    .alt-role-badge[data-alt-role="Mage"] { --badge-color: #9B7CF6; }
    .alt-role-badge[data-alt-role="Marksman"] { --badge-color: #4FB06D; }
    .alt-role-badge[data-alt-role="Support"] { --badge-color: #D96BAA; }
    .alt-role-badge[data-alt-role="Tank"] { --badge-color: #5B8DEF; }
    .champ.secondary-role-match .alt-role-badge {
        opacity: 1;
        transform: translateY(0) scale(1);
    }
    .champ.secondary-role-match:hover .alt-role-badge,
    .champ.secondary-role-match:focus-visible .alt-role-badge {
        box-shadow:
            0 0 0 1px rgba(0,0,0,0.18),
            0 0 10px color-mix(in srgb, var(--badge-color) 28%, transparent),
            0 2px 8px rgba(0,0,0,0.34);
    }
    .alt-role-tooltip {
        position: absolute;
        right: 0;
        bottom: calc(100% + 8px);
        display: flex;
        align-items: center;
        gap: 7px;
        min-width: max-content;
        max-width: min(260px, calc(100vw - 28px));
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.08);
        background: #0b0e13;
        box-shadow: 0 6px 18px rgba(0,0,0,0.55);
        color: #c5cad3;
        font-size: 11px;
        line-height: 1.35;
        white-space: nowrap;
        pointer-events: none;
        opacity: 0;
        transform: translateY(2px);
        transition: opacity 0.12s ease-out, transform 0.12s ease-out;
        z-index: 56;
    }
    .alt-role-tooltip::after {
        content: "";
        position: absolute;
        top: 100%;
        right: 10px;
        border: 6px solid transparent;
        border-top-color: #0b0e13;
    }
    .alt-role-badge.tip-right .alt-role-tooltip {
        left: 0;
        right: auto;
    }
    .alt-role-badge.tip-right .alt-role-tooltip::after {
        left: 10px;
        right: auto;
    }
    .alt-role-badge.tip-below .alt-role-tooltip {
        top: calc(100% + 8px);
        bottom: auto;
    }
    .alt-role-badge.tip-below .alt-role-tooltip::after {
        top: auto;
        bottom: 100%;
        border-top-color: transparent;
        border-bottom-color: #0b0e13;
    }
    .champ.secondary-role-match:hover .alt-role-tooltip,
    .champ.secondary-role-match:focus-visible .alt-role-tooltip,
    .alt-role-badge:hover .alt-role-tooltip {
        opacity: 1;
        transform: translateY(0);
    }
    .alt-role-tooltip-style {
        color: #e6e8eb;
        font-weight: 600;
    }
    .alt-role-tooltip-pick {
        color: #9aa0a6;
        font-variant-numeric: tabular-nums;
    }
    .alt-role-tooltip-lift {
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }
    .alt-role-tooltip-lift.is-good { color: #6bd16b; }
    .alt-role-tooltip-lift.is-bad { color: #ff8a8a; }
    .alt-role-tooltip-lift.is-even { color: #d7dde7; }
    .champ.hidden { display: none; }
    .champ .wr {
        position: absolute;
        left: 2px;
        bottom: 2px;
        font-size: 10px;
        font-weight: 600;
        padding: 1px 4px;
        border-radius: 3px;
        color: #e6e8eb;
        background: rgba(14,17,22,0.78);
    }
    .champ .name {
        position: absolute;
        left: 0; right: 0; bottom: 0;
        padding: 2px 4px;
        font-size: 10px;
        text-align: center;
        background: linear-gradient(to top, rgba(0,0,0,0.85), rgba(0,0,0,0));
        color: #e6e8eb;
        border-bottom-left-radius: 6px;
        border-bottom-right-radius: 6px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        pointer-events: none;
        opacity: 0;
        transition: opacity .15s;
    }
    .champ:hover .name { opacity: 1; }
    .detail-host {
        /* Sits inside .tier-grid; when populated, spans every grid column so
           it appears as a full-width row right after the clicked champion. */
        grid-column: 1 / -1;
    }
    .detail-host:empty { display: none; }
    /* Visually hidden but kept in the DOM as text — so browser Find on Page
       (Ctrl+F / Cmd+F) can still match English aliases like "Aatrox" while
       only the localized zh-TW name is visually drawn. */
    .sr-only {
        position: absolute;
        width: 1px; height: 1px;
        padding: 0; margin: -1px;
        overflow: hidden;
        clip: rect(0,0,0,0);
        white-space: nowrap;
        border: 0;
    }
    .detail {
        margin: 6px 0 4px;
        background: #1b2030;
        border-radius: 10px;
        padding: 16px 18px 18px;
        position: relative;
        animation: slideDown .18s ease-out;
    }
    .detail-close {
        position: absolute;
        top: 10px;
        right: 10px;
        z-index: 1;
        font-size: 18px;
        line-height: 1;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }
    .detail-head {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 16px;
    }
    .detail-avatar {
        width: 34px;
        height: 34px;
        border-radius: 8px;
        object-fit: cover;
        border: 1px solid rgba(255,255,255,0.12);
        box-shadow: 0 4px 10px rgba(0,0,0,0.24);
        flex: 0 0 auto;
    }
    .detail-head .cname { font-size: 16px; font-weight: 600; }
    .detail-tab-input {
        position: absolute;
        width: 1px;
        height: 1px;
        opacity: 0;
        pointer-events: none;
    }
    .detail-tab-list {
        display: flex;
        align-items: center;
        gap: 6px;
        min-width: 0;
        overflow-x: auto;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        scrollbar-width: none;
    }
    .detail-tab-list::-webkit-scrollbar {
        display: none;
    }
    .detail-tab-label {
        flex: 0 0 auto;
        min-height: 38px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0 12px;
        border-bottom: 2px solid transparent;
        color: #9aa0a6;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0;
        cursor: pointer;
        user-select: none;
        white-space: nowrap;
    }
    .detail-tab-label:hover {
        color: #dce4ef;
        background: rgba(255,255,255,0.03);
    }
    .detail-tab-label:focus-visible {
        outline: 2px solid rgba(107,209,255,0.45);
        outline-offset: -2px;
    }
    .detail-tabset > .detail-tab-input:nth-of-type(1):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(1),
    .detail-tabset > .detail-tab-input:nth-of-type(2):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(2),
    .detail-tabset > .detail-tab-input:nth-of-type(3):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(3),
    .detail-tabset > .detail-tab-input:nth-of-type(4):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(4),
    .detail-tabset > .detail-tab-input:nth-of-type(5):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(5),
    .detail-tabset > .detail-tab-input:nth-of-type(6):focus-visible ~ .detail-tab-list .detail-tab-label:nth-child(6) {
        outline: 2px solid rgba(107,209,255,0.45);
        outline-offset: -2px;
    }
    .detail-tab-panels {
        min-width: 0;
    }
    .detail-tab-panel {
        display: none;
        padding-top: 16px;
    }
    .detail-sub-tabs {
        margin-top: 0;
    }
    .detail-sub-tabs .detail-tab-list {
        border-bottom-color: rgba(255,255,255,0.055);
    }
    .detail-sub-tabs .detail-tab-label {
        min-height: 34px;
        padding: 0 11px;
        font-size: 11px;
        font-weight: 700;
    }
    .detail-tabset > .detail-tab-input:nth-of-type(1):checked ~ .detail-tab-list .detail-tab-label:nth-child(1),
    .detail-tabset > .detail-tab-input:nth-of-type(2):checked ~ .detail-tab-list .detail-tab-label:nth-child(2),
    .detail-tabset > .detail-tab-input:nth-of-type(3):checked ~ .detail-tab-list .detail-tab-label:nth-child(3),
    .detail-tabset > .detail-tab-input:nth-of-type(4):checked ~ .detail-tab-list .detail-tab-label:nth-child(4),
    .detail-tabset > .detail-tab-input:nth-of-type(5):checked ~ .detail-tab-list .detail-tab-label:nth-child(5),
    .detail-tabset > .detail-tab-input:nth-of-type(6):checked ~ .detail-tab-list .detail-tab-label:nth-child(6) {
        color: #7fc8ff;
        border-bottom-color: #3aa0ff;
        background: rgba(58,160,255,0.08);
    }
    .detail-tabset > .detail-tab-input:nth-of-type(1):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(1),
    .detail-tabset > .detail-tab-input:nth-of-type(2):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(2),
    .detail-tabset > .detail-tab-input:nth-of-type(3):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(3),
    .detail-tabset > .detail-tab-input:nth-of-type(4):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(4),
    .detail-tabset > .detail-tab-input:nth-of-type(5):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(5),
    .detail-tabset > .detail-tab-input:nth-of-type(6):checked ~ .detail-tab-panels > .detail-tab-panel:nth-child(6) {
        display: block;
    }
    .detail-tab-panel > .detail-section:first-child {
        margin-top: 0;
        padding-top: 0;
        border-top: 0;
    }
    .detail-section + .detail-section {
        margin-top: 18px;
        padding-top: 14px;
        border-top: 1px solid rgba(255,255,255,0.06);
    }
    .detail-section-head {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 10px;
    }
    .detail-section-head h3 {
        margin: 0;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .section-meta {
        color: #9aa0a6;
        font-size: 11px;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
    }
    .augment-strength-meta {
        display: flex;
        align-items: center;
        gap: 5px;
        margin: -2px 0 10px;
    }
    .meta-help-wrap {
        position: relative;
        display: inline-flex;
        align-items: center;
    }
    .meta-help {
        width: 15px;
        height: 15px;
        border-radius: 999px;
        border: 1px solid rgba(154,160,166,0.58);
        background: #1b2030;
        color: #c5cad3;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        font: inherit;
        font-size: 10px;
        line-height: 1;
        cursor: help;
    }
    .meta-help:hover,
    .meta-help:focus-visible {
        border-color: rgba(245,215,128,0.72);
        color: #f5d780;
    }
    .meta-help-tip {
        position: absolute;
        left: 50%;
        bottom: calc(100% + 7px);
        transform: translateX(-50%);
        width: min(260px, calc(100vw - 32px));
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.08);
        background: #0b0e13;
        color: #d4dae4;
        box-shadow: 0 6px 18px rgba(0,0,0,0.55);
        font-family: "Noto Sans TC", -apple-system, "Segoe UI", sans-serif;
        font-size: 11px;
        line-height: 1.5;
        text-align: left;
        pointer-events: none;
        opacity: 0;
        z-index: 52;
        transition: opacity 0.12s ease-out;
    }
    .meta-help-tip::after {
        content: "";
        position: absolute;
        top: 100%;
        left: 50%;
        transform: translateX(-50%);
        border: 6px solid transparent;
        border-top-color: #0b0e13;
    }
    .meta-help-wrap:hover .meta-help-tip,
    .meta-help:focus-visible + .meta-help-tip {
        opacity: 1;
    }
    .aug-set-summary {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(143, 216, 244, 0.10);
        border: 1px solid rgba(143, 216, 244, 0.24);
        color: #c9eefa;
        font-size: 10px;
        line-height: 1.35;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        cursor: help;
    }
    .aug-set-summary.bad {
        background: rgba(255, 125, 125, 0.09);
        border-color: rgba(255, 125, 125, 0.24);
        color: #ffd1d1;
    }
    .aug-set-summary .sum-item {
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fit-chip-list {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 6px;
        min-height: 24px;
    }
    .item-build-carousel {
        display: flex;
        gap: 8px;
        min-width: 0;
        overflow-x: auto;
        overscroll-behavior-inline: contain;
        scroll-snap-type: x proximity;
        scrollbar-width: thin;
        scrollbar-color: rgba(148, 163, 184, 0.45) transparent;
        padding: 0 4px 8px 0;
        -webkit-overflow-scrolling: touch;
    }
    .item-build-carousel::-webkit-scrollbar { height: 7px; }
    .item-build-carousel::-webkit-scrollbar-track { background: transparent; }
    .item-build-carousel::-webkit-scrollbar-thumb {
        background: rgba(148, 163, 184, 0.35);
        border-radius: 999px;
    }
    .item-build-carousel.item-build-grid {
        display: grid;
        align-items: stretch;
        overflow-x: visible;
        scroll-snap-type: none;
        padding: 0;
    }
    .item-build-carousel.single-item-grid {
        grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
    }
    .item-build-carousel.item-pair-grid {
        grid-template-columns: repeat(auto-fill, minmax(76px, 92px));
        justify-content: start;
    }
    .item-build-carousel.item-cluster-grid {
        grid-template-columns: repeat(auto-fill, minmax(176px, 176px));
        justify-content: start;
    }
    .item-build-card {
        flex: 0 0 76px;
        scroll-snap-align: start;
        display: grid;
        grid-template-rows: auto auto 1fr;
        overflow: hidden;
        border-radius: 8px;
        border: 1px solid rgba(107, 209, 107, 0.24);
        background: #11151d;
        color: #e6e8eb;
        text-align: center;
        outline: none;
    }
    .item-build-card.search-hit {
        border-color: #f5c518;
        background: #171711;
        box-shadow:
            0 0 0 2px rgba(245, 197, 24, 0.36),
            0 10px 24px rgba(245, 197, 24, 0.16);
        transform: translateY(-2px);
    }
    .item-build-card.search-hit .item-build-icons {
        background: rgba(245, 197, 24, 0.14);
    }
    .item-build-card.search-hit .item-build-name span,
    .item-build-card.search-hit .item-build-wr {
        color: #ffe58a;
    }
    .item-build-card:focus-visible {
        box-shadow: 0 0 0 2px rgba(255,255,255,0.32);
    }
    .item-build-card.single-item-card {
        flex-basis: 68px;
    }
    .item-build-grid .item-build-card {
        flex: initial;
        min-width: 0;
        scroll-snap-align: unset;
    }
    .item-build-card.item-cluster-card {
        flex-basis: 176px;
        text-align: left;
    }
    .item-build-icons {
        display: grid;
        justify-items: center;
        align-content: center;
        gap: 4px;
        min-height: 90px;
        padding: 4px;
        background: #0f131b;
    }
    .single-item-card .item-build-icons {
        grid-template-columns: 1fr;
        min-height: 50px;
    }
    .item-cluster-card .item-build-icons {
        grid-template-columns: repeat(3, 1fr);
        min-height: 112px;
        padding: 8px;
        gap: 6px;
    }
    .item-build-icon {
        display: block;
        width: 40px;
        height: 40px;
        aspect-ratio: 1 / 1;
        object-fit: contain;
        background: #2a3142;
    }
    .single-item-card .item-build-icon {
        width: 42px;
        height: 42px;
    }
    .item-cluster-card .item-build-icon {
        width: 44px;
        height: 44px;
    }
    .item-build-wr {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 24px;
        border-top: 1px solid rgba(255,255,255,0.08);
        border-bottom: 1px solid rgba(255,255,255,0.08);
        color: #6bd16b;
        font-size: 11px;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }
    .item-build-wr.is-bad {
        color: #ff8a8a;
    }
    .item-build-name {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 28px;
        padding: 3px 3px;
        color: #e6e8eb;
        font-size: 9px;
        font-weight: 600;
        line-height: 1.35;
        overflow: hidden;
    }
    .item-cluster-card .item-build-name {
        justify-content: flex-start;
        min-height: 36px;
        padding: 5px 8px;
        font-size: 10px;
    }
    .item-build-name span {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .fit-chip-wrap {
        position: relative;
        display: inline-flex;
        max-width: 100%;
        border-radius: 999px;
        outline: none;
    }
    .fit-chip-wrap:focus-visible .fit-chip {
        box-shadow: 0 0 0 2px rgba(255,255,255,0.32);
    }
    .fit-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        max-width: 100%;
        padding: 3px 9px;
        border-radius: 999px;
        background: rgba(107, 209, 107, 0.10);
        border: 1px solid rgba(107, 209, 107, 0.25);
        color: #b9f6b9;
        font-size: 11px;
        line-height: 1.35;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        cursor: help;
    }
    .fit-chip.item-build-chip {
        padding: 4px 9px 4px 5px;
    }
    .fit-chip-tooltip {
        position: absolute;
        left: 0;
        bottom: calc(100% + 8px);
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: max-content;
        max-width: min(300px, calc(100vw - 28px));
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.08);
        background: #0b0e13;
        box-shadow: 0 6px 18px rgba(0,0,0,0.55);
        color: #c5cad3;
        font-size: 12px;
        line-height: 1.35;
        white-space: nowrap;
        pointer-events: none;
        opacity: 0;
        transform: translateY(2px);
        transition: opacity 0.12s ease-out, transform 0.12s ease-out;
        z-index: 62;
    }
    .fit-chip-tooltip::after {
        content: "";
        position: absolute;
        top: 100%;
        left: 18px;
        border: 6px solid transparent;
        border-top-color: #0b0e13;
    }
    .fit-chip-wrap.tip-left .fit-chip-tooltip {
        left: auto;
        right: 0;
    }
    .fit-chip-wrap.tip-left .fit-chip-tooltip::after {
        left: auto;
        right: 18px;
    }
    .fit-chip-wrap.tip-below .fit-chip-tooltip {
        top: calc(100% + 8px);
        bottom: auto;
    }
    .fit-chip-wrap.tip-below .fit-chip-tooltip::after {
        top: auto;
        bottom: 100%;
        border-top-color: transparent;
        border-bottom-color: #0b0e13;
    }
    .fit-chip-wrap:hover .fit-chip-tooltip,
    .fit-chip-wrap:focus-within .fit-chip-tooltip {
        opacity: 1;
        transform: translateY(0);
    }
    .fit-tip-name {
        color: #e6e8eb;
        font-weight: 600;
    }
    .fit-tip-pick {
        color: #9aa0a6;
        font-variant-numeric: tabular-nums;
    }
    .fit-tip-lift {
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }
    .fit-tip-lift.is-good { color: #6bd16b; }
    .fit-tip-lift.is-bad { color: #ff8a8a; }
    .fit-tip-lift.is-even { color: #d7dde7; }
    .item-pair-icons {
        display: inline-flex;
        align-items: center;
        flex: 0 0 auto;
    }
    .item-pair-icon-wrap {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 22px;
        height: 22px;
        border-radius: 5px;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,0.16);
        background: #0f131b;
        box-shadow: 0 1px 4px rgba(0,0,0,0.25);
    }
    .item-pair-icon-wrap + .item-pair-icon-wrap {
        margin-left: -4px;
    }
    .item-pair-icon-wrap img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }
    .fit-chip-label {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fit-list {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
        gap: 8px;
    }
    .fit-card {
        background: #11151d;
        border: 1px solid rgba(255,255,255,0.05);
        border-radius: 8px;
        padding: 8px;
        min-width: 0;
    }
    .fit-card.good {
        border-color: rgba(107, 209, 107, 0.22);
        background: linear-gradient(180deg, rgba(107, 209, 107, 0.08), #11151d 42%);
    }
    .fit-card.bad {
        border-color: rgba(255, 107, 107, 0.22);
        background: linear-gradient(180deg, rgba(255, 107, 107, 0.07), #11151d 42%);
    }
    .fit-name {
        color: #e6e8eb;
        font-size: 12px;
        font-weight: 700;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fit-score {
        margin-top: 4px;
        font-size: 12px;
        font-weight: 700;
    }
    .fit-card.good .fit-score { color: #6bd16b; }
    .fit-card.bad .fit-score { color: #ff8b8b; }
    .fit-meta {
        margin-top: 2px;
        color: #9aa0a6;
        font-size: 10px;
        line-height: 1.35;
    }
    .detail-cols {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 18px;
    }
    .detail-col h3 {
        margin: 0 0 8px;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .detail-col-heading {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0 0 8px;
        min-width: 0;
        flex-wrap: wrap;
    }
    .detail-col-heading h3 { margin: 0; }
    .detail-col.best h3 { color: #6bd16b; }
    .detail-col.worst h3 { color: #ff6b6b; }
    .rarity-row {
        display: grid;
        grid-template-columns: 56px 1fr;
        gap: 10px;
        align-items: start;
        margin-bottom: 10px;
        min-width: 0;
    }
    .rlabel {
        font-size: 11px;
        font-weight: 700;
        padding: 5px 6px;
        border-radius: 5px;
        text-align: center;
        color: #0e1116;
        letter-spacing: 0.3px;
        align-self: stretch;
        display: flex;
        align-items: center;
        justify-content: center;
        position: relative;
        overflow: hidden;
    }
    .rlabel.prismatic {
        background: linear-gradient(135deg,#ffffff 0%,#e7d5ff 25%,#bcd6ff 50%,#ffd5ec 75%,#fff1c8 100%);
        background-size: 220% 220%;
        animation: prismShift 6s ease-in-out infinite;
        color: #2a1a4a;
        box-shadow: 0 0 6px rgba(220,180,255,0.5), inset 0 0 0 1px rgba(255,255,255,0.6);
    }
    .rlabel.gold     { background: linear-gradient(135deg,#ffe87a,#f5c518,#d99908); color: #3a2600; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.35); }
    .rlabel.silver   { background: linear-gradient(135deg,#eef0f4,#c0c5cc,#9aa0a6); color: #2a2e35; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.35); }
    .aug-list {
        display: flex;
        gap: 10px;
        min-width: 0;
        overflow-x: auto;
        overscroll-behavior-inline: contain;
        scroll-snap-type: x proximity;
        scrollbar-width: thin;
        scrollbar-color: rgba(148, 163, 184, 0.45) transparent;
        padding: 0 4px 8px 0;
        -webkit-overflow-scrolling: touch;
    }
    .aug-list::-webkit-scrollbar { height: 7px; }
    .aug-list::-webkit-scrollbar-track { background: transparent; }
    .aug-list::-webkit-scrollbar-thumb {
        background: rgba(148, 163, 184, 0.35);
        border-radius: 999px;
    }
    .aug-list.empty-list { color: #6b7280; font-size: 11px; padding: 8px 0; }
    .aug {
        flex: 0 0 92px;
        scroll-snap-align: start;
        background: #11151d;
        border-radius: 8px;
        padding: 8px 6px;
        text-align: center;
        position: relative;
        border: 1px solid rgba(255,255,255,0.04);
    }
    .aug img {
        width: 48px; height: 48px;
        display: block;
        margin: 0 auto 4px;
        border-radius: 6px;
        background: #2a3142;
    }
    .aug .aname {
        font-size: 10px;
        color: #e6e8eb;
        line-height: 1.25;
        margin-bottom: 4px;
        min-height: 24px;
        overflow: hidden;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
    }
    .aug .awr {
        font-size: 11px;
        font-weight: 700;
    }
    .aug.good .awr { color: #6bd16b; }
    .aug.bad  .awr { color: #ff6b6b; }
    .aug .alift {
        font-size: 9px;
        color: #9aa0a6;
        margin-top: 1px;
    }
    /* Custom hover popup with augment description.  Native title is kept too
       as an accessibility/fallback path. */
    .aug-tip {
        position: absolute;
        left: 50%;
        bottom: calc(100% + 8px);
        transform: translateX(-50%);
        width: 220px;
        padding: 8px 10px;
        background: #0b0e13;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 8px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.55);
        color: #e6e8eb;
        font-size: 11px;
        line-height: 1.45;
        text-align: left;
        z-index: 50;
        pointer-events: none;
        opacity: 0;
        transition: opacity 0.12s ease-out;
    }
    .aug-tip::after {
        content: "";
        position: absolute;
        top: 100%;
        left: 50%;
        transform: translateX(-50%);
        border: 6px solid transparent;
        border-top-color: #0b0e13;
    }
    /* When an augment sits near the top of the viewport, JS sets .flip-tip
       so the tooltip drops below the card instead of clipping above. */
    .aug.flip-tip .aug-tip {
        bottom: auto;
        top: calc(100% + 8px);
    }
    .aug.flip-tip .aug-tip::after {
        top: auto;
        bottom: 100%;
        border-top-color: transparent;
        border-bottom-color: #0b0e13;
    }
    .aug:hover .aug-tip,
    .aug:focus-visible .aug-tip { opacity: 1; }
    .aug-tip-name {
        font-weight: 700;
        font-size: 12px;
        margin-bottom: 4px;
        color: #f5d780;
    }
    .aug-tip-desc {
        color: #c5cad3;
        margin-bottom: 6px;
        white-space: normal;
    }
    .aug-tip-stat {
        color: #9aa0a6;
        font-size: 10px;
        border-top: 1px solid rgba(255,255,255,0.08);
        padding-top: 4px;
    }
    .aug-tip-set {
        color: #8fd8f4;
        font-size: 10px;
        margin-bottom: 4px;
    }
    .aug.rarity-kGold   { box-shadow: inset 0 0 0 2px #f5c518; }
    .aug.rarity-kSilver { box-shadow: inset 0 0 0 2px #c0c5cc; }
    .aug.rarity-kPrismatic { box-shadow: inset 0 0 0 2px #d36bff; }
    .aug.search-hit {
        background: rgba(245, 197, 24, 0.12);
        box-shadow:
            inset 0 0 0 2px #f5c518,
            0 0 0 2px rgba(245, 197, 24, 0.28),
            0 10px 22px rgba(245, 197, 24, 0.12);
    }
    .mate-list {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
        gap: 10px;
    }
    .mate-list.empty-list { color: #6b7280; font-size: 11px; padding: 8px 0; }
    .mate-card {
        display: grid;
        grid-template-columns: 42px 1fr;
        gap: 8px;
        align-items: center;
        padding: 8px;
        background: #11151d;
        border-radius: 8px;
        border: 1px solid rgba(255,255,255,0.04);
    }
    .mate-card img {
        width: 42px;
        height: 42px;
        border-radius: 8px;
        display: block;
        background: #2a3142;
    }
    .mate-card .mname {
        font-size: 12px;
        font-weight: 600;
        color: #e6e8eb;
        line-height: 1.25;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .mate-card .mwr {
        margin-top: 2px;
        font-size: 11px;
        font-weight: 700;
    }
    .mate-card.good .mwr { color: #6bd16b; }
    .mate-card.bad .mwr { color: #ff6b6b; }
    .mate-card .mmeta {
        margin-top: 2px;
        font-size: 10px;
        color: #9aa0a6;
        font-family: "Noto Serif TC", "Source Han Serif TC", serif;
        font-variant-numeric: tabular-nums;
        line-height: 1.35;
    }
    .mate-card .mmeta .mmeta-z { white-space: nowrap; }
    .empty { color: #6b7280; font-size: 12px; }
    .footer {
        margin-top: 40px;
        padding-top: 24px;
        border-top: 1px solid #1f2530;
        color: #6b7280;
        font-size: 11px;
        text-align: center;
        line-height: 1.7;
    }
    .footer .cutoffs {
        font-variant-numeric: tabular-nums;
        letter-spacing: 0.02em;
    }
    .footer .cutoffs b {
        color: #c5cad3;
        font-weight: 600;
        margin-right: 2px;
    }
    .footer .freshness {
        margin-top: 6px;
        color: #555a63;
    }
    .footer .disclaimer {
        max-width: 760px;
        margin: 20px auto 0;
        padding-top: 14px;
        border-top: 1px solid #16191f;
        color: #555a63;
        font-size: 10px;
    }
    @media (max-width: 1180px) {
        .app-shell,
        .app-shell.with-side-panel { grid-template-columns: 1fr; }
        .side-panel {
            position: static;
            max-height: none;
            overflow: visible;
            order: -1;
        }
    }
    /* Mobile / narrow viewport: switch the detail panel from two columns
       (best / worst) to a single stack so prismatic / gold / silver rows
       stay readable, and shrink the tier-row label so champions get more
       space.  ~700px is around where the two-column layout starts looking
       cramped on most phones. */
    @media (max-width: 700px) {
        body { padding: 18px 10px 40px; }
        body.rec-modal-open,
        body.detail-modal-open { overflow: hidden; }
        h1 { font-size: 18px; }
        .subtitle { font-size: 12px; }
        .title-patch { font-size: 12px; }
        /* Keep the header compact: title + patch/update chip on the left,
           utility actions on the right. */
        .page-header {
            gap: 6px 8px;
            margin-bottom: 10px;
        }
        .title-meta { gap: 6px; margin-top: 0; }
        .page-actions {
            gap: 6px;
            flex-wrap: wrap;
            justify-content: flex-end;
        }
        .page-actions .icon-btn,
        .page-actions .gh-star,
        .tool-btn.header-update-tab {
            min-height: auto;
            padding: 0 0 2px;
        }
        .page-actions .gh-star { width: auto; }
        /* Filter bar wraps tighter; search input becomes full-width on
           its own row. */
        .filter-bar { padding: 8px; gap: 8px; }
        .role-chips { gap: 4px; }
        .chip { padding: 4px 10px; font-size: 11px; }
        .filter-tools {
            margin-left: 0;
            width: 100%;
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 8px 10px;
            align-items: center;
        }
        #recommend-mode { grid-column: 1 / -1; width: 100%; }
        .tool-btn { min-height: 36px; }
        .updates-panel {
            margin: 0 0 14px;
            padding: 12px;
        }
        .updates-title { font-size: 14px; }
        .updates-list li { font-size: 11px; }
        .change-summary { margin-bottom: 10px; }
        .change-grid { grid-template-columns: 1fr; gap: 12px; }
        .change-row {
            min-height: 42px;
            grid-template-columns: auto minmax(0, 1fr) auto;
            padding: 6px 8px;
        }
        .change-icon,
        .change-duo img {
            width: 30px;
            height: 30px;
        }
        .change-delta {
            min-width: 50px;
            font-size: 10px;
        }
        .side-panel {
            position: fixed;
            z-index: 60;
            left: 12px;
            right: 12px;
            top: 56px;
            bottom: 18px;
            max-height: none;
            overflow: auto;
            padding: 14px;
            border-radius: 14px;
            box-shadow: 0 22px 60px rgba(0,0,0,0.58);
        }
        body.rec-modal-open::before,
        body.detail-modal-open::before {
            content: "";
            position: fixed;
            inset: 0;
            z-index: 55;
            background: rgba(5, 8, 13, 0.72);
        }
        .side-close { display: inline-flex; }
        .rec-titleline {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }
        .rec-name { flex: 1 1 86px; }
        .rec-score { justify-self: auto; }
        .rec-detail {
            display: flex;
            flex-wrap: wrap;
            gap: 4px 8px;
            white-space: normal;
        }
        .detail-host {
            position: fixed;
            z-index: 70;
            inset: 0;
            overflow: auto;
            padding: 56px 12px 18px;
            -webkit-overflow-scrolling: touch;
        }
        .detail-host .detail {
            max-width: 680px;
            min-height: 100%;
            margin: 0 auto;
            padding: 14px;
            border: 1px solid #30363d;
            border-radius: 14px;
            box-shadow: 0 22px 60px rgba(0,0,0,0.58);
        }
        .detail-close { display: inline-flex; }
        .rec-fab { display: none; }
        .rec-fab:not(.is-hidden) { display: inline-flex; }
        .side-sub { font-size: 11px; }
        .pick-slots { gap: 6px; }
        .search-wrap { max-width: none; min-width: 0; }
        .search { max-width: none; min-width: 0; }
        .shown-count { justify-self: end; font-size: 12px; }
        /* Tier heading slimmer; pill stays inline. */
        .tier-heading { margin: 6px 0; gap: 6px; }
        .tier-pill { padding: 3px 12px; font-size: 14px; }
        .tier-count { font-size: 11px; }
        /* Lock to 6 champions per row on mobile (instead of auto-fill which
           packs 7-8 in and makes icons tiny). */
        .tier-grid { grid-template-columns: repeat(6, 1fr); gap: 5px; }
        .detail-head {
            flex-direction: row;
            align-items: center;
            gap: 10px;
            padding-right: 42px;
            margin-bottom: 18px;
        }
        .detail-avatar {
            display: block;
            width: 42px;
            height: 42px;
            border-radius: 9px;
        }
        .detail-section-head {
            flex-direction: column;
            align-items: flex-start;
            gap: 5px;
            margin-bottom: 12px;
        }
        .detail-section-head h3 {
            font-size: 14px;
        }
        .detail-tab-list {
            gap: 6px;
        }
        .detail-tab-label {
            min-height: 38px;
            padding-inline: 10px;
            font-size: 11px;
        }
        .detail-tab-panel {
            padding-top: 18px;
        }
        .detail-sub-tabs {
            margin-top: 2px;
        }
        .detail-sub-tabs .detail-tab-label {
            min-height: 34px;
            padding-inline: 10px;
            font-size: 11px;
        }
        .detail-sub-tabs .detail-tab-panel {
            padding-top: 18px;
        }
        .aug-set-summary {
            max-width: 100%;
            white-space: normal;
        }
        .section-meta {
            font-size: 10px;
            line-height: 1.4;
        }
        .detail-cols { grid-template-columns: 1fr; gap: 14px; }
        .detail-cols.pair-cols { grid-template-columns: 1fr; gap: 14px; }
        .detail-cols.pair-cols .detail-col h3 { margin-bottom: 6px; font-size: 12px; }
        .detail-cols.pair-cols .detail-col-heading h3 { margin-bottom: 0; }
        /* Drop the rarity colored bar (label) on mobile to recover horizontal
           space.  Each augment card still has a rarity-coloured border, so
           which row is which is obvious. */
        .rarity-row { grid-template-columns: 1fr; gap: 4px; }
        .rlabel { display: none; }
        /* Each rarity row is a horizontal carousel; show five cards at a
           glance, then let the rest continue off-canvas for swipe. */
        .aug-list {
            gap: 4px;
            padding-bottom: 6px;
            scroll-snap-type: x mandatory;
        }
        .aug {
            flex-basis: calc((100% - 16px) / 5);
            min-width: 58px;
        }
        .item-build-carousel {
            gap: 8px;
            padding-bottom: 10px;
            scroll-snap-type: x mandatory;
        }
        .item-build-carousel.item-build-grid {
            gap: 7px;
            padding-bottom: 0;
            overflow-x: visible;
            scroll-snap-type: none;
        }
        .item-build-carousel.single-item-grid {
            grid-template-columns: repeat(auto-fill, minmax(58px, 1fr));
        }
        .item-build-carousel.item-pair-grid {
            grid-template-columns: repeat(auto-fill, minmax(58px, 76px));
            justify-content: start;
        }
        .item-build-carousel.item-cluster-grid {
            grid-template-columns: 1fr;
            justify-content: start;
        }
        .item-build-card {
            flex-basis: calc((100% - 20px) / 6);
            min-width: 48px;
        }
        .item-build-card.single-item-card {
            flex-basis: calc((100% - 20px) / 6);
            min-width: 48px;
        }
        .item-build-grid .item-build-card {
            flex-basis: auto;
            min-width: 0;
        }
        .item-build-card.item-cluster-card {
            flex: 0 0 auto;
            min-width: 0;
            width: 100%;
        }
        .item-build-icons {
            min-height: 66px;
            padding: 3px;
            gap: 2px;
        }
        .single-item-card .item-build-icons {
            min-height: 42px;
        }
        .item-cluster-card .item-build-icons {
            min-height: 116px;
            grid-template-columns: repeat(3, 1fr);
            padding: 8px;
            gap: 6px;
        }
        .item-build-icon {
            width: 29px;
            height: 29px;
        }
        .single-item-card .item-build-icon {
            width: 34px;
            height: 34px;
        }
        .item-cluster-card .item-build-icon {
            width: 42px;
            height: 42px;
        }
        .item-build-wr {
            min-height: 22px;
            font-size: 10px;
        }
        .item-build-name {
            min-height: 24px;
            padding: 2px 2px;
            font-size: 9px;
        }
        .item-cluster-card .item-build-name {
            min-height: 36px;
            padding: 5px 8px;
            font-size: 10px;
        }
        .mate-list { grid-template-columns: 1fr; gap: 6px; }
        .mate-card {
            grid-template-columns: 34px 1fr;
            gap: 5px;
            padding: 5px;
            min-width: 0;
        }
        .mate-card > div { min-width: 0; }
        .mate-card img {
            width: 34px;
            height: 34px;
            border-radius: 6px;
        }
        .mate-card .mname { font-size: 11px; }
        .mate-card .mwr { font-size: 10px; }
        .mate-card .mmeta { font-size: 9px; }
        .mate-card .mmeta .mmeta-label,
        .mate-card .mmeta .mmeta-z,
        .mate-card .mmeta .mmeta-games { display: none; }
        .aug { padding: 5px 3px; }
        .aug img { width: 36px; height: 36px; }
        .aug .aname { font-size: 9px; min-height: 22px; }
        .aug .awr { font-size: 10px; }
        /* Hide the lift% / games count on mobile - keep cards compact.
           Numbers still available on hover (tooltip) and via the title attr. */
        .aug .alift { display: none; }
        .aug-tip,
        .alt-role-tooltip,
        .fit-chip-tooltip { display: none; }
        /* Touch-target floor (WCAG 2.5.5).  Chips were 4×10 padding on 11px
           font ≈ 32 px tall.  Bump to a real 44 px tap area without growing
           the visual pill, by adding transparent vertical padding. */
        .chip { min-height: 34px; }
        .icon-btn,
        .gh-star { min-height: 36px; }
        .gh-star { width: 36px; padding: 8px 0; }
        .lang-toggle { min-width: 0; }
    }
    @media (min-width: 520px) and (max-width: 700px) {
        .item-build-carousel.item-cluster-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
    }
    @media (min-width: 320px) and (max-width: 359px) {
        .mate-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (min-width: 360px) and (max-width: 700px) {
        .mate-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    /* Keyboard a11y: every interactive element gets a visible focus ring
       when focused via keyboard (not mouse click).  Uses the tier accent
       (or a neutral white when no tier is in scope) and stays well clear
       of the resting border colour. */
    .chip:focus-visible,
    .icon-btn:focus-visible,
    .gh-star:focus-visible,
    .tool-btn:focus-visible,
    .side-close:focus-visible,
    .detail-close:focus-visible,
    .meta-help:focus-visible,
    .rec-fab:focus-visible,
    .pick-chip:focus-visible,
    .rec-row:focus-visible,
    .search:focus-visible,
    .champ:focus-visible,
    .item-build-card:focus-visible,
    .aug:focus-visible {
        outline: 2px solid #f5e8ff;
        outline-offset: 2px;
    }
    /* Reduced-motion override.  Disables prismShift / shineSweep /
       slideDown so vestibular-sensitive users don't get hue drift and
       sweep effects across the page. */
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.001ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.001ms !important;
        }
    }
    """

    payload = {
        "champs": js_champs,
        "augs": js_augs,
        "min_games_per_pair": min_games_per_pair,
        "min_synergy_games": min_synergy_games,
        "patchChanges": patch_changes or {},
        "recommendation_composition": {
            "weight": RECOMMENDATION_COMPOSITION_WEIGHT,
            "clamp": RECOMMENDATION_COMPOSITION_CLAMP,
            "lack_thresholds": COMPOSITION_LACK_THRESHOLDS,
            "table_weights": RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS,
            "tables": RECOMMENDATION_COMPOSITION_TABLES,
            "damage_mix": {
                "target_ad_share": RECOMMENDATION_DAMAGE_MIX_TARGET_AD,
                "weight": RECOMMENDATION_DAMAGE_MIX_WEIGHT,
                "clamp": RECOMMENDATION_DAMAGE_MIX_CLAMP,
            },
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    if payload_out_path is not None:
        payload_out_path.parent.mkdir(parents=True, exist_ok=True)
        payload_out_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    og_patch_label = f"patch {patch_prefix}" if patch_prefix else "all patches"
    og_title = f"{header_title}資料庫"
    og_desc = f"{og_patch_label}｜【英雄 x 增幅裝置勝率 · 組隊推薦】&#10;by 路燈"

    meta_lines: list[str] = []
    meta_lines.append("<meta charset='utf-8'>")
    meta_lines.append(
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    )
    meta_lines.append(f"<title>{og_title}</title>")
    favicon_version = favicon_asset_version()
    meta_lines.append(
        f"<link rel='icon' type='image/png' href='mayhem-single-die-icon.png?v={favicon_version}'>"
    )
    meta_lines.append(
        f"<link rel='apple-touch-icon' href='apple-touch-icon.png?v={favicon_version}'>"
    )
    meta_lines.append(f"<meta name='description' content=\"{og_desc}\">")
    if site_url:
        meta_lines.append(f"<link rel='canonical' href='{site_url}'>")
        meta_lines.append(f"<meta property='og:url' content='{site_url}'>")
    meta_lines.append("<meta property='og:type' content='website'>")
    meta_lines.append(f"<meta property='og:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta property='og:description' content=\"{og_desc}\">")
    if og_image:
        meta_lines.append(f"<meta property='og:image' content='{og_image}'>")
        meta_lines.append("<meta property='og:image:width' content='512'>")
        meta_lines.append("<meta property='og:image:height' content='512'>")
        meta_lines.append("<meta property='og:image:alt' content='ARAM Mayhem Database preview'>")
        meta_lines.append("<meta name='twitter:card' content='summary'>")
        meta_lines.append(f"<meta name='twitter:image' content='{og_image}'>")
        meta_lines.append("<meta name='twitter:image:alt' content='ARAM Mayhem Database preview'>")
    else:
        meta_lines.append("<meta name='twitter:card' content='summary'>")
    meta_lines.append(f"<meta name='twitter:title' content=\"{og_title}\">")
    meta_lines.append(f"<meta name='twitter:description' content=\"{og_desc}\">")

    parts: list[str] = []
    parts.append("<!doctype html><html lang='zh-Hant'><head>")
    parts.extend(meta_lines)
    parts.extend(
        render_analytics_tags(
            cloudflare_token=cloudflare_analytics_token,
            ga_measurement_id=ga_measurement_id,
        )
    )
    # Webfonts: Noto Sans TC for everything by default; Noto Serif TC only
    # for a couple of small captions (subtitle, panel meta, augment lift)
    # where the mincho gives a "footnote" feel without hurting legibility.
    # `display=swap` lets system fallback paint immediately; weights pruned
    # to what each face actually uses on the page.
    parts.append(
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link href='https://fonts.googleapis.com/css2"
        "?family=Noto+Sans+TC:wght@400;500;600;700"
        "&family=Noto+Serif+TC:wght@400;500"
        "&display=swap' rel='stylesheet'>"
    )
    parts.append(f"<style>{css}</style></head><body>")
    # Header: title + subtitle on the left, language toggle + GitHub star on the right.
    # The repo name is the canonical project URL; if the user later forks /
    # renames, update REPO_URL below.
    REPO_URL = "https://github.com/Lanternko/ARAM-Mayhem-Database"
    short_patch = patch_prefix if patch_prefix else "all patches"
    date_str = f"更新於 {build_date}" if build_date else "日期未標"
    globe_icon = (
        "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='12' cy='12' r='10'></circle>"
        "<path d='M2 12h20'></path>"
        "<path d='M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10Z'></path>"
        "</svg>"
    )
    gh_icon = (
        "<svg viewBox='0 0 16 16' width='14' height='14' fill='currentColor' "
        "aria-hidden='true'><path d='M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1"
        "-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1."
        "23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-."
        "2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0"
        "-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12"
        "-.51.56-.82 1.27-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-"
        ".51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.2"
        "7.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.0"
        "1 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8"
        "Z'></path></svg>"
    )
    parts.append("<div class='page-header'>")
    parts.append("<div><div class='title-meta'>")
    parts.append(f"<h1 id='site-title'>{header_title}</h1>")
    parts.append(f"<div class='subtitle title-patch' id='site-subtitle'>{short_patch}</div>")
    parts.append("</div></div>")
    parts.append("<div class='page-actions'>")
    parts.append(
        '<button class="tool-btn update-tab header-update-tab" id="updates-toggle" type="button" '
        'aria-expanded="false" aria-controls="updates-panel">近期更新</button>'
    )
    parts.append(
        "<button class='icon-btn lang-toggle' id='lang-toggle' type='button' "
        "title='Switch to English' aria-label='切換語言'>"
        f"{globe_icon}<span id='lang-toggle-label'>EN</span>"
        "</button>"
    )
    parts.append(
        f"<a class='gh-star' href='{REPO_URL}' target='_blank' rel='noopener' "
        "aria-label='GitHub' "
        f"title='覺得有用請幫忙按 Star ⭐'>"
        f"{gh_icon}"
        f"</a>"
    )
    parts.append("</div>")
    parts.append("</div>")  # /page-header
    parts.append("<div class='app-shell'>")
    parts.append("<div class='main-col'>")
    parts.append(
        "<section class='updates-panel is-hidden' id='updates-panel' "
        "aria-labelledby='updates-title'>"
        "<div class='updates-head'>"
        "<div>"
        "<span class='updates-kicker' id='updates-kicker'>?祉???</span>"
        "<h2 class='updates-title' id='updates-title'>餈????湔</h2>"
        "</div>"
        "<button class='updates-close' id='updates-close' type='button' "
        "aria-label='關閉近期更新'>&times;</button>"
        "</div>"
        "<div class='updates-list' id='updates-list'>"
        "<li>???拚??支? pair ??嚗?其?????蝯?靽格迤嚗??怠??D/AP?oke??蝺??啁???捆蝻箏??/li>"
        "<li>憓?鋆蔭??撠?蝝?詨?????雿??擃????港?摰?敺?/li>"
        "<li>?啣??箄?憸冽??撟???扼?/li>"
        "</div>"
        "</section>"
    )

    # Filter bar: role chips + free-text search + live "N shown" counter.
    parts.append("<div class='filter-bar'>")
    parts.append("<div class='role-chips'>")
    parts.append('<button class="chip active" data-role="" data-label-zh="★ All" data-label-en="★ All">★ All</button>')
    for role_en in ROLE_ORDER:
        labels = ROLE_LABELS.get(role_en, {})
        role_zh = labels.get("zh", role_en)
        role_label_en = labels.get("en", role_en)
        parts.append(
            f'<button class="chip" data-role="{html.escape(role_en)}" data-label-zh="{html.escape(role_zh)}" '
            f'data-label-en="{html.escape(role_label_en)}">{html.escape(role_zh)}</button>'
    )
    parts.append("</div>")  # /role-chips
    parts.append("<div class='filter-tools'>")
    parts.append(
        '<button class="tool-btn" id="recommend-mode" type="button" '
        'aria-pressed="false">選擇你的隊友：關</button>'
    )
    # Search input wrapped in a label with an inline magnifier SVG sitting
    # in the input's left padding (the wrapper is positioned, the input
    # has padding-left to clear the icon).
    search_icon = (
        "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        "stroke-linejoin='round' aria-hidden='true'>"
        "<circle cx='11' cy='11' r='7'></circle>"
        "<line x1='21' y1='21' x2='16.5' y2='16.5'></line></svg>"
    )
    parts.append(
        "<label class='search-wrap'>"
        f"{search_icon}"
        '<input class="search" id="champ-search" type="search" '
        'placeholder="搜尋英雄（中 / 英）" autocomplete="off" '
        'aria-label="搜尋英雄">'
        "</label>"
    )
    parts.append(
        f'<span class="shown-count"><span id="shown-n">{len(records)}</span> / {len(records)} '
        "<span id='shown-unit'>隻</span></span>"
    )
    parts.append("</div>")  # /filter-tools
    parts.append("</div>")  # /filter-bar

    for tier in TIER_ORDER:
        entries = by_tier[tier]
        if not entries:
            continue
        entries.sort(key=lambda d: -d["bayes_wr"])
        color = TIER_COLOR[tier]
        bg = TIER_LABEL_BG[tier]
        parts.append(
            f"<div class='tier-block' data-tier='{tier}' "
            f"style='--tier-color:{color}; --tier-bg:{bg};'>"
        )
        # New layout: tier name on its own heading row (no side bar), grid
        # takes the full row below.  Same look on desktop + mobile.
        parts.append("<h2 class='tier-heading'>")
        parts.append(f"<span class='tier-pill'><span>{tier}</span></span>")
        parts.append(
            f"<span class='tier-count'>"
            f"<span class='tier-count-num' data-tier='{tier}'>{len(entries)}</span>"
            " <span class='tier-count-unit'>隻</span>"
            "</span>"
        )
        parts.append("</h2>")
        parts.append("<div class='tier-grid'>")
        for r in entries:
            wr_pct = f"{r['bayes_wr'] * 100:.1f}%"
            meta = champ_meta.get(r["champion_id"], {})
            tags = list(meta.get("tags") or [])
            tag_str = " ".join(tags)
            primary_role = tags[0] if tags else ""
            secondary_role = tags[1] if len(tags) > 1 else ""
            alias = meta.get("alias", "")
            search_blob = _champ_search_blob(int(r["champion_id"]), r["name"], meta, tags)
            title = (
                f"{r['name']} · WR {wr_pct} · games {r['games']:,} · "
                f"raw {r['raw_wr']*100:.1f}%"
            )
            aria_label = f"{r['name']} {alias}，tier {tier}，勝率 {wr_pct}"
            parts.append(
                f"<div class='champ' data-cid='{r['champion_id']}' "
                f"data-name-zh=\"{html.escape(r['name'])}\" "
                f"data-name-en=\"{html.escape(meta.get('name_en', alias or r['name']))}\" "
                f"data-tags='{tag_str}' data-primary-role='{html.escape(primary_role)}' "
                f"data-secondary-role='{html.escape(secondary_role)}' "
                f"data-search=\"{html.escape(search_blob, quote=True)}\" "
                f"data-tier='{tier}' data-wr='{wr_pct}' data-games='{r['games']}' "
                f"data-raw-wr='{r['raw_wr']*100:.1f}%' "
                f"role='button' tabindex='0' "
                f"aria-label=\"{aria_label}\" "
                f"title=\"{title}\">"
                f"<img loading='lazy' src='{r['image']}' alt=''>"
                f"<span class='alt-role-badge' data-alt-role='{html.escape(primary_role)}' "
                "title='' aria-label='' hidden></span>"
                # The English alias is rendered as screen-reader-only text so
                # Ctrl+F / Cmd+F can find e.g. "Aatrox" even though only the
                # zh-TW name is drawn.  (aria-label already announces it for
                # actual screen readers.)
                f"<span class='sr-only'>{alias}</span>"
                f"<span class='wr'>{wr_pct}</span>"
                f"<span class='name'>{r['name']}</span>"
                f"</div>"
            )
        # Detail host lives INSIDE .tier-grid so it can grid-span all columns
        # and be inserted right after the clicked champion's visual row.
        parts.append(f"<div class='detail-host' data-tier='{tier}'></div>")
        parts.append("</div>")  # /tier-grid
        parts.append("</div>")  # /tier-block

    # Empty state — toggled by JS when all tiers are filtered out.
    parts.append(
        "<div class='empty-state' id='empty-state'>"
        "<strong id='empty-title'>沒有符合條件的英雄</strong>"
        "<span id='empty-copy'>換個角色篩選，或試試英雄中／英文名。</span>"
        "</div>"
    )

    parts.append("<div class='footer'>")
    parts.append(
        "<div class='cutoffs'>"
        "Tier (Bayes WR): "
        "<b>OP</b>≥55% · "
        "<b>T1</b>≥52% · "
        "<b>T2</b>≥50% · "
        "<b>T3</b>≥48% · "
        "<b>T4</b>≥46% · "
        "<b>T5</b>&lt;46%"
        "</div>"
    )
    if build_date:
        parts.append(
            f"<div class='freshness' id='freshness-copy'>{date_str}（{total_games:,} 場） · {patch_label}</div>"
        )
    parts.append(
        "<div class='disclaimer'>"
        "This site isn't endorsed by Riot Games and doesn't reflect the views "
        "or opinions of Riot Games or anyone officially involved in producing "
        "or managing League of Legends. League of Legends and Riot Games are "
        "trademarks or registered trademarks of Riot Games, Inc. "
        "League of Legends © Riot Games, Inc."
        "</div>"
    )
    parts.append("</div>")
    parts.append("</div>")  # /main-col
    parts.append(
        "<aside class='side-panel' id='side-panel'>"
        "<div class='side-head'>"
        "<div>"
        "<h2 id='side-title'>推薦組合排行</h2>"
        "<div class='side-sub' id='side-sub'>"
        "依歷史搭配排序，並修正傷害比例與陣容缺口。<br>"
        "推薦度越高越適合；可信度是資料穩定度摘要。"
        "</div>"
        "</div>"
        "<button class='side-close' id='side-close' type='button' aria-label='關閉推薦組合'>×</button>"
        "</div>"
        "<div class='pick-slots' id='pick-slots'></div>"
        "<div class='pick-note' id='pick-note'></div>"
        "<div class='rec-list' id='rec-list'></div>"
        "</aside>"
        "<button class='rec-fab is-hidden' id='rec-fab' type='button'>看推薦組合</button>"
    )
    parts.append("</div>")  # /app-shell

    js = """
    async function loadSitePayload(url) {
        const response = await fetch(url, { cache: 'no-cache' });
        if (!response.ok) {
            throw new Error(`payload ${response.status}: ${url}`);
        }
        return await response.json();
    }
    const DATA = __PAYLOAD__;
    const pct = x => (x * 100).toFixed(1) + '%';
    const signed = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const escHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const ROLE_LABELS = __ROLE_LABELS__;
    const ROLE_BADGE_ICONS = {
        Assassin: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.7c1.1 1.72 3.46 4.16 3.46 6.62A3.46 3.46 0 1 1 4.54 8.32C4.54 5.86 6.9 3.42 8 1.7Z"/>
            </svg>
        `,
        Fighter: `
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <rect x="10.3" y="4" width="3.4" height="16" rx="1.2" transform="rotate(-45 12 12)" fill="currentColor"/>
                <rect x="10.3" y="4" width="3.4" height="16" rx="1.2" transform="rotate(45 12 12)" fill="currentColor"/>
            </svg>
        `,
        Mage: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.7 9.28 6.72 14.3 8l-5.02 1.28L8 14.3 6.72 9.28 1.7 8l5.02-1.28L8 1.7Z"/>
            </svg>
        `,
        Marksman: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="m4.05 11.95 1.1-3.1 2 2 4.52-4.52.95.95-4.52 4.52 1.99 1.99-3.09 1.11-3.22 1.1 1.12-3.05Z"/>
            </svg>
        `,
        Support: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M6.75 2.75h2.5v4h4v2.5h-4v4h-2.5v-4h-4v-2.5h4v-4Z"/>
            </svg>
        `,
        Tank: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.65 12.55 3v3.46c0 2.86-1.7 5.44-4.31 6.53L8 13.08l-.24-.09C5.15 11.9 3.45 9.32 3.45 6.46V3L8 1.65Zm0 1.52L4.95 4.08v2.38c0 2.16 1.23 4.11 3.05 5.06 1.82-.95 3.05-2.9 3.05-5.06V4.08L8 3.17Z"/>
            </svg>
        `,
    };
    const HEADER_TITLE_ZH = __HEADER_TITLE_ZH__;
    const HEADER_TITLE_EN = __HEADER_TITLE_EN__;
    const SHORT_PATCH_ZH = __SHORT_PATCH_ZH__;
    const DATE_STR_ZH = __DATE_STR_ZH__;
    const BUILD_DATE = __BUILD_DATE__;
    const PATCH_LABEL = __PATCH_LABEL__;
    const TOTAL_GAMES = __TOTAL_GAMES__;
    const LANG_KEY = 'aram-mayhem-site-lang';
    const SET_RESIDUAL_THRESHOLD = 0.02;
    function trackEvent(name, params = {}) {
        if (typeof gtag === 'function') {
            gtag('event', name, params);
        }
    }
    const COPY = {
        zh: {
            htmlLang: 'zh-Hant',
            subtitle: () => `${SHORT_PATCH_ZH}`,
            searchPlaceholderDesktop: '搜尋英雄（中 / 英）   Ctrl+F',
            searchPlaceholderMobile: '搜尋英雄（中 / 英）',
            searchAria: '搜尋英雄',
            shownUnit: '隻',
            tierUnit: '隻',
            updatesButton: '近期更新',
            updatesKicker: '本版重點',
            updatesTitle: '近期重要更新',
            updatesClose: '關閉近期更新',
            updatesItems: [
                '2026-05-25：英雄詳情新增「單件裝備強度」，六格中出過就計入，幫你選第三到第六件。',
                '2026-05-25：前兩件出裝與單件裝備改成右滑 carousel，手機一次看更多裝備。',
                '2026-05-25：增幅裝置改成同彩度一路由強排到弱，不再拆成兩個區塊。',
            ],
            recModeOn: '選擇你的隊友：開',
            recModeOff: '選擇你的隊友：關',
            emptyTitle: '沒有符合條件的英雄',
            emptyCopy: '換個角色篩選，或試試英雄中／英文名。',
            freshness: () => `${DATE_STR_ZH}（${TOTAL_GAMES} 場） · ${PATCH_LABEL}`,
            sideTitle: '推薦組合排行',
            sideSub: '依歷史搭配排序，並修正傷害比例與陣容缺口。<br>推薦度越高越適合；可信度是資料穩定度摘要。',
            closeRecs: '關閉推薦組合',
            openRecs: n => `看推薦組合 (${n})`,
            langToggleLabel: 'EN',
            langToggleTitle: 'Switch to English',
            langToggleAria: '切換語言',
            removePick: name => `移除 ${name}`,
            pickEmpty: '尚未選擇',
            maxOnly: n => `最多只能選 ${n} 隻英雄。`,
            pickNoteEmpty: n => `最多選 ${n} 隻；先看推薦度，再看原因與樣本。`,
            pickNotePartial: want => `目前這組選角的完整資料較少，先用已知搭配排序。`,
            pickNoteReady: (want, minGames) => `已選 ${want}/${MAX_TEAM_PICKS} 隻；pair 門檻 >= ${minGames} 場。`,
            panelEmpty: '先開啟「選擇你的隊友」，再從英雄列表點 1~4 隻英雄。系統會排出最適合補進來的英雄。',
            panelNoData: '這組英雄目前沒有足夠的 pair 資料。',
            detailEmpty: '這個英雄目前沒有可顯示的資料。',
            detailClose: '關閉詳細資訊',
            pairSectionTitle: '推薦搭檔',
            pairSectionMeta: '適配度為主，勝率為輔',
            setSectionTitle: '增幅裝置系列相性',
            setSectionMeta: '保守分數；負值代表相對較好，但未達正訊號',
            itemSectionTitle: '最強前兩件出裝',
            itemSectionMeta: '不含鞋子，左到右為第 1 到第 3 推薦',
            itemClusterSectionTitle: '出裝路線 TLDR',
            itemClusterSectionMeta: '實際出現過的完整六件；依核心裝備共現分群',
            augTypeSectionTitle: '推薦增幅裝置傾向',
            augTypeSectionMeta: '細分類優先；分數扣掉同角色／傷害型英雄的平均偏好',
            relativeBest: '相對最佳',
            best: '最佳',
            worst: '最差',
            bestAugments: '最佳增幅裝置',
            worstAugments: '最差增幅裝置',
            augmentStrengthMeta: '強度綜合參考勝率與選取率',
            augmentStrengthTip: '排序以勝率提升的保守估計為主，並搭配選取率判斷樣本穩定度；低選取率的高勝率會更保守看待。',
            weak: '偏弱',
            insufficient: '資料不足',
            rarityLabels: { kPrismatic: '彩色', kGold: '金色', kSilver: '銀色' },
            augSetLabel: '系列',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · 系列：' + setName : ''} · WR ${wr} · ${games}場${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}，勝率 ${wr}，相對基準 ${lift}，樣本 ${games} 場${desc ? '，' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games}場`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games}場`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games}場</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · 英雄 lift ${lift} · 類型平均 ${avg} · WR ${wr} · ${games}場`,
            setMeta: (lift, avg, wr, games) => `英雄 ${lift} · 類型 ${avg} · WR ${wr} · ${games}場`,
            itemBuildTitle: (name, pick, lift) => `${name} 選取率 ${pick} 勝率${lift}`,
            itemClusterCardTitle: (name, wr, pick, lift, pairLift, singleLift, games) => `${name} · WR ${wr} · pick ${pick} · route ${lift} · pair ${pairLift} · item ${singleLift} · ${games} 場`,
            expected: value => ` · 預期 ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 推薦度 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            leastFitLabel: '最不適配',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 最不適配 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}，tier ${tier}，勝率 ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} 選取率 ${pick}，勝率${lift}`,
            secondaryRoleBadgePick: pick => `選取率 ${pick}`,
            secondaryRoleBadgeLift: lift => `勝率${lift}`,
        },
        en: {
            htmlLang: 'en',
            subtitle: () => `${SHORT_PATCH_ZH}`,
            searchPlaceholderDesktop: 'Search champions (ZH / EN)   Ctrl+F',
            searchPlaceholderMobile: 'Search champions (ZH / EN)',
            searchAria: 'Search champions',
            shownUnit: 'shown',
            tierUnit: 'shown',
            updatesButton: 'Updates',
            updatesKicker: 'This build',
            updatesTitle: 'Recent important changes',
            updatesClose: 'Close recent updates',
            updatesItems: [
                '2026-05-25: Added Single Item Strength, counting any final-slot item to help choose items three through six.',
                '2026-05-25: First-two-item and single-item recommendations now use swipeable carousels with denser mobile cards.',
                '2026-05-25: Augment rows now run strongest to weakest within each rarity instead of splitting into two blocks.',
            ],
            recModeOn: 'Teammate mode: On',
            recModeOff: 'Teammate mode: Off',
            emptyTitle: 'No champions match the current filters',
            emptyCopy: 'Try a different role, or search by Chinese / English champion name.',
            freshness: () => `Updated ${BUILD_DATE} (${TOTAL_GAMES} games) · ${PATCH_LABEL}`,
            sideTitle: 'Recommended teammates',
            sideSub: 'Ranked by teammate fit, then adjusted for damage mix and team gaps.<br>Higher fit is better; confidence summarizes data stability.',
            closeRecs: 'Close recommendations',
            openRecs: n => `Open recommendations (${n})`,
            langToggleLabel: '中',
            langToggleTitle: '切換成中文',
            langToggleAria: 'Switch language',
            removePick: name => `Remove ${name}`,
            pickEmpty: 'Empty',
            maxOnly: n => `You can only pick up to ${n} champions.`,
            pickNoteEmpty: n => `Pick up to ${n}; read fit first, then reason and sample size.`,
            pickNotePartial: want => `This selected group has less complete data, so the list uses known teammate fits first.`,
            pickNoteReady: (want, minGames) => `${want}/${MAX_TEAM_PICKS} picked; pair threshold >= ${minGames} games.`,
            panelEmpty: 'Turn on teammate mode, then click 1-4 champions in the grid. The site will rank the best additions.',
            panelNoData: 'This combination does not have enough pair data yet.',
            detailEmpty: 'No detail data is available for this champion yet.',
            detailClose: 'Close details',
            pairSectionTitle: 'Recommended Pairings',
            pairSectionMeta: 'Fit first, win rate second',
            setSectionTitle: 'Augment Sets',
            setSectionMeta: 'Conservative score; negative can still be relative-best',
            itemSectionTitle: 'Best First Two Items',
            itemSectionMeta: 'boots excluded; left to right is #1 to #3',
            itemClusterSectionTitle: 'Build Routes TLDR',
            itemClusterSectionMeta: 'observed exact 6-item builds; clustered by co-built core items',
            augTypeSectionTitle: 'Recommended Augment Tendencies',
            augTypeSectionMeta: 'Fine-grained first; scores are adjusted against similar role/damage-profile champions.',
            relativeBest: 'Relative Best',
            best: 'Best',
            worst: 'Worst',
            bestAugments: 'Best Augments',
            worstAugments: 'Worst Augments',
            augmentStrengthMeta: 'Strength considers both win rate and pick rate',
            augmentStrengthTip: 'Ranking is led by conservative win-rate lift, with pick rate used as a stability signal; low-pick high-win results are treated more carefully.',
            weak: 'Weak',
            insufficient: 'Not enough data',
            rarityLabels: { kPrismatic: 'Prismatic', kGold: 'Gold', kSilver: 'Silver' },
            augSetLabel: 'Set',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · Set: ' + setName : ''} · WR ${wr} · ${games} games${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}, win rate ${wr}, versus baseline ${lift}, sample ${games} games${desc ? ', ' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games} games`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games} games`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games} games</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · champion lift ${lift} · type average ${avg} · WR ${wr} · ${games} games`,
            setMeta: (lift, avg, wr, games) => `champ ${lift} · type ${avg} · WR ${wr} · ${games} games`,
            itemBuildTitle: (name, pick, lift) => `${name} pick ${pick}, WR ${lift}`,
            itemClusterCardTitle: (name, wr, pick, lift, pairLift, singleLift, games) => `${name} · WR ${wr} · pick ${pick} · route ${lift} · pair ${pairLift} · item ${singleLift} · ${games} games`,
            expected: value => ` · expected ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            leastFitLabel: 'Least fit',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · least fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}, tier ${tier}, win rate ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} pick ${pick}, WR ${lift}`,
            secondaryRoleBadgePick: pick => `pick ${pick}`,
            secondaryRoleBadgeLift: lift => `WR ${lift}`,
        }
    };
    let currentLang = 'zh';
    let updatesOpen = false;
    let activeUpdateTab = 'heroes';
    let filterState = { role: '', q: '' };

    function tr() {
        return COPY[currentLang] || COPY.zh;
    }

    function roleLabel(role) {
        const labels = ROLE_LABELS[currentLang] || ROLE_LABELS.zh;
        return labels[role] || role || '';
    }

    function styleLabel(info) {
        if (!info) return '';
        return currentLang === 'en'
            ? (info.styleNameEn || info.styleName || '')
            : (info.styleNameZh || info.styleName || info.styleNameEn || '');
    }

    function secondaryRoleBadgeSummary(info, role) {
        const copy = tr();
        const style = styleLabel(info) || roleLabel(role);
        const pick = pct(info.pick || 0);
        const liftValue = Number(info.lift || 0);
        const lift = signed(liftValue);
        return {
            style,
            pick,
            lift,
            title: copy.secondaryRoleBadgeTitle(style, pick, lift),
            pickLabel: copy.secondaryRoleBadgePick(pick),
            liftLabel: copy.secondaryRoleBadgeLift(lift),
            toneClass: liftValue > 0.0005 ? 'is-good' : (liftValue < -0.0005 ? 'is-bad' : 'is-even'),
        };
    }

    function secondaryRoleBadgeTooltipHtml(summary) {
        return `
            <span class="alt-role-tooltip" role="tooltip">
                <span class="alt-role-tooltip-style">${escHtml(summary.style)}</span>
                <span class="alt-role-tooltip-pick">${escHtml(summary.pickLabel)}</span>
                <span class="alt-role-tooltip-lift ${summary.toneClass}">${escHtml(summary.liftLabel)}</span>
            </span>
        `;
    }

    function secondaryRoleBadgeIconHtml(role) {
        return ROLE_BADGE_ICONS[role] || '';
    }

    function refreshSecondaryRoleBadges() {
        const role = filterState.role;
        document.querySelectorAll('.champ').forEach(champ => {
            const badge = champ.querySelector('.alt-role-badge');
            if (!badge) return;
            const cid = champ.getAttribute('data-cid');
            const info = DATA.champs[cid] || {};
            const tags = info.tags || [];
            const secondaryRole = tags[1] || '';
            const primaryRole = tags[0] || secondaryRole || '';
            const show = Boolean(role) && secondaryRole === role;
            if (!show) {
                champ.classList.remove('secondary-role-match');
                badge.setAttribute('hidden', '');
                badge.setAttribute('aria-label', '');
                badge.innerHTML = '';
                return;
            }
            const roleInfo = ((info.roleMeta || {})[secondaryRole]) || null;
            const hasDataBadge = Boolean(
                roleInfo &&
                roleInfo.source === 'data' &&
                typeof roleInfo.pick === 'number'
            );
            champ.classList.toggle('secondary-role-match', show && hasDataBadge);
            if (!hasDataBadge) {
                badge.setAttribute('hidden', '');
                badge.setAttribute('aria-label', '');
                badge.innerHTML = '';
                return;
            }
            const summary = secondaryRoleBadgeSummary(roleInfo, secondaryRole);
            badge.removeAttribute('hidden');
            badge.setAttribute('data-alt-role', primaryRole);
            badge.setAttribute('aria-label', summary.title);
            badge.innerHTML = secondaryRoleBadgeIconHtml(primaryRole) + secondaryRoleBadgeTooltipHtml(summary);
        });
    }

    function positionSecondaryRoleTooltip(badge) {
        if (!badge || badge.hasAttribute('hidden')) return;
        const tooltip = badge.querySelector('.alt-role-tooltip');
        if (!tooltip) return;
        badge.classList.remove('tip-right', 'tip-below');
        const viewportPad = 12;
        let rect = tooltip.getBoundingClientRect();
        if (rect.left < viewportPad) {
            badge.classList.add('tip-right');
            rect = tooltip.getBoundingClientRect();
        }
        if (rect.right > window.innerWidth - viewportPad) {
            badge.classList.remove('tip-right');
        }
        const champ = badge.closest('.champ');
        const champRect = champ ? champ.getBoundingClientRect() : null;
        if (champRect && champRect.top < 96) {
            badge.classList.add('tip-below');
        }
    }

    function positionFitChipTooltip(wrap) {
        if (!wrap) return;
        const tooltip = wrap.querySelector('.fit-chip-tooltip');
        if (!tooltip) return;
        wrap.classList.remove('tip-left', 'tip-below');
        const viewportPad = 12;
        const rect = tooltip.getBoundingClientRect();
        if (rect.right > window.innerWidth - viewportPad) {
            wrap.classList.add('tip-left');
        }
        const wrapRect = wrap.getBoundingClientRect();
        if (wrapRect.top < 96) {
            wrap.classList.add('tip-below');
        }
    }

    function isMobileViewport() {
        return window.matchMedia('(max-width: 700px)').matches;
    }

    function searchPlaceholderFor(copy) {
        return isMobileViewport()
            ? copy.searchPlaceholderMobile
            : copy.searchPlaceholderDesktop;
    }

    function updateSearchPlaceholder() {
        const searchEl = document.getElementById('champ-search');
        if (!searchEl) return;
        const copy = tr();
        searchEl.placeholder = searchPlaceholderFor(copy);
        searchEl.setAttribute('aria-label', copy.searchAria);
    }

    function champName(info) {
        if (!info) return '';
        return currentLang === 'en' ? (info.name_en || info.alias || info.name || '') : (info.name_zh || info.name || info.alias || '');
    }

    function augName(aug) {
        if (!aug) return '';
        return currentLang === 'en' ? (aug.name_en || aug.name || '') : (aug.name_zh || aug.name || '');
    }

    function augDesc(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.desc_en || aug.desc || '';
        return aug.desc_zh || aug.desc || '';
    }

    function augSetName(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.set_en || aug.set || '';
        return aug.set_zh || aug.set || aug.set_en || '';
    }

    function setEntryName(entry) {
        if (!entry) return '';
        if (currentLang === 'en') return entry.name_en || entry.name || '';
        return entry.name_zh || entry.name || entry.name_en || '';
    }

    function compactSearchText(value) {
        return String(value || '').toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, '');
    }

    function searchMatchesText(haystack, query) {
        const q = String(query || '').trim().toLowerCase();
        if (!q) return false;
        const text = String(haystack || '').toLowerCase();
        if (text.includes(q)) return true;
        const compactQ = compactSearchText(q);
        return Boolean(compactQ) && compactSearchText(text).includes(compactQ);
    }

    function entrySearchText(entry) {
        const parts = [
            entry?.name,
            entry?.name_zh,
            entry?.name_en,
            entry?.set,
            entry?.set_zh,
            entry?.set_en,
            entry?.slug,
        ];
        (entry?.items || []).forEach(item => {
            parts.push(item.name, item.name_zh, item.name_en, item.id);
        });
        return parts.filter(Boolean).join(' ');
    }

    function currentSearchQuery() {
        const searchEl = document.getElementById('champ-search');
        return searchEl ? searchEl.value : filterState.q;
    }

    function applySearchHighlights(root = document) {
        const q = currentSearchQuery();
        root.querySelectorAll('[data-match-text]').forEach(card => {
            card.classList.toggle('search-hit', searchMatchesText(card.getAttribute('data-match-text') || '', q));
        });
    }

    function buildAugCard(entry, kind) {
        const aug = DATA.augs[entry.id];
        const name = aug ? augName(aug) : '#' + entry.id;
        const icon = aug && aug.icon ? aug.icon : '';
        const rarity = aug ? (aug.rarity || '') : '';
        const desc = augDesc(aug);
        const setName = augSetName(aug);
        const copy = tr();
        const matchText = [
            name,
            aug?.name,
            aug?.name_zh,
            aug?.name_en,
            setName,
            aug?.set,
            aug?.set_zh,
            aug?.set_en,
            aug?.setSlug,
            entry.id,
        ].filter(Boolean).join(' ');
        const titleAttr = copy.augTitle(name, setName, pct(entry.wr), entry.g, desc);
        const tooltip = `
            <div class="aug-tip">
                <div class="aug-tip-name">${escHtml(name)}</div>
                ${setName ? `<div class="aug-tip-set">${copy.augSetLabel}: ${escHtml(setName)}</div>` : ''}
                ${desc ? `<div class="aug-tip-desc">${escHtml(desc)}</div>` : ''}
                <div class="aug-tip-stat">${copy.augTipStat(pct(entry.wr), signed(entry.lift), entry.g)}</div>
            </div>
        `;
        // Augment card carries its own ARIA semantics so screen readers and
        // keyboard users get the same info hover tooltip shows.
        const ariaLabel = copy.augAria(name, pct(entry.wr), signed(entry.lift), entry.g, desc);
        return `
            <div class="aug ${kind} rarity-${rarity}"
                 tabindex="0"
                 data-match-text="${escHtml(matchText)}"
                 aria-label="${escHtml(ariaLabel)}"
                 title="${escHtml(titleAttr)}">
                ${icon ? `<img loading="lazy" src="${icon}" alt="">` : '<div style="width:48px;height:48px;margin:0 auto 4px;background:#2a3142;border-radius:6px"></div>'}
                <div class="aname">${escHtml(name)}</div>
                <div class="awr">${pct(entry.wr)}</div>
                <div class="alift">${signed(entry.lift)} · ${entry.g}場</div>
                ${tooltip}
            </div>
        `;
    }

    const RARITIES = [
        { key: 'kPrismatic', css: 'prismatic' },
        { key: 'kGold',      css: 'gold' },
        { key: 'kSilver',    css: 'silver' },
    ];
    const MATE_LIST_LIMIT_DESKTOP = 8;
    const MATE_LIST_LIMIT_MOBILE = 6;

    function buildRarityRow(items, kind, r) {
        const copy = tr();
        const cards = (items || []).map(e => {
            const cardKind = kind === 'ranked'
                ? (Number(e.lift || 0) >= 0 ? 'good' : 'bad')
                : kind;
            return buildAugCard(e, cardKind);
        }).join('');
        const body = cards
            ? `<div class="aug-list">${cards}</div>`
            : `<div class="aug-list empty-list">${copy.insufficient}</div>`;
        return `
            <div class="rarity-row">
                <div class="rlabel ${r.css}">${copy.rarityLabels[r.key]}</div>
                ${body}
            </div>
        `;
    }

    function renderDetail(cid) {
        const info = DATA.champs[cid];
        if (!info) {
            return `<div class="empty">${tr().detailEmpty}</div>`;
        }
        const copy = tr();
        const top = info.top || {};
        const setInfo = info.sets || {};
        const setTop = setInfo.top || [];
        const itemInfo = info.items || {};
        const singleItemInfo = info.singleItems || {};
        const bootInfo = info.boots || {};
        const itemClusterInfo = info.itemClusters || {};
        const augTypeInfo = info.augTypes || {};
        const augmentRankTitle = currentLang === 'en' ? 'Augment Ranking' : '增幅裝置排行';
        const singleItemTitle = currentLang === 'en' ? 'Single Item Strength' : '單件裝備強度';
        const singleItemMeta = currentLang === 'en'
            ? 'counts any final-slot item; strongest first, swipe for more'
            : '六格中出過就計入；由強到弱，右滑看更多';
        const singleItemBadTitle = currentLang === 'en' ? 'Common Traps' : '常見但不推薦';
        const singleItemBadMeta = currentLang === 'en'
            ? 'high-pick negative-lift items; commonly built, but they drag this champion below baseline'
            : '高出場但負 lift；很多人出，但相對該英雄 baseline 會拉低勝率';
        const topRows = RARITIES.map(r => buildRarityRow(top[r.key], 'ranked', r)).join('');
        const pairs = info.pairs || [];
        const mateLimit = isMobileViewport() ? MATE_LIST_LIMIT_MOBILE : MATE_LIST_LIMIT_DESKTOP;
        const mateTop = pairs.slice(0, mateLimit);
        const mateBot = [...pairs].slice(-mateLimit).reverse();
        const buildMateCard = (entry, kind) => {
            const mate = DATA.champs[String(entry.id)];
            const name = mate ? champName(mate) : ('#' + entry.id);
            const image = mate && mate.image ? mate.image : '';
            const zText = `${entry.z >= 0 ? '+' : ''}${entry.z.toFixed(2)}`;
            const expectedText = entry.expected !== undefined ? copy.expected(pct(entry.expected)) : '';
            const titleAttr = copy.mateTitle(name, pct(entry.wr), expectedText, signed(entry.lift), zText, entry.g);
            return `
                <div class="mate-card ${kind}" title="${escHtml(titleAttr)}">
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:42px;height:42px;border-radius:8px;background:#2a3142"></div>'}
                    <div>
                        <div class="mname">${escHtml(name)}</div>
                        <div class="mwr">${pct(entry.wr)}</div>
                        <div class="mmeta">${copy.mateMetaHtml(signed(entry.lift), zText, entry.g)}</div>
                    </div>
                </div>
            `;
        };
        const buildMateList = (items, kind) => {
            if (!items.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="mate-list">${items.map(entry => buildMateCard(entry, kind)).join('')}</div>`;
        };
        const buildSetSummary = (rows, bad = false) => {
            const visibleSets = rows
                .filter(entry => {
                    const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                    return bad ? metric <= -SET_RESIDUAL_THRESHOLD : metric >= SET_RESIDUAL_THRESHOLD;
                })
                .slice(0, 3);
            if (!visibleSets.length) return '';
            const titleAttr = visibleSets.map(entry => {
                const name = setEntryName(entry);
                const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                return `${name} score ${signed(metric)}, residual ${signed(entry.res)}, lift ${signed(entry.lift)}, set avg ${signed(entry.avg)}, WR ${pct(entry.wr)}, ${entry.g} games`;
            }).join('\\n');
            return `
                <div class="aug-set-summary ${bad ? 'bad' : ''}" title="${escHtml(titleAttr)}">
                    ${visibleSets.map(entry => `<span class="sum-item">${escHtml(setEntryName(entry))}</span>`).join('')}
                </div>
            `;
        };
        const buildFitChip = (entry, kind) => {
            const name = setEntryName(entry);
            const score = kind === 'bad' ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
            const titleAttr = copy.setTitle(name, signed(entry.res), signed(entry.lift), signed(entry.avg), pct(entry.wr), entry.g);
            const pairItems = Array.isArray(entry.items) ? entry.items : [];
            const liftValue = Number(entry.lift ?? entry.res ?? 0);
            const liftClass = liftValue > 0.0005 ? 'is-good' : (liftValue < -0.0005 ? 'is-bad' : 'is-even');
            const itemTitle = copy.itemBuildTitle(name, pct(entry.pick || 0), signed(liftValue));
            const itemIcons = pairItems.length
                ? `<span class="item-pair-icons">${pairItems.map(item => `
                    <span class="item-pair-icon-wrap">
                        ${item.icon ? `<img src="${escHtml(item.icon)}" alt="" loading="lazy">` : ''}
                    </span>
                `).join('')}</span>`
                : '';
            if (pairItems.length) {
                return `
                    <span class="fit-chip-wrap" tabindex="0" aria-label="${escHtml(itemTitle)}">
                        <span class="fit-chip ${kind} item-build-chip">
                            ${itemIcons}<span class="fit-chip-label">${escHtml(name)}</span>
                        </span>
                        <span class="fit-chip-tooltip" role="tooltip">
                            <span class="fit-tip-name">${escHtml(name)}</span>
                            <span class="fit-tip-pick">${escHtml(copy.secondaryRoleBadgePick(pct(entry.pick || 0)))}</span>
                            <span class="fit-tip-lift ${liftClass}">${escHtml(copy.secondaryRoleBadgeLift(signed(liftValue)))}</span>
                        </span>
                    </span>
                `;
            }
            return `
                <span class="fit-chip ${kind}" title="${escHtml(`${name} ${signed(score)} · ${titleAttr}`)}">
                    ${itemIcons}<span class="fit-chip-label">${escHtml(name)}</span>
                </span>
            `;
        };
        const buildFitList = (rows, kind) => {
            if (!rows || !rows.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="fit-chip-list">${rows.slice(0, 3).map(entry => buildFitChip(entry, kind)).join('')}</div>`;
        };
        const buildItemCard = (entry, options = {}) => {
            const name = setEntryName(entry);
            const pairItems = Array.isArray(entry.items) ? entry.items : [];
            const liftValue = Number(entry.lift ?? entry.res ?? 0);
            const titleForItemCard = copy.itemBuildCardTitle || ((itemName, wr, pick, lift, games) => (
                currentLang === 'en'
                    ? `${itemName} · WR ${wr} · pick ${pick} · lift ${lift} · ${games} games`
                    : `${itemName} · WR ${wr} · 挑選率 ${pick} · 勝率 ${lift} · ${games} 場`
            ));
            const titleAttr = options.itemCluster && copy.itemClusterCardTitle
                ? copy.itemClusterCardTitle(
                    name,
                    pct(entry.wr || 0),
                    pct(entry.pick || 0),
                    signed(liftValue),
                    signed(Number(entry.pairLift || 0)),
                    signed(Number(entry.singleLift || 0)),
                    entry.g || 0,
                )
                : titleForItemCard(
                    name,
                    pct(entry.wr || 0),
                    pct(entry.pick || 0),
                    signed(liftValue),
                    entry.g || 0,
                );
            const iconLimit = options.itemCluster ? 6 : (options.singleItem ? 1 : 2);
            const icons = pairItems.slice(0, iconLimit).map(item => (
                item.icon
                    ? `<img class="item-build-icon" src="${escHtml(item.icon)}" alt="" loading="lazy">`
                    : '<span class="item-build-icon"></span>'
            )).join('');
            const placeholderCount = options.itemCluster ? 6 : (options.singleItem ? 1 : 2);
            const paddedIcons = icons || Array.from(
                { length: placeholderCount },
                () => '<span class="item-build-icon"></span>'
            ).join('');
            const cardClass = options.itemCluster
                ? 'item-build-card item-cluster-card'
                : (options.singleItem ? 'item-build-card single-item-card' : 'item-build-card');
            const wrClass = options.singleItem && !options.bootItem && liftValue < -0.0005
                ? 'item-build-wr is-bad'
                : 'item-build-wr';
            const matchText = entrySearchText(entry);
            return `
                <div class="${cardClass}" tabindex="0" data-match-text="${escHtml(matchText)}" title="${escHtml(titleAttr)}" aria-label="${escHtml(titleAttr)}">
                    <div class="item-build-icons">${paddedIcons}</div>
                    <div class="${wrClass}">${pct(entry.wr || 0)}</div>
                    <div class="item-build-name"><span>${escHtml(name)}</span></div>
                </div>
            `;
        };
        const buildItemCarousel = (rows, options = {}) => {
            if (!rows || !rows.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            const carouselClasses = ['item-build-carousel'];
            if (options.itemCluster) {
                carouselClasses.push('item-build-grid', 'item-cluster-grid');
            } else if (options.itemPairGrid) {
                carouselClasses.push('item-build-grid', 'item-pair-grid');
            } else if (options.singleItem && !options.bootItem) {
                carouselClasses.push('item-build-grid', 'single-item-grid');
            }
            const carouselClass = carouselClasses.join(' ');
            return `<div class="${carouselClass}">${rows.map(entry => buildItemCard(entry, options)).join('')}</div>`;
        };
        const buildItemSectionFromRows = (title, meta, rows, options = {}) => {
            if (!rows || !rows.length) return '';
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            return `
                <div class="detail-section">
                    <div class="detail-section-head">
                        <h3>${title}</h3>
                        ${metaHtml}
                    </div>
                    ${buildItemCarousel(rows, options)}
                </div>
            `;
        };
        const selectCommonTrapRows = (payload, maxRows = 4) => {
            const sourceRows = (payload && (payload.popularBad || payload.bot)) || [];
            const badRows = sourceRows
                .filter(entry => Number(entry.lift ?? 0) <= -0.01);
            if (!badRows.length) return [];
            return [...badRows]
                .sort((a, b) => (
                    Number(b.pick ?? b.pick_rate ?? 0) - Number(a.pick ?? a.pick_rate ?? 0)
                    || Number(b.g ?? b.games ?? 0) - Number(a.g ?? a.games ?? 0)
                    || Number(a.lift ?? 0) - Number(b.lift ?? 0)
                    || String(a.name_en || '').localeCompare(String(b.name_en || ''))
                ))
                .slice(0, maxRows);
        };
        const closeFitRows = (rows, minRows = 1, maxRows = 3, options = {}) => {
            if (!rows || !rows.length) return [];
            const topScore = rows[0].score ?? rows[0].res ?? 0;
            const closeGap = 0.004;
            const selected = [];
            const rowKey = (entry) => String(
                entry.slug ?? entry.name_zh ?? entry.name_en ?? entry.name ?? selected.length
            );
            const selectedSlugs = new Set();
            const addSelected = (entry) => {
                const key = rowKey(entry);
                if (selectedSlugs.has(key)) return false;
                selected.push(entry);
                selectedSlugs.add(key);
                return true;
            };
            rows.forEach((entry, idx) => {
                if (selected.length >= maxRows) return;
                const score = entry.score ?? entry.res ?? topScore;
                if (idx === 0 || (topScore - score) <= closeGap) {
                    addSelected(entry);
                }
            });
            if (options.includeHighestPick) {
                const highestPickRow = rows.reduce((best, entry) => {
                    const entryPick = Number(entry.pick ?? entry.pick_rate ?? 0);
                    const bestPick = Number(best.pick ?? best.pick_rate ?? 0);
                    if (entryPick !== bestPick) return entryPick > bestPick ? entry : best;
                    const entryGames = Number(entry.g ?? entry.games ?? 0);
                    const bestGames = Number(best.g ?? best.games ?? 0);
                    if (entryGames !== bestGames) return entryGames > bestGames ? entry : best;
                    const entryScore = Number(entry.score ?? entry.res ?? 0);
                    const bestScore = Number(best.score ?? best.res ?? 0);
                    return entryScore > bestScore ? entry : best;
                }, rows[0]);
                const highestPickKey = rowKey(highestPickRow);
                if (!selectedSlugs.has(highestPickKey)) {
                    if (selected.length < maxRows) {
                        addSelected(highestPickRow);
                    } else if (selected.length) {
                        selected[selected.length - 1] = highestPickRow;
                        selectedSlugs.clear();
                        selected.forEach(entry => selectedSlugs.add(rowKey(entry)));
                    }
                }
            }
            if (selected.length < minRows) {
                for (const entry of rows) {
                    if (selected.length >= minRows || selected.length >= maxRows) break;
                    addSelected(entry);
                }
            }
            return selected;
        };
        const buildAffinitySection = (title, meta, payload, options = {}) => {
            if (options.itemCarousel) {
                const rows = (payload && payload.top) || [];
                if (!rows.length) return '';
                const itemMeta = currentLang === 'en'
                    ? 'boots excluded; strongest first, swipe for more'
                    : '不含鞋子；勝率分數由高到低，右滑看更多';
                const displayMeta = (options.singleItem || options.itemCluster) && meta ? meta : itemMeta;
                const metaHtml = `<span class="section-meta">${displayMeta}</span>`;
                return `
                    <div class="detail-section">
                        <div class="detail-section-head">
                            <h3>${title}</h3>
                            ${metaHtml}
                        </div>
                        ${buildItemCarousel(rows, {
                            singleItem: Boolean(options.singleItem),
                            itemCluster: Boolean(options.itemCluster),
                            itemPairGrid: Boolean(options.itemPairGrid),
                        })}
                    </div>
                `;
            }
            const bestRows = closeFitRows(
                (payload && payload.top) || [],
                options.minRows || 1,
                options.maxRows || 3,
                { includeHighestPick: Boolean(options.includeHighestPick) },
            );
            if (!bestRows.length) return '';
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            return `
                <div class="detail-section">
                    <div class="detail-section-head">
                        <h3>${title}</h3>
                        ${metaHtml}
                    </div>
                    ${buildFitList(bestRows, 'good')}
                </div>
            `;
        };
        const emptyDetailSection = (title, meta = '') => `
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${title}</h3>
                    ${meta ? `<span class="section-meta">${meta}</span>` : ''}
                </div>
                <div class="mate-list empty-list">${copy.insufficient}</div>
            </div>
        `;
        const buildItemPanel = (title, meta, payload, options = {}) => (
            buildAffinitySection(title, meta, payload, options) || emptyDetailSection(title, meta)
        );
        const buildSingleItemPanel = (title, meta, payload) => {
            const goodSection = buildAffinitySection(title, meta, payload, { itemCarousel: true, singleItem: true });
            const badSection = buildItemSectionFromRows(
                singleItemBadTitle,
                singleItemBadMeta,
                selectCommonTrapRows(payload, 4),
                { singleItem: true },
            );
            if (!goodSection && !badSection) {
                return emptyDetailSection(title, meta);
            }
            return `${goodSection || ''}${badSection || ''}`;
        };
        const buildDetailTabSet = (scope, tabs, extraClass = '') => {
            const name = `detail-${scope}-${cid}`;
            const inputs = tabs.map((tab, idx) => {
                const inputId = `${name}-${tab.key}`;
                return `<input class="detail-tab-input" type="radio" id="${inputId}" name="${name}" ${idx === 0 ? 'checked' : ''} aria-label="${escHtml(tab.label)}">`;
            }).join('');
            const labels = tabs.map(tab => {
                const inputId = `${name}-${tab.key}`;
                return `<label class="detail-tab-label" id="${inputId}-label" role="tab" for="${inputId}">${escHtml(tab.label)}</label>`;
            }).join('');
            const panels = tabs.map(tab => {
                const inputId = `${name}-${tab.key}`;
                return `<section class="detail-tab-panel" role="tabpanel" aria-labelledby="${inputId}-label">${tab.content}</section>`;
            }).join('');
            return `
                <div class="detail-tabset ${extraClass}">
                    ${inputs}
                    <div class="detail-tab-list" role="tablist">${labels}</div>
                    <div class="detail-tab-panels">${panels}</div>
                </div>
            `;
        };
        const mainTabLabels = currentLang === 'en'
            ? { items: 'Items', augments: 'Augments', teammates: 'Teammates' }
            : { items: '出裝', augments: '增幅裝置', teammates: '最佳搭檔' };
        const itemTabLabels = currentLang === 'en'
            ? { routes: '6-item routes', single: 'Single items', pairs: 'First two', boots: 'Boots' }
            : { routes: '六件路線', single: '單件', pairs: '前兩件', boots: '鞋子' };
        const bootItemTitle = currentLang === 'en' ? 'Recommended Boots' : '推薦鞋子';
        const bootItemMeta = currentLang === 'en'
            ? 'boots only; ranked by conservative win-rate lift and pick stability'
            : '只看鞋子；用保守勝率提升與選取率穩定度排序';
        const itemTabContent = buildDetailTabSet('items', [
            {
                key: 'routes',
                label: itemTabLabels.routes,
                content: buildItemPanel(
                    copy.itemClusterSectionTitle || (currentLang === 'en' ? 'Build Routes TLDR' : '\u51fa\u88dd\u8def\u7dda TLDR'),
                    copy.itemClusterSectionMeta || (currentLang === 'en'
                        ? 'observed exact 6-item builds; clustered by co-built core items'
                        : '\u5be6\u969b\u51fa\u73fe\u904e\u7684\u5b8c\u6574\u516d\u4ef6\uff1b\u4f9d\u6838\u5fc3\u88dd\u5099\u5171\u73fe\u5206\u7fa4'),
                    itemClusterInfo,
                    { itemCarousel: true, itemCluster: true },
                ),
            },
            {
                key: 'single',
                label: itemTabLabels.single,
                content: buildSingleItemPanel(
                    singleItemTitle,
                    singleItemMeta,
                    singleItemInfo,
                ),
            },
            {
                key: 'pairs',
                label: itemTabLabels.pairs,
                content: buildItemPanel(
                    copy.itemSectionTitle,
                    copy.itemSectionMeta,
                    itemInfo,
                    { itemCarousel: true, itemPairGrid: true },
                ),
            },
            {
                key: 'boots',
                label: itemTabLabels.boots,
                content: buildItemPanel(
                    bootItemTitle,
                    bootItemMeta,
                    bootInfo,
                    { itemCarousel: true, singleItem: true, bootItem: true },
                ),
            },
        ], 'detail-sub-tabs');
        const augmentTabContent = `
            <div class="detail-section">
                <span class="section-meta augment-strength-meta">
                    ${copy.augmentStrengthMeta}
                    <span class="meta-help-wrap">
                        <button class="meta-help" type="button" aria-label="${escHtml(copy.augmentStrengthTip)}">?</button>
                        <span class="meta-help-tip" role="tooltip">${escHtml(copy.augmentStrengthTip)}</span>
                    </span>
                </span>
                <div class="detail-col best">
                    <div class="detail-col-heading">
                        <h3>${augmentRankTitle}</h3>
                        ${buildSetSummary(setTop)}
                    </div>
                    ${topRows}
                </div>
            </div>
            ${buildAffinitySection(copy.augTypeSectionTitle, copy.augTypeSectionMeta, augTypeInfo)}
        `;
        const teammateTabContent = `
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${copy.pairSectionTitle}</h3>
                    <span class="section-meta">${copy.pairSectionMeta}</span>
                </div>
                <div class="detail-cols">
                    <div class="detail-col best">
                        <h3>${copy.best}</h3>
                        ${buildMateList(mateTop, 'good')}
                    </div>
                    <div class="detail-col worst">
                        <h3>${copy.worst}</h3>
                        ${buildMateList(mateBot, 'bad')}
                    </div>
                </div>
            </div>
        `;
        const detailTabs = buildDetailTabSet('main', [
            { key: 'items', label: mainTabLabels.items, content: itemTabContent },
            { key: 'augments', label: mainTabLabels.augments, content: augmentTabContent },
            { key: 'teammates', label: mainTabLabels.teammates, content: teammateTabContent },
        ], 'detail-main-tabs');
        return `
            <button class="detail-close" type="button" title="${escHtml(copy.detailClose)}" aria-label="${escHtml(copy.detailClose)}">&times;</button>
            <div class="detail-head">
                ${info.image ? `<img class="detail-avatar" loading="lazy" src="${info.image}" alt="">` : ''}
                <span class="cname" id="detail-title-${cid}">${escHtml(champName(info))}</span>
            </div>
            ${detailTabs}
        `;
    }

    const REC_LIST_LIMIT = 12;
    const MAX_TEAM_PICKS = 4;
    let detailSelected = null;
    let recommendMode = false;
    let recModalOpen = false;
    let teamPicks = [];
    let pickNotice = '';

    function zFmt(x) {
        return `${x >= 0 ? '+' : ''}${x.toFixed(2)}`;
    }

    // Find the last .champ in the same visual row as `clicked` (same offsetTop).
    // Tier-grid is a CSS grid so offsetTop tells us the row reliably across
    // viewport widths.
    function lastChampInRow(clicked) {
        const grid = clicked.parentElement;
        const topPx = clicked.offsetTop;
        const champs = grid.querySelectorAll(':scope > .champ');
        let last = clicked;
        for (const c of champs) {
            if (Math.abs(c.offsetTop - topPx) < 2) last = c;
        }
        return last;
    }

    function syncPickDecorations() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const idx = teamPicks.indexOf(cid);
            champ.classList.toggle('pick-selected', idx !== -1);
            if (idx !== -1) {
                champ.setAttribute('data-pick-rank', String(idx + 1));
            } else {
                champ.removeAttribute('data-pick-rank');
            }
        });
    }

    function adBin(adShare) {
        if (adShare < 0.35) return '<35% AD';
        if (adShare < 0.45) return '35-45% AD';
        if (adShare < 0.55) return '45-55% AD';
        if (adShare < 0.65) return '55-65% AD';
        return '>=65% AD';
    }

    function countGroup(projectedCount) {
        if (projectedCount < 0.5) return '0';
        if (projectedCount < 1.5) return '1';
        return '2+';
    }

    function frontGroup(projectedCount) {
        return countGroup(projectedCount) + ' front';
    }

    function tableValue(name, key) {
        const config = DATA.recommendation_composition || {};
        const tables = config.tables || {};
        const table = tables[name] || {};
        const raw = table[key];
        return Number.isFinite(Number(raw)) ? Number(raw) : 0;
    }

    function teamComposition(ids) {
        const config = DATA.recommendation_composition || {};
        const thresholds = config.lack_thresholds || {};
        const sums = { phys: 0, magic: 0, true: 0, wave: 0, cc: 0, engage: 0, damage: 0, poke: 0, sustain: 0, front: 0 };
        const roles = { Mage: 0, Marksman: 0 };
        let frontCount = 0;
        ids.forEach(rawId => {
            const info = DATA.champs[String(rawId)];
            if (!info) return;
            const comp = info.comp || {};
            Object.keys(sums).forEach(key => {
                sums[key] += Number(comp[key] || 0);
            });
            if (Number(comp.front || 0) >= 2.0) frontCount += 1;
            (info.tags || []).forEach(tag => {
                if (Object.prototype.hasOwnProperty.call(roles, tag)) roles[tag] += 1;
            });
        });

        const size = Math.max(1, ids.length);
        const projection = 5 / size;
        const thresholdScale = size / 5;
        const adDen = sums.phys + sums.magic;
        const adShare = adDen > 0 ? sums.phys / adDen : 0.5;
        const lacks = {};
        ['wave', 'cc', 'engage', 'damage', 'poke', 'sustain', 'front'].forEach(key => {
            const threshold = Number(thresholds[key] || 0);
            lacks[key] = threshold > 0 && sums[key] < threshold * thresholdScale;
        });
        const allLacks = Object.values(lacks).filter(Boolean).length;
        return {
            adBin: adBin(adShare),
            frontGroup: frontGroup(frontCount * projection),
            mageGroup: countGroup(roles.Mage * projection),
            marksmanGroup: countGroup(roles.Marksman * projection),
            waveGroup: lacks.wave ? 'wave lack' : 'wave ok',
            engageGroup: lacks.engage ? 'engage lack' : 'engage ok',
            pokeGroup: lacks.poke ? 'poke lack' : 'poke ok',
            allLacksGroup: countGroup(allLacks * projection),
        };
    }

    function teamCompositionScore(ids) {
        if (!ids.length) return 0;
        const config = DATA.recommendation_composition || {};
        const weights = config.table_weights || {};
        const clamp = Number(config.clamp || 0.05);
        const comp = teamComposition(ids);
        let score = 0;
        score += Number(weights.ad_front || 0) * tableValue('ad_front', `${comp.frontGroup}|${comp.adBin}`);
        score += Number(weights.poke_front || 0) * tableValue('poke_front', `${comp.frontGroup}|${comp.pokeGroup}`);
        score += Number(weights.wave_engage || 0) * tableValue('wave_engage', `${comp.waveGroup}|${comp.engageGroup}`);
        score += Number(weights.all_lacks || 0) * tableValue('all_lacks', comp.allLacksGroup);
        score += Number(weights.mage_ad || 0) * tableValue('mage_ad', `${comp.mageGroup}|${comp.adBin}`);
        score += Number(weights.marksman_ad || 0) * tableValue('marksman_ad', `${comp.marksmanGroup}|${comp.adBin}`);
        const sizeWeight = Math.min(1, Math.max(0, (ids.length - 1) / 4));
        return Math.max(-clamp, Math.min(clamp, score)) * sizeWeight;
    }

    function clampAbs(value, maxAbs) {
        return Math.max(-maxAbs, Math.min(maxAbs, value));
    }

    function damageMixScore(comp) {
        const mix = (DATA.recommendation_composition || {}).damage_mix || {};
        const target = Number(mix.target_ad_share || 0.4);
        return -Math.abs(Number(comp.adShare || 0.5) - target);
    }

    function aggregateRecommendations() {
        if (!teamPicks.length) return [];
        const pickedSet = new Set(teamPicks);
        const want = teamPicks.length;
        const compositionConfig = DATA.recommendation_composition || {};
        const compositionWeight = Number(compositionConfig.weight || 0);
        const damageMixConfig = compositionConfig.damage_mix || {};
        const damageMixWeight = Number(damageMixConfig.weight || 0);
        const damageMixClamp = Number(damageMixConfig.clamp || 0.025);
        const beforeComposition = teamCompositionScore(teamPicks);
        const beforeTeamComp = teamComposition(teamPicks);
        const beforeDamageMix = damageMixScore(beforeTeamComp);
        const byCandidate = new Map();
        teamPicks.forEach(anchorId => {
            const info = DATA.champs[anchorId];
            if (!info) return;
            (info.pairs || []).forEach(entry => {
                const candidateId = String(entry.id);
                if (pickedSet.has(candidateId)) return;
                const row = byCandidate.get(candidateId) || {
                    id: candidateId,
                    coverage: 0,
                    zSum: 0,
                    liftSum: 0,
                    wrSum: 0,
                    minGames: Number.POSITIVE_INFINITY,
                };
                row.coverage += 1;
                row.zSum += entry.z;
                row.liftSum += entry.lift;
                row.wrSum += entry.wr;
                row.minGames = Math.min(row.minGames, entry.g);
                byCandidate.set(candidateId, row);
            });
        });
        return [...byCandidate.values()]
            .map(row => {
                const coverageRatio = row.coverage / want;
                const pairFitScore = row.liftSum / want;
                const afterIds = [...teamPicks, row.id];
                const afterTeamComp = teamComposition(afterIds);
                const compositionDelta = teamCompositionScore(afterIds) - beforeComposition;
                const compositionCoverage = 0.5 + 0.5 * coverageRatio;
                const tableContribution = compositionWeight * compositionDelta * compositionCoverage;
                const damageMixDelta = damageMixScore(afterTeamComp) - beforeDamageMix;
                const damageMixContribution = clampAbs(
                    damageMixWeight * damageMixDelta * compositionCoverage,
                    damageMixClamp,
                );
                const compositionContribution = tableContribution + damageMixContribution;
                return {
                    ...row,
                    full: row.coverage === want,
                    coverageRatio,
                    pairFitScore,
                    compositionDelta,
                    tableContribution,
                    damageMixDelta,
                    damageMixContribution,
                    compositionContribution,
                    beforeAdShare: beforeTeamComp.adShare,
                    afterAdShare: afterTeamComp.adShare,
                    beforeFrontGroup: beforeTeamComp.frontGroup,
                    afterFrontGroup: afterTeamComp.frontGroup,
                    beforePokeGroup: beforeTeamComp.pokeGroup,
                    afterPokeGroup: afterTeamComp.pokeGroup,
                    beforeWaveGroup: beforeTeamComp.waveGroup,
                    afterWaveGroup: afterTeamComp.waveGroup,
                    beforeEngageGroup: beforeTeamComp.engageGroup,
                    afterEngageGroup: afterTeamComp.engageGroup,
                    beforeAllLacksGroup: beforeTeamComp.allLacksGroup,
                    afterAllLacksGroup: afterTeamComp.allLacksGroup,
                    fitScore: pairFitScore + compositionContribution,
                    zAvg: row.zSum / row.coverage,
                    liftAvg: row.liftSum / row.coverage,
                    wrAvg: row.wrSum / row.coverage,
                };
            })
            .sort((a, b) =>
                b.fitScore - a.fitScore ||
                b.pairFitScore - a.pairFitScore ||
                b.liftAvg - a.liftAvg ||
                b.zAvg - a.zAvg ||
                Number(b.full) - Number(a.full) ||
                b.coverage - a.coverage ||
                b.minGames - a.minGames
            );
    }

    function recScoreClass(score) {
        if (score >= 0.09) return 'fit-top';
        if (score >= 0.07) return 'fit-strong';
        if (score >= 0.05) return 'fit-solid';
        if (score >= 0.02) return 'fit-soft';
        return 'fit-floor';
    }

    function confidenceLabel(row) {
        const strongCoverage = row.coverageRatio >= 0.75;
        const enoughGames = row.minGames >= 60;
        const signal = Math.abs(row.zAvg || 0);
        if (strongCoverage && enoughGames && signal >= 1.0) {
            return currentLang === 'en' ? 'High confidence' : '可信度高';
        }
        if (row.coverageRatio >= 0.5 && row.minGames >= 40 && signal >= 0.6) {
            return currentLang === 'en' ? 'Medium confidence' : '可信度中';
        }
        return currentLang === 'en' ? 'Early signal' : '樣本偏早';
    }

    function compReasonLabel(row) {
        const value = row.compositionContribution || 0;
        const abs = Math.abs(value);
        const mixValue = row.damageMixContribution || 0;
        if (Math.abs(mixValue) >= 0.004) {
            const addsAD = Number(row.afterAdShare || 0) > Number(row.beforeAdShare || 0);
            if (mixValue > 0) {
                if (currentLang === 'en') return addsAD ? `adds AD ${signed(value)}` : `adds AP ${signed(value)}`;
                return addsAD ? `補AD ${signed(value)}` : `補AP ${signed(value)}`;
            }
            if (currentLang === 'en') return `damage skew ${signed(value)}`;
            return `傷害偏科 ${signed(value)}`;
        }
        if (value > 0.001) {
            if (row.beforeFrontGroup !== row.afterFrontGroup && row.afterFrontGroup !== '0 front') {
                return currentLang === 'en' ? `adds frontline ${signed(value)}` : `補前排 ${signed(value)}`;
            }
            if (row.beforePokeGroup === 'poke lack' && row.afterPokeGroup === 'poke ok') {
                return currentLang === 'en' ? `adds poke ${signed(value)}` : `補Poke ${signed(value)}`;
            }
            if (row.beforeWaveGroup === 'wave lack' && row.afterWaveGroup === 'wave ok') {
                return currentLang === 'en' ? `adds waveclear ${signed(value)}` : `補清兵 ${signed(value)}`;
            }
            if (row.beforeEngageGroup === 'engage lack' && row.afterEngageGroup === 'engage ok') {
                return currentLang === 'en' ? `adds engage ${signed(value)}` : `補開戰 ${signed(value)}`;
            }
            if (row.beforeAllLacksGroup !== row.afterAllLacksGroup) {
                return currentLang === 'en' ? `rounds team ${signed(value)}` : `補陣容 ${signed(value)}`;
            }
        }
        if (abs < 0.001) return currentLang === 'en' ? 'team neutral' : '陣容中性';
        if (value > 0) return currentLang === 'en' ? `team +${(value * 100).toFixed(1)}%` : `陣容加分 ${signed(value)}`;
        return currentLang === 'en' ? `team ${(value * 100).toFixed(1)}%` : `陣容扣分 ${signed(value)}`;
    }

    function recMetaHtml(row, name) {
        const copy = tr();
        const scoreClass = row.leastFit ? 'fit-worst' : recScoreClass(row.fitScore);
        const scoreLabel = row.leastFit
            ? copy.leastFitLabel
            : (currentLang === 'en' ? 'Fit' : '推薦度');
        const confidence = confidenceLabel(row);
        const pairClass = row.pairFitScore >= 0 ? 'good' : 'bad';
        const compClass = row.compositionContribution > 0.001 ? 'good' : (row.compositionContribution < -0.001 ? 'bad' : 'muted');
        const pairLabel = currentLang === 'en'
            ? `pair ${signed(row.pairFitScore)}`
            : `搭配 ${signed(row.pairFitScore)}`;
        return `
            <span class="rec-titleline">
                <span class="rec-name">${escHtml(name)}</span>
                <span class="rec-score ${scoreClass}">${scoreLabel} ${signed(row.fitScore)}</span>
            </span>
            <span class="rec-detail">
                <span class="${pairClass}">${escHtml(pairLabel)}</span>
                <span class="${compClass}">${escHtml(compReasonLabel(row))}</span>
                <span class="muted">${escHtml(confidence)}</span>
            </span>
        `;
    }

    function recommendationDisplayRows(recs) {
        if (!recs.length) return [];
        const rows = recs.slice(0, REC_LIST_LIMIT).map(row => ({ ...row, leastFit: false }));
        if (recs.length <= 1) return rows;

        const worst = { ...recs[recs.length - 1], leastFit: true };
        if (recs.length > REC_LIST_LIMIT) {
            rows[REC_LIST_LIMIT - 1] = worst;
        } else {
            rows[rows.length - 1] = { ...rows[rows.length - 1], leastFit: true };
        }
        return rows;
    }

    function renderSidePanel() {
        const copy = tr();
        const shell = document.querySelector('.app-shell');
        const panel = document.getElementById('side-panel');
        const fab = document.getElementById('rec-fab');
        const slots = document.getElementById('pick-slots');
        const note = document.getElementById('pick-note');
        const recList = document.getElementById('rec-list');
        if (!shell || !panel || !slots || !note || !recList) return;

        const showPanel = recommendMode && teamPicks.length > 0;
        const isMobile = window.matchMedia('(max-width: 700px)').matches;
        if (!showPanel || !isMobile) recModalOpen = false;
        shell.classList.toggle('with-side-panel', showPanel && !isMobile);
        document.body.classList.toggle('rec-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-hidden', !showPanel || (isMobile && !recModalOpen));
        if (fab) {
            fab.classList.toggle('is-hidden', !(showPanel && isMobile && !recModalOpen));
            fab.textContent = copy.openRecs(teamPicks.length);
        }
        if (!showPanel) return;

        const chips = [];
        teamPicks.forEach((cid, idx) => {
            const info = DATA.champs[cid];
            const name = info ? champName(info) : ('#' + cid);
            const image = info && info.image ? info.image : '';
            chips.push(
                `<button class="pick-chip" type="button" data-remove-cid="${cid}" title="${escHtml(copy.removePick(name))}">` +
                `<span class="ord">${idx + 1}</span>` +
                (image ? `<img loading="lazy" src="${image}" alt="">` : '') +
                `<span>${escHtml(name)}</span></button>`
            );
        });
        for (let i = teamPicks.length; i < MAX_TEAM_PICKS; i += 1) {
            chips.push(`<div class="pick-chip empty"><span class="ord">${i + 1}</span>${copy.pickEmpty}</div>`);
        }
        slots.innerHTML = chips.join('');

        const recs = aggregateRecommendations();
        const want = teamPicks.length;
        const hasFull = recs.some(row => row.full);
        if (pickNotice) {
            note.textContent = pickNotice;
        } else if (!teamPicks.length) {
            note.textContent = copy.pickNoteEmpty(MAX_TEAM_PICKS);
        } else if (want > 1 && !hasFull) {
            note.textContent = copy.pickNotePartial(want);
        } else {
            note.textContent = copy.pickNoteReady(want, DATA.min_synergy_games);
        }

        if (!teamPicks.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelEmpty}</div>`;
            return;
        }
        if (!recs.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelNoData}</div>`;
            return;
        }

        recList.innerHTML = recommendationDisplayRows(recs).map((row, idx) => {
            const info = DATA.champs[row.id];
            const name = info ? champName(info) : ('#' + row.id);
            const image = info && info.image ? info.image : '';
            const confidence = confidenceLabel(row);
            const meta = recMetaHtml(row, name);
            const title = row.leastFit
                ? copy.leastFitRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence)
                : copy.recRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence);
            return `
                <button class="rec-row${row.leastFit ? ' least-fit' : ''}" type="button" data-cid="${row.id}" title="${escHtml(title)}">
                    <span class="rec-rank">${idx + 1}</span>
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:40px;height:40px;border-radius:8px;background:#2a3142"></div>'}
                    <span class="rec-main">
                        <span class="rec-meta">${meta}</span>
                    </span>
                </button>
            `;
        }).join('');
    }

    function updateChampCardCopy() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const info = DATA.champs[cid];
            if (!info) return;
            const name = champName(info);
            const alias = info.alias || '';
            const tier = champ.getAttribute('data-tier') || '';
            const wr = champ.getAttribute('data-wr') || '';
            const games = champ.getAttribute('data-games') || '';
            const raw = champ.getAttribute('data-raw-wr') || '';
            const nameEl = champ.querySelector('.name');
            if (nameEl) nameEl.textContent = name;
            champ.setAttribute('title', tr().champCardTitle(name, wr, games, raw));
            champ.setAttribute('aria-label', tr().champCardAria(name, alias, tier, wr));
        });
    }

    function changeLabels() {
        const changes = DATA.patchChanges || {};
        const range = changes.baselinePatch && changes.currentPatch
            ? `${changes.baselinePatch} -> ${changes.currentPatch}`
            : PATCH_LABEL;
        if (currentLang === 'en') {
            return {
                button: 'Patch changes',
                kicker: range,
                title: 'What moved this patch',
                close: 'Close patch changes',
                tabs: { heroes: 'Heroes', items: 'Items', champItems: 'Hero x item' },
                summaryBase: 'Compared with',
                summarySample: 'Sample',
                summaryRule: 'Signal',
                summaryRuleText: `heroes >= ${changes.minHeroGames || 0} games`,
                noData: 'No baseline patch data is available for this build.',
                heroUp: 'Biggest hero climbs',
                heroDown: 'Biggest hero drops',
                itemUp: 'Item win-rate climbs',
                itemDown: 'Item win-rate drops',
                champItemUp: 'Hero-item spikes',
                champItemDown: 'Hero-item slumps',
                itemNote: 'Core items only; boots and augment-gated rewards are excluded. Hero x item compares item lift against that hero baseline.',
                games: 'games',
                uses: 'uses',
                lift: 'lift',
            };
        }
        return {
            button: '版本變動',
            kicker: range,
            title: '這版誰變多了',
            close: '關閉版本變動',
            tabs: { heroes: '英雄', items: '裝備', champItems: '英雄×裝備' },
            summaryBase: '比較基準',
            summarySample: '樣本',
            summaryRule: '訊號門檻',
            summaryRuleText: `英雄 >= ${changes.minHeroGames || 0} 場`,
            noData: '這次 build 沒有可比較的上一版資料。',
            heroUp: '勝率提升最多',
            heroDown: '勝率下降最多',
            itemUp: '裝備勝率提升',
            itemDown: '裝備勝率下降',
            champItemUp: '搭配突然變好',
            champItemDown: '搭配突然變差',
            itemNote: '只看核心裝備，不含鞋子與增幅限定獎勵；英雄×裝備比較的是相對該英雄 baseline 的 lift 變動。',
            games: '場',
            uses: '次',
            lift: 'lift',
        };
    }

    function fmtInt(n) {
        return Number(n || 0).toLocaleString(currentLang === 'en' ? 'en-US' : 'zh-TW');
    }

    function localizedEntityName(entity) {
        if (!entity) return '';
        return currentLang === 'en'
            ? (entity.name_en || entity.alias || entity.name || entity.id || '')
            : (entity.name_zh || entity.name || entity.name_en || entity.alias || entity.id || '');
    }

    function changeDeltaClass(value) {
        return Number(value || 0) >= 0 ? 'up' : 'down';
    }

    function changeHeroRow(row) {
        const labels = changeLabels();
        const name = localizedEntityName(row);
        const title = `${name} ${signed(row.delta || 0)}`;
        const meta = `${pct(row.baseline_wr || 0)} -> ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.games} · ${row.baseline_tier || ''}->${row.current_tier || ''}`;
        return `
            <button class="change-row" type="button" data-change-cid="${row.id}" title="${escHtml(title)}">
                <img class="change-icon" src="${escHtml(row.image || '')}" alt="">
                <span>
                    <span class="change-name">${escHtml(name)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </button>
        `;
    }

    function changeItemRow(row) {
        const labels = changeLabels();
        const name = localizedEntityName(row);
        const title = `${name} ${signed(row.delta || 0)}`;
        const meta = `${pct(row.baseline_wr || 0)} -> ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.uses}`;
        return `
            <div class="change-row" title="${escHtml(title)}">
                <img class="change-icon" src="${escHtml(row.icon || '')}" alt="">
                <span>
                    <span class="change-name">${escHtml(name)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </div>
        `;
    }

    function changeChampItemRow(row) {
        const labels = changeLabels();
        const champ = row.champ || {};
        const item = row.item || {};
        const champName = localizedEntityName(champ);
        const itemName = localizedEntityName(item);
        const title = `${champName} + ${itemName} ${signed(row.delta || 0)}`;
        const meta = `${labels.lift} ${signed(row.baseline_lift || 0)} -> ${signed(row.current_lift || 0)} · WR ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.uses}`;
        return `
            <button class="change-row" type="button" data-change-cid="${champ.id}" title="${escHtml(title)}">
                <span class="change-duo">
                    <img src="${escHtml(champ.image || '')}" alt="">
                    <img src="${escHtml(item.icon || '')}" alt="">
                </span>
                <span>
                    <span class="change-name">${escHtml(champName)} + ${escHtml(itemName)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </button>
        `;
    }

    function changeColumn(title, rows, renderer) {
        const labels = changeLabels();
        const body = rows && rows.length
            ? rows.map(renderer).join('')
            : `<div class="change-empty">${escHtml(labels.noData)}</div>`;
        return `
            <div class="change-column">
                <h3 class="change-column-title">${escHtml(title)}</h3>
                <div class="change-list">${body}</div>
            </div>
        `;
    }

    function renderChangeTabContent(changes, labels) {
        if (!changes || !changes.currentPatch) {
            return `<div class="change-empty">${escHtml(labels.noData)}</div>`;
        }
        if (activeUpdateTab === 'items') {
            return `
                <div class="change-grid">
                    ${changeColumn(labels.itemUp, changes.itemRisers || [], changeItemRow)}
                    ${changeColumn(labels.itemDown, changes.itemFallers || [], changeItemRow)}
                </div>
                <div class="change-meta" style="margin-top:10px">${escHtml(labels.itemNote)}</div>
            `;
        }
        if (activeUpdateTab === 'champItems') {
            return `
                <div class="change-grid">
                    ${changeColumn(labels.champItemUp, changes.champItemRisers || [], changeChampItemRow)}
                    ${changeColumn(labels.champItemDown, changes.champItemFallers || [], changeChampItemRow)}
                </div>
                <div class="change-meta" style="margin-top:10px">${escHtml(labels.itemNote)}</div>
            `;
        }
        return `
            <div class="change-grid">
                ${changeColumn(labels.heroUp, changes.heroRisers || [], changeHeroRow)}
                ${changeColumn(labels.heroDown, changes.heroFallers || [], changeHeroRow)}
            </div>
        `;
    }

    function renderUpdatesPanel() {
        const copy = tr();
        const labels = changeLabels();
        const changes = DATA.patchChanges || {};
        if (!['heroes', 'items', 'champItems'].includes(activeUpdateTab)) {
            activeUpdateTab = 'heroes';
        }
        const button = document.getElementById('updates-toggle');
        const panel = document.getElementById('updates-panel');
        const kicker = document.getElementById('updates-kicker');
        const title = document.getElementById('updates-title');
        const close = document.getElementById('updates-close');
        const list = document.getElementById('updates-list');
        if (button) {
            button.textContent = labels.button || copy.updatesButton;
            button.setAttribute('aria-expanded', updatesOpen ? 'true' : 'false');
        }
        if (panel) panel.classList.toggle('is-hidden', !updatesOpen);
        if (kicker) kicker.textContent = labels.kicker || copy.updatesKicker;
        if (title) title.textContent = labels.title || copy.updatesTitle;
        if (close) close.setAttribute('aria-label', labels.close || copy.updatesClose);
        if (list) {
            const tabHtml = Object.entries(labels.tabs).map(([key, label]) => `
                <button class="change-tab${activeUpdateTab === key ? ' active' : ''}" type="button"
                        data-change-tab="${key}" aria-pressed="${activeUpdateTab === key ? 'true' : 'false'}">
                    ${escHtml(label)}
                </button>
            `).join('');
            const summary = changes.currentPatch ? `
                <div class="change-summary">
                    <span class="change-chip">${escHtml(labels.summaryBase)} ${escHtml(changes.baselinePatch || '')}</span>
                    <span class="change-chip">${escHtml(labels.summarySample)} ${fmtInt(changes.currentGames)} / ${fmtInt(changes.baselineGames)}</span>
                    <span class="change-chip">${escHtml(labels.summaryRule)} ${escHtml(labels.summaryRuleText)}</span>
                </div>
            ` : '';
            list.innerHTML = `
                ${summary}
                <div class="change-tabs" role="tablist">${tabHtml}</div>
                ${renderChangeTabContent(changes, labels)}
            `;
        }
    }

    function applyLanguage(nextLang) {
        currentLang = nextLang === 'en' ? 'en' : 'zh';
        const copy = tr();
        document.documentElement.lang = copy.htmlLang;
        try { localStorage.setItem(LANG_KEY, currentLang); } catch {}

        const titleEl = document.getElementById('site-title');
        if (titleEl) titleEl.textContent = currentLang === 'en' ? HEADER_TITLE_EN : HEADER_TITLE_ZH;
        const subtitleEl = document.getElementById('site-subtitle');
        if (subtitleEl) subtitleEl.innerHTML = copy.subtitle();
        updateSearchPlaceholder();
        const shownUnit = document.getElementById('shown-unit');
        if (shownUnit) shownUnit.textContent = copy.shownUnit;
        document.querySelectorAll('.tier-count-unit').forEach(el => {
            el.textContent = copy.tierUnit;
        });
        document.querySelectorAll('.chip').forEach(chip => {
            chip.textContent = currentLang === 'en'
                ? (chip.getAttribute('data-label-en') || chip.textContent || '')
                : (chip.getAttribute('data-label-zh') || chip.textContent || '');
        });
        const emptyTitle = document.getElementById('empty-title');
        if (emptyTitle) emptyTitle.textContent = copy.emptyTitle;
        const emptyCopy = document.getElementById('empty-copy');
        if (emptyCopy) emptyCopy.textContent = copy.emptyCopy;
        const freshness = document.getElementById('freshness-copy');
        if (freshness) freshness.textContent = copy.freshness();
        const sideTitle = document.getElementById('side-title');
        if (sideTitle) sideTitle.textContent = copy.sideTitle;
        const sideSub = document.getElementById('side-sub');
        if (sideSub) sideSub.innerHTML = copy.sideSub;
        const sideClose = document.getElementById('side-close');
        if (sideClose) sideClose.setAttribute('aria-label', copy.closeRecs);
        const toggle = document.getElementById('lang-toggle');
        const toggleLabel = document.getElementById('lang-toggle-label');
        if (toggle) {
            toggle.title = copy.langToggleTitle;
            toggle.setAttribute('aria-label', copy.langToggleAria);
        }
        if (toggleLabel) toggleLabel.textContent = copy.langToggleLabel;

        updateChampCardCopy();
        refreshSecondaryRoleBadges();
        renderUpdatesPanel();
        setRecommendMode(recommendMode);
        renderSidePanel();
        if (detailSelected) {
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (champ) openDetailForChamp(champ, true);
        }
    }

    function setRecommendMode(next) {
        recommendMode = Boolean(next);
        if (!recommendMode) recModalOpen = false;
        const btn = document.getElementById('recommend-mode');
        if (!btn) return;
        btn.classList.toggle('active', recommendMode);
        btn.setAttribute('aria-pressed', recommendMode ? 'true' : 'false');
        btn.textContent = recommendMode ? tr().recModeOn : tr().recModeOff;
    }

    function syncDetailModalState() {
        document.body.classList.toggle('detail-modal-open', Boolean(detailSelected) && isMobileViewport());
    }

    function closeDetail() {
        document.querySelectorAll('.detail-host').forEach(h => h.innerHTML = '');
        document.querySelectorAll('.champ.detail-selected').forEach(el => el.classList.remove('detail-selected'));
        detailSelected = null;
        syncDetailModalState();
    }

    function openDetailForChamp(champ, force = false) {
        const cid = champ.getAttribute('data-cid');
        const block = champ.closest('.tier-block');
        const host  = block.querySelector('.detail-host');

        // Clear any previously selected highlight + detail elsewhere.
        document.querySelectorAll('.champ.detail-selected').forEach(el => {
            if (el !== champ) el.classList.remove('detail-selected');
        });
        document.querySelectorAll('.detail-host').forEach(el => {
            if (el !== host) el.innerHTML = '';
        });

        if (!force && detailSelected === cid && host.firstChild) {
            closeDetail();
            return;
        }

        // Position the detail host right after the last champ in the clicked
        // row, so the panel always pops up directly under the champion you
        // tapped — never hidden far below by other champs.
        const anchor = lastChampInRow(champ);
        if (anchor.nextSibling !== host) {
            anchor.after(host);
        }

        const dialogAttrs = isMobileViewport()
            ? ` role="dialog" aria-modal="true" aria-labelledby="detail-title-${cid}"`
            : '';
        host.innerHTML = `<div class="detail"${dialogAttrs}>${renderDetail(cid)}</div>`;
        applySearchHighlights(host);
        champ.classList.add('detail-selected');
        detailSelected = cid;
        syncDetailModalState();
        if (isMobileViewport()) {
            host.querySelector('.detail-close')?.focus({ preventScroll: true });
        }
        if (!force) {
            trackEvent('champion_detail_open', {
                champion_id: cid,
                champion_name: champ.getAttribute('data-name-en') || '',
                tier: champ.getAttribute('data-tier') || '',
            });
        }
    }

    function openDetailByCid(cid) {
        const champ = document.querySelector(`.champ[data-cid="${cid}"]:not(.hidden)`);
        if (!champ) return;
        openDetailForChamp(champ);
        champ.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    function toggleTeamPick(cid) {
        pickNotice = '';
        const idx = teamPicks.indexOf(cid);
        if (idx !== -1) {
            teamPicks.splice(idx, 1);
        } else if (teamPicks.length >= MAX_TEAM_PICKS) {
            pickNotice = tr().maxOnly(MAX_TEAM_PICKS);
        } else {
            teamPicks.push(cid);
        }
        syncPickDecorations();
        renderSidePanel();
    }

    document.addEventListener('click', (ev) => {
        const ghStar = ev.target.closest('.gh-star');
        if (ghStar) {
            trackEvent('github_star_click', { location: 'header' });
            return;
        }
        const langBtn = ev.target.closest('#lang-toggle');
        if (langBtn) {
            const nextLang = currentLang === 'en' ? 'zh' : 'en';
            applyLanguage(nextLang);
            trackEvent('language_toggle', { language: nextLang });
            return;
        }
        const fabBtn = ev.target.closest('#rec-fab');
        if (fabBtn) {
            recModalOpen = true;
            renderSidePanel();
            trackEvent('recommendations_open', { source: 'fab', picks: teamPicks.length });
            return;
        }
        const sideClose = ev.target.closest('#side-close');
        if (sideClose) {
            recModalOpen = false;
            renderSidePanel();
            trackEvent('recommendations_close', { source: 'panel', picks: teamPicks.length });
            return;
        }
        const detailClose = ev.target.closest('.detail-close');
        if (detailClose) {
            closeDetail();
            return;
        }
        if (isMobileViewport() && ev.target.classList && ev.target.classList.contains('detail-host')) {
            closeDetail();
            return;
        }
        const changeTab = ev.target.closest('[data-change-tab]');
        if (changeTab) {
            activeUpdateTab = changeTab.getAttribute('data-change-tab') || 'heroes';
            renderUpdatesPanel();
            trackEvent('patch_change_tab', { tab: activeUpdateTab });
            return;
        }
        const changeCid = ev.target.closest('[data-change-cid]');
        if (changeCid) {
            openDetailByCid(changeCid.getAttribute('data-change-cid'));
            trackEvent('patch_change_detail_open', { champion_id: changeCid.getAttribute('data-change-cid') });
            return;
        }
        const updatesBtn = ev.target.closest('#updates-toggle');
        if (updatesBtn) {
            updatesOpen = !updatesOpen;
            renderUpdatesPanel();
            trackEvent('updates_toggle', { open: updatesOpen });
            return;
        }
        const updatesClose = ev.target.closest('#updates-close');
        if (updatesClose) {
            updatesOpen = false;
            renderUpdatesPanel();
            trackEvent('updates_close', {});
            return;
        }
        const modeBtn = ev.target.closest('#recommend-mode');
        if (modeBtn) {
            const nextMode = !recommendMode;
            setRecommendMode(nextMode);
            pickNotice = '';
            renderSidePanel();
            trackEvent('recommend_mode_toggle', { enabled: nextMode });
            return;
        }
        const removeBtn = ev.target.closest('[data-remove-cid]');
        if (removeBtn) {
            const removedCid = removeBtn.getAttribute('data-remove-cid');
            teamPicks = teamPicks.filter(cid => cid !== removeBtn.getAttribute('data-remove-cid'));
            pickNotice = '';
            syncPickDecorations();
            renderSidePanel();
            trackEvent('team_pick_remove', { champion_id: removedCid, picks: teamPicks.length });
            return;
        }
        const recRow = ev.target.closest('.rec-row');
        if (recRow) {
            recModalOpen = false;
            renderSidePanel();
            const recCid = recRow.getAttribute('data-cid');
            trackEvent('recommendation_click', { champion_id: recCid, picks: teamPicks.length });
            openDetailByCid(recCid);
            return;
        }
        const champ = ev.target.closest('.champ');
        if (!champ) return;
        const cid = champ.getAttribute('data-cid');
        if (recommendMode) {
            toggleTeamPick(cid);
            trackEvent('team_pick_toggle', { champion_id: cid, picks: teamPicks.length });
            return;
        }
        openDetailForChamp(champ);
    });

    // When viewport width changes, the row containing the selected champ
    // shifts — re-anchor the detail host so it stays directly under that
    // champ on the new layout.
    let resizeT = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
            updateSearchPlaceholder();
            renderSidePanel();
            if (!detailSelected) return;
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!champ) return;
            const host = champ.closest('.tier-block').querySelector('.detail-host');
            const anchor = lastChampInRow(champ);
            if (anchor.nextSibling !== host) anchor.after(host);
            syncDetailModalState();
        }, 120);
    });

    function addSearchTerm(terms, value) {
        if (value === null || value === undefined) return;
        if (Array.isArray(value)) {
            value.forEach(item => addSearchTerm(terms, item));
            return;
        }
        const text = String(value).trim();
        if (text) terms.push(text);
    }

    function addNamedSearchRow(terms, row) {
        if (!row) return;
        addSearchTerm(terms, [
            row.name, row.name_zh, row.name_en,
            row.set, row.set_zh, row.set_en, row.slug,
        ]);
        (row.items || []).forEach(item => {
            addSearchTerm(terms, [item.name, item.name_zh, item.name_en, item.id]);
        });
    }

    function addAugmentSearchRow(terms, row) {
        if (!row) return;
        const aug = (DATA.augs || {})[String(row.id || row.augment_id || '')];
        if (!aug) return;
        addSearchTerm(terms, [
            aug.name, aug.name_zh, aug.name_en,
            aug.set, aug.set_zh, aug.set_en, aug.setSlug,
        ]);
        (aug.sets || []).forEach(setInfo => {
            addSearchTerm(terms, [
                setInfo.name, setInfo.name_zh, setInfo.name_en, setInfo.slug,
            ]);
        });
    }

    function enrichSearchIndexes() {
        document.querySelectorAll('.champ[data-cid]').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const info = (DATA.champs || {})[String(cid)];
            if (!info) return;
            const terms = [champ.getAttribute('data-search') || ''];
            addSearchTerm(terms, [info.name, info.name_zh, info.name_en, info.alias, info.tags || []]);
            ['top', 'bot'].forEach(side => {
                Object.values(info[side] || {}).forEach(rows => (rows || []).forEach(row => addAugmentSearchRow(terms, row)));
                ['sets', 'items', 'singleItems', 'boots', 'itemClusters', 'augTypes'].forEach(key => {
                    ((info[key] || {})[side] || []).forEach(row => addNamedSearchRow(terms, row));
                });
            });
            const seen = new Set();
            const blob = terms
                .flatMap(term => String(term).toLowerCase().split(/\\s+/))
                .filter(term => {
                    if (!term || seen.has(term)) return false;
                    seen.add(term);
                    return true;
                })
                .join(' ');
            champ.setAttribute('data-search', blob);
        });
    }

    try {
        const savedLang = localStorage.getItem(LANG_KEY);
        if (savedLang === 'en' || savedLang === 'zh') currentLang = savedLang;
    } catch {}

    enrichSearchIndexes();
    setRecommendMode(false);
    syncPickDecorations();
    renderSidePanel();
    applyLanguage(currentLang);

    /* -----  Filter / search  --------------------------------------- */

    function applyFilters() {
        const role = filterState.role;
        const q = filterState.q.trim();
        let shown = 0;
        document.querySelectorAll('.tier-block').forEach(block => {
            let tierShown = 0;
            const champs = block.querySelectorAll(':scope > .tier-grid > .champ');
            champs.forEach(c => {
                const tags = (c.getAttribute('data-tags') || '').split(' ');
                const blob = c.getAttribute('data-search') || '';
                const matchRole = !role || tags.includes(role);
                const matchQ = !q || searchMatchesText(blob, q);
                const hide = !(matchRole && matchQ);
                c.classList.toggle('hidden', hide);
                if (!hide) tierShown++;
            });
            // Update tier count number
            const tier = block.getAttribute('data-tier');
            const numEl = block.querySelector(`.tier-count-num[data-tier="${tier}"]`);
            if (numEl) numEl.textContent = tierShown;
            // Hide whole tier-block when empty
            block.classList.toggle('hidden', tierShown === 0);
            shown += tierShown;
        });
        const shownN = document.getElementById('shown-n');
        if (shownN) shownN.textContent = shown;
        const empty = document.getElementById('empty-state');
        if (empty) empty.classList.toggle('visible', shown === 0);

        // If the currently-selected champ got hidden, close its detail panel.
        if (detailSelected) {
            const sel = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!sel || sel.classList.contains('hidden')) {
                closeDetail();
            }
        }
        refreshSecondaryRoleBadges();
        applySearchHighlights();
    }

    function setActiveChip(role) {
        document.querySelectorAll('.chip').forEach(chip => {
            chip.classList.toggle('active', chip.getAttribute('data-role') === role);
        });
    }

    // Role chip clicks (event delegation).  "All" chip (data-role="") already
    // unsets role filter — no dedicated reset button needed.
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.chip');
        if (!chip) return;
        filterState.role = chip.getAttribute('data-role') || '';
        setActiveChip(filterState.role);
        applyFilters();
        trackEvent('role_filter_click', { role: filterState.role || 'all' });
    });

    // Keyboard activation for cards.  Enter / Space on a `.champ` or `.aug`
    // triggers the same path a click would (they're role="button" /
    // tabindex="0").  Preventing default on Space stops the page from
    // scrolling.
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
            if (detailSelected && isMobileViewport()) {
                closeDetail();
                return;
            }
            if (recModalOpen) {
                recModalOpen = false;
                renderSidePanel();
                return;
            }
            if (updatesOpen) {
                updatesOpen = false;
                renderUpdatesPanel();
                return;
            }
        }
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        const t = ev.target;
        if (!t || !t.classList) return;
        if (t.classList.contains('champ') || t.classList.contains('aug')) {
            ev.preventDefault();
            t.click();
        }
    });

    // Augment tooltip viewport-clip protection: tooltips default to "above"
    // the card.  When the card sits near the top of the viewport, the
    // tooltip would clip — flip it below instead by toggling a class
    // computed from `getBoundingClientRect`.
    document.addEventListener('mouseover', (ev) => {
        const aug = ev.target.closest && ev.target.closest('.aug');
        if (!aug) return;
        const rect = aug.getBoundingClientRect();
        // Tooltip is ~ 110-140 px tall; flip when there's less than 160 px
        // of headroom above the card.
        aug.classList.toggle('flip-tip', rect.top < 160);
    }, { passive: true });

    document.addEventListener('mouseover', (ev) => {
        const fitChip = ev.target.closest && ev.target.closest('.fit-chip-wrap');
        if (fitChip) {
            positionFitChipTooltip(fitChip);
            return;
        }
        const badge = ev.target.closest && ev.target.closest('.alt-role-badge');
        if (badge) {
            positionSecondaryRoleTooltip(badge);
            return;
        }
        const champ = ev.target.closest && ev.target.closest('.champ.secondary-role-match');
        if (!champ) return;
        positionSecondaryRoleTooltip(champ.querySelector('.alt-role-badge'));
    }, { passive: true });

    document.addEventListener('focusin', (ev) => {
        const fitChip = ev.target.closest && ev.target.closest('.fit-chip-wrap');
        if (fitChip) {
            positionFitChipTooltip(fitChip);
            return;
        }
        const badge = ev.target.closest && ev.target.closest('.alt-role-badge');
        if (badge) {
            positionSecondaryRoleTooltip(badge);
            return;
        }
        const champ = ev.target.closest && ev.target.closest('.champ.secondary-role-match');
        if (!champ) return;
        positionSecondaryRoleTooltip(champ.querySelector('.alt-role-badge'));
    });

    // Live search.
    const searchEl = document.getElementById('champ-search');
    if (searchEl) {
        searchEl.addEventListener('input', () => {
            filterState.q = searchEl.value || '';
            applyFilters();
        });
        // Esc inside the search clears the filter and unfocuses, so the
        // typical "open, search, escape back to grid" flow works.
        searchEl.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                searchEl.value = '';
                filterState.q = '';
                applyFilters();
                searchEl.blur();
            }
        });
    }

    // Ctrl+F / Cmd+F shortcut → focus our search input.
    //
    // Rationale: our search already understands zh-TW name + English alias +
    // role keywords (gua-Liang in one go).  Native browser find can also
    // discover champions thanks to the .sr-only English alias spans, but
    // the in-page search additionally filters out non-matches — usually
    // what the user wants.
    //
    // If the user is already inside the search box, fall through to the
    // browser's native find dialog (no preventDefault) so they retain that
    // escape hatch.
    document.addEventListener('keydown', (ev) => {
        const isFind = (ev.ctrlKey || ev.metaKey) && ev.key && ev.key.toLowerCase() === 'f';
        if (!isFind) return;
        const sEl = document.getElementById('champ-search');
        if (!sEl) return;
        if (document.activeElement === sEl) return;  // let browser take over on 2nd press
        ev.preventDefault();
        sEl.focus();
        sEl.select();
    });
    """
    payload_expr = (
        f"await loadSitePayload({json.dumps(payload_url, ensure_ascii=False)})"
        if payload_url
        else payload_json
    )
    js = "(async () => {\n" + js.strip() + "\n})().catch(err => {\n" \
        "    console.error(err);\n" \
        "    document.body.insertAdjacentHTML('afterbegin', " \
        "`<div style=\"margin:16px;padding:12px 14px;border:1px solid #7f1d1d;" \
        "background:#2a1216;color:#ffd7dc;border-radius:8px\">" \
        "資料載入失敗，請稍後再試。</div>`);\n" \
        "});"
    js = js.replace("__PAYLOAD__", payload_expr)
    js = js.replace("__HEADER_TITLE_ZH__", json.dumps(header_title, ensure_ascii=False))
    js = js.replace("__HEADER_TITLE_EN__", json.dumps(header_title_en, ensure_ascii=False))
    js = js.replace("__SHORT_PATCH_ZH__", json.dumps(short_patch, ensure_ascii=False))
    js = js.replace("__DATE_STR_ZH__", json.dumps(date_str, ensure_ascii=False))
    js = js.replace("__BUILD_DATE__", json.dumps(build_date, ensure_ascii=False))
    js = js.replace("__PATCH_LABEL__", json.dumps(patch_label, ensure_ascii=False))
    js = js.replace("__TOTAL_GAMES__", json.dumps(f"{total_games:,}", ensure_ascii=False))
    js = js.replace(
        "__ROLE_LABELS__",
        json.dumps(
            {
                "zh": {role: (ROLE_LABELS.get(role, {}).get("zh", role)) for role in ROLE_ORDER},
                "en": {role: (ROLE_LABELS.get(role, {}).get("en", role)) for role in ROLE_ORDER},
            },
            ensure_ascii=False,
        ),
    )
    parts.append(f"<script>{js}</script>")
    parts.append("</body></html>")
    return "".join(parts)

@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"))
@click.option("--queue", "queue_id", type=int, default=2400, help="450=ARAM, 2400=Mayhem")
@click.option("--patch-prefix", default="16.10", help='e.g. "16.10" or "" for all patches')
@click.option("--ddragon-version", default=None, help="Override Data Dragon version (default: latest)")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=Path("docs/index.html"),
              help="Output HTML path (default: docs/index.html — the only non-root folder GitHub Pages serves from)")
@click.option("--min-games", type=int, default=50, help="Drop champions below this game count")
@click.option("--min-pair-games", type=int, default=15, help="Min games per (champ, augment) pair")
@click.option("--min-synergy-games", type=int, default=40,
              help="Min games per same-team champion pair for synergy / recommendation ranking")
@click.option("--top-n", type=int, default=0,
              help="Max best augments per rarity; 0 keeps all qualifying augments")
@click.option("--bot-n", type=int, default=0,
              help="Max worst augments per rarity; 0 keeps all qualifying augments")
@click.option("--site-url", default="",
              help="Canonical URL (used for OG og:url + <link rel=canonical>), e.g. https://user.github.io/repo/")
@click.option("--og-image", default="",
              help="Override the og:image URL (default: generated og-image.png under --site-url)")
@click.option("--build-date", default="",
              help="Date stamp shown in footer (default: today, YYYY-MM-DD)")
@click.option("--cloudflare-analytics-token", envvar="CLOUDFLARE_ANALYTICS_TOKEN", default="",
              help="Cloudflare Web Analytics token; can also be set via CLOUDFLARE_ANALYTICS_TOKEN")
@click.option("--ga-measurement-id", envvar="GA_MEASUREMENT_ID", default="",
              help="GA4 measurement id, e.g. G-XXXXXXXXXX; can also be set via GA_MEASUREMENT_ID")
@click.option("--payload-out", type=click.Path(path_type=Path), default=None,
              help="Write the frontend DATA payload as JSON for split frontend/backend deployment.")
@click.option("--payload-url", default="",
              help="Have the generated HTML fetch DATA from this URL instead of embedding it inline.")
def main(
    db: Path,
    queue_id: int,
    patch_prefix: str,
    ddragon_version: str | None,
    out_path: Path,
    min_games: int,
    min_pair_games: int,
    min_synergy_games: int,
    top_n: int,
    bot_n: int,
    site_url: str,
    og_image: str,
    build_date: str,
    cloudflare_analytics_token: str,
    ga_measurement_id: str,
    payload_out: Path | None,
    payload_url: str,
) -> None:
    patch_prefix = patch_prefix or None
    click.echo(f"[tierlist] db={db}  queue={queue_id}  patch_prefix={patch_prefix}")

    version, champ_meta = load_champion_metadata(ddragon_version)
    click.echo(f"[tierlist] data dragon version: {version}")

    aug_meta = load_augment_metadata(cache_dir=Path("data/cache"))
    desc_n = sum(1 for v in aug_meta.values() if v.get("desc"))
    click.echo(
        f"[tierlist] augment catalogue: {len(aug_meta)} entries "
        f"({desc_n} with zh-TW description)"
    )
    item_meta = load_item_metadata(cache_dir=Path("data/cache"))
    click.echo(f"[tierlist] item catalogue: {len(item_meta)} entries")

    all_champ_records, champ_aug, champ_pairs = compute_winrates(db, queue_id, patch_prefix)
    total_games = sum(r["games"] for r in all_champ_records) // 10
    champ_records = [r for r in all_champ_records if r["games"] >= min_games]
    click.echo(f"[tierlist] {len(champ_records)} champions after min_games={min_games}")
    click.echo(f"[tierlist] {len(champ_aug):,} (champ, augment) pairs total")
    click.echo(f"[tierlist] {len(champ_pairs):,} ordered same-team champion pairs total")

    patch_changes = None
    baseline_patch_prefix = previous_patch_prefix(patch_prefix)
    if baseline_patch_prefix:
        baseline_champ_records, _, _ = compute_winrates(db, queue_id, baseline_patch_prefix)
        baseline_total_games = sum(r["games"] for r in baseline_champ_records) // 10
        if baseline_total_games:
            patch_changes = compute_patch_changes(
                db,
                queue_id,
                patch_prefix,
                baseline_patch_prefix,
                item_meta,
                champ_meta,
                all_champ_records,
                baseline_champ_records,
            )
            if patch_changes:
                click.echo(
                    f"[tierlist] patch changes: {baseline_patch_prefix} -> {patch_prefix} "
                    f"({baseline_total_games:,} vs {total_games:,} games)"
                )

    aug_prior_strength = estimate_augment_prior_strength(champ_aug)
    click.echo(
        f"[tierlist] augment EB prior strength k={aug_prior_strength:.1f} "
        f"(posterior q={AUGMENT_POSTERIOR_Q:.2f}, pick_weight={AUGMENT_PICK_LIFT_WEIGHT:g})"
    )
    affinity_min_games = max(min_pair_games * 3, 45)
    item_style_min_games = max(affinity_min_games, ITEM_STYLE_MIN_GAMES)
    augment_type_min_games = max(affinity_min_games, AUGMENT_TYPE_MIN_GAMES)
    champ_profiles = load_champion_pick_profiles(champ_meta)
    set_affinity, item_style_affinity, augment_type_affinity = compute_champ_category_affinities(
        db,
        queue_id,
        patch_prefix,
        aug_meta,
        item_meta,
        champ_records,
        champ_profiles,
        min_set_games=affinity_min_games,
        min_item_games=item_style_min_games,
        min_augtype_games=augment_type_min_games,
    )
    click.echo(
        f"[tierlist] {len(set_affinity)} champions have >= 1 augment-set affinity row "
        f"(games >= {affinity_min_games})"
    )
    click.echo(
        f"[tierlist] {len(item_style_affinity)} champions have >= 1 item-style affinity row "
        f"(games >= {item_style_min_games})"
    )
    item_pair_affinity = compute_champ_item_pair_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=ITEM_PAIR_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(item_pair_affinity)} champions have >= 1 core item-pair row "
        f"(games >= {ITEM_PAIR_MIN_GAMES}, no fixed pick floor, "
        f"top_lift >= {ITEM_PAIR_TOP_MIN_LIFT:.1%})"
    )
    single_item_affinity = compute_champ_single_item_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=SINGLE_ITEM_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(single_item_affinity)} champions have >= 1 single-item row "
        f"(games >= {SINGLE_ITEM_MIN_GAMES}, no fixed pick floor, "
        f"top_lift >= {SINGLE_ITEM_TOP_MIN_LIFT:.1%})"
    )
    boot_item_affinity = compute_champ_boot_item_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=BOOT_ITEM_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(boot_item_affinity)} champions have >= 1 boot row "
        f"(games >= {BOOT_ITEM_MIN_GAMES}, top_lift >= {BOOT_ITEM_TOP_MIN_LIFT:.1%})"
    )
    item_build_clusters = compute_champ_item_build_clusters(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        single_item_affinity,
    )
    click.echo(
        f"[tierlist] {len(item_build_clusters)} champions have >= 1 clustered item route "
        f"(pair_games >= {ITEM_CLUSTER_MIN_PAIR_GAMES}, cluster_games >= {ITEM_CLUSTER_MIN_GAMES}, "
        f"exact_games >= {ITEM_CLUSTER_MIN_EXACT_GAMES}, item_evidence >= {ITEM_CLUSTER_ITEM_EVIDENCE_MIN_GAMES} "
        f"or lift >= {ITEM_CLUSTER_ITEM_FALLBACK_MIN_LIFT:.1%}, max_items={ITEM_CLUSTER_MAX_ITEMS})"
    )
    click.echo(
        f"[tierlist] {len(augment_type_affinity)} champions have >= 1 augment-type affinity row "
        f"(games >= {augment_type_min_games})"
    )
    dual_role_count = sum(1 for meta in champ_meta.values() if len(meta.get("tags") or []) > 1)
    click.echo(
        f"[tierlist] using fixed site role tags from scripts/champion_roles.py "
        f"({dual_role_count} dual-role champions)"
    )
    role_spec_path = out_path.parent / "champion-roles.json"
    write_role_definitions_json(
        role_spec_path,
        champ_meta=champ_meta,
        data_dragon_version=version,
        patch_prefix=patch_prefix,
    )
    click.echo(f"[tierlist] wrote {role_spec_path}")

    picks = build_champ_augment_picks(
        champ_aug,
        aug_meta,
        champ_profiles,
        min_games_per_pair=min_pair_games,
        top_n=top_n,
        bot_n=bot_n,
        prior_strength=aug_prior_strength,
    )
    click.echo(
        f"[tierlist] {len(picks)} champions have >= 1 rarity-bucketed pair "
        f"(games >= {min_pair_games})"
    )
    synergy = build_champ_synergy_index(
        champ_pairs,
        min_games=min_synergy_games,
    )
    click.echo(
        f"[tierlist] {len(synergy)} champions have >= 1 teammate synergy row "
        f"(games >= {min_synergy_games})"
    )

    if not build_date:
        build_date = _dt.date.today().isoformat()

    if cloudflare_analytics_token:
        click.echo("[tierlist] Cloudflare Web Analytics enabled")
    if ga_measurement_id:
        click.echo(f"[tierlist] GA4 enabled: {ga_measurement_id}")

    if not og_image:
        og_asset_path = out_path.parent / "og-image.png"
        try:
            write_og_image(
                og_asset_path,
                champ_records,
                champ_meta,
                queue_id=queue_id,
                patch_prefix=patch_prefix,
                total_games=total_games,
            )
            click.echo(f"[tierlist] wrote {og_asset_path}  ({og_asset_path.stat().st_size:,} bytes)")
            if site_url:
                og_version = (build_date or _dt.date.today().isoformat()).replace("-", "")
                og_image = site_url.rstrip("/") + "/" + og_asset_path.name + f"?v={og_version}-thumb"
        except Exception as exc:
            click.echo(f"[tierlist] WARN: og image generation failed: {exc}")

    favicon_outputs = write_favicon_assets(out_path.parent)
    for asset_path in favicon_outputs:
        click.echo(f"[tierlist] wrote {asset_path}  ({asset_path.stat().st_size:,} bytes)")

    html = render_html(
        champ_records,
        champ_meta,
        champ_profiles,
        picks,
        set_affinity,
        item_pair_affinity,
        single_item_affinity,
        boot_item_affinity,
        item_build_clusters,
        augment_type_affinity,
        synergy,
        aug_meta,
        patch_changes,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        ddragon_version=version,
        total_games=total_games,
        min_games_per_pair=min_pair_games,
        min_synergy_games=min_synergy_games,
        site_url=site_url,
        og_image=og_image,
        build_date=build_date,
        cloudflare_analytics_token=cloudflare_analytics_token,
        ga_measurement_id=ga_measurement_id,
        payload_out_path=payload_out,
        payload_url=payload_url,
    )
    if payload_out is not None:
        click.echo(f"[tierlist] wrote {payload_out}  ({payload_out.stat().st_size:,} bytes)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    click.echo(f"[tierlist] wrote {out_path}  ({out_path.stat().st_size:,} bytes)")

    # GitHub Pages: prevent Jekyll preprocessing (we don't have any _-prefixed
    # files today, but adding the marker keeps it that way as we evolve).
    nojekyll = out_path.parent / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")
        click.echo(f"[tierlist] wrote {nojekyll}")

if __name__ == "__main__":
    main()
