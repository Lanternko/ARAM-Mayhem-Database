"""Offline construction and no-clobber publication of private history snapshots."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import stat
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import click

from .player_history_readmodel import (
    MAX_STORED_HISTORY_V1,
    PlayerHistoryV1,
    PlayerLookupV1,
    SnapshotMetaRecordV1,
    SnapshotMetaV1,
    audit_player_history_snapshot_v1,
    audit_player_history_snapshot_streaming_v1,
    canonicalize_player_history_graph_v1,
    canonicalize_player_history_v1,
    canonicalize_player_lookup_v1,
    canonicalize_snapshot_meta_v1,
    create_player_history_schema,
    write_player_history_graph_v1,
)
from .player_history_security import (
    NORMALIZER_ID,
    derive_event_key,
    derive_lookup_key,
    validate_secret,
)
from .player_history_transform import (
    EXCLUSION_CODES_V1,
    _PlayerHistorySourceProjectionV1,
    _project_player_history_source_row_v1,
)


SOURCE_COLUMNS_V1 = (
    "game_id",
    "queue_id",
    "patch",
    "blue_wins",
    "duration_sec",
    "created_ms",
    "participants_json",
    "participants_private_json",
)

_LIVE_QUEUE_ID_V1 = 2400
_LIVE_READ_MAX_SECONDS_V1 = 20 * 60
_LIVE_WAL_MAX_GROWTH_BYTES_V1 = 512 * 1024 * 1024
_LIVE_PROGRESS_OPCODES_V1 = 10_000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EXPECTED_LIVE_GAMES_XINFO_V1 = (
    (0, "game_id", "TEXT", 0, None, 1, 0),
    (1, "queue_id", "INTEGER", 1, None, 0, 0),
    (2, "patch", "TEXT", 1, None, 0, 0),
    (3, "blue_champs", "TEXT", 1, None, 0, 0),
    (4, "red_champs", "TEXT", 1, None, 0, 0),
    (5, "blue_wins", "INTEGER", 1, None, 0, 0),
    (6, "duration_sec", "INTEGER", 1, None, 0, 0),
    (7, "created_ms", "INTEGER", 1, None, 0, 0),
    (8, "captured_at", "TEXT", 1, None, 0, 0),
    (9, "participants_json", "TEXT", 0, None, 0, 0),
    (10, "seed_family", "TEXT", 1, "''", 0, 0),
    (11, "participants_private_json", "TEXT", 0, None, 0, 0),
)


class PlayerHistorySnapshotError(ValueError):
    """Stable failure that never embeds paths, identities, or secrets."""

    _CODES = frozenset(
        {
            "invalid_configuration",
            "invalid_source",
            "source_schema_invalid",
            "destination_exists",
            "snapshot_failed",
            "publish_failed",
        }
    )

    def __init__(self, code: object = None) -> None:
        safe = code if type(code) is str and code in self._CODES else "snapshot_failed"
        self.code = safe
        super().__init__(safe)


@dataclass(frozen=True, slots=True)
class PlayerHistorySnapshotConfigV1:
    dataset_id: str
    patches: tuple[str, ...]
    generated_date: str
    lookup_secret: bytes = dataclass_field(repr=False)
    event_secret: bytes = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class PlayerHistorySnapshotBuildResultV1:
    """Small build result; never retains lookup or history rows in memory."""

    meta: SnapshotMetaRecordV1
    row_count: int
    ready_lookup_count: int
    ambiguous_lookup_count: int
    selected_source_rows: int
    max_created_ms: int | None


@dataclass(slots=True)
class _OfflineSourceEvidenceV1:
    before: tuple[int, int, int, int] | None = None
    after: tuple[int, int, int, int] | None = None

    def manifest_identity(self) -> dict[str, int]:
        if self.before is None or self.after is None:
            raise PlayerHistorySnapshotError("invalid_source")
        return {
            "device": self.before[0],
            "inode": self.before[1],
            "start_size": self.before[2],
            "end_size": self.after[2],
            "start_mtime_ns": self.before[3],
            "end_mtime_ns": self.after[3],
        }


def _validated_config(config: PlayerHistorySnapshotConfigV1) -> PlayerHistorySnapshotConfigV1:
    try:
        if type(config) is not PlayerHistorySnapshotConfigV1:
            raise ValueError
        lookup_secret = validate_secret(config.lookup_secret)
        event_secret = validate_secret(config.event_secret)
        if lookup_secret == event_secret:
            raise ValueError
        canonicalize_snapshot_meta_v1(
            SnapshotMetaV1(
                dataset_id=config.dataset_id,
                patches=config.patches,
                generated_date=config.generated_date,
                exclusions={key: 0 for key in (*EXCLUSION_CODES_V1, "duplicate_event")},
            )
        )
        return config
    except Exception:
        raise PlayerHistorySnapshotError("invalid_configuration") from None


def _duration_bucket(duration_sec: int) -> str:
    if duration_sec < 15 * 60:
        return "lt_15m"
    if duration_sec < 20 * 60:
        return "15_20m"
    if duration_sec < 25 * 60:
        return "20_25m"
    return "ge_25m"


def build_player_history_graph_v1(
    rows: Iterable[dict[str, object]], *, config: PlayerHistorySnapshotConfigV1
):
    """Validate all rows and build a deterministic deidentified read-model graph."""

    config = _validated_config(config)
    exclusions = {key: 0 for key in (*EXCLUSION_CODES_V1, "duplicate_event")}
    grouped: dict[int, list[_PlayerHistorySourceProjectionV1]] = defaultdict(list)
    try:
        iterator = iter(rows)
    except Exception:
        raise PlayerHistorySnapshotError("invalid_source") from None

    try:
        for row in iterator:
            projection, exclusion = _project_player_history_source_row_v1(
                row,
                queue_id=2400,
                patches=config.patches,
                expected_normalizer_id=NORMALIZER_ID,
            )
            if projection is None:
                exclusions[exclusion or "invalid_source_schema"] += 1
            else:
                grouped[projection.game_id].append(projection)
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("invalid_source") from None

    events: list[_PlayerHistorySourceProjectionV1] = []
    for game_id in sorted(grouped):
        candidates = grouped[game_id]
        first = candidates[0]
        if all(candidate == first for candidate in candidates[1:]):
            events.append(first)
            exclusions["duplicate_event"] += len(candidates) - 1
        else:
            exclusions["duplicate_event"] += len(candidates)

    lookup_to_local_ids: dict[bytes, set[str]] = defaultdict(set)
    lookup_events: dict[
        bytes, list[tuple[_PlayerHistorySourceProjectionV1, object]]
    ] = defaultdict(list)
    for event in events:
        for participant in event.participants:
            lookup_key = derive_lookup_key(
                config.lookup_secret,
                expected_normalizer_id=NORMALIZER_ID,
                normalized_riot_id=participant.normalized_riot_id,
            )
            lookup_to_local_ids[lookup_key].add(participant.player_local_id)
            # Privacy boundary: an alias may expose only matches in which that
            # exact normalized alias appeared.  Never join a player's other
            # aliases through the private PUUID/local identifier.
            lookup_events[lookup_key].append((event, participant))

    lookups = []
    histories = []
    for lookup_key in sorted(lookup_to_local_ids):
        local_ids = lookup_to_local_ids[lookup_key]
        if len(local_ids) != 1:
            lookups.append(
                canonicalize_player_lookup_v1(
                    PlayerLookupV1(lookup_key, "ambiguous", None, None)
                )
            )
            continue

        local_id = next(iter(local_ids))
        ordered_events = sorted(
            lookup_events[lookup_key],
            key=lambda item: (item[0].created_ms, item[0].game_id),
            reverse=True,
        )
        lookups.append(
            canonicalize_player_lookup_v1(
                PlayerLookupV1(
                    lookup_key,
                    "ready",
                    len(ordered_events),
                    len(ordered_events) < 20,
                )
            )
        )
        for ordinal, (event, participant) in enumerate(
            ordered_events[:MAX_STORED_HISTORY_V1], 1
        ):
            blue_player = participant.team_id == 100
            won = blue_player == bool(event.blue_wins)
            histories.append(
                canonicalize_player_history_v1(
                    PlayerHistoryV1(
                        lookup_key=lookup_key,
                        event_key=derive_event_key(
                            config.event_secret,
                            expected_normalizer_id=NORMALIZER_ID,
                            player_local_id=local_id,
                            game_id=event.game_id,
                        ),
                        ordinal=ordinal,
                        patch=event.patch,
                        champion_id=participant.champion_id,
                        outcome="win" if won else "loss",
                        duration_bucket=_duration_bucket(event.duration_sec),
                    ),
                    allowed_patches=config.patches,
                )
            )

    meta = canonicalize_snapshot_meta_v1(
        SnapshotMetaV1(
            dataset_id=config.dataset_id,
            patches=config.patches,
            generated_date=config.generated_date,
            exclusions=exclusions,
        )
    )
    return canonicalize_player_history_graph_v1(
        meta=meta, lookups=lookups, histories=histories
    )


def _stage_local_key_v1(secret: bytes, player_local_id: str) -> bytes:
    """Deidentify the transient ambiguity key before it reaches staging disk."""

    return hmac.digest(
        secret,
        b"player-history-stage-local-v1\x00" + player_local_id.encode("utf-8", "strict"),
        "sha256",
    )


def _insert_snapshot_meta_v1(
    connection: sqlite3.Connection, *, meta, row_count: int
) -> None:
    connection.execute(
        "INSERT INTO main.snapshot_meta "
        "(singleton,schema_version,dataset_id,region,queue_id,patches_json,"
        "generated_date,source,coverage,low_sample_floor,row_count,exclusions_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            1,
            meta.dataset_id,
            "TW",
            2400,
            meta.patches_json,
            meta.generated_date,
            "lcu-captured-offline-snapshot",
            "captured-subset",
            20,
            row_count,
            meta.exclusions_json,
        ),
    )


def _build_streaming_snapshot_temp_v1(
    rows: Iterable[dict[str, object]],
    *,
    destination: Path,
    config: PlayerHistorySnapshotConfigV1,
    stats: _LiveSelectionStatsV1 | None = None,
    source_finished: Callable[[], None] | None = None,
) -> tuple[Path, PlayerHistorySnapshotBuildResultV1]:
    """Build through a disk-backed staging DB without materializing the graph.

    SQLite sources require ``games.game_id`` to be the primary key. Therefore
    each selected source row is one unique event and duplicate reconciliation is
    unnecessary on this production path; the legacy iterable graph builder keeps
    its duplicate policy for small/test callers.
    """

    config = _validated_config(config)
    destination = Path(destination)
    exclusions = {key: 0 for key in (*EXCLUSION_CODES_V1, "duplicate_event")}
    meta = canonicalize_snapshot_meta_v1(
        SnapshotMetaV1(
            dataset_id=config.dataset_id,
            patches=config.patches,
            generated_date=config.generated_date,
            exclusions=exclusions,
        )
    )
    stage_path: Path | None = None
    snapshot_path: Path | None = None
    stage: sqlite3.Connection | None = None
    output: sqlite3.Connection | None = None
    succeeded = False
    selected = 0
    max_created_ms: int | None = None
    try:
        stage_fd, stage_raw = tempfile.mkstemp(
            prefix=f".{destination.name}.stage.", suffix=".tmp", dir=destination.parent
        )
        os.close(stage_fd)
        stage_path = Path(stage_raw)
        snapshot_fd, snapshot_raw = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(snapshot_fd)
        snapshot_path = Path(snapshot_raw)

        stage = sqlite3.connect(str(stage_path))
        stage.execute("PRAGMA journal_mode=DELETE")
        stage.execute("PRAGMA synchronous=FULL")
        stage.executescript(
            """
            CREATE TABLE alias_player (
                lookup_key BLOB NOT NULL,
                local_key BLOB NOT NULL,
                PRIMARY KEY (lookup_key, local_key)
            ) STRICT, WITHOUT ROWID;
            CREATE TABLE staged_event (
                lookup_key BLOB NOT NULL,
                event_key BLOB NOT NULL,
                created_ms INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                patch TEXT NOT NULL,
                champion_id INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                duration_bucket TEXT NOT NULL,
                PRIMARY KEY (lookup_key, event_key)
            ) STRICT, WITHOUT ROWID;
            CREATE INDEX staged_event_order
                ON staged_event(lookup_key, created_ms DESC, game_id DESC, event_key);
            """
        )
        stage.execute("BEGIN IMMEDIATE")
        iterator = iter(rows)
        try:
            for row in iterator:
                selected += 1
                raw_created_ms = row.get("created_ms") if type(row) is dict else None
                if type(raw_created_ms) is int and raw_created_ms >= 0:
                    if max_created_ms is None or raw_created_ms > max_created_ms:
                        max_created_ms = raw_created_ms
                projection, exclusion = _project_player_history_source_row_v1(
                    row,
                    queue_id=_LIVE_QUEUE_ID_V1,
                    patches=config.patches,
                    expected_normalizer_id=NORMALIZER_ID,
                )
                if projection is None:
                    exclusions[exclusion or "invalid_source_schema"] += 1
                    continue
                alias_rows: list[tuple[bytes, bytes]] = []
                event_rows: list[tuple[object, ...]] = []
                for participant in projection.participants:
                    lookup_key = derive_lookup_key(
                        config.lookup_secret,
                        expected_normalizer_id=NORMALIZER_ID,
                        normalized_riot_id=participant.normalized_riot_id,
                    )
                    local_key = _stage_local_key_v1(
                        config.event_secret, participant.player_local_id
                    )
                    event_key = derive_event_key(
                        config.event_secret,
                        expected_normalizer_id=NORMALIZER_ID,
                        player_local_id=participant.player_local_id,
                        game_id=projection.game_id,
                    )
                    blue_player = participant.team_id == 100
                    won = blue_player == bool(projection.blue_wins)
                    alias_rows.append((lookup_key, local_key))
                    event_rows.append(
                        (
                            lookup_key,
                            event_key,
                            projection.created_ms,
                            projection.game_id,
                            projection.patch,
                            participant.champion_id,
                            "win" if won else "loss",
                            _duration_bucket(projection.duration_sec),
                        )
                    )
                stage.executemany(
                    "INSERT OR IGNORE INTO alias_player(lookup_key,local_key) VALUES (?,?)",
                    alias_rows,
                )
                stage.executemany(
                    "INSERT INTO staged_event "
                    "(lookup_key,event_key,created_ms,game_id,patch,champion_id,outcome,"
                    "duration_bucket) VALUES (?,?,?,?,?,?,?,?)",
                    event_rows,
                )
            stage.commit()
        except Exception:
            stage.rollback()
            raise
        finally:
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                close_iterator()
        if source_finished is not None:
            source_finished()

        meta = canonicalize_snapshot_meta_v1(
            SnapshotMetaV1(
                dataset_id=config.dataset_id,
                patches=config.patches,
                generated_date=config.generated_date,
                exclusions=exclusions,
            )
        )
        stage.execute(
            "CREATE TABLE lookup_rollup ("
            "lookup_key BLOB PRIMARY KEY,status TEXT NOT NULL,observed_matches INTEGER"
            ") STRICT, WITHOUT ROWID"
        )
        stage.execute(
            "INSERT INTO lookup_rollup "
            "WITH aliases AS (SELECT lookup_key,count(*) AS players FROM alias_player "
            "GROUP BY lookup_key), events AS (SELECT lookup_key,count(*) AS observed "
            "FROM staged_event GROUP BY lookup_key) "
            "SELECT a.lookup_key,CASE WHEN a.players=1 THEN 'ready' ELSE 'ambiguous' END,"
            "CASE WHEN a.players=1 THEN e.observed ELSE NULL END "
            "FROM aliases a JOIN events e USING(lookup_key)"
        )
        stage.commit()
        stage.close()
        stage = None

        output = sqlite3.connect(str(snapshot_path))
        output.execute("PRAGMA temp_store=FILE")
        if output.execute("PRAGMA temp_store").fetchone() != (1,):
            raise PlayerHistorySnapshotError("snapshot_failed")
        create_player_history_schema(output)
        output.execute("PRAGMA foreign_keys=ON")
        output.execute("ATTACH DATABASE ? AS stage", (str(stage_path),))
        output.execute("BEGIN IMMEDIATE")
        output.execute(
            "INSERT INTO main.player_lookup(lookup_key,status,observed_matches,low_sample) "
            "SELECT lookup_key,status,observed_matches,"
            "CASE WHEN status='ready' THEN observed_matches<20 ELSE NULL END "
            "FROM stage.lookup_rollup ORDER BY lookup_key"
        )
        output.execute(
            "INSERT INTO main.player_history "
            "(lookup_key,lookup_status,event_key,ordinal,patch,champion_id,outcome,duration_bucket) "
            "SELECT lookup_key,'ready',event_key,ordinal,patch,champion_id,outcome,duration_bucket "
            "FROM (SELECT e.*,row_number() OVER (PARTITION BY e.lookup_key "
            "ORDER BY e.created_ms DESC,e.game_id DESC,e.event_key) AS ordinal "
            "FROM stage.staged_event e JOIN stage.lookup_rollup l USING(lookup_key) "
            "WHERE l.status='ready') WHERE ordinal<=? ORDER BY lookup_key,ordinal",
            (MAX_STORED_HISTORY_V1,),
        )
        row_count = output.execute(
            "SELECT count(*) FROM main.player_history"
        ).fetchone()[0]
        _insert_snapshot_meta_v1(output, meta=meta, row_count=row_count)
        output.commit()
        output.execute("DETACH DATABASE stage")
        summary = audit_player_history_snapshot_streaming_v1(output)
        if summary.meta != meta or summary.row_count != row_count:
            raise PlayerHistorySnapshotError("snapshot_failed")
        output.close()
        output = None
        with snapshot_path.open("r+b") as snapshot_file:
            snapshot_file.flush()
            os.fsync(snapshot_file.fileno())
        if stats is not None and (
            stats.selected_source_rows != selected or stats.max_created_ms != max_created_ms
        ):
            raise PlayerHistorySnapshotError("snapshot_failed")
        succeeded = True
        return snapshot_path, PlayerHistorySnapshotBuildResultV1(
            meta=summary.meta,
            row_count=summary.row_count,
            ready_lookup_count=summary.ready_lookup_count,
            ambiguous_lookup_count=summary.ambiguous_lookup_count,
            selected_source_rows=selected,
            max_created_ms=max_created_ms,
        )
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("snapshot_failed") from None
    finally:
        if output is not None:
            try:
                output.close()
            except sqlite3.Error:
                pass
        if stage is not None:
            try:
                stage.close()
            except sqlite3.Error:
                pass
        if stage_path is not None:
            try:
                stage_path.unlink(missing_ok=True)
            except OSError:
                pass
        if snapshot_path is not None and not succeeded:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass


def publish_player_history_snapshot_v1(destination: Path, graph) -> None:
    """Create, audit, fsync, and hard-link a snapshot without overwriting."""

    try:
        destination = Path(destination)
        if destination.exists():
            raise PlayerHistorySnapshotError("destination_exists")
        parent = destination.parent
        if not parent.is_dir():
            raise PlayerHistorySnapshotError("publish_failed")
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("publish_failed") from None

    temp_path: Path | None = None
    connection: sqlite3.Connection | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent)
        os.close(fd)
        temp_path = Path(raw_temp)
        connection = sqlite3.connect(str(temp_path))
        create_player_history_schema(connection)
        write_player_history_graph_v1(connection, graph)
        if audit_player_history_snapshot_v1(connection) != graph:
            raise PlayerHistorySnapshotError("snapshot_failed")
        connection.close()
        connection = None
        with temp_path.open("r+b") as snapshot_file:
            snapshot_file.flush()
            os.fsync(snapshot_file.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            raise PlayerHistorySnapshotError("destination_exists") from None
        except OSError:
            raise PlayerHistorySnapshotError("publish_failed") from None
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("snapshot_failed") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def build_and_publish_player_history_snapshot_v1(
    rows: Iterable[dict[str, object]], *, destination: Path, config: PlayerHistorySnapshotConfigV1
):
    graph = build_player_history_graph_v1(rows, config=config)
    publish_player_history_snapshot_v1(destination, graph)
    return graph


def _build_started_utc_v1() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_audited_snapshot_temp_v1(destination: Path, graph) -> Path:
    temp_path: Path | None = None
    connection: sqlite3.Connection | None = None
    succeeded = False
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temp_path = Path(raw_temp)
        connection = sqlite3.connect(str(temp_path))
        create_player_history_schema(connection)
        write_player_history_graph_v1(connection, graph)
        if audit_player_history_snapshot_v1(connection) != graph:
            raise PlayerHistorySnapshotError("snapshot_failed")
        connection.close()
        connection = None
        with temp_path.open("r+b") as snapshot_file:
            snapshot_file.flush()
            os.fsync(snapshot_file.fileno())
        succeeded = True
        return temp_path
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("snapshot_failed") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if temp_path is not None and not succeeded:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_manifest_temp_v1(destination: Path, manifest: dict[str, object]) -> Path:
    temp_path: Path | None = None
    try:
        payload = (
            json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii", "strict")
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temp_path = Path(raw_temp)
        temp_path.write_bytes(payload)
        with temp_path.open("r+b") as manifest_file:
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        return temp_path
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise PlayerHistorySnapshotError("snapshot_failed") from None


def _sha256_file_v1(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_own_publication_v1(destination: Path, temp_path: Path) -> None:
    try:
        published = os.stat(destination, follow_symlinks=False)
        temporary = os.stat(temp_path, follow_symlinks=False)
        if (published.st_dev, published.st_ino) == (temporary.st_dev, temporary.st_ino):
            destination.unlink()
    except OSError:
        raise PlayerHistorySnapshotError("publish_failed") from None


def _publish_live_pair_v1(
    *, destination: Path, manifest_destination: Path, graph, manifest: dict[str, object]
) -> None:
    destination = Path(destination)
    manifest_destination = Path(manifest_destination)
    snapshot_temp: Path | None = None
    manifest_temp: Path | None = None
    manifest_published = False
    try:
        if destination == manifest_destination:
            raise PlayerHistorySnapshotError("publish_failed")
        if os.path.lexists(destination) or os.path.lexists(manifest_destination):
            raise PlayerHistorySnapshotError("destination_exists")
        if not destination.parent.is_dir() or not manifest_destination.parent.is_dir():
            raise PlayerHistorySnapshotError("publish_failed")

        snapshot_temp = _write_audited_snapshot_temp_v1(destination, graph)
        snapshot_sha256 = hashlib.sha256(snapshot_temp.read_bytes()).hexdigest()
        manifest = {**manifest, "snapshot_sha256": snapshot_sha256}
        manifest_temp = _write_manifest_temp_v1(manifest_destination, manifest)
        try:
            os.link(manifest_temp, manifest_destination)
            manifest_published = True
            # The snapshot is the pair's commit marker: observers must not use
            # a manifest unless its named snapshot has appeared.
            os.link(snapshot_temp, destination)
        except FileExistsError:
            if manifest_published:
                _unlink_own_publication_v1(manifest_destination, manifest_temp)
                manifest_published = False
            raise PlayerHistorySnapshotError("destination_exists") from None
        except OSError:
            if manifest_published:
                _unlink_own_publication_v1(manifest_destination, manifest_temp)
                manifest_published = False
            raise PlayerHistorySnapshotError("publish_failed") from None
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        if manifest_published and manifest_temp is not None:
            _unlink_own_publication_v1(manifest_destination, manifest_temp)
        raise PlayerHistorySnapshotError("snapshot_failed") from None
    finally:
        for temp_path in (snapshot_temp, manifest_temp):
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _publish_prebuilt_pair_v1(
    *,
    destination: Path,
    manifest_destination: Path,
    snapshot_temp: Path,
    manifest: dict[str, object],
) -> None:
    manifest_temp: Path | None = None
    manifest_published = False
    try:
        if destination == manifest_destination:
            raise PlayerHistorySnapshotError("publish_failed")
        if os.path.lexists(destination) or os.path.lexists(manifest_destination):
            raise PlayerHistorySnapshotError("destination_exists")
        if not destination.parent.is_dir() or not manifest_destination.parent.is_dir():
            raise PlayerHistorySnapshotError("publish_failed")
        manifest_temp = _write_manifest_temp_v1(
            manifest_destination,
            {**manifest, "snapshot_sha256": _sha256_file_v1(snapshot_temp)},
        )
        try:
            os.link(manifest_temp, manifest_destination)
            manifest_published = True
            os.link(snapshot_temp, destination)
        except FileExistsError:
            if manifest_published:
                _unlink_own_publication_v1(manifest_destination, manifest_temp)
            raise PlayerHistorySnapshotError("destination_exists") from None
        except OSError:
            if manifest_published:
                _unlink_own_publication_v1(manifest_destination, manifest_temp)
            raise PlayerHistorySnapshotError("publish_failed") from None
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        if manifest_published and manifest_temp is not None:
            _unlink_own_publication_v1(manifest_destination, manifest_temp)
        raise PlayerHistorySnapshotError("snapshot_failed") from None
    finally:
        if manifest_temp is not None:
            try:
                manifest_temp.unlink(missing_ok=True)
            except OSError:
                pass


def _canonical_live_database_paths() -> frozenset[Path]:
    repository = Path(__file__).resolve().parents[3]
    live = (repository / "data" / "lcu" / "games.db").resolve()
    return frozenset((live, Path(f"{live}-wal"), Path(f"{live}-shm")))


def _looks_like_canonical_live_database(path: Path) -> bool:
    """Reject a live-layout DB even when this code runs from another worktree."""

    lowered = tuple(part.casefold() for part in path.parts)
    return len(lowered) >= 3 and lowered[-3:] == ("data", "lcu", "games.db")


def _aliases_canonical_live_database(path: Path) -> bool:
    for live_path in _canonical_live_database_paths():
        try:
            if live_path.exists() and os.path.samefile(path, live_path):
                return True
        except OSError:
            return True
    return False


def _read_player_history_source_sqlite_filtered_v1(
    source_path: Path,
    *,
    patches: tuple[str, ...] | None,
    evidence: _OfflineSourceEvidenceV1 | None = None,
) -> Iterator[dict[str, object]]:
    """Read the approved projection, optionally filtering in SQL before JSON parse."""

    try:
        supplied = Path(source_path)
        if supplied.is_symlink():
            raise PlayerHistorySnapshotError("invalid_source")
        path = supplied.resolve(strict=True)
        if (
            path in _canonical_live_database_paths()
            or _looks_like_canonical_live_database(path)
            or _aliases_canonical_live_database(path)
            or path.name.endswith(("-wal", "-shm"))
            or Path(f"{path}-wal").exists()
            or Path(f"{path}-shm").exists()
        ):
            raise PlayerHistorySnapshotError("invalid_source")
        if not path.is_file():
            raise PlayerHistorySnapshotError("invalid_source")
        before = path.stat()
        source_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if evidence is not None:
            evidence.before = source_identity
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
    except PlayerHistorySnapshotError:
        raise
    except Exception:
        raise PlayerHistorySnapshotError("invalid_source") from None

    try:
        table = connection.execute(
            "SELECT type FROM main.sqlite_schema WHERE name='games'"
        ).fetchone()
        if table != ("table",):
            raise PlayerHistorySnapshotError("source_schema_invalid")
        column_rows = tuple(connection.execute("PRAGMA main.table_info('games')"))
        columns = {row[1]: row for row in column_rows}
        expected_descriptors = {
            "game_id": ("TEXT", 0, 1),
            "queue_id": ("INTEGER", 1, 0),
            "patch": ("TEXT", 1, 0),
            "blue_wins": ("INTEGER", 1, 0),
            "duration_sec": ("INTEGER", 1, 0),
            "created_ms": ("INTEGER", 1, 0),
            "participants_json": ("TEXT", 0, 0),
            "participants_private_json": ("TEXT", 0, 0),
        }
        if any(
            name not in columns
            or (columns[name][2].upper(), columns[name][3], columns[name][5]) != descriptor
            for name, descriptor in expected_descriptors.items()
        ):
            raise PlayerHistorySnapshotError("source_schema_invalid")
        sql = (
            "SELECT game_id,queue_id,patch,blue_wins,duration_sec,created_ms,"
            "participants_json,participants_private_json FROM main.games"
        )
        parameters: tuple[object, ...] = ()
        if patches is not None:
            predicates = " OR ".join("patch LIKE ?" for _ in patches)
            sql += f" WHERE queue_id=? AND ({predicates})"
            parameters = (_LIVE_QUEUE_ID_V1, *(f"{patch}.%" for patch in patches))
        cursor = connection.execute(sql, parameters)
        for values in cursor:
            yield dict(zip(SOURCE_COLUMNS_V1, values, strict=True))
        after = path.stat()
        if (
            Path(f"{path}-wal").exists()
            or Path(f"{path}-shm").exists()
            or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            ) != source_identity
        ):
            raise PlayerHistorySnapshotError("invalid_source")
        if evidence is not None:
            evidence.after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
    except PlayerHistorySnapshotError:
        raise
    except (sqlite3.Error, OSError, ValueError):
        raise PlayerHistorySnapshotError("invalid_source") from None
    finally:
        connection.close()


def read_player_history_source_sqlite_v1(source_path: Path) -> Iterator[dict[str, object]]:
    """Compatibility adapter returning the approved unfiltered projection."""

    yield from _read_player_history_source_sqlite_filtered_v1(
        source_path, patches=None
    )


@dataclass(frozen=True, slots=True)
class _LiveFileIdentityV1:
    device: int
    inode: int
    size: int


@dataclass(slots=True)
class _LiveSelectionStatsV1:
    selected_source_rows: int = 0
    max_created_ms: int | None = None

    def observe(self, created_ms: object) -> None:
        if type(created_ms) is not int or created_ms < 0:
            raise PlayerHistorySnapshotError("invalid_source")
        self.selected_source_rows += 1
        if self.max_created_ms is None or created_ms > self.max_created_ms:
            self.max_created_ms = created_ms


def _module_repository_root_v1() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_reparse_or_symlink_v1(status: os.stat_result) -> bool:
    attributes = int(getattr(status, "st_file_attributes", 0))
    return stat.S_ISLNK(status.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_non_reparse_components_v1(
    path: Path, *, final_must_be_directory: bool
) -> None:
    """Reject missing, wrong-kind, symlink, and Windows reparse components."""

    if not path.is_absolute() or not path.anchor:
        raise PlayerHistorySnapshotError("invalid_source")
    current = Path(path.anchor)
    components = path.parts[1:]
    try:
        for index, component in enumerate(components):
            current /= component
            status = os.lstat(current)
            if _is_reparse_or_symlink_v1(status):
                raise PlayerHistorySnapshotError("invalid_source")
            final = index == len(components) - 1
            if final:
                expected_kind = (
                    stat.S_ISDIR(status.st_mode)
                    if final_must_be_directory
                    else stat.S_ISREG(status.st_mode)
                )
                if not expected_kind:
                    raise PlayerHistorySnapshotError("invalid_source")
            elif not stat.S_ISDIR(status.st_mode):
                raise PlayerHistorySnapshotError("invalid_source")
    except PlayerHistorySnapshotError:
        raise
    except OSError:
        raise PlayerHistorySnapshotError("invalid_source") from None


def _assert_live_path_components_v1(path: Path) -> None:
    _assert_non_reparse_components_v1(path, final_must_be_directory=False)


def _trusted_primary_checkout_root_v1() -> Path:
    """Resolve the Git common primary checkout without caller-controlled input."""

    module_root = _module_repository_root_v1()
    dot_git = module_root / ".git"
    try:
        dot_git_status = os.lstat(dot_git)
        if _is_reparse_or_symlink_v1(dot_git_status):
            raise PlayerHistorySnapshotError("invalid_source")
        if stat.S_ISDIR(dot_git_status.st_mode):
            _assert_non_reparse_components_v1(
                dot_git, final_must_be_directory=True
            )
            return module_root
        if not stat.S_ISREG(dot_git_status.st_mode) or dot_git_status.st_size > 4096:
            raise PlayerHistorySnapshotError("invalid_source")
        _assert_non_reparse_components_v1(
            dot_git, final_must_be_directory=False
        )
        raw = dot_git.read_bytes()
        text = raw.decode("ascii", "strict")
        lines = text.splitlines()
        if len(lines) != 1 or text not in (
            lines[0],
            f"{lines[0]}\n",
            f"{lines[0]}\r\n",
        ):
            raise PlayerHistorySnapshotError("invalid_source")
        prefix = "gitdir: "
        if not lines[0].startswith(prefix) or len(lines[0]) == len(prefix):
            raise PlayerHistorySnapshotError("invalid_source")
        supplied_gitdir = Path(lines[0][len(prefix) :])
        if not supplied_gitdir.is_absolute():
            raise PlayerHistorySnapshotError("invalid_source")
        _assert_non_reparse_components_v1(
            supplied_gitdir, final_must_be_directory=True
        )
        resolved_gitdir = supplied_gitdir.resolve(strict=True)
        _assert_non_reparse_components_v1(
            resolved_gitdir, final_must_be_directory=True
        )
        if (
            resolved_gitdir.parent.name != "worktrees"
            or resolved_gitdir.parent.parent.name != ".git"
            or not resolved_gitdir.name
        ):
            raise PlayerHistorySnapshotError("invalid_source")
        primary_root = resolved_gitdir.parents[2]
        _assert_non_reparse_components_v1(
            primary_root / ".git", final_must_be_directory=True
        )
        return primary_root
    except PlayerHistorySnapshotError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PlayerHistorySnapshotError("invalid_source") from None


def _trusted_live_database_path_v1() -> Path:
    """Return the Git-common primary checkout's one accepted live DB path."""

    return _trusted_primary_checkout_root_v1() / "data" / "lcu" / "games.db"


