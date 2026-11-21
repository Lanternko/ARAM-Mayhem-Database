"""read_team_arrays: the numpy view of a pooled parquet's team columns."""
from __future__ import annotations

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aram_nn.lr_matrix import patch_rows
from aram_nn.parquet_batches import SOURCE_ROW, read_team_arrays, read_team_frame


def _write(path, n: int = 230, *, seed: int = 5, row_group_size: int = 64, bad_row: int | None = None):
    rng = np.random.default_rng(seed)
    blue = [rng.choice(170, 5, replace=False).tolist() for _ in range(n)]
    red = [rng.choice(170, 5, replace=False).tolist() for _ in range(n)]
    if bad_row is not None:
        red[bad_row] = red[bad_row][:4]
    patches = [None if i % 37 == 0 else f"16.{21 + i % 3}.{700 + i % 2}.1" for i in range(n)]
    table = pa.table({
        "patch": pa.array(patches, type=pa.large_string()),
        "duration_sec": pa.array(rng.integers(100, 1500, n), type=pa.int64()),
        "game_creation_ms": pa.array(rng.integers(10**12, 2 * 10**12, n), type=pa.int64()),
        "blue_champions": pa.array(blue, type=pa.large_list(pa.int64())),
        "red_champions": pa.array(red, type=pa.large_list(pa.int64())),
        "blue_wins": pa.array(rng.random(n) < 0.5),
        "participants_json": pa.array(["{}"] * n, type=pa.large_string()),
    })
    pq.write_table(table, path, row_group_size=row_group_size)


@pytest.mark.parametrize("min_duration", [0, 300])
def test_arrays_match_the_polars_frame(tmp_path, min_duration):
    path = tmp_path / "pool.parquet"
    _write(path)
    frame = read_team_frame(path).filter(pl.col("duration_sec") >= min_duration)
    teams = read_team_arrays(path, min_duration_sec=min_duration)

    assert teams.source_row.tolist() == frame[SOURCE_ROW].to_list()
    assert teams.created.tolist() == frame["game_creation_ms"].to_list()
    assert teams.blue_wins.dtype == bool
    assert teams.blue_wins.tolist() == frame["blue_wins"].to_list()
    assert teams.blue.tolist() == frame["blue_champions"].to_list()
    assert teams.red.tolist() == frame["red_champions"].to_list()
    assert teams.patch.to_list() == frame["patch"].to_list()
    for prefix in ("16.21", "16.23", "16.2"):
        assert np.array_equal(patch_rows(teams.patch, prefix), patch_rows(frame["patch"], prefix))


def test_a_team_without_five_champions_is_an_error(tmp_path):
    path = tmp_path / "pool.parquet"
    _write(path, bad_row=100)
    with pytest.raises(ValueError, match="5 champions"):
        read_team_arrays(path)
