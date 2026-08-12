"""Run cumulative augment recommendation ablations on one fixed test split.

Ladder:
  independent odds -> sparse logistic main effects -> global augment pairs ->
  role pair residuals -> champion pair residuals -> previous-patch partial
  pooling -> sequence-NN residual -> validation-stacked ensemble.

Every learned interaction is causal-masked: slot t sees only augments < t.
The task remains selected-only conditional win prediction, not causal uplift.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
import torch
from scipy.optimize import minimize
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from aram_nn.models.augment_sequence import AugmentSequenceNN
try:
    from scripts.train_augment_sequence import (
        CATEGORY_ORDER,
        DEFAULT_CATEGORIES,
        _baseline_predictions,
        _baseline_tables,
        _clustered_logloss_delta,
        _metrics,
        _temperature,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/ rather than the repo root.
    from train_augment_sequence import (
        CATEGORY_ORDER,
        DEFAULT_CATEGORIES,
        _baseline_predictions,
        _baseline_tables,
        _clustered_logloss_delta,
        _metrics,
        _temperature,
    )


ROLE_ORDER = ("Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank", "Unknown")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_state() -> dict[str, str]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ("git", *args), capture_output=True, text=True, encoding="utf-8", check=False
        )
        return result.stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "status": run("status", "--short")}


@dataclass
class DataBundle:
    gidx: np.ndarray
    created: np.ndarray
    champions: np.ndarray
    roles: np.ndarray
    labels: np.ndarray
    augments: np.ndarray
    masks: dict[str, np.ndarray]
    champion_ids: list[int]
    augment_ids: list[int]


def _role_lookup(path: Path, champion_ids: list[int]) -> dict[int, int]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("champs"), dict):
        raw = raw["champs"]
    role_index = {name: idx for idx, name in enumerate(ROLE_ORDER)}
    result: dict[int, int] = {}
    for champion_id in champion_ids:
        meta = raw.get(str(champion_id), {})
        tags = meta.get("tags") if isinstance(meta, dict) else None
        role = str(tags[0]) if isinstance(tags, list) and tags else "Unknown"
        result[champion_id] = role_index.get(role, role_index["Unknown"])
    return result


def _load_data(
    path: Path,
    *,
    current_patch: str,
    previous_patch: str,
    role_map_path: Path,
    max_games: int,
) -> DataBundle:
    expressions = [
        pl.col("gidx").cast(pl.Int64),
        pl.col("created_ms").cast(pl.Int64),
        pl.col("patch").cast(pl.String),
        pl.col("champ").cast(pl.Int64),
        pl.col("win").cast(pl.Float32),
    ]
    expressions.extend(
        pl.col("augments")
        .list.get(slot, null_on_oob=True)
        .fill_null(0)
        .cast(pl.Int64)
        .alias(f"a{slot}")
        for slot in range(4)
    )
    frame = (
        pl.scan_parquet(path)
        .filter(pl.col("patch").is_in([current_patch, previous_patch]))
        .select(expressions)
        .filter(pl.col("a0") > 0)
        .collect(engine="streaming")
    )
    if max_games > 0:
        keep_games = (
            frame.select("gidx", "patch", "created_ms")
            .unique("gidx")
            .sort(["patch", "created_ms", "gidx"])
            .group_by("patch", maintain_order=True)
            .head(max_games)
            .get_column("gidx")
        )
        frame = frame.filter(pl.col("gidx").is_in(keep_games.to_list()))

    raw_champions = frame["champ"].to_numpy().astype(np.int64)
    raw_augments = np.column_stack(
        [frame[f"a{slot}"].to_numpy() for slot in range(4)]
    ).astype(np.int64)
    champion_ids = sorted(int(value) for value in np.unique(raw_champions))
    augment_ids = sorted(int(value) for value in np.unique(raw_augments) if value > 0)
    champion_map = {value: idx + 1 for idx, value in enumerate(champion_ids)}
    augment_map = {value: idx + 1 for idx, value in enumerate(augment_ids)}
    champion_lookup = np.zeros(max(champion_ids) + 1, dtype=np.int64)
    for raw_id, compact_id in champion_map.items():
        champion_lookup[raw_id] = compact_id
    champions = champion_lookup[raw_champions]
    augment_lookup = np.zeros(max(augment_ids) + 1, dtype=np.int64)
    for raw_id, compact_id in augment_map.items():
        augment_lookup[raw_id] = compact_id
    augments = augment_lookup[raw_augments]
    role_by_raw = _role_lookup(role_map_path, champion_ids)
    role_lookup = np.full(max(champion_ids) + 1, ROLE_ORDER.index("Unknown"), dtype=np.int64)
    for raw_id, role_id in role_by_raw.items():
        role_lookup[raw_id] = role_id
    roles = role_lookup[raw_champions]

    gidx = frame["gidx"].to_numpy().astype(np.int64)
    created = frame["created_ms"].to_numpy().astype(np.int64)
    labels = frame["win"].to_numpy().astype(np.float32)
    current = (frame["patch"] == current_patch).to_numpy()
    previous = (frame["patch"] == previous_patch).to_numpy()
    game_frame = (
        frame.filter(pl.col("patch") == current_patch)
        .select("gidx", "created_ms")
        .unique("gidx")
        .sort(["created_ms", "gidx"])
    )
    games = game_frame["gidx"].to_numpy().astype(np.int64)
    if len(games) < 20:
        raise click.ClickException(f"only {len(games)} current-patch games")
    val_at = int(len(games) * 0.70)
    test_at = int(len(games) * 0.85)
    validation = current & np.isin(gidx, games[val_at:test_at])
    test = current & np.isin(gidx, games[test_at:])
    train = current & ~validation & ~test
    return DataBundle(
        gidx=gidx,
        created=created,
        champions=champions,
        roles=roles,
        labels=labels,
        augments=augments,
        masks={"previous": previous, "train": train, "validation": validation, "test": test},
        champion_ids=champion_ids,
        augment_ids=augment_ids,
    )


@dataclass
class PairSupport:
    global_gate: torch.Tensor
    role_gate: torch.Tensor
    champion_feature: torch.Tensor
    champion_gate: torch.Tensor
    champion_features: int


def _pair_support(
    data: DataBundle,
    mask: np.ndarray,
    *,
    sample_weights: np.ndarray,
    pair_prior: float,
    champion_pair_min: float,
) -> PairSupport:
    n_champions = len(data.champion_ids) + 1
    n_augments = len(data.augment_ids) + 1
    n_roles = len(ROLE_ORDER)
    champions = data.champions[mask]
    roles = data.roles[mask]
    augments = data.augments[mask]
    weights = sample_weights[mask].astype(np.float32)
    pair_space = n_augments * n_augments
    global_counts_flat = np.zeros(pair_space, dtype=np.float64)
    role_counts_flat = np.zeros(n_roles * pair_space, dtype=np.float64)
    champion_counts_flat = np.zeros(n_champions * pair_space, dtype=np.float64)
    for slot in range(1, 4):
        current = augments[:, slot]
        valid_current = current > 0
        for prior_slot in range(slot):
            prior = augments[:, prior_slot]
            valid = valid_current & (prior > 0)
            if not valid.any():
                continue
            w = weights[valid].astype(np.float64, copy=False)
            pair_id = current[valid] * n_augments + prior[valid]
            global_counts_flat += np.bincount(
                pair_id, weights=w, minlength=pair_space
            )
            role_counts_flat += np.bincount(
                roles[valid] * pair_space + pair_id,
                weights=w,
                minlength=n_roles * pair_space,
            )
            champion_counts_flat += np.bincount(
                champions[valid] * pair_space + pair_id,
                weights=w,
                minlength=n_champions * pair_space,
            )
    global_counts = global_counts_flat.astype(np.float32).reshape(
        n_augments, n_augments
    )
    role_counts = role_counts_flat.astype(np.float32).reshape(
        n_roles, n_augments, n_augments
    )
    champion_counts = champion_counts_flat.astype(np.float32).reshape(
        n_champions, n_augments, n_augments
    )
    champion_feature = np.zeros(champion_counts.shape, dtype=np.int32)
    selected = champion_counts >= champion_pair_min
    champion_feature[selected] = np.arange(1, int(selected.sum()) + 1, dtype=np.int32)
    champion_gate = np.zeros(int(selected.sum()) + 1, dtype=np.float32)
    champion_gate[1:] = champion_counts[selected] / (
        champion_counts[selected] + pair_prior
    )
    return PairSupport(
        global_gate=torch.from_numpy(global_counts / (global_counts + pair_prior)),
        role_gate=torch.from_numpy(role_counts / (role_counts + pair_prior)),
        champion_feature=torch.from_numpy(champion_feature),
        champion_gate=torch.from_numpy(champion_gate),
        champion_features=int(selected.sum()),
    )


class SparseAugmentLogit(nn.Module):
    """Logistic regression over sparse categorical main and pair features."""

    def __init__(
        self,
        *,
        n_champions: int,
        n_augments: int,
        n_roles: int,
        support: PairSupport,
        global_pairs: bool,
        role_pairs: bool,
        champion_pairs: bool,
        patch_delta: bool,
    ) -> None:
        super().__init__()
        self.n_augments = n_augments
        self.global_pairs = global_pairs
        self.role_pairs = role_pairs
        self.champion_pairs = champion_pairs
        self.patch_delta = patch_delta
        self.bias = nn.Parameter(torch.zeros(()))
        self.champion = nn.Embedding(n_champions, 1, padding_idx=0)
        self.current = nn.Embedding(n_augments, 1, padding_idx=0)
        self.prior = nn.Embedding(n_augments, 1, padding_idx=0)
        self.slot = nn.Embedding(4, 1)
        self.champion_current = nn.Embedding(n_champions * n_augments, 1)
        self.pair = nn.Embedding(n_augments * n_augments, 1, padding_idx=0)
        self.role_pair = nn.Embedding(
            n_roles * n_augments * n_augments, 1, padding_idx=0
        )
        self.champion_pair = nn.Embedding(
            support.champion_features + 1, 1, padding_idx=0
        )
        self.register_buffer("global_gate", support.global_gate, persistent=True)
        self.register_buffer("role_gate", support.role_gate, persistent=True)
        self.register_buffer(
            "champion_feature", support.champion_feature.to(torch.int64), persistent=True
        )
        self.register_buffer("champion_gate", support.champion_gate, persistent=True)
        if patch_delta:
            self.delta_bias = nn.Parameter(torch.zeros(()))
            self.delta_champion = nn.Embedding(n_champions, 1, padding_idx=0)
            self.delta_current = nn.Embedding(n_augments, 1, padding_idx=0)
            self.delta_pair = nn.Embedding(n_augments * n_augments, 1, padding_idx=0)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        for module in self.modules():
            if isinstance(module, nn.Embedding):
                nn.init.zeros_(module.weight)

    def forward(
        self,
        champions: torch.Tensor,
        roles: torch.Tensor,
        augments: torch.Tensor,
        current_patch: torch.Tensor,
    ) -> torch.Tensor:
        batch = len(champions)
        slots = torch.arange(4, device=augments.device).unsqueeze(0).expand(batch, -1)
        champ = champions[:, None].expand(-1, 4)
        current_valid = augments > 0
        champion_current_id = champ * self.n_augments + augments
        logits = (
            self.bias
            + self.champion(champions).squeeze(-1)[:, None]
            + self.current(augments).squeeze(-1)
            + self.slot(slots).squeeze(-1)
            + self.champion_current(champion_current_id).squeeze(-1)
        )
        if self.patch_delta:
            patch = current_patch.to(logits.dtype)[:, None]
            logits = logits + patch * (
                self.delta_bias
                + self.delta_champion(champions).squeeze(-1)[:, None]
                + self.delta_current(augments).squeeze(-1)
            )
        for slot in range(1, 4):
            current = augments[:, slot]
            for prior_slot in range(slot):
                prior = augments[:, prior_slot]
                valid = (current > 0) & (prior > 0)
                logits[:, slot] = logits[:, slot] + self.prior(prior).squeeze(-1) * valid
                pair_id = current * self.n_augments + prior
                if self.global_pairs:
                    gate = self.global_gate[current, prior]
                    logits[:, slot] = logits[:, slot] + self.pair(pair_id).squeeze(-1) * gate
                    if self.patch_delta:
                        logits[:, slot] = logits[:, slot] + (
                            self.delta_pair(pair_id).squeeze(-1)
                            * gate
                            * current_patch.to(logits.dtype)
                        )
                if self.role_pairs:
                    role_id = roles * self.n_augments * self.n_augments + pair_id
                    gate = self.role_gate[roles, current, prior]
                    logits[:, slot] = logits[:, slot] + self.role_pair(role_id).squeeze(-1) * gate
                if self.champion_pairs:
                    feature = self.champion_feature[champions, current, prior]
                    gate = self.champion_gate[feature]
                    logits[:, slot] = logits[:, slot] + self.champion_pair(feature).squeeze(-1) * gate
        return logits * current_valid


def _arrays(data: DataBundle, mask: np.ndarray, weights: np.ndarray) -> TensorDataset:
    indices = np.flatnonzero(mask)
    return TensorDataset(
        torch.from_numpy(data.champions[indices]),
        torch.from_numpy(data.roles[indices]),
        torch.from_numpy(data.augments[indices]),
        torch.from_numpy(data.labels[indices]),
        torch.from_numpy((data.masks["train"] | data.masks["validation"] | data.masks["test"])[indices]),
        torch.from_numpy(weights[indices].astype(np.float32)),
    )


@torch.no_grad()
def _predict_glm(
    model: SparseAugmentLogit, data: DataBundle, mask: np.ndarray, batch_size: int
) -> np.ndarray:
    model.eval()
    indices = np.flatnonzero(mask)
    out: list[np.ndarray] = []
    for start in range(0, len(indices), batch_size):
        take = indices[start : start + batch_size]
        logits = model(
            torch.from_numpy(data.champions[take]),
            torch.from_numpy(data.roles[take]),
            torch.from_numpy(data.augments[take]),
            torch.from_numpy(
                (data.masks["train"] | data.masks["validation"] | data.masks["test"])[take]
            ),
        )
        out.append(logits.numpy())
    return np.concatenate(out)


def _fit_glm(
    model: SparseAugmentLogit,
    *,
    data: DataBundle,
    train_mask: np.ndarray,
    weights: np.ndarray,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    weight_decay: float,
    label: str,
) -> tuple[SparseAugmentLogit, list[dict[str, float]], float]:
    loader = DataLoader(
        _arrays(data, train_mask, weights),
        batch_size=batch_size,
        shuffle=True,
        num_workers=0,
    )
    shared_parameters = []
    delta_parameters = []
    for name, parameter in model.named_parameters():
        (delta_parameters if name.startswith("delta_") else shared_parameters).append(parameter)
    parameter_groups: list[dict[str, Any]] = [
        {"params": shared_parameters, "weight_decay": weight_decay}
    ]
    if delta_parameters:
        parameter_groups.append(
            {"params": delta_parameters, "weight_decay": weight_decay * 10.0}
        )
    optimizer = torch.optim.AdamW(parameter_groups, lr=learning_rate)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    best_state: dict[str, torch.Tensor] | None = None
    best_loss = float("inf")
    history: list[dict[str, float]] = []
    validation = data.masks["validation"]
    valid_validation = data.augments[validation] > 0
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = total_weight = 0.0
        for champions, roles, augments, labels, current_patch, sample_weight in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(champions, roles, augments, current_patch)
            valid = augments > 0
            target = labels[:, None].expand_as(logits)
            observation_weight = sample_weight[:, None].expand_as(logits) * valid
            losses = criterion(logits, target)
            loss = (losses * observation_weight).sum() / observation_weight.sum()
            loss.backward()
            optimizer.step()
            total_loss += float((losses * observation_weight).sum().detach())
            total_weight += float(observation_weight.sum())
        val_logits = _predict_glm(model, data, validation, batch_size)
        val_probabilities = 1.0 / (1.0 + np.exp(-np.clip(val_logits, -30, 30)))
        row = {
            "epoch": epoch,
            "train_log_loss": total_loss / total_weight,
            **_metrics(data.labels[validation], val_probabilities, valid_validation),
        }
        history.append(row)
        click.echo(
            f"  {label} epoch={epoch} train={row['train_log_loss']:.6f} "
            f"val={row['log_loss']:.6f} auc={row['auc']:.6f}"
        )
        if row["log_loss"] < best_loss - 1e-6:
            best_loss = row["log_loss"]
            best_state = copy.deepcopy(model.state_dict())
        elif epoch >= 3:
            break
    if best_state is None:
        raise click.ClickException(f"{label} did not train")
    model.load_state_dict(best_state)
    val_logits = _predict_glm(model, data, validation, batch_size)
    temperature = _temperature(val_logits, data.labels[validation], valid_validation)
    return model, history, temperature


def _compact_categories(augment_ids: list[int], path: Path) -> torch.Tensor:
    raw = json.loads(path.read_text(encoding="utf-8"))
    index = {name: idx for idx, name in enumerate(CATEGORY_ORDER)}
    matrix = np.zeros((len(augment_ids) + 1, len(CATEGORY_ORDER)), dtype=np.float32)
    for compact_id, raw_id in enumerate(augment_ids, start=1):
        for category in raw.get(str(raw_id), []):
            if category in index:
                matrix[compact_id, index[category]] = 1.0
    return torch.from_numpy(matrix)


class ResidualSequence(nn.Module):
    def __init__(self, sequence: AugmentSequenceNN) -> None:
        super().__init__()
        self.sequence = sequence
        final = self.sequence.mlp[-1]
        nn.init.zeros_(final.weight)
        nn.init.zeros_(final.bias)

    def forward(self, champions: torch.Tensor, augments: torch.Tensor) -> torch.Tensor:
        return self.sequence(champions, augments)


def _fit_residual(
    base: SparseAugmentLogit | None,
    *,
    data: DataBundle,
    categories: torch.Tensor,
    epochs: int,
    batch_size: int,
    label: str,
) -> tuple[ResidualSequence, list[dict[str, float]], float]:
    if base is not None:
        base.eval()
        for parameter in base.parameters():
            parameter.requires_grad_(False)
    model = ResidualSequence(
        AugmentSequenceNN(
            n_champions=len(data.champion_ids) + 1,
            n_augments=len(data.augment_ids) + 1,
            category_matrix=categories,
            use_history=True,
        )
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=3e-4)
    criterion = nn.BCEWithLogitsLoss(reduction="none")
    train = data.masks["train"]
    weights = np.ones(len(data.labels), dtype=np.float32)
    loader = DataLoader(_arrays(data, train, weights), batch_size=batch_size, shuffle=True)
    validation = data.masks["validation"]
    valid_validation = data.augments[validation] > 0
    best_state: dict[str, torch.Tensor] | None = None
    best_loss = float("inf")
    history: list[dict[str, float]] = []
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = total_n = 0.0
        for champions, roles, augments, labels, current_patch, _sample_weight in loader:
            optimizer.zero_grad(set_to_none=True)
            with torch.no_grad():
                base_logits = (
                    base(champions, roles, augments, current_patch)
                    if base is not None
                    else torch.zeros_like(augments, dtype=torch.float32)
                )
            logits = base_logits + model(champions, augments)
            valid = augments > 0
            losses = criterion(logits, labels[:, None].expand_as(logits))
            loss = losses[valid].mean()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach()) * int(valid.sum())
            total_n += int(valid.sum())
        val_logits = _predict_residual(base, model, data, validation, batch_size)
        val_probabilities = 1.0 / (1.0 + np.exp(-np.clip(val_logits, -30, 30)))
        row = {
            "epoch": epoch,
            "train_log_loss": total_loss / total_n,
            **_metrics(data.labels[validation], val_probabilities, valid_validation),
        }
        history.append(row)
        click.echo(
            f"  {label} epoch={epoch} train={row['train_log_loss']:.6f} "
            f"val={row['log_loss']:.6f} auc={row['auc']:.6f}"
        )
        if row["log_loss"] < best_loss - 1e-6:
            best_loss = row["log_loss"]
            best_state = copy.deepcopy(model.state_dict())
        elif epoch >= 4:
            break
    if best_state is None:
        raise click.ClickException(f"{label} did not train")
    model.load_state_dict(best_state)
    val_logits = _predict_residual(base, model, data, validation, batch_size)
    temperature = _temperature(val_logits, data.labels[validation], valid_validation)
    return model, history, temperature


@torch.no_grad()
def _predict_residual(
    base: SparseAugmentLogit | None,
    model: ResidualSequence,
    data: DataBundle,
    mask: np.ndarray,
    batch_size: int,
) -> np.ndarray:
    if base is not None:
        base.eval()
    model.eval()
    indices = np.flatnonzero(mask)
    current_all = data.masks["train"] | data.masks["validation"] | data.masks["test"]
    out: list[np.ndarray] = []
    for start in range(0, len(indices), batch_size):
        take = indices[start : start + batch_size]
        champions = torch.from_numpy(data.champions[take])
        roles = torch.from_numpy(data.roles[take])
        augments = torch.from_numpy(data.augments[take])
        current = torch.from_numpy(current_all[take])
        base_logits = (
            base(champions, roles, augments, current)
            if base is not None
            else torch.zeros_like(augments, dtype=torch.float32)
        )
        out.append((base_logits + model(champions, augments)).numpy())
    return np.concatenate(out)


def _probabilities(logits: np.ndarray, temperature: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(logits / temperature, -30, 30)))


def _stack_weights(logits: list[np.ndarray], labels: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    matrix = np.stack([value[valid] for value in logits], axis=1).astype(np.float64)
    target = np.broadcast_to(labels[:, None], logits[0].shape)[valid].astype(np.float64)

    def objective(params: np.ndarray) -> float:
        raw_weights = params[:-1]
        weights = np.exp(raw_weights - raw_weights.max())
        weights /= weights.sum()
        z = matrix @ weights + params[-1]
        return float(np.mean(np.logaddexp(0.0, z) - target * z))

    result = minimize(objective, np.zeros(len(logits) + 1), method="BFGS")
    raw = result.x[:-1]
    weights = np.exp(raw - raw.max())
    weights /= weights.sum()
    return {"weights": weights.tolist(), "bias": float(result.x[-1]), "loss": float(result.fun)}


@click.command()
@click.option(
    "--participants-parquet",
    type=click.Path(path_type=Path, exists=True),
    default=Path("data/analysis/augment_ablation_participants_16.14_16.15.parquet"),
    show_default=True,
)
@click.option("--current-patch", default="16.15", show_default=True)
@click.option("--previous-patch", default="16.14", show_default=True)
@click.option("--old-weight", type=float, default=0.25, show_default=True)
@click.option("--pair-prior", type=float, default=100.0, show_default=True)
@click.option("--champion-pair-min", type=float, default=50.0, show_default=True)
@click.option("--glm-epochs", type=int, default=6, show_default=True)
@click.option("--nn-epochs", type=int, default=8, show_default=True)
@click.option("--batch-size", type=int, default=16384, show_default=True)
@click.option("--seed", type=int, default=2400, show_default=True)
@click.option("--max-games", type=int, default=0, show_default=True)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("data/analysis/augment_ablation_16.15_full.json"),
    show_default=True,
)
@click.option(
    "--model-out",
    type=click.Path(path_type=Path),
    default=Path("data/models/augment_ablation_16.15_best.pt"),
    show_default=True,
)
def main(
    participants_parquet: Path,
    current_patch: str,
    previous_patch: str,
    old_weight: float,
    pair_prior: float,
    champion_pair_min: float,
    glm_epochs: int,
    nn_epochs: int,
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
    counts = {name: int(mask.sum()) for name, mask in data.masks.items()}
    click.echo(f"[augment-ablation] participants={len(data.labels):,} split={counts}")
    current_weights = np.ones(len(data.labels), dtype=np.float32)
    current_support = _pair_support(
        data,
        data.masks["train"],
        sample_weights=current_weights,
        pair_prior=pair_prior,
        champion_pair_min=champion_pair_min,
    )
    pooled_weights = np.ones(len(data.labels), dtype=np.float32)
    pooled_weights[data.masks["previous"]] = old_weight
    pooled_mask = data.masks["previous"] | data.masks["train"]
    pooled_support = _pair_support(
        data,
        pooled_mask,
        sample_weights=pooled_weights,
        pair_prior=pair_prior,
        champion_pair_min=champion_pair_min,
    )
    n_champions = len(data.champion_ids) + 1
    n_augments = len(data.augment_ids) + 1
    specifications = [
        ("lr_main", False, False, False),
        ("lr_global_pair", True, False, False),
        ("lr_role_pair", True, True, False),
        ("lr_champion_pair", True, True, True),
    ]
    fitted: dict[str, SparseAugmentLogit] = {}
    histories: dict[str, list[dict[str, float]]] = {}
    temperatures: dict[str, float] = {}
    for name, global_pairs, role_pairs, champion_pairs in specifications:
        click.echo(f"[{name}]")
        model = SparseAugmentLogit(
            n_champions=n_champions,
            n_augments=n_augments,
            n_roles=len(ROLE_ORDER),
            support=current_support,
            global_pairs=global_pairs,
            role_pairs=role_pairs,
            champion_pairs=champion_pairs,
            patch_delta=False,
        )
        model, history, temperature = _fit_glm(
            model,
            data=data,
            train_mask=data.masks["train"],
            weights=current_weights,
            epochs=glm_epochs,
            batch_size=batch_size,
            learning_rate=0.01,
            weight_decay=3e-4,
            label=name,
        )
        fitted[name] = model
        histories[name] = history
        temperatures[name] = temperature

    click.echo("[lr_partial_pool]")
    pooled_model = SparseAugmentLogit(
        n_champions=n_champions,
        n_augments=n_augments,
        n_roles=len(ROLE_ORDER),
        support=pooled_support,
        global_pairs=True,
        role_pairs=True,
        champion_pairs=True,
        patch_delta=True,
    )
    pooled_model, pooled_history, pooled_temperature = _fit_glm(
        pooled_model,
        data=data,
        train_mask=pooled_mask,
        weights=pooled_weights,
        epochs=glm_epochs,
        batch_size=batch_size,
        learning_rate=0.008,
        weight_decay=5e-4,
        label="lr_partial_pool",
    )
    fitted["lr_partial_pool"] = pooled_model
    histories["lr_partial_pool"] = pooled_history
    temperatures["lr_partial_pool"] = pooled_temperature

    click.echo("[sequence_nn]")
    sequence_model, sequence_history, sequence_temperature = _fit_residual(
        None,
        data=data,
        categories=_compact_categories(data.augment_ids, DEFAULT_CATEGORIES),
        epochs=nn_epochs,
        batch_size=batch_size,
        label="sequence_nn",
    )
    histories["sequence_nn"] = sequence_history
    temperatures["sequence_nn"] = sequence_temperature

    click.echo("[nn_residual]")
    residual_model, residual_history, residual_temperature = _fit_residual(
        pooled_model,
        data=data,
        categories=_compact_categories(data.augment_ids, DEFAULT_CATEGORIES),
        epochs=nn_epochs,
        batch_size=batch_size,
        label="nn_residual",
    )
    histories["nn_residual"] = residual_history
    temperatures["nn_residual"] = residual_temperature

    train_mask = data.masks["train"]
    tables = _baseline_tables(data.champions, data.labels, data.augments, train_mask)
    phase_predictions: dict[str, dict[str, np.ndarray]] = {}
    phase_logits: dict[str, dict[str, np.ndarray]] = {}
    metrics: dict[str, Any] = {}
    for phase in ("validation", "test"):
        mask = data.masks[phase]
        labels = data.labels[mask]
        augments = data.augments[mask]
        valid = augments > 0
        baseline = _baseline_predictions(tables, data.champions[mask], augments)
        predictions: dict[str, np.ndarray] = {
            "independent_odds": baseline["independent_odds"]
        }
        logits_by_name: dict[str, np.ndarray] = {
            "independent_odds": np.log(
                np.clip(baseline["independent_odds"], 1e-6, 1 - 1e-6)
                / np.clip(1 - baseline["independent_odds"], 1e-6, 1)
            )
        }
        for name, model in fitted.items():
            raw_logits = _predict_glm(model, data, mask, batch_size)
            calibrated = raw_logits / temperatures[name]
            logits_by_name[name] = calibrated
            predictions[name] = _probabilities(raw_logits, temperatures[name])
        residual_logits = _predict_residual(
            pooled_model, residual_model, data, mask, batch_size
        )
        logits_by_name["nn_residual"] = residual_logits / residual_temperature
        predictions["nn_residual"] = _probabilities(
            residual_logits, residual_temperature
        )
        sequence_logits = _predict_residual(
            None, sequence_model, data, mask, batch_size
        )
        logits_by_name["sequence_nn"] = sequence_logits / sequence_temperature
        predictions["sequence_nn"] = _probabilities(
            sequence_logits, sequence_temperature
        )
        phase_predictions[phase] = predictions
        phase_logits[phase] = logits_by_name
        phase_metrics = {
            name: _metrics(labels, probability, valid)
            for name, probability in predictions.items()
        }
        phase_metrics["by_slot"] = {
            str(slot + 1): {
                name: _metrics(
                    labels,
                    probability,
                    valid & (np.arange(4)[None, :] == slot),
                )
                for name, probability in predictions.items()
            }
            for slot in range(4)
        }
        phase_metrics["clustered_vs_independent_odds"] = {
            name: _clustered_logloss_delta(
                data.gidx[mask], labels, probability, predictions["independent_odds"], valid
            )
            for name, probability in predictions.items()
            if name != "independent_odds"
        }
        metrics[phase] = phase_metrics

    stack_names = [
        "independent_odds", "lr_partial_pool", "sequence_nn", "nn_residual"
    ]
    validation = data.masks["validation"]
    stack = _stack_weights(
        [phase_logits["validation"][name] for name in stack_names],
        data.labels[validation],
        data.augments[validation] > 0,
    )
    for phase in ("validation", "test"):
        mask = data.masks[phase]
        stacked_logits = sum(
            weight * phase_logits[phase][name]
            for name, weight in zip(stack_names, stack["weights"], strict=True)
        ) + stack["bias"]
        stacked = _probabilities(stacked_logits, 1.0)
        phase_predictions[phase]["stacked_ensemble"] = stacked
        metrics[phase]["stacked_ensemble"] = _metrics(
            data.labels[mask], stacked, data.augments[mask] > 0
        )
        metrics[phase]["clustered_vs_independent_odds"]["stacked_ensemble"] = (
            _clustered_logloss_delta(
                data.gidx[mask],
                data.labels[mask],
                stacked,
                phase_predictions[phase]["independent_odds"],
                data.augments[mask] > 0,
            )
        )

    result = {
        "schema_version": 1,
        "model": "augment_ablation_ladder_v1",
        "provenance": {
            "participants_parquet": str(participants_parquet.resolve()),
            "participants_sha256": _sha256(participants_parquet),
            "git": _git_state(),
        },
        "config": {
            "current_patch": current_patch,
            "previous_patch": previous_patch,
            "old_weight": old_weight,
            "pair_prior": pair_prior,
            "champion_pair_min": champion_pair_min,
            "glm_epochs": glm_epochs,
            "nn_epochs": nn_epochs,
            "batch_size": batch_size,
            "seed": seed,
            "max_games_per_patch": max_games,
        },
        "data": {
            "participants": len(data.labels),
            "split_participants": counts,
            "champions": len(data.champion_ids),
            "augments": len(data.augment_ids),
            "current_champion_pair_features": current_support.champion_features,
            "pooled_champion_pair_features": pooled_support.champion_features,
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
            "glm_state_dict": pooled_model.state_dict(),
            "residual_state_dict": residual_model.state_dict(),
            "sequence_state_dict": sequence_model.state_dict(),
            "champion_ids": data.champion_ids,
            "augment_ids": data.augment_ids,
            "temperatures": temperatures,
            "stack": result["stack"],
            "config": result["config"],
            "provenance": result["provenance"],
        },
        model_out,
    )
    click.echo(f"[augment-ablation] wrote {out} and {model_out}")
    for name, row in metrics["test"].items():
        if isinstance(row, dict) and "log_loss" in row:
            click.echo(
                f"  {name:<22} acc={row['accuracy']:.6f} auc={row['auc']:.6f} "
                f"ll={row['log_loss']:.6f} brier={row['brier']:.6f}"
            )


if __name__ == "__main__":
    main()
