from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from aram_nn.site.events import EventValidationError, normalize_event


class NormalizeEventTests(unittest.TestCase):
    def test_keeps_only_allowlisted_params(self) -> None:
        event = normalize_event({
            "name": "search_scope_change",
            "params": {"scope": "all", "query": "lux"},
            "locale": "zh-Hant",
            "viewport": "mobile",
            "puuid": "x",
        })
        self.assertEqual(event, {
            "name": "search_scope_change",
            "params": '{"scope":"all"}',
            "locale": "zh-Hant",
            "viewport": "mobile",
        })

    def test_rejects_unknown_event_and_bad_values(self) -> None:
        with self.assertRaises(EventValidationError):
            normalize_event({"name": "page_view"})
        with self.assertRaises(EventValidationError):
            normalize_event({"name": "search_use", "params": {"scope": "items"}})

    def test_unknown_context_is_blanked(self) -> None:
        event = normalize_event({"name": "search_scope_open", "locale": "fr", "viewport": 3})
        self.assertEqual((event["locale"], event["viewport"]), ("", ""))


class EventsApiTests(unittest.TestCase):
    def test_text_plain_beacon_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "events.db"
            env = {"ARAM_EVENTS_DB": str(db), "ARAM_EVENTS_RATE_LIMIT_PER_HOUR": "10"}
            with mock.patch.dict(os.environ, env, clear=False):
                import aram_nn.site.api as api_mod

                importlib.reload(api_mod)
                from fastapi.testclient import TestClient

                client = TestClient(api_mod.app)
                body = json.dumps({"name": "search_use", "params": {"scope": "champions"}})
                ok = client.post(
                    "/api/events",
                    content=body,
                    headers={"Content-Type": "text/plain;charset=UTF-8"},
                )
                self.assertEqual(ok.status_code, 204, ok.text)
                bad = client.post("/api/events", content="not json")
                self.assertEqual(bad.status_code, 400, bad.text)

            con = sqlite3.connect(db)
            try:
                rows = con.execute("SELECT name, params FROM ui_events").fetchall()
            finally:
                con.close()
            self.assertEqual(rows, [("search_use", '{"scope":"champions"}')])

            importlib.reload(api_mod)


if __name__ == "__main__":
    unittest.main()
