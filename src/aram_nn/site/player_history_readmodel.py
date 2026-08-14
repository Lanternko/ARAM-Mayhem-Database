"""Canonical empty SQLite schema for the public player-history read model."""

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal


__all__ = (
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
)


_ERROR_CODES = frozenset(
    {
        "invalid_connection",
        "transaction_active",
        "schema_not_empty",
        "schema_invalid",
        "database_error",
    }
)


class PlayerHistoryReadModelSchemaError(ValueError):
    """Stable, non-sensitive failure raised by the read-model schema API."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _ERROR_CODES else "schema_invalid"
        self.code = safe_code
        super().__init__(safe_code)


class PlayerHistoryReadModelError(ValueError):
    """Stable, non-sensitive failure raised by metadata canonicalization."""

    def __init__(self, _ignored: object = None) -> None:
        self.code = "invalid_meta"
        super().__init__("invalid_meta")


@dataclass(frozen=True)
class SnapshotMetaV1:
    dataset_id: str
    patches: tuple[str, ...]
    generated_date: str
    exclusions: Mapping[str, int]


@dataclass(frozen=True)
class SnapshotMetaRecordV1:
    dataset_id: str
    patches_json: str
    generated_date: str
    exclusions_json: str


class PlayerLookupValidationError(ValueError):
    """Stable, non-sensitive failure raised by player-lookup canonicalization."""

    def __init__(self, _ignored: object = None) -> None:
        self.code = "invalid_lookup"
        super().__init__("invalid_lookup")


@dataclass(frozen=True)
class PlayerLookupV1:
    lookup_key: bytes
    status: Literal["ready", "ambiguous"]
    observed_matches: int | None
    low_sample: bool | None


@dataclass(frozen=True)
class PlayerLookupRecordV1:
    lookup_key: bytes
    status: str
    observed_matches: int | None
    low_sample: int | None


class PlayerHistoryValidationError(ValueError):
    """Stable, non-sensitive failure raised by history-row canonicalization."""

    def __init__(self, _ignored: object = None) -> None:
        self.code = "invalid_history"
        super().__init__("invalid_history")


@dataclass(frozen=True)
class PlayerHistoryV1:
    lookup_key: bytes
    event_key: bytes
    ordinal: int
    patch: str
    champion_id: int
    outcome: Literal["win", "loss"]
    duration_bucket: Literal["lt_15m", "15_20m", "20_25m", "ge_25m"]


@dataclass(frozen=True)
class PlayerHistoryRecordV1:
    lookup_key: bytes
    lookup_status: str
    event_key: bytes
    ordinal: int
    patch: str
    champion_id: int
    outcome: str
    duration_bucket: str


class PlayerHistoryGraphValidationError(ValueError):
    """Stable, non-sensitive failure raised by graph canonicalization."""

    def __init__(self, _ignored: object = None) -> None:
        self.code = "inconsistent_snapshot"
        super().__init__("inconsistent_snapshot")


_SNAPSHOT_WRITE_ERROR_CODES = frozenset(
    {
        "invalid_connection",
        "transaction_active",
        "schema_invalid",
        "snapshot_not_empty",
        "inconsistent_snapshot",
        "database_error",
    }
)


class PlayerHistorySnapshotWriteError(ValueError):
    """Stable, non-sensitive failure raised by the snapshot writer."""

    def __init__(self, code: object = None, _ignored: object = None) -> None:
        safe_code = (
            code if type(code) is str and code in _SNAPSHOT_WRITE_ERROR_CODES
            else "inconsistent_snapshot"
        )
        self.code = safe_code
        super().__init__(safe_code)


_SNAPSHOT_AUDIT_ERROR_CODES = frozenset(
    {
        "invalid_connection",
        "transaction_active",
        "schema_invalid",
        "snapshot_invalid",
        "database_error",
    }
)


class PlayerHistorySnapshotAuditError(ValueError):
    """Stable, non-sensitive failure raised by the snapshot audit API."""

    def __init__(self, code: object = None, _ignored: object = None) -> None:
        safe_code = (
            code if type(code) is str and code in _SNAPSHOT_AUDIT_ERROR_CODES
            else "snapshot_invalid"
        )
        self.code = safe_code
        super().__init__(safe_code)


@dataclass(frozen=True)
class PlayerHistoryGraphV1:
    meta: SnapshotMetaRecordV1
    lookups: tuple[PlayerLookupRecordV1, ...]
    histories: tuple[PlayerHistoryRecordV1, ...]
    row_count: int
    ready_lookup_count: int
    ambiguous_lookup_count: int


def canonicalize_player_lookup_v1(value: PlayerLookupV1) -> PlayerLookupRecordV1:
    """Validate one privacy-safe player-lookup row without resolving identity."""

    try:
        if type(value) is not PlayerLookupV1:
            raise ValueError
        if type(value.lookup_key) is not bytes or len(value.lookup_key) != 32:
            raise ValueError
        if type(value.status) is not str or value.status not in ("ready", "ambiguous"):
            raise ValueError

        if value.status == "ready":
            if type(value.observed_matches) is not int or value.observed_matches < 1:
                raise ValueError
            if type(value.low_sample) is not bool:
                raise ValueError
            if value.low_sample is not (value.observed_matches < 20):
                raise ValueError
            return PlayerLookupRecordV1(
                lookup_key=value.lookup_key,
                status=value.status,
                observed_matches=value.observed_matches,
                low_sample=int(value.low_sample),
            )

        if value.observed_matches is not None or value.low_sample is not None:
            raise ValueError
        return PlayerLookupRecordV1(
            lookup_key=value.lookup_key,
            status=value.status,
            observed_matches=None,
            low_sample=None,
        )
    except PlayerLookupValidationError:
        raise
    except Exception:
        raise PlayerLookupValidationError() from None


_DATASET_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PATCH_RE = re.compile(r"([0-9]+)\.([0-9]+)")
_EXCLUSION_KEYS = frozenset(
    {
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
    }
)


def _is_canonical_number(component: str) -> bool:
    return component == "0" or component[0] in "123456789"


def _numeric_key(component: str) -> tuple[int, str]:
    return (len(component), component)


def _validate_patch_tuple(
    patches: tuple[str, ...],
) -> tuple[tuple[tuple[int, str], tuple[int, str]], ...]:
    if type(patches) is not tuple or not 1 <= len(patches) <= 3:
        raise ValueError
    parsed_patches: list[tuple[tuple[int, str], tuple[int, str]]] = []
    for patch in patches:
        if type(patch) is not str:
            raise ValueError
        patch.encode("ascii")
        match = _PATCH_RE.fullmatch(patch)
        if match is None:
            raise ValueError
        major, minor = match.groups()
        if not _is_canonical_number(major) or not _is_canonical_number(minor):
            raise ValueError
        parsed_patches.append((_numeric_key(major), _numeric_key(minor)))
    if len(set(patches)) != len(patches):
        raise ValueError
    if any(
        current <= following
        for current, following in zip(parsed_patches, parsed_patches[1:])
    ):
        raise ValueError
    return tuple(parsed_patches)


def canonicalize_player_history_v1(
    value: PlayerHistoryV1, *, allowed_patches: tuple[str, ...]
) -> PlayerHistoryRecordV1:
    """Validate one privacy-safe history row without cross-row inference."""

    try:
        if type(value) is not PlayerHistoryV1:
            raise ValueError
        _validate_patch_tuple(allowed_patches)
        if type(value.lookup_key) is not bytes or len(value.lookup_key) != 32:
            raise ValueError
        if type(value.event_key) is not bytes or len(value.event_key) != 32:
            raise ValueError
        if type(value.ordinal) is not int or value.ordinal <= 0:
            raise ValueError
        if type(value.patch) is not str or value.patch not in allowed_patches:
            raise ValueError
        if type(value.champion_id) is not int or value.champion_id <= 0:
            raise ValueError
        if type(value.outcome) is not str or value.outcome not in ("win", "loss"):
            raise ValueError
        if type(value.duration_bucket) is not str or value.duration_bucket not in (
            "lt_15m",
            "15_20m",
            "20_25m",
            "ge_25m",
        ):
            raise ValueError
        return PlayerHistoryRecordV1(
            lookup_key=value.lookup_key,
            lookup_status="ready",
            event_key=value.event_key,
            ordinal=value.ordinal,
            patch=value.patch,
            champion_id=value.champion_id,
            outcome=value.outcome,
            duration_bucket=value.duration_bucket,
        )
    except PlayerHistoryValidationError:
        raise
    except Exception:
        raise PlayerHistoryValidationError() from None


def canonicalize_snapshot_meta_v1(meta: SnapshotMetaV1) -> SnapshotMetaRecordV1:
    """Validate and encode privacy-safe snapshot metadata deterministically."""

    try:
        if type(meta) is not SnapshotMetaV1:
            raise ValueError

        if type(meta.dataset_id) is not str:
            raise ValueError
        meta.dataset_id.encode("ascii")
        if _DATASET_ID_RE.fullmatch(meta.dataset_id) is None:
            raise ValueError

        _validate_patch_tuple(meta.patches)

        if type(meta.generated_date) is not str or len(meta.generated_date) != 10:
            raise ValueError
        parsed_date = date.fromisoformat(meta.generated_date)
        if parsed_date.isoformat() != meta.generated_date:
            raise ValueError

        if not isinstance(meta.exclusions, Mapping):
            raise ValueError
        exclusion_items = tuple(meta.exclusions.items())
        if (
            len(exclusion_items) != len(_EXCLUSION_KEYS)
            or any(type(key) is not str for key, _ in exclusion_items)
            or {key for key, _ in exclusion_items} != _EXCLUSION_KEYS
            or any(type(value) is not int or value < 0 for _, value in exclusion_items)
        ):
            raise ValueError
        exclusions = dict(sorted(exclusion_items))

        return SnapshotMetaRecordV1(
            dataset_id=meta.dataset_id,
            patches_json=json.dumps(
                list(meta.patches), ensure_ascii=True, separators=(",", ":")
            ),
            generated_date=meta.generated_date,
            exclusions_json=json.dumps(
                exclusions,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    except PlayerHistoryReadModelError:
        raise
    except Exception:
        raise PlayerHistoryReadModelError() from None


def canonicalize_player_history_graph_v1(
    *,
    meta: SnapshotMetaRecordV1,
    lookups: Sequence[PlayerLookupRecordV1],
    histories: Sequence[PlayerHistoryRecordV1],
) -> PlayerHistoryGraphV1:
    """Validate and order a complete privacy-safe player-history snapshot."""

    try:
        if type(meta) is not SnapshotMetaRecordV1:
            raise ValueError
        if type(meta.patches_json) is not str or type(meta.exclusions_json) is not str:
            raise ValueError
        decoded_patches = json.loads(meta.patches_json)
        decoded_exclusions = json.loads(meta.exclusions_json)
        if type(decoded_patches) is not list or type(decoded_exclusions) is not dict:
            raise ValueError
        patches = tuple(decoded_patches)
        canonical_meta = canonicalize_snapshot_meta_v1(
            SnapshotMetaV1(
                dataset_id=meta.dataset_id,
                patches=patches,
                generated_date=meta.generated_date,
                exclusions=decoded_exclusions,
            )
        )
        if canonical_meta != meta:
            raise ValueError

        forbidden_sequences = (str, bytes, bytearray, memoryview)
        if not isinstance(lookups, Sequence) or isinstance(
            lookups, forbidden_sequences
        ):
            raise ValueError
        if not isinstance(histories, Sequence) or isinstance(
            histories, forbidden_sequences
        ):
            raise ValueError
        lookup_records = tuple(lookups)
        history_records = tuple(histories)

        canonical_lookups: list[PlayerLookupRecordV1] = []
        for record in lookup_records:
            if type(record) is not PlayerLookupRecordV1:
                raise ValueError
            if record.status == "ready":
                if type(record.low_sample) is not int or record.low_sample not in (
                    0,
                    1,
                ):
                    raise ValueError
                low_sample: bool | None = bool(record.low_sample)
            elif record.status == "ambiguous":
                if record.low_sample is not None:
                    raise ValueError
                low_sample = None
            else:
                raise ValueError
            canonical_record = canonicalize_player_lookup_v1(
                PlayerLookupV1(
                    lookup_key=record.lookup_key,
                    status=record.status,
                    observed_matches=record.observed_matches,
                    low_sample=low_sample,
                )
            )
            if canonical_record != record:
                raise ValueError
            canonical_lookups.append(canonical_record)

        canonical_histories: list[PlayerHistoryRecordV1] = []
        for record in history_records:
            if type(record) is not PlayerHistoryRecordV1:
                raise ValueError
            if type(record.lookup_status) is not str or record.lookup_status != "ready":
                raise ValueError
            canonical_record = canonicalize_player_history_v1(
                PlayerHistoryV1(
                    lookup_key=record.lookup_key,
                    event_key=record.event_key,
                    ordinal=record.ordinal,
                    patch=record.patch,
                    champion_id=record.champion_id,
                    outcome=record.outcome,
                    duration_bucket=record.duration_bucket,
                ),
                allowed_patches=patches,
            )
            if canonical_record != record:
                raise ValueError
            canonical_histories.append(canonical_record)

        ordered_lookups = tuple(
            sorted(canonical_lookups, key=lambda record: record.lookup_key)
        )
        ordered_histories = tuple(
            sorted(
                canonical_histories,
                key=lambda record: (
                    record.lookup_key,
                    record.ordinal,
                    record.event_key,
                ),
            )
        )

        lookup_by_key: dict[bytes, PlayerLookupRecordV1] = {}
        for record in ordered_lookups:
            if record.lookup_key in lookup_by_key:
                raise ValueError
            lookup_by_key[record.lookup_key] = record

        event_identities: set[tuple[bytes, bytes]] = set()
        ordinal_identities: set[tuple[bytes, int]] = set()
        histories_by_lookup: dict[bytes, list[PlayerHistoryRecordV1]] = {}
        for record in ordered_histories:
            parent = lookup_by_key.get(record.lookup_key)
            if parent is None or parent.status != "ready":
                raise ValueError
            event_identity = (record.lookup_key, record.event_key)
            ordinal_identity = (record.lookup_key, record.ordinal)
            if event_identity in event_identities or ordinal_identity in ordinal_identities:
                raise ValueError
            event_identities.add(event_identity)
            ordinal_identities.add(ordinal_identity)
            histories_by_lookup.setdefault(record.lookup_key, []).append(record)

        ready_lookup_count = 0
        ambiguous_lookup_count = 0
        for lookup in ordered_lookups:
            if lookup.status == "ready":
                ready_lookup_count += 1
                lookup_histories = histories_by_lookup.get(lookup.lookup_key, [])
                if len(lookup_histories) != lookup.observed_matches:
                    raise ValueError
                expected_ordinal = 1
                for history in lookup_histories:
                    if history.ordinal != expected_ordinal:
                        raise ValueError
                    expected_ordinal += 1
            else:
                ambiguous_lookup_count += 1
                if lookup.lookup_key in histories_by_lookup:
                    raise ValueError

        return PlayerHistoryGraphV1(
            meta=meta,
            lookups=ordered_lookups,
            histories=ordered_histories,
            row_count=len(ordered_histories),
            ready_lookup_count=ready_lookup_count,
            ambiguous_lookup_count=ambiguous_lookup_count,
        )
    except PlayerHistoryGraphValidationError:
        raise
    except Exception:
        raise PlayerHistoryGraphValidationError() from None


_DDL = (
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
        CHECK (
            (status = 'ready'
             AND typeof(observed_matches) = 'integer'
             AND observed_matches >= 1
             AND typeof(low_sample) = 'integer'
             AND low_sample IN (0,1)
             AND low_sample = (observed_matches < 20))
            OR
            (status = 'ambiguous'
             AND observed_matches IS NULL
             AND low_sample IS NULL)
        )
    ) STRICT, WITHOUT ROWID;""",
    """CREATE TABLE player_history (
        lookup_key BLOB NOT NULL CHECK (length(lookup_key) = 32),
        lookup_status TEXT NOT NULL DEFAULT 'ready' CHECK (lookup_status = 'ready'),
        event_key BLOB NOT NULL CHECK (length(event_key) = 32),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        patch TEXT NOT NULL,
        champion_id INTEGER NOT NULL CHECK (champion_id > 0),
        outcome TEXT NOT NULL CHECK (outcome IN ('win','loss')),
        duration_bucket TEXT NOT NULL CHECK (
            duration_bucket IN ('lt_15m','15_20m','20_25m','ge_25m')
        ),
        PRIMARY KEY (lookup_key, event_key),
        UNIQUE (lookup_key, ordinal),
        FOREIGN KEY (lookup_key, lookup_status)
            REFERENCES player_lookup(lookup_key, status)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT, WITHOUT ROWID;""",
)


