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
import hashlib
import html
from io import BytesIO
import json
import math
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import click
import httpx

from aram_nn import patch_snapshot

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
# Augment tier board is scoped to the current patch.  Augments with fewer than this
# many current-patch games get their sample topped up with the previous patch (at the
# previous patch's win rate) so thin / newly-released augments still clear the frontend
# AUG_TIER_MIN_GAMES floor instead of vanishing.  Keep in sync with that floor (500).
AUGMENT_CURRENT_MIN_GAMES = 500
# An augment needs at least this many current-patch games to count as "in the current
# patch".  0 games across a mature patch means Riot removed/disabled it (often it still
# lingers in the CommunityDragon catalogue), so we drop it rather than resurrect it from
# the previous patch.  Raise it to also cut augments that are technically present but
# vanishingly rare this patch.
AUGMENT_PRESENT_MIN_GAMES = 1
# Champion headline win rate may use the PREVIOUS patch's win rate as an early-patch
# prior, but only while the current patch is still immature.  Once the current patch
# has 100,000 complete games, its own sample is the source of truth: do not let a
# fixed cross-patch pseudo-sample hide real balance changes.  The previous patch is
# still retained for the version-comparison view.
CURRENT_PATCH_MATURE_GAMES = 100_000
CHAMP_PREV_PATCH_PRIOR_GAMES = 2000.0
# A champion needs at least this many previous-patch games before its previous-patch
# rate is trusted as a prior.  Below that the "prior" is itself mostly noise and
# would inject error rather than remove it, so those champions keep the flat 0.50
# prior.  A mature patch gives even the rarest champion several thousand games, so
# this only bites on brand-new releases and on reruns over a truncated patch.
CHAMP_PREV_PATCH_MIN_GAMES = 500
# Same treatment for same-team pair synergy, which needs it far more than champions
# do.  Measured on 16.13/16.14: a pair's lift has a split-half reliability of only
# ~0.23 across a FULL mature patch -- i.e. the raw number the site used to show was
# roughly 80% noise even at 420k games -- while 88% of whatever reliable signal does
# exist survives the patch boundary.  True synergy is a small effect (SD ~1.3pp)
# hiding under a large sampling error, which is exactly the regime shrinkage fixes.
#
# lift = (g*raw_lift + K*prior) / (g + K), prior = previous patch's lift shrunk
# toward 0 by its own sample (g_prev / (g_prev + SHRINK)).  Feeding in the raw
# previous lift instead makes things WORSE at large N -- that estimate carries its
# own noise, and predicting 0 beats predicting noise when the true effect is 1.3pp.
#
# Replaying 16.14 (fit on first N games, scored on the rest), RMSE / correlation
# against out-of-sample truth:
#   N= 10,000   raw 7.19pp r=+0.16  ->  blended 1.52pp r=+0.57
#   N= 30,000   raw 6.29pp r=+0.12  ->  blended 1.94pp r=+0.39
#   N=120,000   raw 4.92pp r=+0.11  ->  blended 2.79pp r=+0.24
PAIR_PREV_PATCH_PRIOR_GAMES = 3000.0
PAIR_PREV_PATCH_SHRINK_GAMES = 1600.0
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
# Absolute sample floors + relative pick floors (share of that champion's games).
# Relative floors drop niche champ×item cells that pass raw min_games only because
# the champion has a huge sample (e.g. 30 / 8k games ≈ 0.4% pick).
ITEM_PAIR_MIN_GAMES = 60
ITEM_PAIR_FALLBACK_MIN_GAMES = 40
ITEM_PAIR_TOP_MIN_LIFT = -0.02
ITEM_PAIR_TOP_MIN_PICK_RATE = 0.02  # ≥2% of champ games to surface as a core pair
ITEM_PAIR_PICK_LIFT_WEIGHT = 0.0
ITEM_PAIR_PICK_LIFT_CAP = AUGMENT_PICK_LIFT_CAP
ITEM_PAIR_PICK_RATE_WEIGHT = 0.012
ITEM_PAIR_PICK_RATE_REF = 0.005
ITEM_PAIR_PICK_RATE_CAP = 0.045
ITEM_PAIR_ORDER_PRIOR_GAMES = 20
SINGLE_ITEM_MIN_GAMES = 60
SINGLE_ITEM_FALLBACK_MIN_GAMES = 40
SINGLE_ITEM_TOP_MIN_LIFT = -0.02
# 1% floor feeds the 出裝 filter bar's 全部 tier.  Below this the real binding
# constraint is SINGLE_ITEM_MIN_GAMES (60 games ≈ 1.2% for a 5k-game champion),
# so a lower floor buys almost nothing (0.5% adds ~2 items/champ).
SINGLE_ITEM_TOP_MIN_PICK_RATE = 0.01  # ≥1% of champ games to surface as a single item
SINGLE_ITEM_PICK_LIFT_WEIGHT = ITEM_PAIR_PICK_LIFT_WEIGHT
SINGLE_ITEM_PICK_LIFT_CAP = ITEM_PAIR_PICK_LIFT_CAP
SINGLE_ITEM_PICK_RATE_WEIGHT = ITEM_PAIR_PICK_RATE_WEIGHT
SINGLE_ITEM_PICK_RATE_REF = ITEM_PAIR_PICK_RATE_REF
SINGLE_ITEM_PICK_RATE_CAP = ITEM_PAIR_PICK_RATE_CAP
SINGLE_ITEM_COMMON_TRAP_N = 6
SINGLE_ITEM_COMMON_TRAP_MIN_LIFT = -0.01
BOOT_ITEM_MIN_GAMES = 60
BOOT_ITEM_FALLBACK_MIN_GAMES = 40
BOOT_ITEM_TOP_MIN_LIFT = -0.04
BOOT_ITEM_TOP_MIN_PICK_RATE = 0.04  # boots are concentrated; require ≥4%
SPELL_MIN_GAMES = 50
SPELL_FALLBACK_MIN_GAMES = 30
SPELL_TOP_MIN_LIFT = -0.04
SPELL_TOP_N = 5
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
# Core-3 build recommender: stats live on the 3 core items (stable sample); a
# real observed 6-item completion is attached only as confirmation.
ITEM_CORE_BUILD_PRIOR_GAMES = 20.0          # smoothing pseudo-games toward champ baseline
ITEM_CORE_BUILD_EARLY_PRIOR = 20.0          # shrinkage for the build-order (earliness) signal
ITEM_CORE_BUILD_MIN_GAMES = 80              # a core triple must be observed >= this often
ITEM_CORE_BUILD_MIN_CONFIRM = 3            # a real 6-item completion must exist >= this often
ITEM_CORE_BUILD_WINRATE_MIN_GAMES = 80      # stricter sample floor for the winrate lane
ITEM_CORE_BUILD_WINRATE_MIN_LCB = 0.0       # winrate lane must be confidently above baseline
ITEM_CORE_BUILD_LCB_Z = 1.65                # small-sample penalty (lower confidence bound)
ITEM_CORE_BUILD_PICK_RATE_REF = 0.01
ITEM_CORE_BUILD_PICK_CREDIT_WEIGHT = 0.012
ITEM_CORE_BUILD_PICK_CREDIT_CAP = 0.035
ITEM_CORE_BUILD_LIFT_WEIGHT = 0.55
ITEM_CORE_BUILD_GAMES_WEIGHT = 0.02
ITEM_CORE_BUILD_OPTION_MIN_GAMES = 30       # any pairing item needs this many games to show
ITEM_CORE_BUILD_OPTION_MIN_PICK = 0.03      # ...or this pick rate — the "popular" half of the OR gate
ITEM_CORE_BUILD_OPTION_TOP_N = 8            # max pairing items shown per core-2 group
ITEM_CORE_BUILD_TAIL_N = 3                  # extra "also common" items shown dim per group
ITEM_CORE_BUILD_GROUP_TOP_N = 3             # max core groups (distinct build paths) per champion
ITEM_CORE_BUILD_GROUP_MIN_PICK = 0.06       # drop tiny build-path groups (the top group is always kept)
PATCH_CHANGE_TOP_N = 10
PATCH_CHANGE_HERO_MIN_GAMES = 500
PATCH_CHANGE_ITEM_CURRENT_MIN_GAMES = 500
PATCH_CHANGE_ITEM_BASELINE_MIN_GAMES = 800
PATCH_CHANGE_CHAMP_ITEM_CURRENT_MIN_GAMES = 80
PATCH_CHANGE_CHAMP_ITEM_BASELINE_MIN_GAMES = 120
# Relative floor on top of the absolute ones: the pairing must be this share of
# the champion's own games in BOTH patches.  Without it a popular champion's
# fringe build (110 games out of 21k = 0.5%) outranks a niche champion's core
# build purely because the absolute count is easier to hit.
PATCH_CHANGE_CHAMP_ITEM_MIN_PICK = 0.015
PATCH_CHANGE_ITEM_PRIOR_GAMES = 200
PATCH_CHANGE_CHAMP_ITEM_PRIOR_GAMES = 30
PATCH_CHANGE_AUGMENT_CURRENT_MIN_GAMES = 500
PATCH_CHANGE_AUGMENT_BASELINE_MIN_GAMES = 800
PATCH_CHANGE_AUGMENT_PRIOR_GAMES = 200
# 英雄×增幅 needs a stricter relative floor than 英雄×裝備 (1.5%).  Augments are
# thinner per cell: 4 picks/player out of a 206-augment pool means the median
# champ×augment cell is only 72 games / 0.83% pick, so a 1.5% gate would fill
# the board with 60-game noise.  At 5% the surviving cells run 125-495 games and
# the extreme delta drops from +15.6% to +9.2% — in line with the item board.
PATCH_CHANGE_CHAMP_AUG_CURRENT_MIN_GAMES = 80
PATCH_CHANGE_CHAMP_AUG_BASELINE_MIN_GAMES = 120
PATCH_CHANGE_CHAMP_AUG_MIN_PICK = 0.05
PATCH_CHANGE_CHAMP_AUG_PRIOR_GAMES = 30
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
# Augment quest / anvil rewards — not shop-buildable core items. Exclude from
# build recs and 版本變動 item ladders so they don't crowd out real purchases.
# IDs cover base + mode-remapped catalogue variants (CDragon often keeps several).
AUGMENT_GATED_ITEM_IDS = frozenset({
    223069,  # Void Immolation (Icathia's Fall / Sunfire+Hollow fuse)
    228002,  # Wooglet's Witchcap
    1111,    # Jarvan I's
    4403,    # The Golden Spatula
    224403,  # The Golden Spatula (mode id)
    664403,  # The Golden Spatula (mode id)
    994403,  # Golden Spatula (LCU / live Mayhem id)
})
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


