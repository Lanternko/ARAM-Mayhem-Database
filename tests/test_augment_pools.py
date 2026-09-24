from __future__ import annotations

import json
import unittest

from aram_nn.site.augment_pool_observed import PatchCounts, classify
from aram_nn.site.augment_pools import (
    AUGS_PATH,
    CHAMPS_PATH,
    GROUPS_PATH,
    KIWI_PATH,
    MAP12_PATH,
    OPERATORS_PATH,
    build_payload,
    fnv1a32,
    pool_hue,
    POOL_HUES,
    public_payload,
    resolve_pool_name,
)

A = "Maps/ModeSpecificData/Augments/"


def _groups(cc_augs):
    return {
        "{k1}": {"ID": "AH", "augments": [{"Augment": A + "One"}], "__type": "{fead7e9b}"},
        "{k2}": {"ID": "{56299123}", "augments": [{"Augment": A + x} for x in cc_augs], "__type": "{fead7e9b}"},
        "{k3}": {"ID": "{0c7ef8ce}", "augments": [{"Augment": A + "Two"}], "__type": "{fead7e9b}"},
        "{k4}": {"ID": "MS", "augments": [], "__type": "{fead7e9b}"},
        "__linked": [],
    }


def _map12(ah_weight):
    return {
        "{tbl}": {"{0bf9074a}": [{"championName": "Characters/Ryze", "{f1500c60}": "{c1}"}]},
        "{c1}": {"{248cf7db}": [{"{c940fe53}": "{k1}", "WEIGHT": ah_weight}, {"{c940fe53}": "{k2}"},
                                {"{c940fe53}": "{k4}"}],
                 "__type": "{f9e46502}"},
    }


FILES = {
    "cur": {
        GROUPS_PATH: _groups(["One", "Two"]),
        OPERATORS_PATH: {"{op}": {"{62f43a1d}": [{"{8fa6f4d0}": ["{k3}"]}], "__type": "{adaf4f78}"}},
        MAP12_PATH: _map12(200),
    },
    "prev": {GROUPS_PATH: _groups(["One"]), MAP12_PATH: _map12(150)},
    "shared": {
        KIWI_PATH: {
            A + "One": {"AugmentPlatformId": 101, "__type": "AugmentData"},
            A + "Two": {"AugmentPlatformId": 102, "__type": "AugmentData"},
        },
        CHAMPS_PATH.format(locale="default"): [{"id": -1, "alias": "None"}, {"id": 13, "alias": "Ryze"}],
        AUGS_PATH.format(locale="zh_tw"): [{"id": 101, "nameTRA": "甲"}, {"id": 102, "nameTRA": "乙"}],
        AUGS_PATH.format(locale="default"): [
            {"id": 101, "nameTRA": "One", "rarity": "kSilver",
             "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/One_small.png"},
            {"id": 102, "nameTRA": "Two"},
        ],
    },
}


def fake_fetch(version: str, path: str):
    for bucket in (version, "shared"):
        if path in FILES[bucket]:
            return FILES[bucket][path]
    raise FileNotFoundError(path)