_TABLE_XINFO = {
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


_INDEXES = {
    "snapshot_meta": {
        "sqlite_autoindex_snapshot_meta_1": ("pk", ((0, 0, "singleton"),)),
    },
    "player_lookup": {
        "sqlite_autoindex_player_lookup_1": ("pk", ((0, 0, "lookup_key"),)),
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


class _SchemaInvalid(Exception):
    pass


class _SnapshotInconsistent(Exception):
    pass


def _raise(code: str) -> None:
    raise PlayerHistoryReadModelSchemaError(code)


def _require_connection(connection: sqlite3.Connection) -> None:
    if type(connection) is not sqlite3.Connection:
        _raise("invalid_connection")
    try:
        connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        _raise("invalid_connection")
    if connection.in_transaction:
        _raise("transaction_active")


def _user_objects(connection: sqlite3.Connection, schema: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        connection.execute(
            f"SELECT type, name, tbl_name, rootpage, sql FROM {schema}.sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def _assert_equal(actual: Any, expected: Any) -> None:
    if actual != expected:
        raise _SchemaInvalid


def _audit_manifest(connection: sqlite3.Connection) -> None:
    rows = tuple(
        connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM main.sqlite_schema ORDER BY type, name"
        ).fetchall()
    )
    expected_identity = {
        ("table", "snapshot_meta", "snapshot_meta", False),
        ("table", "player_lookup", "player_lookup", False),
        ("table", "player_history", "player_history", False),
        (
            "index",
            "sqlite_autoindex_player_lookup_2",
            "player_lookup",
            True,
        ),
        (
            "index",
            "sqlite_autoindex_player_history_2",
            "player_history",
            True,
        ),
    }
    actual_identity = {
        (object_type, name, table_name, sql is None)
        for object_type, name, table_name, rootpage, sql in rows
        if isinstance(rootpage, int) and rootpage > 0
    }
    if len(rows) != len(expected_identity):
        raise _SchemaInvalid
    _assert_equal(actual_identity, expected_identity)
    if any(not isinstance(row[3], int) or row[3] <= 0 for row in rows):
        raise _SchemaInvalid
    if any(row[4] is None for row in rows if row[0] == "table"):
        raise _SchemaInvalid
    _assert_equal(_user_objects(connection, "temp"), ())

    table_list = {
        (schema, name, object_type, columns, without_rowid, strict)
        for schema, name, object_type, columns, without_rowid, strict in connection.execute(
            "PRAGMA main.table_list"
        ).fetchall()
        if not name.startswith("sqlite_")
    }
    _assert_equal(
        table_list,
        {
            ("main", "snapshot_meta", "table", 12, 1, 1),
            ("main", "player_lookup", "table", 4, 1, 1),
            ("main", "player_history", "table", 8, 1, 1),
        },
    )

    for table_name, expected_xinfo in _TABLE_XINFO.items():
        actual_xinfo = tuple(
            connection.execute(f"PRAGMA main.table_xinfo('{table_name}')").fetchall()
        )
        _assert_equal(actual_xinfo, expected_xinfo)

        actual_indexes = {}
        for _, index_name, unique, origin, partial in connection.execute(
            f"PRAGMA main.index_list('{table_name}')"
        ).fetchall():
            if unique != 1 or partial != 0:
                raise _SchemaInvalid
            index_info = tuple(
                connection.execute(f"PRAGMA main.index_info('{index_name}')").fetchall()
            )
            actual_indexes[index_name] = (origin, index_info)
        _assert_equal(actual_indexes, _INDEXES[table_name])

    _assert_equal(
        tuple(connection.execute("PRAGMA main.foreign_key_list('snapshot_meta')")),
        (),
    )
    _assert_equal(
        tuple(connection.execute("PRAGMA main.foreign_key_list('player_lookup')")),
        (),
    )
    history_fk = tuple(
        sorted(
            connection.execute("PRAGMA main.foreign_key_list('player_history')"),
            key=lambda row: row[1],
        )
    )
    _assert_equal(
        history_fk,
        (
            (0, 0, "player_lookup", "lookup_key", "lookup_key", "RESTRICT", "RESTRICT", "NONE"),
            (0, 1, "player_lookup", "lookup_status", "status", "RESTRICT", "RESTRICT", "NONE"),
        ),
    )


_Statement = tuple[str, Sequence[Any]]


def _probe(
    connection: sqlite3.Connection,
    probe_number: int,
    setup: Sequence[_Statement],
    action: _Statement,
    *,
    succeeds: bool,
    verify: Callable[[], bool] | None = None,
) -> None:
    name = f"ph_schema_probe_{probe_number}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        for sql, parameters in setup:
            try:
                connection.execute(sql, parameters)
            except sqlite3.Error as exc:
                raise _SchemaInvalid from exc
        try:
            connection.execute(action[0], action[1])
        except sqlite3.IntegrityError as exc:
            if succeeds:
                raise _SchemaInvalid from exc
        except sqlite3.Error as exc:
            raise _SchemaInvalid from exc
        else:
            if not succeeds:
                raise _SchemaInvalid
            if verify is not None and not verify():
                raise _SchemaInvalid
    finally:
        connection.execute(f"ROLLBACK TO {name}")
        connection.execute(f"RELEASE {name}")


def _audit_constraints(connection: sqlite3.Connection) -> None:
    key_a = bytes(range(32))
    key_b = bytes(range(1, 33))
    event_a = bytes(reversed(range(32)))
    event_b = bytes(reversed(range(1, 33)))

    meta_sql = (
        "INSERT INTO snapshot_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    valid_meta = (
        1,
        1,
        "dataset",
        "TW",
        2400,
        "[]",
        "2026-01-01",
        "lcu-captured-offline-snapshot",
        "captured-subset",
        20,
        0,
        "[]",
    )
    lookup_sql = (
        "INSERT INTO player_lookup "
        "(lookup_key,status,observed_matches,low_sample) VALUES (?,?,?,?)"
    )
    history_sql = (
        "INSERT INTO player_history "
        "(lookup_key,event_key,ordinal,patch,champion_id,outcome,duration_bucket) "
        "VALUES (?,?,?,?,?,?,?)"
    )

    probes: list[tuple[Sequence[_Statement], _Statement, bool, Callable[[], bool] | None]] = []

    probes.append(((), (meta_sql, valid_meta), True, None))
    for position, invalid_value in (
        (0, 2),
        (1, 2),
        (3, "NA1"),
        (4, 450),
        (7, "other-source"),
        (8, "complete"),
        (9, 19),
        (10, -1),
    ):
        values = list(valid_meta)
        values[position] = invalid_value
        probes.append(((), (meta_sql, tuple(values)), False, None))

    for values in (
        (key_a, "ready", 1, 1),
        (key_a, "ready", 19, 1),
        (key_a, "ready", 20, 0),
        (key_a, "ambiguous", None, None),
    ):
        probes.append(((), (lookup_sql, values), True, None))
    for values in (
        (key_a[:-1], "ready", 20, 0),
        (key_a + b"x", "ready", 20, 0),
        ("x" * 32, "ready", 20, 0),
        (key_a, "ready", 0, 1),
        (key_a, "ready", 19, 0),
        (key_a, "ready", 20, 1),
        (key_a, "ready", None, 1),
        (key_a, "ready", 20, None),
        (key_a, "ambiguous", 20, 0),
        (key_a, "unknown", None, None),
        (key_a, None, None, None),
    ):
        probes.append(((), (lookup_sql, values), False, None))

    ready_parent = ((lookup_sql, (key_a, "ready", 20, 0)),)
    valid_history = (key_a, event_a, 1, "26.16", 1, "win", "lt_15m")
    probes.append(
        (
            ready_parent,
            (history_sql, valid_history),
            True,
            lambda: connection.execute(
                "SELECT lookup_status FROM player_history WHERE lookup_key=? AND event_key=?",
                (key_a, event_a),
            ).fetchone()
            == ("ready",),
        )
    )
    probes.append(((), (history_sql, valid_history), False, None))
    ambiguous_parent = ((lookup_sql, (key_a, "ambiguous", None, None)),)
    probes.append((ambiguous_parent, (history_sql, valid_history), False, None))

    for position, invalid_value in (
        (0, key_a[:-1]),
        (0, "x" * 32),
        (1, event_a[:-1]),
        (1, "y" * 32),
        (2, 0),
        (3, None),
        (4, 0),
        (5, "draw"),
        (6, "25_30m"),
    ):
        values = list(valid_history)
        values[position] = invalid_value
        probes.append((ready_parent, (history_sql, tuple(values)), False, None))

    for outcome in ("win", "loss"):
        values = list(valid_history)
        values[5] = outcome
        probes.append((ready_parent, (history_sql, tuple(values)), True, None))
    for bucket in ("lt_15m", "15_20m", "20_25m", "ge_25m"):
        values = list(valid_history)
        values[6] = bucket
        probes.append((ready_parent, (history_sql, tuple(values)), True, None))

    probes.append(
        (
            ready_parent + ((history_sql, valid_history),),
            (history_sql, valid_history),
            False,
            None,
        )
    )
    second_event_same_ordinal = (key_a, event_b, 1, "26.16", 2, "loss", "15_20m")
    probes.append(
        (
            ready_parent + ((history_sql, valid_history),),
            (history_sql, second_event_same_ordinal),
            False,
            None,
        )
    )
    other_player_same_event = (key_b, event_a, 1, "26.16", 2, "loss", "15_20m")
    probes.append(
        (
            ready_parent + ((lookup_sql, (key_b, "ready", 20, 0)),),
            (history_sql, other_player_same_event),
            True,
            None,
        )
    )

    explicit_status_sql = (
        "INSERT INTO player_history "
        "(lookup_key,lookup_status,event_key,ordinal,patch,champion_id,outcome,duration_bucket) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )
    probes.append(
        (
            ready_parent,
            (
                explicit_status_sql,
                (key_a, "ambiguous", event_a, 1, "26.16", 1, "win", "lt_15m"),
            ),
            False,
            None,
        )
    )

    probes.append(
        (
            ready_parent + ((history_sql, valid_history),),
            ("DELETE FROM player_lookup WHERE lookup_key=?", (key_a,)),
            False,
            None,
        )
    )
    probes.append(
        (
            ready_parent + ((history_sql, valid_history),),
            (
                "UPDATE player_lookup SET lookup_key=? WHERE lookup_key=?",
                (key_b, key_a),
            ),
            False,
            None,
        )
    )

    for number, (setup, action, succeeds, verify) in enumerate(probes):
        _probe(
            connection,
            number,
            setup,
            action,
            succeeds=succeeds,
            verify=verify,
        )


def _audit_in_transaction(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise _SchemaInvalid
    _audit_manifest(connection)
    for table_name in ("snapshot_meta", "player_lookup", "player_history"):
        if connection.execute(f"SELECT count(*) FROM {table_name}").fetchone() != (0,):
            raise _SchemaInvalid
    _audit_constraints(connection)


def _rollback_owned(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


def create_player_history_schema(connection: sqlite3.Connection) -> None:
    """Create and audit the canonical empty public player-history schema."""

    _require_connection(connection)
    try:
        if _user_objects(connection, "main") or _user_objects(connection, "temp"):
            _raise("schema_not_empty")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            _raise("database_error")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _DDL:
            connection.execute(statement)
        _audit_in_transaction(connection)
        connection.commit()
    except PlayerHistoryReadModelSchemaError:
        _rollback_owned(connection)
        raise
    except _SchemaInvalid:
        _rollback_owned(connection)
        _raise("schema_invalid")
    except sqlite3.Error:
        _rollback_owned(connection)
        _raise("database_error")


def audit_player_history_schema(connection: sqlite3.Connection) -> None:
    """Validate the exact schema and constraints without lasting mutations."""

    _require_connection(connection)
    try:
        connection.execute("BEGIN")
        _audit_in_transaction(connection)
    except _SchemaInvalid:
        _rollback_owned(connection)
        _raise("schema_invalid")
    except sqlite3.Error:
        _rollback_owned(connection)
        _raise("database_error")
    else:
        _rollback_owned(connection)


def _raise_snapshot_write(code: str) -> None:
    raise PlayerHistorySnapshotWriteError(code)


def _require_snapshot_write_connection(connection: sqlite3.Connection) -> None:
    if type(connection) is not sqlite3.Connection:
        _raise_snapshot_write("invalid_connection")
    try:
        connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        _raise_snapshot_write("invalid_connection")
    if connection.in_transaction:
        _raise_snapshot_write("transaction_active")


def _rollback_snapshot_write(connection: sqlite3.Connection) -> bool:
    """Rollback writer-owned work, containing an authorizer that denies rollback.

    The caller-supplied authorizer is preserved unless it blocks rollback. A hostile
    rollback denial causes the authorizer to be removed before one defensive retry.
    """

    if not connection.in_transaction:
        return True
    try:
        connection.rollback()
    except sqlite3.Error:
        try:
            connection.set_authorizer(None)
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            return not connection.in_transaction
    return not connection.in_transaction


_SNAPSHOT_TABLES = ("snapshot_meta", "player_lookup", "player_history")


def _snapshot_table_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
        for table_name in _SNAPSHOT_TABLES
    )


def _require_snapshot_structure(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise _SchemaInvalid
    _audit_manifest(connection)


_SQL_PUNCTUATION = frozenset("(),;.+-*/%~=<>|&^")
_SQL_MULTI_CHARACTER_OPERATORS = (
    "->>",
    "||",
    "->",
    "<<",
    ">>",
    "<=",
    ">=",
    "==",
    "!=",
    "<>",
)


def _normalize_create_table_sql(sql: object) -> tuple[str, ...]:
    """Tokenize CREATE TABLE SQL while ignoring formatting whitespace only."""

    if type(sql) is not str:
        raise _SchemaInvalid
    tokens: list[str] = []
    position = 0
    while position < len(sql):
        character = sql[position]
        if character.isspace():
            position += 1
            continue

        if character in ("'", '"', "`", "["):
            closing = "]" if character == "[" else character
            start = position
            position += 1
            while position < len(sql):
                if sql[position] != closing:
                    position += 1
                    continue
                if (
                    closing != "]"
                    and position + 1 < len(sql)
                    and sql[position + 1] == closing
                ):
                    position += 2
                    continue
                position += 1
                tokens.append(sql[start:position])
                break
            else:
                raise _SchemaInvalid
            continue

        operator = next(
            (
                candidate
                for candidate in _SQL_MULTI_CHARACTER_OPERATORS
                if sql.startswith(candidate, position)
            ),
            None,
        )
        if operator is not None:
            tokens.append(operator)
            position += len(operator)
            continue
        if character in _SQL_PUNCTUATION:
            tokens.append(character)
            position += 1
            continue

        start = position
        while (
            position < len(sql)
            and not sql[position].isspace()
            and sql[position] not in "'\"`["
            and sql[position] not in _SQL_PUNCTUATION
        ):
            position += 1
        if start == position:
            raise _SchemaInvalid
        tokens.append(sql[start:position].casefold())

    while tokens and tokens[-1] == ";":
        tokens.pop()
    if not tokens:
        raise _SchemaInvalid
    return tuple(tokens)


def _audit_snapshot_table_sql(connection: sqlite3.Connection) -> None:
    table_names = ("snapshot_meta", "player_lookup", "player_history")
    canonical_sql = {
        table_name: _normalize_create_table_sql(statement)
        for table_name, statement in zip(table_names, _DDL, strict=True)
    }
    actual_rows = tuple(
        connection.execute(
            "SELECT name,sql FROM main.sqlite_schema "
            "WHERE type='table' AND name IN (?,?,?) ORDER BY name",
            table_names,
        )
    )
    if len(actual_rows) != len(table_names):
        raise _SchemaInvalid
    actual_sql = {
        table_name: _normalize_create_table_sql(statement)
        for table_name, statement in actual_rows
    }
    if actual_sql != canonical_sql:
        raise _SchemaInvalid


def _verify_written_snapshot(
    connection: sqlite3.Connection,
    graph: PlayerHistoryGraphV1,
) -> None:
    expected_meta = (
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
        graph.row_count,
        graph.meta.exclusions_json,
    )
    expected_lookups = tuple(
        (
            record.lookup_key,
            record.status,
            record.observed_matches,
            record.low_sample,
        )
        for record in graph.lookups
    )
    expected_histories = tuple(
        (
            record.lookup_key,
            record.lookup_status,
            record.event_key,
            record.ordinal,
            record.patch,
            record.champion_id,
            record.outcome,
            record.duration_bucket,
        )
        for record in graph.histories
    )

    if tuple(connection.execute("PRAGMA foreign_key_check")) != ():
        raise _SnapshotInconsistent
    actual_meta = tuple(
        connection.execute(
            "SELECT singleton,schema_version,dataset_id,region,queue_id,"
            "patches_json,generated_date,source,coverage,low_sample_floor,"
            "row_count,exclusions_json FROM snapshot_meta ORDER BY singleton"
        )
    )
    actual_lookups = tuple(
        connection.execute(
            "SELECT lookup_key,status,observed_matches,low_sample "
            "FROM player_lookup ORDER BY lookup_key"
        )
    )
    actual_histories = tuple(
        connection.execute(
            "SELECT lookup_key,lookup_status,event_key,ordinal,patch,champion_id,"
            "outcome,duration_bucket FROM player_history "
            "ORDER BY lookup_key,ordinal,event_key"
        )
    )
    if actual_meta != (expected_meta,):
        raise _SnapshotInconsistent
    if actual_lookups != expected_lookups or actual_histories != expected_histories:
        raise _SnapshotInconsistent

    meta_types = tuple(
        connection.execute(
            "SELECT typeof(singleton),typeof(schema_version),typeof(dataset_id),"
            "typeof(region),typeof(queue_id),typeof(patches_json),"
            "typeof(generated_date),typeof(source),typeof(coverage),"
            "typeof(low_sample_floor),typeof(row_count),typeof(exclusions_json) "
            "FROM snapshot_meta ORDER BY singleton"
        )
    )
    if meta_types != (
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
    ):
        raise _SnapshotInconsistent

    lookup_types = tuple(
        connection.execute(
            "SELECT typeof(lookup_key),typeof(status),typeof(observed_matches),"
            "typeof(low_sample) FROM player_lookup ORDER BY lookup_key"
        )
    )
    expected_lookup_types = tuple(
        (
            "blob",
            "text",
            "null" if record.observed_matches is None else "integer",
            "null" if record.low_sample is None else "integer",
        )
        for record in graph.lookups
    )
    if lookup_types != expected_lookup_types:
        raise _SnapshotInconsistent

    history_types = tuple(
        connection.execute(
            "SELECT typeof(lookup_key),typeof(lookup_status),typeof(event_key),"
            "typeof(ordinal),typeof(patch),typeof(champion_id),typeof(outcome),"
            "typeof(duration_bucket) FROM player_history "
            "ORDER BY lookup_key,ordinal,event_key"
        )
    )
    expected_history_types = tuple(
        ("blob", "text", "blob", "integer", "text", "integer", "text", "text")
        for _ in graph.histories
    )
    if history_types != expected_history_types:
        raise _SnapshotInconsistent

    if _snapshot_table_counts(connection) != (
        1,
        len(graph.lookups),
        graph.row_count,
    ):
        raise _SnapshotInconsistent
    status_counts = dict(
        connection.execute(
            "SELECT status,count(*) FROM player_lookup GROUP BY status ORDER BY status"
        )
    )
    if status_counts.get("ready", 0) != graph.ready_lookup_count:
        raise _SnapshotInconsistent
    if status_counts.get("ambiguous", 0) != graph.ambiguous_lookup_count:
        raise _SnapshotInconsistent
    if set(status_counts) - {"ready", "ambiguous"}:
        raise _SnapshotInconsistent


def write_player_history_graph_v1(
    connection: sqlite3.Connection,
    graph: PlayerHistoryGraphV1,
) -> None:
    """Write one canonical graph atomically into an exact empty schema."""

    try:
        if type(graph) is not PlayerHistoryGraphV1:
            raise ValueError
        canonical_graph = canonicalize_player_history_graph_v1(
            meta=graph.meta,
            lookups=graph.lookups,
            histories=graph.histories,
        )
        if canonical_graph != graph:
            raise ValueError
    except Exception:
        _raise_snapshot_write("inconsistent_snapshot")

    _require_snapshot_write_connection(connection)
    try:
        _require_snapshot_structure(connection)
        if _snapshot_table_counts(connection) != (0, 0, 0):
            _raise_snapshot_write("snapshot_not_empty")
        try:
            audit_player_history_schema(connection)
        except PlayerHistoryReadModelSchemaError as exc:
            if exc.code == "schema_invalid":
                _raise_snapshot_write("schema_invalid")
            _raise_snapshot_write("database_error")

        connection.execute("BEGIN IMMEDIATE")
        _require_snapshot_structure(connection)
        if _snapshot_table_counts(connection) != (0, 0, 0):
            _raise_snapshot_write("snapshot_not_empty")

        connection.execute(
            "INSERT INTO main.snapshot_meta "
            "(singleton,schema_version,dataset_id,region,queue_id,patches_json,"
            "generated_date,source,coverage,low_sample_floor,row_count,exclusions_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                graph.row_count,
                graph.meta.exclusions_json,
            ),
        )
        connection.executemany(
            "INSERT INTO main.player_lookup "
            "(lookup_key,status,observed_matches,low_sample) VALUES (?,?,?,?)",
            (
                (
                    record.lookup_key,
                    record.status,
                    record.observed_matches,
                    record.low_sample,
                )
                for record in graph.lookups
            ),
        )
        connection.executemany(
            "INSERT INTO main.player_history "
            "(lookup_key,lookup_status,event_key,ordinal,patch,champion_id,outcome,"
            "duration_bucket) VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    record.lookup_key,
                    record.lookup_status,
                    record.event_key,
                    record.ordinal,
                    record.patch,
                    record.champion_id,
                    record.outcome,
                    record.duration_bucket,
                )
                for record in graph.histories
            ),
        )
        _verify_written_snapshot(connection, graph)
        connection.commit()
    except PlayerHistorySnapshotWriteError:
        if not _rollback_snapshot_write(connection):
            _raise_snapshot_write("database_error")
        raise
    except _SchemaInvalid:
        if not _rollback_snapshot_write(connection):
            _raise_snapshot_write("database_error")
        _raise_snapshot_write("schema_invalid")
    except _SnapshotInconsistent:
        if not _rollback_snapshot_write(connection):
            _raise_snapshot_write("database_error")
        _raise_snapshot_write("inconsistent_snapshot")
    except sqlite3.Error:
        if not _rollback_snapshot_write(connection):
            _raise_snapshot_write("database_error")
        _raise_snapshot_write("database_error")


def _raise_snapshot_audit(code: str) -> None:
    raise PlayerHistorySnapshotAuditError(code)


def _require_snapshot_audit_connection(connection: sqlite3.Connection) -> None:
    if type(connection) is not sqlite3.Connection:
        _raise_snapshot_audit("invalid_connection")
    try:
        probe = connection.cursor()
        probe.close()
        active = connection.in_transaction
    except sqlite3.Error:
        _raise_snapshot_audit("invalid_connection")
    if active:
        _raise_snapshot_audit("transaction_active")


def _rollback_snapshot_audit(connection: sqlite3.Connection) -> tuple[bool, bool]:
    """Return ``(inactive, containment_used)`` after an audit-owned rollback."""

    if not connection.in_transaction:
        return True, False
    try:
        connection.rollback()
    except sqlite3.Error:
        try:
            connection.set_authorizer(None)
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            return not connection.in_transaction, True
        return not connection.in_transaction, True
    return not connection.in_transaction, False


def _read_snapshot_rows(
    connection: sqlite3.Connection,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        meta_rows = cursor.execute(
            "SELECT singleton,schema_version,dataset_id,region,queue_id,"
            "patches_json,generated_date,source,coverage,low_sample_floor,"
            "row_count,exclusions_json,typeof(singleton),typeof(schema_version),"
            "typeof(dataset_id),typeof(region),typeof(queue_id),"
            "typeof(patches_json),typeof(generated_date),typeof(source),"
            "typeof(coverage),typeof(low_sample_floor),typeof(row_count),"
            "typeof(exclusions_json) FROM main.snapshot_meta"
        ).fetchall()
        lookup_rows = cursor.execute(
            "SELECT lookup_key,status,observed_matches,low_sample,"
            "typeof(lookup_key),typeof(status),typeof(observed_matches),"
            "typeof(low_sample) FROM main.player_lookup"
        ).fetchall()
        history_rows = cursor.execute(
            "SELECT lookup_key,lookup_status,event_key,ordinal,patch,champion_id,"
            "outcome,duration_bucket,typeof(lookup_key),typeof(lookup_status),"
            "typeof(event_key),typeof(ordinal),typeof(patch),typeof(champion_id),"
            "typeof(outcome),typeof(duration_bucket) FROM main.player_history"
        ).fetchall()
    finally:
        cursor.close()
    return meta_rows, lookup_rows, history_rows


def _canonical_graph_from_snapshot_rows(
    meta_rows: Sequence[tuple[Any, ...]],
    lookup_rows: Sequence[tuple[Any, ...]],
    history_rows: Sequence[tuple[Any, ...]],
) -> PlayerHistoryGraphV1:
    try:
        if len(meta_rows) != 1:
            raise ValueError
        meta_row = meta_rows[0]
        if len(meta_row) != 24:
            raise ValueError
        meta_values = meta_row[:12]
        if tuple(meta_row[12:]) != (
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
        ):
            raise ValueError
        (
            singleton,
            schema_version,
            dataset_id,
            region,
            queue_id,
            patches_json,
            generated_date,
            source,
            coverage,
            low_sample_floor,
            stored_row_count,
            exclusions_json,
        ) = meta_values
        if (
            type(singleton) is not int
            or singleton != 1
            or type(schema_version) is not int
            or schema_version != 1
            or type(dataset_id) is not str
            or type(region) is not str
            or region != "TW"
            or type(queue_id) is not int
            or queue_id != 2400
            or type(patches_json) is not str
            or type(generated_date) is not str
            or type(source) is not str
            or source != "lcu-captured-offline-snapshot"
            or type(coverage) is not str
            or coverage != "captured-subset"
            or type(low_sample_floor) is not int
            or low_sample_floor != 20
            or type(stored_row_count) is not int
            or stored_row_count < 0
            or type(exclusions_json) is not str
        ):
            raise ValueError

        decoded_patches = json.loads(patches_json)
        decoded_exclusions = json.loads(exclusions_json)
        if type(decoded_patches) is not list or type(decoded_exclusions) is not dict:
            raise ValueError
        meta = SnapshotMetaRecordV1(
            dataset_id=dataset_id,
            patches_json=patches_json,
            generated_date=generated_date,
            exclusions_json=exclusions_json,
        )
        canonical_meta = canonicalize_snapshot_meta_v1(
            SnapshotMetaV1(
                dataset_id=dataset_id,
                patches=tuple(decoded_patches),
                generated_date=generated_date,
                exclusions=decoded_exclusions,
            )
        )
        if canonical_meta != meta:
            raise ValueError

        lookups: list[PlayerLookupRecordV1] = []
        for row in lookup_rows:
            if len(row) != 8:
                raise ValueError
            lookup_key, status, observed_matches, low_sample = row[:4]
            observed_type = "null" if observed_matches is None else "integer"
            low_sample_type = "null" if low_sample is None else "integer"
            if (
                tuple(row[4:])
                != ("blob", "text", observed_type, low_sample_type)
                or type(lookup_key) is not bytes
                or type(status) is not str
                or (
                    observed_matches is not None
                    and type(observed_matches) is not int
                )
                or (low_sample is not None and type(low_sample) is not int)
            ):
                raise ValueError
            lookups.append(
                PlayerLookupRecordV1(
                    lookup_key=lookup_key,
                    status=status,
                    observed_matches=observed_matches,
                    low_sample=low_sample,
                )
            )

        histories: list[PlayerHistoryRecordV1] = []
        expected_history_types = (
            "blob",
            "text",
            "blob",
            "integer",
            "text",
            "integer",
            "text",
            "text",
        )
        for row in history_rows:
            if len(row) != 16 or tuple(row[8:]) != expected_history_types:
                raise ValueError
            (
                lookup_key,
                lookup_status,
                event_key,
                ordinal,
                patch,
                champion_id,
                outcome,
                duration_bucket,
            ) = row[:8]
            if (
                type(lookup_key) is not bytes
                or type(lookup_status) is not str
                or type(event_key) is not bytes
                or type(ordinal) is not int
                or type(patch) is not str
                or type(champion_id) is not int
                or type(outcome) is not str
                or type(duration_bucket) is not str
            ):
                raise ValueError
            histories.append(
                PlayerHistoryRecordV1(
                    lookup_key=lookup_key,
                    lookup_status=lookup_status,
                    event_key=event_key,
                    ordinal=ordinal,
                    patch=patch,
                    champion_id=champion_id,
                    outcome=outcome,
                    duration_bucket=duration_bucket,
                )
            )

        graph = canonicalize_player_history_graph_v1(
            meta=meta,
            lookups=lookups,
            histories=histories,
        )
        if graph.row_count != stored_row_count:
            raise ValueError
        return graph
    except Exception:
        raise _SnapshotInconsistent from None


def audit_player_history_snapshot_v1(
    connection: sqlite3.Connection,
) -> PlayerHistoryGraphV1:
    """Audit and return one populated canonical player-history snapshot."""

    _require_snapshot_audit_connection(connection)
    graph: PlayerHistoryGraphV1 | None = None
    failure_code: str | None = None
    owned_transaction = False
    try:
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise _SchemaInvalid
        connection.execute("BEGIN")
        owned_transaction = True
        _require_snapshot_structure(connection)
        _audit_snapshot_table_sql(connection)
        meta_rows, lookup_rows, history_rows = _read_snapshot_rows(connection)
        graph = _canonical_graph_from_snapshot_rows(
            meta_rows,
            lookup_rows,
            history_rows,
        )
        if tuple(connection.execute("PRAGMA main.foreign_key_check")) != ():
            raise _SnapshotInconsistent
    except PlayerHistorySnapshotAuditError as exc:
        failure_code = exc.code
    except _SchemaInvalid:
        failure_code = "schema_invalid"
    except _SnapshotInconsistent:
        failure_code = "snapshot_invalid"
    except sqlite3.Error:
        failure_code = "database_error"
    finally:
        if owned_transaction:
            inactive, containment_used = _rollback_snapshot_audit(connection)
            if not inactive or containment_used:
                graph = None
                failure_code = "database_error"

    if failure_code is not None:
        _raise_snapshot_audit(failure_code)
    if graph is None:
        _raise_snapshot_audit("database_error")
    return graph
