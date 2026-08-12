"""Leakage-safe strength features and validation decisions for augment round 3.

All empirical tables are fitted from an explicit training mask.  Cross-fitted
training features keep an entire game in one fold and never pool patches.
Validation features are fitted from the 16.15 training partition only.
"""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from sklearn.metrics import roc_auc_score
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from aram_nn.models.augment_sequence import AugmentSequenceNN


CATEGORY_ORDER = ("ap", "ad", "tank", "support", "gold", "mechanic", "cd", "new", "crit", "amp")
PILOT_PATCHES: dict[str, tuple[str, ...]] = {
    "P0": ("16.14", "16.15"),
    "P1": ("16.14", "16.15"),
    "P2": ("16.13", "16.14", "16.15"),
    "P3": ("16.12", "16.13", "16.14", "16.15"),
}
UNCERTAINTY_ENABLED = {"P0": False, "P1": True, "P2": True, "P3": True}
COMPLEXITY_RANK = {"P1": 1, "P2": 2, "P3": 3}


def seed_everything(seed: int = 2400) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def logit(probability: np.ndarray) -> np.ndarray:
    value = np.clip(probability, 1e-6, 1.0 - 1e-6)
    return np.log(value / (1.0 - value)).astype(np.float32)


def probability(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(logits / max(temperature, 1e-3), -30.0, 30.0)))


@dataclass(frozen=True)
class StrengthTable:
    """Patch-local empirical champion/augment table."""

    global_rate: float
    champion_games: np.ndarray
    champion_rate: np.ndarray
    pair_games: np.ndarray
    pair_alpha: np.ndarray
    pair_beta: np.ndarray


def fit_strength_table(
    champions: np.ndarray,
    labels: np.ndarray,
    augments: np.ndarray,
    mask: np.ndarray,
    *,
    n_champions: int | None = None,
    n_augments: int | None = None,
) -> StrengthTable:
    """Fit one table from only the rows selected by ``mask``."""
    champions = np.asarray(champions, dtype=np.int64)
    labels = np.asarray(labels, dtype=np.float64)
    augments = np.asarray(augments, dtype=np.int64)
    mask = np.asarray(mask, dtype=bool)
    if augments.ndim != 2 or augments.shape[1] != 4 or len(mask) != len(labels):
        raise ValueError("E_STRENGTH_SHAPE")
    if not mask.any():
        raise ValueError("E_STRENGTH_EMPTY")
    n_champions = int(n_champions or (int(champions.max()) + 1))
    n_augments = int(n_augments or (int(augments.max()) + 1))
    c = champions[mask]
    y = labels[mask]
    a = augments[mask]
    # Match the canonical round-2 independent-odds control exactly.  The
    # global rate is empirical; champion and champion/augment cells are each
    # shrunk by 100 observations at their respective parent rate.
    global_rate = float(y.mean())

    champion_games = np.bincount(c, minlength=n_champions).astype(np.float64)
    champion_wins = np.bincount(c, weights=y, minlength=n_champions).astype(np.float64)
    champion_rate = (champion_wins + 100.0 * global_rate) / (champion_games + 100.0)

    pair_games = np.zeros((n_champions, n_augments), dtype=np.float64)
    pair_wins = np.zeros_like(pair_games)
    for slot in range(4):
        current = a[:, slot]
        valid = current > 0
        np.add.at(pair_games, (c[valid], current[valid]), 1.0)
        np.add.at(pair_wins, (c[valid], current[valid]), y[valid])
    prior_alpha = champion_rate[:, None] * 100.0
    prior_beta = (1.0 - champion_rate[:, None]) * 100.0
    pair_alpha = pair_wins + prior_alpha
    pair_beta = pair_games - pair_wins + prior_beta
    return StrengthTable(
        global_rate=global_rate,
        champion_games=champion_games,
        champion_rate=champion_rate.astype(np.float32),
        pair_games=pair_games,
        pair_alpha=pair_alpha,
        pair_beta=pair_beta,
    )


