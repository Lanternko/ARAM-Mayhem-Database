"""The pooled models' memory-bounded matrices must be the same matrices."""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from sklearn.linear_model import LogisticRegression

from aram_nn.lr_matrix import (
    RowMask, champion_vocab, chunked_csr, csr_from_dense_chunks, masked_weights, patch_rows,
    time_split,
)


def _dense(rows: int = 137, cols: int = 9, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(rows, cols)).astype(np.float32)
    x[rng.random(size=x.shape) < 0.7] = 0.0
    x[5] = 0.0  # an all-zero row must survive as a row
    return x


@pytest.mark.parametrize("chunk_rows", [1, 10, 137, 1000])
def test_chunked_csr_equals_the_dense_matrix(chunk_rows):
    x = _dense()
    got = chunked_csr(len(x), lambda a, b: x[a:b], chunk_rows=chunk_rows)
    assert got.shape == x.shape and got.dtype == np.float64
    np.testing.assert_array_equal(got.toarray(), x)


def test_chunks_must_agree_on_width():
    with pytest.raises(ValueError, match="columns"):
        csr_from_dense_chunks([np.zeros((2, 3), np.float32), np.zeros((2, 4), np.float32)])
    with pytest.raises(ValueError, match="no chunks"):
        csr_from_dense_chunks([])


def test_zero_weight_rows_give_the_same_model_as_leaving_them_out():
    x = _dense(rows=4000, cols=12, seed=1)
    rng = np.random.default_rng(2)
    y = (x @ rng.normal(size=12) + rng.normal(size=len(x)) > 0).astype(np.float64)
    mask = np.arange(len(x)) < 3000
    w = rng.uniform(0.2, 3.0, size=3000)
    w *= len(w) / w.sum()
    sparse_x = chunked_csr(len(x), lambda a, b: x[a:b], chunk_rows=700)
    x = x.astype(np.float64)  # float32 fits stop ~1e-4 apart from rounding alone

    for weights in (None, w):
        subset = LogisticRegression(C=0.3, max_iter=5000, tol=1e-10).fit(
            x[mask], y[mask], sample_weight=weights)
        whole = LogisticRegression(C=0.3, max_iter=5000, tol=1e-10).fit(
            sparse_x, y, sample_weight=masked_weights(mask, weights))
        np.testing.assert_allclose(whole.coef_, subset.coef_, atol=1e-5)
        np.testing.assert_allclose(whole.intercept_, subset.intercept_, atol=1e-5)


def test_row_mask_is_a_membership_test():
    mask = RowMask(np.array([0, 7, 3]))
    assert [i in mask for i in (0, 1, 3, 7, 8, 10_000, -1)] == [
        True, False, True, True, False, False, False]
    assert 0 not in RowMask(np.array([], dtype=np.int64))


def test_champion_vocab_matches_the_list_based_mapping():
    df = pl.DataFrame({"blue_champions": [[5, 1, 99], [3, 1, 5]],
                       "red_champions": [[2, 7, 1], [99, 64, 2]]})
    ids = {c for col in ("blue_champions", "red_champions") for row in df[col].to_list() for c in row}
    assert champion_vocab(df) == {cid: i for i, cid in enumerate(sorted(ids))}


def _games(n: int = 400, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    patches = ["16.21.1", "16.21.2", "16.22.5", "16.23.1", "16.23", "16.230.1", None]
    return pl.DataFrame({
        "patch": [patches[i] for i in rng.integers(0, len(patches), size=n)],
        # distinct timestamps, shuffled: file order is not time order
        "game_creation_ms": rng.permutation(n) * 1000 + 5,
    }).with_row_index("row")


def test_patch_rows_selects_the_major_minor_prefix():
    df = _games()
    prefix = df["patch"].str.split(".").list.slice(0, 2).list.join(".")
    for pp in ("16.21", "16.23", "16.230", "17.1"):
        assert patch_rows(df["patch"], pp).tolist() == (prefix == pp).fill_null(False).to_list()


@pytest.mark.parametrize(("holdout", "val_size"), [(20, 30), (0, 0), (5, 10_000)])
def test_time_split_matches_sorting_and_slicing_the_frame(holdout, val_size):
    df = _games()
    is_cur = patch_rows(df["patch"], "16.23")
    is_earlier = df["patch"].is_not_null().to_numpy() & ~is_cur
    train, val, test = time_split(df["game_creation_ms"].to_numpy(), is_cur, is_earlier,
                                  holdout=holdout, val_size=val_size)

    ordered = df.sort("game_creation_ms")
    cur = ordered.filter(pl.Series(is_cur)[ordered["row"]])
    earlier = ordered.filter(pl.Series(is_earlier)[ordered["row"]])
    n_pool = max(earlier.height - val_size, 0)
    want_train = pl.concat([earlier.head(n_pool), cur.head(cur.height - holdout)]).sort("game_creation_ms")
    assert train.tolist() == want_train["row"].to_list()
    assert val.tolist() == earlier.slice(n_pool)["row"].to_list()
    assert test.tolist() == cur.slice(cur.height - holdout)["row"].to_list()
