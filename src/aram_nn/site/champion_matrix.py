"""Sparse equivalent of the shipped champion-identity design matrix."""
import numpy as np
from scipy.sparse import csr_matrix


def champion_matrix(dataset, n_champs):
    n = len(dataset)
    if not n:
        return csr_matrix((0, n_champs), dtype=np.float64)
    # Teams are structurally 5v5. Sparse subtraction alone would cancel mirror
    # picks; the old dense builder assigns red last, so preserve its -1 there.
    indptr = np.arange(0, (n + 1) * 5, 5, dtype=np.int32)
    blue = csr_matrix((np.ones(n*5), np.asarray(dataset.blue, dtype=np.int32).ravel(), indptr),
                      shape=(n, n_champs))
    red = csr_matrix((np.ones(n*5), np.asarray(dataset.red, dtype=np.int32).ravel(), indptr.copy()),
                     shape=(n, n_champs))
    # Assignment is idempotent even if an imported record repeats a champion.
    for team in (blue, red):
        team.sum_duplicates()
        team.data.fill(1)
    return blue - red - blue.multiply(red)
