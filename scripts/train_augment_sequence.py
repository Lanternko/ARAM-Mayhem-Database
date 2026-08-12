"""Train and backtest an autoregressive Mayhem augment-sequence NN.

The chronological 70/15/15 split is by game.  At pick slot t the NN receives
only champion, current augment, t, and already-selected augments/categories.
It is compared with three train-only empirical baselines:

* champ_augment: champion + current augment, ignoring order/history
* slot: champion + current augment + pick slot
* independent_odds: champion odds multiplied by independent per-augment odds
  ratios for the prefix selected so far
"""
from __future__ import annotations

import json
import math
import random
import gc
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
import torch
from scipy.optimize import minimize_scalar
from sklearn.metrics import log_loss, roc_auc_score
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from aram_nn.models.augment_sequence import AugmentSequenceNN
try:
    from scripts.analyze_augment_order import _parquet_bounds, _patch_created_bounds
except ModuleNotFoundError:  # Direct execution adds scripts/ rather than the repo root.
    from analyze_augment_order import _parquet_bounds, _patch_created_bounds


DEFAULT_DB = Path("data/lcu/games.db")
DEFAULT_PARQUET = Path("data/ratings/participants__q2400__1785746398.parquet")
DEFAULT_CATEGORIES = Path("scripts/augment_category_overrides.json")
CATEGORY_ORDER = ("ap", "ad", "tank", "support", "gold", "mechanic", "cd", "new", "crit", "amp")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _load_rows(parquet: Path, created_min: int, created_max: int) -> tuple[np.ndarray, ...]:
    expressions = [
        pl.col("gidx").cast(pl.Int64),
        pl.col("created_ms").cast(pl.Int64),
        pl.col("champ").cast(pl.Int64),
        pl.col("win").cast(pl.Float32),
    ]
    expressions.extend(
        pl.col("augments").list.get(slot, null_on_oob=True).fill_null(0).cast(pl.Int64).alias(f"a{slot}")
        for slot in range(4)
    )
    frame = (
        pl.scan_parquet(parquet)
        .filter(pl.col("created_ms").is_between(created_min, created_max))
        .select(expressions)
        .filter(pl.col("a0") > 0)
        .collect(engine="streaming")
    )
    augments = np.column_stack([frame[f"a{slot}"].to_numpy() for slot in range(4)]).astype(np.int64)
    return (
        frame["gidx"].to_numpy().astype(np.int64),
        frame["created_ms"].to_numpy().astype(np.int64),
        frame["champ"].to_numpy().astype(np.int64),
        frame["win"].to_numpy().astype(np.float32),
        augments,
    )


def _split_masks(
    gidx: np.ndarray,
    created: np.ndarray,
    val_key: tuple[int, str],
    test_key: tuple[int, str],
) -> dict[str, np.ndarray]:
    val_created, val_gidx = int(val_key[0]), int(val_key[1])
    test_created, test_gidx = int(test_key[0]), int(test_key[1])
    before_val = (created < val_created) | ((created == val_created) & (gidx < val_gidx))
    before_test = (created < test_created) | ((created == test_created) & (gidx < test_gidx))
    return {
        "train": before_val,
        "validation": (~before_val) & before_test,
        "test": ~before_test,
    }


def _category_matrix(path: Path, n_augments: int) -> torch.Tensor:
    raw = json.loads(path.read_text(encoding="utf-8"))
    index = {name: idx for idx, name in enumerate(CATEGORY_ORDER)}
    matrix = np.zeros((n_augments, len(CATEGORY_ORDER)), dtype=np.float32)
    for raw_id, categories in raw.items():
        augment_id = int(raw_id)
        if not 0 < augment_id < n_augments:
            continue
        for category in categories:
            if category in index:
                matrix[augment_id, index[category]] = 1.0
    return torch.from_numpy(matrix)


def _smoothed_rate(wins: np.ndarray, games: np.ndarray, prior: np.ndarray, k: float) -> np.ndarray:
    return (wins + prior * k) / np.maximum(games + k, 1.0)


