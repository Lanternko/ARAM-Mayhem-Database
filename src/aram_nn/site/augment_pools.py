"""Mayhem (queue 2400) augment pools, read from CommunityDragon game data.

Patch notes only describe pool *changes*; the pools themselves live in the
client files:

- ``game/maps/modespecificdata/augmentgroups.bin`` defines every pool
  (an ``ID`` plus a list of augment paths).
- ``game/data/maps/shipping/map12/map12.bin`` (Howling Abyss) holds the
  champion -> pools table: each champion points at a config listing
  ``(pool, WEIGHT)`` pairs.  A missing WEIGHT means the class default.
- ``game/maps/modespecificdata/augmentoperators.bin`` (new in 26.18) holds
  global rules; today one rule marks a group of augments as never handed out
  at random.  The file records which pools a rule selects but not what it
  does, so the effect comes from the patch notes, not from the data.
- ``kiwi.bin`` AugmentData maps each augment path to its public numeric id
  (``AugmentPlatformId``), the same id the site's augment payload uses.

Most pool IDs are stored as 32-bit FNV-1a hashes of their lower-cased name.
``KNOWN_POOL_NAMES`` lists candidate names; a name only counts as resolved
when its hash matches the stored value, so every resolved name is verified at
build time rather than trusted from this list.

Per-augment champion exclusions announced in patch notes (e.g. Tank Engine
never offered to Ryze) are NOT in these files; they are applied server-side.
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any

CDRAGON_BASE = "https://raw.communitydragon.org"
GROUPS_PATH = "game/maps/modespecificdata/augmentgroups.bin.json"
OPERATORS_PATH = "game/maps/modespecificdata/augmentoperators.bin.json"
KIWI_PATH = "game/maps/modespecificdata/kiwi.bin.json"
MAP12_PATH = "game/data/maps/shipping/map12/map12.bin.json"
AUGS_PATH = "plugins/rcp-be-lol-game-data/global/{locale}/v1/cherry-augments.json"
CHAMPS_PATH = "plugins/rcp-be-lol-game-data/global/{locale}/v1/champion-summary.json"
META_PATH = "content-metadata.json"

# Unnamed bin fields/types (CDragon has no names for them yet).
_GROUP_TYPE = "{fead7e9b}"
_TABLE_LIST = "{0bf9074a}"
_TABLE_CFG = "{f1500c60}"
_CFG_ENTRIES = "{248cf7db}"
_ENTRY_POOL = "{c940fe53}"
_OP_CLAUSES = "{62f43a1d}"
_OP_GROUPS = "{8fa6f4d0}"

STAT_POOLS = {
    "AH": ("技能急速", "Ability Haste"),
    "AP": ("魔攻", "Ability Power"),
    "AD": ("物攻", "Attack Damage"),
    "AS": ("攻速", "Attack Speed"),
    "CritChance": ("暴擊", "Crit Chance"),
    "ArmorPen": ("物穿", "Armor Pen"),
    "MagicPen": ("魔穿", "Magic Pen"),
    "LifeSteal": ("普攻吸血", "Life Steal"),
    "Omnivamp": ("全能吸血", "Omnivamp"),
    "Spellvamp": ("技能吸血", "Spell Vamp"),
    "Health": ("生命", "Health"),
    "Armor": ("物防", "Armor"),
    "MR": ("魔防", "Magic Resist"),
    "Mana": ("魔力", "Mana"),
    "MS": ("跑速", "Move Speed"),
}
FUNCTION_POOLS = {
    "CC": ("控場", "Crowd Control"),
    "Engage": ("開戰", "Engage"),
    "Peel": ("保護隊友", "Peel"),
    "DashBlink": ("突進與位移", "Dash & Blink"),
    "HealSelfish": ("自我治療", "Self Heal"),
    "HealSelfless": ("治療隊友", "Ally Heal"),
    "ShieldSelfish": ("自我護盾", "Self Shield"),
    "ShieldSelfless": ("護盾隊友", "Ally Shield"),
    "SizeBig": ("體型變大", "Size Up"),
    "SizeSmall": ("體型變小", "Size Down"),
    "Burn": ("燃燒", "Burn"),
    "Economy": ("金錢", "Economy"),
    "EarlySpike": ("前期強勢", "Early Spike"),
    "SnowBall": ("雪球", "Snowball"),
    "SummonerSpell": ("召喚師技能", "Summoner Spell"),
}
_ARCH_RE = re.compile(r"^(Melee|Ranged)(Attacker|Caster)(AD|AP|Burst|DPS)$")
_ARCH_ZH = {
    "Melee": "近戰", "Ranged": "遠程", "Attacker": "普攻型", "Caster": "施法型",
    "AD": "物攻", "AP": "魔攻", "Burst": "爆發", "DPS": "持續",
}
_ARCH_EN = {"Attacker": "Attacker", "Caster": "Caster", "DPS": "DPS"}
KNOWN_POOL_NAMES = tuple(STAT_POOLS) + tuple(FUNCTION_POOLS) + tuple(
    f"{r}{s}{d}"
    for r in ("Melee", "Ranged")
    for s in ("Attacker", "Caster")
    for d in ("AD", "AP", "Burst", "DPS")
)

# Pools whose name hash is not recoverable.  Labels describe the contents and
# are keyed by the stored hash, which is stable across patches.  ``patch``
# means the label is quoted from patch notes that match the pool exactly.
INFERRED_POOLS = {
    "99d70c96": ("強勢後期成長", "Strong Late-game Scaling", "patch"),
    "10f8e38e": ("坦克（廣）", "Tank (broad)", "inferred"),
    "ba0256ad": ("坦克（核心）", "Tank (core)", "inferred"),
    "563cdf9c": ("通用 A", "General A", "inferred"),
    "cb218786": ("通用 B：質變與怪招", "General B: transmutes & oddities", "inferred"),
    "ba7415bd": ("通用 C：技能與冷卻", "General C: abilities & cooldowns", "inferred"),
    "73f04b68": ("大絕招", "Ultimate", "inferred"),
    "8c232386": ("隱身與跑速", "Stealth & speed", "inferred"),
    "2b5a8648": ("衝進敵陣", "Dive in", "inferred"),
    "794c79e3": ("近戰通用", "Melee general", "inferred"),
    "9f7a920a": ("遠程通用", "Ranged general", "inferred"),
    "4f40633b": ("機動與刺殺", "Mobility & assassination", "inferred"),
    "a1d5fd67": ("輔助（小）", "Support (small)", "inferred"),
    "86d9be15": ("治療與護盾輔助", "Heal & shield support", "inferred"),
    "257a6f57": ("恐懼與困住", "Fear & trap", "inferred"),
}

Fetch = Callable[[str, str], Any]


def fnv1a32(text: str) -> str:
    """Lower-cased 32-bit FNV-1a, formatted like CDragon's ``{xxxxxxxx}``."""
    h = 0x811C9DC5
    for byte in text.lower().encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