def apply_strength_table(
    table: StrengthTable,
    champions: np.ndarray,
    augments: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return independent-odds logits and three causal uncertainty features.

    Slot ``t`` uses champion, current augment, and augment effects through ``t``.
    No feature indexes a later slot.
    """
    champions = np.asarray(champions, dtype=np.int64)
    augments = np.asarray(augments, dtype=np.int64)
    if augments.ndim != 2 or augments.shape[1] != 4:
        raise ValueError("E_STRENGTH_SHAPE")
    champion_logit = logit(table.champion_rate[champions])
    games = table.pair_games[champions[:, None], augments]
    alpha = table.pair_alpha[champions[:, None], augments]
    beta = table.pair_beta[champions[:, None], augments]
    total = alpha + beta
    pair_rate = alpha / total
    pair_effect = logit(pair_rate) - champion_logit[:, None]
    valid = augments > 0
    pair_effect = np.where(valid, pair_effect, 0.0)
    base = champion_logit[:, None] + np.cumsum(pair_effect, axis=1)

    variance = (alpha * beta) / np.maximum(total * total * (total + 1.0), 1e-12)
    uncertainty = np.stack(
        (np.log1p(games), variance, (games == 0).astype(np.float64)), axis=-1
    ).astype(np.float32)
    base = np.where(valid, base, 0.0).astype(np.float32)
    uncertainty *= valid[:, :, None]
    return base, uncertainty


def cross_fit_strength(
    champions: np.ndarray,
    labels: np.ndarray,
    augments: np.ndarray,
    game_ids: np.ndarray,
    patches: np.ndarray,
    train_mask: np.ndarray,
    *,
    folds: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Build game-level OOF features independently inside every patch."""
    n = len(labels)
    base = np.full((n, 4), np.nan, dtype=np.float32)
    uncertainty = np.full((n, 4, 3), np.nan, dtype=np.float32)
    n_champions = int(np.max(champions)) + 1
    n_augments = int(np.max(augments)) + 1
    for patch in sorted(str(value) for value in np.unique(patches[train_mask])):
        patch_mask = train_mask & (patches == patch)
        games = np.unique(game_ids[patch_mask])
        if len(games) < folds:
            raise ValueError("E_OOF_TOO_FEW_GAMES")
        games.sort()
        fold_by_game = {int(game): idx % folds for idx, game in enumerate(games)}
        fold_ids = np.asarray([fold_by_game.get(int(game), -1) for game in game_ids])
        for fold in range(folds):
            holdout = patch_mask & (fold_ids == fold)
            fit = patch_mask & (fold_ids != fold)
            if not holdout.any() or not fit.any():
                raise ValueError("E_OOF_EMPTY_FOLD")
            table = fit_strength_table(
                champions, labels, augments, fit,
                n_champions=n_champions, n_augments=n_augments,
            )
            base[holdout], uncertainty[holdout] = apply_strength_table(
                table, champions[holdout], augments[holdout]
            )
    if np.isnan(base[train_mask]).any() or np.isnan(uncertainty[train_mask]).any():
        raise ValueError("E_OOF_INCOMPLETE")
    return base, uncertainty


class AugmentResidualNN(nn.Module):
    """Existing sequence defaults plus an optional uncertainty residual."""

    def __init__(
        self,
        *,
        n_champions: int,
        n_augments: int,
        category_matrix: torch.Tensor,
        use_uncertainty: bool,
    ) -> None:
        super().__init__()
        self.sequence = AugmentSequenceNN(
            n_champions=n_champions,
            n_augments=n_augments,
            category_matrix=category_matrix,
            use_history=True,
        )
        final = self.sequence.mlp[-1]
        nn.init.zeros_(final.weight)
        nn.init.zeros_(final.bias)
        self.use_uncertainty = bool(use_uncertainty)
        if self.use_uncertainty:
            self.uncertainty = nn.Sequential(
                nn.Linear(3, 16), nn.GELU(), nn.Linear(16, 1)
            )
            nn.init.zeros_(self.uncertainty[-1].weight)
            nn.init.zeros_(self.uncertainty[-1].bias)

    def forward(
        self,
        champions: torch.Tensor,
        augments: torch.Tensor,
        uncertainty: torch.Tensor,
    ) -> torch.Tensor:
        residual = self.sequence(champions, augments)
        if self.use_uncertainty:
            residual = residual + self.uncertainty(uncertainty).squeeze(-1)
        return residual


@torch.no_grad()
def predict_logits(
    model: AugmentResidualNN,
    champions: np.ndarray,
    augments: np.ndarray,
    base_logits: np.ndarray,
    uncertainty: np.ndarray,
    *,
    batch_size: int = 32768,
) -> np.ndarray:
    model.eval()
    output: list[np.ndarray] = []
    for start in range(0, len(champions), batch_size):
        stop = start + batch_size
        output.append((
            torch.from_numpy(base_logits[start:stop])
            + model(
                torch.from_numpy(champions[start:stop]),
                torch.from_numpy(augments[start:stop]),
                torch.from_numpy(uncertainty[start:stop]),
            )
        ).numpy())
    return np.concatenate(output) if output else np.empty((0, 4), dtype=np.float32)


def metric_values(
    labels: np.ndarray,
    probabilities: np.ndarray,
    valid: np.ndarray,
    *,
    include_slots: bool = True,
) -> dict[str, Any]:
    y = np.broadcast_to(labels[:, None], probabilities.shape)[valid].astype(np.float64)
    p = np.clip(probabilities[valid].astype(np.float64), 1e-6, 1.0 - 1e-6)
    if not len(y):
        raise ValueError("E_METRICS_EMPTY")
    accuracy = float(np.mean((p >= 0.5) == y))
    auc = float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else 0.5
    loss = -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
    bins = np.minimum((p * 10).astype(np.int64), 9)
    ece = 0.0
    for bin_id in range(10):
        selected = bins == bin_id
        if selected.any():
            ece += float(selected.mean()) * abs(float(p[selected].mean()) - float(y[selected].mean()))
    result: dict[str, Any] = {
        "observations": int(len(y)),
        "accuracy": accuracy,
        "auc": auc,
        "logloss": float(loss.mean()),
        "brier": float(np.mean((p - y) ** 2)),
        "ece": ece,
    }
    if include_slots:
        result["per_slot"] = {
            str(slot + 1): metric_values(
                labels,
                probabilities,
                valid & (np.arange(4)[None, :] == slot),
                include_slots=False,
            )
            for slot in range(4)
            if (valid & (np.arange(4)[None, :] == slot)).any()
        }
    return result


def _temperature(logits: np.ndarray, labels: np.ndarray, valid: np.ndarray) -> float:
    y = np.broadcast_to(labels[:, None], logits.shape)[valid].astype(np.float64)
    z = logits[valid].astype(np.float64)
    candidates = np.exp(np.linspace(-2.0, 2.0, 161))
    losses = []
    for value in candidates:
        p = probability(z, float(value))
        losses.append(float(np.mean(-(y * np.log(p) + (1 - y) * np.log(1 - p)))))
    return float(candidates[int(np.argmin(losses))])


def fit_residual_model(
    model: AugmentResidualNN,
    *,
    champions: np.ndarray,
    augments: np.ndarray,
    labels: np.ndarray,
    base_logits: np.ndarray,
    uncertainty: np.ndarray,
    sample_weights: np.ndarray,
    validation: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    epochs: int = 8,
    batch_size: int = 32768,
) -> tuple[dict[str, torch.Tensor], list[dict[str, float]], float]:
    """Fit with fixed round-3 optimizer settings and select on validation only."""
    dataset = TensorDataset(
        torch.from_numpy(champions), torch.from_numpy(augments),
        torch.from_numpy(labels), torch.from_numpy(base_logits),
        torch.from_numpy(uncertainty), torch.from_numpy(sample_weights.astype(np.float32)),
    )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=3e-4)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    vc, va, vy, vb, vu = validation
    valid_validation = va > 0
    best_loss = float("inf")
    best_state: dict[str, torch.Tensor] | None = None
    history: list[dict[str, float]] = []
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = total_weight = 0.0
        for bc, ba, by, bb, bu, bw in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = bb + model(bc, ba, bu)
            valid = ba > 0
            weights = bw[:, None].expand_as(logits) * valid
            losses = criterion(logits, by[:, None].expand_as(logits))
            loss = (losses * weights).sum() / weights.sum()
            loss.backward()
            optimizer.step()
            total_loss += float((losses * weights).sum().detach())
            total_weight += float(weights.sum())
        logits = predict_logits(model, vc, va, vb, vu, batch_size=batch_size)
        metrics = metric_values(vy, probability(logits), valid_validation)
        history.append({"epoch": epoch, "train_logloss": total_loss / total_weight, "validation_logloss": metrics["logloss"]})
        if metrics["logloss"] < best_loss:
            best_loss = float(metrics["logloss"])
            best_state = copy.deepcopy(model.state_dict())
    if best_state is None:
        raise ValueError("E_TRAINING_EMPTY")
    model.load_state_dict(best_state)
    best_logits = predict_logits(model, vc, va, vb, vu, batch_size=batch_size)
    temperature = _temperature(best_logits, vy, valid_validation)
    return best_state, history, temperature