def _baseline_tables(
    champions: np.ndarray,
    labels: np.ndarray,
    augments: np.ndarray,
    train_mask: np.ndarray,
) -> dict[str, np.ndarray]:
    max_champion = int(champions.max()) + 1
    max_augment = int(augments.max()) + 1
    c = champions[train_mask]
    y = labels[train_mask]
    a = augments[train_mask]
    global_rate = float(y.mean())

    champion_games = np.bincount(c, minlength=max_champion).astype(np.float64)
    champion_wins = np.bincount(c, weights=y, minlength=max_champion).astype(np.float64)
    champion_rate = _smoothed_rate(
        champion_wins,
        champion_games,
        np.full(max_champion, global_rate),
        100.0,
    ).astype(np.float32)

    pair_shape = (max_champion, max_augment)
    pair_games = np.zeros(pair_shape, dtype=np.uint32)
    pair_wins = np.zeros(pair_shape, dtype=np.float32)
    for slot in range(4):
        valid = a[:, slot] > 0
        np.add.at(pair_games, (c[valid], a[valid, slot]), 1)
        np.add.at(pair_wins, (c[valid], a[valid, slot]), y[valid])
    pair_rate = _smoothed_rate(
        pair_wins,
        pair_games,
        champion_rate[:, None],
        100.0,
    ).astype(np.float32)
    del pair_games, pair_wins

    slot_rate = np.empty((4, max_champion, max_augment), dtype=np.float32)
    for slot in range(4):
        games = np.zeros(pair_shape, dtype=np.uint32)
        wins = np.zeros(pair_shape, dtype=np.float32)
        valid = a[:, slot] > 0
        np.add.at(games, (c[valid], a[valid, slot]), 1)
        np.add.at(wins, (c[valid], a[valid, slot]), y[valid])
        slot_rate[slot] = _smoothed_rate(wins, games, pair_rate, 150.0)
    return {"champion": champion_rate, "pair": pair_rate, "slot": slot_rate}


def _logit(p: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, 1e-5, 1 - 1e-5)
    return np.log(clipped / (1 - clipped))


def _baseline_predictions(
    tables: dict[str, np.ndarray],
    champions: np.ndarray,
    augments: np.ndarray,
) -> dict[str, np.ndarray]:
    n = len(champions)
    champion_rate = tables["champion"][champions]
    pair = tables["pair"][champions[:, None], augments]
    slot = np.empty((n, 4), dtype=np.float32)
    for idx in range(4):
        slot[:, idx] = tables["slot"][idx, champions, augments[:, idx]]
    effects = _logit(pair) - _logit(champion_rate[:, None])
    independent = 1.0 / (1.0 + np.exp(-(_logit(champion_rate)[:, None] + effects.cumsum(axis=1))))
    return {
        "champ_augment": pair.astype(np.float32),
        "slot": slot,
        "independent_odds": independent.astype(np.float32),
    }


def _metrics(labels: np.ndarray, probabilities: np.ndarray, valid: np.ndarray) -> dict[str, float]:
    y = np.broadcast_to(labels[:, None], probabilities.shape)[valid].astype(np.float64)
    p = np.clip(probabilities[valid].astype(np.float64), 1e-6, 1 - 1e-6)
    return {
        "observations": int(len(y)),
        "accuracy": round(float(np.mean((p >= 0.5) == y)), 6),
        "auc": round(float(roc_auc_score(y, p)), 6),
        "log_loss": round(float(log_loss(y, p, labels=[0.0, 1.0])), 6),
        "brier": round(float(np.mean((p - y) ** 2)), 6),
    }


@torch.no_grad()
def _predict_logits(model: nn.Module, champions: np.ndarray, augments: np.ndarray, batch_size: int) -> np.ndarray:
    model.eval()
    out = []
    for start in range(0, len(champions), batch_size):
        stop = start + batch_size
        logits = model(
            torch.from_numpy(champions[start:stop]),
            torch.from_numpy(augments[start:stop]),
        )
        out.append(logits.cpu().numpy())
    return np.concatenate(out, axis=0)


def _temperature(logits: np.ndarray, labels: np.ndarray, valid: np.ndarray) -> float:
    y = np.broadcast_to(labels[:, None], logits.shape)[valid].astype(np.float64)
    z = logits[valid].astype(np.float64)

    def objective(log_t: float) -> float:
        temperature = math.exp(log_t)
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(z / temperature, -30, 30)))
        return float(log_loss(y, probabilities, labels=[0.0, 1.0]))

    result = minimize_scalar(objective, bounds=(-2.0, 2.0), method="bounded")
    return float(math.exp(result.x))