def _live_file_identity_v1(
    path: Path, *, required: bool
) -> _LiveFileIdentityV1 | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise PlayerHistorySnapshotError("invalid_source") from None
        return None
    except OSError:
        raise PlayerHistorySnapshotError("invalid_source") from None
    if _is_reparse_or_symlink_v1(status) or not stat.S_ISREG(status.st_mode):
        raise PlayerHistorySnapshotError("invalid_source")
    return _LiveFileIdentityV1(status.st_dev, status.st_ino, status.st_size)


@dataclass(slots=True)
class _LiveReadGuardV1:
    database_path: Path
    deadline: float
    identities: dict[Path, _LiveFileIdentityV1]
    start_identities: dict[Path, _LiveFileIdentityV1]
    wal_baseline_size: int
    interrupted: bool = False

    @property
    def companion_paths(self) -> tuple[Path, Path]:
        return (
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        )

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise PlayerHistorySnapshotError("invalid_source")
        _assert_live_path_components_v1(self.database_path)
        for path in (self.database_path, *self.companion_paths):
            current = _live_file_identity_v1(
                path, required=path == self.database_path
            )
            expected = self.identities.get(path)
            if current is None:
                if expected is not None:
                    raise PlayerHistorySnapshotError("invalid_source")
                continue
            if expected is None:
                self.identities[path] = current
            elif (current.device, current.inode) != (
                expected.device,
                expected.inode,
            ):
                raise PlayerHistorySnapshotError("invalid_source")
            if (
                path == self.companion_paths[0]
                and current.size - self.wal_baseline_size
                > _LIVE_WAL_MAX_GROWTH_BYTES_V1
            ):
                raise PlayerHistorySnapshotError("invalid_source")

    def progress(self) -> int:
        try:
            self.check()
        except Exception:
            self.interrupted = True
            return 1
        return 0