def load_summoner_spell_metadata(version: str) -> dict[int, dict]:
    """Fetch summoner-spell id -> name (zh/en) + icon from Data Dragon.

    Icons live on the same DDragon CDN as champions/items, so no self-hosting is
    needed.  Returns an empty dict on a network hiccup — the spell rail then just
    falls back to the numeric id and a blank icon rather than failing the build.
    """
    by_id: dict[int, dict] = {}
    url_zh = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_TW/summoner.json"
    url_en = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/summoner.json"
    try:
        r_zh = httpx.get(url_zh, timeout=30)
        r_en = httpx.get(url_en, timeout=30)
    except Exception as exc:  # pragma: no cover - network hiccup
        click.echo(f"[tierlist] WARN: summoner-spell metadata fetch failed ({exc})")
        return by_id
    raw_zh = r_zh.json()["data"] if r_zh.status_code == 200 else {}
    raw_en = r_en.json()["data"] if r_en.status_code == 200 else {}
    source = raw_en or raw_zh
    for alias, entry_base in source.items():
        try:
            spell_id = int(entry_base["key"])
        except (KeyError, TypeError, ValueError):
            continue
        entry_en = raw_en.get(alias, entry_base)
        entry_zh = raw_zh.get(alias, entry_base)
        name_en = entry_en.get("name") or alias
        name_zh = entry_zh.get("name") or name_en
        image = (entry_en.get("image") or entry_zh.get("image") or {}).get("full") or f"{alias}.png"
        by_id[spell_id] = {
            "name": name_zh,
            "name_zh": name_zh,
            "name_en": name_en,
            "alias": alias,
            "icon": f"https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{image}",
        }
    return by_id

def _icon_url(lcu_path: str) -> str:
    """Convert an LCU asset path to a CommunityDragon URL."""
    stripped = lcu_path.replace("/lol-game-data/assets/", "", 1).lower()
    return f"{CDRAGON_BASE}/{stripped}"

# The plugins tree (CDRAGON_BASE) only ships the gray `_small` line-art augment
# icons; the colored per-rarity art (silver / gold / prismatic, what the client
# and sites like Blitz show) exists only in the raw game-asset dump under the
# same relative path with a `_large` suffix.
_GAME_ASSET_BASE = "https://raw.communitydragon.org/latest/game"

def _augment_colored_icon_url(
    small_icon_path: str,
    listing_cache: dict[str, set[str]],
) -> str:
    """Colored variant of an augment's gray `_small` icon, or "".

    Prefers `{name}_large.png`, falling back to suffixless `{name}.png`
    (a few augments, e.g. drop_bear, ship their colored art that way).
    Availability is checked against the game-asset dump's directory listing
    (fetched once per directory and memoized in `listing_cache`); a listing
    fetch failure just means every augment in that directory keeps its gray
    icon.
    """
    rel = small_icon_path.replace("/lol-game-data/assets/", "", 1).lower()
    dir_path, _, fname = rel.rpartition("/")
    if not dir_path or not fname.endswith("_small.png"):
        return ""
    if dir_path not in listing_cache:
        try:
            resp = httpx.get(f"{_GAME_ASSET_BASE}/{dir_path}/", timeout=20)
            resp.raise_for_status()
            listing_cache[dir_path] = set(
                re.findall(r'href="([^"/]+\.png)"', resp.text)
            )
        except Exception:
            listing_cache[dir_path] = set()
    for cand in (
        fname.replace("_small.png", "_large.png"),
        fname.replace("_small.png", ".png"),
    ):
        if cand in listing_cache[dir_path]:
            return f"{_GAME_ASSET_BASE}/{dir_path}/{cand}"
    return ""

# New augments ship every patch and are described only in kiwi.bin.json +
# the stringtable.  These files used to be cached forever, so once the cache was
# written (patch 16.10) every later augment had a blank description — which broke
# both category auto-classification and the hand-correction editor.  Re-fetch the
# augment caches at most once a day; everything else keeps the never-expire cache.
AUGMENT_CACHE_MAX_AGE_HOURS = 24.0


