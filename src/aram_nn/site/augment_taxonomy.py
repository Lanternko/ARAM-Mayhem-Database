"""Shared display taxonomy: detailed filters and compact, unique pool groups.

Stable category IDs preserve curated tags and existing public snapshots.
This is presentation metadata, not the game's pool membership or draw formula.
"""

AUGMENT_PURPOSE_GROUPS = (
    {"id": "damage", "zh": "輸出", "en": "Damage", "categories": ("ad", "ap", "crit", "amp")},
    {"id": "tank", "zh": "防守", "en": "Defense", "categories": ("tank",)},
    {"id": "support", "zh": "輔助", "en": "Support", "categories": ("support",)},
    {"id": "cd", "zh": "冷卻", "en": "Cooldown", "categories": ("cd",)},
    {"id": "gold", "zh": "經濟", "en": "Economy", "categories": ("gold",)},
    {"id": "mechanic", "zh": "特殊機制", "en": "Special mechanics", "categories": ("mechanic",)},
    {"id": "other", "zh": "未分類", "en": "Unclassified", "categories": ("other",)},
)
AUGMENT_CATEGORY_LABELS = {
    "ad": {"zh": "AD", "en": "AD"},
    "ap": {"zh": "AP", "en": "AP"},
    "crit": {"zh": "暴擊", "en": "Crit"},
    "amp": {"zh": "增傷", "en": "Damage amp"},
    **{group["id"]: {"zh": group["zh"], "en": group["en"]}
       for group in AUGMENT_PURPOSE_GROUPS if group["id"] != "damage"},
    "new": {"zh": "本版新增", "en": "New this patch"},
}
# New is a status filter, never a primary purpose.
AUGMENT_CATEGORY_ORDER = tuple(
    cat for group in AUGMENT_PURPOSE_GROUPS for cat in group["categories"]
) + ("new",)
AUGMENT_PRIMARY_PRIORITY = ("gold", "cd", "support", "tank", "ad", "ap", "crit", "amp", "mechanic")


def augment_taxonomy_payload() -> dict:
    return {
        "groups": list(AUGMENT_PURPOSE_GROUPS),
        "order": list(AUGMENT_CATEGORY_ORDER),
        "labels": AUGMENT_CATEGORY_LABELS,
        "primaryPriority": list(AUGMENT_PRIMARY_PRIORITY),
    }