_HASH_TO_NAME = {fnv1a32(n): n for n in KNOWN_POOL_NAMES}


def resolve_pool_name(raw_id: str) -> tuple[str | None, str | None]:
    """Return ``(name, hash)``; ``name`` is None when the hash is unresolved."""
    if not raw_id.startswith("{"):
        return raw_id, None
    h = raw_id.strip("{}").lower()
    return _HASH_TO_NAME.get(h), h


def fetch_json(version: str, path: str, timeout: float = 90.0) -> Any:
    url = f"{CDRAGON_BASE}/{version}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "arammeta-augment-pools"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_pools(groups: dict) -> dict[str, dict]:
    """Group object key -> {"id": raw ID, "augs": [augment paths]}."""
    return {
        key: {"id": obj["ID"], "augs": [a["Augment"] for a in obj.get("augments", [])]}
        for key, obj in groups.items()
        if isinstance(obj, dict) and obj.get("__type") == _GROUP_TYPE
    }


def parse_operator_group_keys(operators: dict) -> set[str]:
    keys: set[str] = set()
    for obj in operators.values():
        if not isinstance(obj, dict):
            continue
        for clause in obj.get(_OP_CLAUSES, []) or []:
            keys.update(clause.get(_OP_GROUPS, []) or [])
    return keys


def parse_champion_pools(map12: dict) -> dict[str, list[tuple[str, int]]]:
    """Champion alias -> [(group object key, weight)]; weight 0 = default."""
    table = next(
        (o for o in map12.values() if isinstance(o, dict) and _TABLE_LIST in o), None
    )
    if table is None:
        raise ValueError("map12.bin has no champion augment-pool table")
    out: dict[str, list[tuple[str, int]]] = {}
    for entry in table[_TABLE_LIST]:
        alias = entry["championName"].split("/")[-1]
        cfg = map12[entry[_TABLE_CFG]]
        out[alias] = [(e[_ENTRY_POOL], int(e.get("WEIGHT", 0))) for e in cfg.get(_CFG_ENTRIES, [])]
    return out