def _cached_get_json(
    url: str,
    cache_path: Path,
    timeout: float = 60,
    max_age_hours: float | None = None,
) -> dict | list:
    """Fetch JSON with on-disk caching (the kiwi.bin.json + stringtable are large).

    With ``max_age_hours`` set, a cache file older than that is refreshed from the
    network; if the refresh fails the stale copy is reused so an automated build
    never hard-fails on a transient error.  Without it the cache never expires."""
    if cache_path.exists():
        stale = False
        if max_age_hours is not None:
            try:
                age_h = (_dt.datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600.0
                stale = age_h >= max_age_hours
            except OSError:
                stale = False
        if not stale:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        else:
            try:
                r = httpx.get(url, timeout=timeout)
                r.raise_for_status()
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(r.text, encoding="utf-8")
                return r.json()
            except Exception:
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
        max_age_hours=AUGMENT_CACHE_MAX_AGE_HOURS,
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
        max_age_hours=AUGMENT_CACHE_MAX_AGE_HOURS,
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
        max_age_hours=AUGMENT_CACHE_MAX_AGE_HOURS,
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
    colored_listing_cache: dict[str, set[str]] = {}
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
        icon_url = _icon_url(icon_path) if icon_path else ""
        if icon_path:
            colored = _augment_colored_icon_url(icon_path, colored_listing_cache)
            if colored:
                icon_url = colored
        en_lookup_name = entry.get("nameTRA") or entry.get("name") or entry.get("simpleNameTRA") or ""
        set_infos = set_by_augment.get(_normalize_augment_name(en_lookup_name), [])
        by_id[aug_id] = {
            "name": name or f"#{aug_id}",
            "name_zh": name_zh or name or f"#{aug_id}",
            "name_en": name_en or name or f"#{aug_id}",
            "icon": icon_url,
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

def _ddragon_item_ids(version: str, cache_dir: Path | None = None) -> set[int]:
    """Item IDs that Data Dragon serves an icon for.

    CommunityDragon (`raw.communitydragon.org`) is the only source for Mayhem-only
    items, but it is slow / unreachable on some networks, leaving every item icon
    stuck loading. Data Dragon (Riot's official, Cloudflare-backed CDN — already
    used for champion portraits) is reliable everywhere, so we prefer it for any
    item it actually has and only fall back to CommunityDragon for the handful of
    Mayhem-exclusive items it 403s on.
    """
    try:
        data = _cached_get_json(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json",
            (cache_dir or Path("data/cache")) / f"ddragon_items_{version}.json",
        )
    except Exception as exc:  # pragma: no cover - network/cache failure is non-fatal
        click.echo(f"[tierlist] WARN: ddragon item.json fetch failed ({exc}); items stay on CommunityDragon")
        return set()
    raw = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return set()
    ids: set[int] = set()
    for key in raw:
        try:
            ids.add(int(key))
        except (TypeError, ValueError):
            continue
    return ids

def clean_item_description(raw_html: str | None) -> str:
    """Strip Riot item HTML into compact plain text for site tooltips.

    Keeps line breaks between stats / passive blocks so the frontend can render
    them as separate rows without shipping raw <mainText> markup.
    """
    if not raw_html:
        return ""
    text = str(raw_html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # Drop known Riot wrapper / scaling tags but keep their text content.
    text = re.sub(
        r"</?(?:mainText|stats|passive|active|attention|rules|status|"
        r"scaleAP|scaleAD|scaleHealth|scaleMana|scaleArmor|scaleMR|"
        r"physicalDamage|magicDamage|trueDamage|OnHit|speed|shield|"
        r"healing|lifeSteal|keywordMajor|keyword|rarityMythic|rarityLegendary|"
        r"rarityGeneric|ornnBonus|flavorText)(?:\s[^>]*)?>",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_item_metadata(
    cache_dir: Path | None = None,
    ddragon_version: str | None = None,
) -> dict[int, dict]:
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
    ddragon_ids = (
        _ddragon_item_ids(ddragon_version, cache_dir) if ddragon_version else set()
    )
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
            "desc_zh": clean_item_description(zh_row.get("description") or row.get("description")),
            "desc_en": clean_item_description(row.get("description") or zh_row.get("description")),
            "icon": (
                f"https://ddragon.leagueoflegends.com/cdn/{ddragon_version}/img/item/{item_id}.png"
                if item_id in ddragon_ids
                else (_icon_url(icon_path) if icon_path else "")
            ),
        }
    return out

def _scan_patch_counters(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
) -> dict[str, Counter]:
    """One pass over a patch's games -> the raw tallies every stat is built from.

    Split out of ``compute_winrates`` so a settled (non-current) patch can be
    frozen as counters and re-derived without re-reading hundreds of thousands
    of rows; see ``aram_nn.patch_snapshot`` for the settle / re-settle rules.
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

    return {
        "games": games,
        "wins": wins,
        "ca_games": ca_games,
        "ca_wins": ca_wins,
        "cp_games": cp_games,
        "cp_wins": cp_wins,
    }


def compute_winrates(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    prior: float = 0.5,
    k: int = 200,
    prev_wr_by_champ: dict[int, float] | None = None,
    prev_k: float = CHAMP_PREV_PATCH_PRIOR_GAMES,
    prev_pair_lift: dict[tuple[int, int], tuple[float, int]] | None = None,
    prev_pair_k: float = PAIR_PREV_PATCH_PRIOR_GAMES,
):
    """Compute champion winrates + per-(champion, augment) winrates.

    ``prev_wr_by_champ`` maps champion_id -> that champion's PREVIOUS-patch win
    rate.  When supplied, a champion's ``bayes_wr`` is shrunk toward its own
    previous-patch rate with ``prev_k`` pseudo-games instead of toward the flat
    ``prior`` -- see CHAMP_PREV_PATCH_PRIOR_GAMES for why and for the measured
    error reduction.  Champions absent from the map (brand new, or the previous
    patch had no data) fall back to the flat ``prior`` / ``k`` path.  Pass nothing
    when computing the baseline patch itself, or the prior would chain backwards.

    ``prev_pair_lift`` maps (champion_id, teammate_id) -> (that pair's raw lift on
    the PREVIOUS patch, its game count there).  Supplying it switches ``lift`` from
    the raw residual to the shrunk estimate described at
    PAIR_PREV_PATCH_PRIOR_GAMES; pairs with no previous-patch entry still shrink,
    toward 0.  Pass nothing when computing the baseline patch itself -- the prior
    is built FROM those raw lifts, so blending them first would double-shrink.

    Returns: (champ_records, champ_aug_records, champ_pair_records)
      champ_records: list of dicts with champion_id, games, wins, raw_wr, bayes_wr,
                    prev_wr (prior used, None if flat) and prev_mix (0..1 share of
                    the headline number contributed by the previous patch)
      champ_pair_records: ``lift`` is the shrunk estimate when prev_pair_lift is
                    given, with ``raw_lift`` keeping the unshrunk residual and
                    ``lift_prev_mix`` the prior's share.  ``raw_wr`` stays the
                    observed pair rate and is deliberately NOT reconciled with the
                    shrunk lift -- one is an observation, the other an estimate.
      champ_aug_records: list of dicts with champion_id, augment_id, games, wins,
                        raw_wr, smoothed_wr, lift (smoothed_wr - champ_baseline_wr)
      champ_pair_records: list of dicts with champion_id, teammate_id, games,
                        wins, expected_wr, lift, delta_vs_rest, z_score
    """
    return _derive_winrate_records(
        _scan_patch_counters(db_path, queue_id, patch_prefix),
        prior=prior,
        k=k,
        prev_wr_by_champ=prev_wr_by_champ,
        prev_k=prev_k,
        prev_pair_lift=prev_pair_lift,
        prev_pair_k=prev_pair_k,
    )


def _derive_winrate_records(
    counters: dict[str, Counter],
    *,
    prior: float = 0.5,
    k: int = 200,
    prev_wr_by_champ: dict[int, float] | None = None,
    prev_k: float = CHAMP_PREV_PATCH_PRIOR_GAMES,
    prev_pair_lift: dict[tuple[int, int], tuple[float, int]] | None = None,
    prev_pair_k: float = PAIR_PREV_PATCH_PRIOR_GAMES,
):
    """Turn raw patch counters into the three record lists compute_winrates returns.

    Kept separate from the scan so the smoothing can keep changing without
    invalidating settled patch snapshots: those freeze counters, and the current
    formulas are re-applied to them on every build.
    """
    games = counters["games"]
    wins = counters["wins"]
    ca_games = counters["ca_games"]
    ca_wins = counters["ca_wins"]
    cp_games = counters["cp_games"]
    cp_wins = counters["cp_wins"]

    champ_records = []
    for cid, g in games.items():
        w = wins[cid]
        raw = w / g if g else 0.0
        prev_wr = (prev_wr_by_champ or {}).get(cid)
        if prev_wr is None:
            bayes = (w + prior * k) / (g + k)
            prev_mix = 0.0
        else:
            bayes = (w + prev_wr * prev_k) / (g + prev_k)
            prev_mix = prev_k / (g + prev_k)
        champ_records.append({
            "champion_id": cid,
            "games": g,
            "wins": w,
            "raw_wr": raw,
            "bayes_wr": bayes,
            "prev_wr": prev_wr,
            "prev_mix": prev_mix,
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

        # Shrink the residual toward the previous patch's (itself shrunk) lift.
        # Without a prior map this is a no-op, which is what the baseline pass wants.
        if prev_pair_lift is None:
            lift = delta_vs_expected
            lift_prev_mix = 0.0
        else:
            prev_entry = prev_pair_lift.get((cid, teammate_id))
            if prev_entry:
                prev_lift, prev_games = prev_entry
                prior = prev_lift * (prev_games / (prev_games + PAIR_PREV_PATCH_SHRINK_GAMES))
            else:
                prior = 0.0
            lift = (g * delta_vs_expected + prev_pair_k * prior) / (g + prev_pair_k)
            lift_prev_mix = prev_pair_k / (g + prev_pair_k)

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
            "lift": lift,
            "raw_lift": delta_vs_expected,
            "lift_prev_mix": lift_prev_mix,
            "delta_vs_rest": delta_vs_rest,
            "z_score": z_score,
        })

    return champ_records, champ_aug_records, champ_pair_records

SNAPSHOT_CHAMP_SECTION = "champ_counters"
SNAPSHOT_ITEM_SECTION = "core_item_counters"


def _encode_champ_counters(counters: dict[str, Counter]) -> dict:
    """Counters -> JSON.  Tuple keys become flat rows; ints stay ints."""
    return {
        "champ": [[int(cid), int(g), int(counters["wins"][cid])] for cid, g in counters["games"].items()],
        "champ_aug": [
            [int(cid), int(aid), int(g), int(counters["ca_wins"][(cid, aid)])]
            for (cid, aid), g in counters["ca_games"].items()
        ],
        "champ_pair": [
            [int(cid), int(tid), int(g), int(counters["cp_wins"][(cid, tid)])]
            for (cid, tid), g in counters["cp_games"].items()
        ],
    }


def _decode_champ_counters(payload: dict) -> dict[str, Counter]:
    games: Counter[int] = Counter()
    wins: Counter[int] = Counter()
    ca_games: Counter[tuple[int, int]] = Counter()
    ca_wins: Counter[tuple[int, int]] = Counter()
    cp_games: Counter[tuple[int, int]] = Counter()
    cp_wins: Counter[tuple[int, int]] = Counter()
    for cid, g, w in payload.get("champ") or []:
        games[int(cid)] = int(g)
        wins[int(cid)] = int(w)
    for cid, aid, g, w in payload.get("champ_aug") or []:
        ca_games[(int(cid), int(aid))] = int(g)
        ca_wins[(int(cid), int(aid))] = int(w)
    for cid, tid, g, w in payload.get("champ_pair") or []:
        cp_games[(int(cid), int(tid))] = int(g)
        cp_wins[(int(cid), int(tid))] = int(w)
    return {
        "games": games,
        "wins": wins,
        "ca_games": ca_games,
        "ca_wins": ca_wins,
        "cp_games": cp_games,
        "cp_wins": cp_wins,
    }


def settled_patch_counters(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    *,
    snapshot_dir: Path | None = None,
    live_games: int | None = None,
    log=None,
) -> dict[str, Counter]:
    """Patch counters for a CLOSED patch, reusing its frozen snapshot when valid.

    Only ever call this for a patch that is no longer the current one -- the
    current patch grows by the minute, so freezing it would publish stale
    headline numbers.  Everything else (the comparison baseline, the walk-back
    the 新-augment window does) is settled data and is read from
    data/patch_snapshots instead of rescanning the DB.
    """
    total = count_patch_games(db_path, queue_id, patch_prefix) if live_games is None else live_games
    payload, status = patch_snapshot.load_section(
        patch_prefix,
        queue_id=queue_id,
        section=SNAPSHOT_CHAMP_SECTION,
        live_games=total,
        snapshot_dir=snapshot_dir,
    )
    if log:
        log(f"[settle] {patch_prefix} champ counters: {status.describe()}")
    if payload is not None:
        return _decode_champ_counters(payload)
    counters = _scan_patch_counters(db_path, queue_id, patch_prefix)
    patch_snapshot.save_section(
        patch_prefix,
        queue_id=queue_id,
        section=SNAPSHOT_CHAMP_SECTION,
        payload=_encode_champ_counters(counters),
        live_games=total,
        snapshot_dir=snapshot_dir,
    )
    return counters


def compute_settled_winrates(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    *,
    snapshot_dir: Path | None = None,
    live_games: int | None = None,
    log=None,
):
    """``compute_winrates`` for a closed patch, served from its patch snapshot.

    Deliberately takes no prior arguments: every settled-patch caller (the
    comparison baseline, the 新-augment walk-back) wants the unsmoothed-by-prior
    form, and chaining priors backwards across patches is what the compute_winrates
    docstring warns against.
    """
    counters = settled_patch_counters(
        db_path,
        queue_id,
        patch_prefix,
        snapshot_dir=snapshot_dir,
        live_games=live_games,
        log=log,
    )
    return _derive_winrate_records(counters)


def count_patch_games(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
) -> int:
    """Return the total number of games in the requested queue / patch scope."""
    con = sqlite3.connect(str(db_path))
    try:
        if patch_prefix:
            row = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=? AND patch LIKE ?",
                (queue_id, f"{patch_prefix}%"),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()


def count_participant_games(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
) -> int:
    """Number of games (current patch) that actually carry participant augment data.

    This is the right denominator for the augment board's per-game appearance
    rate: games without ``participants_json`` contributed no augment counts, so
    dividing by the full game count would deflate every augment uniformly.  We
    count games where augments are observable instead.
    """
    con = sqlite3.connect(str(db_path))
    try:
        if patch_prefix:
            row = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=? AND patch LIKE ? "
                "AND participants_json IS NOT NULL AND participants_json != ''",
                (queue_id, f"{patch_prefix}%"),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) FROM games WHERE queue_id=? "
                "AND participants_json IS NOT NULL AND participants_json != ''",
                (queue_id,),
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()

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

def raw_wilson_bounds(wins: int, games: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for the *raw* per-pair winrate.

    Used as a trust region for the displayed number: the smoothed/shrunk value
    is clamped into this interval so the headline a user reads can never sit
    outside what the raw sample actually supports.  Wilson (not Wald) so the
    interval does not collapse to a point at p_hat in {0, 1}.
    """
    if games <= 0:
        return 0.0, 1.0
    p = min(max(wins / games, 0.0), 1.0)
    n = float(games)
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


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

def build_augment_global_stats(
    champ_aug_records: list[dict],
    aug_meta: dict[int, dict],
    *,
    appearance_games: int = 0,
    prior_strength: float = AUGMENT_PRIOR_DEFAULT,
    prev_champ_aug_records: list[dict] | None = None,
    current_min_games: int = AUGMENT_CURRENT_MIN_GAMES,
    present_min_games: int = AUGMENT_PRESENT_MIN_GAMES,
) -> dict[int, dict]:
    """Roll the per-(champion, augment) rows up into a global per-augment WR.

    Every Mayhem game equips augments on both teams, so summing wins/games across
    all champions for an augment_id yields that augment's true overall sample (the
    sums are additive).  We EB-shrink the rate toward the overall augment WR
    (~0.50) with posterior_wr_summary -- the same machinery champions use -- and
    attach the average per-game appearance count (``cur_g / appearance_games``) as
    a popularity signal.  ``appearance_games`` is the number of games that carry
    augment data, and a game where several players equip the same augment counts
    every pick, so the share can exceed 1.0.  Tiering is left to the frontend
    (within-rarity percentile of wr) so it can be tuned without a full rebuild.

    The board is scoped to the current patch (``champ_aug_records``).  An augment
    with fewer than ``present_min_games`` games this patch is treated as not in the
    current patch (removed / disabled -- e.g. 0 picks across a mature patch) and is
    dropped entirely, never resurrected from the previous patch.  An augment that
    *is* present but has fewer than ``current_min_games`` games has its sample
    topped up toward that floor with previous-patch games
    (``prev_champ_aug_records``) at the previous patch's win rate -- "mix in some of
    last patch when this patch is thin" -- so rarely-picked augments still surface
    instead of being cut by the frontend's min-games floor.  Well-sampled augments
    stay 100% current patch; ``pick`` always reflects current-patch popularity.
    ``curG`` / ``prevMix`` expose the blend.
    """
    def _rollup(records: list[dict] | None) -> tuple[dict[int, int], dict[int, int]]:
        g_by: dict[int, int] = {}
        w_by: dict[int, int] = {}
        for r in records or []:
            aid = int(r["augment_id"])
            g_by[aid] = g_by.get(aid, 0) + int(r["games"])
            w_by[aid] = w_by.get(aid, 0) + int(r["wins"])
        return g_by, w_by

    games_by, wins_by = _rollup(champ_aug_records)
    prev_games_by, prev_wins_by = _rollup(prev_champ_aug_records)

    total_g = sum(games_by.values())
    total_w = sum(wins_by.values())
    overall = total_w / total_g if total_g else 0.5
    stats: dict[int, dict] = {}
    # Only augments actually present in the current patch.  curG below
    # present_min_games (0 across a mature patch == removed/disabled) is dropped,
    # never resurrected from the previous patch.
    for aid, cur_g in games_by.items():
        if aid not in aug_meta or cur_g < present_min_games:
            continue
        cur_w = wins_by.get(aid, 0)
        eff_g = float(cur_g)
        eff_w = float(cur_w)
        borrowed_g = 0.0
        if cur_g < current_min_games and prev_games_by.get(aid, 0) > 0:
            pg = prev_games_by[aid]
            pw = prev_wins_by[aid]
            borrowed_g = float(min(current_min_games - cur_g, pg))
            eff_g = cur_g + borrowed_g
            eff_w = cur_w + (pw / pg) * borrowed_g
        if eff_g <= 0:
            continue
        mean, lower = posterior_wr_summary(eff_w, eff_g, overall, prior_strength)
        stats[aid] = {
            "g": int(round(eff_g)),
            "wr": mean,
            "rawWr": eff_w / eff_g,
            "lcb": lower,
            "lift": mean - overall,
            # 選用率 = average appearances per current-patch game, counting
            # multiplicity (a game where N players equip this augment contributes
            # N).  Can exceed 1.0 for the most popular augments.
            "pick": (cur_g / appearance_games) if appearance_games > 0 else 0.0,
            "curG": cur_g,
            "prevMix": round(borrowed_g / eff_g, 3) if eff_g else 0.0,
        }
    return stats

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
    # Do not add official `damage` for every matching augment: that bucket is
    # intentionally broad and would drown out AP/AD/crit/snowball style chips.
    # Support (5) / Ally (0) now feed the dedicated `support` chip instead.
    2: {"official_general"},
    3: {"official_tenacity"},
    4: {"official_speed"},
    5: {"official_support"},
    7: {"economy"},
}

_OFFICIAL_AUGMENT_TYPE_LABELS = {
    "official_ally": {"zh": "隊友", "en": "Ally"},
    "official_support": {"zh": "輔助", "en": "Support"},
    "official_general": {"zh": "一般 / 質變", "en": "General / Transmute"},
    "official_tenacity": {"zh": "韌性", "en": "Tenacity"},
    "official_speed": {"zh": "速度", "en": "Speed"},
}

def augment_type_slugs(meta: dict | None) -> set[str]:
    """Fine-grained internal augment-type slugs from name/desc/set/displayTag.

    Shared by the per-champion 'augment tendencies' affinity labels
    (`augment_type_infos`) and the coarse user-facing category filter
    (`augment_filter_categories`)."""
    if not meta:
        return set()
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
    return slugs


def augment_type_infos(meta: dict | None) -> list[dict[str, str]]:
    slugs = augment_type_slugs(meta)
    if not slugs:
        return []
    labels = {**AUGMENT_TYPE_LABELS, **_OFFICIAL_AUGMENT_TYPE_LABELS}
    return [_label_entry(labels, slug) for slug in sorted(slugs)]


# ---- User-facing augment category filter (site chips) -----------------------
# Nine coarse buckets the tier-list site exposes as filter chips above each
# champion's augment ranking.  Most are derived from the finer internal type
# slugs; `cd` and `amp` add dedicated keyword passes (cooldown / damage-amp are
# folded into broader internal slugs), and `new` is the data-driven "introduced
# this patch" set.  Buckets intentionally OVERLAP — an AP burn augment is both
# `ap` and `amp` — because the chips are OR filters, so an augment shows under
# every chip that fits it.
AUGMENT_CATEGORY_ORDER = ("ap", "ad", "tank", "support", "gold", "mechanic", "cd", "new", "crit", "amp")
AUGMENT_CATEGORY_LABELS = {
    "ap":       {"zh": "AP",   "en": "AP"},
    "ad":       {"zh": "AD",   "en": "AD"},
    "tank":     {"zh": "坦度", "en": "Tank"},
    "support":  {"zh": "輔助", "en": "Support"},
    "gold":     {"zh": "金錢", "en": "Gold"},
    "mechanic": {"zh": "機制", "en": "Mechanic"},
    "cd":       {"zh": "CD",   "en": "CD"},
    "new":      {"zh": "新",   "en": "New"},
    "crit":     {"zh": "暴擊", "en": "Crit"},
    "amp":      {"zh": "增傷", "en": "Amp"},
}
# Internal type slug -> user chip.  mobility / utility / official_speed have no
# dedicated chip (they show only under "全部").
_TYPE_SLUG_TO_CATEGORY = {
    "spell": "ap",
    "attack": "ad",
    "crit": "crit",
    "tank": "tank",
    "sustain": "tank",            # heal / shield / lifesteal -> survivability
    "official_tenacity": "tank",
    "official_ally": "support",   # Riot Ally (0) tag
    "official_support": "support",  # Riot Support (5) tag
    "economy": "gold",
    "damage": "amp",
    "auto": "mechanic",
    "stacking": "mechanic",
    "snowball": "mechanic",
    "official_general": "mechanic",  # transmute / general gameplay-altering
}
# Ability-haste / cooldown augments (folded into `spell` internally; the site
# wants a dedicated CD chip).  Matched against en + zh name/desc text.
_AUGMENT_CD_KEYWORDS = (
    "ability haste", "cooldown", "haste", "recharge", "refund",
    "技能急速", "冷卻", "急速",
)
# Damage amplification beyond the internal `damage` type: true damage, execute,
# bonus / extra / increased / % more damage.
_AUGMENT_AMP_KEYWORDS = (
    "true damage", "execute", "amplif", "bonus damage", "extra damage",
    "increased damage", "more damage", "% damage", "deal more",
    "真實傷害", "處決", "增傷", "增加傷害", "額外傷害", "提高傷害", "傷害提高",
)


def augment_filter_categories(
    aid: int,
    meta: dict | None,
    new_aug_ids: set[int] | frozenset[int] = frozenset(),
) -> list[str]:
    """Coarse user-facing categories for the site's augment filter chips.

    Returns an ordered subset of AUGMENT_CATEGORY_ORDER (possibly empty)."""
    if not meta:
        return []
    cats: set[str] = set()
    for slug in augment_type_slugs(meta):
        cat = _TYPE_SLUG_TO_CATEGORY.get(slug)
        if cat:
            cats.add(cat)
    text = " ".join(
        str(meta.get(key) or "")
        for key in ("name", "name_en", "desc", "desc_en")
    ).lower()
    if any(kw in text for kw in _AUGMENT_CD_KEYWORDS):
        cats.add("cd")
    if any(kw in text for kw in _AUGMENT_AMP_KEYWORDS):
        cats.add("amp")
    try:
        if int(aid) in new_aug_ids:
            cats.add("new")
    except (TypeError, ValueError):
        pass
    return [c for c in AUGMENT_CATEGORY_ORDER if c in cats]


# Hand-curated category corrections exported from the augment-category editor
# (scripts/build_augment_category_editor.py -> "匯出").  Maps augment id -> the
# exact category list, REPLACING the fuzzy keyword auto-classification for that
# augment.  `new` is ignored on load and always recomputed from new_aug_ids, so
# the "introduced this patch" flag stays correct on every auto-rebuild.
AUGMENT_CATEGORY_OVERRIDES_PATH = (
    Path(__file__).resolve().parent / "augment_category_overrides.json"
)


def load_augment_category_overrides(
    path: Path = AUGMENT_CATEGORY_OVERRIDES_PATH,
) -> dict[int, list[str]]:
    if not path or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    valid = set(AUGMENT_CATEGORY_ORDER)
    out: dict[int, list[str]] = {}
    for key, val in raw.items():
        try:
            aid = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(val, list):
            continue
        chosen = {c for c in val if c in valid and c != "new"}
        out[aid] = [c for c in AUGMENT_CATEGORY_ORDER if c in chosen]
    return out


def resolve_augment_categories(
    aid: int,
    meta: dict | None,
    new_aug_ids: set[int] | frozenset[int] = frozenset(),
    overrides: dict[int, list[str]] | None = None,
) -> list[str]:
    """Curated override wins over the keyword auto-classifier; `new` is always
    (re)computed from new_aug_ids regardless of any override."""
    try:
        key: int | None = int(aid)
    except (TypeError, ValueError):
        key = None
    if overrides and key in overrides:
        cats = set(overrides[key])
        if key in new_aug_ids:
            cats.add("new")
        return [c for c in AUGMENT_CATEGORY_ORDER if c in cats]
    return augment_filter_categories(aid, meta, new_aug_ids)


def derive_new_augment_ids(
    current_champ_aug: list[dict],
    baseline_champ_aug: list[dict] | None,
    *,
    min_games: int = 20,
    max_baseline_games: int = 3,
) -> frozenset[int]:
    """Augment ids introduced in the current patch.

    "New" = present with at least ``min_games`` games this patch but at most
    ``max_baseline_games`` in the previous patch (essentially absent before).
    Empirically the two sets are cleanly separated — a fresh patch ships a whole
    new id block that is literally absent earlier — so the thresholds only guard
    against parse noise.  Takes the already-aggregated champ x augment rows so it
    costs nothing beyond scans the build already runs."""
    cur: Counter[int] = Counter()
    for row in current_champ_aug:
        cur[int(row["augment_id"])] += int(row.get("games", 0))
    base: Counter[int] = Counter()
    for row in baseline_champ_aug or []:
        base[int(row["augment_id"])] += int(row.get("games", 0))
    return frozenset(
        aid
        for aid, games in cur.items()
        if games >= min_games and base.get(aid, 0) <= max_baseline_games
    )


# An augment stays flagged 新 for this many patches after it first appears.  A
# fresh patch usually ships no new augments and has little data, so anchoring 新
# to *only* the current patch makes the chip empty the moment the patch rolls.
# Window=2 keeps last patch's augments "new" through the following patch.
NEW_AUGMENT_PATCH_WINDOW = 2


def derive_recent_augment_ids(
    db,
    queue_id: int,
    patch_prefix: str | None,
    current_champ_aug: list[dict],
    *,
    window: int = NEW_AUGMENT_PATCH_WINDOW,
    baseline_prefix: str | None = None,
    baseline_champ_aug: list[dict] | None = None,
    log=None,
) -> frozenset[int]:
    """Augment ids introduced within the last ``window`` patches.

    Unions ``derive_new_augment_ids`` across each recent patch transition
    (current-vs-previous, previous-vs-previous2, ...), so an augment shipped one
    patch ago is still flagged 新.  Each transition compares against its own
    adjacent, data-rich baseline rather than one far baseline, keeping the
    "absent before" test reliable even when the newest patch has few games.
    Reuses the already-computed ``baseline_champ_aug`` for the first hop."""
    new_ids: set[int] = set()
    cur_rows = current_champ_aug
    cur_prefix = patch_prefix
    for _ in range(max(1, window)):
        prev_prefix = previous_patch_prefix(cur_prefix)
        if not prev_prefix:
            break
        if prev_prefix == baseline_prefix and baseline_champ_aug is not None:
            prev_rows = baseline_champ_aug
        else:
            # Every hop here is a closed patch, so it is served from (or seeds)
            # that patch's settled snapshot instead of a full rescan -- this walk
            # used to re-read entire 500k-game patches on every build.
            prev_rows = compute_settled_winrates(db, queue_id, prev_prefix, log=log)[1]
        new_ids |= set(derive_new_augment_ids(cur_rows, prev_rows))
        cur_rows = prev_rows
        cur_prefix = prev_prefix
    return frozenset(new_ids)


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
        top_min_pick_rate=ITEM_PAIR_TOP_MIN_PICK_RATE,
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
    top_min_pick_rate: float = 0.0,
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
        top_min_pick_rate=top_min_pick_rate,
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
        top_min_pick_rate=SINGLE_ITEM_TOP_MIN_PICK_RATE,
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
        top_min_pick_rate=BOOT_ITEM_TOP_MIN_PICK_RATE,
        top_n=4,
    )

def compute_champ_spell_affinities(
    db_path: Path,
    queue_id: int,
    patch_prefix: str | None,
    spell_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    min_games: int = SPELL_MIN_GAMES,
) -> dict[int, dict]:
    """Champion-level win/pick rate per summoner spell.

    Mirrors the boot/item affinity pipeline (same empirical-Bayes shrinkage so
    small cells lean on the global per-spell baseline) but reads each
    participant's ``spells`` list instead of items.  Mayhem players freely pick
    *two* spells (Flash is near-universal; Mark/Dash, Ghost, Heal … are the real
    second-slot choices), so every spell is counted and pick rates sum to ~200%
    across the two slots.
    """
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND participants_json LIKE '%\"spells\"%'",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL "
            "AND participants_json LIKE '%\"spells\"%'",
            (queue_id,),
        )

    cs_games: Counter[tuple[int, str]] = Counter()
    cs_wins: Counter[tuple[int, str]] = Counter()
    cs_baseline_games: Counter[tuple[int, str]] = Counter()
    champ_total_games: Counter[int] = Counter()
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
                chosen = [int(s) for s in (participant.get("spells") or []) if int(s) > 0]
                if not chosen:
                    continue
                champ_total_games[cid] += 1
                baseline = baseline_by_champ.get(cid, 0.5)
                player_won = 1 if (team_id == 100) == blue_won else 0
                for spell_id in chosen:
                    slug = str(spell_id)
                    key = (cid, slug)
                    cs_games[key] += 1
                    cs_wins[key] += player_won
                    cs_baseline_games[key] += baseline
                    category_games[slug] += 1
                    category_wins[slug] += player_won
                    category_baseline_games[slug] += baseline
                    if slug not in category_names:
                        meta = spell_meta.get(spell_id) or {}
                        name_zh = str(meta.get("name_zh") or meta.get("name") or f"#{spell_id}")
                        name_en = str(meta.get("name_en") or name_zh)
                        category_names[slug] = {
                            "name": name_zh,
                            "name_zh": name_zh,
                            "name_en": name_en,
                            "items": [{
                                "id": spell_id,
                                "name": name_zh,
                                "name_zh": name_zh,
                                "name_en": name_en,
                                "icon": str(meta.get("icon") or ""),
                            }],
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
        fallback_min_games=SPELL_FALLBACK_MIN_GAMES,
        pick_lift_weight=SINGLE_ITEM_PICK_LIFT_WEIGHT,
        pick_lift_cap=SINGLE_ITEM_PICK_LIFT_CAP,
        pick_rate_weight=SINGLE_ITEM_PICK_RATE_WEIGHT,
        pick_rate_ref=SINGLE_ITEM_PICK_RATE_REF,
        pick_rate_cap=SINGLE_ITEM_PICK_RATE_CAP,
        rank_mode="lift",
        top_min_lift=SPELL_TOP_MIN_LIFT,
        top_n=SPELL_TOP_N,
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

def display_patch_prefix(patch_prefix: str | None) -> str | None:
    """Map internal Riot patch numbers to the public display version.

    Riot's data endpoints and local Mayhem DB still use internal versions like
    ``16.11``, while the public patch notes / user-facing copy show ``26.11``.
    This helper is display-only; queries and asset fetches must keep using the
    original internal version string.
    """
    if not patch_prefix:
        return None
    stripped = patch_prefix.strip()
    match = re.fullmatch(r"(\d+)(\..+)", stripped)
    if not match:
        return stripped
    major = int(match.group(1))
    if 10 <= major < 20:
        return f"{major + 10}{match.group(2)}"
    return stripped

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

def _patch_augment_payload(aid: int, aug_meta: dict[int, dict]) -> dict[str, object]:
    meta = aug_meta.get(aid, {})
    return {
        "id": aid,
        "name": meta.get("name", str(aid)),
        "name_zh": meta.get("name_zh", meta.get("name", str(aid))),
        "name_en": meta.get("name_en", meta.get("name", str(aid))),
        "icon": meta.get("icon", ""),
        "rarity": meta.get("rarity", ""),
    }

def _rollup_augment_records(records: list[dict] | None) -> tuple[dict[int, int], dict[int, int]]:
    games_by: dict[int, int] = {}
    wins_by: dict[int, int] = {}
    for r in records or []:
        aid = int(r["augment_id"])
        games_by[aid] = games_by.get(aid, 0) + int(r["games"])
        wins_by[aid] = wins_by.get(aid, 0) + int(r["wins"])
    return games_by, wins_by

def _compute_core_item_patch_stats(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    item_meta: dict[int, dict],
    champ_records: list[dict],
) -> dict[str, object]:
    return _apply_core_item_baselines(
        _scan_core_item_counters(db_path, queue_id, patch_prefix, item_meta),
        champ_records,
    )


def _apply_core_item_baselines(
    counters: dict[str, object],
    champ_records: list[dict],
) -> dict[str, object]:
    """Attach the champion-baseline terms to raw item counters.

    ``baseline_sum`` is just games x that champion's raw win rate, and
    ``global_wr`` comes straight from champ_records, so neither belongs in a
    frozen snapshot -- both are re-derived here from whatever records the caller
    computed this build.
    """
    champ_baseline = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5) or 0.5)
        for row in champ_records
    }
    champ_item_stats: dict[tuple[int, int], dict[str, float]] = {}
    for (cid, item_id), bucket in (counters["champ_item"] or {}).items():
        games = float(bucket["games"])
        champ_item_stats[(int(cid), int(item_id))] = {
            "games": games,
            "wins": float(bucket["wins"]),
            "baseline_sum": games * champ_baseline.get(int(cid), 0.5),
        }
    return {
        "item": counters["item"],
        "champ_item": champ_item_stats,
        "champ_games": counters["champ_games"],
        "global_wr": _record_global_wr(champ_records),
    }


