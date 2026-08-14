"""Lossy SQLite quarantine for encrypted player-history seed candidates.

The crash contract is intentional: claiming atomically moves one row from
``pending`` to ``taken`` and erases its ciphertext before returning it.  A
worker crash therefore loses a noncritical seed instead of retaining a second
decryptable copy.  Stale ``taken`` rows may only become ``abandoned``; they are
never requeued.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from cryptography.hazmat.primitives.asymmetric import rsa

from aram_nn.site.player_history_security import (
    MAX_RSA_KEY_BITS,
    PlayerHistorySecurityError,
    derive_candidate_key,
    encrypt_candidate,
    parse_candidate_envelope,
    validate_dataset_id,
    validate_expected_normalizer_id,
    validate_sqlite_key,
    validate_timestamp_ms,
)


CREATE_CANDIDATE_QUARANTINE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS player_seed_quarantine (
    id              INTEGER PRIMARY KEY,
    candidate_key   BLOB NOT NULL CHECK(typeof(candidate_key) = 'blob' AND length(candidate_key) = 32),
    dataset_id      TEXT NOT NULL,
    normalizer_id   TEXT NOT NULL,
    key_id          TEXT NOT NULL CHECK(length(key_id) = 32 AND key_id NOT GLOB '*[^0-9a-f]*'),
    ciphertext      BLOB,
    state           TEXT NOT NULL CHECK(state IN ('pending','taken','promoted','rejected','dead','abandoned')),
    created_ms      INTEGER NOT NULL CHECK(created_ms >= 0),
    updated_ms      INTEGER NOT NULL CHECK(updated_ms >= 0),
    taken_ms        INTEGER CHECK(taken_ms IS NULL OR taken_ms >= 0),
    terminal_ms     INTEGER CHECK(terminal_ms IS NULL OR terminal_ms >= 0),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    terminal_reason TEXT,
    UNIQUE(candidate_key, dataset_id),
    CHECK(
        (state = 'pending' AND typeof(ciphertext) = 'blob' AND taken_ms IS NULL AND terminal_ms IS NULL)
        OR (state = 'taken' AND ciphertext IS NULL AND taken_ms IS NOT NULL AND terminal_ms IS NULL)
        OR (state IN ('promoted','rejected','dead','abandoned') AND ciphertext IS NULL AND terminal_ms IS NOT NULL)
    )
);
"""

_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"promoted", "rejected", "dead", "abandoned"}
)
_TERMINAL_REASON_ALLOWLIST: Final[dict[str, frozenset[str | None]]] = {
    "promoted": frozenset({None}),
    "rejected": frozenset({None, "policy", "invalid_candidate"}),
    "dead": frozenset({None, "decrypt_failed", "key_retired", "dataset_mismatch"}),
    "abandoned": frozenset({None, "stale_claim"}),
}


class QuarantineInvariantError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimHandle:
    candidate_key: bytes
    dataset_id: str

    def __post_init__(self) -> None:
        validate_sqlite_key(self.candidate_key)
        validate_dataset_id(self.dataset_id)


@dataclass(frozen=True)
class ClaimedCandidate:
    handle: ClaimHandle
    envelope: bytes


def _require_connection(connection: sqlite3.Connection) -> None:
    if type(connection) is not sqlite3.Connection:
        raise QuarantineInvariantError("invalid_connection")
    try:
        connection.total_changes
    except sqlite3.Error as exc:
        raise QuarantineInvariantError("invalid_connection") from exc
    if connection.in_transaction:
        raise QuarantineInvariantError("transaction_active")


