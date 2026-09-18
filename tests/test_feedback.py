from __future__ import annotations

import importlib
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

from aram_nn.site.feedback import (
    FeedbackValidationError,
    insert_feedback,
    normalize_feedback,
)


def feedback_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "category": "feature",
        "feature": "draft",
        "message": "希望可以比較兩個陣容的差異。",
        "impact": "idea",
        "page_path": "/draft/",
        "locale": "zh-Hant",
        "theme": "dark",
        "viewport": "desktop",
        "contact_email": "",
        "contact_consent": False,
        "website": "",
    }
    payload.update(overrides)
    return payload


class FeedbackStorageTests(unittest.TestCase):
    def test_normalize_keeps_only_the_feedback_contract(self) -> None:
        normalized = normalize_feedback(feedback_payload())
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["feature"], "draft")
        self.assertNotIn("website", normalized)
        self.assertNotIn("contact_consent", normalized)

    def test_email_requires_explicit_reply_consent(self) -> None:
        with self.assertRaisesRegex(FeedbackValidationError, "contact_consent"):
            normalize_feedback(
                feedback_payload(contact_email="player@example.com")
            )

        normalized = normalize_feedback(
            feedback_payload(
                contact_email="player@example.com",
                contact_consent=True,
            )
        )
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["contact_email"], "player@example.com")

    def test_honeypot_is_success_shaped_but_not_stored(self) -> None:
        self.assertIsNone(normalize_feedback(feedback_payload(website="bot")))

    def test_insert_creates_private_feedback_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "feedback.db"
            normalized = normalize_feedback(feedback_payload())
            assert normalized is not None
            feedback_id = insert_feedback(
                db,
                normalized,
                created_at="2026-09-12T00:00:00+00:00",
            )
            self.assertEqual(feedback_id, 1)
            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "SELECT category, feature, message, status FROM feedback"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row, ("feature", "draft", "希望可以比較兩個陣容的差異。", "new"))


class FeedbackApiTests(unittest.TestCase):
    def test_post_feedback_and_cors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "feedback.db"
            env = {
                "ARAM_FEEDBACK_DB": str(db),
                "ARAM_FEEDBACK_RATE_LIMIT_PER_HOUR": "10",
                "ARAM_SITE_CORS_ORIGINS": "https://arammeta.com",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                import aram_nn.site.api as api_mod

                importlib.reload(api_mod)
                from fastapi.testclient import TestClient

                client = TestClient(api_mod.app)
                response = client.post(
                    "/api/feedback",
                    json=feedback_payload(
                        contact_email="player@example.com",
                        contact_consent=True,
                    ),
                    headers={"Origin": "https://arammeta.com"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertTrue(body["ok"])
                self.assertTrue(body["accepted"])
                self.assertRegex(body["reference"], r"^F-000001$")
                self.assertEqual(
                    response.headers.get("access-control-allow-origin"),
                    "https://arammeta.com",
                )

                invalid = client.post(
                    "/api/feedback",
                    json=feedback_payload(contact_email="player@example.com"),
                )
                self.assertEqual(invalid.status_code, 400, invalid.text)

            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "SELECT contact_email FROM feedback WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row, ("player@example.com",))

            # Do not let this test's environment-dependent app configuration
            # leak into the rest of the suite.
            importlib.reload(api_mod)


if __name__ == "__main__":
    unittest.main()