def _scan_core_item_counters(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    item_meta: dict[int, dict],
) -> dict[str, object]:
    """One pass over a patch's builds -> raw (games, wins) per item and champ x item.

    ``observed_item_ids`` keeps every item id the patch actually contained,
    filter or no filter, so a settled snapshot can tell whether a later change to
    _is_recommendable_core_item would have changed what it counted (see
    _core_item_fingerprint) without re-invalidating on every unrelated Data
    Dragon addition.
    """
    item_stats: dict[int, dict[str, float]] = defaultdict(
        lambda: {"games": 0.0, "wins": 0.0}
    )
    champ_item_stats: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: {"games": 0.0, "wins": 0.0}
    )
    observed_item_ids: set[int] = set()
    # Denominator for the relative (pick-share) floor on 版本變動: how many games
    # this champion had *with any core item*, i.e. the same population the
    # champ_item numerators are drawn from.
    champ_games_stats: dict[int, float] = defaultdict(float)
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
                # Core completed items only (no boots / guardian starters /
                # anti-heal components / augment-gated quest rewards). Matches the
                # site copy on 版本變動 and keeps regular-ARAM starters out.
                raw_ids = participant.get("items") or participant.get("itemSlots") or []
                selected_ids: list[int] = []
                seen_ids: set[int] = set()
                for raw_id in raw_ids:
                    try:
                        item_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if item_id <= 0 or item_id in seen_ids:
                        continue
                    observed_item_ids.add(item_id)
                    if not _is_recommendable_core_item(item_meta.get(item_id)):
                        continue
                    selected_ids.append(item_id)
                    seen_ids.add(item_id)
                if not selected_ids:
                    continue
                player_won = 1 if (team_id == 100) == blue_won else 0
                champ_games_stats[cid] += 1
                for item_id in selected_ids:
                    item_bucket = item_stats[item_id]
                    item_bucket["games"] += 1
                    item_bucket["wins"] += player_won
                    champ_bucket = champ_item_stats[(cid, item_id)]
                    champ_bucket["games"] += 1
                    champ_bucket["wins"] += player_won
    finally:
        con.close()
    return {
        "item": item_stats,
        "champ_item": champ_item_stats,
        "champ_games": champ_games_stats,
        "observed_item_ids": sorted(observed_item_ids),
    }


