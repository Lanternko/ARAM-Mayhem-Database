from __future__ import annotations

import json
import unittest

from aram_nn.lcu.poller import _build_participant_record
from aram_nn.site.db import _assert_no_private_identifiers


class SummonerSpellCaptureTests(unittest.TestCase):
    def test_match_history_shape_captures_sorted_spells(self) -> None:
        # /lol-match-history participant: spell ids at the top level.
        raw = {"teamId": 100, "championId": 268, "spell1Id": 4, "spell2Id": 32}
        record = _build_participant_record(100, 268, raw)
        # Sorted ascending like champions — Mark/Dash (32) never leads.
        self.assertEqual(record["spells"], [4, 32])

    def test_eog_shape_captures_spells(self) -> None:
        # End-of-game player object carries spell ids alongside nested stats.
        raw = {"championId": 91, "spell1Id": 32, "spell2Id": 7, "stats": {"item0": 6694}}
        record = _build_participant_record(200, 91, raw)
        self.assertEqual(record["spells"], [7, 32])

    def test_public_api_aliases_are_accepted(self) -> None:
        raw = {"summoner1Id": 6, "summoner2Id": 32}
        record = _build_participant_record(100, 1, raw)
        self.assertEqual(record["spells"], [6, 32])

    def test_missing_spells_omits_the_key(self) -> None:
        record = _build_participant_record(100, 1, {"stats": {}})
        self.assertNotIn("spells", record)

    def test_zero_spell_ids_are_dropped(self) -> None:
        raw = {"spell1Id": 0, "spell2Id": 14}
        record = _build_participant_record(100, 1, raw)
        self.assertEqual(record["spells"], [14])

    def test_spells_pass_public_sync_validation(self) -> None:
        # Spell ids are integers — they must not trip the PII denylist.
        record = _build_participant_record(100, 268, {"spell1Id": 4, "spell2Id": 32})
        payload = json.dumps([record], ensure_ascii=False)
        _assert_no_private_identifiers(payload)  # raises on violation


if __name__ == "__main__":
    unittest.main()