def _blend_weight(
    nn_logits: np.ndarray,
    independent_probabilities: np.ndarray,
    labels: np.ndarray,
    valid: np.ndarray,
) -> float:
    """Validation-only logit blend weight; 0=odds baseline, 1=sequence NN."""
    targets = np.broadcast_to(labels[:, None], nn_logits.shape)[valid].astype(np.float64)
    baseline = np.clip(independent_probabilities, 1e-6, 1 - 1e-6)
    baseline_logits = np.log(baseline / (1 - baseline))

    def objective(weight: float) -> float:
        logits = weight * nn_logits + (1 - weight) * baseline_logits
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
        return float(log_loss(targets, probabilities[valid], labels=[0.0, 1.0]))

    result = minimize_scalar(objective, bounds=(0.0, 1.0), method="bounded")
    return float(result.x)


def _fit_model(
    model: nn.Module,
    *,
    train_data: TensorDataset,
    validation_champions: np.ndarray,
    validation_augments: np.ndarray,
    validation_labels: np.ndarray,
    validation_valid: np.ndarray,
    epochs: int,
    batch_size: int,
    label: str,
) -> tuple[dict[str, torch.Tensor], list[dict[str, float]]]:
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    loader = DataLoader(train_data, batch_size=batch_size, shuffle=True, num_workers=0)
    best_loss = float("inf")
    best_state: dict[str, torch.Tensor] | None = None
    history: list[dict[str, float]] = []
    for epoch in range(1, epochs + 1):
        model.train()
        loss_sum = observations = 0
        for batch_champions, batch_augments, batch_labels in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(batch_champions, batch_augments)
            valid = batch_augments > 0
            targets = batch_labels[:, None].expand_as(logits)
            losses = criterion(logits, targets)
            loss = losses[valid].mean()
            loss.backward()
            optimizer.step()
            loss_sum += float(loss.detach()) * int(valid.sum())
            observations += int(valid.sum())
        val_logits = _predict_logits(
            model, validation_champions, validation_augments, batch_size
        )
        val_probabilities = 1.0 / (1.0 + np.exp(-np.clip(val_logits, -30, 30)))
        val_metrics = _metrics(validation_labels, val_probabilities, validation_valid)
        row = {
            "epoch": epoch,
            "train_log_loss": loss_sum / observations,
            **val_metrics,
        }
        history.append(row)
        click.echo(
            f"  {label} epoch={epoch} train_ll={row['train_log_loss']:.6f} "
            f"val_ll={row['log_loss']:.6f} auc={row['auc']:.6f}"
        )
        if row["log_loss"] < best_loss - 1e-5:
            best_loss = row["log_loss"]
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
        elif epoch >= 4:
            break
    if best_state is None:
        raise click.ClickException(f"{label} training produced no model")
    model.load_state_dict(best_state)
    return best_state, history