class AugmentPoolTests(unittest.TestCase):
    def test_fnv1a_matches_hashes_stored_in_game_files(self) -> None:
        # Values observed in 26.18 augmentgroups.bin.
        self.assertEqual(fnv1a32("CC"), "56299123")
        self.assertEqual(fnv1a32("RangedAttackerAD"), "2fe0f044")
        self.assertEqual(fnv1a32("meleecasterdps"), fnv1a32("MeleeCasterDPS"))

    def test_resolve_pool_name(self) -> None:
        self.assertEqual(resolve_pool_name("AH"), ("AH", None))
        self.assertEqual(resolve_pool_name("{56299123}"), ("CC", "56299123"))
        self.assertEqual(resolve_pool_name("{deadbeef}"), (None, "deadbeef"))

    def test_build_payload_pools_weights_and_exclusion(self) -> None:
        p = build_payload(fake_fetch, "cur", "prev")
        pools = {x["id"]: x for x in p["pools"]}
        self.assertEqual(pools["{56299123}"]["name"], "CC")
        self.assertEqual(pools["{56299123}"]["family"], "function")
        self.assertEqual(pools["{56299123}"]["hue"], "function")
        self.assertEqual(pools["{56299123}"]["augs"], [101, 102])
        self.assertEqual(pools["{0c7ef8ce}"]["family"], "norandom")
        self.assertEqual(pools["{0c7ef8ce}"]["hue"], "other")
        self.assertEqual(pools["AH"]["family"], "stat")
        self.assertEqual(pools["AH"]["hue"], "cd")
        self.assertEqual(p["champs"], {"13": [["AH", 200], ["{56299123}", 0]]})
        self.assertEqual(p["augs"]["101"]["zh"], "甲")
        self.assertTrue(p["augs"]["101"]["icon"].endswith("/assets/ux/cherry/augments/icons/one_small.png"))

    def test_empty_pools_are_dropped_everywhere(self) -> None:
        # A pool with no augments can never be offered, even though map12 still
        # points every champion at it; it must not reach the page at all.
        p = build_payload(fake_fetch, "cur", "prev")
        self.assertNotIn("MS", {x["id"] for x in p["pools"]})
        self.assertNotIn("MS", [pid for pid, _ in p["champs"]["13"]])

    def test_diff_against_previous_patch(self) -> None:
        d = build_payload(fake_fetch, "cur", "prev")["diff"]
        self.assertEqual(d["pools"], [{"id": "{56299123}", "added": [102], "removed": []}])
        self.assertEqual(d["weights"], [{"champ": 13, "pool": "AH", "before": 150, "after": 200}])
        self.assertEqual(d["prev_version"], "prev")

    def test_empty_group_file_fails_loudly(self) -> None:
        def fetch(version: str, path: str):
            return {"__linked": []} if path == GROUPS_PATH else fake_fetch("cur", path)

        with self.assertRaisesRegex(ValueError, "no augment pools"):
            build_payload(fetch, "cur", None)

    def test_public_payload_hides_internal_names(self) -> None:
        pub = public_payload(build_payload(fake_fetch, "cur", "prev"))
        text = json.dumps(pub, ensure_ascii=False)
        for secret in ('"AH"', '"CC"', "56299123", "0c7ef8ce", '"name":', '"hash":'):
            self.assertNotIn(secret, text)
        ids = {p["label_zh"]: p["id"] for p in pub["pools"]}
        self.assertTrue(all(pid.startswith("p") for pid in ids.values()))
        self.assertEqual(pub["champs"]["13"], [[ids["技能急速"], 200], [ids["控場"], 0]])
        self.assertEqual(pub["diff"]["pools"][0]["id"], ids["控場"])
        self.assertEqual(pub["diff"]["weights"][0]["pool"], ids["技能急速"])

    def test_public_payload_places_archetype_pools_by_cell(self) -> None:
        internal = {"pools": [{"id": "{2fe0f044}", "name": "RangedAttackerAD", "hash": "2fe0f044"}],
                    "champs": {}, "diff": None}
        self.assertEqual(public_payload(internal)["pools"][0]["cell"], [1, 0])

    def test_pool_hue_categories(self) -> None:
        self.assertEqual(pool_hue("AD", None, "stat"), "ad")
        self.assertEqual(pool_hue("AS", None, "stat"), "ad")
        self.assertEqual(pool_hue("AH", None, "stat"), "cd")
        self.assertEqual(pool_hue("AP", None, "stat"), "ap")
        self.assertEqual(pool_hue("Armor", None, "stat"), "tank")
        self.assertEqual(pool_hue("MS", None, "stat"), "function")
        self.assertEqual(pool_hue("Economy", None, "function"), "gold")
        self.assertEqual(pool_hue("CC", None, "function"), "function")
        self.assertEqual(pool_hue("Peel", None, "function"), "support")
        self.assertEqual(pool_hue("HealSelfless", None, "function"), "support")
        self.assertEqual(pool_hue("ShieldSelfless", None, "function"), "support")
        self.assertEqual(pool_hue("HealSelfish", None, "function"), "function")
        self.assertEqual(pool_hue(None, "a1d5fd67", "inferred"), "support")
        self.assertEqual(pool_hue(None, "86d9be15", "inferred"), "support")
        self.assertEqual(pool_hue("Burn", None, "function"), "ap")
        self.assertEqual(pool_hue("SizeBig", None, "function"), "tank")
        self.assertEqual(pool_hue("RangedAttackerAD", None, "archetype"), "ad")
        self.assertEqual(pool_hue("RangedCasterAD", None, "archetype"), "ad")
        self.assertEqual(pool_hue("MeleeAttackerAP", None, "archetype"), "ap")
        self.assertEqual(pool_hue("RangedAttackerDPS", None, "archetype"), "ad")
        self.assertEqual(pool_hue("RangedCasterBurst", None, "archetype"), "ap")
        self.assertEqual(pool_hue(None, "ba7415bd", "inferred"), "cd")
        self.assertEqual(pool_hue(None, "10f8e38e", "inferred"), "tank")
        self.assertEqual(pool_hue(None, "563cdf9c", "inferred"), "other")
        self.assertEqual(pool_hue(None, "73f04b68", "inferred"), "function")
        self.assertEqual(pool_hue("AH", None, "norandom"), "other")

    def test_public_payload_keeps_hue(self) -> None:
        pub = public_payload(build_payload(fake_fetch, "cur", "prev"))
        hues = {p["label_zh"]: p["hue"] for p in pub["pools"]}
        self.assertEqual(hues["技能急速"], "cd")
        self.assertEqual(hues["控場"], "function")
        self.assertTrue(set(hues.values()) <= set(POOL_HUES))