def augment_ids(kiwi: dict) -> dict[str, int]:
    return {
        path: int(obj["AugmentPlatformId"])
        for path, obj in kiwi.items()
        if isinstance(obj, dict) and obj.get("__type") == "AugmentData" and "AugmentPlatformId" in obj
    }


def _icon_url(path: str | None) -> str:
    marker = "/lol-game-data/assets/"
    if not path or marker not in path:
        return ""
    rest = path.split(marker, 1)[1].lower()
    return f"{CDRAGON_BASE}/latest/plugins/rcp-be-lol-game-data/global/default/{rest}"


def _pool_meta(raw_id: str, operator_group: bool) -> dict:
    name, h = resolve_pool_name(raw_id)
    if operator_group:
        # 16.18 patch notes: augments in this group are never handed out at random
        # (Transmute, Pandora's Box, Crown Me King).  Normal selection still offers
        # them, so do NOT subtract them from a champion's reachable augments.
        return {"name": name, "hash": h, "family": "norandom",
                "label_zh": "排除池", "label_en": "Excluded from random", "label_source": "operator"}
    if name in STAT_POOLS:
        zh, en = STAT_POOLS[name]
        return {"name": name, "hash": h, "family": "stat", "label_zh": zh, "label_en": en, "label_source": "name"}
    if name in FUNCTION_POOLS:
        zh, en = FUNCTION_POOLS[name]
        return {"name": name, "hash": h, "family": "function", "label_zh": zh, "label_en": en, "label_source": "name"}
    m = _ARCH_RE.match(name or "")
    if m:
        r, s, d = m.groups()
        return {"name": name, "hash": h, "family": "archetype",
                "label_zh": f"{_ARCH_ZH[r]}{_ARCH_ZH[s]}・{_ARCH_ZH[d]}",
                "label_en": f"{r} {_ARCH_EN[s]} · {d}", "label_source": "name"}
    if name:  # resolved but not labelled yet: never surface the raw name as a label
        return {"name": name, "hash": h, "family": "function",
                "label_zh": "未分類", "label_en": "Unclassified", "label_source": "name"}
    zh, en, src = INFERRED_POOLS.get(h or "", ("未命名", "Unnamed", "inferred"))
    family = "function" if src == "patch" else "inferred"
    return {"name": None, "hash": h, "family": family, "label_zh": zh, "label_en": en, "label_source": src}


def _snapshot(fetch: Fetch, version: str, kiwi_ids: dict[str, int] | None = None) -> dict:
    groups = parse_pools(fetch(version, GROUPS_PATH))
    champs = parse_champion_pools(fetch(version, MAP12_PATH))
    ids = kiwi_ids if kiwi_ids is not None else augment_ids(fetch(version, KIWI_PATH))
    key_to_id = {k: g["id"] for k, g in groups.items()}
    pools = {g["id"]: [ids[p] for p in g["augs"] if p in ids] for g in groups.values()}
    weights = {
        alias: {key_to_id[k]: w for k, w in entries if k in key_to_id}
        for alias, entries in champs.items()
    }
    return {"groups": groups, "key_to_id": key_to_id, "pools": pools, "weights": weights, "ids": ids}


