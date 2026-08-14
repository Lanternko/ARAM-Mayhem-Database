from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa

from aram_nn.site.player_history_rate_limit import (
    SQLiteFixedWindowRateLimiter,
    canonical_ip,
)
from aram_nn.site.player_history_security import (
    NORMALIZER_ID,
    decrypt_candidate,
    normalize_riot_id_v1,
    rsa_public_key_id,
)
from aram_nn.site.player_seed_quarantine import CandidateQuarantineStore


RATE_SECRET = bytes(range(160, 192))
CANDIDATE_SECRET = bytes(range(96, 128))


class PlayerHistoryRateLimitTests(unittest.TestCase):
    def test_persists_across_instances_restart_and_never_stores_raw_ip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rate.sqlite"
            first = SQLiteFixedWindowRateLimiter(path, RATE_SECRET, limit=2)
            second = SQLiteFixedWindowRateLimiter(path, RATE_SECRET, limit=2)
            self.assertEqual(first.charge("203.0.113.7", now_ms=1_000), (True, 3599))
            self.assertEqual(second.charge("203.0.113.7", now_ms=2_000)[0], True)
            restarted = SQLiteFixedWindowRateLimiter(path, RATE_SECRET, limit=2)
            allowed, retry = restarted.charge("203.0.113.7", now_ms=3_000)
            self.assertFalse(allowed)
            self.assertGreater(retry, 0)
            self.assertNotIn(b"203.0.113.7", path.read_bytes())

    def test_lock_capacity_and_corruption_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rate.sqlite"
            limiter = SQLiteFixedWindowRateLimiter(path, RATE_SECRET, capacity=1)
            self.assertTrue(limiter.charge("192.0.2.1", now_ms=1)[0])
            self.assertFalse(limiter.charge("192.0.2.2", now_ms=1)[0])
            blocker = sqlite3.connect(path)
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                self.assertFalse(limiter.charge("192.0.2.1", now_ms=2)[0])
            finally:
                blocker.rollback()
                blocker.close()
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE player_history_rate_limit")
            connection.commit()
            connection.close()
            self.assertFalse(limiter.charge("192.0.2.1", now_ms=3)[0])

    def test_canonical_ip_rejects_spelling_ports_zones_and_lists(self) -> None:
        self.assertEqual(canonical_ip("2001:db8::1"), "2001:db8::1")
        for value in ("2001:0db8::1", "1.2.3.4:80", "fe80::1%3", "1.2.3.4,5.6.7.8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_ip(value)


class CandidateQuarantineStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)

    def test_encrypts_candidate_and_test_private_key_can_decrypt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quarantine.sqlite"
            store = CandidateQuarantineStore(
                path,
                CANDIDATE_SECRET,
                self.private_key.public_key(),
                dataset_id="tw-16.16",
                normalizer_id=NORMALIZER_ID,
            )
            normalized = normalize_riot_id_v1("Unknown#TW01")
            self.assertTrue(store.admit(normalized, now_ms=1000))
            connection = sqlite3.connect(path)
            envelope = connection.execute(
                "SELECT ciphertext FROM player_seed_quarantine"
            ).fetchone()[0]
            connection.close()
            key_id = rsa_public_key_id(self.private_key.public_key())
            plaintext = decrypt_candidate(
                envelope,
                private_key=self.private_key,
                allowed_key_ids={key_id: self.private_key},
                expected_dataset_id="tw-16.16",
                expected_normalizer_id=NORMALIZER_ID,
            )
            self.assertEqual(plaintext.normalized_riot_id, normalized)
            self.assertNotIn(normalized, path.read_bytes())

    def test_cap_ttl_and_lock_are_bounded_silent_drops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quarantine.sqlite"
            store = CandidateQuarantineStore(
                path,
                CANDIDATE_SECRET,
                self.private_key.public_key(),
                dataset_id="tw-16.16",
                normalizer_id=NORMALIZER_ID,
                capacity=1,
            )
            self.assertTrue(store.admit(normalize_riot_id_v1("First#TW01"), now_ms=1))
            self.assertFalse(store.admit(normalize_riot_id_v1("Second#TW01"), now_ms=2))
            thirty_one_days = 31 * 24 * 60 * 60 * 1000
            self.assertTrue(
                store.admit(normalize_riot_id_v1("Second#TW01"), now_ms=thirty_one_days)
            )
            blocker = sqlite3.connect(path)
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                self.assertFalse(
                    store.admit(normalize_riot_id_v1("Third#TW01"), now_ms=thirty_one_days + 1)
                )
            finally:
                blocker.rollback()
                blocker.close()


if __name__ == "__main__":
    unittest.main()
