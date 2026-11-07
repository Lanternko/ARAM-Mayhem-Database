"""Spell/item-set import: payload shape and the never-clobber guarantees."""
from __future__ import annotations

import unittest

from aram_nn.lcu.loadout import (
    ITEM_SET_PREFIX, apply_item_set, apply_summoner_spells, build_item_set, merge_item_sets, order_spells,
)

FLASH, MARK, GHOST = 4, 32, 6


class FakeLCU:
    def __init__(self, gets: dict | None = None, ok: bool = True) -> None:
        self.gets = gets or {}
        self.ok = ok
        self.sent: list[tuple[str, str, object]] = []

    def get(self, path: str, **_params):
        return self.gets.get(path)

    def send(self, method: str, path: str, payload=None) -> bool:
        self.sent.append((method, path, payload))
        return self.ok


def _session(spell1: int, spell2: int) -> dict:
    return {"localPlayerCellId": 2, "myTeam": [
        {"cellId": 1, "spell1Id": 1, "spell2Id": 3},
        {"cellId": 2, "spell1Id": spell1, "spell2Id": spell2},
    ]}


class OrderSpellsTest(unittest.TestCase):
    def test_keeps_flash_on_its_key(self) -> None:
        self.assertEqual(order_spells([FLASH, MARK], [GHOST, FLASH]), (MARK, FLASH))
        self.assertEqual(order_spells([MARK, FLASH], [FLASH, GHOST]), (FLASH, MARK))

    def test_fresh_slots_follow_recommendation(self) -> None:
        self.assertEqual(order_spells([MARK, FLASH], [0, 0]), (MARK, FLASH))

    def test_rejects_bad_recommendation(self) -> None:
        self.assertIsNone(order_spells([FLASH], [0, 0]))
        self.assertIsNone(order_spells([FLASH, FLASH], [0, 0]))


class ApplySpellsTest(unittest.TestCase):
    def test_patches_my_selection(self) -> None:
        lcu = FakeLCU()
        ok, _ = apply_summoner_spells(lcu, _session(GHOST, FLASH), [FLASH, MARK])
        self.assertTrue(ok)
        self.assertEqual(lcu.sent, [("PATCH", "/lol-champ-select/v1/session/my-selection",
                                     {"spell1Id": MARK, "spell2Id": FLASH})])

    def test_noop_when_already_set(self) -> None:
        lcu = FakeLCU()
        ok, _ = apply_summoner_spells(lcu, _session(MARK, FLASH), [FLASH, MARK])
        self.assertTrue(ok)
        self.assertEqual(lcu.sent, [])

    def test_outside_champ_select(self) -> None:
        lcu = FakeLCU()
        self.assertFalse(apply_summoner_spells(lcu, None, [FLASH, MARK])[0])
        self.assertEqual(lcu.sent, [])


class ItemSetTest(unittest.TestCase):
    def test_build_skips_empty_blocks(self) -> None:
        item_set = build_item_set(23, "泰達米爾", [("核心裝", [3078, 6631]), ("搭配裝備", []), ("鞋子", [3047])])
        self.assertEqual(item_set["title"], f"{ITEM_SET_PREFIX} 泰達米爾")
        self.assertEqual(item_set["associatedChampions"], [23])
        self.assertEqual([b["type"] for b in item_set["blocks"]], ["核心裝", "鞋子"])
        self.assertEqual(item_set["blocks"][0]["items"], [{"id": "3078", "count": 1}, {"id": "6631", "count": 1}])

    def test_merge_replaces_only_our_set_for_that_champion(self) -> None:
        mine_old = {"title": f"{ITEM_SET_PREFIX} 泰達米爾", "associatedChampions": [23]}
        mine_other = {"title": f"{ITEM_SET_PREFIX} 劫", "associatedChampions": [238]}
        hand_made = {"title": "我的泰達", "associatedChampions": [23]}
        existing = {"accountId": 1, "itemSets": [mine_old, mine_other, hand_made], "timestamp": 0}
        new = build_item_set(23, "泰達米爾", [("核心裝", [3078])])
        merged = merge_item_sets(existing, new)
        self.assertEqual(merged["accountId"], 1)
        self.assertEqual(merged["itemSets"], [new, mine_other, hand_made])

    def test_apply_puts_merged_body(self) -> None:
        path = "/lol-item-sets/v1/item-sets/77/sets"
        lcu = FakeLCU(gets={
            "/lol-summoner/v1/current-summoner": {"summonerId": 77},
            path: {"accountId": 9, "itemSets": [], "timestamp": 0},
        })
        ok, _ = apply_item_set(lcu, 23, "泰達米爾", [("核心裝", [3078])])
        self.assertTrue(ok)
        method, sent_path, body = lcu.sent[0]
        self.assertEqual((method, sent_path), ("PUT", path))
        self.assertEqual(body["accountId"], 9)
        self.assertEqual(len(body["itemSets"]), 1)

    def test_apply_refuses_when_existing_sets_unreadable(self) -> None:
        # Writing without the current list would wipe the player's own sets.
        lcu = FakeLCU(gets={"/lol-summoner/v1/current-summoner": {"summonerId": 77}})
        self.assertFalse(apply_item_set(lcu, 23, "泰達米爾", [("核心裝", [3078])])[0])
        self.assertEqual(lcu.sent, [])


if __name__ == "__main__":
    unittest.main()
