"""Compact leave-one-out score replay for exact quantiles and residual cells."""
from array import array

import numpy as np


def score_buffers(dataset, idx_to_cid, cap_pct, *, score, archetypes):
    # Preserve score evaluation and insertion order exactly. A double buffer uses
    # 8 bytes/value, instead of a list pointer plus a retained Python float.
    per_arch = {name: array('d') for name in archetypes}
    for blue, red in zip(dataset.blue, dataset.red):
        for team in (blue, red):
            cids = [idx_to_cid[i] for i in team]
            for cid in cids:
                others = [other for other in cids if other != cid]
                for name, value in score(others, cap_pct).items():
                    per_arch[name].append(value)
    return per_arch


def thresholds_from_scores(scores, quantile):
    return {name: float(np.quantile(values, quantile)) for name, values in scores.items() if values}


def accumulate_scores(dataset, residual, scores, thresholds, n_champs):
    """Replay membership scores; preserve each cell's original addition order."""
    n = len(residual)
    if not n:
        return {}, {}
    champions = np.empty((n, 10), dtype=np.int32)
    champions[:, :5] = dataset.blue
    champions[:, 5:] = dataset.red
    champions = champions.ravel()
    signed_residual = np.repeat(np.asarray(residual, dtype=np.float64), 10)
    signed_residual.reshape(n, 10)[:, 5:] *= -1
    sums, counts = {}, {}
    for name, buffer in scores.items():
        values = np.frombuffer(buffer, dtype=np.float64)
        selected = values >= thresholds[name]
        indices = champions[selected]
        total = np.zeros(n_champs, dtype=np.float64)
        # Unlike a weighted bincount/reduction, unbuffered add.at retains the
        # legacy per-participant floating-point addition order for every cell.
        np.add.at(total, indices, signed_residual[selected])
        support = np.bincount(indices, minlength=n_champs)
        for champion in np.flatnonzero(support):
            key = (int(champion), name)
            sums[key] = total[champion]
            counts[key] = int(support[champion])
    return sums, counts
