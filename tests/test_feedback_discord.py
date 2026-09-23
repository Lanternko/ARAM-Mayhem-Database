from __future__ import annotations

import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from aram_nn.site.feedback import insert_feedback, normalize_feedback, ensure_feedback_schema
from aram_nn.site.feedback_discord import deliver_one, notification_payload, discord_webhook_url

WEBHOOK = "https://discord.com/api/webhooks/123/test-token"


class FeedbackDiscordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "feedback.db"
        payload = normalize_feedback({"message": "Testing @everyone <@123> feedback"})
        self.id = insert_feedback(self.db, payload)

    def state(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            return con.execute("SELECT attempts, next_attempt_at, delivered_at, discord_message_id, last_error FROM feedback_discord_outbox").fetchone()

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_confirmed_delivery_is_not_repeated_and_disables_mentions(self):
        import json
        calls = []

        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(request.url.params["wait"], "true")
            self.assertEqual(body["allowed_mentions"]["parse"], [])
            self.assertIn("@everyone", body["embeds"][0]["description"])
            calls.append(request)
            return httpx.Response(200, json={"id": "98765"})

        client = self.client(handler)
        self.assertTrue(deliver_one(self.db, WEBHOOK, client, now=100))
        self.assertFalse(deliver_one(self.db, WEBHOOK, client, now=200))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state()[2:4], (100, "98765"))

    def test_failure_persists_and_retries_after_new_client(self):
        failed = self.client(lambda request: httpx.Response(503))
        self.assertTrue(deliver_one(self.db, WEBHOOK, failed, now=100))
        self.assertEqual(self.state(), (1, 115, None, None, "http_503"))
        self.assertFalse(deliver_one(self.db, WEBHOOK, failed, now=114))
        success = self.client(lambda request: httpx.Response(200, json={"id": "98765"}))
        self.assertTrue(deliver_one(self.db, WEBHOOK, success, now=115))
        self.assertEqual(self.state()[0], 2)
        self.assertEqual(self.state()[4], None)

    def test_discord_rate_limit_delay(self):
        client = self.client(lambda request: httpx.Response(429, json={"retry_after": 61.5}))
        deliver_one(self.db, WEBHOOK, client, now=100)
        self.assertEqual(self.state()[1], 161.5)
        insert_feedback(self.db, normalize_feedback({"message": "Another pending feedback"}))
        self.assertFalse(deliver_one(self.db, WEBHOOK, client, now=102))

    def test_network_exception_does_not_store_or_log_secret(self):
        def handler(request):
            raise httpx.ConnectError(WEBHOOK, request=request)

        with self.assertLogs("aram_nn.site.feedback_discord", level="WARNING") as logs:
            deliver_one(self.db, WEBHOOK, self.client(handler), now=100)
        self.assertNotIn("test-token", str(logs.output))
        self.assertEqual(self.state()[4], "network")

    def test_inflight_lease_prevents_second_sender_and_recovers_after_expiry(self):
        client = self.client(lambda request: httpx.Response(200, json={"id": "98765"}))
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE feedback_discord_outbox SET next_attempt_at=220")
        self.assertFalse(deliver_one(self.db, WEBHOOK, client, now=219))
        self.assertTrue(deliver_one(self.db, WEBHOOK, client, now=220))

    def test_max_unicode_message_is_complete_and_fits_discord(self):
        message = "😀" * 3000
        body = notification_payload({"id": 1, "message": message, "created_at": "now", "contact_email": "reply@example.com"})
        self.assertEqual("".join(e["description"] for e in body["embeds"]), message)
        lengths = [len(e["description"].encode("utf-16-le")) // 2 for e in body["embeds"]]
        self.assertLessEqual(max(lengths), 4096)
        self.assertLessEqual(sum(lengths), 6000)
        self.assertIn("reply@example.com", body["content"])

    def test_schema_upgrade_does_not_replay_old_feedback(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DROP TABLE feedback_discord_outbox")
            ensure_feedback_schema(con)
            self.assertEqual(con.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM feedback_discord_outbox").fetchone()[0], 0)

    def test_outbox_insert_failure_rolls_back_feedback(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON feedback_discord_outbox BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_feedback(self.db, normalize_feedback({"message": "Should roll back"}))
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)

    def test_private_file_configuration_and_sanitized_validation(self):
        secret = Path(self.tmp.name) / "webhook.txt"
        secret.write_text(WEBHOOK, encoding="utf-8")
        with patch.dict(os.environ, {"ARAM_FEEDBACK_DISCORD_WEBHOOK_URL": "", "ARAM_FEEDBACK_DISCORD_WEBHOOK_FILE": str(secret)}):
            self.assertEqual(discord_webhook_url(), WEBHOOK)
            secret.write_text("https://example.com/secret", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid feedback Discord webhook configuration"):
                discord_webhook_url()


if __name__ == "__main__":
    unittest.main()
