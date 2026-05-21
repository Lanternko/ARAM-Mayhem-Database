"""Shared champion role definitions for Mayhem site tooling.

Primary role is curated and stable per champion. Do not infer it from
Data Dragon tags. Secondary roles are curated here as sparse, explicit
Mayhem identities; do not infer them from Data Dragon tags.
"""

from __future__ import annotations

ROLE_ORDER = ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank")
ROLE_SORT_PRIORITY = {role: idx for idx, role in enumerate(ROLE_ORDER)}

ROLE_LABELS = {
    "Assassin": {"zh": "刺客", "en": "Assassin"},
    "Fighter": {"zh": "戰士", "en": "Fighter"},
    "Mage": {"zh": "法師", "en": "Mage"},
    "Marksman": {"zh": "射手", "en": "Marksman"},
    "Support": {"zh": "輔助", "en": "Support"},
    "Tank": {"zh": "坦克", "en": "Tank"},
}

# Data Dragon tags are Riot general/SR labels, so Mayhem keeps one curated
# primary role per champion. This map is intentionally static.
PRIMARY_ROLE_OVERRIDES: dict[str, str] = {
    # --- Assassin ---
    "Akali": "Assassin",
    "Diana": "Assassin",
    "Ekko": "Assassin",
    "Evelynn": "Assassin",
    "Fizz": "Assassin",
    "Kassadin": "Assassin",
    "Katarina": "Assassin",
    "Leblanc": "Assassin",
    "Naafiri": "Assassin",
    "Nocturne": "Assassin",
    "Qiyana": "Assassin",
    "Rengar": "Assassin",
    # --- Fighter ---
    "Briar": "Fighter",
    "Fiora": "Fighter",
    "Irelia": "Fighter",
    "Jax": "Fighter",
    "Kayn": "Fighter",
    "LeeSin": "Fighter",
    "MasterYi": "Fighter",
    "Pantheon": "Fighter",
    "Riven": "Fighter",
    "Tryndamere": "Fighter",
    "Vi": "Fighter",
    "Viego": "Fighter",
    "XinZhao": "Fighter",
    "Yasuo": "Fighter",
    "Yone": "Fighter",
    "Zaahen": "Fighter",
    "Aatrox": "Fighter",
    "Ambessa": "Fighter",
    "Camille": "Fighter",
    "Darius": "Fighter",
    "Garen": "Fighter",
    "Gnar": "Fighter",
    "Hecarim": "Fighter",
    "Illaoi": "Fighter",
    "JarvanIV": "Fighter",
    "Jayce": "Fighter",
    "Kled": "Fighter",
    "MonkeyKing": "Fighter",
    "Mordekaiser": "Fighter",
    "Olaf": "Fighter",
    "RekSai": "Fighter",
    "Renekton": "Fighter",
    "Sett": "Fighter",
    "Shyvana": "Fighter",
    "Trundle": "Fighter",
    "Udyr": "Fighter",
    "Urgot": "Fighter",
    "Warwick": "Fighter",
    "Yorick": "Fighter",
    "Singed": "Fighter",
    # --- Tank ---
    "Poppy": "Tank",
    "Malphite": "Tank",
    "Maokai": "Tank",
    "DrMundo": "Tank",
    "KSante": "Tank",
    "Nunu": "Tank",
    "Ornn": "Tank",
    "Rammus": "Tank",
    "Sejuani": "Tank",
    "Sion": "Tank",
    "Skarner": "Tank",
    "Zac": "Tank",
    "Amumu": "Tank",
    "Chogath": "Tank",
    "Galio": "Tank",
    "Nasus": "Tank",
    "Volibear": "Tank",
    "TahmKench": "Tank",
    "Taric": "Tank",
    # --- Marksman ---
    "Akshan": "Marksman",
    "Ashe": "Marksman",
    "Corki": "Marksman",
    "Ezreal": "Marksman",
    "Jhin": "Marksman",
    "Kaisa": "Marksman",
    "Kayle": "Marksman",
    "KogMaw": "Marksman",
    "Lucian": "Marksman",
    "MissFortune": "Marksman",
    "Nilah": "Marksman",
    "Quinn": "Marksman",
    "Samira": "Marksman",
    "Smolder": "Marksman",
    "Tristana": "Marksman",
    "Twitch": "Marksman",
    "Varus": "Marksman",
    "Vayne": "Marksman",
    # --- Mage ---
    "Azir": "Mage",
    "Aurora": "Mage",
    "Fiddlesticks": "Mage",
    "Karma": "Mage",
    "Lux": "Mage",
    "Mel": "Mage",
    "Nidalee": "Mage",
    "Orianna": "Mage",
    "Rumble": "Mage",
    "Seraphine": "Mage",
    "Swain": "Mage",
    "Taliyah": "Mage",
    "Teemo": "Mage",
    "Zoe": "Mage",
    "Zyra": "Mage",
    "Annie": "Mage",
    "Brand": "Mage",
    "Heimerdinger": "Mage",
    "Hwei": "Mage",
    "Neeko": "Mage",
    "Velkoz": "Mage",
    "Xerath": "Mage",
    "TwistedFate": "Mage",
    "Vladimir": "Mage",
    # --- Support ---
    "Thresh": "Support",
    "Morgana": "Support",
    "Bard": "Support",
    "Janna": "Support",
    "Lulu": "Support",
    "Nami": "Support",
    "Sona": "Support",
    "Soraka": "Support",
    "Yuumi": "Support",
    "Zilean": "Support",
    "Ivern": "Support",
    "Milio": "Support",
    "Renata": "Support",
    # --- Fixed primary roles that previously matched Data Dragon's first tag ---
    "Ahri": "Mage",
    "Alistar": "Tank",
    "Anivia": "Mage",
    "Aphelios": "Marksman",
    "AurelionSol": "Mage",
    "Belveth": "Fighter",
    "Blitzcrank": "Tank",
    "Braum": "Tank",
    "Caitlyn": "Marksman",
    "Cassiopeia": "Mage",
    "Draven": "Marksman",
    "Elise": "Assassin",
    "Gangplank": "Fighter",
    "Gragas": "Fighter",
    "Graves": "Marksman",
    "Gwen": "Fighter",
    "Jinx": "Marksman",
    "Kalista": "Marksman",
    "Karthus": "Mage",
    "Kennen": "Mage",
    "Khazix": "Assassin",
    "Kindred": "Marksman",
    "Leona": "Tank",
    "Lillia": "Fighter",
    "Lissandra": "Mage",
    "Malzahar": "Mage",
    "Nautilus": "Tank",
    "Pyke": "Support",
    "Rakan": "Support",
    "Rell": "Tank",
    "Ryze": "Mage",
    "Senna": "Support",
    "Shaco": "Assassin",
    "Shen": "Tank",
    "Sivir": "Marksman",
    "Syndra": "Mage",
    "Sylas": "Mage",
    "Talon": "Assassin",
    "Veigar": "Mage",
    "Vex": "Mage",
    "Viktor": "Mage",
    "Xayah": "Marksman",
    "Yunara": "Marksman",
    "Zed": "Assassin",
    "Zeri": "Marksman",
    "Ziggs": "Mage",
}