def clustered_paired_delta(
    game_ids: np.ndarray,
    labels: np.ndarray,
    candidate: np.ndarray,
    baseline: np.ndarray,
    valid: np.ndarray,
) -> float:
    target = np.broadcast_to(labels[:, None], candidate.shape)
    candidate = np.clip(candidate, 1e-6, 1.0 - 1e-6)
    baseline = np.clip(baseline, 1e-6, 1.0 - 1e-6)
    delta = (-(target * np.log(candidate) + (1-target) * np.log(1-candidate))
             + target * np.log(baseline) + (1-target) * np.log(1-baseline))[valid]
    games = np.broadcast_to(game_ids[:, None], valid.shape)[valid]
    unique, inverse = np.unique(games, return_inverse=True)
    return float(np.mean(np.bincount(inverse, weights=delta) / np.bincount(inverse))) if len(unique) else float("nan")


def validation_verdict(results: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    """Apply the fixed full-precision eligibility and deterministic tie-break."""
    if set(results) != set(PILOT_PATCHES):
        raise ValueError("E_VERDICT_PILOTS")
    baseline = results["P0"]
    eligible: list[str] = []
    evaluations: dict[str, dict[str, Any]] = {}
    for pilot in ("P1", "P2", "P3"):
        row = results[pilot]
        checks = {
            "paired_logloss": float(row["paired_delta"]) <= -0.00002,
            "accuracy": float(row["accuracy"]) - float(baseline["accuracy"]) >= -0.0002,
            "auc": float(row["auc"]) - float(baseline["auc"]) >= -0.0001,
            "brier": float(row["brier"]) - float(baseline["brier"]) <= 0.00002,
        }
        evaluations[pilot] = {"eligible": all(checks.values()), "checks": checks}
        if all(checks.values()):
            eligible.append(pilot)
    if not eligible:
        return {"verdict": "STOP_NO_TEST", "selected": None, "eligible": [], "evaluations": evaluations}
    selected = min(
        eligible,
        key=lambda name: (
            float(results[name]["logloss"]), float(results[name]["brier"]),
            -float(results[name]["auc"]), -float(results[name]["accuracy"]),
            COMPLEXITY_RANK[name], name,
        ),
    )
    return {"verdict": f"SELECT_{selected}", "selected": selected, "eligible": eligible, "evaluations": evaluations}


def checkpoint_payload(
    state: Mapping[str, torch.Tensor],
    config: Mapping[str, Any],
    patch_labels: Sequence[str],
) -> dict[str, Any]:
    """Construct the exact checkpoint top-level contract."""
    return {
        "tensor_state": dict(state),
        "config": dict(config),
        "category_order": CATEGORY_ORDER,
        "patch_labels": tuple(patch_labels),
    }
