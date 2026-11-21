"""Bounded participant reads; row offsets refer to the original parquet file."""
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow.compute as pc
import pyarrow.parquet as pq

TEAM_COLUMNS = ["patch", "duration_sec", "game_creation_ms", "blue_champions",
                "red_champions", "blue_wins"]
SOURCE_ROW = "_source_row"


def read_team_frame(path: Path) -> pl.DataFrame:
    """Load team columns with original parquet offsets.

    Polars 1.40 drops ``row_index_name`` when ``columns=`` is set, so the
    index is attached after the projection. Offsets still match
    ``iter_parquet_rows`` because both walk the file in order.
    """
    return pl.read_parquet(path, columns=TEAM_COLUMNS).with_row_index(SOURCE_ROW)


@dataclass
class TeamArrays:
    """Team columns of a pooled parquet as arrays, one entry per kept game in
    file order.  ``blue``/``red`` are ``(games, 5)`` champion ids."""
    source_row: np.ndarray
    patch: pl.Series
    created: np.ndarray
    blue: np.ndarray
    red: np.ndarray
    blue_wins: np.ndarray


def read_team_arrays(path: Path, *, min_duration_sec: int = 0) -> TeamArrays:
    """Team columns of games lasting at least *min_duration_sec*.

    Read a row group at a time straight into numpy: the champion lists are 20
    bytes a team here, and nothing is left behind by the reader.  Loading them
    as a polars frame kept ~1.2GB committed beyond the data at 4.4M games.
    """
    rows, created, blue, red, wins, codes = [], [], [], [], [], []
    names: dict[str, int] = {}
    offset = 0
    with pq.ParquetFile(path) as parquet:
        for group in range(parquet.metadata.num_row_groups):
            table = parquet.read_row_group(group, columns=TEAM_COLUMNS, use_threads=False)
            keep = np.flatnonzero(
                pc.fill_null(table.column("duration_sec"), 0).to_numpy() >= min_duration_sec)
            rows.append(offset + keep)
            created.append(table.column("game_creation_ms").to_numpy()[keep])
            wins.append(table.column("blue_wins").to_numpy()[keep])
            for side, out in (("blue_champions", blue), ("red_champions", red)):
                teams = table.column(side).combine_chunks()
                ids = teams.flatten()
                if ids.null_count or len(ids) != 5 * len(teams) or not pc.all(
                        pc.equal(pc.list_value_length(teams), 5)).as_py():
                    raise ValueError(f"{path}: {side} must hold 5 champions in every game")
                out.append(ids.to_numpy().reshape(-1, 5)[keep].astype(np.int32))
            patch = table.column("patch").combine_chunks().dictionary_encode()
            local = np.array([names.setdefault(name, len(names))
                              for name in patch.dictionary.to_pylist()], dtype=np.int32)
            index = pc.fill_null(patch.indices, -1).to_numpy()
            codes.append(np.where(index >= 0, local[index], -1)[keep] if len(local)
                         else np.full(len(keep), -1, dtype=np.int32))
            offset += table.num_rows
    if not rows:
        raise ValueError(f"{path}: no row groups")
    code = np.concatenate(codes)
    return TeamArrays(
        source_row=np.concatenate(rows),
        patch=pl.Series("patch", [*names, None], dtype=pl.String)[
            np.where(code < 0, len(names), code)],
        created=np.concatenate(created),
        blue=np.concatenate(blue),
        red=np.concatenate(red),
        blue_wins=np.concatenate(wins),
    )


def _batches(parquet: pq.ParquetFile, batch_size: int, columns: list[str]):
    """Batches in file order, one reader per row group.

    A single ``iter_batches`` over the whole file keeps every row group it has
    decoded allocated until the reader closes (pyarrow 2026-11: the arrow pool
    grew by the full size of ``participants_json``, ~7GB over a 4.3M-game
    pool, while each batch was dropped immediately).  A reader scoped to one
    row group returns its memory when that row group is done.
    """
    for row_group in range(parquet.metadata.num_row_groups):
        yield from parquet.iter_batches(batch_size=batch_size, row_groups=[row_group],
                                        columns=columns, use_threads=False)


def iter_parquet_rows(path: Path, columns: Sequence[str], *,
                      selected_rows=None, batch_size: int = 256) -> Iterator[tuple]:
    """Decode at most one batch, selecting exact source offsets before Python conversion.

    Selection uses offsets rather than timestamps (ties may cross split boundaries).
    Arrow batching also avoids loading an entire parquet row group of JSON.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    offset = 0
    with pq.ParquetFile(path) as parquet:
        for batch in _batches(parquet, batch_size, list(columns)):
            frame = pl.from_arrow(batch)
            if selected_rows is not None:
                frame = frame.filter(pl.Series([
                    i in selected_rows for i in range(offset, offset + batch.num_rows)
                ]))
            offset += batch.num_rows
            yield from frame.iter_rows(buffer_size=batch_size)