def _new_live_read_guard_v1(database_path: Path) -> _LiveReadGuardV1:
    _assert_live_path_components_v1(database_path)
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    identities: dict[Path, _LiveFileIdentityV1] = {}
    for path, required in (
        (database_path, True),
        (wal_path, False),
        (shm_path, False),
    ):
        identity = _live_file_identity_v1(path, required=required)
        if identity is not None:
            identities[path] = identity
    wal_identity = identities.get(wal_path)
    return _LiveReadGuardV1(
        database_path=database_path,
        deadline=time.monotonic() + _LIVE_READ_MAX_SECONDS_V1,
        identities=identities,
        start_identities=identities.copy(),
        wal_baseline_size=wal_identity.size if wal_identity is not None else 0,
    )


def _live_source_file_evidence_v1(
    guard: _LiveReadGuardV1,
) -> dict[str, dict[str, int | None]]:
    guard.check()
    evidence: dict[str, dict[str, int | None]] = {}
    paths = (guard.database_path, *guard.companion_paths)
    for role, path in zip(("db", "wal", "shm"), paths, strict=True):
        start = guard.start_identities.get(path)
        end = _live_file_identity_v1(path, required=role == "db")
        identity = end if end is not None else start
        if identity is not None and start is not None and end is not None:
            if (start.device, start.inode) != (end.device, end.inode):
                raise PlayerHistorySnapshotError("invalid_source")
        evidence[role] = {
            "device": identity.device if identity is not None else None,
            "inode": identity.inode if identity is not None else None,
            "start_size": start.size if start is not None else None,
            "end_size": end.size if end is not None else None,
        }
    return evidence


