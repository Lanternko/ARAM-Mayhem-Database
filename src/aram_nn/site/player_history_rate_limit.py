"""Persistent fail-closed fixed-window limiter for the public history service."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .player_history_security import validate_secret


CREATE_RATE_LIMIT_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS player_history_rate_limit (
    client_key   BLOB PRIMARY KEY CHECK(typeof(client_key)='blob' AND length(client_key)=32),
    window_start INTEGER NOT NULL CHECK(window_start >= 0),
    request_count INTEGER NOT NULL CHECK(request_count >= 1)
) STRICT, WITHOUT ROWID;
"""


class RateLimitConfigurationError(ValueError):
    pass


def canonical_ip(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid_ip")
    if any(token in value for token in (",", "%", "[", "]")):
        raise ValueError("invalid_ip")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("invalid_ip") from None
    canonical = str(address)
    if value != canonical:
        raise ValueError("invalid_ip")
    return canonical


def _schema(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    xinfo = tuple(connection.execute("PRAGMA table_xinfo('player_history_rate_limit')"))
    indexes = tuple(connection.execute("PRAGMA index_list('player_history_rate_limit')"))
    index_xinfo = tuple(
        (row[1], tuple(connection.execute(f"PRAGMA index_xinfo('{row[1]}')")))
        for row in indexes
    )
    return objects, xinfo, indexes, index_xinfo


def _audit_schema(connection: sqlite3.Connection) -> None:
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute(CREATE_RATE_LIMIT_SQL)
        if _schema(connection) != _schema(reference):
            raise RateLimitConfigurationError("rate_database_invalid")
        if tuple(connection.execute("PRAGMA integrity_check")) != (("ok",),):
            raise RateLimitConfigurationError("rate_database_invalid")
    finally:
        reference.close()


@dataclass(frozen=True, slots=True)
class SQLiteFixedWindowRateLimiter:
    path: Path
    secret: bytes = field(repr=False)
    limit: int = 20
    window_seconds: int = 3600
    capacity: int = 50_000
    timeout_ms: int = 50

    def __post_init__(self) -> None:
        try:
            path = Path(self.path).resolve(strict=False)
            if path.name.endswith(("-wal", "-shm")) or path.exists() and not path.is_file():
                raise ValueError
            path.parent.mkdir(parents=True, exist_ok=True)
            validate_secret(self.secret)
            if (
                type(self.limit) is not int
                or not 1 <= self.limit <= 10_000
                or type(self.window_seconds) is not int
                or not 1 <= self.window_seconds <= 86_400
                or type(self.capacity) is not int
                or not 1 <= self.capacity <= 50_000
                or type(self.timeout_ms) is not int
                or not 1 <= self.timeout_ms <= 50
            ):
                raise ValueError
            object.__setattr__(self, "path", path)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(CREATE_RATE_LIMIT_SQL)
                _audit_schema(connection)
                connection.commit()
            finally:
                connection.close()
        except RateLimitConfigurationError:
            raise
        except Exception:
            raise RateLimitConfigurationError("rate_database_invalid") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_ms / 1000)
        connection.execute(f"PRAGMA busy_timeout={self.timeout_ms}")
        return connection

    def _key(self, client_ip: str) -> bytes:
        canonical = canonical_ip(client_ip).encode("ascii")
        framed = b"arammeta-player-history-rate-v1\x00" + struct.pack(">H", len(canonical)) + canonical
        return hmac.new(self.secret, framed, hashlib.sha256).digest()

    def charge(self, client_ip: str, *, now_ms: int | None = None) -> tuple[bool, int]:
        """Charge before body parsing; all database failures deny the request."""

        retry_after = self.window_seconds
        connection: sqlite3.Connection | None = None
        try:
            key = self._key(client_ip)
            current = int(time.time() * 1000) if now_ms is None else now_ms
            if type(current) is not int or current < 0:
                raise ValueError
            window_ms = self.window_seconds * 1000
            window_start = current - current % window_ms
            retry_after = max(1, (window_start + window_ms - current + 999) // 1000)
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM player_history_rate_limit WHERE window_start < ?",
                (window_start,),
            )
            row = connection.execute(
                "SELECT window_start,request_count FROM player_history_rate_limit "
                "WHERE client_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if row[0] != window_start or type(row[1]) is not int:
                    raise sqlite3.DatabaseError
                if row[1] >= self.limit:
                    connection.commit()
                    return False, retry_after
                connection.execute(
                    "UPDATE player_history_rate_limit SET request_count=request_count+1 "
                    "WHERE client_key=? AND window_start=?",
                    (key, window_start),
                )
            else:
                count = connection.execute(
                    "SELECT count(*) FROM player_history_rate_limit"
                ).fetchone()[0]
                if type(count) is not int or count >= self.capacity:
                    connection.rollback()
                    return False, retry_after
                connection.execute(
                    "INSERT INTO player_history_rate_limit VALUES (?,?,1)",
                    (key, window_start),
                )
            connection.commit()
            return True, retry_after
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            return False, retry_after
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
