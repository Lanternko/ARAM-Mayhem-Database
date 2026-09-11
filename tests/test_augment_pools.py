from __future__ import annotations

import json
import unittest

from aram_nn.site.augment_pools import (
    AUGS_PATH,
    CHAMPS_PATH,
    GROUPS_PATH,
    KIWI_PATH,
    MAP12_PATH,
    OPERATORS_PATH,
    build_payload,
    fnv1a32,
    public_payload,
    resolve_pool_name,
)

A = "Maps/ModeSpecificData/Augments/"


def _groups(cc_augs):
    return {
        "{k1}": {"ID": "AH", "augments": [{"Augment": A + "One"}], "__type": "{fead7e9b}"},
        "{k2}": {"ID": "{56299123}", "augments": [{"Augment": A + x} for x in cc_augs], "__type": "{fead7e9b}"},
        "{k3}": {"ID": "{0c7ef8ce}", "augments": [{"Augment": A + "Two"}], "__type": "{fead7e9b}"},
        "__linked": [],
    }


def _map12(ah_weight):
    return {
        "{tbl}": {"{0bf9074a}": [{"championName": "Characters/Ryze", "{f1500c60}": "{c1}"}]},
        "{c1}": {"{248cf7db}": [{"{c940fe53}": "{k1}", "WEIGHT": ah_weight}, {"{c940fe53}": "{k2}"}],
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
        self.assertEqual(pools["{56299123}"]["augs"], [101, 102])
        self.assertEqual(pools["{0c7ef8ce}"]["family"], "excluded")
        self.assertEqual(pools["AH"]["family"], "stat")
        self.assertEqual(p["champs"], {"13": [["AH", 200], ["{56299123}", 0]]})
        self.assertEqual(p["augs"]["101"]["zh"], "甲")
        self.assertTrue(p["augs"]["101"]["icon"].endswith("/assets/ux/cherry/augments/icons/one_small.png"))

    def test_diff_against_previous_patch(self) -> None:
        d = build_payload(fake_fetch, "cur", "prev")["diff"]
        self.assertEqual(d["pools"], [{"id": "{56299123}", "added": [102], "removed": []}])
        self.assertEqual(d["weights"], [{"champ": 13, "pool": "AH", "before": 150, "after": 200}])
        self.assertEqual(d["prev_version"], "prev")

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


if __name__ == "__main__":
    unittest.main()