def _core_item_fingerprint(observed_item_ids, item_meta: dict[int, dict]) -> str:
    """Identity of the core-item FILTER as applied to one patch's observed items.

    A snapshot must be rebuilt when _is_recommendable_core_item starts including
    or excluding an item that patch actually had -- but not when a later Data
    Dragon adds items that never appeared in it.  Hashing the filter's verdict
    over the snapshot's own observed ids gives exactly that.
    """
    core = [int(i) for i in sorted(observed_item_ids) if _is_recommendable_core_item(item_meta.get(int(i)))]
    digest = hashlib.sha1(json.dumps(core, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:16]


def _encode_core_item_counters(counters: dict[str, object]) -> dict:
    return {
        "item": [[int(i), int(b["games"]), int(b["wins"])] for i, b in counters["item"].items()],
        "champ_item": [
            [int(cid), int(iid), int(b["games"]), int(b["wins"])]
            for (cid, iid), b in counters["champ_item"].items()
        ],
        "champ_games": [[int(cid), int(n)] for cid, n in counters["champ_games"].items()],
        "observed_item_ids": [int(i) for i in counters["observed_item_ids"]],
    }


def _decode_core_item_counters(payload: dict) -> dict[str, object]:
    item_stats: dict[int, dict[str, float]] = {}
    for iid, games, wins in payload.get("item") or []:
        item_stats[int(iid)] = {"games": float(games), "wins": float(wins)}
    champ_item: dict[tuple[int, int], dict[str, float]] = {}
    for cid, iid, games, wins in payload.get("champ_item") or []:
        champ_item[(int(cid), int(iid))] = {"games": float(games), "wins": float(wins)}
    champ_games: dict[int, float] = {
        int(cid): float(n) for cid, n in (payload.get("champ_games") or [])
    }
    return {
        "item": item_stats,
        "champ_item": champ_item,
        "champ_games": champ_games,
        "observed_item_ids": [int(i) for i in (payload.get("observed_item_ids") or [])],
    }


def settled_core_item_patch_stats(
    db_path: Path,
    queue_id: int,
    patch_prefix: str,
    item_meta: dict[int, dict],
    champ_records: list[dict],
    *,
    snapshot_dir: Path | None = None,
    live_games: int | None = None,
    log=None,
) -> dict[str, object]:
    """``_compute_core_item_patch_stats`` for a closed patch, from its snapshot.

    Only for non-current patches -- same rule as settled_patch_counters.
    """
    total = count_patch_games(db_path, queue_id, patch_prefix) if live_games is None else live_games
    stored = patch_snapshot.read_section(
        patch_prefix,
        queue_id=queue_id,
        section=SNAPSHOT_ITEM_SECTION,
        snapshot_dir=snapshot_dir,
    )
    stored_observed = ((stored or {}).get("payload") or {}).get("observed_item_ids") or []
    payload, status = patch_snapshot.load_section(
        patch_prefix,
        queue_id=queue_id,
        section=SNAPSHOT_ITEM_SECTION,
        live_games=total,
        # Recomputed from the snapshot's OWN observed ids, so it matches what was
        # stored unless the filter's verdict on those ids actually changed.
        fingerprint=_core_item_fingerprint(stored_observed, item_meta) if stored else None,
        snapshot_dir=snapshot_dir,
    )
    if log:
        log(f"[settle] {patch_prefix} core-item counters: {status.describe()}")
    if payload is not None:
        return _apply_core_item_baselines(_decode_core_item_counters(payload), champ_records)
    counters = _scan_core_item_counters(db_path, queue_id, patch_prefix, item_meta)
    patch_snapshot.save_section(
        patch_prefix,
        queue_id=queue_id,
        section=SNAPSHOT_ITEM_SECTION,
        payload=_encode_core_item_counters(counters),
        live_games=total,
        fingerprint=_core_item_fingerprint(counters["observed_item_ids"], item_meta),
        snapshot_dir=snapshot_dir,
    )
    return _apply_core_item_baselines(counters, champ_records)

def compute_patch_changes(
    db_path: Path,
    queue_id: int,
    current_patch: str | None,
    baseline_patch: str | None,
    item_meta: dict[int, dict],
    champ_meta: dict[int, dict],
    current_records: list[dict],
    baseline_records: list[dict],
    champ_aug_records: list[dict] | None = None,
    baseline_champ_aug: list[dict] | None = None,
    aug_meta: dict[int, dict] | None = None,
    log=None,
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
    # The baseline patch is closed, so its item tallies come from (or seed) its
    # settled snapshot; the current patch is always rescanned.
    baseline_item_stats = settled_core_item_patch_stats(
        db_path, queue_id, baseline_patch, item_meta, baseline_records, log=log
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
    current_champ_games = current_item_stats.get("champ_games") or {}
    baseline_champ_games = baseline_item_stats.get("champ_games") or {}
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
        current_pick = current_games / max(float(current_champ_games.get(cid, 0.0)), 1.0)
        baseline_pick = baseline_games / max(float(baseline_champ_games.get(cid, 0.0)), 1.0)
        if (
            current_pick < PATCH_CHANGE_CHAMP_ITEM_MIN_PICK
            or baseline_pick < PATCH_CHANGE_CHAMP_ITEM_MIN_PICK
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
            "current_pick": round(current_pick, 4),
            "baseline_pick": round(baseline_pick, 4),
        })

    augment_rows: list[dict[str, object]] = []
    if aug_meta:
        cur_aug_games, cur_aug_wins = _rollup_augment_records(champ_aug_records)
        base_aug_games, base_aug_wins = _rollup_augment_records(baseline_champ_aug)
        cur_total_g = sum(cur_aug_games.values())
        base_total_g = sum(base_aug_games.values())
        cur_aug_global = (sum(cur_aug_wins.values()) / cur_total_g) if cur_total_g else 0.5
        base_aug_global = (sum(base_aug_wins.values()) / base_total_g) if base_total_g else 0.5
        for aid, current_games in cur_aug_games.items():
            baseline_games = base_aug_games.get(aid, 0)
            if aid not in aug_meta:
                continue
            if (
                current_games < PATCH_CHANGE_AUGMENT_CURRENT_MIN_GAMES
                or baseline_games < PATCH_CHANGE_AUGMENT_BASELINE_MIN_GAMES
            ):
                continue
            current_wr = _smoothed_patch_wr(
                cur_aug_wins.get(aid, 0), current_games, cur_aug_global, PATCH_CHANGE_AUGMENT_PRIOR_GAMES
            )
            baseline_wr = _smoothed_patch_wr(
                base_aug_wins.get(aid, 0), baseline_games, base_aug_global, PATCH_CHANGE_AUGMENT_PRIOR_GAMES
            )
            augment_rows.append({
                **_patch_augment_payload(aid, aug_meta),
                "current_wr": round(current_wr, 4),
                "baseline_wr": round(baseline_wr, 4),
                "delta": round(current_wr - baseline_wr, 4),
                "current_games": int(current_games),
                "baseline_games": int(baseline_games),
            })

    # 英雄×增幅: same shape as champ×item, but reusing the per-champion augment
    # records the augment board already loads — no extra DB scan.
    champ_aug_rows: list[dict[str, object]] = []
    if aug_meta:
        def _index_champ_aug(records: list[dict] | None) -> dict[tuple[int, int], dict[str, int]]:
            out: dict[tuple[int, int], dict[str, int]] = defaultdict(
                lambda: {"games": 0, "wins": 0}
            )
            for r in records or []:
                key = (int(r["champion_id"]), int(r["augment_id"]))
                out[key]["games"] += int(r["games"])
                out[key]["wins"] += int(r["wins"])
            return out

        cur_ca = _index_champ_aug(champ_aug_records)
        base_ca = _index_champ_aug(baseline_champ_aug)
        for key, current in cur_ca.items():
            cid, aid = key
            baseline = base_ca.get(key)
            if not baseline or aid not in aug_meta or cid not in champ_meta:
                continue
            cur_games = int(current["games"])
            base_games = int(baseline["games"])
            if (
                cur_games < PATCH_CHANGE_CHAMP_AUG_CURRENT_MIN_GAMES
                or base_games < PATCH_CHANGE_CHAMP_AUG_BASELINE_MIN_GAMES
            ):
                continue
            cur_rec = current_by_champ.get(cid)
            base_rec = baseline_by_champ.get(cid)
            if not cur_rec or not base_rec:
                continue
            cur_pick = cur_games / max(int(cur_rec.get("games", 0) or 0), 1)
            base_pick = base_games / max(int(base_rec.get("games", 0) or 0), 1)
            if (
                cur_pick < PATCH_CHANGE_CHAMP_AUG_MIN_PICK
                or base_pick < PATCH_CHANGE_CHAMP_AUG_MIN_PICK
            ):
                continue
            cur_base_wr = float(cur_rec.get("raw_wr", 0.5) or 0.5)
            base_base_wr = float(base_rec.get("raw_wr", 0.5) or 0.5)
            cur_wr = _smoothed_patch_wr(
                int(current["wins"]), cur_games, cur_base_wr, PATCH_CHANGE_CHAMP_AUG_PRIOR_GAMES
            )
            base_wr = _smoothed_patch_wr(
                int(baseline["wins"]), base_games, base_base_wr, PATCH_CHANGE_CHAMP_AUG_PRIOR_GAMES
            )
            cur_lift = cur_wr - cur_base_wr
            base_lift = base_wr - base_base_wr
            champ_aug_rows.append({
                "champ": _patch_champ_payload(cid, champ_meta),
                "augment": _patch_augment_payload(aid, aug_meta),
                "current_wr": round(cur_wr, 4),
                "baseline_wr": round(base_wr, 4),
                "current_lift": round(cur_lift, 4),
                "baseline_lift": round(base_lift, 4),
                "delta": round(cur_lift - base_lift, 4),
                "current_games": cur_games,
                "baseline_games": base_games,
                "current_pick": round(cur_pick, 4),
                "baseline_pick": round(base_pick, 4),
            })

    return {
        "currentPatch": current_patch,
        "baselinePatch": baseline_patch,
        "currentGames": _record_total_games(current_records),
        "baselineGames": _record_total_games(baseline_records),
        "minHeroGames": PATCH_CHANGE_HERO_MIN_GAMES,
        "minItemGames": PATCH_CHANGE_ITEM_CURRENT_MIN_GAMES,
        "minChampItemGames": PATCH_CHANGE_CHAMP_ITEM_CURRENT_MIN_GAMES,
        "minChampItemPick": PATCH_CHANGE_CHAMP_ITEM_MIN_PICK,
        "heroRisers": sorted(hero_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "heroFallers": sorted(hero_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "itemRisers": sorted(item_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "itemFallers": sorted(item_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "champItemRisers": sorted(champ_item_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "champItemFallers": sorted(champ_item_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "minAugmentGames": PATCH_CHANGE_AUGMENT_CURRENT_MIN_GAMES,
        "augmentRisers": sorted(augment_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "augmentFallers": sorted(augment_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
        "minChampAugGames": PATCH_CHANGE_CHAMP_AUG_CURRENT_MIN_GAMES,
        "minChampAugPick": PATCH_CHANGE_CHAMP_AUG_MIN_PICK,
        "champAugRisers": sorted(champ_aug_rows, key=lambda row: row["delta"], reverse=True)[:PATCH_CHANGE_TOP_N],
        "champAugFallers": sorted(champ_aug_rows, key=lambda row: row["delta"])[:PATCH_CHANGE_TOP_N],
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
    core_min_games: int = ITEM_CORE_BUILD_MIN_GAMES,
    min_confirm_games: int = ITEM_CORE_BUILD_MIN_CONFIRM,
    winrate_min_games: int = ITEM_CORE_BUILD_WINRATE_MIN_GAMES,
) -> dict[int, dict]:
    """Recommend item builds keyed by their CORE 2 (earliest-built) items.

    Builds are grouped by their shared rush (the two earliest-built items).  For
    each group we then surface every item built afterwards — not just the single
    3rd-earliest one — that clears a pick-rate OR win-rate bar, each scored on
    its own conditional win/pick rate.  The core-2 pick/win rate carries orders
    of magnitude more support than any exact 6-item route, and a real observed
    6-item completion is kept only as a confirmation badge (``exact_games``).
    Per champion we curate up to ``top_n`` core-2 groups: 1 popular (highest
    pick), up to 2 winrate (highest lower-confidence-bound lift on a stable
    sample), then the rest by a blended score.  ``bot`` stays empty for
    payload-shape parity with the old output.
    """
    core_count = max(1, ITEM_CLUSTER_CORE_ITEM_COUNT)
    baseline_by_champ = {
        int(row["champion_id"]): float(row.get("raw_wr", 0.5))
        for row in champ_records
    }
    con = sqlite3.connect(str(db_path))
    if patch_prefix:
        sql_rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND patch LIKE ? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id, f"{patch_prefix}%"),
        )
    else:
        sql_rows = con.execute(
            "SELECT blue_wins, participants_json FROM games "
            "WHERE queue_id=? AND participants_json IS NOT NULL "
            "AND (participants_json LIKE '%\"items\"%' OR participants_json LIKE '%\"itemSlots\"%')",
            (queue_id,),
        )

    champ_total: Counter[int] = Counter()
    champ_builds: dict[int, list[tuple[tuple[int, ...], int, int]]] = defaultdict(list)
    try:
        for blue_wins, participants_json in sql_rows:
            if not participants_json:
                continue
            blue_won = bool(blue_wins)
            for participant in json.loads(participants_json):
                cid = int(participant.get("championId", 0) or 0)
                team_id = int(participant.get("teamId", 0) or 0)
                if cid <= 0 or team_id not in (100, 200):
                    continue
                route_ids = _participant_route_item_ids(
                    participant.get("items") or participant.get("itemSlots") or [],
                    item_meta,
                )
                champ_total[cid] += 1
                if not route_ids:
                    continue
                core = sorted({
                    item_id for item_id in route_ids
                    if not _is_boot_item(item_meta.get(item_id))
                })
                if not core:
                    continue
                # Keep short (1-2 item) builds too: they carry the build-order
                # signal even though they cannot form a 3-item core themselves.
                boots = [item_id for item_id in route_ids if _is_boot_item(item_meta.get(item_id))]
                player_won = 1 if (team_id == 100) == blue_won else 0
                champ_builds[cid].append((tuple(core), boots[0] if boots else 0, player_won))
    finally:
        con.close()

    full_core_len = max_items - 1
    out: dict[int, dict] = {}
    for cid, builds in champ_builds.items():
        total = champ_total.get(cid, 0)
        if total < min_games or len(builds) < core_min_games:
            continue
        baseline = baseline_by_champ.get(cid, 0.5)

        item_games: Counter[int] = Counter()
        item_size_sum: Counter[int] = Counter()
        for build_core, _boot, _won in builds:
            size = len(build_core)
            for item_id in build_core:
                item_games[item_id] += 1
                item_size_sum[item_id] += size
        item_count = sum(item_games.values())
        global_mean_size = (sum(item_size_sum.values()) / item_count) if item_count else float(max_items)

        def earliness(item_id: int) -> float:
            # Mean build size when the item is present, shrunk toward the champ
            # average.  LOWER = the item shows up even in games that ended early
            # = built first = more core.  Recovers build order from short games.
            return (
                item_size_sum.get(item_id, 0) + ITEM_CORE_BUILD_EARLY_PRIOR * global_mean_size
            ) / (item_games.get(item_id, 0) + ITEM_CORE_BUILD_EARLY_PRIOR)

        def earliness_key(item_id: int) -> tuple[float, int, int]:
            return (earliness(item_id), -item_games.get(item_id, 0), item_id)

        # Group builds by their shared rush — the two earliest-built items.
        # EVERY item built after the core-2 (within the capped canonical route),
        # not just the single 3rd-earliest one, is a candidate "option" scored by
        # its own win/pick rate.  A core-2 block therefore lists all the items it
        # commonly pairs with — collapsing reshuffled-staple cards into one block
        # while still surfacing the 4th/5th-item choices the old single-slot
        # version hid.
        option_index = core_count - 1  # first slot after the core-2 pair
        group_games: Counter[tuple[int, int]] = Counter()
        group_wins: Counter[tuple[int, int]] = Counter()
        option_games: dict[tuple[int, int], Counter[int]] = defaultdict(Counter)
        option_wins: dict[tuple[int, int], Counter[int]] = defaultdict(Counter)
        option_full_games: dict[tuple[int, int], Counter[int]] = defaultdict(Counter)
        for build_core, boot, won in builds:
            if len(build_core) < 2:
                continue
            ranked_core = sorted(build_core, key=earliness_key)  # earliest built first
            core_pair = (ranked_core[0], ranked_core[1])
            group_games[core_pair] += 1
            group_wins[core_pair] += won
            # Canonical 6-item completion: keep the earliest items up to the 6
            # inventory slots (+ boot), dropping whatever is built latest, so the
            # latest-dropped items never become options.
            route_ranked = ranked_core[:full_core_len] if boot else ranked_core[:max_items]
            is_full = len(route_ranked) + (1 if boot else 0) == max_items
            for follow in route_ranked[option_index:]:
                option_games[core_pair][follow] += 1
                option_wins[core_pair][follow] += won
                if is_full:
                    option_full_games[core_pair][follow] += 1

        def smoothed_lift(wins_: int, games_: int) -> tuple[float, float, float]:
            smoothed = (wins_ + baseline * ITEM_CORE_BUILD_PRIOR_GAMES) / (
                games_ + ITEM_CORE_BUILD_PRIOR_GAMES
            )
            raw = wins_ / games_ if games_ else baseline
            stderr = math.sqrt(max(raw * (1.0 - raw), 1e-6) / games_) if games_ else 0.0
            return smoothed, smoothed - baseline, (smoothed - ITEM_CORE_BUILD_LCB_Z * stderr) - baseline

        groups_out: list[dict] = []
        for core_pair, gg in group_games.items():
            if gg < core_min_games:
                continue
            options: list[dict] = []
            for option, og in (option_games.get(core_pair) or {}).items():
                if option in core_pair or og < ITEM_CORE_BUILD_OPTION_MIN_GAMES:
                    continue
                o_smoothed, o_lift, o_lcb = smoothed_lift(option_wins[core_pair][option], og)
                pick_rate = og / max(total, 1)
                # "一定選用率或勝率以上" gate: keep an item if it clears EITHER bar —
                # popular enough (pick rate) OR a confident winrate lift on a
                # stable sample.  All such pairings show, not just the 3rd item.
                is_popular = pick_rate >= ITEM_CORE_BUILD_OPTION_MIN_PICK
                is_winrate = (
                    og >= winrate_min_games and o_lcb > ITEM_CORE_BUILD_WINRATE_MIN_LCB
                )
                if not (is_popular or is_winrate):
                    continue
                options.append({
                    "id": int(option),
                    "games": og,
                    "smoothed_wr": o_smoothed,
                    "lift": o_lift,
                    "core_lcb": o_lcb,
                    "pick_rate": pick_rate,
                    "exact_games": option_full_games[core_pair].get(option, 0),
                    "lane": "",
                })
            if not options:
                continue
            options.sort(key=lambda o: (-o["pick_rate"], -o["lift"]))
            options[0]["lane"] = "popular"
            winrate_opt = max(
                (o for o in options
                 if o is not options[0]
                 and o["games"] >= winrate_min_games
                 and o["core_lcb"] > ITEM_CORE_BUILD_WINRATE_MIN_LCB),
                key=lambda o: o["core_lcb"],
                default=None,
            )
            if winrate_opt is not None:
                winrate_opt["lane"] = "winrate"
            # Keep the laned standouts (popular + winrate) ahead of the rest so
            # the TOP_N cap can never drop a low-pick / high-winrate pick; the
            # remaining qualifying items then follow by pick rate.
            options.sort(key=lambda o: (o["lane"] == "", -o["pick_rate"], -o["lift"]))
            options = options[:ITEM_CORE_BUILD_OPTION_TOP_N]
            option_ids = {o["id"] for o in options}

            g_smoothed, g_lift, _ = smoothed_lift(group_wins[core_pair], gg)
            group_pick = gg / max(total, 1)
            pick_credit = min(
                ITEM_CORE_BUILD_PICK_CREDIT_CAP,
                ITEM_CORE_BUILD_PICK_CREDIT_WEIGHT
                * math.log1p(group_pick / max(ITEM_CORE_BUILD_PICK_RATE_REF, 1e-9)),
            )
            core_ordered = list(core_pair)  # already earliest-first
            # "Also common" tail: frequent follow-ups that did not clear the
            # options gate (or overflowed the cap), shown dim for context.
            tail_ids = [
                item_id for item_id, og in option_games[core_pair].most_common()
                if item_id not in core_pair
                and item_id not in option_ids
                and og >= ITEM_CORE_BUILD_OPTION_MIN_GAMES
            ][:ITEM_CORE_BUILD_TAIL_N]
            names = _item_cluster_names(core_ordered + [o["id"] for o in options[:1]], item_meta)
            groups_out.append({
                **names,
                "slug": "coregroup:" + "+".join(str(i) for i in core_ordered),
                "core_ids": tuple(core_ordered),
                "core_items": _item_pair_payload(core_ordered, item_meta),
                "games": gg,
                "smoothed_wr": g_smoothed,
                "lift": g_lift,
                "pick_rate": group_pick,
                "rank_score": (
                    ITEM_CORE_BUILD_LIFT_WEIGHT * g_lift
                    + pick_credit
                    + ITEM_CORE_BUILD_GAMES_WEIGHT * math.log1p(gg)
                ),
                "options": [
                    {**opt, "item": _item_pair_payload([opt["id"]], item_meta)[0]}
                    for opt in options
                    if _item_pair_payload([opt["id"]], item_meta)
                ],
                "tail_items": _item_pair_payload(tail_ids, item_meta),
            })

        if not groups_out:
            continue
        # Most-played build paths first; keep distinct core pairs only.
        groups_out.sort(key=lambda g: (-g["pick_rate"], -g["rank_score"]))
        selected_groups: list[dict] = []
        seen_pairs: set[frozenset[int]] = set()
        for group in groups_out:
            key = frozenset(group["core_ids"])
            if key in seen_pairs:
                continue
            if selected_groups and group["pick_rate"] < ITEM_CORE_BUILD_GROUP_MIN_PICK:
                continue
            seen_pairs.add(key)
            selected_groups.append(group)
            if len(selected_groups) >= ITEM_CORE_BUILD_GROUP_TOP_N:
                break
        if selected_groups:
            out[cid] = {"groups": selected_groups}
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
    prior_games: float = 0.0,
) -> dict[int, list[dict]]:
    """Per champion, keep same-team teammate rows sorted by synergy lift.

    `lift` is pair WR minus the additive expectation from each champion's
    marginal winrate.  z-score is kept as a confidence tie-breaker, not the
    primary fit metric.
    """
    by_champ: dict[int, list[dict]] = {}
    prior_games = max(0.0, float(prior_games or 0.0))
    for source_row in champ_pairs:
        row = dict(source_row)
        if row["games"] < min_games:
            continue
        games = max(0, int(row.get("games") or 0))
        shrink = games / (games + prior_games) if games + prior_games > 0 else 0.0
        # Raw lift remains in the public row for auditability.  score_lift is
        # the confidence-shrunk value used to decide which 12 top + 12 bottom
        # rows survive payload slimming, matching the runtime pair prior.
        row["score_lift"] = float(row.get("lift") or 0.0) * shrink
        by_champ.setdefault(row["champion_id"], []).append(row)

    for cid, rows in by_champ.items():
        rows.sort(
            key=lambda r: (
                -r["score_lift"],
                -r["z_score"],
                -r["games"],
                r["teammate_id"],
            )
        )
    return by_champ
