"""Audited, privacy-safe queries over an immutable player-history snapshot."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import click

from .player_history_readmodel import (
    MAX_STORED_HISTORY_V1,
    audit_player_history_snapshot_streaming_v1,
)
from .player_history_security import (
    NORMALIZER_ID,
    derive_lookup_key,
    normalize_riot_id_v1,
    validate_secret,
)


class PlayerHistoryQueryError(ValueError):
    """Stable query failure with no caller-controlled diagnostics."""

    def __init__(self, code: object = None) -> None:
        safe = code if code in ("invalid_query", "snapshot_invalid") else "snapshot_invalid"
        self.code = safe
        super().__init__(safe)


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        return connection
    except Exception:
        raise PlayerHistoryQueryError("snapshot_invalid") from None


@dataclass(frozen=True, slots=True)
class PlayerHistorySnapshotHandle:
    """One startup audit plus a pinned file identity for indexed queries.

    The full graph returned by the auditor is deliberately discarded.  Only
    public snapshot metadata and the immutable file identity survive startup.
    Every request uses a new read-only/query-only connection.
    """

    path: Path = field(repr=False)
    file_identity: tuple[int, int, int, int]
    snapshot: dict[str, object]

    @classmethod
    def open(cls, snapshot_path: Path) -> "PlayerHistorySnapshotHandle":
        connection: sqlite3.Connection | None = None
        try:
            supplied = Path(snapshot_path)
            if supplied.is_symlink():
                raise ValueError
            path = supplied.resolve(strict=True)
            if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                raise ValueError
            before = _identity(path)
            connection = _readonly_connection(path)
            summary = audit_player_history_snapshot_streaming_v1(connection)
            after = _identity(path)
            if after != before:
                raise ValueError
            patches = json.loads(summary.meta.patches_json)
            if type(patches) is not list or any(type(item) is not str for item in patches):
                raise ValueError
            snapshot = {
                "dataset_id": summary.meta.dataset_id,
                "patches": patches,
                "generated_date": summary.meta.generated_date,
            }
            return cls(path=path, file_identity=before, snapshot=snapshot)
        except PlayerHistoryQueryError:
            raise
        except Exception:
            raise PlayerHistoryQueryError("snapshot_invalid") from None
        finally:
            if connection is not None:
                connection.close()

    def _open_verified(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            if _identity(self.path) != self.file_identity:
                raise ValueError
            connection = _readonly_connection(self.path)
            if _identity(self.path) != self.file_identity:
                raise ValueError
            return connection
        except PlayerHistoryQueryError:
            raise
        except Exception:
            if connection is not None:
                connection.close()
            raise PlayerHistoryQueryError("snapshot_invalid") from None

    def query(self, *, riot_id: str, lookup_secret: bytes) -> dict[str, object]:
        try:
            normalized = normalize_riot_id_v1(riot_id)
            secret = validate_secret(lookup_secret)
            lookup_key = derive_lookup_key(
                secret,
                expected_normalizer_id=NORMALIZER_ID,
                normalized_riot_id=normalized,
            )
        except Exception:
            raise PlayerHistoryQueryError("invalid_query") from None

        connection = self._open_verified()
        try:
            row = connection.execute(
                "SELECT status,observed_matches,low_sample FROM player_lookup "
                "WHERE lookup_key=? LIMIT 1",
                (lookup_key,),
            ).fetchone()
            if row is None or row[0] != "ready":
                return {
                    "status": "not_found",
                    "snapshot": None,
                    "observed_matches": None,
                    "low_sample": None,
                    "histories": [],
                }
            observed_matches = row[1]
            if type(observed_matches) is not int or observed_matches < 1:
                raise PlayerHistoryQueryError("snapshot_invalid")
            histories = connection.execute(
                "SELECT ordinal,patch,champion_id,outcome,duration_bucket "
                "FROM player_history WHERE lookup_key=? ORDER BY ordinal LIMIT ?",
                (lookup_key, MAX_STORED_HISTORY_V1 + 1),
            ).fetchall()
            if len(histories) != min(observed_matches, MAX_STORED_HISTORY_V1):
                raise PlayerHistoryQueryError("snapshot_invalid")
            return {
                "status": "ready",
                "snapshot": dict(self.snapshot),
                "observed_matches": observed_matches,
                "low_sample": bool(row[2]),
                "histories": [
                    {
                        "ordinal": history[0],
                        "patch": history[1],
                        "champion_id": history[2],
                        "outcome": history[3],
                        "duration_bucket": history[4],
                    }
                    for history in histories
                ],
            }
        except PlayerHistoryQueryError:
            raise
        except sqlite3.Error:
            raise PlayerHistoryQueryError("snapshot_invalid") from None
        finally:
            connection.close()


def query_player_history_v1(
    *,
    riot_id: str,
    lookup_secret: bytes,
    handle: PlayerHistorySnapshotHandle | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, object]:
    """Compatibility wrapper; services and CLI should reuse ``handle``."""

    if handle is None:
        if snapshot_path is None:
            raise PlayerHistoryQueryError("snapshot_invalid")
        handle = PlayerHistorySnapshotHandle.open(snapshot_path)
    elif snapshot_path is not None or type(handle) is not PlayerHistorySnapshotHandle:
        raise PlayerHistoryQueryError("snapshot_invalid")
    return handle.query(riot_id=riot_id, lookup_secret=lookup_secret)


def lookup_secret_from_environment() -> bytes:
    value = os.environ.get("ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX")
    if value is None:
        value = click.prompt("ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX", hide_input=True)
    try:
        return validate_secret(bytes.fromhex(value))
    except Exception:
        raise click.ClickException("invalid secret") from None


@click.group()
def main() -> None:
    """Query or locally serve an audited private history snapshot."""


@main.command("query")
@click.option("--snapshot", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.argument("riot_id", required=False)
def query_command(snapshot: Path, riot_id: str | None) -> None:
    if riot_id is None:
        riot_id = click.prompt("Riot ID", hide_input=True)
    try:
        handle = PlayerHistorySnapshotHandle.open(snapshot)
        result = handle.query(riot_id=riot_id, lookup_secret=lookup_secret_from_environment())
    except PlayerHistoryQueryError as exc:
        raise click.ClickException(exc.code) from None
    click.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


@main.command("serve")
@click.option("--snapshot", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.option("--port", type=click.IntRange(1024, 65535), default=8765, show_default=True)
def serve_command(snapshot: Path, port: int) -> None:
    from .player_history_local_app import run_player_history_local_app

    run_player_history_local_app(
        snapshot_path=snapshot,
        lookup_secret=lookup_secret_from_environment(),
        port=port,
    )
