from __future__ import annotations

import base64
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aram_nn.site.player_history_security import (
    MAX_CIPHERTEXT_BYTES,
    MAX_ENVELOPE_BYTES,
    MAX_RSA_KEY_BITS,
    NORMALIZER_ID,
    CandidateEnvelope,
    PlayerHistorySecurityError,
    decrypt_candidate,
    derive_candidate_key,
    derive_event_key,
    derive_lookup_key,
    derive_player_key,
    encode_candidate_envelope,
    encrypt_candidate,
    frame_v1,
    hmac_key_id,
    normalize_riot_id_v1,
    parse_candidate_envelope,
    parse_frame_v1,
    rsa_public_key_id,
    validate_role_secrets,
    validate_sqlite_key,
)
from aram_nn.site import player_history_security as security
from aram_nn.site.player_seed_quarantine import (
    CREATE_CANDIDATE_QUARANTINE_SQL,
    ClaimHandle,
    QuarantineInvariantError,
    abandon_stale,
    claim_next,
    ensure_quarantine_schema,
    terminalize,
    upsert_candidate,
)


LOOKUP_SECRET = bytes(range(0, 32))
PLAYER_SECRET = bytes(range(32, 64))
EVENT_SECRET = bytes(range(64, 96))
CANDIDATE_SECRET = bytes(range(96, 128))
DATASET_ID = "tw-16.14"
NORMALIZED = b"alice#tw1"


class NormalizerTests(unittest.TestCase):
    def test_fixed_conformance_cases(self) -> None:
        self.assertEqual(NORMALIZER_ID, "nfkc-casefold-v1-u15.1.0")
        cases = {
            "Alice#TW1": b"alice#tw1",
            "\uff21\uff4c\uff49\uff43\uff45#\uff34\uff37\uff11": b"alice#tw1",
            "Cafe\u0301#TW1": "caf\u00e9#tw1".encode(),
            "Stra\u00dfe#EUW": b"strasse#euw",
            "\u73a9\u5bb6\u540d\u7a31#\u53f0\u70631": "\u73a9\u5bb6\u540d\u7a31#\u53f0\u70631".encode(),
            "abc def#123": b"abc def#123",
            "abcdefghijklmnop#12345": b"abcdefghijklmnop#12345",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_riot_id_v1(raw), expected)

    def test_adversarial_inputs_fail_with_fixed_non_echoing_codes(self) -> None:
        invalid = [
            None,
            b"alice#tw1",
            "ab#123",
            ("a" * 124) + "#1234",
            "alice",
            "alice#tw1#x",
            " alice#tw1",
            "alice#tw1 ",
            "alice #tw1",
            "alice# tw1",
            "ali\u00a0ce#tw1",
            "alice#t w1",
            "alice#t-w",
            "alice#tw!",
            "alice#tw",
            "alice#123456",
            "ab\u200bcd#tw1",
            "ab\x00cd#tw1",
            "ab\ue000cd#tw1",
            "ab\u0378cd#tw1",
            "ab\u05d0cd#tw1",
            "ab\u202ecd#tw1",
            "ab\ud800cd#tw1",
        ]
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(PlayerHistorySecurityError) as caught:
                normalize_riot_id_v1(value)  # type: ignore[arg-type]
            self.assertNotIn("alice", str(caught.exception).lower())
            self.assertRegex(caught.exception.code, r"^[a-z_]+$")

    def test_every_derivation_rejects_runtime_normalizer_mismatch(self) -> None:
        wrong = NORMALIZER_ID + "-other"
        calls = [
            lambda: derive_lookup_key(LOOKUP_SECRET, expected_normalizer_id=wrong, normalized_riot_id=NORMALIZED),
            lambda: derive_player_key(PLAYER_SECRET, expected_normalizer_id=wrong, player_local_id="p1"),
            lambda: derive_event_key(EVENT_SECRET, expected_normalizer_id=wrong, player_local_id="p1", game_id=1),
            lambda: derive_candidate_key(CANDIDATE_SECRET, expected_normalizer_id=wrong, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED),
        ]
        for call in calls:
            with self.assertRaisesRegex(PlayerHistorySecurityError, "^normalizer_mismatch$"):
                call()


