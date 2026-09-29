"""Logistic Regression baseline on one-hot champion presence.

Feature vector: length = n_champs
  +1 if champion is on blue team
  -1 if champion is on red team
   0 if absent

This is equivalent to: logit = Σ w_i for i∈blue − Σ w_i for i∈red
Linear model with no interaction terms — our lower bound for the NN.
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from aram_nn.data import ARAMDataset


def _to_matrix(dataset: ARAMDataset, n_champs: int) -> tuple[np.ndarray, np.ndarray]:
    X = np.zeros((len(dataset), n_champs), dtype=np.float32)
    y = np.empty(len(dataset), dtype=np.float32)
    for i in range(len(dataset)):
        blue, red, label = dataset[i]
        for c in blue.numpy():
            X[i, c] = 1.0
        for c in red.numpy():
            X[i, c] = -1.0
        y[i] = float(label)
    return X, y


def train_and_eval(
    train: ARAMDataset,
    val: ARAMDataset,
    test: ARAMDataset,
    n_champs: int,
    C: float | None = None,  # None = sweep on val
    C_grid: tuple[float, ...] = (0.01, 0.1, 0.5, 1.0, 5.0, 10.0),
) -> dict:
    X_tr, y_tr = _to_matrix(train, n_champs)
    X_va, y_va = _to_matrix(val,   n_champs)
    X_te, y_te = _to_matrix(test,  n_champs)

    if C is None:
        best_C, best_ll = C_grid[0], float("inf")
        for c in C_grid:
            clf_ = LogisticRegression(C=c, max_iter=1000, solver="lbfgs")
            clf_.fit(X_tr, y_tr)
            ll = log_loss(y_va, clf_.predict_proba(X_va)[:, 1])
            if ll < best_ll:
                best_ll, best_C = ll, c
        C = best_C

    clf = LogisticRegression(C=C, max_iter=1000, solver="lbfgs")
    clf.fit(X_tr, y_tr)

    def _metrics(X, y, split):
        probs = clf.predict_proba(X)[:, 1]
        ll = log_loss(y, probs)
        acc = ((probs >= 0.5) == y.astype(bool)).mean()
        return {f"{split}/log_loss": ll, f"{split}/acc": acc}

    return {
        **_metrics(X_tr, y_tr, "train"),
        **_metrics(X_va, y_va, "val"),
        **_metrics(X_te, y_te, "test"),
        "model": clf,
        "best_C": C,
    }
