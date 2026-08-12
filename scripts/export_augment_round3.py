"""Export ended-patch participants and materialize the round-3 physical split.

The SQLite path is opened in URI read-only mode and all source reads occur in
one explicitly established read transaction.  Public artifacts contain only
the compact participant schema below; errors are intentionally fixed codes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import click
import pyarrow as pa
import pyarrow.parquet as pq


QUEUE_ID = 2400
ENDED_PATCHES = ("16.12", "16.13")
CURRENT_PATCH_SHA256 = "e08feb9b84080bbd144dab2d9508dc0f49e8fc6dc2f56a71917e174e8beac219"
VALIDATION_START = (1786108273146, 929409)
TEST_START = (1786213823614, 1028460)
SCHEMA = pa.schema([
    ("gidx", pa.int64()), ("created_ms", pa.int64()), ("patch", pa.string()),
    ("champ", pa.int64()), ("win", pa.int8()), ("augments", pa.list_(pa.int64())),
])
# The canonical index is ``(queue_id, patch, created_ms)``; SQLite secondary
# indexes use rowid as their final tie-break.  This order therefore gives a
# stable snapshot-local enumeration without sorting the 60 GB source.
EXPORT_ROWS_SQL = (
    "SELECT patch, created_ms, blue_wins, participants_json "
    "FROM games INDEXED BY idx_games_queue_patch_created "
    "WHERE queue_id=? AND created_ms<=? AND "
    "((patch=? OR patch LIKE ?) OR (patch=? OR patch LIKE ?)) "
    "ORDER BY patch, created_ms, rowid"
)
EXPORT_META_KEYS = frozenset({
    "schema_version", "source_kind", "source", "output", "queue",
    "requested_patches", "requested_patch_counts", "accepted_patch_counts",
    "accepted_patch_ranges", "exclusions", "cutoff_created_ms", "games",
    "participants", "output_size", "output_sha256",
})
SPLIT_META_KEYS = frozenset({
    "schema_version", "source_kind", "artifact_kind", "sources", "output",
    "queue", "patches", "games", "participants", "validation_start_source",
    "validation_start_materialized", "test_start_source", "split_rule",
    "source_sha256", "output_size", "output_sha256",
})


class Round3ExportError(RuntimeError):
    pass


def _fail(code: str) -> Round3ExportError:
    return Round3ExportError(code)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _prefix(value: object) -> str:
    parts = str(value or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(value or "")


def _safe_meta(meta: Mapping[str, Any], allowlist: frozenset[str]) -> bytes:
    if set(meta) != allowlist:
        raise _fail("E_META_CONTRACT")
    for key in ("source", "output"):
        value = meta.get(key)
        if isinstance(value, str) and value != Path(value).name:
            raise _fail("E_META_LOGICAL_NAME")
    for value in meta.get("sources", []):
        if value != Path(value).name:
            raise _fail("E_META_LOGICAL_NAME")
    return (json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_atomic_bytes(path: Path, content: bytes) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_bytes(content)
        os.replace(partial, path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise _fail("E_ATOMIC_WRITE") from None


def _participant_rows(raw: object, blue_wins: object) -> tuple[list[tuple[int, int, list[int]]] | None, str | None]:
    try:
        roster = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(roster, list) or len(roster) != 10:
        return None, "invalid_roster"
    result: list[tuple[int, int, list[int]]] = []
    team_counts = {100: 0, 200: 0}
    for participant in roster:
        if not isinstance(participant, dict):
            return None, "invalid_participant"
        try:
            team = int(participant.get("teamId", 0))
            champion = int(participant.get("championId", 0))
            raw_augments = participant.get("augments") or []
            if not isinstance(raw_augments, list):
                return None, "invalid_participant"
            augments = [int(value) for value in raw_augments if int(value) > 0][:4]
        except (TypeError, ValueError):
            return None, "invalid_participant"
        if team not in team_counts or champion <= 0:
            return None, "invalid_participant"
        team_counts[team] += 1
        result.append((champion, int((team == 100) == bool(blue_wins)), augments))
    if team_counts != {100: 5, 200: 5}:
        return None, "invalid_team_shape"
    return result, None


def _assert_no_temp_sort(connection: sqlite3.Connection, parameters: tuple[Any, ...]) -> None:
    """Fail before export if SQLite cannot stream the canonical index order."""
    plan = connection.execute("EXPLAIN QUERY PLAN " + EXPORT_ROWS_SQL, parameters).fetchall()
    details = " ".join(str(row[3]).upper() for row in plan)
    if "TEMP B-TREE" in details or "SORT" in details:
        raise _fail("E_EXPORT_QUERY_PLAN")


def export_ended_patches(
    db: Path,
    output: Path,
    *,
    patches: Sequence[str] = ENDED_PATCHES,
    after_snapshot: Callable[[], None] | None = None,
    min_games: int = 1,
) -> dict[str, Any]:
    """Stream a transactionally consistent, privacy-minimal parquet."""
    requested = tuple(sorted(str(value) for value in patches))
    if requested != ENDED_PATCHES:
        raise _fail("E_PATCH_SCOPE")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    meta_path = output.with_suffix(output.suffix + ".meta.json")
    meta_partial = meta_path.with_suffix(meta_path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    meta_partial.unlink(missing_ok=True)
    connection: sqlite3.Connection | None = None
    writer: pq.ParquetWriter | None = None
    try:
        connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        # This first read establishes the snapshot.  It contains a time-only
        # cutoff plus raw requested counts; all later range/row reads share it.
        first = connection.execute(
            "SELECT MAX(created_ms), "
            "SUM(CASE WHEN (patch=? OR patch LIKE ?) THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN (patch=? OR patch LIKE ?) THEN 1 ELSE 0 END) "
            "FROM games WHERE queue_id=?",
            ("16.12", "16.12.%", "16.13", "16.13.%", QUEUE_ID),
        ).fetchone()
        cutoff = int(first[0]) if first and first[0] is not None else 0
        requested_counts = {"16.12": int(first[1] or 0), "16.13": int(first[2] or 0)}
        if after_snapshot is not None:
            after_snapshot()
        ranges: dict[str, list[int | None]] = {}
        for patch in requested:
            row = connection.execute(
                "SELECT MIN(created_ms), MAX(created_ms) FROM games "
                "WHERE queue_id=? AND created_ms<=? AND (patch=? OR patch LIKE ?)",
                (QUEUE_ID, cutoff, patch, f"{patch}.%"),
            ).fetchone()
            ranges[patch] = [int(row[0]) if row and row[0] is not None else None,
                             int(row[1]) if row and row[1] is not None else None]
        row_parameters = (QUEUE_ID, cutoff, "16.12", "16.12.%", "16.13", "16.13.%")
        _assert_no_temp_sort(connection, row_parameters)
        cursor = connection.execute(EXPORT_ROWS_SQL, row_parameters)
        writer = pq.ParquetWriter(partial, SCHEMA, compression="zstd")
        accepted = {patch: 0 for patch in requested}
        exclusions = {key: 0 for key in ("invalid_time", "invalid_json", "invalid_roster", "invalid_participant", "invalid_team_shape")}
        gidx = 0
        columns: dict[str, list[Any]] = {name: [] for name in SCHEMA.names}
        for patch_raw, created_raw, blue_wins, participants_raw in cursor:
            patch = _prefix(patch_raw)
            try:
                created = int(created_raw)
                if created <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                exclusions["invalid_time"] += 1
                continue
            rows, reason = _participant_rows(participants_raw, blue_wins)
            if rows is None:
                exclusions[str(reason)] += 1
                continue
            for champion, win, augments in rows:
                columns["gidx"].append(gidx); columns["created_ms"].append(created)
                columns["patch"].append(patch); columns["champ"].append(champion)
                columns["win"].append(win); columns["augments"].append(augments)
            accepted[patch] += 1
            gidx += 1
            if len(columns["gidx"]) >= 100_000:
                writer.write_table(pa.Table.from_pydict(columns, schema=SCHEMA))
                columns = {name: [] for name in SCHEMA.names}
        if columns["gidx"]:
            writer.write_table(pa.Table.from_pydict(columns, schema=SCHEMA))
        writer.close(); writer = None
        connection.rollback(); connection.close(); connection = None
        if gidx < min_games:
            raise _fail("E_TOO_FEW_GAMES")
        os.replace(partial, output)
        meta = {
            "schema_version": 1, "source_kind": "sqlite_read_transaction",
            "source": db.name, "output": output.name, "queue": QUEUE_ID,
            "requested_patches": list(requested), "requested_patch_counts": requested_counts,
            "accepted_patch_counts": accepted, "accepted_patch_ranges": ranges,
            "exclusions": exclusions, "cutoff_created_ms": cutoff, "games": gidx,
            "participants": gidx * 10, "output_size": output.stat().st_size,
            "output_sha256": sha256(output),
        }
        _write_atomic_bytes(meta_path, _safe_meta(meta, EXPORT_META_KEYS))
        return meta
    except Round3ExportError:
        raise
    except Exception:
        raise _fail("E_EXPORT_FAILED") from None
    finally:
        if writer is not None:
            writer.close()
        if connection is not None:
            connection.close()
        partial.unlink(missing_ok=True)
        meta_partial.unlink(missing_ok=True)


def _iter_games(path: Path) -> Iterator[tuple[int, int, str, list[int], list[int], list[list[int]]]]:
    parquet = pq.ParquetFile(path)
    if parquet.schema_arrow != SCHEMA:
        raise _fail("E_SCHEMA")
    pending: dict[str, Any] | None = None
    last_gidx: int | None = None
    for batch in parquet.iter_batches(batch_size=100_000, columns=SCHEMA.names):
        data = batch.to_pydict()
        for row in zip(*(data[name] for name in SCHEMA.names), strict=True):
            old, created, patch, champion, win, augments = row
            if pending is None or int(old) != pending["gidx"]:
                if pending is not None:
                    if len(pending["champions"]) != 10:
                        raise _fail("E_TEN_ROWS")
                    yield (pending["gidx"], pending["created"], pending["patch"], pending["champions"], pending["wins"], pending["augments"])
                    last_gidx = pending["gidx"]
                if last_gidx is not None and int(old) <= last_gidx:
                    raise _fail("E_GIDX_ORDER")
                pending = {"gidx": int(old), "created": int(created), "patch": str(patch), "champions": [], "wins": [], "augments": []}
            if int(created) != pending["created"] or str(patch) != pending["patch"]:
                raise _fail("E_GAME_FIELDS")
            pending["champions"].append(int(champion)); pending["wins"].append(int(win))
            pending["augments"].append([int(value) for value in (augments or [])])
    if pending is not None:
        if len(pending["champions"]) != 10:
            raise _fail("E_TEN_ROWS")
        yield (pending["gidx"], pending["created"], pending["patch"], pending["champions"], pending["wins"], pending["augments"])


class _GameWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.partial = path.with_suffix(path.suffix + ".partial")
        self.partial.unlink(missing_ok=True)
        self.writer = pq.ParquetWriter(self.partial, SCHEMA, compression="zstd")
        self.games = 0
        self.columns: dict[str, list[Any]] = {name: [] for name in SCHEMA.names}

    def add(self, created: int, patch: str, champions: list[int], wins: list[int], augments: list[list[int]]) -> int:
        new_gidx = self.games
        for champion, win, values in zip(champions, wins, augments, strict=True):
            self.columns["gidx"].append(new_gidx); self.columns["created_ms"].append(created)
            self.columns["patch"].append(patch); self.columns["champ"].append(champion)
            self.columns["win"].append(win); self.columns["augments"].append(values)
        self.games += 1
        if len(self.columns["gidx"]) >= 100_000:
            self.flush()
        return new_gidx

    def flush(self) -> None:
        if self.columns["gidx"]:
            self.writer.write_table(pa.Table.from_pydict(self.columns, schema=SCHEMA))
            self.columns = {name: [] for name in SCHEMA.names}

    def close(self) -> None:
        self.flush(); self.writer.close(); os.replace(self.partial, self.path)

    def abort(self) -> None:
        try: self.writer.close()
        finally: self.partial.unlink(missing_ok=True)


def materialize_split(
    ended: Path,
    current: Path,
    combined_output: Path,
    dev_output: Path,
    frozen_output: Path,
    *,
    expected_current_sha256: str = CURRENT_PATCH_SHA256,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Write the full four-patch corpus plus physically isolated dev/test."""
    try:
        current_sha256 = sha256(current)
    except Exception:
        raise _fail("E_SOURCE_READ") from None
    if current_sha256 != expected_current_sha256:
        raise _fail("E_PINNED_SOURCE_SHA")
    outputs = (combined_output, dev_output, frozen_output)
    if len({path.resolve() for path in outputs}) != len(outputs):
        raise _fail("E_OUTPUT_COLLISION")
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
    combined = _GameWriter(combined_output)
    dev = _GameWriter(dev_output)
    frozen = _GameWriter(frozen_output)
    validation_materialized: tuple[int, int] | None = None
    saw_validation = saw_test = False
    try:
        for _old, created, patch, champions, wins, augments in _iter_games(ended):
            if patch not in ENDED_PATCHES:
                raise _fail("E_PATCH_SCOPE")
            combined.add(created, patch, champions, wins, augments)
            dev.add(created, patch, champions, wins, augments)
        for old, created, patch, champions, wins, augments in _iter_games(current):
            if patch not in ("16.14", "16.15"):
                raise _fail("E_PATCH_SCOPE")
            key = (created, old)
            if patch == "16.15" and key == VALIDATION_START:
                saw_validation = True
                validation_materialized = (created, dev.games)
            if patch == "16.15" and key == TEST_START:
                saw_test = True
            combined.add(created, patch, champions, wins, augments)
            if patch == "16.15" and key >= TEST_START:
                frozen.add(created, patch, champions, wins, augments)
            else:
                dev.add(created, patch, champions, wins, augments)
        if not saw_validation or not saw_test or validation_materialized is None:
            raise _fail("E_SPLIT_FINGERPRINT")
        combined.close(); dev.close(); frozen.close()
        common = {
            "schema_version": 1, "source_kind": "pinned_parquet_concatenation",
            "sources": [ended.name, current.name], "queue": QUEUE_ID,
            "validation_start_source": list(VALIDATION_START),
            "validation_start_materialized": list(validation_materialized),
            "test_start_source": list(TEST_START),
            "split_rule": "16.15_key_lt_test_start_is_dev",
            "source_sha256": {ended.name: sha256(ended), current.name: current_sha256},
        }
        dev_meta = {**common, "artifact_kind": "development", "output": dev_output.name,
                    "patches": list(("16.12", "16.13", "16.14", "16.15")),
                    "games": dev.games, "participants": dev.games * 10,
                    "output_size": dev_output.stat().st_size, "output_sha256": sha256(dev_output)}
        frozen_meta = {**common, "artifact_kind": "frozen_test", "output": frozen_output.name,
                       "patches": ["16.15"], "games": frozen.games,
                       "participants": frozen.games * 10,
                       "output_size": frozen_output.stat().st_size, "output_sha256": sha256(frozen_output)}
        combined_meta = {
            **common, "artifact_kind": "combined", "output": combined_output.name,
            "patches": list(("16.12", "16.13", "16.14", "16.15")),
            "games": combined.games, "participants": combined.games * 10,
            "output_size": combined_output.stat().st_size,
            "output_sha256": sha256(combined_output),
        }
        _write_atomic_bytes(combined_output.with_suffix(combined_output.suffix + ".meta.json"), _safe_meta(combined_meta, SPLIT_META_KEYS))
        _write_atomic_bytes(dev_output.with_suffix(dev_output.suffix + ".meta.json"), _safe_meta(dev_meta, SPLIT_META_KEYS))
        _write_atomic_bytes(frozen_output.with_suffix(frozen_output.suffix + ".meta.json"), _safe_meta(frozen_meta, SPLIT_META_KEYS))
        return combined_meta, dev_meta, frozen_meta
    except Round3ExportError:
        combined.abort(); dev.abort(); frozen.abort(); raise
    except Exception:
        combined.abort(); dev.abort(); frozen.abort(); raise _fail("E_MATERIALIZE_FAILED") from None


@click.group()
def main() -> None:
    """Round-3 privacy-minimal export and physical split."""


@main.command("export-ended")
@click.option("--db", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--out", required=True, type=click.Path(path_type=Path, dir_okay=False))
def export_ended_command(db: Path, out: Path) -> None:
    try:
        click.echo(json.dumps(export_ended_patches(db, out), ensure_ascii=False, sort_keys=True))
    except Round3ExportError as error:
        raise click.ClickException(str(error)) from None


@main.command("materialize")
@click.option("--ended-parquet", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--current-parquet", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--combined-out", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--dev-out", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--frozen-out", required=True, type=click.Path(path_type=Path, dir_okay=False))
def materialize_command(ended_parquet: Path, current_parquet: Path, combined_out: Path, dev_out: Path, frozen_out: Path) -> None:
    try:
        combined, dev, frozen = materialize_split(
            ended_parquet, current_parquet, combined_out, dev_out, frozen_out
        )
        click.echo(json.dumps(
            {"combined": combined, "dev": dev, "frozen": frozen},
            ensure_ascii=False, sort_keys=True,
        ))
    except Round3ExportError as error:
        raise click.ClickException(str(error)) from None


if __name__ == "__main__":
    main()