def _configure_live_connection_v1(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA busy_timeout=250")
    if (
        connection.execute("PRAGMA query_only").fetchone() != (1,)
        or connection.execute("PRAGMA trusted_schema").fetchone() != (0,)
        or connection.execute("PRAGMA temp_store").fetchone() != (2,)
        or connection.execute("PRAGMA busy_timeout").fetchone() != (250,)
    ):
        raise PlayerHistorySnapshotError("invalid_source")


def _require_live_source_schema_v1(connection: sqlite3.Connection) -> None:
    schema_row = connection.execute(
        "SELECT type,name,tbl_name FROM main.sqlite_schema WHERE name=?",
        ("games",),
    ).fetchone()
    table_list_rows = tuple(
        row for row in connection.execute("PRAGMA main.table_list") if row[1] == "games"
    )
    if (
        schema_row != ("table", "games", "games")
        or len(table_list_rows) != 1
        or tuple(table_list_rows[0][:3]) != ("main", "games", "table")
        or tuple(connection.execute("PRAGMA main.table_xinfo('games')"))
        != _EXPECTED_LIVE_GAMES_XINFO_V1
    ):
        raise PlayerHistorySnapshotError("source_schema_invalid")


def _iter_live_source_rows_v1(
    connection: sqlite3.Connection,
    *,
    patches: tuple[str, ...],
    guard: _LiveReadGuardV1,
    stats: _LiveSelectionStatsV1,
) -> Iterator[dict[str, object]]:
    predicates = " OR ".join("patch LIKE ?" for _ in patches)
    parameters: tuple[object, ...] = (
        _LIVE_QUEUE_ID_V1,
        *(f"{patch}.%" for patch in patches),
    )
    guard.check()
    cursor = connection.execute(
        "SELECT game_id,queue_id,patch,blue_wins,duration_sec,created_ms,"
        "participants_json,participants_private_json FROM main.games "
        f"WHERE queue_id=? AND ({predicates})",
        parameters,
    )
    for values in cursor:
        guard.check()
        stats.observe(values[5])
        yield dict(zip(SOURCE_COLUMNS_V1, values, strict=True))
    guard.check()


def build_and_publish_player_history_snapshot_from_live_v1(
    *, destination: Path, manifest: Path, config: PlayerHistorySnapshotConfigV1
):
    """Stream the trusted live DB into one audited snapshot/manifest pair."""

    validated_config = _validated_config(config)
    destination = Path(destination)
    manifest = Path(manifest)
    if destination == manifest or os.path.lexists(destination) or os.path.lexists(manifest):
        raise PlayerHistorySnapshotError("destination_exists")
    if not destination.parent.is_dir() or not manifest.parent.is_dir():
        raise PlayerHistorySnapshotError("publish_failed")
    build_started_utc = _build_started_utc_v1()
    database_path = _trusted_live_database_path_v1()
    guard = _new_live_read_guard_v1(database_path)
    stats = _LiveSelectionStatsV1()
    connection: sqlite3.Connection | None = None
    snapshot_temp: Path | None = None
    source_files: dict[str, dict[str, int | None]] | None = None
    cleanup_failed = False
    try:
        uri = f"file:{quote(database_path.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        _configure_live_connection_v1(connection)
        connection.set_progress_handler(guard.progress, _LIVE_PROGRESS_OPCODES_V1)
        guard.check()
        connection.execute("BEGIN")
        _require_live_source_schema_v1(connection)

        def finish_source() -> None:
            nonlocal connection, source_files
            assert connection is not None
            guard.check()
            source_files = _live_source_file_evidence_v1(guard)
            connection.set_progress_handler(None, 0)
            connection.rollback()
            connection.close()
            connection = None

        snapshot_temp, result = _build_streaming_snapshot_temp_v1(
            _iter_live_source_rows_v1(
                connection,
                patches=validated_config.patches,
                guard=guard,
                stats=stats,
            ),
            destination=destination,
            config=validated_config,
            stats=stats,
            source_finished=finish_source,
        )
        if source_files is None:
            raise PlayerHistorySnapshotError("invalid_source")
        patches = json.loads(result.meta.patches_json)
        exclusions = json.loads(result.meta.exclusions_json)
        private_manifest: dict[str, object] = {
            "schema_version": 1,
            "dataset_id": result.meta.dataset_id,
            "region": "TW",
            "queue_id": _LIVE_QUEUE_ID_V1,
            "patches": patches,
            "coverage": "captured-subset",
            "generated_date": result.meta.generated_date,
            "build_started_utc": build_started_utc,
            "source_class": "git-common-primary/data/lcu/games.db",
            "source_identities": source_files,
            "selected_source_rows": result.selected_source_rows,
            "max_created_ms": result.max_created_ms,
            "exclusions": exclusions,
            "snapshot_row_count": result.row_count,
            "audit_status": "ok",
            "public_identifiers_emitted": False,
        }
        _publish_prebuilt_pair_v1(
            destination=destination,
            manifest_destination=manifest,
            snapshot_temp=snapshot_temp,
            manifest=private_manifest,
        )
        return result
    except PlayerHistorySnapshotError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise PlayerHistorySnapshotError("invalid_source") from None
    finally:
        if connection is not None:
            try:
                connection.set_progress_handler(None, 0)
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                cleanup_failed = True
            try:
                connection.close()
            except sqlite3.Error:
                cleanup_failed = True
        if snapshot_temp is not None:
            try:
                snapshot_temp.unlink(missing_ok=True)
            except OSError:
                pass
        if cleanup_failed:
            raise PlayerHistorySnapshotError("invalid_source") from None


def build_and_publish_player_history_snapshot_from_sqlite_v1(
    *,
    source: Path,
    destination: Path,
    manifest: Path,
    config: PlayerHistorySnapshotConfigV1,
) -> PlayerHistorySnapshotBuildResultV1:
    """Stream an immutable backup and publish a private lineage pair."""

    validated_config = _validated_config(config)
    destination = Path(destination)
    manifest = Path(manifest)
    if destination == manifest:
        raise PlayerHistorySnapshotError("publish_failed")
    if os.path.lexists(destination) or os.path.lexists(manifest):
        raise PlayerHistorySnapshotError("destination_exists")
    if not destination.parent.is_dir() or not manifest.parent.is_dir():
        raise PlayerHistorySnapshotError("publish_failed")
    snapshot_temp: Path | None = None
    evidence = _OfflineSourceEvidenceV1()
    build_started_utc = _build_started_utc_v1()
    try:
        snapshot_temp, result = _build_streaming_snapshot_temp_v1(
            _read_player_history_source_sqlite_filtered_v1(
                source,
                patches=validated_config.patches,
                evidence=evidence,
            ),
            destination=destination,
            config=validated_config,
        )
        private_manifest: dict[str, object] = {
            "schema_version": 1,
            "dataset_id": result.meta.dataset_id,
            "region": "TW",
            "queue_id": _LIVE_QUEUE_ID_V1,
            "patches": json.loads(result.meta.patches_json),
            "coverage": "captured-subset",
            "generated_date": result.meta.generated_date,
            "build_started_utc": build_started_utc,
            "source_class": "operator-sqlite-backup",
            "source_identity": evidence.manifest_identity(),
            "selected_source_rows": result.selected_source_rows,
            "max_created_ms": result.max_created_ms,
            "exclusions": json.loads(result.meta.exclusions_json),
            "snapshot_row_count": result.row_count,
            "audit_status": "ok",
            "public_identifiers_emitted": False,
        }
        _publish_prebuilt_pair_v1(
            destination=destination,
            manifest_destination=manifest,
            snapshot_temp=snapshot_temp,
            manifest=private_manifest,
        )
        return result
    finally:
        if snapshot_temp is not None:
            try:
                snapshot_temp.unlink(missing_ok=True)
            except OSError:
                pass


def _secret_from_env(name: str) -> bytes:
    value = os.environ.get(name)
    if value is None:
        value = click.prompt(name, hide_input=True)
    try:
        secret = bytes.fromhex(value)
        return validate_secret(secret)
    except Exception:
        raise click.ClickException("invalid secret") from None


@click.command("player-history-build")
@click.option("--source", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--live-source", is_flag=True, default=False)
@click.option(
    "--manifest",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option("--destination", type=click.Path(path_type=Path, dir_okay=False), required=True)
@click.option("--dataset-id", required=True)
@click.option("--patch", "patches", multiple=True, required=True)
@click.option("--generated-date", required=True)
def main(
    source: Path | None,
    live_source: bool,
    manifest: Path,
    destination: Path,
    dataset_id: str,
    patches: tuple[str, ...],
    generated_date: str,
) -> None:
    """Build an audited private snapshot from offline or trusted live SQLite."""

    try:
        validated_config = _validated_config(
            PlayerHistorySnapshotConfigV1(
                dataset_id=dataset_id,
                patches=patches,
                generated_date=generated_date,
                lookup_secret=_secret_from_env("ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX"),
                event_secret=_secret_from_env("ARAM_PLAYER_HISTORY_EVENT_SECRET_HEX"),
            )
        )
        if (source is None) == (not live_source):
            raise PlayerHistorySnapshotError("invalid_source")
        if live_source:
            graph = build_and_publish_player_history_snapshot_from_live_v1(
                destination=destination,
                manifest=manifest,
                config=validated_config,
            )
        else:
            assert source is not None
            graph = build_and_publish_player_history_snapshot_from_sqlite_v1(
                source=source,
                manifest=manifest,
                destination=destination,
                config=validated_config,
            )
    except PlayerHistorySnapshotError as exc:
        raise click.ClickException(exc.code) from None
    click.echo(json.dumps({"status": "ready", "row_count": graph.row_count}, separators=(",", ":")))
