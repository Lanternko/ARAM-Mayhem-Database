"""Train the four validation-only augment round-3 pilots and decide a verdict."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import click
import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.augment_residual import (
    CATEGORY_ORDER, PILOT_PATCHES, UNCERTAINTY_ENABLED, AugmentResidualNN,
    apply_strength_table, checkpoint_payload, clustered_paired_delta,
    cross_fit_strength, fit_residual_model, fit_strength_table, metric_values,
    predict_logits, probability, seed_everything, validation_verdict,
)


EXPECTED_COLUMNS = ("gidx", "created_ms", "patch", "champ", "win", "augments")
EXPECTED_SCHEMA = pa.schema([
    ("gidx", pa.int64()), ("created_ms", pa.int64()), ("patch", pa.string()),
    ("champ", pa.int64()), ("win", pa.int8()), ("augments", pa.list_(pa.int64())),
])
VALIDATION_PATCH = "16.15"
FIXED_CONFIG = {
    "previous_weight": 0.5, "folds": 5, "epochs": 8,
    "batch_size": 32768, "learning_rate": 0.001,
    "weight_decay": 0.0003, "seed": 2400,
}
CHECKPOINT_KEYS = frozenset({"tensor_state", "config", "category_order", "patch_labels"})
PINNED_CURRENT_SHA256 = "e08feb9b84080bbd144dab2d9508dc0f49e8fc6dc2f56a71917e174e8beac219"
DEV_META_KEYS = frozenset({
    "schema_version", "source_kind", "artifact_kind", "sources", "output",
    "queue", "patches", "games", "participants", "validation_start_source",
    "validation_start_materialized", "test_start_source", "split_rule",
    "source_sha256", "output_size", "output_sha256",
})


class Round3TrainError(RuntimeError):
    pass


def _fail(code: str) -> Round3TrainError:
    return Round3TrainError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(partial, path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise _fail("E_OUTPUT_WRITE") from None


def _output_root(path: Path, *, create: bool) -> Path:
    """Resolve a real pilots root; symlink roots are not an artifact boundary."""
    try:
        if path.is_symlink():
            raise _fail("E_OUTPUT_ROUTE")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise _fail("E_OUTPUT_ROUTE")
        return path.resolve(strict=True)
    except Round3TrainError:
        raise
    except (OSError, RuntimeError):
        raise _fail("E_OUTPUT_ROUTE") from None


def _pilot_output_dir(root: Path, pilot: str, *, create: bool) -> Path:
    """Return the one fixed, non-symlinked directory owned by a pilot."""
    if pilot not in PILOT_PATCHES:
        raise _fail("E_PILOT")
    resolved_root = _output_root(root, create=create)
    target = root / pilot.lower()
    try:
        if target.is_symlink():
            raise _fail("E_OUTPUT_ROUTE")
        if create:
            target.mkdir(exist_ok=True)
        if not target.is_dir():
            raise _fail("E_OUTPUT_ROUTE")
        resolved_target = target.resolve(strict=True)
        if resolved_target.parent != resolved_root:
            raise _fail("E_OUTPUT_ROUTE")
        return resolved_target
    except Round3TrainError:
        raise
    except (OSError, RuntimeError):
        raise _fail("E_OUTPUT_ROUTE") from None


def _load_validation_key(dev: Path) -> tuple[int, int]:
    meta_path = dev.with_suffix(dev.suffix + ".meta.json")
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        raise _fail("E_DEV_META") from None
    if set(raw) != DEV_META_KEYS:
        raise _fail("E_DEV_META")
    if (raw.get("artifact_kind") != "development"
            or raw.get("split_rule") != "16.15_key_lt_test_start_is_dev"
            or raw.get("queue") != 2400
            or raw.get("patches") != ["16.12", "16.13", "16.14", "16.15"]
            or raw.get("output") != dev.name
            or raw.get("output_size") != dev.stat().st_size
            or raw.get("output_sha256") != _sha256(dev)
            or PINNED_CURRENT_SHA256 not in set(raw.get("source_sha256", {}).values())):
        raise _fail("E_NOT_DEVELOPMENT_DATA")
    key = raw.get("validation_start_materialized")
    if not isinstance(key, list) or len(key) != 2:
        raise _fail("E_DEV_META")
    return int(key[0]), int(key[1])


def _load_raw(dev: Path, wanted: tuple[str, ...]) -> dict[str, np.ndarray]:
    parquet = pq.ParquetFile(dev)
    if parquet.schema_arrow != EXPECTED_SCHEMA:
        raise _fail("E_DEV_SCHEMA")
    expressions = [
        pl.col("gidx").cast(pl.Int64), pl.col("created_ms").cast(pl.Int64),
        pl.col("patch").cast(pl.String), pl.col("champ").cast(pl.Int64),
        pl.col("win").cast(pl.Float32),
    ] + [
        pl.col("augments").list.get(slot, null_on_oob=True).fill_null(0).cast(pl.Int64).alias(f"a{slot}")
        for slot in range(4)
    ]
    frame = (
        pl.scan_parquet(dev).filter(pl.col("patch").is_in(list(wanted)))
        .select(expressions).collect(engine="streaming")
    )
    if frame.is_empty():
        raise _fail("E_DEV_EMPTY")
    counts = frame.group_by("gidx").len().get_column("len")
    if counts.min() != 10 or counts.max() != 10:
        raise _fail("E_TEN_ROWS")
    return {
        "gidx": frame["gidx"].to_numpy().astype(np.int64),
        "created": frame["created_ms"].to_numpy().astype(np.int64),
        "patch": frame["patch"].to_numpy(),
        "champ_raw": frame["champ"].to_numpy().astype(np.int64),
        "labels": frame["win"].to_numpy().astype(np.float32),
        "augment_raw": np.column_stack([frame[f"a{slot}"].to_numpy() for slot in range(4)]).astype(np.int64),
    }


def _compact(raw: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], list[int], list[int]]:
    champion_ids = sorted(int(value) for value in np.unique(raw["champ_raw"]))
    augment_ids = sorted(int(value) for value in np.unique(raw["augment_raw"]) if int(value) > 0)
    champion_map = {value: index + 1 for index, value in enumerate(champion_ids)}
    augment_map = {value: index + 1 for index, value in enumerate(augment_ids)}
    champions = np.asarray([champion_map[int(value)] for value in raw["champ_raw"]], dtype=np.int64)
    augments = np.zeros_like(raw["augment_raw"], dtype=np.int64)
    for raw_id, compact_id in augment_map.items():
        augments[raw["augment_raw"] == raw_id] = compact_id
    return {**raw, "champions": champions, "augments": augments}, champion_ids, augment_ids


def _masks(data: dict[str, np.ndarray], validation_key: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    created, gidx = data["created"], data["gidx"]
    current = data["patch"] == VALIDATION_PATCH
    before_validation = (created < validation_key[0]) | ((created == validation_key[0]) & (gidx < validation_key[1]))
    train = (~current) | (current & before_validation)
    validation = current & ~before_validation
    if not train.any() or not validation.any():
        raise _fail("E_SPLIT_EMPTY")
    return train, validation


def _categories(path: Path, augment_ids: list[int]) -> torch.Tensor:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise _fail("E_CATEGORIES") from None
    index = {name: idx for idx, name in enumerate(CATEGORY_ORDER)}
    matrix = np.zeros((len(augment_ids) + 1, len(CATEGORY_ORDER)), dtype=np.float32)
    for compact_id, raw_id in enumerate(augment_ids, start=1):
        for category in raw.get(str(raw_id), []):
            if category in index:
                matrix[compact_id, index[category]] = 1.0
    return torch.from_numpy(matrix)


def _features(
    data: dict[str, np.ndarray], train: np.ndarray, validation: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    base, uncertainty = cross_fit_strength(
        data["champions"], data["labels"], data["augments"], data["gidx"],
        data["patch"], train, folds=FIXED_CONFIG["folds"],
    )
    current_train = train & (data["patch"] == VALIDATION_PATCH)
    table = fit_strength_table(data["champions"], data["labels"], data["augments"], current_train)
    validation_base, validation_uncertainty = apply_strength_table(
        table, data["champions"][validation], data["augments"][validation]
    )
    return base[train], uncertainty[train], validation_base, validation_uncertainty


def train_pilot(dev: Path, categories: Path, output_dir: Path, pilot: str) -> dict[str, Any]:
    if pilot not in PILOT_PATCHES:
        raise _fail("E_PILOT")
    pilot_output = _pilot_output_dir(output_dir, pilot, create=True)
    validation_key = _load_validation_key(dev)
    raw = _load_raw(dev, PILOT_PATCHES[pilot])
    data, champion_ids, augment_ids = _compact(raw)
    train, validation = _masks(data, validation_key)
    seed_everything(FIXED_CONFIG["seed"])
    train_base, train_uncertainty, val_base, val_uncertainty = _features(data, train, validation)
    model = AugmentResidualNN(
        n_champions=len(champion_ids) + 1, n_augments=len(augment_ids) + 1,
        category_matrix=_categories(categories, augment_ids),
        use_uncertainty=UNCERTAINTY_ENABLED[pilot],
    )
    weights = np.ones(int(train.sum()), dtype=np.float32)
    weights[data["patch"][train] != VALIDATION_PATCH] = FIXED_CONFIG["previous_weight"]
    best_state, history, temperature = fit_residual_model(
        model, champions=data["champions"][train], augments=data["augments"][train],
        labels=data["labels"][train], base_logits=train_base,
        uncertainty=train_uncertainty, sample_weights=weights,
        validation=(data["champions"][validation], data["augments"][validation],
                    data["labels"][validation], val_base, val_uncertainty),
        epochs=FIXED_CONFIG["epochs"], batch_size=FIXED_CONFIG["batch_size"],
    )
    logits = predict_logits(
        model, data["champions"][validation], data["augments"][validation],
        val_base, val_uncertainty, batch_size=FIXED_CONFIG["batch_size"],
    )
    metrics = metric_values(data["labels"][validation], probability(logits, temperature), data["augments"][validation] > 0)
    config = {
        **FIXED_CONFIG, "pilot": pilot, "patches": list(PILOT_PATCHES[pilot]),
        "uncertainty_enabled": UNCERTAINTY_ENABLED[pilot],
        "champion_ids": champion_ids, "augment_ids": augment_ids,
        "temperature": temperature, "validation_start": list(validation_key),
        "dev_sha256": _sha256(dev), "categories_sha256": _sha256(categories),
    }
    payload = checkpoint_payload(best_state, config, PILOT_PATCHES[pilot])
    checkpoint = pilot_output / f"{pilot}.pt"
    partial = checkpoint.with_suffix(checkpoint.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        torch.save(payload, partial); os.replace(partial, checkpoint)
    except Exception:
        partial.unlink(missing_ok=True); raise _fail("E_OUTPUT_WRITE") from None
    result = {
        "schema_version": 1, "pilot": pilot, "scope": "validation_only",
        "config": {key: config[key] for key in (
            "previous_weight", "folds", "epochs", "batch_size", "learning_rate",
            "weight_decay", "seed", "patches", "uncertainty_enabled")},
        "split": {"train_participants": int(train.sum()), "validation_participants": int(validation.sum())},
        "temperature": temperature, "history": history, "metrics": metrics,
        "checkpoint_sha256": _sha256(checkpoint),
    }
    _atomic_json(pilot_output / f"{pilot}.json", result)
    _atomic_json(pilot_output / f"{pilot}.log.json", {
        "schema_version": 1, "pilot": pilot, "events": [
            "DEV_CONTRACT_OK", "PATCH_LOCAL_OOF_COMPLETE", "VALIDATION_SELECTED", "OUTPUTS_WRITTEN"
        ],
    })
    return result


def _load_checkpoint(output_dir: Path, pilot: str) -> dict[str, Any]:
    pilot_output = _pilot_output_dir(output_dir, pilot, create=False)
    try:
        saved = torch.load(pilot_output / f"{pilot}.pt", map_location="cpu", weights_only=True)
    except Exception:
        raise _fail("E_CHECKPOINT_READ") from None
    if not isinstance(saved, dict) or set(saved) != CHECKPOINT_KEYS:
        raise _fail("E_CHECKPOINT_CONTRACT")
    if tuple(saved["category_order"]) != CATEGORY_ORDER or tuple(saved["patch_labels"]) != PILOT_PATCHES[pilot]:
        raise _fail("E_CHECKPOINT_CONTRACT")
    return saved


def _evaluate_checkpoint(dev: Path, categories: Path, saved: dict[str, Any], pilot: str) -> tuple[dict[str, Any], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    config = saved["config"]
    if config.get("pilot") != pilot or config.get("dev_sha256") != _sha256(dev) or config.get("categories_sha256") != _sha256(categories):
        raise _fail("E_CHECKPOINT_INPUT")
    validation_key = _load_validation_key(dev)
    raw = _load_raw(dev, PILOT_PATCHES[pilot])
    data, champion_ids, augment_ids = _compact(raw)
    if champion_ids != config.get("champion_ids") or augment_ids != config.get("augment_ids"):
        raise _fail("E_CHECKPOINT_MAPPING")
    train, validation = _masks(data, validation_key)
    current_train = train & (data["patch"] == VALIDATION_PATCH)
    table = fit_strength_table(data["champions"], data["labels"], data["augments"], current_train)
    base, uncertainty = apply_strength_table(table, data["champions"][validation], data["augments"][validation])
    model = AugmentResidualNN(
        n_champions=len(champion_ids) + 1, n_augments=len(augment_ids) + 1,
        category_matrix=_categories(categories, augment_ids),
        use_uncertainty=bool(config["uncertainty_enabled"]),
    )
    model.load_state_dict(saved["tensor_state"], strict=True)
    probabilities = probability(predict_logits(
        model, data["champions"][validation], data["augments"][validation],
        base, uncertainty, batch_size=FIXED_CONFIG["batch_size"],
    ), float(config["temperature"]))
    valid = data["augments"][validation] > 0
    metrics = metric_values(data["labels"][validation], probabilities, valid)
    return metrics, data["gidx"][validation], data["labels"][validation], probabilities, valid


def decide(dev: Path, categories: Path, output_dir: Path) -> dict[str, Any]:
    resolved_root = _output_root(output_dir, create=False)
    evaluated: dict[str, tuple[dict[str, Any], np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for pilot in PILOT_PATCHES:
        evaluated[pilot] = _evaluate_checkpoint(dev, categories, _load_checkpoint(output_dir, pilot), pilot)
    base_metrics, base_games, base_labels, base_predictions, base_valid = evaluated["P0"]
    rows: dict[str, dict[str, Any]] = {"P0": {
        key: float(base_metrics[key]) for key in ("accuracy", "auc", "logloss", "brier", "ece")
    }}
    rows["P0"]["paired_delta"] = 0.0
    rows["P0"]["per_slot"] = base_metrics["per_slot"]
    for pilot in ("P1", "P2", "P3"):
        metrics, games, labels, predictions, valid = evaluated[pilot]
        if (not np.array_equal(games, base_games) or not np.array_equal(labels, base_labels)
                or not np.array_equal(valid, base_valid)):
            raise _fail("E_VALIDATION_MISMATCH")
        rows[pilot] = {key: float(metrics[key]) for key in ("accuracy", "auc", "logloss", "brier", "ece")}
        rows[pilot]["per_slot"] = metrics["per_slot"]
        rows[pilot]["paired_delta"] = clustered_paired_delta(
            games, labels, predictions, base_predictions,
            valid,
        )
    outcome = validation_verdict(rows)
    result = {"schema_version": 1, "scope": "validation_only", "pilots": rows, **outcome}
    _atomic_json(resolved_root / "verdict.json", result)
    _atomic_json(resolved_root / "verdict.log.json", {
        "schema_version": 1, "events": ["VALIDATION_CHECKPOINTS_REPLAYED", outcome["verdict"]]
    })
    return result


@click.group()
def main() -> None:
    """Validation-only round-3 pilot runner."""


@main.command("pilot")
@click.option("--dev-parquet", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--categories", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--output-dir", required=True, type=click.Path(path_type=Path, file_okay=False))
@click.option("--pilot", required=True, type=click.Choice(tuple(PILOT_PATCHES), case_sensitive=True))
def pilot_command(dev_parquet: Path, categories: Path, output_dir: Path, pilot: str) -> None:
    try:
        result = train_pilot(dev_parquet, categories, output_dir, pilot)
        click.echo(json.dumps({"pilot": pilot, "metrics": result["metrics"]}, sort_keys=True))
    except Round3TrainError as error:
        raise click.ClickException(str(error)) from None
    except Exception:
        raise click.ClickException("E_TRAIN_FAILED") from None


@main.command("verdict")
@click.option("--dev-parquet", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--categories", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.option("--output-dir", required=True, type=click.Path(path_type=Path, file_okay=False))
def verdict_command(dev_parquet: Path, categories: Path, output_dir: Path) -> None:
    try:
        click.echo(json.dumps(decide(dev_parquet, categories, output_dir), sort_keys=True))
    except Round3TrainError as error:
        raise click.ClickException(str(error)) from None
    except Exception:
        raise click.ClickException("E_VERDICT_FAILED") from None


if __name__ == "__main__":
    main()