def build_payload(fetch: Fetch, version: str, prev_version: str | None = None) -> dict:
    """Assemble the internal payload (pool names and hashes included); publish via ``public_payload``."""
    cur = _snapshot(fetch, version)
    try:
        operators = fetch(version, OPERATORS_PATH)
    except Exception:  # file absent before 26.18
        operators = {}
    operator_ids = {cur["key_to_id"][k] for k in parse_operator_group_keys(operators) if k in cur["key_to_id"]}

    champ_rows = fetch(version, CHAMPS_PATH.format(locale="default"))
    alias_to_id = {str(c["alias"]).lower(): int(c["id"]) for c in champ_rows if int(c.get("id", -1)) > 0}

    def cid_of(alias: str) -> int | None:
        return alias_to_id.get(alias.lower())

    champs_out: dict[str, list] = {}
    members: dict[str, int] = {}
    for alias, pools in sorted(cur["weights"].items()):
        cid = cid_of(alias)
        if cid is None:
            continue
        champs_out[str(cid)] = [[pid, w] for pid, w in pools.items()]
        for pid in pools:
            members[pid] = members.get(pid, 0) + 1

    pools_out = []
    for pid, augs in cur["pools"].items():
        meta = _pool_meta(pid, pid in operator_ids)
        if meta["family"] not in ("norandom",) and (not augs or not members.get(pid)):
            meta["family"] = "unused"
        pools_out.append({"id": pid, **meta, "augs": augs, "champs": members.get(pid, 0)})

    aug_ids = sorted({a for p in cur["pools"].values() for a in p})
    zh_rows = {int(a["id"]): a for a in fetch(version, AUGS_PATH.format(locale="zh_tw"))}
    en_rows = {int(a["id"]): a for a in fetch(version, AUGS_PATH.format(locale="default"))}
    augs_out = {}
    for aid in aug_ids:
        zh, en = zh_rows.get(aid, {}), en_rows.get(aid, {})
        augs_out[str(aid)] = {
            "zh": zh.get("nameTRA") or en.get("nameTRA") or str(aid),
            "en": en.get("nameTRA") or zh.get("nameTRA") or str(aid),
            "icon": _icon_url(en.get("augmentSmallIconPath") or zh.get("augmentSmallIconPath")),
            "rarity": en.get("rarity") or zh.get("rarity") or "",
        }

    payload = {
        "version": version,
        "source": "CommunityDragon: augmentgroups.bin, augmentoperators.bin, kiwi.bin, map12.bin",
        "pools": pools_out,
        "champs": champs_out,
        "augs": augs_out,
        "diff": None,
    }
    if prev_version:
        payload["diff"] = diff_snapshots(_snapshot(fetch, prev_version, cur["ids"]), cur, cid_of)
        payload["diff"]["prev_version"] = prev_version
    return payload


_ARCH_ROWS = ("MeleeAttacker", "RangedAttacker", "MeleeCaster", "RangedCaster")
_ARCH_COLS = ("AD", "AP", "Burst", "DPS")


def public_payload(payload: dict) -> dict:
    """Copy of ``payload`` safe to publish: no internal pool names or hashes.

    The page shows only the zh/en labels. Pool ids become opaque ``p1``..``pN``
    (consistent within one file), and archetype pools carry their matrix slot
    as ``cell`` = [row, col] so the page never has to parse a name.
    """
    ids: dict[str, str] = {}

    def pub(pid: str) -> str:
        return ids.setdefault(pid, f"p{len(ids) + 1}")

    pools = []
    for p in payload["pools"]:
        out = {k: v for k, v in p.items() if k not in ("name", "hash")}
        out["id"] = pub(p["id"])
        m = _ARCH_RE.match(p.get("name") or "")
        if m:
            r, s, d = m.groups()
            out["cell"] = [_ARCH_ROWS.index(r + s), _ARCH_COLS.index(d)]
        pools.append(out)
    champs = {cid: [[pub(pid), w] for pid, w in rows] for cid, rows in payload["champs"].items()}
    diff = payload.get("diff")
    if diff:
        diff = {
            **diff,
            "pools": [{**x, "id": pub(x["id"])} for x in diff["pools"]],
            "new_pools": [pub(x) for x in diff["new_pools"]],
            "removed_pools": [pub(x) for x in diff["removed_pools"]],
            "weights": [{**x, "pool": pub(x["pool"])} for x in diff["weights"]],
        }
    return {**payload, "pools": pools, "champs": champs, "diff": diff}


def diff_snapshots(prev: dict, cur: dict, cid_of: Callable[[str], int | None]) -> dict:
    pools = []
    for pid, augs in cur["pools"].items():
        before = prev["pools"].get(pid)
        if before is None:
            continue
        added = [a for a in augs if a not in before]
        removed = [a for a in before if a not in augs]
        if added or removed:
            pools.append({"id": pid, "added": added, "removed": removed})
    weights = []
    for alias in sorted(set(prev["weights"]) | set(cur["weights"])):
        cid = cid_of(alias)
        if cid is None:
            continue
        a, b = prev["weights"].get(alias, {}), cur["weights"].get(alias, {})
        for pid in sorted(set(a) | set(b)):
            if a.get(pid) != b.get(pid):
                weights.append({"champ": cid, "pool": pid, "before": a.get(pid), "after": b.get(pid)})
    return {
        "pools": pools,
        "new_pools": sorted(set(cur["pools"]) - set(prev["pools"])),
        "removed_pools": sorted(set(prev["pools"]) - set(cur["pools"])),
        "weights": weights,
    }