def _clustered_logloss_delta(
    game_ids: np.ndarray,
    labels: np.ndarray,
    candidate: np.ndarray,
    baseline: np.ndarray,
    valid: np.ndarray,
) -> dict[str, float]:
    targets = np.broadcast_to(labels[:, None], candidate.shape)
    candidate = np.clip(candidate, 1e-6, 1 - 1e-6)
    baseline = np.clip(baseline, 1e-6, 1 - 1e-6)
    candidate_loss = -(targets * np.log(candidate) + (1 - targets) * np.log(1 - candidate))
    baseline_loss = -(targets * np.log(baseline) + (1 - targets) * np.log(1 - baseline))
    observation_delta = (candidate_loss - baseline_loss)[valid]
    observation_games = np.broadcast_to(game_ids[:, None], valid.shape)[valid]
    _, inverse = np.unique(observation_games, return_inverse=True)
    sums = np.bincount(inverse, weights=observation_delta)
    counts = np.bincount(inverse)
    per_game = sums / counts
    mean = float(per_game.mean())
    se = float(per_game.std(ddof=1) / math.sqrt(len(per_game)))
    return {
        "games": int(len(per_game)),
        "mean_delta": round(mean, 8),
        "ci95_low": round(mean - 1.96 * se, 8),
        "ci95_high": round(mean + 1.96 * se, 8),
    }


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option("--participants-parquet", type=click.Path(path_type=Path, exists=True), default=DEFAULT_PARQUET)
@click.option("--patch-prefix", default="16.15", show_default=True)
@click.option("--epochs", type=int, default=8, show_default=True)
@click.option("--batch-size", type=int, default=8192, show_default=True)
@click.option("--seed", type=int, default=2400, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("data/analysis/augment_sequence_nn_16.15.json"))
@click.option("--model-out", type=click.Path(path_type=Path), default=Path("data/models/augment_sequence_nn_16.15.pt"))
def main(
    db: Path,
    participants_parquet: Path,
    patch_prefix: str,
    epochs: int,
    batch_size: int,
    seed: int,
    out: Path,
    model_out: Path,
) -> None:
    _seed_everything(seed)
    created_min, created_max = _patch_created_bounds(db, queue_id=2400, patch_prefix=patch_prefix)
    total_games, val_key, test_key = _parquet_bounds(
        participants_parquet, created_min=created_min, created_max=created_max
    )
    gidx, created, champions, labels, augments = _load_rows(
        participants_parquet, int(created_min), int(created_max)
    )
    masks = _split_masks(gidx, created, val_key, test_key)
    valid_slots = augments > 0
    click.echo(
        f"[augment-sequence] games={total_games:,} participants={len(labels):,} "
        f"train/val/test={masks['train'].sum():,}/{masks['validation'].sum():,}/{masks['test'].sum():,}"
    )

    max_augment = int(augments.max()) + 1
    categories = _category_matrix(DEFAULT_CATEGORIES, max_augment)
    model_kwargs = {
        "n_champions": int(champions.max()) + 1,
        "n_augments": max_augment,
        "category_matrix": categories,
    }
    model = AugmentSequenceNN(
        **model_kwargs,
        use_history=True,
    )
    no_history_model = AugmentSequenceNN(
        **model_kwargs,
        use_history=False,
    )
    train_indices = np.flatnonzero(masks["train"])
    train_data = TensorDataset(
        torch.from_numpy(champions[train_indices]),
        torch.from_numpy(augments[train_indices]),
        torch.from_numpy(labels[train_indices]),
    )
    validation_indices = np.flatnonzero(masks["validation"])
    fit_kwargs = {
        "train_data": train_data,
        "validation_champions": champions[validation_indices],
        "validation_augments": augments[validation_indices],
        "validation_labels": labels[validation_indices],
        "validation_valid": valid_slots[validation_indices],
        "epochs": epochs,
        "batch_size": batch_size,
    }
    best_state, history = _fit_model(model, label="sequence", **fit_kwargs)
    no_history_state, no_history_history = _fit_model(
        no_history_model, label="no_history", **fit_kwargs
    )

    # Dense empirical tables occupy a few hundred MB.  They are evaluation-only,
    # so build them after both models finish instead of competing with autograd.
    gc.collect()
    tables = _baseline_tables(champions, labels, augments, masks["train"])

    metrics: dict[str, dict[str, Any]] = {}
    val_logits = _predict_logits(model, champions[validation_indices], augments[validation_indices], batch_size)
    temperature = _temperature(val_logits, labels[validation_indices], valid_slots[validation_indices])
    no_history_val_logits = _predict_logits(
        no_history_model,
        champions[validation_indices],
        augments[validation_indices],
        batch_size,
    )
    no_history_temperature = _temperature(
        no_history_val_logits,
        labels[validation_indices],
        valid_slots[validation_indices],
    )
    validation_baseline = _baseline_predictions(
        tables,
        champions[validation_indices],
        augments[validation_indices],
    )["independent_odds"]
    blend_weight = _blend_weight(
        val_logits / temperature,
        validation_baseline,
        labels[validation_indices],
        valid_slots[validation_indices],
    )
    for phase in ("validation", "test"):
        indices = np.flatnonzero(masks[phase])
        phase_champions = champions[indices]
        phase_augments = augments[indices]
        phase_labels = labels[indices]
        phase_valid = valid_slots[indices]
        baseline = _baseline_predictions(tables, phase_champions, phase_augments)
        logits = _predict_logits(model, phase_champions, phase_augments, batch_size)
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits / temperature, -30, 30)))
        no_history_logits = _predict_logits(
            no_history_model, phase_champions, phase_augments, batch_size
        )
        no_history_probabilities = 1.0 / (
            1.0 + np.exp(-np.clip(no_history_logits / no_history_temperature, -30, 30))
        )
        independent = np.clip(baseline["independent_odds"], 1e-6, 1 - 1e-6)
        independent_logits = np.log(independent / (1 - independent))
        ensemble_logits = (
            blend_weight * (logits / temperature)
            + (1 - blend_weight) * independent_logits
        )
        ensemble_probabilities = 1.0 / (
            1.0 + np.exp(-np.clip(ensemble_logits, -30, 30))
        )
        phase_metrics = {
            name: _metrics(phase_labels, values, phase_valid)
            for name, values in baseline.items()
        }
        phase_metrics["sequence_nn"] = _metrics(phase_labels, probabilities, phase_valid)
        phase_metrics["no_history_nn"] = _metrics(
            phase_labels, no_history_probabilities, phase_valid
        )
        phase_metrics["ensemble"] = _metrics(
            phase_labels, ensemble_probabilities, phase_valid
        )
        all_predictions = {
            **baseline,
            "no_history_nn": no_history_probabilities,
            "sequence_nn": probabilities,
            "ensemble": ensemble_probabilities,
        }
        phase_metrics["by_slot"] = {
            str(slot + 1): {
                name: _metrics(
                    phase_labels,
                    values,
                    phase_valid & (np.arange(4)[None, :] == slot),
                )
                for name, values in all_predictions.items()
            }
            for slot in range(4)
        }
        phase_metrics["clustered_logloss_delta"] = {
            name: _clustered_logloss_delta(
                gidx[indices],
                phase_labels,
                probabilities,
                baseline_values,
                phase_valid,
            )
            for name, baseline_values in {
                "vs_champ_augment": baseline["champ_augment"],
                "vs_independent_odds": baseline["independent_odds"],
                "vs_no_history_nn": no_history_probabilities,
            }.items()
        }
        phase_metrics["clustered_logloss_delta"]["ensemble_vs_independent_odds"] = (
            _clustered_logloss_delta(
                gidx[indices],
                phase_labels,
                ensemble_probabilities,
                baseline["independent_odds"],
                phase_valid,
            )
        )
        metrics[phase] = phase_metrics

    result = {
        "schema_version": 1,
        "model": "augment_sequence_nn_v1",
        "patch_prefix": patch_prefix,
        "split": {
            "method": "chronological_70_15_15_by_game",
            "games": total_games,
            "participants": int(len(labels)),
            "train_participants": int(masks["train"].sum()),
            "validation_participants": int(masks["validation"].sum()),
            "test_participants": int(masks["test"].sum()),
            "validation_start": list(val_key),
            "test_start": list(test_key),
        },
        "features": [
            "champion", "current_augment", "pick_slot", "prior_augment_embeddings",
            "current_x_prior_augment", "current_categories", "prior_category_counts",
            "current_x_prior_categories",
        ],
        "temperature": temperature,
        "no_history_temperature": no_history_temperature,
        "ensemble_nn_weight": blend_weight,
        "history": history,
        "no_history_history": no_history_history,
        "metrics": metrics,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    model_out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": best_state,
            "temperature": temperature,
            "category_order": CATEGORY_ORDER,
            "patch_prefix": patch_prefix,
        },
        model_out,
    )
    click.echo(f"[augment-sequence] wrote {out} and {model_out}")
    for phase in ("validation", "test"):
        click.echo(f"[{phase}]")
        for name in (
            "champ_augment", "slot", "independent_odds", "no_history_nn", "sequence_nn",
            "ensemble",
        ):
            row = metrics[phase][name]
            click.echo(
                f"  {name:<16} acc={row['accuracy']:.4f} auc={row['auc']:.4f} "
                f"ll={row['log_loss']:.6f} brier={row['brier']:.6f}"
            )


if __name__ == "__main__":
    main()
