"""Shared identities for mana items before and after their stack transformation."""

STACKED_ITEM_IDS = {3003: 3040, 3004: 3042, 3119: 3121}


def canonical_item_id(item_id: int, item_meta: dict[int, dict]) -> int:
    """Use the stacked identity when its metadata is available."""
    stacked = STACKED_ITEM_IDS.get(item_id, item_id)
    return stacked if stacked in item_meta else item_id


def item_families_payload(item_meta: dict[int, dict]) -> dict[str, dict]:
    """Keep the pre-stack identity for icons, names and merge explanations."""
    return {
        str(stacked): {
            key: item_meta[base].get(key, "")
            for key in ("id", "name", "name_zh", "name_en", "icon")
        }
        for base, stacked in STACKED_ITEM_IDS.items()
        if base in item_meta and stacked in item_meta
    }
