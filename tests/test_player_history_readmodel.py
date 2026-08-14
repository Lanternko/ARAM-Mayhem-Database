import contextlib
import inspect
import io
import json
import sqlite3
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from types import MappingProxyType
from typing import Literal

from aram_nn.site.player_history_readmodel import (
    PlayerHistoryGraphV1,
    PlayerHistoryGraphValidationError,
    PlayerHistoryRecordV1,
    PlayerHistoryReadModelError,
    PlayerHistoryReadModelSchemaError,
    PlayerHistorySnapshotAuditError,
    PlayerHistorySnapshotWriteError,
    PlayerHistoryV1,
    PlayerHistoryValidationError,
    PlayerLookupRecordV1,
    PlayerLookupV1,
    PlayerLookupValidationError,
    SnapshotMetaRecordV1,
    SnapshotMetaV1,
    audit_player_history_snapshot_v1,
    audit_player_history_schema,
    canonicalize_player_history_graph_v1,
    canonicalize_player_history_v1,
    canonicalize_player_lookup_v1,
    canonicalize_snapshot_meta_v1,
    create_player_history_schema,
    write_player_history_graph_v1,
)
import aram_nn.site.player_history_readmodel as readmodel


DDL = (
    """CREATE TABLE snapshot_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        dataset_id TEXT NOT NULL,
        region TEXT NOT NULL CHECK (region = 'TW'),
        queue_id INTEGER NOT NULL CHECK (queue_id = 2400),
        patches_json TEXT NOT NULL,
        generated_date TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source = 'lcu-captured-offline-snapshot'),
        coverage TEXT NOT NULL CHECK (coverage = 'captured-subset'),
        low_sample_floor INTEGER NOT NULL CHECK (low_sample_floor = 20),
        row_count INTEGER NOT NULL CHECK (row_count >= 0),
        exclusions_json TEXT NOT NULL
    ) STRICT, WITHOUT ROWID;""",
    """CREATE TABLE player_lookup (
        lookup_key BLOB PRIMARY KEY CHECK (length(lookup_key) = 32),
        status TEXT NOT NULL CHECK (status IN ('ready','ambiguous')),
        observed_matches INTEGER,
        low_sample INTEGER,
        UNIQUE (lookup_key, status),
        CHECK ((status = 'ready' AND typeof(observed_matches) = 'integer'
            AND observed_matches >= 1 AND typeof(low_sample) = 'integer'
            AND low_sample IN (0,1) AND low_sample = (observed_matches < 20))
            OR (status = 'ambiguous' AND observed_matches IS NULL
            AND low_sample IS NULL))
    ) STRICT, WITHOUT ROWID;""",
    """CREATE TABLE player_history (
        lookup_key BLOB NOT NULL CHECK (length(lookup_key) = 32),
        lookup_status TEXT NOT NULL DEFAULT 'ready' CHECK (lookup_status = 'ready'),
        event_key BLOB NOT NULL CHECK (length(event_key) = 32),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        patch TEXT NOT NULL,
        champion_id INTEGER NOT NULL CHECK (champion_id > 0),
        outcome TEXT NOT NULL CHECK (outcome IN ('win','loss')),
        duration_bucket TEXT NOT NULL CHECK (duration_bucket IN
            ('lt_15m','15_20m','20_25m','ge_25m')),
        PRIMARY KEY (lookup_key, event_key),
        UNIQUE (lookup_key, ordinal),
        FOREIGN KEY (lookup_key, lookup_status)
            REFERENCES player_lookup(lookup_key, status)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT, WITHOUT ROWID;""",
)


EXPECTED_XINFO = {
    "snapshot_meta": (
        (0, "singleton", "INTEGER", 1, None, 1, 0),
        (1, "schema_version", "INTEGER", 1, None, 0, 0),
        (2, "dataset_id", "TEXT", 1, None, 0, 0),
        (3, "region", "TEXT", 1, None, 0, 0),
        (4, "queue_id", "INTEGER", 1, None, 0, 0),
        (5, "patches_json", "TEXT", 1, None, 0, 0),
        (6, "generated_date", "TEXT", 1, None, 0, 0),
        (7, "source", "TEXT", 1, None, 0, 0),
        (8, "coverage", "TEXT", 1, None, 0, 0),
        (9, "low_sample_floor", "INTEGER", 1, None, 0, 0),
        (10, "row_count", "INTEGER", 1, None, 0, 0),
        (11, "exclusions_json", "TEXT", 1, None, 0, 0),
    ),
    "player_lookup": (
        (0, "lookup_key", "BLOB", 1, None, 1, 0),
        (1, "status", "TEXT", 1, None, 0, 0),
        (2, "observed_matches", "INTEGER", 0, None, 0, 0),
        (3, "low_sample", "INTEGER", 0, None, 0, 0),
    ),
    "player_history": (
        (0, "lookup_key", "BLOB", 1, None, 1, 0),
        (1, "lookup_status", "TEXT", 1, "'ready'", 0, 0),
        (2, "event_key", "BLOB", 1, None, 2, 0),
        (3, "ordinal", "INTEGER", 1, None, 0, 0),
        (4, "patch", "TEXT", 1, None, 0, 0),
        (5, "champion_id", "INTEGER", 1, None, 0, 0),
        (6, "outcome", "TEXT", 1, None, 0, 0),
        (7, "duration_bucket", "TEXT", 1, None, 0, 0),
    ),
}


EXCLUSION_KEYS = (
    "duplicate_event",
    "invalid_cardinality",
    "invalid_champion",
    "invalid_identity",
    "invalid_participant_alignment",
    "invalid_participants_json",
    "invalid_private_json",
    "invalid_riot_id",
    "invalid_row_scalar",
    "invalid_source_schema",
    "invalid_team",
    "out_of_scope",
)


def exclusions(value=0):
    return {key: value for key in EXCLUSION_KEYS}


def snapshot_meta(**changes):
    values = {
        "dataset_id": "mayhem.tw.26-16",
        "patches": ("26.16",),
        "generated_date": "2026-08-12",
        "exclusions": exclusions(),
    }
    values.update(changes)
    return SnapshotMetaV1(**values)


def key(number):
    return bytes([number]) * 32


def history(**changes):
    values = {
        "lookup_key": key(1),
        "event_key": key(2),
        "ordinal": 1,
        "patch": "26.16",
        "champion_id": 10,
        "outcome": "win",
        "duration_bucket": "lt_15m",
    }
    values.update(changes)
    return PlayerHistoryV1(**values)


def snapshot_meta_record(**changes):
    return canonicalize_snapshot_meta_v1(snapshot_meta(**changes))


def lookup_record(number=1, *, status="ready", observed_matches=1):
    if status == "ambiguous":
        return canonicalize_player_lookup_v1(
            PlayerLookupV1(key(number), status, None, None)
        )
    return canonicalize_player_lookup_v1(
        PlayerLookupV1(
            key(number),
            status,
            observed_matches,
            observed_matches < 20,
        )
    )


def history_record(
    lookup_number=1,
    ordinal=1,
    *,
    event_number=None,
    patch="26.16",
    allowed_patches=("26.16",),
):
    return canonicalize_player_history_v1(
        history(
            lookup_key=key(lookup_number),
            event_key=key(event_number if event_number is not None else ordinal + 50),
            ordinal=ordinal,
            patch=patch,
        ),
        allowed_patches=allowed_patches,
    )


def user_schema(connection):
    return tuple(
        connection.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_schema ORDER BY type,name"
        )
    )


def empty_graph():
    return canonicalize_player_history_graph_v1(
        meta=snapshot_meta_record(),
        lookups=(),
        histories=(),
    )


def multi_graph():
    lookups = (
        lookup_record(3, status="ambiguous"),
        lookup_record(2, observed_matches=2),
        lookup_record(1),
    )
    histories = (
        history_record(2, 2, event_number=82),
        history_record(1, 1, event_number=71),
        history_record(2, 1, event_number=81),
    )
    return canonicalize_player_history_graph_v1(
        meta=snapshot_meta_record(),
        lookups=lookups,
        histories=histories,
    )


