"""Train sequence residuals on top of patch-specific champion/augment strength.

The empirical strength feature is cross-fitted by game for every training row.
Validation and test rows use tables built from the current-patch training split
only.  A residual sequence NN therefore learns ordering and interaction effects
without relearning the marginal champion/augment win rate from embeddings.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import click
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from aram_nn.models.augment_sequence import AugmentSequenceNN
try:
    from scripts.train_augment_ablation import (
        DataBundle,
        ResidualSequence,
        _compact_categories,
        _git_state,
        _load_data,
        _metrics,
        _probabilities,
        _seed_everything,
        _sha256,
        _stack_weights,
    )
    from scripts.train_augment_sequence import (
        DEFAULT_CATEGORIES,
        _baseline_predictions,
        _baseline_tables,
        _clustered_logloss_delta,
        _temperature,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/ rather than repo root.
    from train_augment_ablation import (
        DataBundle,
        ResidualSequence,
        _compact_categories,
        _git_state,
        _load_data,
        _metrics,
        _probabilities,
        _seed_everything,
        _sha256,
        _stack_weights,
    )
    from train_augment_sequence import (
        DEFAULT_CATEGORIES,
        _baseline_predictions,
        _baseline_tables,
        _clustered_logloss_delta,
        _temperature,
    )


def _logits(probabilities: np.ndarray) -> np.ndarray:
    values = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(values / (1 - values)).astype(np.float32)


def _cross_fitted_strength(
    data: DataBundle,
    mask: np.ndarray,
    *,
    folds: int,
) -> dict[str, np.ndarray]:
    """Return out-of-fold strength logits; an entire game stays in one fold."""
    pair = np.zeros((len(data.labels), 4), dtype=np.float32)
    odds = np.zeros_like(pair)
    fold_id = np.mod(data.gidx, folds)
    for fold in range(folds):
        holdout = mask & (fold_id == fold)
        fit = mask & (fold_id != fold)
        if not holdout.any() or not fit.any():
            raise click.ClickException(f"empty strength fold {fold}")
        tables = _baseline_tables(data.champions, data.labels, data.augments, fit)
        values = _baseline_predictions(
            tables, data.champions[holdout], data.augments[holdout]
        )
        pair[holdout] = _logits(values["champ_augment"])
        odds[holdout] = _logits(values["independent_odds"])
    return {"pair": pair, "odds": odds}


def _evaluation_strength(data: DataBundle) -> dict[str, np.ndarray]:
    """Use only current-patch train outcomes for validation/test features."""
    tables = _baseline_tables(
        data.champions, data.labels, data.augments, data.masks["train"]
    )
    result = {
        "pair": np.zeros((len(data.labels), 4), dtype=np.float32),
        "odds": np.zeros((len(data.labels), 4), dtype=np.float32),
    }
    evaluation = data.masks["validation"] | data.masks["test"]
    values = _baseline_predictions(
        tables, data.champions[evaluation], data.augments[evaluation]
    )
    result["pair"][evaluation] = _logits(values["champ_augment"])
    result["odds"][evaluation] = _logits(values["independent_odds"])
    return result


def _dataset(
    data: DataBundle,
    mask: np.ndarray,
    base_logits: np.ndarray,
    weights: np.ndarray,
) -> TensorDataset:
    return TensorDataset(
        torch.from_numpy(data.champions[mask]),
        torch.from_numpy(data.augments[mask]),
        torch.from_numpy(data.labels[mask]),
        torch.from_numpy(base_logits[mask]),
        torch.from_numpy(weights[mask]),
    )


@torch.no_grad()
def _predict(
    model: ResidualSequence,
    data: DataBundle,
    mask: np.ndarray,
    base_logits: np.ndarray,
    batch_size: int,
) -> np.ndarray:
    model.eval()
    indices = np.flatnonzero(mask)
    output: list[np.ndarray] = []
    for start in range(0, len(indices), batch_size):
        take = indices[start : start + batch_size]
        champions = torch.from_numpy(data.champions[take])
        augments = torch.from_numpy(data.augments[take])
        base = torch.from_numpy(base_logits[take])
        output.append((base + model(champions, augments)).numpy())
    return np.concatenate(output)


def _fit(
    *,
    data: DataBundle,
    base_logits: np.ndarray,
    train_mask: np.ndarray,
    previous_weight: float,
    epochs: int,
    batch_size: int,
    label: str,
) -> tuple[ResidualSequence, list[dict[str, float]], float]:
    model = ResidualSequence(
        AugmentSequenceNN(
            n_champions=len(data.champion_ids) + 1,
            n_augments=len(data.augment_ids) + 1,
            category_matrix=_compact_categories(data.augment_ids, DEFAULT_CATEGORIES),
            use_history=True,
        )
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=3e-4)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    weights = np.ones(len(data.labels), dtype=np.float32)
    weights[data.masks["previous"]] = previous_weight
    loader = DataLoader(
        _dataset(data, train_mask, base_logits, weights),
        batch_size=batch_size,
        shuffle=True,
    )
    validation = data.masks["validation"]
    valid_validation = data.augments[validation] > 0
    best_state: dict[str, torch.Tensor] | None = None
    best_loss = float("inf")
    history: list[dict[str, float]] = []
    stale = 0
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = total_weight = 0.0
        for champions, augments, labels, base, sample_weight in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = base + model(champions, augments)
            valid = augments > 0
            loss_matrix = criterion(logits, labels[:, None].expand_as(logits))
            expanded_weight = sample_weight[:, None].expand_as(logits)
            denominator = expanded_weight[valid].sum()
            loss = (loss_matrix[valid] * expanded_weight[valid]).sum() / denominator
            loss.backward()
            optimizer.step()
            total_loss += float(
                (loss_matrix[valid] * expanded_weight[valid]).sum().detach()
            )
            total_weight += float(denominator)
        val_logits = _predict(model, data, validation, base_logits, batch_size)
        val_probability = _probabilities(val_logits, 1.0)
        row = {
            "epoch": epoch,
            "train_log_loss": total_loss / total_weight,
            **_metrics(data.labels[validation], val_probability, valid_validation),
        }
        history.append(row)
        click.echo(
            f"  {label} epoch={epoch} train={row['train_log_loss']:.6f} "
            f"val={row['log_loss']:.6f} auc={row['auc']:.6f}"
        )
        if row["log_loss"] < best_loss - 1e-6:
            best_loss = row["log_loss"]
            best_state = copy.deepcopy(model.state_dict())
            stale = 0
        else:
            stale += 1
            if stale >= 2:
                break
    if best_state is None:
        raise click.ClickException(f"{label} did not train")
    model.load_state_dict(best_state)
    val_logits = _predict(model, data, validation, base_logits, batch_size)
    temperature = _temperature(val_logits, data.labels[validation], valid_validation)
    return model, history, temperature


def _load_sequence(
    checkpoint: Path,
    data: DataBundle,
) -> tuple[ResidualSequence, float]:
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
    if saved["champion_ids"] != data.champion_ids or saved["augment_ids"] != data.augment_ids:
        raise click.ClickException("existing checkpoint ID mapping differs from data")
    model = ResidualSequence(
        AugmentSequenceNN(
            n_champions=len(data.champion_ids) + 1,
            n_augments=len(data.augment_ids) + 1,
            category_matrix=_compact_categories(data.augment_ids, DEFAULT_CATEGORIES),
            use_history=True,
        )
    )
    model.load_state_dict(saved["sequence_state_dict"])
    return model, float(saved["temperatures"]["sequence_nn"])


@click.command()
@click.option(
    "--participants-parquet",
    type=click.Path(path_type=Path, exists=True),
    default=Path("data/analysis/augment_ablation_participants_16.14_16.15.parquet"),
    show_default=True,
)
@click.option(
    "--existing-checkpoint",
    type=click.Path(path_type=Path, exists=True),
    default=Path("data/models/augment_ablation_16.15_best.pt"),
    show_default=True,
)
@click.option("--current-patch", default="16.15", show_default=True)
@click.option("--previous-patch", default="16.14", show_default=True)
@click.option("--previous-weight", type=float, default=0.5, show_default=True)
@click.option("--folds", type=int, default=5, show_default=True)
@click.option("--epochs", type=int, default=8, show_default=True)
@click.option("--batch-size", type=int, default=32768, show_default=True)
@click.option("--seed", type=int, default=2400, show_default=True)
@click.option("--max-games", type=int, default=0, show_default=True)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("data/analysis/augment_strength_residual_16.15_full.json"),
    show_default=True,
)
@click.option(
    "--model-out",
    type=click.Path(path_type=Path),
    default=Path("data/models/augment_strength_residual_16.15.pt"),
    show_default=True,
)
def main(
    participants_parquet: Path,
    existing_checkpoint: Path,
    current_patch: str,
    previous_patch: str,
    previous_weight: float,
    folds: int,
    epochs: int,
    batch_size: int,
    seed: int,
    max_games: int,
    out: Path,
    model_out: Path,
) -> None:
    started = time.time()
    _seed_everything(seed)
    data = _load_data(
        participants_parquet,
        current_patch=current_patch,
        previous_patch=previous_patch,
        role_map_path=Path("docs/api/tier-list.json"),
        max_games=max_games,
    )
    click.echo("[strength] cross-fitting current patch")
    current_oof = _cross_fitted_strength(data, data.masks["train"], folds=folds)
    click.echo("[strength] cross-fitting previous patch")
    previous_oof = _cross_fitted_strength(data, data.masks["previous"], folds=folds)
    evaluation = _evaluation_strength(data)
    base: dict[str, np.ndarray] = {}
    for name in ("pair", "odds"):
        values = current_oof[name] + previous_oof[name] + evaluation[name]
        base[name] = values
    del current_oof, previous_oof, evaluation

    current_train = data.masks["train"]
    pooled_train = current_train | data.masks["previous"]
    click.echo("[pair_strength_residual_current]")
    pair_model, pair_history, pair_temperature = _fit(
        data=data,
        base_logits=base["pair"],
        train_mask=current_train,
        previous_weight=previous_weight,
        epochs=epochs,
        batch_size=batch_size,
        label="pair_strength_residual_current",
    )
    click.echo("[odds_residual_current]")
    odds_current_model, odds_current_history, odds_current_temperature = _fit(
        data=data,
        base_logits=base["odds"],
        train_mask=current_train,
        previous_weight=previous_weight,
        epochs=epochs,
        batch_size=batch_size,
        label="odds_residual_current",
    )
    click.echo("[odds_residual_cross_patch]")
    odds_cross_model, odds_cross_history, odds_cross_temperature = _fit(
        data=data,
        base_logits=base["odds"],
        train_mask=pooled_train,
        previous_weight=previous_weight,
        epochs=epochs,
        batch_size=batch_size,
        label="odds_residual_cross_patch",
    )
    sequence_model, sequence_temperature = _load_sequence(existing_checkpoint, data)

    temperatures = {
        "pair_strength_residual_current": pair_temperature,
        "odds_residual_current": odds_current_temperature,
        "odds_residual_cross_patch": odds_cross_temperature,
        "sequence_nn": sequence_temperature,
    }
    histories = {
        "pair_strength_residual_current": pair_history,
        "odds_residual_current": odds_current_history,
        "odds_residual_cross_patch": odds_cross_history,
    }
    models = {
        "pair_strength_residual_current": (pair_model, "pair"),
        "odds_residual_current": (odds_current_model, "odds"),
        "odds_residual_cross_patch": (odds_cross_model, "odds"),
    }
    phase_logits: dict[str, dict[str, np.ndarray]] = {}
    phase_probabilities: dict[str, dict[str, np.ndarray]] = {}
    metrics: dict[str, Any] = {}
    for phase in ("validation", "test"):
        mask = data.masks[phase]
        valid = data.augments[mask] > 0
        logits_by_name = {
            "champ_augment_strength": base["pair"][mask],
            "independent_odds": base["odds"][mask],
            "sequence_nn": _predict(
                sequence_model,
                data,
                mask,
                np.zeros_like(base["odds"]),
                batch_size,
            )
            / sequence_temperature,
        }
        for name, (model, base_name) in models.items():
            raw = _predict(model, data, mask, base[base_name], batch_size)
            logits_by_name[name] = raw / temperatures[name]
        probability = {
            name: _probabilities(values, 1.0) for name, values in logits_by_name.items()
        }
        phase_logits[phase] = logits_by_name
        phase_probabilities[phase] = probability
        metrics[phase] = {
            name: _metrics(data.labels[mask], values, valid)
            for name, values in probability.items()
        }
        metrics[phase]["by_slot"] = {
            str(slot + 1): {
                name: _metrics(
                    data.labels[mask],
                    values,
                    valid & (np.arange(4)[None, :] == slot),
                )
                for name, values in probability.items()
            }
            for slot in range(4)
        }

    stack_names = [
        "independent_odds",
        "sequence_nn",
        "pair_strength_residual_current",
        "odds_residual_current",
        "odds_residual_cross_patch",
    ]
    validation = data.masks["validation"]
    stack = _stack_weights(
        [phase_logits["validation"][name] for name in stack_names],
        data.labels[validation],
        data.augments[validation] > 0,
    )
    for phase in ("validation", "test"):
        mask = data.masks[phase]
        valid = data.augments[mask] > 0
        stacked_logits = sum(
            weight * phase_logits[phase][name]
            for name, weight in zip(stack_names, stack["weights"], strict=True)
        ) + stack["bias"]
        stacked = _probabilities(stacked_logits, 1.0)
        metrics[phase]["stacked_ensemble"] = _metrics(
            data.labels[mask], stacked, valid
        )
        metrics[phase]["clustered_vs_independent_odds"] = {
            name: _clustered_logloss_delta(
                data.gidx[mask],
                data.labels[mask],
                probability,
                phase_probabilities[phase]["independent_odds"],
                valid,
            )
            for name, probability in {
                **phase_probabilities[phase],
                "stacked_ensemble": stacked,
            }.items()
            if name != "independent_odds"
        }

    result = {
        "schema_version": 1,
        "model": "augment_strength_residual_v1",
        "provenance": {
            "participants_parquet": str(participants_parquet.resolve()),
            "participants_sha256": _sha256(participants_parquet),
            "existing_checkpoint": str(existing_checkpoint.resolve()),
            "git": _git_state(),
        },
        "config": {
            "current_patch": current_patch,
            "previous_patch": previous_patch,
            "previous_weight": previous_weight,
            "folds": folds,
            "epochs": epochs,
            "batch_size": batch_size,
            "seed": seed,
            "max_games": max_games,
        },
        "data": {
            "participants": len(data.labels),
            "split_participants": {
                name: int(mask.sum()) for name, mask in data.masks.items()
            },
        },
        "temperatures": temperatures,
        "histories": histories,
        "stack": {"names": stack_names, **stack},
        "metrics": metrics,
        "elapsed_sec": round(time.time() - started, 1),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    model_out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "pair_state_dict": pair_model.state_dict(),
            "odds_current_state_dict": odds_current_model.state_dict(),
            "odds_cross_state_dict": odds_cross_model.state_dict(),
            "champion_ids": data.champion_ids,
            "augment_ids": data.augment_ids,
            "temperatures": temperatures,
            "stack": result["stack"],
            "config": result["config"],
            "provenance": result["provenance"],
        },
        model_out,
    )
    click.echo(f"[strength] wrote {out} and {model_out}")
    for name, row in metrics["test"].items():
        if isinstance(row, dict) and "log_loss" in row:
            click.echo(
                f"  {name:<32} acc={row['accuracy']:.6f} auc={row['auc']:.6f} "
                f"ll={row['log_loss']:.6f} brier={row['brier']:.6f}"
            )


if __name__ == "__main__":
    main()
