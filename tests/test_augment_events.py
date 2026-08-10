from __future__ import annotations

import json
import unittest

from aram_nn.augment_events import event_id, normalize_event, validate_event, validate_jsonl


def _event(event_type: str = "offer") -> dict:
    return {
        "schema_version": 1,
        "event_type": event_type,
        "event_id": event_id(match_id="m1", player_key="p1", round_index=1, event_type=event_type),
        "match_id": "m1",
        "player_key": "p1",
        "round_index": 1,
        "champion_id": 17,
        "patch": "16.15.1",
        "augment_ids": [1001, 1002, 1003],
        "picked_augment_id": None if event_type == "offer" else 1002,
        "captured_at": "2026-08-03T12:00:00+00:00",
        "source": "overwolf",
    }


class AugmentEventTests(unittest.TestCase):
    def test_valid_offer_and_pick(self) -> None:
        self.assertEqual(validate_event(_event()), [])
        self.assertEqual(validate_event(_event("picked")), [])
        self.assertEqual(normalize_event(_event("picked"))["picked_augment_id"], 1002)


    def test_pick_must_be_in_offer(self) -> None:
        row = _event("picked")
        row["picked_augment_id"] = 9999
        self.assertIn("picked_in_offer", validate_event(row))


    def test_private_identity_is_rejected(self) -> None:
        row = _event()
        row["puuid"] = "not-public"
        self.assertIn("private_identifier_field", validate_event(row))


    def test_raw_bridge_log_reports_missing_join_context(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(json.dumps({"payload": {"event": {"type": "offer", "augments": [{"name": "x"}]}}}) + "\n", encoding="utf-8")
            report = validate_jsonl(str(path))
        self.assertEqual(report["counts"]["lines"], 1)
        self.assertEqual(report["counts"]["invalid_contract"], 1)
        self.assertEqual(report["violations"]["match_id"], 1)
