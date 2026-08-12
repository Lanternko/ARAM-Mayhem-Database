"""Stream a compact multi-patch participant parquet for augment ablations.

The source is the public pooled game parquet.  Only patch/time/champion/win and
ordered augment ids are retained; player identifiers and final inventories are
deliberately excluded.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import click
import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA = pa.schema(
    [
        ("gidx", pa.int64()),
        ("created_ms", pa.int64()),
        ("patch", pa.string()),
        ("champ", pa.int64()),
        ("win", pa.int8()),
        ("augments", pa.list_(pa.int64())),
    ]
)


def _prefix(value: object) -> str:
    parts = str(value or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(value or "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@click.command()
@click.option(
    "--db",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Read a transactionally consistent snapshot from games.db.",
)
@click.option(
    "--pooled-parquet",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Read a stable pooled parquet instead of games.db.",
)
@click.option("--patch", "patches", multiple=True, default=("16.14", "16.15"))
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("data/analysis/augment_ablation_participants_16.14_16.15.parquet"),
    show_default=True,
)
@click.option("--batch-games", type=int, default=2000, show_default=True)
def main(
    db: Path | None,
    pooled_parquet: Path | None,
    patches: tuple[str, ...],
    out: Path,
    batch_games: int,
) -> None:
    if (db is None) == (pooled_parquet is None):
        raise click.ClickException("pass exactly one of --db or --pooled-parquet")
    wanted = {str(value) for value in patches}
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    writer = pq.ParquetWriter(temporary, SCHEMA, compression="zstd")
    games = participants = gidx = 0
    patch_games: dict[str, int] = {}
    try:
        if db is not None:
            connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            connection.execute("BEGIN")
            clauses = " OR ".join("patch LIKE ?" for _ in wanted)
            cursor = connection.execute(
                "SELECT patch, created_ms, blue_wins, participants_json FROM games "
                f"WHERE queue_id=2400 AND participants_json IS NOT NULL AND ({clauses}) "
                # The trainer sorts unique games by (created_ms, gidx) before
                # splitting.  Sorting this 60 GB source query only creates a
                # multi-minute SQLite temp sort without changing that split.
                "",
                tuple(f"{patch}%" for patch in sorted(wanted)),
            )

            def batches():
                while rows := cursor.fetchmany(batch_games):
                    yield {
                        "patch": [row[0] for row in rows],
                        "game_creation_ms": [row[1] for row in rows],
                        "blue_wins": [row[2] for row in rows],
                        "participants_json": [row[3] for row in rows],
                    }
        else:
            source = pq.ParquetFile(pooled_parquet)

            def batches():
                for batch in source.iter_batches(
                    batch_size=batch_games,
                    columns=("patch", "game_creation_ms", "blue_wins", "participants_json"),
                ):
                    yield batch.to_pydict()

        for raw in batches():
            columns: dict[str, list] = {name: [] for name in SCHEMA.names}
            for patch_raw, created_ms, blue_wins, participants_raw in zip(
                raw["patch"],
                raw["game_creation_ms"],
                raw["blue_wins"],
                raw["participants_json"],
                strict=True,
            ):
                patch = _prefix(patch_raw)
                if patch not in wanted:
                    continue
                try:
                    roster = json.loads(participants_raw or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(roster, list) or len(roster) != 10:
                    continue
                valid_rows: list[tuple[int, int, list[int]]] = []
                for participant in roster:
                    try:
                        team_id = int(participant.get("teamId", 0))
                        champion_id = int(participant.get("championId", 0))
                        augments = [
                            int(value)
                            for value in (participant.get("augments") or [])
                            if int(value) > 0
                        ][:4]
                    except (AttributeError, TypeError, ValueError):
                        valid_rows = []
                        break
                    if team_id not in (100, 200) or champion_id <= 0 or not augments:
                        valid_rows = []
                        break
                    won = int((team_id == 100) == bool(blue_wins))
                    valid_rows.append((champion_id, won, augments))
                if len(valid_rows) != 10:
                    continue
                for champion_id, won, augments in valid_rows:
                    columns["gidx"].append(gidx)
                    columns["created_ms"].append(int(created_ms or 0))
                    columns["patch"].append(patch)
                    columns["champ"].append(champion_id)
                    columns["win"].append(won)
                    columns["augments"].append(augments)
                games += 1
                participants += 10
                patch_games[patch] = patch_games.get(patch, 0) + 1
                gidx += 1
            if columns["gidx"]:
                writer.write_table(pa.Table.from_pydict(columns, schema=SCHEMA))
            if games and games % 100_000 < batch_games:
                click.echo(f"[augment-ablation-export] games={games:,}", err=True)
    finally:
        writer.close()
        if db is not None:
            connection.close()
    if games < 20:
        temporary.unlink(missing_ok=True)
        raise click.ClickException(f"only {games} valid games exported")
    os.replace(temporary, out)
    source_path = db if db is not None else pooled_parquet
    assert source_path is not None
    source_stat = source_path.stat()
    metadata = {
        "source": str(source_path.resolve()),
        "source_kind": "sqlite_snapshot" if db is not None else "pooled_parquet",
        "source_sha256": None if db is not None else _sha256(source_path),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "output": str(out.resolve()),
        "output_sha256": _sha256(out),
        "patch_games": patch_games,
        "games": games,
        "participants": participants,
    }
    out.with_suffix(out.suffix + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    click.echo(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
