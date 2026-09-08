"""Bounded participant reads; row offsets refer to the original parquet file."""
from pathlib import Path
from collections.abc import Iterator, Sequence

import polars as pl
import pyarrow.parquet as pq

TEAM_COLUMNS = ["patch", "duration_sec", "game_creation_ms", "blue_champions",
                "red_champions", "blue_wins"]
SOURCE_ROW = "_source_row"


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
        for batch in parquet.iter_batches(batch_size=batch_size, columns=list(columns),
                                          use_threads=False):
            frame = pl.from_arrow(batch)
            if selected_rows is not None:
                frame = frame.filter(pl.Series([
                    i in selected_rows for i in range(offset, offset + batch.num_rows)
                ]))
            offset += batch.num_rows
            yield from frame.iter_rows(buffer_size=batch_size)
