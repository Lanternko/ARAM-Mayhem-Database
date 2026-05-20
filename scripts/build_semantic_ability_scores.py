"""Build reviewable champion-level semantic ability scores.

Inputs:
  data/cache/champion_abilities.json from scripts/fetch_champion_abilities.py

Outputs:
  data/cache/champion_semantic_scores.csv
  data/cache/champion_semantic_scores.json

Scores are heuristic 0..3 priors, not ground truth.  The main upgrade in this
version is that `engage_score` and `wave_clear_score` are no longer only
keyword sums.  They now flow through a formula-style pipeline:

  skill metadata -> normalized components -> weighted aggregation -> 0..3 clip

This keeps the downstream interface stable while making the scores much easier
to audit and iterate on.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

import click


SCORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
    "poke_score",
    "sustain_score",
    "frontline_score",
)

CORE_COLUMNS = (
    "wave_clear_score",
    "cc_score",
    "engage_score",
    "damage_score",
)

ENGAGE_WEIGHTS = {
    "range": 0.16,
    "cc": 0.25,
    "follow": 0.17,
    "targets": 0.18,
    "cd": 0.11,
    "hit": 0.13,
}

WAVE_WEIGHTS = {
    "coverage": 0.27,
    "damage": 0.24,
    "cd": 0.18,
    "safety": 0.07,
    "growth": 0.11,
    "reliability": 0.07,
    "prep": 0.06,
}

WAVE_TOP3_WEIGHTS = (0.55, 0.30, 0.15)

BUILD_PROFILE_STATS = {
    "ap_burn": {
        "items": ("Liandry's Torment", "Blackfire Torch", "Rylai's Crystal Scepter"),
        "ap": 260.0,
        "bonus_ad": 0.0,
    },
    "ap_burst": {
        "items": ("Luden's Companion", "Shadowflame", "Stormsurge"),
        "ap": 310.0,
        "bonus_ad": 0.0,
    },
    "ap_battle": {
        "items": ("Riftmaker", "Liandry's Torment", "Rylai's Crystal Scepter"),
        "ap": 260.0,
        "bonus_ad": 0.0,
    },
    "ap_support": {
        "items": ("Imperial Mandate", "Staff of Flowing Water", "Ardent Censer"),
        "ap": 210.0,
        "bonus_ad": 0.0,
    },
    "soldier_mage": {
        "items": ("Nashor's Tooth", "Liandry's Torment", "Shadowflame"),
        "ap": 300.0,
        "bonus_ad": 0.0,
    },
    "ad_crit": {
        "items": ("Infinity Edge", "The Collector", "Rapid Firecannon"),
        "ap": 0.0,
        "bonus_ad": 140.0,
    },
    "ad_caster": {
        "items": ("Manamune", "Serylda's Grudge", "Opportunity"),
        "ap": 0.0,
        "bonus_ad": 165.0,
    },
    "ad_fighter": {
        "items": ("Black Cleaver", "Sundered Sky", "Death's Dance"),
        "ap": 0.0,
        "bonus_ad": 135.0,
    },
    "hybrid_caster": {
        "items": ("Manamune", "Trinity Force", "Rapid Firecannon"),
        "ap": 0.0,
        "bonus_ad": 120.0,
    },
    "tank": {
        "items": ("Sunfire Aegis", "Unending Despair", "Jak'Sho"),
        "ap": 0.0,
        "bonus_ad": 0.0,
    },
}

CHAMPION_BUILD_ARCHETYPE = {
    "Ahri": "ap_burst",
    "Anivia": "ap_burn",
    "Azir": "soldier_mage",
    "Brand": "ap_burn",
    "Corki": "hybrid_caster",
    "KogMaw": "hybrid_caster",
    "Lux": "ap_burst",
    "Malzahar": "ap_burn",
    "Morgana": "ap_burn",
    "Seraphine": "ap_burst",
    "Syndra": "ap_burst",
    "TwistedFate": "ap_burst",
    "Varus": "ad_caster",
    "Velkoz": "ap_burst",
    "Vex": "ap_burst",
    "Xerath": "ap_burst",
    "Ziggs": "ap_burst",
    "Zoe": "ap_burst",
    "Aatrox": "ad_fighter",
    "Diana": "ap_battle",
    "Kayn": "ad_fighter",
    "Rumble": "ap_burn",
    "Smolder": "ad_caster",
}

FORMULA_VERSION = "v4_top3_itemized_scaling"
EMPIRICAL_SCORE_CSV = Path("data/cache/champion_scores_empirical_merged.csv")
SKILL_SEMANTIC_FEATURES_JSON = Path("data/cache/skill_semantic_features.json")

HARD_CC_WORDS = (
    "stun",
    "stuns",
    "stunning",
    "stunned",
    "root",
    "roots",
    "rooted",
    "rooting",
    "airborne",
    "knock up",
    "knocks up",
    "knock back",
    "knocks back",
    "knocked up",
    "knocked back",
    "suppress",
    "suppression",
    "silence",
    "silenced",
    "charm",
    "charms",
    "charmed",
    "taunt",
    "taunts",
    "taunted",
    "fear",
    "fears",
    "feared",
    "flee",
    "flees",
    "sleep",
    "asleep",
    "polymorph",
    "bind",
    "binds",
    "binding",
    "bound",
    "snare",
    "snares",
    "snared",
    "entangle",
    "entangles",
    "entangled",
    "immobilize",
    "immobilizes",
    "immobilized",
    "impale",
    "impales",
    "impaled",
)

SOFT_CC_WORDS = (
    "slow",
    "slows",
    "slowed",
    "cripple",
    "ground",
    "grounded",
    "nearsight",
    "drowsy",
)

MOBILITY_WORDS = (
    "dash",
    "dashes",
    "dashed",
    "dashing",
    "leap",
    "leaps",
    "leapt",
    "blink",
    "blinks",
    "jump",
    "jumps",
    "vault",
    "vaults",
    "rush",
    "rushes",
    "charge toward",
    "charge to",
    "lunges",
    "lunge",
)

AOE_WORDS = (
    "area",
    "nearby",
    "around",
    "cone",
    "line",
    "explosion",
    "explode",
    "shockwave",
    "zone",
    "all enemies",
    "enemies hit",
    "each enemy",
    "bounce",
    "chain",
    "field",
    "storm",
    "minefield",
    "ray",
)

POKE_WORDS = (
    "missile",
    "projectile",
    "beam",
    "bolt",
    "shot",
    "skillshot",
    "orb",
    "spear",
    "rocket",
    "ray",
)

SUSTAIN_TERMS = (
    "heal",
    "heals",
    "healing",
    "healed",
    "shield",
    "shields",
    "shielding",
    "shielded",
    "restore",
    "restores",
    "restoring",
    "restored",
    "regenerate",
    "regenerates",
    "regeneration",
    "life steal",
    "lifesteal",
    "omnivamp",
    "drain",
    "drains",
    "draining",
)

FRONTLINE_WORDS = (
    "damage reduction",
    "armor",
    "magic resist",
    "resistances",
    "unstoppable",
    "tenacity",
    "maximum health",
    "bonus health",
)

TARGETED_WORDS = (
    "next attack",
    "next basic attack",
    "the first enemy hit",
    "target champion",
    "target enemy",
    "at an enemy",
    "to the enemy hit",
    "enemy hit by",
)

AUTO_ATTACK_WORDS = (
    "basic attack",
    "basic attacks",
    "auto attacks",
    "next attack",
    "on-hit",
    "attack speed",
    "attacks are empowered",
)

ATTACK_RESET_WORDS = (
    "next attack",
    "empowered attack",
    "basic attack",
    "on-hit",
    "resets",
    "ricochet",
    "bounce",
)

MINION_HINT_WORDS = (
    "minion",
    "minions",
    "wave",
    "nearby targets",
    "subsequent targets",
    "bounce",
)

FALSE_PULL_PHRASES = (
    "pulls back her orb",
    "throws then pulls back her orb",
    "sends out and pulls back her orb",
    "grappling hook",
    "hook into terrain",
    "attaching to the first terrain hit",
    "pulls the spirit",
    "pull the spirit",
    "spirit from an enemy champion",
)

CHAMPION_ONLY_HINTS = (
    "enemy champion",
    "enemy champions",
    "ally champion",
    "ally champions",
    "allied champion",
    "allied champions",
    "target champion",
    "targeted champion",
    "between allied and enemy champions",
    "nearby enemy champions",
)

UTILITY_ONLY_HINTS = (
    "grant vision",
    "reveals the area",
    "reveal the area",
    "opens a one-way portal",
    "portal through terrain",
    "gaining access to new abilities",
    "gain access to new abilities",
    "cannot attack",
    "health shrine",
)

MINION_WORDS = (
    "minion",
    "minions",
    "lane minion",
    "lane minions",
    "wave",
)

MONSTER_WORDS = (
    "monster",
    "monsters",
    "epic monster",
    "epic monsters",
)

GENERIC_ENEMY_WORDS = (
    "enemy",
    "enemies",
    "foe",
    "foes",
)

RECAST_HINT_WORDS = (
    "recast:",
    "may recast",
    "can recast",
    "can cast",
    "allows",
    "for the next",
)

THIRD_CAST_HINT_WORDS = (
    "third hit",
    "third cast",
    "three times",
    "reactivated three times",
)

TRANSFORM_HINT_WORDS = (
    "transforms into",
    "transforming into",
    "true form",
    "pairing up",
    "consuming void coral",
)

CONDITIONAL_TRIGGER_HINT_WORDS = (
    "if enemies dash through",
    "if enemies are knocked through",
    "if this hits an enemy,",
    "if sonic wave hits",
    "if tempest hits an enemy",
    "detonated early by another",
    "enemies in the center are also stunned",
    "in the center are also stunned",
    "if it hits",
    "if the target is ablaze",
    "if an enemy unit or structure is hit",
)

STACK_GATED_HINT_WORDS = (
    "at 2 stacks",
    "while he has 2 stacks",
    "at full stacks",
    "next basic attack against an enemy champion",
)

SETUP_REQUIRED_HINT_WORDS = (
    "affected by",
    "per stack of",
    "if this ability is cast during",
    "if this ability is cast towards",
)

ALLY_SETUP_HINT_WORDS = (
    "to an airborne enemy champion",
    "holding all airborne enemies",
)

RESOURCE_GATED_HINT_WORDS = (
    "can only use this ability if",
    "style rating is s",
    "consumes all style rating",
)

ALLY_BENEFIT_HINT_WORDS = (
    "allies hit",
    "ally hit",
    "allied champions",
    "allied champion",
    "grant allies",
    "allies gain",
)

FORM_BRANCH_HINT_WORDS = (
    "darkin slayer:",
    "shadow assassin:",
    "transcendent state",
    "max ferocity:",
    "human form:",
    "cougar form:",
    "mounted:",
    "dismounted:",
)

CHARGE_UP_HINT_WORDS = (
    "begin charging",
    "begins charging",
    "charges up",
    "charging:",
)

TRAP_DELAY_HINT_WORDS = (
    "stealths itself after",
    "activates when an enemy comes near",
    "when triggered",
    "when sprung",
    "when an enemy walks over it",
    "steps on it",
)


# Per-ability review overrides for important edge cases where Data Dragon text
# alone is too lossy.  These are intentionally sparse and should stay reviewable.
ABILITY_REVIEW_OVERRIDES: dict[tuple[str, str], dict[str, float | str]] = {
    ("Blitzcrank", "Q"): {
        "shape": "line",
        "width": 140.0,
        "speed": 1800.0,
        "cast_time": 0.25,
        "cc_type": "hook_pull",
        "cc_duration": 1.0,
        "certainty": 0.72,
        "targeting_bonus": 0.52,
        "expected_targets": 1.0,
        "entry_followthrough": 1.0,
        "wave_reliability": 0.18,
        "damage_proxy": 0.22,
    },
    ("Blitzcrank", "E"): {
        "cc_type": "single_hard",
        "cc_duration": 1.0,
        "certainty": 0.98,
        "entry_followthrough": 0.72,
    },
    ("Lux", "Q"): {
        "shape": "line",
        "width": 140.0,
        "speed": 1200.0,
        "cast_time": 0.25,
        "cc_type": "root",
        "cc_duration": 2.0,
        "certainty": 0.78,
        "targeting_bonus": 0.6,
        "expected_targets": 1.35,
        "entry_followthrough": 0.35,
    },
    ("Lux", "E"): {
        "shape": "circle",
        "radius": 310.0,
        "persistence": 0.45,
        "wave_reliability": 0.88,
    },
    ("Sivir", "Q"): {
        "shape": "line",
        "width": 100.0,
        "speed": 1350.0,
        "cast_time": 0.25,
        "pierce_bounce": 0.95,
        "wave_reliability": 0.84,
    },
    ("Sivir", "W"): {
        "shape": "bounce",
        "cast_range": 525.0,
        "pierce_bounce": 1.0,
        "self_commit": 0.0,
        "wave_reliability": 0.97,
        "damage_proxy": 0.7,
        "growth_proxy": 0.58,
    },
    ("Vayne", "E"): {
        "shape": "line",
        "width": 80.0,
        "speed": 1600.0,
        "cast_time": 0.25,
        "cc_type": "conditional_knockback",
        "cc_duration": 1.0,
        "certainty": 0.58,
        "expected_targets": 1.0,
        "entry_followthrough": 0.12,
        "condition_penalty": 0.28,
    },
    ("Ziggs", "Q"): {
        "shape": "circle",
        "radius": 180.0,
        "wave_reliability": 0.78,
        "damage_proxy": 0.78,
    },
    ("Ziggs", "E"): {
        "shape": "circle",
        "radius": 325.0,
        "persistence": 1.0,
        "wave_reliability": 0.9,
        "damage_proxy": 0.74,
    },
    ("Leona", "E"): {
        "shape": "line",
        "width": 140.0,
        "speed": 2000.0,
        "cast_time": 0.25,
        "cc_type": "root",
        "cc_duration": 0.5,
        "certainty": 0.82,
        "targeting_bonus": 0.75,
        "entry_followthrough": 0.92,
    },
    ("Leona", "R"): {
        "shape": "circle",
        "radius": 250.0,
        "cc_type": "aoe_hard",
        "cc_duration": 1.5,
        "certainty": 0.74,
        "expected_targets": 1.9,
        "entry_followthrough": 0.58,
    },
    ("Ashe", "R"): {
        "range": 2500.0,
        "shape": "line",
        "width": 130.0,
        "speed": 1600.0,
        "cast_time": 0.25,
        "cc_type": "single_hard",
        "cc_duration": 1.75,
        "certainty": 0.68,
        "expected_targets": 1.0,
        "entry_followthrough": 0.28,
        "wave_reliability": 0.08,
        "damage_proxy": 0.18,
    },
    ("Orianna", "Q"): {
        "shape": "line",
        "width": 100.0,
        "cast_range": 825.0,
        "wave_reliability": 0.64,
        "damage_proxy": 0.54,
    },
    ("Orianna", "W"): {
        "shape": "circle",
        "radius": 215.0,
        "wave_reliability": 0.52,
        "damage_proxy": 0.4,
    },
    ("Orianna", "R"): {
        "shape": "circle",
        "radius": 300.0,
        "wave_reliability": 0.42,
        "damage_proxy": 0.34,
    },
    ("Xerath", "Q"): {
        "shape": "line",
        "width": 180.0,
        "cast_range": 1300.0,
        "wave_reliability": 0.96,
        "damage_proxy": 0.94,
    },
    ("Xerath", "W"): {
        "shape": "circle",
        "radius": 260.0,
        "wave_reliability": 0.66,
        "damage_proxy": 0.5,
    },
    ("Xerath", "E"): {
        "shape": "line",
        "width": 70.0,
        "speed": 1400.0,
        "certainty": 0.58,
        "targeting_bonus": 0.46,
    },
    ("AurelionSol", "Q"): {
        "shape": "line",
        "width": 180.0,
        "persistence": 0.82,
        "wave_reliability": 0.84,
        "damage_proxy": 0.82,
    },
    ("AurelionSol", "E"): {
        "shape": "circle",
        "radius": 260.0,
        "persistence": 0.95,
        "wave_reliability": 0.88,
        "damage_proxy": 0.54,
    },
    ("Nidalee", "W"): {
        "shape": "targeted",
        "persistence": 0.15,
        "wave_reliability": 0.08,
        "damage_proxy": 0.06,
    },
    ("Brand", "W"): {
        "shape": "circle",
        "radius": 250.0,
        "wave_reliability": 0.9,
        "damage_proxy": 0.82,
    },
    ("Brand", "E"): {
        "shape": "circle",
        "radius": 260.0,
        "wave_reliability": 0.78,
        "damage_proxy": 0.62,
    },
    ("Bard", "Q"): {
        "shape": "line",
        "range": 950.0,
        "width": 90.0,
        "speed": 1500.0,
        "certainty": 0.6,
        "condition_penalty": 0.2,
        "wave_reliability": 0.28,
        "damage_proxy": 0.26,
    },
    ("Renata", "E"): {
        "shape": "line",
        "width": 120.0,
        "wave_reliability": 0.58,
        "damage_proxy": 0.44,
    },
    ("Caitlyn", "W"): {
        "shape": "targeted",
        "certainty": 0.34,
        "condition_penalty": 0.35,
        "entry_followthrough": 0.05,
    },
    ("Caitlyn", "E"): {
        "shape": "line",
        "cc_type": "soft_slow",
        "engage_gate": "soft_cc_only",
        "certainty": 0.58,
        "targeting_bonus": 0.24,
        "entry_followthrough": 0.04,
    },
    ("Rell", "W"): {
        "shape": "dash",
        "cc_type": "aoe_hard",
        "cc_duration": 1.0,
        "certainty": 0.76,
        "expected_targets": 1.8,
        "entry_followthrough": 0.82,
    },
    ("Rell", "R"): {
        "shape": "circle",
        "cc_type": "aoe_hard",
        "cc_duration": 1.2,
        "certainty": 0.88,
        "expected_targets": 2.1,
        "entry_followthrough": 0.86,
    },
    ("Morgana", "Q"): {
        "shape": "line",
        "width": 140.0,
        "speed": 1200.0,
        "certainty": 0.68,
    },
    ("Zyra", "E"): {
        "shape": "line",
        "width": 140.0,
        "speed": 1500.0,
        "cc_type": "root",
        "certainty": 0.74,
        "expected_targets": 1.55,
    },
    ("Malphite", "R"): {
        "shape": "dash",
        "cc_type": "aoe_hard",
        "cc_duration": 1.5,
        "certainty": 0.92,
        "expected_targets": 1.9,
        "entry_followthrough": 0.94,
    },
    ("TwistedFate", "Q"): {
        "shape": "line",
        "width": 150.0,
        "cast_range": 1450.0,
        "wave_reliability": 0.88,
        "damage_proxy": 0.74,
    },
    ("Zoe", "W"): {
        "shape": "targeted",
        "wave_reliability": 0.08,
        "damage_proxy": 0.16,
    },
    ("Soraka", "E"): {
        "shape": "circle",
        "radius": 250.0,
        "wave_reliability": 0.42,
        "damage_proxy": 0.34,
        "prep_bonus": 0.12,
    },
    ("Diana", "Q"): {
        "shape": "line",
        "width": 180.0,
        "cast_range": 900.0,
        "wave_reliability": 0.9,
        "damage_proxy": 0.82,
    },
    ("Diana", "W"): {
        "shape": "circle",
        "cast_range": 200.0,
        "wave_reliability": 0.44,
        "damage_proxy": 0.42,
    },
    ("Diana", "W"): {
        "shape": "circle",
        "cast_range": 200.0,
        "wave_reliability": 0.44,
        "damage_proxy": 0.42,
    },
    ("Ashe", "W"): {
        "shape": "cone",
        "cast_range": 1200.0,
        "wave_reliability": 0.9,
        "damage_proxy": 0.76,
    },
    ("Corki", "Q"): {
        "shape": "circle",
        "radius": 250.0,
        "cast_range": 825.0,
        "wave_reliability": 0.84,
        "damage_proxy": 0.72,
        "prep_bonus": 0.16,
    },
    ("Corki", "R"): {
        "shape": "line",
        "width": 90.0,
        "wave_reliability": 0.58,
        "damage_proxy": 0.52,
    },
    ("Corki", "R"): {
        "shape": "line",
        "width": 90.0,
        "wave_reliability": 0.58,
        "damage_proxy": 0.52,
    },
    ("XinZhao", "E"): {
        "shape": "dash",
        "wave_reliability": 0.14,
        "damage_proxy": 0.18,
    },
    ("Yuumi", "Q"): {
        "shape": "line",
        "width": 70.0,
        "wave_reliability": 0.12,
        "damage_proxy": 0.12,
    },
    ("Rumble", "Q"): {
        "shape": "cone",
        "cast_range": 600.0,
        "wave_reliability": 0.86,
        "damage_proxy": 0.78,
    },
    ("Kayn", "Q"): {
        "shape": "dash",
        "wave_reliability": 0.58,
        "damage_proxy": 0.58,
    },
    ("Kayn", "W"): {
        "shape": "line",
        "wave_reliability": 0.66,
        "damage_proxy": 0.54,
    },
    ("Smolder", "W"): {
        "shape": "line",
        "width": 140.0,
        "cast_range": 1000.0,
        "wave_reliability": 0.84,
        "damage_proxy": 0.7,
    },
    ("Smolder", "E"): {
        "shape": "line",
        "width": 180.0,
        "cast_range": 800.0,
        "persistence": 0.45,
        "wave_reliability": 0.82,
        "damage_proxy": 0.64,
    },
    ("Anivia", "R"): {
        "shape": "circle",
        "radius": 400.0,
        "cast_range": 750.0,
        "persistence": 1.0,
        "wave_reliability": 0.94,
        "damage_proxy": 0.84,
        "prep_bonus": 0.28,
    },
    ("Anivia", "Q"): {
        "shape": "line",
        "wave_reliability": 0.56,
        "damage_proxy": 0.42,
    },
    ("Ahri", "Q"): {
        "shape": "line",
        "pierce_bounce": 0.82,
        "wave_reliability": 0.82,
        "damage_proxy": 0.76,
    },
    ("Ahri", "W"): {
        "shape": "circle",
        "wave_reliability": 0.54,
        "damage_proxy": 0.34,
    },
    ("Sion", "E"): {
        "shape": "line",
        "wave_reliability": 0.4,
        "damage_proxy": 0.3,
        "pierce_bounce": 0.34,
    },
    ("Varus", "Q"): {
        "shape": "line",
        "width": 120.0,
        "cast_range": 1550.0,
        "wave_reliability": 0.82,
        "damage_proxy": 0.72,
    },
    ("Varus", "E"): {
        "shape": "circle",
        "radius": 260.0,
        "cast_range": 925.0,
        "wave_reliability": 0.84,
        "damage_proxy": 0.76,
        "prep_bonus": 0.18,
    },
    ("Malzahar", "E"): {
        "shape": "bounce",
        "pierce_bounce": 0.92,
        "persistence": 0.9,
        "wave_reliability": 0.94,
        "damage_proxy": 0.72,
        "growth_proxy": 0.62,
    },
    ("Swain", "Q"): {
        "shape": "cone",
        "cast_range": 725.0,
        "wave_reliability": 0.82,
        "damage_proxy": 0.72,
    },
    ("Nasus", "E"): {
        "shape": "circle",
        "radius": 400.0,
        "cast_range": 650.0,
        "persistence": 0.9,
        "wave_reliability": 0.88,
        "damage_proxy": 0.68,
        "prep_bonus": 0.2,
    },
    ("Aatrox", "Q"): {
        "shape": "line",
        "width": 220.0,
        "cast_range": 650.0,
        "wave_reliability": 0.74,
        "damage_proxy": 0.68,
    },
    ("Akali", "Q"): {
        "shape": "cone",
        "cast_range": 500.0,
        "wave_reliability": 0.72,
        "damage_proxy": 0.62,
    },
    ("Neeko", "Q"): {
        "shape": "circle",
        "radius": 250.0,
        "cast_range": 800.0,
        "persistence": 0.62,
        "wave_reliability": 0.86,
        "damage_proxy": 0.74,
    },
    ("Syndra", "Q"): {
        "shape": "circle",
        "radius": 225.0,
        "cast_range": 800.0,
        "wave_reliability": 0.86,
        "damage_proxy": 0.76,
    },
    ("Syndra", "W"): {
        "shape": "circle",
        "radius": 260.0,
        "cast_range": 925.0,
        "wave_reliability": 0.78,
        "damage_proxy": 0.64,
    },
    ("Syndra", "E"): {
        "shape": "line",
        "width": 220.0,
        "cast_range": 700.0,
        "wave_reliability": 0.7,
        "damage_proxy": 0.52,
    },
    ("Velkoz", "Q"): {
        "shape": "line",
        "width": 100.0,
        "cast_range": 1100.0,
        "wave_reliability": 0.78,
        "damage_proxy": 0.64,
    },
    ("Velkoz", "W"): {
        "shape": "line",
        "width": 180.0,
        "cast_range": 1050.0,
        "persistence": 0.52,
        "wave_reliability": 0.88,
        "damage_proxy": 0.8,
    },
    ("Velkoz", "E"): {
        "shape": "circle",
        "radius": 225.0,
        "cast_range": 850.0,
        "wave_reliability": 0.76,
        "damage_proxy": 0.54,
    },
    ("Kennen", "W"): {
        "shape": "circle",
        "radius": 300.0,
        "wave_reliability": 0.18,
        "damage_proxy": 0.2,
    },
    ("Kennen", "E"): {
        "shape": "dash",
        "wave_reliability": 0.42,
        "damage_proxy": 0.38,
    },
    ("Kennen", "R"): {
        "shape": "circle",
        "radius": 550.0,
        "persistence": 0.95,
        "cc_type": "aoe_hard",
        "cc_duration": 1.2,
        "certainty": 0.72,
        "expected_targets": 1.9,
        "entry_followthrough": 0.72,
    },
    ("Morgana", "W"): {
        "shape": "circle",
        "radius": 300.0,
        "persistence": 0.95,
        "damage_proxy": 0.78,
        "wave_reliability": 0.92,
    },
    ("Nami", "Q"): {
        "shape": "circle",
        "radius": 180.0,
        "damage_proxy": 0.28,
        "wave_reliability": 0.35,
    },
    ("Nami", "W"): {
        "shape": "bounce",
        "damage_proxy": 0.18,
        "wave_reliability": 0.22,
    },
    ("RekSai", "Q"): {
        "shape": "circle",
        "radius": 180.0,
        "damage_proxy": 0.58,
        "wave_reliability": 0.58,
    },
    ("RekSai", "W"): {
        "range": 250.0,
        "shape": "circle",
        "radius": 220.0,
        "wave_reliability": 0.05,
        "damage_proxy": 0.12,
    },
    ("Tristana", "E"): {
        "shape": "circle",
        "radius": 220.0,
        "damage_proxy": 0.58,
        "wave_reliability": 0.74,
    },
}


# Champion-level score overrides are intentionally disabled for now.  The
# current iteration should expose mechanism failures through debug output rather
# than masking them with final-score patches.
REVIEWED_OVERRIDES: dict[str, dict[str, float]] = {}


def clamp_score(value: float) -> float:
    return round(max(0.0, min(3.0, value)), 2)


_EMPIRICAL_FLOOR_CACHE: dict[str, tuple[float, float]] | None = None
_EMPIRICAL_DAMAGE_MIX_CACHE: dict[str, tuple[float, float, float]] | None = None
_SKILL_FEATURE_CACHE: dict[tuple[str, str], dict[str, Any]] | None = None


def empirical_floor_map() -> dict[str, tuple[float, float]]:
    global _EMPIRICAL_FLOOR_CACHE
    if _EMPIRICAL_FLOOR_CACHE is not None:
        return _EMPIRICAL_FLOOR_CACHE
    out: dict[str, tuple[float, float]] = {}
    if not EMPIRICAL_SCORE_CSV.exists():
        _EMPIRICAL_FLOOR_CACHE = out
        return out
    with EMPIRICAL_SCORE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dpms = [float(r.get("empirical_damage_per_min") or 0.0) for r in rows]
    dpms = [v for v in dpms if v > 0.0]
    dpm_min = min(dpms) if dpms else 0.0
    dpm_max = max(dpms) if dpms else 1.0
    for row in rows:
        alias = str(row.get("champion_alias") or "")
        if not alias:
            continue
        dpm = float(row.get("empirical_damage_per_min") or 0.0)
        dmg_score = float(row.get("damage_score") or 0.0)
        dpm_norm = norm(dpm, dpm_min, dpm_max) if dpm > 0.0 and dpm_max > dpm_min else 0.0
        dmg_norm = clamp01(dmg_score / 3.0)
        out[alias] = (dpm_norm, dmg_norm)
    _EMPIRICAL_FLOOR_CACHE = out
    return out


def empirical_damage_mix_map() -> dict[str, tuple[float, float, float]]:
    global _EMPIRICAL_DAMAGE_MIX_CACHE
    if _EMPIRICAL_DAMAGE_MIX_CACHE is not None:
        return _EMPIRICAL_DAMAGE_MIX_CACHE
    out: dict[str, tuple[float, float, float]] = {}
    if not EMPIRICAL_SCORE_CSV.exists():
        _EMPIRICAL_DAMAGE_MIX_CACHE = out
        return out
    with EMPIRICAL_SCORE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        alias = str(row.get("champion_alias") or "")
        if not alias:
            continue
        physical = clamp01(float(row.get("empirical_physical_damage_ratio") or 0.0))
        magic = clamp01(float(row.get("empirical_magic_damage_ratio") or 0.0))
        true = clamp01(float(row.get("empirical_true_damage_ratio") or 0.0))
        out[alias] = (physical, magic, true)
    _EMPIRICAL_DAMAGE_MIX_CACHE = out
    return out


def skill_feature_map() -> dict[tuple[str, str], dict[str, Any]]:
    global _SKILL_FEATURE_CACHE
    if _SKILL_FEATURE_CACHE is not None:
        return _SKILL_FEATURE_CACHE
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not SKILL_SEMANTIC_FEATURES_JSON.exists():
        _SKILL_FEATURE_CACHE = out
        return out
    raw = json.loads(SKILL_SEMANTIC_FEATURES_JSON.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        _SKILL_FEATURE_CACHE = out
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        alias = str(row.get("champion_alias") or "")
        slot = str(row.get("spell_slot") or "")
        if not alias or not slot:
            continue
        out[(alias, slot)] = row
    _SKILL_FEATURE_CACHE = out
    return out


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def norm(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return clamp01((value - lo) / (hi - lo))


def text_of(ability: dict[str, Any]) -> str:
    parts = [
        ability.get("description_en_clean", ""),
        ability.get("tooltip_en_clean", ""),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def passive_text(champion: dict[str, Any]) -> str:
    passive = champion.get("passive") or {}
    return " ".join(
        str(passive.get(k, ""))
        for k in ("name_en", "description_en_clean")
        if passive.get(k)
    ).lower()


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def scrub_analysis_text(text: str) -> str:
    text = re.sub(r"passive:\s*leveling up this ability allows.*", "", text)
    kept: list[str] = []
    for sentence in split_sentences(text):
        lowered = sentence.lower()
        if not lowered:
            continue
        if "spellmodifierdescriptionappend" in lowered:
            continue
        if "passive:" in lowered and "active:" not in lowered:
            continue
        if contains_any(lowered, MONSTER_WORDS) and not (
            contains_any(lowered, GENERIC_ENEMY_WORDS)
            or contains_any(lowered, CHAMPION_ONLY_HINTS)
            or contains_any(lowered, MINION_WORDS)
        ):
            continue
        kept.append(lowered)
    return " ".join(kept)


def infer_target_domain(text: str) -> str:
    champion_only = contains_any(text, CHAMPION_ONLY_HINTS)
    minion = contains_any(text, MINION_WORDS)
    monster = contains_any(text, MONSTER_WORDS)
    generic_enemy = contains_any(text, GENERIC_ENEMY_WORDS)

    if champion_only and not (minion or monster or generic_enemy):
        return "champion_only"
    if monster and not (champion_only or minion or generic_enemy):
        return "monster_only"
    if champion_only and (minion or monster or generic_enemy):
        return "mixed"
    if minion and monster:
        return "mixed"
    if minion:
        return "minion_ok"
    if generic_enemy:
        return "enemy_generic"
    if monster:
        return "monster_only"
    return "unknown"


def infer_effect_scope(text: str) -> str:
    lowered = text.lower()
    if "leveling up this ability allows" in lowered or "evolve one of his abilities" in lowered:
        return "evolve_branch"
    if "passive:" in lowered and "active:" in lowered:
        return "active_plus_passive"
    if contains_any(lowered, MONSTER_WORDS):
        return "monster_clause"
    return "primary"


def infer_cast_state(text: str) -> str:
    lowered = text.lower()
    if contains_any(lowered, THIRD_CAST_HINT_WORDS):
        return "third_cast"
    if contains_any(lowered, ("next attack", "next basic attack")):
        return "attack_window"
    if contains_any(lowered, FORM_BRANCH_HINT_WORDS):
        return "form_branch"
    if contains_any(lowered, CHARGE_UP_HINT_WORDS):
        return "charge_up"
    if contains_any(lowered, TRAP_DELAY_HINT_WORDS):
        return "trap_delay"
    if contains_any(lowered, ALLY_SETUP_HINT_WORDS):
        return "ally_setup"
    if contains_any(lowered, RESOURCE_GATED_HINT_WORDS):
        return "resource_gated"
    if contains_any(lowered, STACK_GATED_HINT_WORDS):
        return "stack_gated"
    if contains_any(lowered, SETUP_REQUIRED_HINT_WORDS):
        return "setup_required"
    if contains_any(lowered, CONDITIONAL_TRIGGER_HINT_WORDS):
        return "conditional_trigger"
    if "worked ground" in lowered:
        return "worked_ground"
    if contains_any(lowered, TRANSFORM_HINT_WORDS):
        return "conditional_transform"
    if "attacks to become" in lowered or "attacks become" in lowered:
        return "attack_window"
    if contains_any(lowered, RECAST_HINT_WORDS):
        return "recast"
    return "always"


def infer_engage_gate(
    cc_type: str,
    mobility: bool,
    target_domain: str,
    cast_state: str,
    text: str,
) -> str:
    if target_domain == "monster_only":
        return "invalid_monster_only"
    if cc_type == "hook_pull":
        return "forced_displacement"
    if cc_type in {"aoe_hard", "single_hard", "root", "conditional_knockback"}:
        if not mobility and contains_any(text, ALLY_BENEFIT_HINT_WORDS):
            return "soft_followup"
        if cast_state in {
            "third_cast",
            "recast",
            "worked_ground",
            "conditional_transform",
            "conditional_trigger",
            "form_branch",
            "charge_up",
            "trap_delay",
            "stack_gated",
            "ally_setup",
            "setup_required",
            "resource_gated",
        }:
            return "conditional_hard_cc"
        return "hard_cc"
    if cc_type in {"soft_slow", "utility_cc"}:
        return "soft_followup" if mobility else "soft_cc_only"
    if mobility:
        return "mobility_only"
    return "none"


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def contains_terms(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text):
            return True
    return False


def number_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and math.isfinite(float(item)):
            out.append(float(item))
    return out


def spell_range(ability: dict[str, Any]) -> float:
    xs = [x for x in number_list(ability.get("range")) if 0 < x < 10000]
    if xs:
        return max(xs)
    raw = str(ability.get("range_burn") or "")
    nums: list[float] = []
    for part in raw.split("/"):
        try:
            value = float(part)
        except ValueError:
            continue
        if 0 < value < 10000:
            nums.append(value)
    return max(nums) if nums else 0.0


def infer_range(alias: str, slot: str, ability: dict[str, Any]) -> float:
    override = ability_stat_override(alias, slot, "range")
    if isinstance(override, (int, float)):
        return float(override)
    return spell_range(ability)


def cooldown_min(ability: dict[str, Any]) -> float:
    xs = [x for x in number_list(ability.get("cooldown")) if x > 0]
    return min(xs) if xs else 99.0


def effect_numbers(ability: dict[str, Any]) -> list[float]:
    nums: list[float] = []
    for raw in ability.get("effect_burn") or []:
        if not raw:
            continue
        for part in str(raw).split("/"):
            try:
                value = float(part)
            except ValueError:
                continue
            if math.isfinite(value):
                nums.append(value)
    return nums


def ability_stat_override(alias: str, slot: str, key: str) -> float | str | None:
    return ABILITY_REVIEW_OVERRIDES.get((alias, slot), {}).get(key)


def ability_context(champion: dict[str, Any], ability: dict[str, Any]) -> dict[str, Any]:
    alias = str(champion.get("alias") or "")
    tags = set(champion.get("tags") or [])
    slot = str(ability.get("slot") or "?")
    spell_name_en = str(ability.get("name_en") or "")
    raw_text = text_of(ability)
    spell_name_lower = spell_name_en.lower().strip()
    if spell_name_lower and len(spell_name_lower.split()) > 1:
        raw_text = raw_text.replace(spell_name_lower, " ")
        for part in (part.strip() for part in spell_name_lower.split("/") if part.strip()):
            if len(part.split()) > 1:
                raw_text = raw_text.replace(part, " ")
    scoped_text = scrub_analysis_text(raw_text)
    target_domain = infer_target_domain(scoped_text)
    effect_scope = infer_effect_scope(raw_text)
    cast_state = infer_cast_state(raw_text)
    rng = infer_range(alias, slot, ability)
    cast_range = infer_cast_range(alias, slot, rng)
    cd = cooldown_min(ability)
    aoe = is_aoe(ability, scoped_text)
    mobility = has_mobility(scoped_text)
    shape = infer_shape(alias, slot, ability, scoped_text, rng)
    width = infer_width(alias, slot, scoped_text, shape)
    radius = infer_radius(alias, slot, scoped_text, effect_numbers(ability))
    speed = infer_speed(alias, slot, scoped_text, shape)
    cast_time = infer_cast_time(alias, slot, scoped_text, shape)
    return {
        "alias": alias,
        "tags": tags,
        "slot": slot,
        "spell_name_en": spell_name_en,
        "raw_text": raw_text,
        "text": scoped_text,
        "target_domain": target_domain,
        "effect_scope": effect_scope,
        "cast_state": cast_state,
        "range": rng,
        "cast_range": cast_range,
        "cd": cd,
        "aoe": aoe,
        "mobility": mobility,
        "shape": shape,
        "width": width,
        "radius": radius,
        "speed": speed,
        "cast_time": cast_time,
        "ability": ability,
    }


def has_damage(text: str) -> bool:
    return bool(
        re.search(r"\b(?:deal|deals|dealing|dealt)\b.{0,80}\bdamage\b", text)
        or "damages " in text
        or re.search(r"\bdamage\b.{0,40}\benem", text)
    )


def is_aoe(ability: dict[str, Any], text: str) -> bool:
    tags = set(ability.get("heuristic_tags") or [])
    return "aoe_or_multitarget" in tags or contains_any(text, AOE_WORDS)


def has_hard_cc(text: str) -> bool:
    scrubbed = (
        text.replace("immobilizes himself", "")
        .replace("roots himself", "")
        .replace("stuns himself", "")
    )
    return contains_terms(scrubbed, HARD_CC_WORDS)


def has_soft_cc(text: str) -> bool:
    return contains_terms(text, SOFT_CC_WORDS)


def has_mobility(text: str) -> bool:
    return contains_terms(text, MOBILITY_WORDS)


def has_sustain(text: str) -> bool:
    if contains_terms(text, ("shield", "shields", "shielding", "shielded")):
        return True
    if contains_terms(text, ("heal", "heals", "healing", "healed")):
        return True
    if contains_terms(
        text,
        ("life steal", "lifesteal", "omnivamp", "regenerate", "regenerates", "regeneration"),
    ):
        return True
    if re.search(r"\brestore(?:s|d|ing)?\b.{0,40}\bhealth\b", text):
        return True
    if re.search(r"\bdrain(?:s|ed|ing)?\b.{0,60}\bhealth\b", text):
        return True
    return False


def has_true_hook_pull_cc(text: str) -> bool:
    scrubbed = text
    for phrase in FALSE_PULL_PHRASES:
        scrubbed = scrubbed.replace(phrase, "")
    patterns = (
        r"\bhook(?:s|ed|ing)?\b.{0,40}\b(first enemy hit|enemy|target|champion)\b",
        r"\bpull(?:s|ed|ing)?\b.{0,40}\b(first enemy hit|enemy|target|champion)\b.{0,30}\b(toward|towards|to)\b",
        r"\bdrag(?:s|ged|ging)?\b.{0,40}\b(enemy|target|champion)\b",
    )
    return any(re.search(pattern, scrubbed) for pattern in patterns)


def is_champion_only_ability(text: str) -> bool:
    return contains_any(text, CHAMPION_ONLY_HINTS) and not contains_any(text, ("minion", "minions", "wave"))


def is_utility_only_ability(text: str) -> bool:
    if not contains_any(text, UTILITY_ONLY_HINTS):
        return False
    if has_damage(text):
        return False
    if contains_any(
        text,
        (
            "detonate",
            "detonates",
            "explodes",
            "deals magic damage",
            "deals physical damage",
            "dealing magic damage",
            "dealing physical damage",
            "damage nearby enemies",
            "damage to any enemies it passes through",
        ),
    ):
        return False
    return True


def infer_shape(alias: str, slot: str, ability: dict[str, Any], text: str, rng: float) -> str:
    override = ability_stat_override(alias, slot, "shape")
    if isinstance(override, str):
        return override
    targeted = (
        contains_any(text, TARGETED_WORDS)
        or bool(re.search(r"\bat an? enemy\b", text))
        or bool(re.search(r"\b(?:the|his|her|a)\s+target\b", text))
    )
    if "on the way out" in text and "on the way back" in text:
        return "line"
    if has_mobility(text):
        if (
            "passes through" in text
            or "swings around" in text
            or "launches herself" in text
            or "launches himself" in text
            or "flies to the location" in text
            or "flies to the" in text
            or "rams into" in text
        ):
            return "dash"
        if targeted and not contains_any(text, ("all enemies", "nearby enemies", "surrounding enemies", "target area")):
            return "dash"
    if "cone" in text:
        return "cone"
    if "bounce" in text or "ricochet" in text:
        return "bounce"
    if targeted and not contains_any(
        text,
        ("nearby enemies", "all enemies", "surrounding enemies", "target area", "zone", "field", "line", "beam", "projectile", "first enemy hit"),
    ):
        return "targeted"
    if (
        "all nearby enemies" in text
        or "all enemies around" in text
        or "nearby enemies" in text
        or "surrounding enemies" in text
        or "target area" in text
        or "in an area" in text
    ):
        return "circle"
    if (
        "zone" in text
        or "field" in text
        or "minefield" in text
        or "maelstrom" in text
        or "worked ground" in text
        or "on the ground" in text
        or "ground at" in text
        or "shockwave" in text
    ) and "danger zone" not in text:
        return "circle"
    if (
        "line" in text
        or "ray" in text
        or "beam" in text
        or "target direction" in text
        or "enemy it encounters" in text
        or "first enemy it encounters" in text
        or "through" in text
    ):
        return "line"
    if "the first enemy hit" in text or "projectile" in text or "missile" in text:
        return "line"
    if rng <= 325:
        return "melee"
    if has_mobility(text):
        return "dash"
    if targeted:
        return "targeted"
    return "targeted"


def infer_width(alias: str, slot: str, text: str, shape: str) -> float:
    override = ability_stat_override(alias, slot, "width")
    if isinstance(override, (int, float)):
        return float(override)
    if shape == "line":
        if contains_terms(text, ("hook", "hooks", "pull", "pulls")):
            return 140.0
        if contains_terms(text, ("root", "roots", "snare", "bind", "binding")):
            return 120.0
        if "ray" in text or "beam" in text:
            return 180.0
        return 100.0
    if shape == "melee":
        return 220.0
    if shape == "dash":
        return 140.0
    return 0.0


def infer_radius(alias: str, slot: str, text: str, eff_nums: list[float]) -> float:
    override = ability_stat_override(alias, slot, "radius")
    if isinstance(override, (int, float)):
        return float(override)
    candidates = [x for x in eff_nums if 100 <= x <= 450]
    if candidates:
        return max(candidates)
    if "nearby enemies" in text or "surrounding enemies" in text or "target area" in text:
        return 300.0
    if ("zone" in text and "danger zone" not in text) or "field" in text or "worked ground" in text or "minefield" in text:
        return 275.0
    if "explodes" in text or "detonates" in text:
        return 240.0
    return 0.0


def infer_speed(alias: str, slot: str, text: str, shape: str) -> float:
    override = ability_stat_override(alias, slot, "speed")
    if isinstance(override, (int, float)):
        return float(override)
    if shape in {"targeted", "melee"}:
        return 2200.0
    if shape == "dash":
        return 1800.0
    if has_true_hook_pull_cc(text):
        return 1600.0
    if "ray" in text or "beam" in text:
        return 2200.0
    if shape == "line":
        return 1400.0
    if shape in {"circle", "bounce"}:
        return 1300.0
    return 1200.0


def infer_cast_time(alias: str, slot: str, text: str, shape: str) -> float:
    override = ability_stat_override(alias, slot, "cast_time")
    if isinstance(override, (int, float)):
        return float(override)
    if "after a delay" in text or "charges up" in text or "takes a moment" in text:
        return 0.55
    if "detonates after" in text:
        return 0.35
    if shape == "melee":
        return 0.05
    if shape == "targeted":
        return 0.1
    return 0.25


def infer_targeting_bonus(text: str, shape: str, rng: float) -> float:
    targeted = contains_any(text, TARGETED_WORDS)
    if shape == "melee":
        return 0.98
    if targeted and rng <= 350:
        return 0.96
    if shape == "targeted":
        return 0.9
    if shape == "dash":
        return 0.8
    if shape == "circle":
        return 0.68
    if shape == "bounce":
        return 0.94
    return 0.56


def infer_cc_type(alias: str, slot: str, text: str, aoe: bool, target_domain: str) -> str:
    override = ability_stat_override(alias, slot, "cc_type")
    if isinstance(override, str):
        return override
    if target_domain == "monster_only":
        return "none"
    if has_true_hook_pull_cc(text):
        return "hook_pull"
    if "dragged along" in text:
        return "hook_pull"
    if re.search(r"\bknock\w*\b.{0,20}\bback\b", text) and "towards" not in text and "toward" not in text:
        return "single_hard"
    if contains_terms(text, ("knock back", "knocks back", "knocked back", "knocking back")) and "towards" not in text and "toward" not in text:
        return "single_hard"
    if contains_terms(text, ("knock back", "knocks back", "knocked back", "knock up", "knocks up", "knocked up")) and "terrain" in text:
        return "conditional_knockback"
    if "into the air" in text and "ricochet" not in text and "high up into the air" not in text:
        return "aoe_hard" if aoe else "single_hard"
    if contains_terms(text, ("root", "roots", "bind", "binding", "snare", "snared", "immobilize", "immobilized")):
        return "root"
    if contains_terms(text, ("silence", "silenced", "polymorph", "grounded")):
        return "utility_cc"
    if has_hard_cc(text):
        return "aoe_hard" if aoe else "single_hard"
    if has_soft_cc(text):
        return "soft_slow"
    return "none"


def cc_base_score(cc_type: str, text: str) -> float:
    if cc_type == "hook_pull":
        return 3.0
    if cc_type == "aoe_hard":
        return 2.8
    if cc_type in {"single_hard", "conditional_knockback"}:
        return 2.5
    if cc_type == "root":
        return 2.0
    if cc_type == "utility_cc":
        return 1.6
    if cc_type == "soft_slow":
        if "by 60%" in text or "by 70%" in text:
            return 1.2
        return 0.8
    return 0.0


def infer_cc_duration(alias: str, slot: str, cc_type: str, text: str) -> float:
    override = ability_stat_override(alias, slot, "cc_duration")
    if isinstance(override, (int, float)):
        return float(override)
    if cc_type == "hook_pull":
        return 1.0
    if cc_type == "aoe_hard":
        return 1.25
    if cc_type == "single_hard":
        return 1.0
    if cc_type == "conditional_knockback":
        return 0.75
    if cc_type == "root":
        if "two enemies" in text or "first two enemies" in text:
            return 1.7
        return 1.3
    if cc_type == "utility_cc":
        return 1.0
    if cc_type == "soft_slow":
        return 1.2
    return 0.0


def infer_expected_targets(alias: str, slot: str, text: str, aoe: bool, shape: str) -> float:
    override = ability_stat_override(alias, slot, "expected_targets")
    if isinstance(override, (int, float)):
        return float(override)
    if "first two enemies" in text or "two enemies" in text:
        return 1.35
    if aoe and contains_terms(text, ("all enemies", "nearby enemies")):
        return 1.9
    if aoe and shape == "circle":
        return 1.6
    if shape == "bounce":
        return 2.3
    if shape == "line" and "all enemies" in text:
        return 1.7
    return 1.0


def infer_entry_followthrough(
    alias: str,
    slot: str,
    text: str,
    cc_type: str,
    mobility: bool,
    rng: float,
    cast_state: str,
) -> float:
    override = ability_stat_override(alias, slot, "entry_followthrough")
    if isinstance(override, (int, float)):
        return float(override)
    if cc_type == "hook_pull":
        return 1.0
    conditional = cast_state in {
        "third_cast",
        "recast",
        "worked_ground",
        "conditional_transform",
        "conditional_trigger",
        "form_branch",
        "charge_up",
        "trap_delay",
        "stack_gated",
        "ally_setup",
        "setup_required",
        "resource_gated",
        "attack_window",
    }
    if mobility and cc_type in {"single_hard", "root", "aoe_hard"}:
        return 0.46 if conditional else 0.72
    if cc_type == "aoe_hard":
        if rng >= 800:
            return 0.12 if conditional else 0.18
        return 0.24 if conditional else 0.42
    if cc_type == "single_hard":
        if rng >= 700:
            return 0.2 if conditional else 0.32
        return 0.24 if conditional else 0.38
    if cc_type == "root":
        return 0.08 if conditional else 0.18
    if mobility:
        return 0.22
    return 0.0


def infer_certainty(alias: str, slot: str, text: str, shape: str, targeting_bonus: float, cast_state: str) -> float:
    override = ability_stat_override(alias, slot, "certainty")
    if isinstance(override, (int, float)):
        return float(override)
    value = 0.55 + 0.35 * targeting_bonus
    if "after a delay" in text or "charges up" in text:
        value -= 0.1
    if cast_state in {
        "third_cast",
        "recast",
        "worked_ground",
        "conditional_transform",
        "conditional_trigger",
        "attack_window",
        "form_branch",
        "charge_up",
        "trap_delay",
        "stack_gated",
        "ally_setup",
        "setup_required",
        "resource_gated",
    }:
        value -= 0.08
    if shape == "bounce":
        value += 0.1
    return clamp01(value)


def infer_pierce_bounce(alias: str, slot: str, text: str, shape: str) -> float:
    override = ability_stat_override(alias, slot, "pierce_bounce")
    if isinstance(override, (int, float)):
        return float(override)
    if shape == "bounce":
        return 0.95
    if "all enemies it cuts through" in text or "subsequent targets" in text:
        return 0.9
    if "pierces" in text or "passes through" in text:
        return 0.65
    return 0.0


def infer_persistence(alias: str, slot: str, text: str) -> float:
    override = ability_stat_override(alias, slot, "persistence")
    if isinstance(override, (int, float)):
        return float(override)
    if "zone" in text or "field" in text or "minefield" in text or "maelstrom" in text or "worked ground" in text:
        return 0.75
    if "every " in text and ("nearby enemies" in text or "surrounding enemies" in text):
        return 0.65
    if "detonates after" in text:
        return 0.35
    return 0.0


def infer_self_commit(tags: set[str], text: str, shape: str, rng: float) -> float:
    override = None
    if shape and isinstance(shape, str):
        override = None
    if shape == "melee":
        return 1.0
    if shape == "dash":
        return 0.9
    if shape == "cone":
        return 0.78 if rng <= 650 else 0.58
    if rng <= 350:
        return 0.85
    if "knocked away" in text:
        return 0.25
    if shape == "circle" and rng >= 700 and "nearby enemies" in text:
        return 0.18
    if "nearby enemies" in text:
        return 0.65
    if "Tank" in tags and "Mage" not in tags and rng <= 500:
        return 0.7
    return 0.1


def infer_wave_reliability(
    alias: str,
    slot: str,
    text: str,
    shape: str,
    rng: float,
    target_domain: str,
    cast_state: str,
) -> float:
    override = ability_stat_override(alias, slot, "wave_reliability")
    if isinstance(override, (int, float)):
        return float(override)
    if shape == "bounce":
        return 0.96
    value = 0.58
    if shape == "circle":
        value += 0.08
    if rng >= 800:
        value += 0.05
    if "the first enemy hit" in text:
        value -= 0.12
    if target_domain == "champion_only":
        value -= 0.25
    if cast_state == "recast":
        value -= 0.22
    elif cast_state == "third_cast":
        value -= 0.14
    elif cast_state == "conditional_transform":
        value -= 0.28
    elif cast_state == "conditional_trigger":
        value -= 0.2
    elif cast_state == "attack_window":
        value -= 0.18
    elif cast_state == "form_branch":
        value -= 0.22
    elif cast_state == "trap_delay":
        value -= 0.18
    elif cast_state == "stack_gated":
        value -= 0.16
    elif cast_state == "ally_setup":
        value -= 0.26
    elif cast_state == "setup_required":
        value -= 0.2
    elif cast_state == "resource_gated":
        value -= 0.3
    return clamp01(value)


def infer_wave_prep_bonus(
    text: str,
    shape: str,
    cast_range: float,
    cast_state: str,
    persistence: float,
    self_commit: float,
    slot: str,
) -> float:
    if slot == "R":
        return 0.0
    if cast_state in {"trap_delay", "resource_gated", "third_cast", "recast"}:
        return 0.0
    if cast_range < 700 or self_commit > 0.35:
        return 0.0

    value = 0.0
    preplace_text = (
        "to an area" in text
        or "to a location" in text
        or "target area" in text
        or "zone" in text
        or "field" in text
        or "minefield" in text
        or "worked ground" in text
        or "after a delay" in text
    )
    if preplace_text and shape == "circle":
        value += 0.55
    if persistence >= 0.45:
        value += 0.2
    if "detonate" in text or "detonates" in text:
        value += 0.1
    if "charges up" in text or "begin charging" in text or "begins charging" in text:
        value += 0.08
    return clamp01(value)


def infer_build_profile(alias: str, tags: set[str]) -> str:
    if alias in CHAMPION_BUILD_ARCHETYPE:
        return CHAMPION_BUILD_ARCHETYPE[alias]
    if "Mage" in tags and "Support" in tags:
        return "ap_burst"
    if "Mage" in tags:
        return "ap_burst"
    if "Marksman" in tags and "Mage" in tags:
        return "hybrid_caster"
    if "Marksman" in tags:
        return "ad_crit"
    if "Fighter" in tags or "Assassin" in tags:
        return "ad_fighter"
    if "Tank" in tags:
        return "tank"
    return "ap_support"


def build_profile_record(alias: str, tags: set[str]) -> dict[str, Any]:
    profile = infer_build_profile(alias, tags)
    stats = BUILD_PROFILE_STATS.get(profile, {"items": (), "ap": 0.0, "bonus_ad": 0.0})
    return {
        "profile": profile,
        "items": tuple(str(x) for x in (stats.get("items") or ())),
        "ap": float(stats.get("ap") or 0.0),
        "bonus_ad": float(stats.get("bonus_ad") or 0.0),
    }


def build_stat_budget(alias: str, tags: set[str]) -> tuple[float, float, float]:
    record = build_profile_record(alias, tags)
    ap = float(record["ap"])
    bonus_ad = float(record["bonus_ad"])
    ap_norm = norm(ap, 0.0, 320.0) if ap > 0.0 else 0.0
    ad_norm = norm(bonus_ad, 0.0, 180.0) if bonus_ad > 0.0 else 0.0
    return ap, bonus_ad, max(ap_norm, ad_norm)


def skill_feature_row(alias: str, slot: str) -> dict[str, Any]:
    return skill_feature_map().get((alias, slot), {})


def infer_scaling_mix(
    alias: str,
    slot: str,
    text: str,
    tags: set[str],
) -> tuple[float, float]:
    override = ability_stat_override(alias, slot, "scaling_stat")
    if override == "ap":
        return 1.0, 0.0
    if override == "bonus_ad":
        return 0.0, 1.0
    if override == "mixed":
        return 0.55, 0.55
    if override == "none":
        return 0.0, 0.0

    physical_ratio, magic_ratio, _true_ratio = empirical_damage_mix_map().get(alias, (0.0, 0.0, 0.0))
    ap_weight = 0.0
    ad_weight = 0.0
    if "Mage" in tags:
        ap_weight += 0.65
    if "Support" in tags and "Mage" in tags:
        ap_weight += 0.1
    if "Marksman" in tags:
        ad_weight += 0.42
    if "Fighter" in tags or "Assassin" in tags:
        ad_weight += 0.48
    if "magic damage" in text:
        ap_weight += 0.3
    if "physical damage" in text:
        ad_weight += 0.3
    if contains_any(text, AUTO_ATTACK_WORDS):
        ad_weight += 0.12
    ap_weight += 0.35 * magic_ratio
    ad_weight += 0.35 * physical_ratio
    total = ap_weight + ad_weight
    if total <= 0.0:
        return 0.0, 0.0
    return clamp01(ap_weight / total), clamp01(ad_weight / total)


def base_damage_component(alias: str, slot: str, ability: dict[str, Any]) -> float:
    feature_row = skill_feature_row(alias, slot)
    series = feature_row.get("base_damage_by_rank")
    if isinstance(series, list) and series:
        nums = [float(x) for x in series if isinstance(x, (int, float))]
        if nums:
            return clamp01(norm(max(nums), 60.0, 320.0))
    eff_nums = [x for x in effect_numbers(ability) if 30.0 <= x <= 350.0]
    eff_peak = max(eff_nums) if eff_nums else 0.0
    return clamp01(norm(eff_peak, 60.0, 280.0))


def scaling_proxy(
    alias: str,
    slot: str,
    text: str,
    tags: set[str],
    shape: str,
    aoe: bool,
    cast_state: str,
) -> float:
    override = ability_stat_override(alias, slot, "scaling_proxy")
    if isinstance(override, (int, float)):
        return float(override)
    feature_row = skill_feature_row(alias, slot)
    coeff_rows = feature_row.get("scaling_coeff_by_rank")
    if isinstance(coeff_rows, list) and coeff_rows:
        ap, bonus_ad, _stat_norm = build_stat_budget(alias, tags)
        ap_norm = norm(ap, 0.0, 320.0) if ap > 0.0 else 0.0
        ad_norm = norm(bonus_ad, 0.0, 180.0) if bonus_ad > 0.0 else 0.0
        total = 0.0
        for row in coeff_rows:
            if not isinstance(row, dict):
                continue
            stat = str(row.get("stat") or "")
            values = row.get("values") or []
            coeff = max((float(v) for v in values if isinstance(v, (int, float))), default=0.0)
            if stat == "ap":
                total += coeff * ap_norm
            elif stat in {"bonus_ad", "total_ad"}:
                total += coeff * ad_norm
            elif stat in {"max_hp", "armor", "mr"}:
                total += coeff * 0.35
        if total > 0.0:
            value = 0.15 + 0.6 * clamp01(total)
            if aoe:
                value += 0.08
            if shape in {"line", "circle", "cone"}:
                value += 0.05
            if "per second" in text or "for up to" in text or "on the way back" in text:
                value += 0.08
            if contains_any(text, AUTO_ATTACK_WORDS):
                value += 0.06
            if cast_state == "recast":
                value -= 0.08
            elif cast_state == "third_cast":
                value -= 0.04
            elif cast_state == "conditional_transform":
                value -= 0.1
            elif cast_state == "conditional_trigger":
                value -= 0.06
            elif cast_state == "attack_window":
                value -= 0.08
            elif cast_state == "form_branch":
                value -= 0.06
            elif cast_state == "resource_gated":
                value -= 0.12
            return clamp01(value)
    ap, bonus_ad, _stat_norm = build_stat_budget(alias, tags)
    ap_weight, ad_weight = infer_scaling_mix(alias, slot, text, tags)
    ap_norm = norm(ap, 0.0, 320.0) if ap > 0.0 else 0.0
    ad_norm = norm(bonus_ad, 0.0, 180.0) if bonus_ad > 0.0 else 0.0
    stat_signal = ap_weight * ap_norm + ad_weight * ad_norm
    value = 0.16 + 0.58 * stat_signal
    if aoe:
        value += 0.08
    if shape in {"line", "circle", "cone"}:
        value += 0.05
    if "per second" in text or "for up to" in text or "on the way back" in text:
        value += 0.08
    if contains_any(text, AUTO_ATTACK_WORDS):
        value += 0.06
    if cast_state == "recast":
        value -= 0.08
    elif cast_state == "third_cast":
        value -= 0.04
    elif cast_state == "conditional_transform":
        value -= 0.1
    elif cast_state == "conditional_trigger":
        value -= 0.06
    elif cast_state == "attack_window":
        value -= 0.08
    elif cast_state == "form_branch":
        value -= 0.06
    elif cast_state == "resource_gated":
        value -= 0.12
    return clamp01(value)


def infer_cast_range(alias: str, slot: str, rng: float) -> float:
    override = ability_stat_override(alias, slot, "cast_range")
    if isinstance(override, (int, float)):
        return float(override)
    return rng


def damage_proxy(
    alias: str,
    slot: str,
    ability: dict[str, Any],
    text: str,
    tags: set[str],
    rng: float,
    aoe: bool,
    shape: str,
    cast_state: str,
) -> float:
    override = ability_stat_override(alias, slot, "damage_proxy")
    if isinstance(override, (int, float)):
        return float(override)
    if not has_damage(text):
        if contains_any(text, AUTO_ATTACK_WORDS):
            return 0.38
        return 0.0
    base_damage = base_damage_component(alias, slot, ability)
    scaling = scaling_proxy(alias, slot, text, tags, shape, aoe, cast_state)
    value = 0.16 + 0.36 * base_damage + 0.26 * scaling
    if aoe:
        value += 0.12
    if "per second" in text or "every " in text:
        value += 0.12
    if cooldown_min(ability) <= 8:
        value += 0.12
    if rng >= 800:
        value += 0.08
    if ability.get("slot") == "R":
        value += 0.1
    if "Tank" in tags and "Mage" not in tags and "Marksman" not in tags:
        value -= 0.08
    if cast_state == "recast":
        value -= 0.12
    elif cast_state == "third_cast":
        value -= 0.08
    elif cast_state == "conditional_transform":
        value -= 0.18
    elif cast_state == "conditional_trigger":
        value -= 0.14
    elif cast_state == "attack_window":
        value -= 0.16
    elif cast_state == "form_branch":
        value -= 0.18
    elif cast_state == "charge_up":
        value -= 0.1
    elif cast_state == "trap_delay":
        value -= 0.18
    elif cast_state == "stack_gated":
        value -= 0.12
    elif cast_state == "ally_setup":
        value -= 0.22
    elif cast_state == "setup_required":
        value -= 0.16
    elif cast_state == "resource_gated":
        value -= 0.26
    return clamp01(value)


def growth_proxy(alias: str, slot: str, text: str, tags: set[str], aoe: bool) -> float:
    override = ability_stat_override(alias, slot, "growth_proxy")
    if isinstance(override, (int, float)):
        return float(override)
    value = 0.18
    if "Attack Speed" in text or "attack speed" in text:
        value += 0.18
    if "missing health" in text or "max Health" in text:
        value += 0.12
    if contains_any(text, AUTO_ATTACK_WORDS):
        value += 0.18
    if aoe and "Mage" in tags:
        value += 0.18
    if "Marksman" in tags:
        value += 0.18
    if "Tank" in tags and "Mage" not in tags:
        value -= 0.08
    return clamp01(value)


def engage_skill_score(champion: dict[str, Any], ability: dict[str, Any]) -> dict[str, Any]:
    ctx = ability_context(champion, ability)
    alias = str(ctx["alias"])
    slot = str(ctx["slot"])
    text = str(ctx["text"])
    rng = float(ctx["range"])
    cd = float(ctx["cd"])
    aoe = bool(ctx["aoe"])
    mobility = bool(ctx["mobility"])
    shape = str(ctx["shape"])
    width = float(ctx["width"])
    speed = float(ctx["speed"])
    cast_time = float(ctx["cast_time"])
    target_domain = str(ctx["target_domain"])
    effect_scope = str(ctx["effect_scope"])
    cast_state = str(ctx["cast_state"])

    targeting_bonus = infer_targeting_bonus(text, shape, rng)
    targeting_override = ability_stat_override(alias, slot, "targeting_bonus")
    if isinstance(targeting_override, (int, float)):
        targeting_bonus = float(targeting_override)
    certainty = infer_certainty(alias, slot, text, shape, targeting_bonus, cast_state)
    cc_type = infer_cc_type(alias, slot, text, aoe, target_domain)
    cc_base = cc_base_score(cc_type, text)
    cc_duration = infer_cc_duration(alias, slot, cc_type, text)
    expected_targets = infer_expected_targets(alias, slot, text, aoe, shape)
    follow = infer_entry_followthrough(alias, slot, text, cc_type, mobility, rng, cast_state)
    condition_penalty = ability_stat_override(alias, slot, "condition_penalty")
    penalty = float(condition_penalty) if isinstance(condition_penalty, (int, float)) else 0.0
    engage_gate = infer_engage_gate(cc_type, mobility, target_domain, cast_state, text)

    range_eff = rng
    if mobility and rng <= 350:
        range_eff = 450.0
    if cc_type == "hook_pull":
        range_eff += 120.0

    r_score = clamp01(math.log1p(max(range_eff, 0.0) / 200.0) / math.log1p(1800.0 / 200.0))
    dur = clamp01(max(0.6, min(1.2, cc_duration / 1.5)))
    cc_score = clamp01((cc_base / 3.0) * dur * certainty)
    target_score = clamp01(max(0.0, min(1.0, 0.45 + 0.18 * (expected_targets - 1.0))))
    cd_score = 1.0 - clamp01((cd - 4.0) / 21.0)
    hit_score = clamp01(
        0.35 * norm(width, 60.0, 260.0)
        + 0.25 * norm(speed, 900.0, 2200.0)
        + 0.15 * (1.0 - norm(cast_time, 0.0, 0.75))
        + 0.25 * targeting_bonus
    )

    value = 3.0 * (
        ENGAGE_WEIGHTS["range"] * r_score
        + ENGAGE_WEIGHTS["cc"] * cc_score
        + ENGAGE_WEIGHTS["follow"] * follow
        + ENGAGE_WEIGHTS["targets"] * target_score
        + ENGAGE_WEIGHTS["cd"] * cd_score
        + ENGAGE_WEIGHTS["hit"] * hit_score
    )
    if cast_state == "third_cast":
        penalty += 0.22
    elif cast_state == "recast":
        penalty += 0.18
    elif cast_state == "worked_ground":
        penalty += 0.28
    elif cast_state == "conditional_transform":
        penalty += 0.25
    elif cast_state == "conditional_trigger":
        penalty += 0.2
    elif cast_state == "attack_window":
        penalty += 0.16
    elif cast_state == "stack_gated":
        penalty += 0.18
    elif cast_state == "ally_setup":
        penalty += 0.28
    elif cast_state == "setup_required":
        penalty += 0.18
    elif cast_state == "resource_gated":
        penalty += 0.32
    if effect_scope == "active_plus_passive":
        penalty += 0.05

    gate_scale = {
        "forced_displacement": 1.0,
        "hard_cc": 1.0 if follow >= 0.45 else 0.78 if follow >= 0.25 else 0.58,
        "conditional_hard_cc": 0.7 if follow >= 0.35 else 0.54 if follow >= 0.18 else 0.38,
        "soft_followup": 0.38,
        "soft_cc_only": 0.26,
        "mobility_only": 0.16,
        "none": 0.08,
        "invalid_monster_only": 0.0,
    }.get(engage_gate, 0.08)

    value *= max(0.0, 1.0 - penalty)
    value *= gate_scale

    return {
        "slot": slot,
        "spell_name_en": str(ctx["spell_name_en"]),
        "shape": shape,
        "range": rng,
        "score": clamp_score(value),
        "hard_cc": cc_base >= 2.0,
        "soft_cc": 0.0 < cc_base < 2.0,
        "follow": follow,
        "cc_component": cc_score,
        "expected_targets": expected_targets,
        "hit": hit_score,
        "certainty": certainty,
        "mobility": mobility,
        "target_domain": target_domain,
        "effect_scope": effect_scope,
        "cast_state": cast_state,
        "engage_gate": engage_gate,
        "cc_type": cc_type,
        "cc_duration": cc_duration,
        "targeting_bonus": targeting_bonus,
        "width": width,
        "speed": speed,
        "cast_time": cast_time,
        "condition_penalty": penalty,
        "analysis_text": text,
        "note": f"{slot}:{cc_type or 'none'}:{engage_gate}:{clamp_score(value)}",
    }


def wave_geo(shape: str, length: float, width: float, radius: float, angle: float) -> float:
    if shape == "line":
        return clamp01(math.sqrt(max(length * width, 0.0) / (1200.0 * 140.0)))
    if shape == "circle":
        return clamp01(radius / 275.0)
    if shape == "cone":
        return clamp01(math.sqrt(max(radius * angle, 0.0) / (650.0 * 70.0)))
    if shape == "bounce":
        return 0.38
    if shape == "melee":
        return clamp01(math.sqrt(max(length * width, 0.0) / (1200.0 * 140.0)) * 0.78)
    return 0.14


def wave_skill_score(champion: dict[str, Any], ability: dict[str, Any]) -> dict[str, Any]:
    ctx = ability_context(champion, ability)
    alias = str(ctx["alias"])
    tags = set(ctx["tags"])
    slot = str(ctx["slot"])
    text = str(ctx["text"])
    rng = float(ctx["range"])
    cast_range = float(ctx["cast_range"])
    cd = float(ctx["cd"])
    aoe = bool(ctx["aoe"])
    shape = str(ctx["shape"])
    width = float(ctx["width"])
    radius = float(ctx["radius"])
    cast_time = float(ctx["cast_time"])
    target_domain = str(ctx["target_domain"])
    effect_scope = str(ctx["effect_scope"])
    cast_state = str(ctx["cast_state"])
    angle = 55.0 if shape == "cone" else 0.0
    length = cast_range if shape in {"line", "melee"} else rng
    pierce_bounce = infer_pierce_bounce(alias, slot, text, shape)
    persistence = infer_persistence(alias, slot, text)
    self_commit = infer_self_commit(tags, text, shape, cast_range)
    reliability = infer_wave_reliability(alias, slot, text, shape, cast_range, target_domain, cast_state)
    prep_bonus = infer_wave_prep_bonus(text, shape, cast_range, cast_state, persistence, self_commit, slot)
    damage = damage_proxy(alias, slot, ability, text, tags, cast_range, aoe, shape, cast_state)
    growth = growth_proxy(alias, slot, text, tags, aoe)
    cc_type = infer_cc_type(alias, slot, text, aoe, target_domain)
    champion_only = is_champion_only_ability(text)
    utility_only = is_utility_only_ability(text)

    geo = wave_geo(shape, max(length, 300.0), max(width, 90.0), max(radius, 0.0), angle)
    coverage = clamp01(0.6 * geo + 0.2 * pierce_bounce + 0.2 * persistence)
    cd_score = 1.0 - clamp01((cd - 4.0) / 10.0)
    safety = clamp01(0.75 * norm(cast_range, 125.0, 1300.0) + 0.25 * (1.0 - self_commit))

    value = 3.0 * (
        WAVE_WEIGHTS["coverage"] * coverage
        + WAVE_WEIGHTS["damage"] * damage
        + WAVE_WEIGHTS["cd"] * cd_score
        + WAVE_WEIGHTS["safety"] * safety
        + WAVE_WEIGHTS["growth"] * growth
        + WAVE_WEIGHTS["reliability"] * reliability
        + WAVE_WEIGHTS["prep"] * prep_bonus
    )

    fire_and_forget = (
        shape in {"line", "circle", "bounce"}
        and has_damage(text)
        and cast_range >= 900
        and self_commit <= 0.25
        and cast_state == "always"
        and slot != "R"
        and cast_time <= 0.3
        and "per second" not in text
        and "channel" not in text
        and "for up to" not in text
    )
    if fire_and_forget:
        value *= 1.12 + 0.08 * norm(cast_range, 900.0, 1300.0)

    sustained_close_clear = (
        shape in {"cone", "melee"}
        and cast_range <= 650
        and ("per second" in text or "for 3 seconds" in text or "for 2 seconds" in text or "for a few seconds" in text)
    )
    if sustained_close_clear:
        value *= 0.76

    supports_wave = target_domain != "monster_only" and (
        pierce_bounce > 0.0
        or persistence > 0.0
        or contains_any(text, MINION_HINT_WORDS)
        or (
            shape == "line"
            and damage > 0.05
            and "the first enemy hit" not in text
            and "first enemy hit" not in text
            and "enemy it encounters" not in text
        )
        or (shape == "cone" and damage > 0.05)
        or (
            aoe
            and damage > 0.05
            and not champion_only
            and not utility_only
            and ("nearby enemies" in text or "surrounding enemies" in text or "all enemies" in text)
        )
        or (
            shape == "circle"
            and damage > 0.05
            and cast_range >= 650
            and self_commit <= 0.35
            and slot != "R"
        )
    )
    if not has_damage(text) and not contains_any(text, AUTO_ATTACK_WORDS):
        value *= 0.25
    if champion_only:
        value *= 0.3
    if utility_only:
        value *= 0.2
    if cast_state == "recast":
        value *= 0.5
    elif cast_state == "third_cast":
        value *= 0.72
    elif cast_state == "conditional_transform":
        value *= 0.45
    elif cast_state == "conditional_trigger":
        value *= 0.62
    elif cast_state == "attack_window":
        value *= 0.58
    elif cast_state == "form_branch":
        value *= 0.64
    elif cast_state == "charge_up":
        value *= 0.82
    elif cast_state == "trap_delay":
        value *= 0.46
    elif cast_state == "worked_ground":
        value *= 0.82
    elif cast_state == "stack_gated":
        value *= 0.68
    elif cast_state == "ally_setup":
        value *= 0.45
    elif cast_state == "setup_required":
        value *= 0.58
    elif cast_state == "resource_gated":
        value *= 0.32
    if not supports_wave:
        if contains_any(text, AUTO_ATTACK_WORDS):
            value *= 0.35
        else:
            value *= 0.12
    if slot == "R" and cast_state in {"attack_window", "conditional_transform", "resource_gated"}:
        value *= 0.42
    if shape == "targeted" and not contains_any(text, AUTO_ATTACK_WORDS):
        value *= 0.2
    if cc_type in {"single_hard", "root"} and shape in {"line", "targeted"} and not contains_any(text, MINION_HINT_WORDS):
        value *= 0.25
    if "first enemy hit" in text and ("surrounding enemies" in text or "nearby enemies" in text):
        value *= 0.45
    if has_hard_cc(text) and shape == "circle" and persistence <= 0.0 and not contains_any(text, ("minion", "minions", "wave")):
        value *= 0.42
    if "Tank" in tags and "Mage" not in tags and "Marksman" not in tags and slot == "R":
        value *= 0.6
    if "Tank" in tags and "Mage" not in tags and "Marksman" not in tags and not supports_wave:
        value *= 0.75
    if "Tank" in tags and "Mage" not in tags and "Marksman" not in tags and self_commit >= 0.75:
        value *= 0.75
    if shape == "dash" and self_commit >= 0.8 and persistence <= 0.0 and pierce_bounce <= 0.0:
        value *= 0.62
    if (
        shape == "dash"
        and "passes through" in text
        and "nearby enemies" not in text
        and "surrounding enemies" not in text
        and "deals the damage again" not in text
    ):
        value *= 0.4
    if (
        self_commit >= 0.75
        and shape == "circle"
        and persistence <= 0.0
        and pierce_bounce <= 0.0
        and not contains_any(text, MINION_HINT_WORDS)
        and tags & {"Assassin", "Fighter"}
    ):
        value *= 0.72
    if (
        "Tank" in tags
        and "Mage" not in tags
        and "Marksman" not in tags
        and shape == "circle"
        and cast_range <= 500
        and self_commit >= 0.6
        and persistence <= 0.0
        and slot != "R"
    ):
        value *= 0.55
    if "two nearby enemies" in text or "single target" in text:
        value *= 0.55
    if slot == "R" and cd >= 20 and persistence <= 0.0 and not contains_any(text, MINION_HINT_WORDS):
        value *= 0.38
    if slot == "R" and cd >= 20 and persistence < 0.5 and cast_range <= 900 and "per second" not in text:
        value *= 0.55

    return {
        "slot": slot,
        "spell_name_en": str(ctx["spell_name_en"]),
        "shape": shape,
        "score": clamp_score(value),
        "coverage": coverage,
        "damage_component": damage,
        "safety": safety,
        "reliability": reliability,
        "prep_bonus": prep_bonus,
        "target_domain": target_domain,
        "effect_scope": effect_scope,
        "cast_state": cast_state,
        "pierce_bounce": pierce_bounce,
        "persistence": persistence,
        "self_commit": self_commit,
        "supports_wave": supports_wave,
        "cast_range": cast_range,
        "width": width,
        "radius": radius,
        "analysis_text": text,
        "note": f"{slot}:{shape}:{cast_state}:{clamp_score(value)}",
    }


def basic_attack_floor(champion: dict[str, Any]) -> float:
    alias = str(champion.get("alias") or "")
    tags = set(champion.get("tags") or [])
    ability_text = " ".join(text_of(ability) for ability in (champion.get("abilities") or []))
    ptext = passive_text(champion)
    full_text = f"{ability_text} {ptext}"
    stats = champion.get("stats") or {}
    attack_range = float(stats.get("attackrange") or 0.0) if isinstance(stats, dict) else 0.0
    empirical_dpm_norm, empirical_damage_norm = empirical_floor_map().get(alias, (0.0, 0.0))

    auto_attack_profile = (
        "Marksman" in tags
        or contains_any(full_text, AUTO_ATTACK_WORDS)
        or (
            tags & {"Fighter", "Assassin"}
            and (empirical_dpm_norm >= 0.45 or empirical_damage_norm >= 0.38)
        )
    )
    if not auto_attack_profile:
        return 0.0

    if "Marksman" in tags:
        aa_dps = 0.68
    elif "Fighter" in tags:
        aa_dps = 0.42
    elif "Mage" in tags:
        aa_dps = 0.22
    elif "Tank" in tags:
        aa_dps = 0.12
    else:
        aa_dps = 0.26

    if contains_any(full_text, AUTO_ATTACK_WORDS):
        aa_dps += 0.1

    if attack_range > 0:
        aa_range = norm(attack_range, 125.0, 650.0)
    elif "Marksman" in tags:
        aa_range = 0.72
    elif "Mage" in tags:
        aa_range = 0.52
    else:
        aa_range = 0.26

    reset_onhit = 0.0
    if contains_any(full_text, ATTACK_RESET_WORDS):
        reset_onhit = 0.55
    if "attack speed" in full_text:
        reset_onhit += 0.15
    if "max health" in full_text or "every third" in full_text:
        reset_onhit += 0.1
    reset_onhit = clamp01(reset_onhit)

    heuristic_floor = 0.95 * clamp01(0.5 * clamp01(aa_dps) + 0.3 * aa_range + 0.2 * reset_onhit)
    empirical_floor = 0.0
    if empirical_dpm_norm > 0.0 or empirical_damage_norm > 0.0:
        empirical_floor = 1.05 * clamp01(0.62 * empirical_dpm_norm + 0.38 * empirical_damage_norm)
        if "Tank" in tags and "Marksman" not in tags and "Fighter" not in tags and "Assassin" not in tags:
            empirical_floor *= 0.42
        elif "Mage" in tags and "Marksman" not in tags:
            empirical_floor *= 0.55
        elif "Fighter" in tags or "Assassin" in tags:
            empirical_floor *= 1.02
        elif "Marksman" in tags:
            empirical_floor *= 0.96
    if "Marksman" not in tags and reset_onhit <= 0.0:
        heuristic_floor *= 0.78
    floor = max(heuristic_floor, empirical_floor)
    return clamp_score(floor)


def legacy_aux_scores(champion: dict[str, Any]) -> tuple[float, float, float, float, dict[str, list[str]]]:
    tags = set(champion.get("tags") or [])
    ability_rows = champion.get("abilities") or []

    cc = 0.0
    damage = 0.0
    poke = 0.0
    sustain = 0.0
    frontline = 0.0
    evidence: dict[str, list[str]] = {col: [] for col in SCORE_COLUMNS}

    for ability in ability_rows:
        slot = str(ability.get("slot") or "?")
        text = text_of(ability)
        rng = infer_range(str(champion.get("alias") or ""), slot, ability)
        cd = cooldown_min(ability)
        dmg = has_damage(text)
        aoe = is_aoe(ability, text)
        hard = has_hard_cc(text)
        soft = has_soft_cc(text)

        if dmg:
            spell_damage = 0.45
            if cd <= 7:
                spell_damage += 0.25
            if aoe:
                spell_damage += 0.2
            if rng >= 800:
                spell_damage += 0.15
            if slot == "R":
                spell_damage += 0.25
            damage += min(0.9, spell_damage)
            evidence["damage_score"].append(slot)

        if hard:
            cc += 0.85 if slot != "R" else 0.75
            if rng >= 650:
                cc += 0.15
            if cd <= 14:
                cc += 0.1
            if rng >= 900 and contains_terms(text, ("pull", "pulls", "hook", "hooks", "knock back", "knocks back")):
                cc += 0.2
            evidence["cc_score"].append(f"{slot}:hard")
        elif soft:
            cc += 0.35 if slot != "R" else 0.3
            if rng >= 700:
                cc += 0.1
            evidence["cc_score"].append(f"{slot}:soft")

        if dmg and rng >= 850:
            spell_poke = 0.35
            if rng >= 1000:
                spell_poke += 0.25
            if cd <= 10:
                spell_poke += 0.2
            if contains_any(text, POKE_WORDS):
                spell_poke += 0.15
            poke += min(0.9, spell_poke)
            evidence["poke_score"].append(slot)

        if has_sustain(text):
            sustain += 0.55
            if contains_terms(text, ("shield", "shields", "shielded", "shielding")):
                sustain += 0.2
            if contains_terms(text, ("heal", "heals", "healing", "restore", "restores", "drain", "drains", "draining")):
                sustain += 0.2
            evidence["sustain_score"].append(slot)

        if contains_any(text, FRONTLINE_WORDS):
            frontline += 0.25
            evidence["frontline_score"].append(slot)
        if hard and (slot == "R" or rng >= 550):
            frontline += 0.18
            evidence["frontline_score"].append(f"{slot}:peel")

    ptext = passive_text(champion)
    if has_sustain(ptext):
        sustain += 0.35
        evidence["sustain_score"].append("P")
    if contains_any(ptext, FRONTLINE_WORDS):
        frontline += 0.35
        evidence["frontline_score"].append("P")

    if "Marksman" in tags:
        damage += 0.75
        poke += 0.15
    if "Mage" in tags:
        damage += 0.1
        poke += 0.15
    if "Tank" in tags:
        frontline += 1.0
    if "Fighter" in tags:
        frontline += 0.45
        damage += 0.15
    if "Support" in tags and sustain > 0:
        sustain += 0.15
        if sustain >= 1.2:
            damage -= 0.35

    return cc, damage, poke, sustain, frontline, evidence


def chain_bonus(engage_skills: list[dict[str, Any]]) -> float:
    hard_count = sum(1 for skill in engage_skills if skill["hard_cc"])
    soft_count = sum(1 for skill in engage_skills if skill["soft_cc"])
    best_follow = max((float(skill["follow"]) for skill in engage_skills), default=0.0)
    if best_follow >= 0.8 and hard_count >= 2:
        return 1.0
    if best_follow >= 0.55 and (hard_count >= 2 or (hard_count >= 1 and soft_count >= 1)):
        return 0.5
    return 0.0


def build_skill_debug_rows(
    champion: dict[str, Any],
    engage_skills: list[dict[str, Any]],
    wave_skills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    engage_by_slot = {str(skill["slot"]): skill for skill in engage_skills}
    wave_by_slot = {str(skill["slot"]): skill for skill in wave_skills}
    top_engage_slots = {str(skill["slot"]) for skill in engage_skills[:2]}
    top_wave_slots = {str(skill["slot"]) for skill in wave_skills[:3]}
    rows: list[dict[str, Any]] = []
    for ability in champion.get("abilities") or []:
        slot = str(ability.get("slot") or "?")
        engage = engage_by_slot.get(slot, {})
        wave = wave_by_slot.get(slot, {})
        analysis_text = str(engage.get("analysis_text") or wave.get("analysis_text") or "")
        rows.append(
            {
                "champion_id": int(champion["champion_id"]),
                "champion_alias": champion.get("alias", ""),
                "champion_name_en": champion.get("name_en", ""),
                "champion_name_zh": champion.get("name_zh", ""),
                "spell_slot": slot,
                "spell_name_en": engage.get("spell_name_en") or wave.get("spell_name_en") or ability.get("name_en", ""),
                "target_domain": engage.get("target_domain") or wave.get("target_domain") or "",
                "effect_scope": engage.get("effect_scope") or wave.get("effect_scope") or "",
                "cast_state": engage.get("cast_state") or wave.get("cast_state") or "",
                "engage_gate": engage.get("engage_gate") or "",
                "shape": engage.get("shape") or wave.get("shape") or "",
                "range": engage.get("range") or "",
                "cast_range": wave.get("cast_range") or "",
                "width": engage.get("width") or wave.get("width") or "",
                "radius": wave.get("radius") or "",
                "speed": engage.get("speed") or "",
                "cast_time": engage.get("cast_time") or "",
                "cc_type": engage.get("cc_type") or "",
                "cc_duration": engage.get("cc_duration") or "",
                "expected_targets": engage.get("expected_targets") or "",
                "entry_followthrough": engage.get("follow") or "",
                "certainty": engage.get("certainty") or "",
                "targeting_bonus": engage.get("targeting_bonus") or "",
                "condition_penalty": engage.get("condition_penalty") or "",
                "coverage": wave.get("coverage") or "",
                "damage_component": wave.get("damage_component") or "",
                "safety": wave.get("safety") or "",
                "reliability": wave.get("reliability") or "",
                "prep_bonus": wave.get("prep_bonus") or "",
                "pierce_bounce": wave.get("pierce_bounce") or "",
                "persistence": wave.get("persistence") or "",
                "self_commit": wave.get("self_commit") or "",
                "supports_wave": wave.get("supports_wave") if "supports_wave" in wave else "",
                "engage_skill_score": engage.get("score") or "",
                "wave_skill_score": wave.get("score") or "",
                "is_engage_top2": slot in top_engage_slots,
                "is_wave_top3": slot in top_wave_slots,
                "semantic_formula_version": FORMULA_VERSION,
                "analysis_text": analysis_text,
            }
        )
    return rows


def score_champion(
    champion: dict[str, Any],
    *,
    include_skill_debug: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], list[dict[str, Any]]]:
    build_record = build_profile_record(
        str(champion.get("alias") or ""),
        set(champion.get("tags") or []),
    )
    engage_skills = [engage_skill_score(champion, ability) for ability in (champion.get("abilities") or [])]
    wave_skills = [wave_skill_score(champion, ability) for ability in (champion.get("abilities") or [])]
    engage_skills.sort(key=lambda row: float(row["score"]), reverse=True)
    wave_skills.sort(key=lambda row: float(row["score"]), reverse=True)

    top_engage = float(engage_skills[0]["score"]) if engage_skills else 0.0
    second_engage = float(engage_skills[1]["score"]) if len(engage_skills) > 1 else 0.0
    engage_total = clamp_score(
        0.72 * top_engage
        + 0.18 * second_engage
        + 0.10 * 3.0 * chain_bonus(engage_skills)
    )

    top_wave = float(wave_skills[0]["score"]) if wave_skills else 0.0
    second_wave = float(wave_skills[1]["score"]) if len(wave_skills) > 1 else 0.0
    third_wave = float(wave_skills[2]["score"]) if len(wave_skills) > 2 else 0.0
    st_floor = float(basic_attack_floor(champion))
    wave_total = clamp_score(
        max(
            WAVE_TOP3_WEIGHTS[0] * top_wave
            + WAVE_TOP3_WEIGHTS[1] * second_wave
            + WAVE_TOP3_WEIGHTS[2] * third_wave,
            st_floor,
        )
    )

    cc, damage, poke, sustain, frontline, evidence = legacy_aux_scores(champion)
    evidence["engage_score"] = [skill["note"] for skill in engage_skills[:2]]
    evidence["wave_clear_score"] = [skill["note"] for skill in wave_skills[:3]]

    row = {
        "champion_id": int(champion["champion_id"]),
        "champion_alias": champion.get("alias", ""),
        "champion_name_en": champion.get("name_en", ""),
        "champion_name_zh": champion.get("name_zh", ""),
        "tags": "|".join(champion.get("tags") or []),
        "build_profile": build_record["profile"],
        "build_items": " + ".join(build_record["items"]),
        "build_ap": float(build_record["ap"]),
        "build_bonus_ad": float(build_record["bonus_ad"]),
        "wave_clear_score": wave_total,
        "cc_score": clamp_score(cc),
        "engage_score": engage_total,
        "damage_score": clamp_score(damage),
        "poke_score": clamp_score(poke),
        "sustain_score": clamp_score(sustain),
        "frontline_score": clamp_score(frontline),
        "engage_top_spells": ",".join(str(skill["slot"]) for skill in engage_skills[:2]),
        "wave_top_spells": ",".join(str(skill["slot"]) for skill in wave_skills[:3]),
        "st_floor": st_floor,
        "semantic_formula_version": FORMULA_VERSION,
        "notes": "; ".join(
            f"{col.replace('_score', '')}={','.join(vals[:5])}"
            for col, vals in evidence.items()
            if vals
        ),
    }

    for key, value in REVIEWED_OVERRIDES.get(str(champion.get("alias", "")), {}).items():
        row[key] = float(value)

    row["core_min_score"] = min(float(row[col]) for col in CORE_COLUMNS)
    row["core_mean_score"] = round(
        sum(float(row[col]) for col in CORE_COLUMNS) / len(CORE_COLUMNS),
        2,
    )
    if include_skill_debug:
        return row, build_skill_debug_rows(champion, engage_skills, wave_skills)
    return row


def build_scores(ability_json: Path) -> list[dict[str, Any]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    rows = [score_champion(champion) for champion in raw.get("champions", [])]
    return sorted(rows, key=lambda row: int(row["champion_id"]))


def build_scores_with_debug(ability_json: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    champion_rows: list[dict[str, Any]] = []
    skill_rows: list[dict[str, Any]] = []
    for champion in raw.get("champions", []):
        champ_row, debug_rows = score_champion(champion, include_skill_debug=True)
        champion_rows.append(champ_row)
        skill_rows.extend(debug_rows)
    champion_rows.sort(key=lambda row: int(row["champion_id"]))
    skill_rows.sort(key=lambda row: (int(row["champion_id"]), str(row["spell_slot"])))
    return champion_rows, skill_rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        raise click.ClickException("No rows to write")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option(
    "--ability-json",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_abilities.json"),
    show_default=True,
)
@click.option(
    "--out-csv",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_scores.json"),
    show_default=True,
)
@click.option(
    "--debug-csv",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_skill_debug.csv"),
    show_default=True,
)
@click.option(
    "--debug-json",
    type=click.Path(path_type=Path),
    default=Path("data/cache/champion_semantic_skill_debug.json"),
    show_default=True,
)
def main(
    ability_json: Path,
    out_csv: Path,
    out_json: Path,
    debug_csv: Path,
    debug_json: Path,
) -> None:
    rows, skill_rows = build_scores_with_debug(ability_json)
    write_csv(rows, out_csv)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(skill_rows, debug_csv)
    debug_json.parent.mkdir(parents=True, exist_ok=True)
    debug_json.write_text(json.dumps(skill_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    click.echo(f"[semantic] wrote {len(rows)} champion rows")
    click.echo(f"[semantic] wrote {len(skill_rows)} skill rows")
    click.echo(f"[semantic] csv : {out_csv}")
    click.echo(f"[semantic] json: {out_json}")
    click.echo(f"[semantic] debug csv : {debug_csv}")
    click.echo(f"[semantic] debug json: {debug_json}")


if __name__ == "__main__":
    main()
