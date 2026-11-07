"""Push recommended summoner spells and an item set into the League client.

Both go through documented LCU endpoints (the same ones Blitz uses), not the
game process:

- spells: PATCH /lol-champ-select/v1/session/my-selection  (champ select only)
- item set: PUT /lol-item-sets/v1/item-sets/{summonerId}/sets

The item set is tagged with ITEM_SET_PREFIX so re-importing replaces only our
own set for that champion and never touches sets the player made by hand.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import Any

from .client import LCUClient

ITEM_SET_PREFIX = "AramMeta"


def order_spells(recommended: Sequence[int], current: Sequence[int]) -> tuple[int, int] | None:
    """Return (spell1Id, spell2Id), keeping a recommended spell on the key it already uses.

    Players have Flash on D or F by habit; swapping keys mid champ select is
    worse than not importing at all.
    """
    rec = [int(s) for s in recommended if int(s) > 0][:2]
    if len(rec) != 2 or rec[0] == rec[1]:
        return None
    cur = [int(s) for s in current][:2] if len(current) >= 2 else [0, 0]
    slots: list[int | None] = [None, None]
    for spell in rec:
        if spell in cur and slots[cur.index(spell)] is None:
            slots[cur.index(spell)] = spell
    rest = [s for s in rec if s not in slots]
    for i in range(2):
        if slots[i] is None:
            slots[i] = rest.pop(0)
    return int(slots[0]), int(slots[1])


def _local_player(session: dict | None) -> dict | None:
    if not session:
        return None
    cell = session.get("localPlayerCellId")
    for player in session.get("myTeam") or []:
        if player.get("cellId") == cell:
            return player
    return None


def apply_summoner_spells(client: LCUClient, session: dict | None, recommended: Sequence[int]) -> tuple[bool, str]:
    player = _local_player(session)
    if player is None:
        return False, "不在選角階段"
    order = order_spells(recommended, [player.get("spell1Id") or 0, player.get("spell2Id") or 0])
    if order is None:
        return False, "沒有推薦技能"
    if (player.get("spell1Id"), player.get("spell2Id")) == order:
        return True, "已是推薦技能"
    ok = client.send("PATCH", "/lol-champ-select/v1/session/my-selection",
                     {"spell1Id": order[0], "spell2Id": order[1]})
    return ok, "已套用召喚師技能" if ok else "客戶端拒絕（可能已鎖定）"


def _block(label: str, item_ids: Sequence[int]) -> dict:
    return {
        "type": label,
        "hideIfSummonerSpell": "",
        "showIfSummonerSpell": "",
        "items": [{"id": str(int(i)), "count": 1} for i in item_ids],
    }


def build_item_set(champion_id: int, champion_name: str, blocks: Sequence[tuple[str, Sequence[int]]]) -> dict:
    return {
        "uid": str(uuid.uuid4()),
        "title": f"{ITEM_SET_PREFIX} {champion_name}".strip(),
        "associatedChampions": [int(champion_id)],
        # Empty = every map; the Mayhem map id has changed between seasons.
        "associatedMaps": [],
        "blocks": [_block(label, ids) for label, ids in blocks if ids],
        "map": "any",
        "mode": "any",
        "preferredItemSlots": [],
        "sortrank": 0,
        "startedFrom": "blank",
        "type": "custom",
    }


def merge_item_sets(existing: dict | None, new_set: dict) -> dict:
    """Replace our previous set for the same champion; keep everything else."""
    body: dict[str, Any] = dict(existing or {})
    champs = set(new_set["associatedChampions"])
    kept = [
        s for s in body.get("itemSets") or []
        if not (str(s.get("title") or "").startswith(ITEM_SET_PREFIX)
                and set(s.get("associatedChampions") or []) & champs)
    ]
    body["itemSets"] = [new_set, *kept]
    body["timestamp"] = int(time.time() * 1000)
    return body


def apply_item_set(client: LCUClient, champion_id: int, champion_name: str,
                   blocks: Sequence[tuple[str, Sequence[int]]]) -> tuple[bool, str]:
    summoner = client.get("/lol-summoner/v1/current-summoner") or {}
    summoner_id = summoner.get("summonerId")
    if not summoner_id:
        return False, "讀不到召喚師"
    new_set = build_item_set(champion_id, champion_name, blocks)
    if not new_set["blocks"]:
        return False, "沒有推薦出裝"
    path = f"/lol-item-sets/v1/item-sets/{summoner_id}/sets"
    existing = client.get(path)
    if existing is None:
        return False, "讀不到現有裝備欄"
    ok = client.send("PUT", path, merge_item_sets(existing, new_set))
    return ok, "已匯入裝備欄" if ok else "客戶端拒絕裝備欄"
