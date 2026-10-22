"""Resolve CommunityDragon's current RST keys and nested tooltip references."""
from __future__ import annotations

import re
from collections.abc import Mapping

from xxhash import xxh3_64_intdigest

_REFERENCE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def stringtable_text(entries: Mapping[str, str], key: str) -> str:
    """Look up a readable key or its 38-bit XXH3 RST key (game >= 15.2).

    CDragon only expands known hashes to readable keys. Newly shipped keys can
    remain hashed, even when the localized text is already in the same table.
    Algorithm: CommunityDragon/CDTB, cdtb/rstfile.py::key_to_hash.
    """
    if not key:
        return ""
    key = key.lower()
    direct = entries.get(key)
    if direct:
        return direct
    hashed = xxh3_64_intdigest(key.encode("utf-8")) & ((1 << 38) - 1)
    return entries.get(f"{{{hashed:010x}}}", "")


def augment_description_text(
    entries: Mapping[str, str], key: str, *, locale: str
) -> str:
    """Expand localized references, preserving the champion-dependent spell scope."""
    spell_name = "對應技能" if locale == "zh_tw" else "the affected ability"

    def expand(current: str, seen: frozenset[str]) -> str:
        normalized = current.lower()
        if normalized == "spellname":
            # Riot supplies this per champion at runtime. Do not guess Q/W/E/R.
            return spell_name
        if normalized in seen or len(seen) >= 12:
            return ""
        raw = stringtable_text(entries, normalized)
        return _REFERENCE.sub(
            lambda match: expand(match[1], seen | {normalized}), raw
        )

    return expand(key, frozenset())