class PlayerHistoryReadModelSchemaTests(unittest.TestCase):
    def assert_schema_error(self, code, function, *args):
        with self.assertRaises(PlayerHistoryReadModelSchemaError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def assert_graph_error(self, meta, lookups=(), histories=()):
        with self.assertRaises(PlayerHistoryGraphValidationError) as caught:
            canonicalize_player_history_graph_v1(
                meta=meta,
                lookups=lookups,
                histories=histories,
            )
        self.assertEqual(
            (caught.exception.code, str(caught.exception)),
            ("inconsistent_snapshot", "inconsistent_snapshot"),
        )

    def make_created(self):
        connection = sqlite3.connect(":memory:")
        create_player_history_schema(connection)
        return connection

    def make_from_ddl(self, ddl=DDL):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in ddl:
            connection.execute(statement)
        return connection

    def test_public_api_signatures_and_error_inventory(self):
        self.assertEqual(
            readmodel.__all__,
            (
                "PlayerHistoryReadModelSchemaError",
                "create_player_history_schema",
                "audit_player_history_schema",
                "PlayerHistoryReadModelError",
                "SnapshotMetaV1",
                "SnapshotMetaRecordV1",
                "canonicalize_snapshot_meta_v1",
                "PlayerLookupValidationError",
                "PlayerLookupV1",
                "PlayerLookupRecordV1",
                "canonicalize_player_lookup_v1",
                "PlayerHistoryValidationError",
                "PlayerHistoryV1",
                "PlayerHistoryRecordV1",
                "canonicalize_player_history_v1",
                "PlayerHistoryGraphValidationError",
                "PlayerHistoryGraphV1",
                "canonicalize_player_history_graph_v1",
                "PlayerHistorySnapshotWriteError",
                "write_player_history_graph_v1",
                "PlayerHistorySnapshotAuditError",
                "audit_player_history_snapshot_v1",
            ),
        )
        for function in (create_player_history_schema, audit_player_history_schema):
            signature = inspect.signature(function)
            self.assertEqual(tuple(signature.parameters), ("connection",))
            self.assertIs(
                signature.parameters["connection"].annotation,
                sqlite3.Connection,
            )
            self.assertIs(signature.return_annotation, None)
        writer_signature = inspect.signature(write_player_history_graph_v1)
        self.assertEqual(tuple(writer_signature.parameters), ("connection", "graph"))
        self.assertIs(
            writer_signature.parameters["connection"].annotation,
            sqlite3.Connection,
        )
        self.assertIs(
            writer_signature.parameters["graph"].annotation,
            PlayerHistoryGraphV1,
        )
        self.assertEqual(
            writer_signature.parameters["connection"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertEqual(
            writer_signature.parameters["graph"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertIs(writer_signature.return_annotation, None)
        allowed = {
            "invalid_connection",
            "transaction_active",
            "schema_not_empty",
            "schema_invalid",
            "database_error",
        }
        for code in allowed:
            error = PlayerHistoryReadModelSchemaError(code)
            self.assertEqual(error.code, code)
            self.assertEqual(str(error), code)
        sanitized = PlayerHistoryReadModelSchemaError("private-value")
        self.assertEqual(sanitized.code, "schema_invalid")
        self.assertNotIn("private-value", str(sanitized))

    def test_lookup_dataclasses_constructor_signatures_and_fixed_error(self):
        expected_value_annotations = {
            "lookup_key": bytes,
            "status": Literal["ready", "ambiguous"],
            "observed_matches": int | None,
            "low_sample": bool | None,
        }
        expected_record_annotations = {
            "lookup_key": bytes,
            "status": str,
            "observed_matches": int | None,
            "low_sample": int | None,
        }
        self.assertEqual(
            tuple((field.name, field.type) for field in fields(PlayerLookupV1)),
            tuple(expected_value_annotations.items()),
        )
        self.assertEqual(PlayerLookupV1.__annotations__, expected_value_annotations)
        self.assertEqual(
            tuple((field.name, field.type) for field in fields(PlayerLookupRecordV1)),
            tuple(expected_record_annotations.items()),
        )
        self.assertEqual(
            PlayerLookupRecordV1.__annotations__, expected_record_annotations
        )

        for value_class, expected_annotations in (
            (PlayerLookupV1, expected_value_annotations),
            (PlayerLookupRecordV1, expected_record_annotations),
        ):
            signature = inspect.signature(value_class)
            self.assertEqual(tuple(signature.parameters), tuple(expected_annotations))
            self.assertEqual(
                {
                    name: parameter.annotation
                    for name, parameter in signature.parameters.items()
                },
                expected_annotations,
            )
            self.assertTrue(
                all(
                    parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )

        with self.assertRaises(FrozenInstanceError):
            PlayerLookupV1(key(1), "ready", 1, True).status = "ambiguous"
        with self.assertRaises(FrozenInstanceError):
            PlayerLookupRecordV1(key(1), "ready", 1, 1).low_sample = 0

        self.assertTrue(issubclass(PlayerLookupValidationError, ValueError))
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        for supplied in (None, sensitive, object()):
            error = PlayerLookupValidationError(supplied)
            self.assertEqual(error.code, "invalid_lookup")
            self.assertEqual(str(error), "invalid_lookup")
            self.assertNotIn(sensitive, str(error))

    def test_lookup_canonicalizer_exact_signature(self):
        signature = inspect.signature(canonicalize_player_lookup_v1)
        self.assertEqual(tuple(signature.parameters), ("value",))
        parameter = signature.parameters["value"]
        self.assertIs(parameter.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertIs(parameter.annotation, PlayerLookupV1)
        self.assertIs(signature.return_annotation, PlayerLookupRecordV1)

    def test_lookup_valid_rows_are_canonical_deterministic_and_detached(self):
        for observed_matches in (1, 19, 20, 10**100):
            with self.subTest(observed_matches=observed_matches):
                low_sample = observed_matches < 20
                value = PlayerLookupV1(
                    lookup_key=key(1),
                    status="ready",
                    observed_matches=observed_matches,
                    low_sample=low_sample,
                )
                before = (
                    value.lookup_key,
                    value.status,
                    value.observed_matches,
                    value.low_sample,
                )
                first = canonicalize_player_lookup_v1(value)
                second = canonicalize_player_lookup_v1(value)
                self.assertEqual(first, second)
                self.assertIsNot(first, second)
                self.assertEqual(
                    first,
                    PlayerLookupRecordV1(
                        key(1), "ready", observed_matches, int(low_sample)
                    ),
                )
                self.assertIs(type(first.low_sample), int)
                self.assertEqual(
                    (
                        value.lookup_key,
                        value.status,
                        value.observed_matches,
                        value.low_sample,
                    ),
                    before,
                )

        ambiguous = PlayerLookupV1(key(2), "ambiguous", None, None)
        first = canonicalize_player_lookup_v1(ambiguous)
        second = canonicalize_player_lookup_v1(ambiguous)
        self.assertEqual(
            first, PlayerLookupRecordV1(key(2), "ambiguous", None, None)
        )
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_lookup_rejects_wrong_object_key_and_status_shapes(self):
        @dataclass(frozen=True)
        class LookupSubclass(PlayerLookupV1):
            pass

        invalid_values = (
            object(),
            PlayerLookupRecordV1(key(1), "ready", 1, 1),
            LookupSubclass(key(1), "ready", 1, True),
            PlayerLookupV1(bytearray(key(1)), "ready", 1, True),
            PlayerLookupV1(memoryview(key(1)), "ready", 1, True),
            PlayerLookupV1("x" * 32, "ready", 1, True),
            PlayerLookupV1(key(1)[:-1], "ready", 1, True),
            PlayerLookupV1(key(1) + b"x", "ready", 1, True),
            PlayerLookupV1(key(1), 1, 1, True),
            PlayerLookupV1(key(1), "READY", 1, True),
            PlayerLookupV1(key(1), "unknown", 1, True),
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(PlayerLookupValidationError) as caught:
                    canonicalize_player_lookup_v1(invalid)
                self.assertEqual(
                    (caught.exception.code, str(caught.exception)),
                    ("invalid_lookup", "invalid_lookup"),
                )

    def test_lookup_rejects_invalid_ready_and_ambiguous_payloads(self):
        invalid_ready = (
            PlayerLookupV1(key(1), "ready", True, True),
            PlayerLookupV1(key(1), "ready", 0, True),
            PlayerLookupV1(key(1), "ready", -1, True),
            PlayerLookupV1(key(1), "ready", 1.0, True),
            PlayerLookupV1(key(1), "ready", "1", True),
            PlayerLookupV1(key(1), "ready", None, True),
            PlayerLookupV1(key(1), "ready", 1, 1),
            PlayerLookupV1(key(1), "ready", 1, None),
            PlayerLookupV1(key(1), "ready", 1, False),
            PlayerLookupV1(key(1), "ready", 20, True),
        )
        invalid_ambiguous = (
            PlayerLookupV1(key(1), "ambiguous", 1, None),
            PlayerLookupV1(key(1), "ambiguous", None, False),
            PlayerLookupV1(key(1), "ambiguous", 1, False),
        )
        for invalid in invalid_ready + invalid_ambiguous:
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerLookupValidationError):
                    canonicalize_player_lookup_v1(invalid)

    def test_lookup_failures_are_silent_and_never_echo_input(self):
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            for invalid in (
                sensitive,
                PlayerLookupV1(sensitive, "ready", 1, True),
                PlayerLookupV1(key(1), sensitive, 1, True),
            ):
                with self.assertRaises(PlayerLookupValidationError) as caught:
                    canonicalize_player_lookup_v1(invalid)
                self.assertEqual(caught.exception.code, "invalid_lookup")
                self.assertEqual(str(caught.exception), "invalid_lookup")
                self.assertNotIn(sensitive, str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_history_dataclasses_constructors_signature_and_fixed_error(self):
        value_annotations = {
            "lookup_key": bytes,
            "event_key": bytes,
            "ordinal": int,
            "patch": str,
            "champion_id": int,
            "outcome": Literal["win", "loss"],
            "duration_bucket": Literal[
                "lt_15m", "15_20m", "20_25m", "ge_25m"
            ],
        }
        record_annotations = {
            "lookup_key": bytes,
            "lookup_status": str,
            "event_key": bytes,
            "ordinal": int,
            "patch": str,
            "champion_id": int,
            "outcome": str,
            "duration_bucket": str,
        }
        for value_class, expected in (
            (PlayerHistoryV1, value_annotations),
            (PlayerHistoryRecordV1, record_annotations),
        ):
            self.assertEqual(value_class.__annotations__, expected)
            self.assertEqual(
                tuple((field.name, field.type) for field in fields(value_class)),
                tuple(expected.items()),
            )
            signature = inspect.signature(value_class)
            self.assertEqual(tuple(signature.parameters), tuple(expected))
            self.assertEqual(
                {
                    name: parameter.annotation
                    for name, parameter in signature.parameters.items()
                },
                expected,
            )
            self.assertTrue(
                all(
                    parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )

        with self.assertRaises(FrozenInstanceError):
            history().ordinal = 2
        with self.assertRaises(FrozenInstanceError):
            PlayerHistoryRecordV1(
                key(1), "ready", key(2), 1, "26.16", 10, "win", "lt_15m"
            ).lookup_status = "changed"

        self.assertTrue(issubclass(PlayerHistoryValidationError, ValueError))
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        for supplied in (None, sensitive, object()):
            error = PlayerHistoryValidationError(supplied)
            self.assertEqual(error.code, "invalid_history")
            self.assertEqual(str(error), "invalid_history")
            self.assertNotIn(sensitive, str(error))

    def test_history_canonicalizer_exact_signature(self):
        signature = inspect.signature(canonicalize_player_history_v1)
        self.assertEqual(tuple(signature.parameters), ("value", "allowed_patches"))
        value_parameter = signature.parameters["value"]
        self.assertIs(
            value_parameter.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        self.assertIs(value_parameter.annotation, PlayerHistoryV1)
        patches_parameter = signature.parameters["allowed_patches"]
        self.assertIs(patches_parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(patches_parameter.annotation, tuple[str, ...])
        self.assertIs(signature.return_annotation, PlayerHistoryRecordV1)

    def test_history_valid_rows_are_ready_deterministic_and_detached(self):
        allowed = ("26.16", "26.15", "25.24")
        cases = tuple(
            (patch, outcome, bucket)
            for patch in allowed
            for outcome in ("win", "loss")
            for bucket in ("lt_15m", "15_20m", "20_25m", "ge_25m")
        ) + (("999.999", "win", "lt_15m"),)
        for patch, outcome, bucket in cases:
            with self.subTest(patch=patch, outcome=outcome, bucket=bucket):
                patches = ("999.999",) if patch == "999.999" else allowed
                value = history(
                    patch=patch,
                    outcome=outcome,
                    duration_bucket=bucket,
                )
                before = tuple(getattr(value, field.name) for field in fields(value))
                first = canonicalize_player_history_v1(
                    value, allowed_patches=patches
                )
                second = canonicalize_player_history_v1(
                    value, allowed_patches=patches
                )
                self.assertEqual(first, second)
                self.assertIsNot(first, second)
                self.assertEqual(first.lookup_status, "ready")
                self.assertEqual(
                    first,
                    PlayerHistoryRecordV1(
                        value.lookup_key,
                        "ready",
                        value.event_key,
                        value.ordinal,
                        patch,
                        value.champion_id,
                        outcome,
                        bucket,
                    ),
                )
                self.assertEqual(
                    tuple(getattr(value, field.name) for field in fields(value)),
                    before,
                )

    def test_history_rejects_wrong_object_and_subclass(self):
        @dataclass(frozen=True)
        class HistorySubclass(PlayerHistoryV1):
            pass

        for invalid in (
            object(),
            PlayerHistoryRecordV1(
                key(1), "ready", key(2), 1, "26.16", 10, "win", "lt_15m"
            ),
            HistorySubclass(
                key(1), key(2), 1, "26.16", 10, "win", "lt_15m"
            ),
        ):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(PlayerHistoryValidationError):
                    canonicalize_player_history_v1(
                        invalid, allowed_patches=("26.16",)
                    )

    def test_history_rejects_invalid_allowed_patch_contracts(self):
        invalid_allowed = (
            ["26.16"],
            True,
            "26.16",
            (),
            ("26.16", "26.15", "25.24", "25.23"),
            (True,),
            (2616,),
            ("２６.１６",),
            ("2616",),
            ("26.16.1",),
            ("01.2",),
            ("1.02",),
            ("26.16", "26.16"),
            ("26.15", "26.16"),
        )
        for invalid in invalid_allowed:
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerHistoryValidationError):
                    canonicalize_player_history_v1(
                        history(), allowed_patches=invalid
                    )

    def test_history_rejects_invalid_binary_keys(self):
        class BytesSubclass(bytes):
            pass

        invalid_keys = (
            bytearray(key(1)),
            memoryview(key(1)),
            "x" * 32,
            BytesSubclass(key(1)),
            key(1)[:-1],
            key(1) + b"x",
        )
        for field_name in ("lookup_key", "event_key"):
            for invalid in invalid_keys:
                with self.subTest(field_name=field_name, invalid_type=type(invalid).__name__):
                    with self.assertRaises(PlayerHistoryValidationError):
                        canonicalize_player_history_v1(
                            history(**{field_name: invalid}),
                            allowed_patches=("26.16",),
                        )

    def test_history_rejects_invalid_scalars_and_patch_membership(self):
        for field_name in ("ordinal", "champion_id"):
            for invalid in (True, 0, -1, 1.0, "1"):
                with self.subTest(field_name=field_name, invalid=invalid):
                    with self.assertRaises(PlayerHistoryValidationError):
                        canonicalize_player_history_v1(
                            history(**{field_name: invalid}),
                            allowed_patches=("26.16",),
                        )

        for invalid in (
            2616,
            "26.15",
            "Name#TW1",
            "C:/private/path",
            "２６.１６",
        ):
            with self.subTest(patch=invalid):
                with self.assertRaises(PlayerHistoryValidationError):
                    canonicalize_player_history_v1(
                        history(patch=invalid), allowed_patches=("26.16",)
                    )

    def test_history_rejects_invalid_enums_silently_without_echo(self):
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        invalid_values = (
            history(outcome=1),
            history(outcome="WIN"),
            history(outcome="draw"),
            history(outcome=sensitive),
            history(duration_bucket=1),
            history(duration_bucket="LT_15M"),
            history(duration_bucket="25_30m"),
            history(duration_bucket=sensitive),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            for invalid in invalid_values:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(PlayerHistoryValidationError) as caught:
                        canonicalize_player_history_v1(
                            invalid, allowed_patches=("26.16",)
                        )
                    self.assertEqual(caught.exception.code, "invalid_history")
                    self.assertEqual(str(caught.exception), "invalid_history")
                    self.assertNotIn(sensitive, str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_metadata_dataclasses_signature_and_fixed_error(self):
        self.assertEqual(
            tuple(field.name for field in fields(SnapshotMetaV1)),
            ("dataset_id", "patches", "generated_date", "exclusions"),
        )
        self.assertEqual(
            SnapshotMetaV1.__annotations__,
            {
                "dataset_id": str,
                "patches": tuple[str, ...],
                "generated_date": str,
                "exclusions": Mapping[str, int],
            },
        )
        self.assertEqual(
            tuple(field.name for field in fields(SnapshotMetaRecordV1)),
            ("dataset_id", "patches_json", "generated_date", "exclusions_json"),
        )
        self.assertEqual(
            SnapshotMetaRecordV1.__annotations__,
            {
                "dataset_id": str,
                "patches_json": str,
                "generated_date": str,
                "exclusions_json": str,
            },
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot_meta().dataset_id = "changed"
        with self.assertRaises(FrozenInstanceError):
            SnapshotMetaRecordV1("a", "[]", "2026-01-01", "{}").dataset_id = "b"

        signature = inspect.signature(canonicalize_snapshot_meta_v1)
        self.assertEqual(tuple(signature.parameters), ("meta",))
        self.assertIs(signature.parameters["meta"].annotation, SnapshotMetaV1)
        self.assertIs(signature.return_annotation, SnapshotMetaRecordV1)
        for supplied in (None, "private-riot-id-fixture", object()):
            error = PlayerHistoryReadModelError(supplied)
            self.assertEqual(error.code, "invalid_meta")
            self.assertEqual(str(error), "invalid_meta")

    def test_metadata_valid_examples_are_canonical_and_detached(self):
        mutable = exclusions()
        mutable["duplicate_event"] = 7
        one_patch = canonicalize_snapshot_meta_v1(
            snapshot_meta(exclusions=MappingProxyType(mutable))
        )
        self.assertEqual(
            one_patch,
            SnapshotMetaRecordV1(
                dataset_id="mayhem.tw.26-16",
                patches_json='["26.16"]',
                generated_date="2026-08-12",
                exclusions_json=(
                    '{"duplicate_event":7,"invalid_cardinality":0,'
                    '"invalid_champion":0,"invalid_identity":0,'
                    '"invalid_participant_alignment":0,'
                    '"invalid_participants_json":0,"invalid_private_json":0,'
                    '"invalid_riot_id":0,"invalid_row_scalar":0,'
                    '"invalid_source_schema":0,"invalid_team":0,"out_of_scope":0}'
                ),
            ),
        )
        mutable["duplicate_event"] = 999
        self.assertIn('"duplicate_event":7', one_patch.exclusions_json)
        self.assertNotIn("999", one_patch.exclusions_json)

        three_patches = canonicalize_snapshot_meta_v1(
            snapshot_meta(
                patches=("26.16", "26.15", "25.24"),
                generated_date="2024-02-29",
            )
        )
        self.assertEqual(three_patches.patches_json, '["26.16","26.15","25.24"]')
        open_patch = canonicalize_snapshot_meta_v1(snapshot_meta(patches=("999.999",)))
        self.assertEqual(open_patch.patches_json, '["999.999"]')

    def test_metadata_encoding_is_deterministic_across_mapping_order(self):
        forward = exclusions()
        reverse = dict(reversed(tuple(forward.items())))
        forward["invalid_team"] = 123456789012345678901234567890
        reverse["invalid_team"] = 123456789012345678901234567890
        first = canonicalize_snapshot_meta_v1(snapshot_meta(exclusions=forward))
        second = canonicalize_snapshot_meta_v1(snapshot_meta(exclusions=reverse))
        self.assertEqual(first, second)
        self.assertEqual(first.exclusions_json, second.exclusions_json)

    def test_metadata_dataset_id_boundaries(self):
        for valid in ("a", "0", "a" * 64, "a.b_c-9"):
            with self.subTest(valid=valid):
                self.assertEqual(
                    canonicalize_snapshot_meta_v1(snapshot_meta(dataset_id=valid)).dataset_id,
                    valid,
                )
        for invalid in (
            "",
            "a" * 65,
            "UPPER",
            "mayhém",
            "a#b",
            "a/b",
            "a\\b",
            "a:b",
            "a b",
            ".leading",
            "-leading",
            "_leading",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerHistoryReadModelError) as caught:
                    canonicalize_snapshot_meta_v1(snapshot_meta(dataset_id=invalid))
                self.assertEqual((caught.exception.code, str(caught.exception)), ("invalid_meta", "invalid_meta"))

    def test_metadata_patch_rejections(self):
        invalid_patches = (
            [],
            (),
            ("26.16", "26.15", "25.24", "25.23"),
            (True,),
            (2616,),
            ("２６.１６",),
            ("2616",),
            ("26.16.1",),
            ("01.2",),
            ("1.02",),
            ("26.16", "26.16"),
            ("26.15", "26.16"),
            ("25.24", "26.1"),
        )
        for invalid in invalid_patches:
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerHistoryReadModelError):
                    canonicalize_snapshot_meta_v1(snapshot_meta(patches=invalid))

    def test_metadata_date_rejections_and_leap_date(self):
        valid = canonicalize_snapshot_meta_v1(snapshot_meta(generated_date="2024-02-29"))
        self.assertEqual(valid.generated_date, "2024-02-29")
        for invalid in (
            "2025-02-29",
            "2026-2-03",
            "2026-02-3",
            "2026-08-12T00:00:00",
            "C:/private",
            20260812,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerHistoryReadModelError):
                    canonicalize_snapshot_meta_v1(snapshot_meta(generated_date=invalid))

    def test_metadata_exclusion_taxonomy_and_values_are_exact(self):
        valid = exclusions()
        valid["duplicate_event"] = 0
        valid["out_of_scope"] = 10**100
        canonicalize_snapshot_meta_v1(snapshot_meta(exclusions=valid))

        missing = exclusions()
        missing.pop("invalid_team")
        extra = exclusions()
        extra["other"] = 0
        nonstring = exclusions()
        nonstring[1] = nonstring.pop("invalid_team")
        bool_value = exclusions()
        bool_value["invalid_team"] = True
        float_value = exclusions()
        float_value["invalid_team"] = 1.0
        negative = exclusions()
        negative["invalid_team"] = -1
        for invalid in ([], missing, extra, nonstring, bool_value, float_value, negative):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PlayerHistoryReadModelError):
                    canonicalize_snapshot_meta_v1(snapshot_meta(exclusions=invalid))

    def test_metadata_failures_are_silent_and_never_echo_input(self):
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            for invalid in (object(), snapshot_meta(dataset_id=sensitive)):
                with self.assertRaises(PlayerHistoryReadModelError) as caught:
                    canonicalize_snapshot_meta_v1(invalid)
                self.assertEqual(caught.exception.code, "invalid_meta")
                self.assertEqual(str(caught.exception), "invalid_meta")
                self.assertNotIn(sensitive, str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_graph_public_shape_signature_and_fixed_error(self):
        self.assertTrue(issubclass(PlayerHistoryGraphValidationError, ValueError))
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123"
        error = PlayerHistoryGraphValidationError(sensitive)
        self.assertEqual(
            (error.code, str(error)),
            ("inconsistent_snapshot", "inconsistent_snapshot"),
        )
        self.assertNotIn(sensitive, str(error))

        expected_annotations = {
            "meta": SnapshotMetaRecordV1,
            "lookups": tuple[PlayerLookupRecordV1, ...],
            "histories": tuple[PlayerHistoryRecordV1, ...],
            "row_count": int,
            "ready_lookup_count": int,
            "ambiguous_lookup_count": int,
        }
        self.assertEqual(
            [(field.name, field.type) for field in fields(PlayerHistoryGraphV1)],
            list(expected_annotations.items()),
        )
        self.assertEqual(
            inspect.signature(PlayerHistoryGraphV1),
            inspect.Signature(
                [
                    inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation)
                    for name, annotation in expected_annotations.items()
                ],
                return_annotation=None,
            ),
        )

        signature = inspect.signature(canonicalize_player_history_graph_v1)
        self.assertEqual(tuple(signature.parameters), ("meta", "lookups", "histories"))
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )
        self.assertIs(signature.parameters["meta"].annotation, SnapshotMetaRecordV1)
        self.assertEqual(
            signature.parameters["lookups"].annotation,
            Sequence[PlayerLookupRecordV1],
        )
        self.assertEqual(
            signature.parameters["histories"].annotation,
            Sequence[PlayerHistoryRecordV1],
        )
        self.assertIs(signature.return_annotation, PlayerHistoryGraphV1)

    def test_graph_valid_empty_is_frozen_and_exactly_counted(self):
        meta = snapshot_meta_record()
        graph = canonicalize_player_history_graph_v1(
            meta=meta,
            lookups=(),
            histories=(),
        )
        self.assertEqual(
            graph,
            PlayerHistoryGraphV1(meta, (), (), 0, 0, 0),
        )
        self.assertIs(graph.meta, meta)
        self.assertIs(type(graph.lookups), tuple)
        self.assertIs(type(graph.histories), tuple)
        with self.assertRaises(FrozenInstanceError):
            graph.row_count = 1

    def test_graph_valid_one_and_low_sample_boundary_counts(self):
        meta = snapshot_meta_record()
        for count in (1, 19, 20):
            with self.subTest(count=count):
                lookup = lookup_record(1, observed_matches=count)
                histories = tuple(history_record(1, ordinal) for ordinal in range(1, count + 1))
                graph = canonicalize_player_history_graph_v1(
                    meta=meta,
                    lookups=(lookup,),
                    histories=histories,
                )
                self.assertEqual(
                    (graph.row_count, graph.ready_lookup_count, graph.ambiguous_lookup_count),
                    (count, 1, 0),
                )
                self.assertEqual(graph.lookups[0].low_sample, int(count < 20))
                self.assertEqual(tuple(row.ordinal for row in graph.histories), tuple(range(1, count + 1)))

    def test_graph_multiple_lookups_are_canonical_ordered_and_detached(self):
        meta = snapshot_meta_record()
        lookups = [
            lookup_record(3, observed_matches=2),
            lookup_record(2, status="ambiguous"),
            lookup_record(1),
        ]
        histories = [
            history_record(3, 2, event_number=92),
            history_record(1, 1, event_number=81),
            history_record(3, 1, event_number=91),
        ]
        graph = canonicalize_player_history_graph_v1(
            meta=meta,
            lookups=lookups,
            histories=histories,
        )
        permuted = canonicalize_player_history_graph_v1(
            meta=meta,
            lookups=tuple(reversed(lookups)),
            histories=tuple(reversed(histories)),
        )
        self.assertEqual(graph, permuted)
        self.assertEqual(tuple(row.lookup_key for row in graph.lookups), (key(1), key(2), key(3)))
        self.assertEqual(
            tuple((row.lookup_key, row.ordinal, row.event_key) for row in graph.histories),
            ((key(1), 1, key(81)), (key(3), 1, key(91)), (key(3), 2, key(92))),
        )
        self.assertEqual(
            (graph.row_count, graph.ready_lookup_count, graph.ambiguous_lookup_count),
            (3, 2, 1),
        )
        lookups.clear()
        histories.append(history_record(4))
        self.assertEqual(len(graph.lookups), 3)
        self.assertEqual(len(graph.histories), 3)

    def test_graph_rejects_wrong_subclass_and_noncanonical_metadata(self):
        meta = snapshot_meta_record()

        @dataclass(frozen=True)
        class MetaSubclass(SnapshotMetaRecordV1):
            pass

        self.assert_graph_error(object())
        self.assert_graph_error(MetaSubclass(**meta.__dict__))

        bool_exclusions = exclusions()
        bool_exclusions["invalid_team"] = True
        variants = (
            replace(meta, patches_json="{"),
            replace(meta, exclusions_json="[0]"),
            replace(meta, patches_json='[ "26.16" ]'),
            replace(meta, exclusions_json=json.dumps(exclusions(), sort_keys=False)),
            replace(meta, dataset_id="UPPER"),
            replace(meta, generated_date="2026-02-30"),
            replace(
                meta,
                exclusions_json=json.dumps(
                    bool_exclusions,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_graph_error(variant)

    def test_graph_rejects_bad_sequence_containers_and_record_subclasses(self):
        meta = snapshot_meta_record()
        for invalid in (None, object(), (item for item in ()), "", b"", bytearray(), memoryview(b"")):
            with self.subTest(side="lookups", kind=type(invalid).__name__):
                self.assert_graph_error(meta, invalid, ())
            with self.subTest(side="histories", kind=type(invalid).__name__):
                self.assert_graph_error(meta, (), invalid)

        @dataclass(frozen=True)
        class LookupSubclass(PlayerLookupRecordV1):
            pass

        @dataclass(frozen=True)
        class HistorySubclass(PlayerHistoryRecordV1):
            pass

        lookup = lookup_record()
        row = history_record()
        self.assert_graph_error(meta, (LookupSubclass(**lookup.__dict__),), ())
        self.assert_graph_error(meta, (lookup,), (HistorySubclass(**row.__dict__),))

    def test_graph_rejects_tampered_lookup_and_history_storage(self):
        meta = snapshot_meta_record()
        lookup = lookup_record()
        invalid_lookups = (
            replace(lookup, lookup_key=key(1)[:-1]),
            replace(lookup, status="unknown"),
            replace(lookup, observed_matches=True),
            replace(lookup, low_sample=True),
            replace(lookup, low_sample=0),
            PlayerLookupRecordV1(key(1), "ambiguous", 1, 1),
        )
        for invalid in invalid_lookups:
            with self.subTest(lookup=invalid):
                self.assert_graph_error(meta, (invalid,), ())

        row = history_record()
        invalid_histories = (
            replace(row, lookup_status="ambiguous"),
            replace(row, lookup_key=key(1)[:-1]),
            replace(row, event_key=key(2)[:-1]),
            replace(row, ordinal=0),
            replace(row, champion_id=True),
            replace(row, outcome="draw"),
            replace(row, duration_bucket="unknown"),
        )
        for invalid in invalid_histories:
            with self.subTest(history=invalid):
                self.assert_graph_error(meta, (lookup,), (invalid,))

    def test_graph_rejects_duplicate_lookup_event_and_ordinal_identities(self):
        meta = snapshot_meta_record()
        single = lookup_record()
        self.assert_graph_error(meta, (single, single), (history_record(),))

        pair = lookup_record(observed_matches=2)
        duplicate_event = (
            history_record(1, 1, event_number=80),
            history_record(1, 2, event_number=80),
        )
        duplicate_ordinal = (
            history_record(1, 1, event_number=80),
            history_record(1, 1, event_number=81),
        )
        self.assert_graph_error(meta, (pair,), duplicate_event)
        self.assert_graph_error(meta, (pair,), duplicate_ordinal)

    def test_graph_rejects_missing_or_ambiguous_history_parent(self):
        meta = snapshot_meta_record()
        row = history_record()
        self.assert_graph_error(meta, (), (row,))
        self.assert_graph_error(
            meta,
            (lookup_record(status="ambiguous"),),
            (row,),
        )

    def test_graph_rejects_count_and_ordinal_inconsistency(self):
        meta = snapshot_meta_record()
        one = history_record(1, 1, event_number=80)
        two = history_record(1, 2, event_number=81)
        three = history_record(1, 3, event_number=82)
        self.assert_graph_error(meta, (lookup_record(observed_matches=2),), (one,))
        self.assert_graph_error(meta, (lookup_record(observed_matches=1),), (one, two))
        self.assert_graph_error(meta, (lookup_record(observed_matches=2),), (one, three))
        self.assert_graph_error(meta, (lookup_record(observed_matches=1),), (two,))

    def test_graph_rejects_history_patch_outside_metadata(self):
        meta = snapshot_meta_record()
        outside = history_record(
            patch="26.15",
            allowed_patches=("26.16", "26.15"),
        )
        self.assert_graph_error(meta, (lookup_record(),), (outside,))

    def test_graph_huge_observed_count_rejects_without_range_allocation(self):
        meta = snapshot_meta_record()
        huge = lookup_record(observed_matches=10**100)
        self.assert_graph_error(meta, (huge,), ())
        source = inspect.getsource(canonicalize_player_history_graph_v1)
        self.assertNotIn("range(", source)
        self.assertNotIn(".decode(", source)

    def test_graph_failures_are_silent_and_never_echo_sensitive_input(self):
        meta = snapshot_meta_record()
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        invalid = PlayerLookupRecordV1(sensitive, "ready", 1, 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assert_graph_error(meta, (invalid,), ())
        self.assertEqual(output.getvalue(), "")

    def test_clean_creation_audit_and_second_create_refusal(self):
        connection = self.make_created()
        try:
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
            self.assertEqual(
                tuple(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("snapshot_meta", "player_lookup", "player_history")
                ),
                (0, 0, 0),
            )
            before = user_schema(connection)
            audit_player_history_schema(connection)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(user_schema(connection), before)
            self.assert_schema_error(
                "schema_not_empty", create_player_history_schema, connection
            )
            self.assertFalse(connection.in_transaction)
            self.assertEqual(user_schema(connection), before)
            self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
        finally:
            connection.close()

    def test_invalid_closed_and_active_connections_are_refused_safely(self):
        for function in (create_player_history_schema, audit_player_history_schema):
            with self.subTest(function=function.__name__, kind="wrong"):
                self.assert_schema_error("invalid_connection", function, object())

            closed = sqlite3.connect(":memory:")
            closed.close()
            with self.subTest(function=function.__name__, kind="closed"):
                self.assert_schema_error("invalid_connection", function, closed)

            active = sqlite3.connect(":memory:")
            try:
                active.execute("BEGIN")
                self.assert_schema_error("transaction_active", function, active)
                self.assertTrue(active.in_transaction)
                active.rollback()
            finally:
                active.close()

    def test_exact_sqlite_manifests(self):
        connection = self.make_created()
        try:
            schema_rows = user_schema(connection)
            self.assertEqual(len(schema_rows), 5)
            self.assertEqual(
                {
                    (kind, name, table, rootpage > 0, sql is None)
                    for kind, name, table, rootpage, sql in schema_rows
                },
                {
                    ("table", "snapshot_meta", "snapshot_meta", True, False),
                    ("table", "player_lookup", "player_lookup", True, False),
                    ("table", "player_history", "player_history", True, False),
                    (
                        "index",
                        "sqlite_autoindex_player_lookup_2",
                        "player_lookup",
                        True,
                        True,
                    ),
                    (
                        "index",
                        "sqlite_autoindex_player_history_2",
                        "player_history",
                        True,
                        True,
                    ),
                },
            )
            self.assertNotIn(
                "sqlite_autoindex_player_lookup_1", {row[1] for row in schema_rows}
            )
            self.assertNotIn(
                "sqlite_autoindex_player_history_1", {row[1] for row in schema_rows}
            )

            self.assertEqual(
                {
                    (schema, name, kind, columns, wr, strict)
                    for schema, name, kind, columns, wr, strict in connection.execute(
                        "PRAGMA table_list"
                    )
                    if not name.startswith("sqlite_")
                },
                {
                    ("main", "snapshot_meta", "table", 12, 1, 1),
                    ("main", "player_lookup", "table", 4, 1, 1),
                    ("main", "player_history", "table", 8, 1, 1),
                },
            )
            for table, expected in EXPECTED_XINFO.items():
                self.assertEqual(tuple(connection.execute(f"PRAGMA table_xinfo('{table}')")), expected)

            expected_indexes = {
                "snapshot_meta": {
                    "sqlite_autoindex_snapshot_meta_1": (
                        "pk",
                        ((0, 0, "singleton"),),
                    ),
                },
                "player_lookup": {
                    "sqlite_autoindex_player_lookup_1": (
                        "pk",
                        ((0, 0, "lookup_key"),),
                    ),
                    "sqlite_autoindex_player_lookup_2": (
                        "u",
                        ((0, 0, "lookup_key"), (1, 1, "status")),
                    ),
                },
                "player_history": {
                    "sqlite_autoindex_player_history_1": (
                        "pk",
                        ((0, 0, "lookup_key"), (1, 2, "event_key")),
                    ),
                    "sqlite_autoindex_player_history_2": (
                        "u",
                        ((0, 0, "lookup_key"), (1, 3, "ordinal")),
                    ),
                },
            }
            for table, expected in expected_indexes.items():
                actual = {}
                for _, name, unique, origin, partial in connection.execute(
                    f"PRAGMA index_list('{table}')"
                ):
                    self.assertEqual((unique, partial), (1, 0))
                    actual[name] = (origin, tuple(connection.execute(f"PRAGMA index_info('{name}')")))
                self.assertEqual(actual, expected)

            self.assertEqual(
                tuple(
                    sorted(
                        connection.execute("PRAGMA foreign_key_list('player_history')"),
                        key=lambda row: row[1],
                    )
                ),
                (
                    (0, 0, "player_lookup", "lookup_key", "lookup_key", "RESTRICT", "RESTRICT", "NONE"),
                    (0, 1, "player_lookup", "lookup_status", "status", "RESTRICT", "RESTRICT", "NONE"),
                ),
            )
        finally:
            connection.close()

    def test_constraint_behavior_and_privacy_shape(self):
        connection = self.make_created()
        try:
            lookup = (
                "INSERT INTO player_lookup "
                "(lookup_key,status,observed_matches,low_sample) VALUES (?,?,?,?)"
            )
            connection.execute(lookup, (key(1), "ready", 19, 1))
            connection.execute(lookup, (key(2), "ready", 20, 0))
            connection.execute(lookup, (key(3), "ambiguous", None, None))
            for values in (
                (key(4)[:-1], "ready", 20, 0),
                ("x" * 32, "ready", 20, 0),
                (key(5), "ready", 19, 0),
                (key(6), "ready", 20, 1),
                (key(7), "ambiguous", 20, 0),
            ):
                with self.subTest(lookup=values[1:]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(lookup, values)

            history = (
                "INSERT INTO player_history "
                "(lookup_key,event_key,ordinal,patch,champion_id,outcome,duration_bucket) "
                "VALUES (?,?,?,?,?,?,?)"
            )
            valid = (key(1), key(10), 1, "26.16", 10, "win", "lt_15m")
            connection.execute(history, valid)
            self.assertEqual(
                connection.execute(
                    "SELECT lookup_status FROM player_history WHERE lookup_key=?",
                    (key(1),),
                ).fetchone(),
                ("ready",),
            )
            invalid_history = (
                (key(3), key(11), 1, "26.16", 10, "win", "lt_15m"),
                (key(1), key(10), 2, "26.16", 10, "win", "lt_15m"),
                (key(1), key(11), 1, "26.16", 10, "win", "lt_15m"),
                (key(1), key(12), 0, "26.16", 10, "win", "lt_15m"),
                (key(1), key(13), 2, "26.16", 0, "win", "lt_15m"),
                (key(1), key(14), 2, "26.16", 10, "draw", "lt_15m"),
                (key(1), key(15), 2, "26.16", 10, "loss", "unknown"),
                (key(1), key(16), 2, None, 10, "loss", "ge_25m"),
            )
            for values in invalid_history:
                with self.subTest(history=values[2:]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(history, values)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM player_lookup WHERE lookup_key=?", (key(1),))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE player_lookup SET lookup_key=? WHERE lookup_key=?",
                    (key(8), key(1)),
                )

            public_columns = {
                row[1]
                for table in ("snapshot_meta", "player_lookup", "player_history")
                for row in connection.execute(f"PRAGMA table_xinfo('{table}')")
            }
            for forbidden in ("riot_id", "puuid", "summoner_name", "path", "timestamp"):
                self.assertNotIn(forbidden, public_columns)
            self.assertEqual(
                connection.execute(
                    "SELECT type FROM pragma_table_xinfo('player_lookup') "
                    "WHERE name='lookup_key'"
                ).fetchone(),
                ("BLOB",),
            )
        finally:
            connection.rollback()
            connection.close()

    def test_schema_mutations_are_rejected(self):
        snapshot, lookup, history = DDL
        mutations = {
            "missing_column": (
                snapshot.replace(",\n        exclusions_json TEXT NOT NULL", ""),
                lookup,
                history,
            ),
            "extra_column": (
                snapshot.replace(
                    "exclusions_json TEXT NOT NULL\n    )",
                    "exclusions_json TEXT NOT NULL, leaked TEXT\n    )",
                ),
                lookup,
                history,
            ),
            "renamed_column": (snapshot.replace("dataset_id", "dataset_key"), lookup, history),
            "changed_type": (snapshot.replace("dataset_id TEXT", "dataset_id BLOB"), lookup, history),
            "changed_nullability": (
                snapshot.replace("dataset_id TEXT NOT NULL", "dataset_id TEXT"),
                lookup,
                history,
            ),
            "changed_default": (
                snapshot,
                lookup,
                history.replace("DEFAULT 'ready'", "DEFAULT 'ambiguous'"),
            ),
            "changed_primary_key": (
                snapshot,
                lookup,
                history.replace(
                    "PRIMARY KEY (lookup_key, event_key)",
                    "PRIMARY KEY (event_key, lookup_key)",
                ),
            ),
            "missing_unique": (
                snapshot,
                lookup,
                history.replace("        UNIQUE (lookup_key, ordinal),\n", ""),
            ),
            "changed_fk_mapping": (
                snapshot,
                lookup,
                history.replace(
                    "REFERENCES player_lookup(lookup_key, status)",
                    "REFERENCES player_lookup(status, lookup_key)",
                ),
            ),
            "changed_fk_action": (
                snapshot,
                lookup,
                history.replace("ON UPDATE RESTRICT", "ON UPDATE CASCADE"),
            ),
            "changed_check": (
                snapshot.replace("queue_id = 2400", "queue_id = 450"),
                lookup,
                history,
            ),
            "not_strict": (
                snapshot.replace(") STRICT, WITHOUT ROWID;", ") WITHOUT ROWID;"),
                lookup,
                history,
            ),
            "with_rowid": (
                snapshot.replace(") STRICT, WITHOUT ROWID;", ") STRICT;"),
                lookup,
                history,
            ),
            "renamed_object": (
                snapshot.replace("snapshot_meta", "snapshot_metadata"),
                lookup,
                history,
            ),
        }
        for name, ddl in mutations.items():
            with self.subTest(name=name):
                connection = self.make_from_ddl(ddl)
                try:
                    self.assert_schema_error(
                        "schema_invalid", audit_player_history_schema, connection
                    )
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
                finally:
                    connection.close()

        for sql in (
            "CREATE VIEW extra_view AS SELECT 1 AS value",
            "CREATE INDEX extra_index ON player_history(patch)",
        ):
            with self.subTest(extra_object=sql.split()[1]):
                connection = self.make_from_ddl()
                try:
                    connection.execute(sql)
                    self.assert_schema_error(
                        "schema_invalid", audit_player_history_schema, connection
                    )
                finally:
                    connection.close()

    def test_create_database_failure_rolls_back_and_does_not_leak(self):
        connection = sqlite3.connect(":memory:")
        sensitive = "private-riot-id-fixture"

        def authorizer(action, _arg1, _arg2, _database, _trigger):
            if action == sqlite3.SQLITE_CREATE_TABLE:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assert_schema_error(
                    "database_error", create_player_history_schema, connection
                )
            connection.set_authorizer(None)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(user_schema(connection), ())
            self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
            self.assertNotIn(sensitive, output.getvalue())
            self.assertEqual(output.getvalue(), "")
        finally:
            connection.set_authorizer(None)
            connection.close()

    def test_nonempty_schema_failure_is_unchanged_and_silent(self):
        connection = sqlite3.connect(":memory:")
        sensitive = "private-path-fixture"
        connection.execute(f'CREATE TABLE "{sensitive}" (value INTEGER)')
        before = user_schema(connection)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assert_schema_error(
                    "schema_not_empty", create_player_history_schema, connection
                )
            self.assertEqual(user_schema(connection), before)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(output.getvalue(), "")
        finally:
            connection.close()


class PlayerHistorySnapshotWriterTests(unittest.TestCase):
    tables = ("snapshot_meta", "player_lookup", "player_history")

    def make_created(self):
        connection = sqlite3.connect(":memory:")
        create_player_history_schema(connection)
        return connection

    def assert_write_error(self, code, connection, graph):
        with self.assertRaises(PlayerHistorySnapshotWriteError) as caught:
            write_player_history_graph_v1(connection, graph)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def rows(self, connection):
        return (
            tuple(
                connection.execute(
                    "SELECT singleton,schema_version,dataset_id,region,queue_id,"
                    "patches_json,generated_date,source,coverage,low_sample_floor,"
                    "row_count,exclusions_json FROM snapshot_meta ORDER BY singleton"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT lookup_key,status,observed_matches,low_sample "
                    "FROM player_lookup ORDER BY lookup_key"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT lookup_key,lookup_status,event_key,ordinal,patch,"
                    "champion_id,outcome,duration_bucket FROM player_history "
                    "ORDER BY lookup_key,ordinal,event_key"
                )
            ),
        )

    def assert_empty_open_inactive(self, connection):
        self.assertFalse(connection.in_transaction)
        self.assertEqual(
            tuple(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in self.tables
            ),
            (0, 0, 0),
        )
        self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))

    def test_error_inventory_inheritance_and_non_echo(self):
        self.assertTrue(issubclass(PlayerHistorySnapshotWriteError, ValueError))
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123"
        allowed = {
            "invalid_connection",
            "transaction_active",
            "schema_invalid",
            "snapshot_not_empty",
            "inconsistent_snapshot",
            "database_error",
        }
        for code in allowed:
            error = PlayerHistorySnapshotWriteError(code, sensitive)
            self.assertEqual((error.code, str(error), error.args), (code, code, (code,)))
            self.assertNotIn(sensitive, str(error))
        fallback = PlayerHistorySnapshotWriteError(sensitive)
        self.assertEqual(
            (fallback.code, str(fallback)),
            ("inconsistent_snapshot", "inconsistent_snapshot"),
        )

    def test_empty_graph_writes_one_fixed_metadata_row(self):
        graph = empty_graph()
        connection = self.make_created()
        try:
            write_player_history_graph_v1(connection, graph)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(
                self.rows(connection),
                (
                    (
                        (
                            1,
                            1,
                            graph.meta.dataset_id,
                            "TW",
                            2400,
                            graph.meta.patches_json,
                            graph.meta.generated_date,
                            "lcu-captured-offline-snapshot",
                            "captured-subset",
                            20,
                            0,
                            graph.meta.exclusions_json,
                        ),
                    ),
                    (),
                    (),
                ),
            )
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())
        finally:
            connection.close()

    def test_multi_graph_exact_rows_types_counts_and_determinism(self):
        graph = multi_graph()
        connections = [self.make_created(), self.make_created()]
        try:
            for connection in connections:
                write_player_history_graph_v1(connection, graph)
            first_rows = self.rows(connections[0])
            self.assertEqual(self.rows(connections[1]), first_rows)
            self.assertEqual(
                first_rows[1],
                tuple(
                    (row.lookup_key, row.status, row.observed_matches, row.low_sample)
                    for row in graph.lookups
                ),
            )
            self.assertEqual(
                first_rows[2],
                tuple(
                    (
                        row.lookup_key,
                        row.lookup_status,
                        row.event_key,
                        row.ordinal,
                        row.patch,
                        row.champion_id,
                        row.outcome,
                        row.duration_bucket,
                    )
                    for row in graph.histories
                ),
            )
            self.assertEqual(
                tuple(
                    connections[0].execute(
                        "SELECT typeof(lookup_key),typeof(status),"
                        "typeof(observed_matches),typeof(low_sample) "
                        "FROM player_lookup ORDER BY lookup_key"
                    )
                ),
                (
                    ("blob", "text", "integer", "integer"),
                    ("blob", "text", "integer", "integer"),
                    ("blob", "text", "null", "null"),
                ),
            )
            self.assertEqual(
                tuple(
                    connections[0].execute(
                        "SELECT typeof(lookup_key),typeof(lookup_status),"
                        "typeof(event_key),typeof(ordinal),typeof(patch),"
                        "typeof(champion_id),typeof(outcome),typeof(duration_bucket) "
                        "FROM player_history ORDER BY lookup_key,ordinal,event_key"
                    )
                ),
                (("blob", "text", "blob", "integer", "text", "integer", "text", "text"),) * 3,
            )
            self.assertEqual(
                connections[0].execute(
                    "SELECT typeof(singleton),typeof(schema_version),"
                    "typeof(dataset_id),typeof(region),typeof(queue_id),"
                    "typeof(patches_json),typeof(generated_date),typeof(source),"
                    "typeof(coverage),typeof(low_sample_floor),typeof(row_count),"
                    "typeof(exclusions_json) FROM snapshot_meta"
                ).fetchone(),
                (
                    "integer",
                    "integer",
                    "text",
                    "text",
                    "integer",
                    "text",
                    "text",
                    "text",
                    "text",
                    "integer",
                    "integer",
                    "text",
                ),
            )
            self.assertEqual(
                dict(
                    connections[0].execute(
                        "SELECT status,count(*) FROM player_lookup GROUP BY status"
                    )
                ),
                {"ready": 2, "ambiguous": 1},
            )
            self.assertEqual(tuple(connections[0].execute("PRAGMA foreign_key_check")), ())
        finally:
            for connection in connections:
                connection.close()

    def test_wrong_closed_and_active_connections_are_preserved(self):
        graph = empty_graph()
        self.assert_write_error("invalid_connection", object(), graph)

        closed = sqlite3.connect(":memory:")
        closed.close()
        self.assert_write_error("invalid_connection", closed, graph)

        active = self.make_created()
        try:
            active.execute("BEGIN")
            self.assert_write_error("transaction_active", active, graph)
            self.assertTrue(active.in_transaction)
            self.assertEqual(
                tuple(
                    active.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in self.tables
                ),
                (0, 0, 0),
            )
            active.rollback()
        finally:
            active.close()

    def test_foreign_keys_off_is_schema_invalid_without_toggle(self):
        connection = self.make_created()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            self.assert_write_error("schema_invalid", connection, empty_graph())
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (0,))
            self.assert_empty_open_inactive(connection)
        finally:
            connection.close()

    def test_missing_extra_and_tampered_schema_are_invalid(self):
        cases = []
        missing = sqlite3.connect(":memory:")
        missing.execute("PRAGMA foreign_keys = ON")
        cases.append(missing)
        extra = self.make_created()
        extra.execute("CREATE TABLE extra_public_value (value INTEGER)")
        cases.append(extra)
        tampered = sqlite3.connect(":memory:")
        tampered.execute("PRAGMA foreign_keys = ON")
        for statement in (
            DDL[0].replace("queue_id = 2400", "queue_id = 450"),
            DDL[1],
            DDL[2],
        ):
            tampered.execute(statement)
        cases.append(tampered)
        try:
            for connection in cases:
                with self.subTest(schema=user_schema(connection)):
                    before = user_schema(connection)
                    self.assert_write_error("schema_invalid", connection, empty_graph())
                    self.assertEqual(user_schema(connection), before)
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
        finally:
            for connection in cases:
                connection.close()

    def test_each_prepopulated_table_is_snapshot_not_empty_and_unchanged(self):
        graph = empty_graph()
        cases = []

        meta_connection = self.make_created()
        meta_connection.execute(
            "INSERT INTO snapshot_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                1,
                "existing",
                "TW",
                2400,
                "[]",
                "2026-08-12",
                "lcu-captured-offline-snapshot",
                "captured-subset",
                20,
                0,
                "{}",
            ),
        )
        meta_connection.commit()
        cases.append(meta_connection)

        lookup_connection = self.make_created()
        lookup_connection.execute(
            "INSERT INTO player_lookup VALUES (?,?,?,?)",
            (key(8), "ambiguous", None, None),
        )
        lookup_connection.commit()
        cases.append(lookup_connection)

        history_connection = self.make_created()
        history_connection.execute(
            "INSERT INTO player_lookup VALUES (?,?,?,?)",
            (key(8), "ready", 1, 1),
        )
        history_connection.execute(
            "INSERT INTO player_history "
            "(lookup_key,event_key,ordinal,patch,champion_id,outcome,duration_bucket) "
            "VALUES (?,?,?,?,?,?,?)",
            (key(8), key(9), 1, "26.16", 10, "win", "lt_15m"),
        )
        history_connection.commit()
        cases.append(history_connection)

        try:
            for connection in cases:
                before = self.rows(connection)
                self.assert_write_error("snapshot_not_empty", connection, graph)
                self.assertEqual(self.rows(connection), before)
                self.assertFalse(connection.in_transaction)
        finally:
            for connection in cases:
                connection.close()

    def test_subclass_tampered_and_noncanonical_graphs_are_rejected_prewrite(self):
        graph = multi_graph()

        class GraphSubclass(PlayerHistoryGraphV1):
            pass

        candidates = (
            GraphSubclass(**graph.__dict__),
            replace(graph, row_count=graph.row_count + 1),
            replace(graph, lookups=tuple(reversed(graph.lookups))),
        )
        for candidate in candidates:
            connection = self.make_created()
            try:
                self.assert_write_error("inconsistent_snapshot", connection, candidate)
                self.assert_empty_open_inactive(connection)
            finally:
                connection.close()

    def test_insert_denials_rollback_every_table_and_keep_connection_open(self):
        graph = multi_graph()
        for denied_table in self.tables:
            connection = self.make_created()
            state = {"begins": 0}

            def authorizer(action, arg1, _arg2, _database, _trigger):
                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
                    state["begins"] += 1
                if (
                    state["begins"] >= 2
                    and action == sqlite3.SQLITE_INSERT
                    and arg1 == denied_table
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorizer)
            try:
                with self.subTest(table=denied_table):
                    self.assert_write_error("database_error", connection, graph)
                    connection.set_authorizer(None)
                    self.assert_empty_open_inactive(connection)
            finally:
                connection.set_authorizer(None)
                connection.close()

    def test_hostile_rollback_denial_is_contained_and_fully_rolled_back(self):
        connection = self.make_created()
        graph = multi_graph()
        state = {"begins": 0, "insert_denied": False, "rollback_denied": False}

        def authorizer(action, arg1, _arg2, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
                state["begins"] += 1
            if (
                state["begins"] >= 2
                and action == sqlite3.SQLITE_INSERT
                and arg1 == "player_history"
            ):
                state["insert_denied"] = True
                return sqlite3.SQLITE_DENY
            if (
                state["begins"] >= 2
                and action == sqlite3.SQLITE_TRANSACTION
                and arg1 == "ROLLBACK"
            ):
                state["rollback_denied"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            self.assert_write_error("database_error", connection, graph)
            self.assertTrue(state["insert_denied"])
            self.assertTrue(state["rollback_denied"])
            self.assert_empty_open_inactive(connection)

            connection.execute("BEGIN")
            connection.rollback()
            self.assertFalse(connection.in_transaction)
        finally:
            connection.set_authorizer(None)
            connection.close()

    def test_readback_denial_rolls_back_and_keeps_connection_open(self):
        connection = self.make_created()
        graph = multi_graph()
        state = {"begins": 0, "history_insert": False, "denied": False}

        def authorizer(action, arg1, arg2, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
                state["begins"] += 1
            if (
                state["begins"] >= 2
                and action == sqlite3.SQLITE_INSERT
                and arg1 == "player_history"
            ):
                state["history_insert"] = True
            if (
                state["history_insert"]
                and not state["denied"]
                and action == sqlite3.SQLITE_READ
                and arg1 == "snapshot_meta"
                and arg2 == "singleton"
            ):
                state["denied"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            self.assert_write_error("database_error", connection, graph)
            self.assertTrue(state["denied"])
            connection.set_authorizer(None)
            self.assert_empty_open_inactive(connection)
        finally:
            connection.set_authorizer(None)
            connection.close()

    def test_failures_are_silent_and_source_has_no_path_api(self):
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        graph = replace(
            empty_graph(),
            meta=replace(empty_graph().meta, dataset_id=sensitive),
        )
        connection = self.make_created()
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assert_write_error("inconsistent_snapshot", connection, graph)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(sensitive, output.getvalue())
            self.assert_empty_open_inactive(connection)
        finally:
            connection.close()

        source = inspect.getsource(write_player_history_graph_v1)
        for forbidden in ("open(", "Path(", "pathlib", "os.", "print("):
            self.assertNotIn(forbidden, source)


class PlayerHistorySnapshotAuditTests(unittest.TestCase):
    tables = ("snapshot_meta", "player_lookup", "player_history")

    def make_created(self):
        connection = sqlite3.connect(":memory:")
        create_player_history_schema(connection)
        return connection

    def assert_audit_error(self, code, connection):
        with self.assertRaises(PlayerHistorySnapshotAuditError) as caught:
            audit_player_history_snapshot_v1(connection)
        self.assertEqual(
            (caught.exception.code, str(caught.exception), caught.exception.args),
            (code, code, (code,)),
        )

    def rows(self, connection):
        return (
            tuple(
                connection.execute(
                    "SELECT singleton,schema_version,dataset_id,region,queue_id,"
                    "patches_json,generated_date,source,coverage,low_sample_floor,"
                    "row_count,exclusions_json FROM snapshot_meta ORDER BY singleton"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT lookup_key,status,observed_matches,low_sample "
                    "FROM player_lookup ORDER BY lookup_key"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT lookup_key,lookup_status,event_key,ordinal,patch,"
                    "champion_id,outcome,duration_bucket FROM player_history "
                    "ORDER BY lookup_key,ordinal,event_key"
                )
            ),
        )

    def insert_meta(self, connection, graph, **changes):
        values = {
            "singleton": 1,
            "schema_version": 1,
            "dataset_id": graph.meta.dataset_id,
            "region": "TW",
            "queue_id": 2400,
            "patches_json": graph.meta.patches_json,
            "generated_date": graph.meta.generated_date,
            "source": "lcu-captured-offline-snapshot",
            "coverage": "captured-subset",
            "low_sample_floor": 20,
            "row_count": graph.row_count,
            "exclusions_json": graph.meta.exclusions_json,
        }
        values.update(changes)
        connection.execute(
            "INSERT INTO snapshot_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(values.values()),
        )

    def insert_graph_rows(self, connection, graph, *, reverse=False):
        lookups = reversed(graph.lookups) if reverse else graph.lookups
        histories = reversed(graph.histories) if reverse else graph.histories
        connection.executemany(
            "INSERT INTO player_lookup VALUES (?,?,?,?)",
            (
                (row.lookup_key, row.status, row.observed_matches, row.low_sample)
                for row in lookups
            ),
        )
        connection.executemany(
            "INSERT INTO player_history VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    row.lookup_key,
                    row.lookup_status,
                    row.event_key,
                    row.ordinal,
                    row.patch,
                    row.champion_id,
                    row.outcome,
                    row.duration_bucket,
                )
                for row in histories
            ),
        )

    def test_public_api_error_inventory_non_echo_and_signature(self):
        self.assertEqual(len(readmodel.__all__), 22)
        self.assertTrue(issubclass(PlayerHistorySnapshotAuditError, ValueError))
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123"
        allowed = {
            "invalid_connection",
            "transaction_active",
            "schema_invalid",
            "snapshot_invalid",
            "database_error",
        }
        for code in allowed:
            error = PlayerHistorySnapshotAuditError(code, sensitive)
            self.assertEqual((error.code, str(error), error.args), (code, code, (code,)))
            self.assertNotIn(sensitive, str(error))
        fallback = PlayerHistorySnapshotAuditError(sensitive)
        self.assertEqual(
            (fallback.code, str(fallback), fallback.args),
            ("snapshot_invalid", "snapshot_invalid", ("snapshot_invalid",)),
        )
        signature = inspect.signature(audit_player_history_snapshot_v1)
        self.assertEqual(tuple(signature.parameters), ("connection",))
        self.assertIs(
            signature.parameters["connection"].annotation,
            sqlite3.Connection,
        )
        self.assertEqual(
            signature.parameters["connection"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertIs(signature.return_annotation, PlayerHistoryGraphV1)

    def test_writer_created_empty_and_multi_round_trip_without_mutation(self):
        for graph in (empty_graph(), multi_graph()):
            connection = self.make_created()
            try:
                write_player_history_graph_v1(connection, graph)
                before = self.rows(connection)
                self.assertEqual(audit_player_history_snapshot_v1(connection), graph)
                self.assertEqual(self.rows(connection), before)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
            finally:
                connection.close()

    def test_manual_reverse_inserts_return_canonical_order(self):
        graph = multi_graph()
        connection = self.make_created()
        try:
            self.insert_meta(connection, graph)
            self.insert_graph_rows(connection, graph, reverse=True)
            connection.commit()
            before = self.rows(connection)
            self.assertEqual(audit_player_history_snapshot_v1(connection), graph)
            self.assertEqual(self.rows(connection), before)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.close()

    def test_wrong_closed_active_and_foreign_keys_off_are_preserved(self):
        self.assert_audit_error("invalid_connection", object())
        closed = sqlite3.connect(":memory:")
        closed.close()
        self.assert_audit_error("invalid_connection", closed)

        active = self.make_created()
        try:
            active.execute("BEGIN")
            self.assert_audit_error("transaction_active", active)
            self.assertTrue(active.in_transaction)
            active.rollback()
        finally:
            active.close()

        foreign_keys_off = self.make_created()
        try:
            foreign_keys_off.execute("PRAGMA foreign_keys = OFF")
            self.assert_audit_error("schema_invalid", foreign_keys_off)
            self.assertEqual(foreign_keys_off.execute("PRAGMA foreign_keys").fetchone(), (0,))
            self.assertFalse(foreign_keys_off.in_transaction)
        finally:
            foreign_keys_off.close()

    def test_missing_meta_is_snapshot_invalid_but_schema_drift_is_schema_invalid(self):
        missing_meta = self.make_created()
        try:
            self.assert_audit_error("snapshot_invalid", missing_meta)
            self.assertFalse(missing_meta.in_transaction)
        finally:
            missing_meta.close()

        cases = []
        missing_schema = sqlite3.connect(":memory:")
        missing_schema.execute("PRAGMA foreign_keys = ON")
        cases.append(missing_schema)
        extra = self.make_created()
        extra.execute("CREATE TABLE extra_public_value (value INTEGER)")
        cases.append(extra)
        tampered = sqlite3.connect(":memory:")
        tampered.execute("PRAGMA foreign_keys = ON")
        for statement in (
            DDL[0],
            DDL[1],
            DDL[2].replace("champion_id INTEGER", "champion_id TEXT"),
        ):
            tampered.execute(statement)
        cases.append(tampered)
        try:
            for connection in cases:
                with self.subTest(schema=user_schema(connection)):
                    before = user_schema(connection)
                    self.assert_audit_error("schema_invalid", connection)
                    self.assertEqual(user_schema(connection), before)
                    self.assertFalse(connection.in_transaction)
        finally:
            for connection in cases:
                connection.close()

    def test_weakened_check_sql_is_schema_invalid_with_valid_empty_snapshot(self):
        graph = empty_graph()
        replacements = (
            (
                "CHECK (region = 'TW')",
                "CHECK (region = 'TW' OR 1=1)",
            ),
            (
                "CHECK (champion_id > 0)",
                "CHECK (champion_id > 0 OR 1=1)",
            ),
        )
        for target, weakened in replacements:
            connection = sqlite3.connect(":memory:")
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                statements = tuple(
                    statement.replace(target, weakened)
                    for statement in readmodel._DDL
                )
                self.assertNotEqual(statements, readmodel._DDL)
                for statement in statements:
                    connection.execute(statement)
                self.insert_meta(connection, graph)
                connection.commit()
                before = self.rows(connection)
                self.assert_audit_error("schema_invalid", connection)
                self.assertEqual(self.rows(connection), before)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

    def test_invalid_fixed_meta_json_canonicality_and_row_count(self):
        graph = empty_graph()
        cases = (
            {"region": "NA1"},
            {"schema_version": 2},
            {"patches_json": "{"},
            {"patches_json": '[ "26.16" ]'},
            {"exclusions_json": json.dumps(exclusions(), indent=1)},
            {"row_count": 1},
        )
        for changes in cases:
            connection = self.make_created()
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                self.insert_meta(connection, graph, **changes)
                connection.commit()
                before = self.rows(connection)
                self.assert_audit_error("snapshot_invalid", connection)
                self.assertEqual(self.rows(connection), before)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

    def test_lookup_key_status_and_exact_types_are_rejected(self):
        graph = empty_graph()
        corruptions = (
            (key(1)[:-1], "ambiguous", None, None),
            (key(1), "unknown", None, None),
            (key(1), "ready", 1, 0),
        )
        for lookup in corruptions:
            connection = self.make_created()
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                self.insert_meta(connection, graph)
                connection.execute("INSERT INTO player_lookup VALUES (?,?,?,?)", lookup)
                connection.commit()
                self.assert_audit_error("snapshot_invalid", connection)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

        type_drift = sqlite3.connect(":memory:")
        type_drift.execute("PRAGMA foreign_keys = ON")
        try:
            for statement in (
                DDL[0],
                DDL[1].replace("status TEXT", "status BLOB"),
                DDL[2],
            ):
                type_drift.execute(statement)
            self.assert_audit_error("schema_invalid", type_drift)
        finally:
            type_drift.close()

    def test_history_enum_count_gap_patch_and_parent_are_snapshot_invalid(self):
        graph = empty_graph()
        cases = (
            ((key(1), "ready", 1, 1), (key(1), "ready", key(2), 1, "26.16", 10, "draw", "lt_15m")),
            ((key(1), "ready", 2, 1), (key(1), "ready", key(2), 1, "26.16", 10, "win", "lt_15m")),
            ((key(1), "ready", 1, 1), (key(1), "ready", key(2), 2, "26.16", 10, "win", "lt_15m")),
            ((key(1), "ready", 1, 1), (key(1), "ready", key(2), 1, "26.15", 10, "win", "lt_15m")),
        )
        for lookup, history_row in cases:
            connection = self.make_created()
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                self.insert_meta(connection, graph, row_count=1)
                connection.execute("INSERT INTO player_lookup VALUES (?,?,?,?)", lookup)
                connection.execute("INSERT INTO player_history VALUES (?,?,?,?,?,?,?,?)", history_row)
                connection.commit()
                self.assert_audit_error("snapshot_invalid", connection)
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

        missing_parent = self.make_created()
        try:
            missing_parent.execute("PRAGMA foreign_keys = OFF")
            self.insert_meta(missing_parent, graph, row_count=1)
            missing_parent.execute(
                "INSERT INTO player_history VALUES (?,?,?,?,?,?,?,?)",
                (key(1), "ready", key(2), 1, "26.16", 10, "win", "lt_15m"),
            )
            missing_parent.commit()
            missing_parent.execute("PRAGMA foreign_keys = ON")
            self.assert_audit_error("snapshot_invalid", missing_parent)
            self.assertFalse(missing_parent.in_transaction)
        finally:
            missing_parent.close()

    def test_duplicate_constraints_cannot_be_weakened_without_schema_invalid(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            weakened_lookup = DDL[1].replace("UNIQUE (lookup_key, status),", "")
            weakened_history = DDL[2].replace("UNIQUE (lookup_key, ordinal),", "")
            for statement in (DDL[0], weakened_lookup, weakened_history):
                connection.execute(statement)
            self.assert_audit_error("schema_invalid", connection)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.close()

    def test_select_denial_is_database_error_with_full_rollback(self):
        graph = multi_graph()
        connection = self.make_created()
        write_player_history_graph_v1(connection, graph)
        before = self.rows(connection)
        state = {"begun": False, "denied": False}

        def authorizer(action, arg1, _arg2, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
                state["begun"] = True
            if state["begun"] and action == sqlite3.SQLITE_SELECT:
                state["denied"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            self.assert_audit_error("database_error", connection)
            self.assertTrue(state["denied"])
            connection.set_authorizer(None)
            self.assertEqual(self.rows(connection), before)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.set_authorizer(None)
            connection.close()

    def test_normal_authorizer_is_preserved_and_hostile_rollback_is_contained(self):
        graph = empty_graph()
        normal = self.make_created()
        write_player_history_graph_v1(normal, graph)
        state = {"deny_later": False}

        def preserved_authorizer(action, _arg1, _arg2, _database, _trigger):
            if state["deny_later"] and action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        normal.set_authorizer(preserved_authorizer)
        try:
            self.assertEqual(audit_player_history_snapshot_v1(normal), graph)
            state["deny_later"] = True
            with self.assertRaises(sqlite3.DatabaseError):
                normal.execute("SELECT 987654321")
            self.assertFalse(normal.in_transaction)
        finally:
            normal.set_authorizer(None)
            normal.close()

        hostile = self.make_created()
        write_player_history_graph_v1(hostile, graph)
        before = self.rows(hostile)
        rollback_state = {"denied": False}

        def rollback_authorizer(action, arg1, _arg2, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "ROLLBACK":
                rollback_state["denied"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        hostile.set_authorizer(rollback_authorizer)
        try:
            self.assert_audit_error("database_error", hostile)
            self.assertTrue(rollback_state["denied"])
            self.assertEqual(self.rows(hostile), before)
            self.assertFalse(hostile.in_transaction)
            hostile.execute("BEGIN")
            hostile.rollback()
            self.assertFalse(hostile.in_transaction)
        finally:
            hostile.set_authorizer(None)
            hostile.close()

    def test_failures_are_silent_non_echoing_and_source_has_no_path_api(self):
        sensitive = "PrivateName#TW1/C:/Users/private/puuid-123/game-456"
        connection = self.make_created()
        output = io.StringIO()
        try:
            self.insert_meta(connection, empty_graph(), dataset_id=sensitive)
            connection.commit()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assert_audit_error("snapshot_invalid", connection)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(sensitive, output.getvalue())
            self.assertFalse(connection.in_transaction)
        finally:
            connection.close()

        source = inspect.getsource(readmodel)
        for forbidden in ("open(", "Path(", "pathlib", "os.", "print("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