def _counts(games: dict[int, int], pairs: dict[tuple[int, int], int]) -> PatchCounts:
    c = PatchCounts()
    c.games.update(games)
    c.pairs.update(pairs)
    return c


class ObservedFilterTests(unittest.TestCase):
    def test_classify_dead_blocked_and_thin_evidence(self) -> None:
        # Twelve champions get augment 5 at 10%; champion 99 plays as often and never does.
        games = {c: 1000 for c in range(1, 13)} | {99: 1000, 98: 30}
        pairs = {(c, 5): 100 for c in range(1, 13)}
        reach = {c: {5} for c in range(1, 13)} | {99: {5, 6}, 98: {5}}
        v = classify([(_counts(games, pairs), reach)])
        self.assertEqual(v["dead"], [6])  # nobody gets 6
        self.assertEqual(v["blocked"], {99: [5]})  # 98's 30 games expect only 3

    def test_classify_counts_exposure_only_where_reachable(self) -> None:
        prev = _counts({c: 1000 for c in range(1, 13)},
                       {(c, 5): 100 for c in range(1, 13)} | {(c, 7): 100 for c in range(2, 13)})
        cur = _counts({c: 20 for c in range(1, 13)}, {(c, 7): 2 for c in range(2, 13)})
        # Augment 7 joins champion 1's pools this patch; last patch's 1000 games don't count.
        v = classify([
            (cur, {c: {5, 7} for c in range(1, 13)}),
            (prev, {1: {5}} | {c: {5, 7} for c in range(2, 13)}),
        ])
        self.assertEqual(v["blocked"], {})

    def test_build_payload_drops_dead_augments(self) -> None:
        seen = _counts({13: 1000}, {(13, 101): 300})
        observed = {"cur": seen, "prev": seen, "patches": ["cur", "prev"], "games": 100}
        p = build_payload(fake_fetch, "cur", "prev", observed=observed)
        pools = {x["id"]: x for x in p["pools"]}
        self.assertEqual(pools["{56299123}"]["augs"], [101])
        self.assertNotIn("102", p["augs"])
        self.assertEqual(p["observed"]["dead"], [{"id": 102, "zh": "乙", "en": "Two", "rarity": ""}])
        self.assertEqual(p["observed"]["blocked"], {})
        self.assertEqual(p["diff"]["pools"], [])  # stripped from both snapshots: no fake "added"
        self.assertEqual(public_payload(p)["observed"], p["observed"])

    def test_build_payload_without_observed_is_unchanged(self) -> None:
        p = build_payload(fake_fetch, "cur", "prev")
        self.assertIsNone(p["observed"])
        self.assertIn("102", p["augs"])


if __name__ == "__main__":
    unittest.main()
