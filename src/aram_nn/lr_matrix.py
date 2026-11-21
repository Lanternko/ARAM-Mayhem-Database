"""Memory-bounded design matrices for the pooled logistic models.

The pooled refresh used to hold every game's features as dense float32 plus a
Python profile object per team.  That grows linearly with the pool and passed
the machine's headroom at ~4M games (18 straight aborts, 2026-11-12..19).  The
features are mostly zeros (10 champions of ~170, one-hot interaction bins), so
they are built a slice of games at a time and kept as CSR.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np
import polars as pl
from scipy import sparse

DEFAULT_CHUNK_ROWS = 50_000


def champion_vocab(df: pl.DataFrame) -> dict[int, int]:
    """Champion id -> column, ids ascending (the mapping of
    ``train_ability_nn.build_vocab``) without a Python list per game."""
    # Distinct ids per side first: concatenating both sides' exploded lists
    # made a second copy of every champion slot in the pool.
    ids = pl.concat([df[side].explode().unique()
                     for side in ("blue_champions", "red_champions")])
    return {int(cid): i for i, cid in enumerate(ids.drop_nulls().unique().sort().to_list())}


@dataclass
class TeamRows:
    """The part of ``train_ability_nn.TeamDataset`` the feature builders read:
    each side's champion columns, one list per game."""
    blue: list[list[int]]
    red: list[list[int]]

    def __len__(self) -> int:
        return len(self.blue)


def patch_rows(patch: pl.Series, prefix: str) -> np.ndarray:
    """Boolean mask of games whose ``major.minor`` patch is *prefix*.

    Decided once per distinct patch string, so no per-game prefix column is
    built.  Games without a patch match nothing.
    """
    names = [p for p in patch.drop_nulls().unique().to_list()
             if ".".join(p.split(".")[:2]) == prefix]
    return patch.is_in(names).fill_null(False).to_numpy()


def time_split(created: np.ndarray, is_current: np.ndarray, is_earlier: np.ndarray, *,
               holdout: int, val_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Row indices ``(train, val, test)`` for the pooled fits, each oldest first.

    *test* is the newest *holdout* current-patch games, *val* the newest
    *val_size* earlier-patch games, *train* every other game of either kind.
    The rows themselves are never moved: sorting and filtering whole frames
    per split copied every champion list several times over.
    """
    oldest_first = np.argsort(created, kind="stable")
    current = oldest_first[is_current[oldest_first]]
    earlier = oldest_first[is_earlier[oldest_first]]
    n_early_current = max(len(current) - holdout, 0)
    n_pool = max(len(earlier) - val_size, 0)
    in_train = np.zeros(len(created), dtype=bool)
    in_train[current[:n_early_current]] = True
    in_train[earlier[:n_pool]] = True
    return oldest_first[in_train[oldest_first]], earlier[n_pool:], current[n_early_current:]


def csr_from_dense_chunks(chunks: Iterable[np.ndarray]) -> sparse.csr_matrix:
    """Stack dense 2-D float32 chunks into one float64 CSR without a dense whole.

    Row order is chunk order; exact zeros are dropped, every other value is
    the float32 value widened exactly.  The result is float64 because
    scikit-learn fits sparse input in float64 and would otherwise copy the
    whole matrix for the length of every fit (~2GB at 4.4M games).
    """
    data, indices, counts = [], [], []
    n_cols = None
    for chunk in chunks:
        part = sparse.csr_matrix(np.asarray(chunk, dtype=np.float32))
        if n_cols is None:
            n_cols = part.shape[1]
        elif part.shape[1] != n_cols:
            raise ValueError(f"chunk has {part.shape[1]} columns, expected {n_cols}")
        data.append(part.data)
        indices.append(part.indices.astype(np.int32, copy=False))
        counts.append(np.diff(part.indptr))
    if n_cols is None:
        raise ValueError("no chunks")
    indptr = np.concatenate([[0], np.cumsum(np.concatenate(counts), dtype=np.int64)])
    # One list at a time, each chunk dropped as soon as it is copied: the
    # transient peak stays near the finished matrix plus the float32 values.
    all_indices = np.concatenate(indices)
    indices.clear()
    all_data = np.empty(int(indptr[-1]), dtype=np.float64)
    stop = len(all_data)
    while data:
        part = data.pop()
        all_data[stop - len(part):stop] = part
        stop -= len(part)
    return sparse.csr_matrix((all_data, all_indices, indptr),
                             shape=(len(indptr) - 1, n_cols))


def chunked_csr(n_rows: int, build: Callable[[int, int], np.ndarray], *,
                chunk_rows: int = DEFAULT_CHUNK_ROWS) -> sparse.csr_matrix:
    """CSR of ``build(start, stop)`` over consecutive row ranges of *n_rows*."""
    if chunk_rows <= 0:
        raise ValueError("chunk_rows must be positive")
    return csr_from_dense_chunks(
        build(start, min(start + chunk_rows, n_rows))
        for start in range(0, n_rows, chunk_rows))


def masked_weights(mask: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Sample weights that restrict a fit to *mask* rows of a larger matrix.

    A zero-weight row adds nothing to the weighted log-loss, so fitting the
    whole matrix with these weights minimises the same objective as fitting the
    selected rows alone -- without copying them out.  *weights* (one per
    selected row, in row order) default to 1.
    """
    out = np.zeros(len(mask), dtype=np.float64)
    out[mask] = 1.0 if weights is None else weights
    return out


class RowMask:
    """``row in mask`` over a boolean array: a set of millions of ints costs
    hundreds of MB for the whole parquet pass, this costs one byte per row."""

    def __init__(self, rows: np.ndarray):
        rows = np.asarray(rows, dtype=np.int64)
        self._mask = np.zeros(int(rows.max()) + 1 if len(rows) else 0, dtype=bool)
        self._mask[rows] = True

    def __contains__(self, row: int) -> bool:
        return 0 <= row < len(self._mask) and bool(self._mask[row])