def _user_schema(connection: sqlite3.Connection, schema: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        connection.execute(
            f"SELECT type, name, tbl_name, sql FROM {schema}.sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[Any, ...]:
    user_schema = _user_schema(connection, "main")
    table_list = tuple(
        sorted(
            row
            for row in connection.execute("PRAGMA main.table_list").fetchall()
            if not row[1].startswith("sqlite_")
        )
    )
    table_names = tuple(
        row[1] for row in user_schema if row[0] == "table"
    )
    table_details = []
    for table_name in table_names:
        indexes = tuple(connection.execute(f"PRAGMA main.index_list('{table_name}')"))
        index_details = tuple(
            (
                index_row,
                tuple(
                    connection.execute(
                        f"PRAGMA main.index_xinfo('{index_row[1]}')"
                    )
                ),
            )
            for index_row in indexes
        )
        table_details.append(
            (
                table_name,
                tuple(connection.execute(f"PRAGMA main.table_xinfo('{table_name}')")),
                indexes,
                index_details,
                tuple(connection.execute(f"PRAGMA main.foreign_key_list('{table_name}')")),
            )
        )
    return user_schema, table_list, tuple(table_details)


def _audit_quarantine_schema(connection: sqlite3.Connection) -> None:
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute(CREATE_CANDIDATE_QUARANTINE_SQL)
        if _user_schema(connection, "temp"):
            raise QuarantineInvariantError("schema_mismatch")
        if _user_schema(connection, "main") != _user_schema(reference, "main"):
            raise QuarantineInvariantError("schema_mismatch")
        if _schema_snapshot(connection) != _schema_snapshot(reference):
            raise QuarantineInvariantError("schema_mismatch")
    finally:
        reference.close()


def _rollback_owned(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


def ensure_quarantine_schema(connection: sqlite3.Connection) -> None:
    _require_connection(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(CREATE_CANDIDATE_QUARANTINE_SQL)
        _audit_quarantine_schema(connection)
        connection.commit()
    except BaseException:
        _rollback_owned(connection)
        raise


def _validate_envelope_binding(envelope: bytes, key_id: str) -> bytes:
    if type(envelope) is not bytes:
        raise PlayerHistorySecurityError("invalid_envelope")
    parsed = parse_candidate_envelope(envelope)
    if parsed.key_id != key_id:
        raise PlayerHistorySecurityError("key_mismatch")
    return envelope


def _validate_terminal_reason(target: str, terminal_reason: str | None) -> str | None:
    if type(target) is not str or target not in _TERMINAL_STATES:
        raise PlayerHistorySecurityError("invalid_transition")
    if terminal_reason is not None and type(terminal_reason) is not str:
        raise PlayerHistorySecurityError("invalid_terminal_reason")
    if terminal_reason not in _TERMINAL_REASON_ALLOWLIST[target]:
        raise PlayerHistorySecurityError("invalid_terminal_reason")
    return terminal_reason


_T = TypeVar("_T")


def _run_owned_transaction(
    connection: sqlite3.Connection, operation: Callable[[], _T]
) -> _T:
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = operation()
        connection.commit()
        return result
    except BaseException:
        _rollback_owned(connection)
        raise


def upsert_candidate(
    connection: sqlite3.Connection,
    *,
    candidate_key: bytes,
    dataset_id: str,
    normalizer_id: str,
    key_id: str,
    envelope: bytes,
    created_ms: int,
) -> bool:
    """Insert a new pending row; an existing key/dataset pair is untouched."""

    _require_connection(connection)
    canonical_key = validate_sqlite_key(candidate_key)
    dataset = validate_dataset_id(dataset_id)
    normalizer = validate_expected_normalizer_id(normalizer_id)
    created = validate_timestamp_ms(created_ms)
    payload = _validate_envelope_binding(envelope, key_id)
    def operation() -> bool:
        cursor = connection.execute(
            """
            INSERT INTO player_seed_quarantine (
                candidate_key, dataset_id, normalizer_id, key_id, ciphertext,
                state, created_ms, updated_ms, attempts, terminal_reason
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, NULL)
            ON CONFLICT(candidate_key, dataset_id) DO NOTHING
            """,
            (canonical_key, dataset, normalizer, key_id, payload, created, created),
        )
        return cursor.rowcount == 1

    return _run_owned_transaction(connection, operation)


def claim_next(connection: sqlite3.Connection, *, taken_ms: int) -> ClaimedCandidate | None:
    """Atomically erase and return the oldest pending encrypted candidate."""

    _require_connection(connection)
    claimed_at = validate_timestamp_ms(taken_ms)
    def operation() -> ClaimedCandidate | None:
        row = connection.execute(
            """
            SELECT candidate_key, dataset_id, ciphertext
            FROM player_seed_quarantine
            WHERE state = 'pending'
            ORDER BY created_ms, candidate_key
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        candidate_key = validate_sqlite_key(row[0])
        dataset_id = validate_dataset_id(row[1])
        envelope = row[2]
        if type(envelope) is not bytes:
            raise QuarantineInvariantError("pending_ciphertext_not_blob")
        cursor = connection.execute(
            """
            UPDATE player_seed_quarantine
            SET state = 'taken', ciphertext = NULL, attempts = attempts + 1,
                taken_ms = ?, updated_ms = ?
            WHERE candidate_key = ? AND dataset_id = ? AND state = 'pending'
            """,
            (claimed_at, claimed_at, candidate_key, dataset_id),
        )
        if cursor.rowcount == 0:
            return None
        if cursor.rowcount != 1:
            raise QuarantineInvariantError("claim_updated_multiple_rows")
        return ClaimedCandidate(ClaimHandle(candidate_key, dataset_id), envelope)

    return _run_owned_transaction(connection, operation)


def terminalize(
    connection: sqlite3.Connection,
    handle: ClaimHandle,
    *,
    target: str,
    terminal_ms: int,
    terminal_reason: str | None = None,
) -> bool:
    """Perform the sole normal transition from taken to a terminal state."""

    _require_connection(connection)
    if not isinstance(handle, ClaimHandle):
        raise PlayerHistorySecurityError("invalid_claim_handle")
    completed_at = validate_timestamp_ms(terminal_ms)
    reason = _validate_terminal_reason(target, terminal_reason)

    def operation() -> bool:
        cursor = connection.execute(
            """
            UPDATE player_seed_quarantine
            SET state = ?, updated_ms = ?, terminal_ms = ?, terminal_reason = ?
            WHERE candidate_key = ? AND dataset_id = ?
              AND state = 'taken' AND ciphertext IS NULL
            """,
            (
                target,
                completed_at,
                completed_at,
                reason,
                handle.candidate_key,
                handle.dataset_id,
            ),
        )
        if cursor.rowcount > 1:
            raise QuarantineInvariantError("terminal_updated_multiple_rows")
        return cursor.rowcount == 1

    return _run_owned_transaction(connection, operation)


def abandon_stale(
    connection: sqlite3.Connection,
    handle: ClaimHandle,
    *,
    stale_before_ms: int,
    abandoned_ms: int,
    terminal_reason: str | None = "stale_claim",
) -> bool:
    """Abandon one exact stale taken handle without ever requeueing it."""

    _require_connection(connection)
    if not isinstance(handle, ClaimHandle):
        raise PlayerHistorySecurityError("invalid_claim_handle")
    cutoff = validate_timestamp_ms(stale_before_ms)
    completed_at = validate_timestamp_ms(abandoned_ms)
    reason = _validate_terminal_reason("abandoned", terminal_reason)

    def operation() -> bool:
        cursor = connection.execute(
            """
            UPDATE player_seed_quarantine
            SET state = 'abandoned', updated_ms = ?, terminal_ms = ?, terminal_reason = ?
            WHERE candidate_key = ? AND dataset_id = ?
              AND state = 'taken' AND ciphertext IS NULL
              AND taken_ms <= ?
            """,
            (
                completed_at,
                completed_at,
                reason,
                handle.candidate_key,
                handle.dataset_id,
                cutoff,
            ),
        )
        if cursor.rowcount > 1:
            raise QuarantineInvariantError("abandon_updated_multiple_rows")
        return cursor.rowcount == 1

    return _run_owned_transaction(connection, operation)


_PENDING_TTL_MS: Final[int] = 30 * 24 * 60 * 60 * 1000
_TERMINAL_TTL_MS: Final[int] = 90 * 24 * 60 * 60 * 1000
_MAX_QUARANTINE_ROWS: Final[int] = 10_000
_MAX_QUARANTINE_BYTES: Final[int] = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateQuarantineStore:
    """Short-timeout, bounded admission surface for untrusted candidates."""

    path: Path
    candidate_secret: bytes = field(repr=False)
    public_key: rsa.RSAPublicKey = field(repr=False)
    dataset_id: str
    normalizer_id: str
    timeout_ms: int = 50
    capacity: int = _MAX_QUARANTINE_ROWS

    def __post_init__(self) -> None:
        try:
            path = Path(self.path).resolve(strict=False)
            if path.name.endswith(("-wal", "-shm")) or path.exists() and not path.is_file():
                raise ValueError
            path.parent.mkdir(parents=True, exist_ok=True)
            validate_sqlite_key(self.candidate_secret)
            validate_dataset_id(self.dataset_id)
            validate_expected_normalizer_id(self.normalizer_id)
            if (
                not isinstance(self.public_key, rsa.RSAPublicKey)
                or not 3072 <= self.public_key.key_size <= MAX_RSA_KEY_BITS
            ):
                raise ValueError
            if (
                type(self.timeout_ms) is not int
                or not 1 <= self.timeout_ms <= 50
                or type(self.capacity) is not int
                or not 1 <= self.capacity <= _MAX_QUARANTINE_ROWS
            ):
                raise ValueError
            object.__setattr__(self, "path", path)
            connection = self._connect()
            try:
                ensure_quarantine_schema(connection)
            finally:
                connection.close()
        except Exception:
            raise QuarantineInvariantError("quarantine_invalid") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_ms / 1000)
        connection.execute(f"PRAGMA busy_timeout={self.timeout_ms}")
        return connection

    def admit(self, normalized_riot_id: bytes, *, now_ms: int) -> bool:
        """Encrypt and admit one candidate; every failure is a silent drop."""

        connection: sqlite3.Connection | None = None
        try:
            now = validate_timestamp_ms(now_ms)
            if self.path.exists() and self.path.stat().st_size >= _MAX_QUARANTINE_BYTES:
                return False
            candidate_key = derive_candidate_key(
                self.candidate_secret,
                expected_normalizer_id=self.normalizer_id,
                dataset_id=self.dataset_id,
                normalized_riot_id=normalized_riot_id,
            )
            envelope = encrypt_candidate(
                self.public_key,
                expected_normalizer_id=self.normalizer_id,
                dataset_id=self.dataset_id,
                normalized_riot_id=normalized_riot_id,
            )
            parsed = parse_candidate_envelope(envelope)
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE player_seed_quarantine SET state='abandoned',ciphertext=NULL,"
                "updated_ms=?,terminal_ms=?,terminal_reason='stale_claim' "
                "WHERE state='taken' AND taken_ms < ?",
                (now, now, max(0, now - _PENDING_TTL_MS)),
            )
            connection.execute(
                "DELETE FROM player_seed_quarantine "
                "WHERE (state='pending' AND created_ms <= ?) "
                "OR (state IN ('promoted','rejected','dead','abandoned') AND terminal_ms <= ?)",
                (max(0, now - _PENDING_TTL_MS), max(0, now - _TERMINAL_TTL_MS)),
            )
            existing = connection.execute(
                "SELECT 1 FROM player_seed_quarantine "
                "WHERE candidate_key=? AND dataset_id=?",
                (candidate_key, self.dataset_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return False
            count = connection.execute(
                "SELECT count(*) FROM player_seed_quarantine"
            ).fetchone()[0]
            if type(count) is not int or count >= self.capacity:
                connection.rollback()
                return False
            connection.execute(
                "INSERT INTO player_seed_quarantine "
                "(candidate_key,dataset_id,normalizer_id,key_id,ciphertext,state,"
                "created_ms,updated_ms,attempts,terminal_reason) "
                "VALUES (?,?,?,?,?,'pending',?,?,0,NULL)",
                (
                    candidate_key,
                    self.dataset_id,
                    self.normalizer_id,
                    parsed.key_id,
                    envelope,
                    now,
                    now,
                ),
            )
            connection.commit()
            return True
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
