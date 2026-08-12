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
    _audit_constraints(connection)
    for table_name in ("snapshot_meta", "player_lookup", "player_history"):
        if connection.execute(f"SELECT count(*) FROM {table_name}").fetchone() != (0,):
            raise _SchemaInvalid


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