# Optional static secondary roles.  Keep this sparse: add a champion only when
# both roles are legitimate Mayhem identities.  Downstream ML treats primary and
# secondary roles with equal one-hot weight.
SECONDARY_ROLE_OVERRIDES: dict[str, str] = {
    "Akali": "Mage",
    "Anivia": "Fighter",
    "Bard": "Mage",
    "Cassiopeia": "Fighter",
    "Diana": "Tank",
    "Ekko": "Mage",
    "Elise": "Mage",
    "Fizz": "Mage",
    "Gnar": "Tank",
    "Gragas": "Mage",
    "Gwen": "Mage",
    "Karma": "Support",
    "Kassadin": "Mage",
    "Katarina": "Fighter",
    "KogMaw": "Mage",
    "Leblanc": "Mage",
    "Lillia": "Mage",
    "MissFortune": "Mage",
    "Morgana": "Mage",
    "Nasus": "Mage",
    "Rakan": "Tank",
    "RekSai": "Tank",
    "Rengar": "Fighter",
    "Rumble": "Fighter",
    "Senna": "Marksman",
    "Shaco": "Mage",
    "Swain": "Fighter",
    "Sylas": "Fighter",
    "Thresh": "Tank",
    "Trundle": "Tank",
    "Udyr": "Mage",
    "Volibear": "Fighter",
    "Zilean": "Mage",
}

ROLE_FROM_ITEM_STYLE: dict[str, str] = {
    "ap_burn": "Mage",
    "ap_burst": "Mage",
    "ap_bruiser": "Fighter",
    "ad_bruiser": "Fighter",
    "tank": "Tank",
    "heartsteel": "Tank",
    "support": "Support",
}

MARKSMAN_ITEM_STYLES = {"crit", "onhit", "ad_poke", "ap_onhit"}
ROLE_RANGED_ALIAS_OVERRIDES = {"Kayle"}
RANGED_ATTACK_RANGE_MIN = 400


def role_definitions_payload() -> dict[str, object]:
    """Return the public, site-wide role specification payload."""
    primary_by_role = {role: [] for role in ROLE_ORDER}
    for alias, role in sorted(PRIMARY_ROLE_OVERRIDES.items()):
        primary_by_role.setdefault(role, []).append(alias)
    return {
        "schema_version": 1,
        "role_order": list(ROLE_ORDER),
        "role_labels": ROLE_LABELS,
        "primary_role_policy": {
            "summary_zh": "主職業是全站固定規格，不從 Data Dragon tags 或當前勝率推斷。",
            "summary_en": "Primary role is a fixed site-wide spec, not inferred from Data Dragon tags or current win rate.",
            "source": "scripts/champion_roles.py",
        },
        "secondary_role_policy": {
            "summary_zh": "副職業是全站固定規格中的少量明確雙職業，不從 Data Dragon tags 推斷。",
            "summary_en": "Secondary role is a sparse curated site-wide spec for explicit dual-role champions, not inferred from Data Dragon tags.",
            "source": "scripts/champion_roles.py",
        },
        "primary_roles": primary_by_role,
        "primary_role_by_champion": dict(sorted(PRIMARY_ROLE_OVERRIDES.items())),
        "secondary_role_by_champion": dict(sorted(SECONDARY_ROLE_OVERRIDES.items())),
    }


def primary_role_for_alias(alias: str, ddragon_tags: list[str] | tuple[str, ...] = ()) -> str:
    """Return the fixed Mayhem primary role, falling back only for unknown champions."""
    return PRIMARY_ROLE_OVERRIDES.get(alias) or (str(ddragon_tags[0]) if ddragon_tags else "")


def role_tags_for_alias(alias: str, ddragon_tags: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Return the stable site/ML tag list: primary plus optional secondary."""
    primary = primary_role_for_alias(alias, ddragon_tags)
    if not primary:
        return [str(ddragon_tags[0])] if ddragon_tags else []
    secondary = SECONDARY_ROLE_OVERRIDES.get(alias)
    if secondary and secondary != primary:
        return [primary, secondary]
    return [primary]