class FrameAndHmacTests(unittest.TestCase):
    def test_frame_is_exact_and_parser_rejects_domain_or_shape_mismatch(self) -> None:
        expected = bytes.fromhex(
            "6172616d6d6574612d7068000100066c6f6f6b7570"
            "0000000161000000026263"
        )
        actual = frame_v1("lookup", (b"a", b"bc"))
        self.assertEqual(actual, expected)
        self.assertEqual(parse_frame_v1(actual, expected_domain="lookup", expected_part_count=2), (b"a", b"bc"))
        with self.assertRaises(PlayerHistorySecurityError):
            parse_frame_v1(actual, expected_domain="player", expected_part_count=2)
        with self.assertRaises(PlayerHistorySecurityError):
            parse_frame_v1(actual, expected_domain="lookup", expected_part_count=1)
        with self.assertRaises(PlayerHistorySecurityError):
            frame_v1("other", (b"a",))

    def test_fixed_full_hmac_known_answer_vectors_for_all_roles(self) -> None:
        self.assertEqual(
            derive_lookup_key(LOOKUP_SECRET, expected_normalizer_id=NORMALIZER_ID, normalized_riot_id=NORMALIZED).hex(),
            "6a8d41b94243c5e07653e82b47fb712b5d44fee8f29f97ce84a42e84f8be1cc9",
        )
        self.assertEqual(
            derive_player_key(PLAYER_SECRET, expected_normalizer_id=NORMALIZER_ID, player_local_id="local-\u73a9\u5bb6").hex(),
            "a4d99548315cec3895e8537281e862ebd3ee70932abbefb487fa2a7287800ad2",
        )
        self.assertEqual(
            derive_event_key(EVENT_SECRET, expected_normalizer_id=NORMALIZER_ID, player_local_id="local-\u73a9\u5bb6", game_id=1234567890).hex(),
            "4307810be54b03b27183742e7aa6fecfaff07e0323c1714168391e9f98e0dcb9",
        )
        self.assertEqual(
            derive_candidate_key(CANDIDATE_SECRET, expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED).hex(),
            "b17a17caea6da338a5b9aaf480ec95eb67a17dee323c870b8ca314b8d95adef0",
        )
        self.assertEqual(hmac_key_id(LOOKUP_SECRET), "630dcd2966c4336691125448bbb25b4f")

    def test_secret_roles_are_pairwise_distinct_and_keys_are_raw_blobs(self) -> None:
        validate_role_secrets(LOOKUP_SECRET, PLAYER_SECRET, EVENT_SECRET, CANDIDATE_SECRET)
        with self.assertRaises(PlayerHistorySecurityError):
            validate_role_secrets(LOOKUP_SECRET, LOOKUP_SECRET, EVENT_SECRET, CANDIDATE_SECRET)
        with self.assertRaises(PlayerHistorySecurityError):
            validate_role_secrets(b"short", PLAYER_SECRET, EVENT_SECRET, CANDIDATE_SECRET)
        key = derive_lookup_key(LOOKUP_SECRET, expected_normalizer_id=NORMALIZER_ID, normalized_riot_id=NORMALIZED)
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE keys (value BLOB)")
            connection.execute("INSERT INTO keys VALUES (?)", (key,))
            roundtrip = connection.execute("SELECT value FROM keys").fetchone()[0]
        self.assertEqual(validate_sqlite_key(roundtrip), key)
        for invalid in (key.hex(), base64.b64encode(key).decode(), key[:-1], bytearray(key), memoryview(key)):
            with self.assertRaises(PlayerHistorySecurityError):
                validate_sqlite_key(invalid)

    def test_concat_domain_and_encoding_are_not_interchangeable(self) -> None:
        a = derive_event_key(EVENT_SECRET, expected_normalizer_id=NORMALIZER_ID, player_local_id="ab", game_id=12)
        b = derive_event_key(EVENT_SECRET, expected_normalizer_id=NORMALIZER_ID, player_local_id="ab1", game_id=2)
        self.assertNotEqual(a, b)
        lookup = derive_lookup_key(LOOKUP_SECRET, expected_normalizer_id=NORMALIZER_ID, normalized_riot_id=NORMALIZED)
        player = derive_player_key(LOOKUP_SECRET, expected_normalizer_id=NORMALIZER_ID, player_local_id="alice#tw1")
        self.assertNotEqual(lookup, player)
        self.assertNotEqual(normalize_riot_id_v1("\uff21lice#TW1"), "Alice#TW1".encode())


class CandidateEnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.key_id = rsa_public_key_id(cls.private_key.public_key())
        cls.allowed = {cls.key_id: cls.private_key}

    def test_exact_envelope_json_codec(self) -> None:
        envelope = CandidateEnvelope(1, "RSA-OAEP-SHA256", "a" * 32, b"\x00\xfb\xff")
        encoded = encode_candidate_envelope(envelope)
        self.assertEqual(
            encoded,
            b'{"v":1,"alg":"RSA-OAEP-SHA256","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"APv_"}',
        )
        self.assertEqual(parse_candidate_envelope(encoded), envelope)
        invalid = [
            b"{}",
            b'{"v":1,"alg":"RSA-OAEP-SHA256","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"AA","extra":1}',
            b'{"v":true,"alg":"RSA-OAEP-SHA256","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"AA"}',
            b'{"v":1,"alg":"rsa","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"AA"}',
            b'{"v":1,"alg":"RSA-OAEP-SHA256","key_id":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","ciphertext":"AA"}',
            b'{"v":1,"alg":"RSA-OAEP-SHA256","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"AA=="}',
            b'{"v":1,"v":1,"alg":"RSA-OAEP-SHA256","key_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ciphertext":"AA"}',
            b"\xff",
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(PlayerHistorySecurityError):
                parse_candidate_envelope(raw)

    def test_rsa_key_size_has_exact_lower_and_upper_bounds(self) -> None:
        for key_type, validator in (
            (rsa.RSAPublicKey, security._validate_public_key),
            (rsa.RSAPrivateKey, security._validate_private_key),
        ):
            for bits, error in (
                (3071, "rsa_key_too_small"),
                (3072, None),
                (MAX_RSA_KEY_BITS, None),
                (MAX_RSA_KEY_BITS + 1, "rsa_key_too_large"),
            ):
                with self.subTest(key_type=key_type.__name__, bits=bits):
                    key = mock.Mock(spec=key_type)
                    key.key_size = bits
                    if error is None:
                        self.assertIs(validator(key), key)
                    else:
                        with self.assertRaisesRegex(PlayerHistorySecurityError, f"^{error}$"):
                            validator(key)

    def test_ciphertext_size_has_exact_bounds_before_and_after_base64(self) -> None:
        for length in (MAX_CIPHERTEXT_BYTES - 1, MAX_CIPHERTEXT_BYTES):
            with self.subTest(length=length):
                envelope = CandidateEnvelope(
                    1, "RSA-OAEP-SHA256", "a" * 32, b"x" * length
                )
                self.assertEqual(parse_candidate_envelope(encode_candidate_envelope(envelope)), envelope)
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^invalid_ciphertext$"):
            encode_candidate_envelope(
                CandidateEnvelope(
                    1,
                    "RSA-OAEP-SHA256",
                    "a" * 32,
                    b"x" * (MAX_CIPHERTEXT_BYTES + 1),
                )
            )
        oversized_b64 = json.dumps(
            {
                "v": 1,
                "alg": "RSA-OAEP-SHA256",
                "key_id": "a" * 32,
                "ciphertext": "A" * 1367,
            },
            separators=(",", ":"),
        ).encode()
        with mock.patch.object(security.base64, "b64decode") as decoder:
            with self.assertRaisesRegex(PlayerHistorySecurityError, "^invalid_ciphertext$"):
                parse_candidate_envelope(oversized_b64)
            decoder.assert_not_called()

    def test_envelope_size_has_exact_bounds_and_rejects_before_parsing(self) -> None:
        encoded = encode_candidate_envelope(
            CandidateEnvelope(1, "RSA-OAEP-SHA256", "a" * 32, b"x")
        )
        for length in (MAX_ENVELOPE_BYTES - 1, MAX_ENVELOPE_BYTES):
            with self.subTest(length=length):
                padded = encoded + b" " * (length - len(encoded))
                self.assertEqual(parse_candidate_envelope(padded).ciphertext, b"x")
        oversized = encoded + b" " * (MAX_ENVELOPE_BYTES + 1 - len(encoded))
        with (
            mock.patch.object(security.json, "loads") as json_loads,
            mock.patch.object(security.base64, "b64decode") as decoder,
            self.assertRaisesRegex(PlayerHistorySecurityError, "^invalid_envelope$"),
        ):
            parse_candidate_envelope(oversized)
        json_loads.assert_not_called()
        decoder.assert_not_called()
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^invalid_envelope$"):
            parse_candidate_envelope(b"")

    def test_rsa_roundtrip_is_randomized_and_public_key_cannot_decrypt(self) -> None:
        first = encrypt_candidate(self.private_key.public_key(), expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED)
        second = encrypt_candidate(self.private_key.public_key(), expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED)
        self.assertNotEqual(parse_candidate_envelope(first).ciphertext, parse_candidate_envelope(second).ciphertext)
        plaintext = decrypt_candidate(first, private_key=self.private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        self.assertEqual(plaintext.normalized_riot_id, NORMALIZED)
        with self.assertRaises(PlayerHistorySecurityError):
            decrypt_candidate(first, private_key=self.private_key.public_key(), allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)  # type: ignore[arg-type]

    def test_unknown_retired_swapped_corrupt_and_small_keys_fail_closed(self) -> None:
        envelope_bytes = encrypt_candidate(self.private_key.public_key(), expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED)
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^key_not_allowed$"):
            decrypt_candidate(envelope_bytes, private_key=self.private_key, allowed_key_ids={}, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^key_mismatch$"):
            decrypt_candidate(envelope_bytes, private_key=self.private_key, allowed_key_ids={self.key_id: self.other_private_key}, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^key_mismatch$"):
            decrypt_candidate(envelope_bytes, private_key=self.other_private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        parsed = parse_candidate_envelope(envelope_bytes)
        corrupt = encode_candidate_envelope(CandidateEnvelope(parsed.v, parsed.alg, parsed.key_id, bytes([parsed.ciphertext[0] ^ 1]) + parsed.ciphertext[1:]))
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^decrypt_failed$"):
            decrypt_candidate(corrupt, private_key=self.private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^rsa_key_too_small$"):
            encrypt_candidate(small.public_key(), expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED)

    def _wrap_plaintext(self, plaintext: bytes) -> bytes:
        ciphertext = self.private_key.public_key().encrypt(
            plaintext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=b"arammeta-ph-candidate-v1:" + self.key_id.encode(),
            ),
        )
        return encode_candidate_envelope(CandidateEnvelope(1, "RSA-OAEP-SHA256", self.key_id, ciphertext))

    def test_plaintext_frame_dataset_and_normalizer_are_validated(self) -> None:
        wrong_domain = self._wrap_plaintext(frame_v1("candidate", (DATASET_ID.encode(), NORMALIZER_ID.encode(), NORMALIZED)))
        with self.assertRaises(PlayerHistorySecurityError):
            decrypt_candidate(wrong_domain, private_key=self.private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        wrong_dataset = self._wrap_plaintext(frame_v1("candidate-plaintext", (b"other", NORMALIZER_ID.encode(), NORMALIZED)))
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^dataset_mismatch$"):
            decrypt_candidate(wrong_dataset, private_key=self.private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)
        wrong_normalizer = self._wrap_plaintext(frame_v1("candidate-plaintext", (DATASET_ID.encode(), b"other", NORMALIZED)))
        with self.assertRaisesRegex(PlayerHistorySecurityError, "^normalizer_mismatch$"):
            decrypt_candidate(wrong_normalizer, private_key=self.private_key, allowed_key_ids=self.allowed, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)


class QuarantineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = CandidateEnvelopeTests.private_key
        cls.key_id = CandidateEnvelopeTests.key_id
        cls.envelope = encrypt_candidate(cls.private_key.public_key(), expected_normalizer_id=NORMALIZER_ID, dataset_id=DATASET_ID, normalized_riot_id=NORMALIZED)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "quarantine.db"
        self.connection = sqlite3.connect(self.db_path, timeout=30.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        ensure_quarantine_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def _insert(self, index: int, *, created_ms: int | None = None) -> bytes:
        key = index.to_bytes(32, "big")
        inserted = upsert_candidate(
            self.connection,
            candidate_key=key,
            dataset_id=DATASET_ID,
            normalizer_id=NORMALIZER_ID,
            key_id=self.key_id,
            envelope=self.envelope,
            created_ms=index if created_ms is None else created_ms,
        )
        self.assertTrue(inserted)
        return key

    def test_schema_creation_is_canonical_and_reauditable(self) -> None:
        ensure_quarantine_schema(self.connection)
        ensure_quarantine_schema(self.connection)
        self.assertEqual(
            self.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall(),
            [("player_seed_quarantine",)],
        )

    def test_schema_drift_and_temp_objects_fail_closed(self) -> None:
        drift_ddls = {
            "missing_check": CREATE_CANDIDATE_QUARANTINE_SQL.replace(
                " CHECK(created_ms >= 0)", "", 1
            ),
            "missing_unique": CREATE_CANDIDATE_QUARANTINE_SQL.replace(
                "    UNIQUE(candidate_key, dataset_id),\n", "", 1
            ),
        }
        for name, ddl in drift_ddls.items():
            with self.subTest(name=name), contextlib.closing(sqlite3.connect(":memory:")) as connection:
                connection.execute(ddl)
                connection.commit()
                with self.assertRaisesRegex(QuarantineInvariantError, "^schema_mismatch$"):
                    ensure_quarantine_schema(connection)

        for extra_sql in (
            "CREATE TABLE extra (value INTEGER)",
            "CREATE TRIGGER extra AFTER INSERT ON player_seed_quarantine BEGIN SELECT 1; END",
            "CREATE TEMP TABLE extra (value INTEGER)",
        ):
            with self.subTest(extra_sql=extra_sql), contextlib.closing(sqlite3.connect(":memory:")) as connection:
                connection.execute(CREATE_CANDIDATE_QUARANTINE_SQL)
                connection.execute(extra_sql)
                connection.commit()
                with self.assertRaisesRegex(QuarantineInvariantError, "^schema_mismatch$"):
                    ensure_quarantine_schema(connection)

    def test_failed_schema_audit_rolls_back_owned_creation(self) -> None:
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TEMP TABLE extra (value INTEGER)")
            with self.assertRaisesRegex(QuarantineInvariantError, "^schema_mismatch$"):
                ensure_quarantine_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM main.sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall(),
                [],
            )

    def test_connection_guard_rejects_non_sqlite_closed_and_ambient_transactions(self) -> None:
        with self.assertRaisesRegex(QuarantineInvariantError, "^invalid_connection$"):
            ensure_quarantine_schema(object())  # type: ignore[arg-type]
        closed = sqlite3.connect(":memory:")
        closed.close()
        with self.assertRaisesRegex(QuarantineInvariantError, "^invalid_connection$"):
            ensure_quarantine_schema(closed)

        handle = ClaimHandle(b"h" * 32, DATASET_ID)
        calls = (
            lambda connection: ensure_quarantine_schema(connection),
            lambda connection: upsert_candidate(
                connection,
                candidate_key=b"u" * 32,
                dataset_id=DATASET_ID,
                normalizer_id=NORMALIZER_ID,
                key_id=self.key_id,
                envelope=self.envelope,
                created_ms=1,
            ),
            lambda connection: claim_next(connection, taken_ms=1),
            lambda connection: terminalize(
                connection, handle, target="promoted", terminal_ms=1
            ),
            lambda connection: abandon_stale(
                connection, handle, stale_before_ms=1, abandoned_ms=1
            ),
        )
        for call in calls:
            with self.subTest(call=repr(call)), contextlib.closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("CREATE TABLE sentinel (value INTEGER)")
                connection.commit()
                connection.execute("INSERT INTO sentinel VALUES (1)")
                with self.assertRaisesRegex(QuarantineInvariantError, "^transaction_active$"):
                    call(connection)
                self.assertTrue(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchall(), [(1,)])
                connection.rollback()
                self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchall(), [])

    def test_terminal_reason_allowlist_is_exact(self) -> None:
        allowed = {
            "promoted": (None,),
            "rejected": (None, "policy", "invalid_candidate"),
            "dead": (None, "decrypt_failed", "key_retired", "dataset_mismatch"),
        }
        index = 1000
        for target, reasons in allowed.items():
            for reason in reasons:
                with self.subTest(target=target, reason=reason):
                    key = self._insert(index)
                    index += 1
                    claimed = claim_next(self.connection, taken_ms=index)
                    self.assertTrue(
                        terminalize(
                            self.connection,
                            claimed.handle,  # type: ignore[union-attr]
                            target=target,
                            terminal_ms=index + 1,
                            terminal_reason=reason,
                        )
                    )
        for reason in (None, "stale_claim"):
            with self.subTest(target="abandoned", reason=reason):
                key = self._insert(index)
                index += 1
                claimed = claim_next(self.connection, taken_ms=index)
                self.assertTrue(
                    abandon_stale(
                        self.connection,
                        claimed.handle,  # type: ignore[union-attr]
                        stale_before_ms=index,
                        abandoned_ms=index + 1,
                        terminal_reason=reason,
                    )
                )

    def test_invalid_terminal_reasons_fail_before_missing_table_sql(self) -> None:
        handle = ClaimHandle(b"r" * 32, DATASET_ID)
        allowed = {
            "promoted": {None},
            "rejected": {None, "policy", "invalid_candidate"},
            "dead": {None, "decrypt_failed", "key_retired", "dataset_mismatch"},
            "abandoned": {None, "stale_claim"},
        }
        all_codes = set().union(*allowed.values())
        invalid = [(target, reason) for target, reasons in allowed.items() for reason in all_codes - reasons]
        invalid.extend(
            (
                ("pending", None),
                (1, None),
                ("rejected", 1),
                ("rejected", "x" * 10_000),
                ("rejected", "alice#tw1"),
            )
        )
        for target, reason in invalid:
            with self.subTest(target=target, reason=reason), contextlib.closing(sqlite3.connect(":memory:")) as connection:
                with self.assertRaises(PlayerHistorySecurityError):
                    terminalize(
                        connection,
                        handle,
                        target=target,  # type: ignore[arg-type]
                        terminal_ms=1,
                        terminal_reason=reason,  # type: ignore[arg-type]
                    )
                self.assertFalse(connection.in_transaction)
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            for reason in all_codes - allowed["abandoned"] | {"x" * 10_000, "alice#tw1", 1}:
                with self.subTest(abandon_reason=reason), self.assertRaises(PlayerHistorySecurityError):
                    abandon_stale(
                        connection,
                        handle,
                        stale_before_ms=1,
                        abandoned_ms=1,
                        terminal_reason=reason,  # type: ignore[arg-type]
                    )
                self.assertFalse(connection.in_transaction)

    def test_order_upsert_noop_and_raw_blob_constraint(self) -> None:
        key2 = self._insert(2, created_ms=1)
        key1 = self._insert(1, created_ms=1)
        key3 = self._insert(3, created_ms=0)
        self.assertFalse(upsert_candidate(self.connection, candidate_key=key3, dataset_id=DATASET_ID, normalizer_id=NORMALIZER_ID, key_id=self.key_id, envelope=self.envelope, created_ms=999))
        self.assertEqual(claim_next(self.connection, taken_ms=10).handle.candidate_key, key3)  # type: ignore[union-attr]
        self.assertEqual(claim_next(self.connection, taken_ms=11).handle.candidate_key, key1)  # type: ignore[union-attr]
        self.assertEqual(claim_next(self.connection, taken_ms=12).handle.candidate_key, key2)  # type: ignore[union-attr]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO player_seed_quarantine (candidate_key,dataset_id,normalizer_id,key_id,ciphertext,state,created_ms,updated_ms,attempts) VALUES (?,?,?,?,?,'pending',0,0,0)",
                ("0" * 32, DATASET_ID, NORMALIZER_ID, self.key_id, self.envelope),
            )

    def test_hundred_thread_contention_is_at_most_once(self) -> None:
        for index in range(1, 101):
            self._insert(index, created_ms=index)

        barrier = threading.Barrier(100)

        def worker(worker_id: int) -> bytes | None:
            connection = sqlite3.connect(self.db_path, timeout=30.0)
            connection.execute("PRAGMA busy_timeout=30000")
            try:
                barrier.wait(timeout=10)
                claimed = claim_next(connection, taken_ms=1000 + worker_id)
                return None if claimed is None else claimed.handle.candidate_key
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=100) as pool:
            claimed_keys = list(pool.map(worker, range(100)))
        self.assertEqual(len([key for key in claimed_keys if key is not None]), 100)
        self.assertEqual(len(set(claimed_keys)), 100)
        rows = self.connection.execute("SELECT state,ciphertext,attempts FROM player_seed_quarantine").fetchall()
        self.assertTrue(all(state == "taken" and ciphertext is None and attempts == 1 for state, ciphertext, attempts in rows))

    def test_corrupt_claim_can_dead_exact_handle_and_leave_unrelated_row(self) -> None:
        parsed = parse_candidate_envelope(self.envelope)
        corrupt = encode_candidate_envelope(CandidateEnvelope(1, parsed.alg, parsed.key_id, bytes([parsed.ciphertext[0] ^ 1]) + parsed.ciphertext[1:]))
        bad_key = (10).to_bytes(32, "big")
        good_key = (11).to_bytes(32, "big")
        upsert_candidate(self.connection, candidate_key=bad_key, dataset_id=DATASET_ID, normalizer_id=NORMALIZER_ID, key_id=self.key_id, envelope=corrupt, created_ms=0)
        upsert_candidate(self.connection, candidate_key=good_key, dataset_id=DATASET_ID, normalizer_id=NORMALIZER_ID, key_id=self.key_id, envelope=self.envelope, created_ms=1)
        claimed = claim_next(self.connection, taken_ms=2)
        self.assertEqual(claimed.handle, ClaimHandle(bad_key, DATASET_ID))  # type: ignore[union-attr]
        with self.assertRaises(PlayerHistorySecurityError):
            decrypt_candidate(claimed.envelope, private_key=self.private_key, allowed_key_ids={self.key_id: self.private_key}, expected_dataset_id=DATASET_ID, expected_normalizer_id=NORMALIZER_ID)  # type: ignore[union-attr]
        self.assertTrue(terminalize(self.connection, claimed.handle, target="dead", terminal_ms=3, terminal_reason="decrypt_failed"))  # type: ignore[union-attr]
        states = dict(self.connection.execute("SELECT candidate_key,state FROM player_seed_quarantine"))
        self.assertEqual(states, {bad_key: "dead", good_key: "pending"})

    def test_illegal_and_terminal_transitions_do_not_reset(self) -> None:
        key = self._insert(20)
        handle = ClaimHandle(key, DATASET_ID)
        self.assertFalse(terminalize(self.connection, handle, target="promoted", terminal_ms=21))
        with self.assertRaises(PlayerHistorySecurityError):
            terminalize(self.connection, handle, target="pending", terminal_ms=21)
        claimed = claim_next(self.connection, taken_ms=22)
        self.assertTrue(terminalize(self.connection, claimed.handle, target="promoted", terminal_ms=23))  # type: ignore[union-attr]
        self.assertFalse(terminalize(self.connection, claimed.handle, target="rejected", terminal_ms=24))  # type: ignore[union-attr]
        self.assertFalse(upsert_candidate(self.connection, candidate_key=key, dataset_id=DATASET_ID, normalizer_id=NORMALIZER_ID, key_id=self.key_id, envelope=self.envelope, created_ms=25))
        row = self.connection.execute("SELECT state,ciphertext,attempts FROM player_seed_quarantine WHERE candidate_key=?", (key,)).fetchone()
        self.assertEqual(row, ("promoted", None, 1))

    def test_crash_contract_leaves_taken_null_then_exact_stale_abandon(self) -> None:
        first_key = self._insert(30, created_ms=1)
        second_key = self._insert(31, created_ms=2)
        claimed = claim_next(self.connection, taken_ms=100)
        self.assertEqual(claimed.handle.candidate_key, first_key)  # type: ignore[union-attr]
        row = self.connection.execute("SELECT state,ciphertext FROM player_seed_quarantine WHERE candidate_key=?", (first_key,)).fetchone()
        self.assertEqual(row, ("taken", None))
        self.assertFalse(abandon_stale(self.connection, claimed.handle, stale_before_ms=99, abandoned_ms=200))  # type: ignore[union-attr]
        self.assertTrue(abandon_stale(self.connection, claimed.handle, stale_before_ms=100, abandoned_ms=201))  # type: ignore[union-attr]
        states = dict(self.connection.execute("SELECT candidate_key,state FROM player_seed_quarantine"))
        self.assertEqual(states, {first_key: "abandoned", second_key: "pending"})

    def test_operations_emit_no_identifier_secret_or_ciphertext(self) -> None:
        fixture_id = "fixture-player-sensitive-9981"
        secret_text = LOOKUP_SECRET.hex()
        ciphertext_text = base64.urlsafe_b64encode(parse_candidate_envelope(self.envelope).ciphertext).decode()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            normalized = normalize_riot_id_v1("FixturePlayer#TW1")
            derive_lookup_key(LOOKUP_SECRET, expected_normalizer_id=NORMALIZER_ID, normalized_riot_id=normalized)
            key = self._insert(40)
            claimed = claim_next(self.connection, taken_ms=41)
            terminalize(self.connection, claimed.handle, target="rejected", terminal_ms=42, terminal_reason="policy")  # type: ignore[union-attr]
        captured = stdout.getvalue() + stderr.getvalue()
        for forbidden in (fixture_id, secret_text, ciphertext_text):
            self.assertNotIn(forbidden, captured)


if __name__ == "__main__":
    unittest.main()
