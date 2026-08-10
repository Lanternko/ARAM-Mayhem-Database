"""Compare naive, pick-slot, and roster-context augment win-rate models.

This is an analysis/backtest tool, not yet a production recommendation model.
It uses chronological game splits and participant-level selected-augment rows.
The result answers whether a chosen augment's observed WR varies by slot and
coarse ally/enemy role shape.  It cannot estimate causal uplift without the
offered-but-not-picked augment set.

Example:
    python scripts/analyze_augment_order.py --patch-prefix 16.15 \
        --out data/analysis/augment_order_16.15.json
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
import pyarrow.parquet as pq
from sklearn.metrics import log_loss, roc_auc_score

from aram_nn.augment_order import (
    AugmentObservation,
    iter_observations,
    load_role_map,
    smoothed_rate,
)


DEFAULT_DB = Path("data/lcu/games.db")
DEFAULT_ROLE_MAP = Path("data/cache/ddragon_champion_byid.json")
DEFAULT_PRIOR_GAMES = 100.0
DEFAULT_SLOT_PRIOR_GAMES = 150.0
DEFAULT_CONTEXT_PRIOR_GAMES = 250.0
MIN_CONTEXT_GAMES = 25


def _connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _where(queue_id: int, patch_prefix: str | None) -> tuple[str, list[Any]]:
    clauses = ["queue_id = ?"]
    params: list[Any] = [int(queue_id)]
    if patch_prefix:
        clauses.append("patch LIKE ?")
        params.append(f"{patch_prefix}%")
    return " AND ".join(clauses), params


def _patch_created_bounds(
    db: Path,
    *,
    queue_id: int,
    patch_prefix: str,
) -> tuple[int | None, int | None]:
    where, params = _where(queue_id, patch_prefix)
    con = _connect_ro(db)
    try:
        row = con.execute(
            f"SELECT MIN(created_ms), MAX(created_ms) FROM games WHERE {where}",
            params,
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        raise click.ClickException(
            f"no games found for queue={queue_id} patch prefix={patch_prefix}"
        )
    return int(row[0]), int(row[1])


def _split_keys(
    db: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
) -> tuple[int, tuple[int, str], tuple[int, str]]:
    """Return total rows and first validation/test keys in chronological order."""
    where, params = _where(queue_id, patch_prefix)
    con = _connect_ro(db)
    try:
        total = int(con.execute(f"SELECT COUNT(*) FROM games WHERE {where}", params).fetchone()[0])
        if total < 20:
            raise click.ClickException(f"not enough games for a split: {total}")
        train_idx = max(1, min(total - 2, int(total * 0.70)))
        test_idx = max(train_idx + 1, min(total - 1, int(total * 0.85)))
        cur = con.execute(
            f"SELECT created_ms, game_id FROM games WHERE {where} "
            "ORDER BY created_ms, game_id",
            params,
        )
        val_key: tuple[int, str] | None = None
        test_key: tuple[int, str] | None = None
        for idx, (created_ms, game_id) in enumerate(cur):
            key = (int(created_ms or 0), str(game_id))
            if idx == train_idx:
                val_key = key
            if idx == test_idx:
                test_key = key
                break
        if val_key is None or test_key is None:
            raise click.ClickException("could not resolve chronological split boundaries")
        return total, val_key, test_key
    finally:
        con.close()


def _iter_games_with_participants(
    db: Path,
    *,
    queue_id: int,
    patch_prefix: str | None,
):
    where, params = _where(queue_id, patch_prefix)
    con = _connect_ro(db)
    try:
        cur = con.execute(
            f"SELECT game_id, created_ms, blue_wins, blue_champs, red_champs, participants_json "
            f"FROM games WHERE {where}",
            params,
        )
        for game_id, created_ms, blue_wins, blue_raw, red_raw, participants_raw in cur:
            try:
                blue = json.loads(blue_raw or "[]")
                red = json.loads(red_raw or "[]")
                participants = json.loads(participants_raw or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(blue, list) or not isinstance(red, list):
                continue
            if not isinstance(participants, list) or len(participants) != 10:
                continue
            yield (
                (int(created_ms or 0), str(game_id)),
                int(bool(blue_wins)),
                blue,
                red,
                participants,
            )
    finally:
        con.close()


def _iter_parquet_games(
    parquet: Path,
    *,
    created_min: int | None = None,
    created_max: int | None = None,
):
    """Stream the extracted participant cache as one 5v5 game at a time.

    ``extract_participants.py`` intentionally omits private IDs and patch/game
    IDs.  Its original row order is nevertheless five blue participants
    followed by five red participants, and ``gidx`` is contiguous, so this is
    sufficient for an offline chronology backtest.  Do not sort by ``pid``:
    that is a global player index, not an in-game slot.  The optional timestamp
    bounds are used to select a patch window (the exact patch label remains in
    SQLite).
    """
    pf = pq.ParquetFile(parquet)
    columns = ["gidx", "created_ms", "pid", "champ", "win", "augments"]
    pending: list[dict[str, Any]] = []
    pending_gidx: int | None = None

    def emit(rows: list[dict[str, Any]]):
        if len(rows) != 10:
            return None
        created_ms = int(rows[0]["created_ms"] or 0)
        if created_min is not None and created_ms < created_min:
            return None
        if created_max is not None and created_ms > created_max:
            return None
        blue = [int(row["champ"]) for row in rows[:5]]
        red = [int(row["champ"]) for row in rows[5:]]
        participants = [
            {
                "teamId": 100 if idx < 5 else 200,
                "championId": int(row["champ"]),
                "augments": list(row.get("augments") or []),
            }
            for idx, row in enumerate(rows)
        ]
        return (
            (created_ms, f"{int(rows[0]['gidx']):012d}"),
            int(bool(rows[0]["win"])),
            blue,
            red,
            participants,
        )

    for batch in pf.iter_batches(batch_size=100_000, columns=columns):
        for row in batch.to_pylist():
            gidx = int(row["gidx"])
            if pending_gidx is None:
                pending_gidx = gidx
            if gidx != pending_gidx:
                item = emit(pending)
                if item is not None:
                    yield item
                pending = []
                pending_gidx = gidx
            pending.append(row)
    if pending:
        item = emit(pending)
        if item is not None:
            yield item


def _parquet_bounds(
    parquet: Path,
    *,
    created_min: int | None = None,
    created_max: int | None = None,
) -> tuple[int, tuple[int, str], tuple[int, str]]:
    """Find chronological split keys without materialising participant lists."""
    scan = pl.scan_parquet(parquet).select("gidx", "created_ms")
    if created_min is not None:
        scan = scan.filter(pl.col("created_ms") >= int(created_min))
    if created_max is not None:
        scan = scan.filter(pl.col("created_ms") <= int(created_max))
    summary = scan.select(
        pl.col("gidx").min().alias("first"),
        pl.col("gidx").max().alias("last"),
        pl.col("gidx").n_unique().alias("games"),
    ).collect().row(0)
    first, last, total = (int(summary[0]), int(summary[1]), int(summary[2]))
    if total < 20:
        raise click.ClickException(f"not enough parquet games for a split: {total}")
    train_idx = max(1, min(total - 2, int(total * 0.70)))
    test_idx = max(train_idx + 1, min(total - 1, int(total * 0.85)))
    targets = [first + train_idx, first + test_idx]
    target_scan = scan.filter(pl.col("gidx").is_in(targets)).unique("gidx")
    target_rows = target_scan.collect().sort("gidx").to_dicts()
    if len(target_rows) != 2:
        raise click.ClickException("could not resolve parquet chronological split boundaries")
    val_key = (int(target_rows[0]["created_ms"]), f"{targets[0]:012d}")
    test_key = (int(target_rows[1]["created_ms"]), f"{targets[1]:012d}")
    return total, val_key, test_key


class CountTables:
    def __init__(self) -> None:
        self.global_games = 0
        self.global_wins = 0
        self.champ_games: Counter[int] = Counter()
        self.champ_wins: Counter[int] = Counter()
        self.champ_aug_games: Counter[tuple[int, int]] = Counter()
        self.champ_aug_wins: Counter[tuple[int, int]] = Counter()
        self.slot_games: Counter[tuple[int, int, int]] = Counter()
        self.slot_wins: Counter[tuple[int, int, int]] = Counter()
        # Pool context across champions; a full champion×augment×slot×roster
        # cell is too sparse to learn on ARAM.
        self.context_aug_games: Counter[tuple[int, int, str]] = Counter()
        self.context_aug_wins: Counter[tuple[int, int, str]] = Counter()

    def add(self, observation: AugmentObservation) -> None:
        self.global_games += 1
        self.global_wins += observation.won
        champ = observation.champion_id
        augment = observation.augment_id
        slot_key = (champ, augment, observation.slot)
        self.champ_games[champ] += 1
        self.champ_wins[champ] += observation.won
        self.champ_aug_games[(champ, augment)] += 1
        self.champ_aug_wins[(champ, augment)] += observation.won
        self.slot_games[slot_key] += 1
        self.slot_wins[slot_key] += observation.won
        context_aug_key = (augment, observation.slot, observation.context_key)
        self.context_aug_games[context_aug_key] += 1
        self.context_aug_wins[context_aug_key] += observation.won

    def _champ_rate(self, champion: int) -> float:
        return smoothed_rate(
            self.champ_wins[champion],
            self.champ_games[champion],
            prior_rate=self.global_wins / self.global_games if self.global_games else 0.5,
            prior_games=DEFAULT_PRIOR_GAMES,
        )

    def naive_rate(self, champion: int, augment: int) -> float:
        champ_rate = self._champ_rate(champion)
        key = (champion, augment)
        return smoothed_rate(
            self.champ_aug_wins[key],
            self.champ_aug_games[key],
            prior_rate=champ_rate,
            prior_games=DEFAULT_PRIOR_GAMES,
        )

    def slot_rate(self, champion: int, augment: int, slot: int) -> float:
        naive = self.naive_rate(champion, augment)
        key = (champion, augment, slot)
        return smoothed_rate(
            self.slot_wins[key],
            self.slot_games[key],
            prior_rate=naive,
            prior_games=DEFAULT_SLOT_PRIOR_GAMES,
        )

    def context_rate(
        self,
        champion: int,
        augment: int,
        slot: int,
        context_key: str,
    ) -> tuple[float, bool]:
        slot_rate = self.slot_rate(champion, augment, slot)
        key = (augment, slot, context_key)
        n = self.context_aug_games[key]
        if n == 0:
            return slot_rate, False
        return (
            smoothed_rate(
                self.context_aug_wins[key],
                n,
                prior_rate=slot_rate,
                prior_games=DEFAULT_CONTEXT_PRIOR_GAMES,
            ),
            n >= MIN_CONTEXT_GAMES,
        )


def _metrics(labels: list[int], probabilities: list[float]) -> dict[str, float]:
    y = np.asarray(labels, dtype=np.float64)
    p = np.asarray(probabilities, dtype=np.float64)
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    try:
        auc = float(roc_auc_score(y, clipped))
    except ValueError:
        auc = 0.5
    bins = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    for idx in range(10):
        mask = (clipped >= bins[idx]) & (
            clipped < bins[idx + 1] if idx < 9 else clipped <= bins[idx + 1]
        )
        if not np.any(mask):
            continue
        ece += float(mask.mean()) * abs(float(clipped[mask].mean()) - float(y[mask].mean()))
    return {
        "observations": int(len(y)),
        "actual_win_rate": round(float(y.mean()), 6) if len(y) else 0.0,
        "mean_prediction": round(float(clipped.mean()), 6) if len(y) else 0.0,
        "accuracy": round(float(np.mean((clipped >= 0.5) == y)), 6) if len(y) else 0.0,
        "auc": round(auc, 6),
        "log_loss": round(float(log_loss(y, clipped, labels=[0.0, 1.0])), 6) if len(y) else 0.0,
        "brier": round(float(np.mean((clipped - y) ** 2)), 6) if len(y) else 0.0,
        "ece10": round(ece, 6),
    }


def _phase(key: tuple[int, str], val_key: tuple[int, str], test_key: tuple[int, str]) -> str:
    if key < val_key:
        return "train"
    if key < test_key:
        return "validation"
    return "test"


def run_analysis(
    *,
    db: Path,
    queue_id: int,
    patch_prefix: str | None,
    role_map: dict[int, str],
    participants_parquet: Path | None = None,
    created_min: int | None = None,
    created_max: int | None = None,
    min_slot_games: int = 50,
    top_n: int = 50,
) -> dict[str, Any]:
    if participants_parquet is None:
        total_games, val_key, test_key = _split_keys(
            db, queue_id=queue_id, patch_prefix=patch_prefix
        )
        iter_games = lambda: _iter_games_with_participants(
            db, queue_id=queue_id, patch_prefix=patch_prefix
        )
    else:
        total_games, val_key, test_key = _parquet_bounds(
            participants_parquet,
            created_min=created_min,
            created_max=created_max,
        )
        iter_games = lambda: _iter_parquet_games(
            participants_parquet,
            created_min=created_min,
            created_max=created_max,
        )
    counts = CountTables()
    phase_counts: Counter[str] = Counter()
    for key, blue_wins, blue, red, participants in iter_games():
        phase = _phase(key, val_key, test_key)
        phase_counts[phase] += 1
        if phase != "train":
            continue
        for observation in iter_observations(
            participants,
            blue_wins=blue_wins,
            blue_champions=blue,
            red_champions=red,
            role_map=role_map,
        ):
            counts.add(observation)

    labels_by_phase: dict[str, list[int]] = {"validation": [], "test": []}
    predictions: dict[str, dict[str, list[float]]] = {
        "validation": {"naive": [], "slot": [], "context": []},
        "test": {"naive": [], "slot": [], "context": []},
    }
    context_used: Counter[str] = Counter()
    for key, blue_wins, blue, red, participants in iter_games():
        phase = _phase(key, val_key, test_key)
        if phase == "train":
            continue
        for observation in iter_observations(
            participants,
            blue_wins=blue_wins,
            blue_champions=blue,
            red_champions=red,
            role_map=role_map,
        ):
            labels_by_phase[phase].append(observation.won)
            naive = counts.naive_rate(observation.champion_id, observation.augment_id)
            slot = counts.slot_rate(
                observation.champion_id,
                observation.augment_id,
                observation.slot,
            )
            context, used = counts.context_rate(
                observation.champion_id,
                observation.augment_id,
                observation.slot,
                observation.context_key,
            )
            predictions[phase]["naive"].append(naive)
            predictions[phase]["slot"].append(slot)
            predictions[phase]["context"].append(context)
            context_used[phase] += int(used)

    slot_rows: list[dict[str, Any]] = []
    for (champion, augment, slot), games in counts.slot_games.items():
        if games < min_slot_games:
            continue
        naive = counts.naive_rate(champion, augment)
        slot_rate = counts.slot_rate(champion, augment, slot)
        slot_rows.append(
            {
                "champion_id": champion,
                "augment_id": augment,
                "slot": slot,
                "games": games,
                "wins": counts.slot_wins[(champion, augment, slot)],
                "wr": round(slot_rate, 6),
                "naive_wr": round(naive, 6),
                "lift_vs_naive_pp": round((slot_rate - naive) * 100.0, 4),
            }
        )
    slot_rows.sort(key=lambda row: (-abs(row["lift_vs_naive_pp"]), -row["games"]))

    metrics: dict[str, dict[str, dict[str, float]]] = {}
    for phase in ("validation", "test"):
        metrics[phase] = {
            name: _metrics(labels_by_phase[phase], predictions[phase][name])
            for name in ("naive", "slot", "context")
        }

    return {
        "schema_version": 2,
        "model": "observed_augment_order_v2",
        "queue_id": queue_id,
        "patch_prefix": patch_prefix,
        "source": {
            "kind": "participants_parquet" if participants_parquet else "games_sqlite",
            "path": str(participants_parquet or db),
            "created_min": created_min,
            "created_max": created_max,
        },
        "split": {
            "method": "chronological_70_15_15_by_game",
            "total_games": total_games,
            "train_games": phase_counts["train"],
            "validation_games": phase_counts["validation"],
            "test_games": phase_counts["test"],
            "validation_start": list(val_key),
            "test_start": list(test_key),
        },
        "priors": {
            "champion_games": DEFAULT_PRIOR_GAMES,
            "champion_augment_games": DEFAULT_PRIOR_GAMES,
            "slot_games": DEFAULT_SLOT_PRIOR_GAMES,
            "context_games": DEFAULT_CONTEXT_PRIOR_GAMES,
        },
        "train_observations": counts.global_games,
        "metrics": metrics,
        "context_usage": dict(context_used),
        "slot_effects_top": slot_rows[: max(0, int(top_n))],
        "coverage": {
            "champion_augment_cells": len(counts.champ_aug_games),
            "slot_cells": len(counts.slot_games),
            "context_augment_cells": len(counts.context_aug_games),
            "context_augment_cells_min_games": sum(
                games >= MIN_CONTEXT_GAMES for games in counts.context_aug_games.values()
            ),
            "slot_cells_min_games": sum(
                games >= min_slot_games for games in counts.slot_games.values()
            ),
        },
    }


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=DEFAULT_DB, show_default=True)
@click.option(
    "--participants-parquet",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Use the extracted participant cache instead of parsing SQLite JSON.",
)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patch-prefix", default="auto", show_default=True)
@click.option(
    "--role-map",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROLE_MAP,
    show_default=True,
)
@click.option("--min-slot-games", type=int, default=50, show_default=True)
@click.option("--top-n", type=int, default=50, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=None)
def main(
    db: Path,
    participants_parquet: Path | None,
    queue_id: int,
    patch_prefix: str,
    role_map: Path,
    min_slot_games: int,
    top_n: int,
    out: Path | None,
) -> None:
    if patch_prefix.lower() == "auto":
        con = _connect_ro(db)
        try:
            latest = con.execute(
                "SELECT patch FROM games WHERE queue_id=? "
                "ORDER BY created_ms DESC, game_id DESC LIMIT 1",
                (queue_id,),
            ).fetchone()
        finally:
            con.close()
        if not latest or not latest[0]:
            raise click.ClickException(f"no games found for queue={queue_id}")
        patch_prefix = ".".join(str(latest[0]).split(".")[:2])
    created_min = created_max = None
    if participants_parquet is not None and patch_prefix:
        created_min, created_max = _patch_created_bounds(
            db, queue_id=queue_id, patch_prefix=patch_prefix
        )
    role_data = json.loads(role_map.read_text(encoding="utf-8"))
    result = run_analysis(
        db=db,
        queue_id=queue_id,
        patch_prefix=patch_prefix or None,
        role_map=load_role_map(role_data),
        participants_parquet=participants_parquet,
        created_min=created_min,
        created_max=created_max,
        min_slot_games=min_slot_games,
        top_n=top_n,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        click.echo(f"[augment-order] wrote {out}")
    click.echo(
        f"[augment-order] queue={queue_id} patch={patch_prefix or 'all'} "
        f"games={result['split']['total_games']:,} "
        f"train_observations={result['train_observations']:,}"
    )
    for phase, rows in result["metrics"].items():
        click.echo(f"[{phase}]")
        for name, metrics in rows.items():
            click.echo(
                f"  {name:<7} acc={metrics['accuracy']:.4f} "
                f"auc={metrics['auc']:.4f} ll={metrics['log_loss']:.5f} "
                f"brier={metrics['brier']:.5f} ece10={metrics['ece10']:.5f}"
            )


if __name__ == "__main__":
    main()
