from __future__ import annotations

import time
import traceback
from pathlib import Path

import click

from .model_refresh import (
    DEFAULT_GROWTH_RATIO,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_MIN_CURRENT_GAMES,
    DEFAULT_OUT_DIR,
    DEFAULT_PARQUET,
    DEFAULT_POOL_PATCHES,
    DEFAULT_STATE_PATH,
    refresh_models_once,
)


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--state", "state_path", type=click.Path(path_type=Path), default=DEFAULT_STATE_PATH, show_default=True)
@click.option("--out-dir", type=click.Path(path_type=Path), default=DEFAULT_OUT_DIR, show_default=True)
@click.option("--parquet", type=click.Path(path_type=Path), default=DEFAULT_PARQUET, show_default=True,
              help="Pooled parquet export path (regenerated each refresh).")
@click.option("--threshold", type=int, default=0, show_default=True,
              help="Minimum absolute current-patch growth before refreshing. 0 = ratio-only.")
@click.option("--growth-ratio", type=float, default=DEFAULT_GROWTH_RATIO, show_default=True,
              help="Refresh after this fractional current-patch growth since the last refresh.")
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patches", default="auto", show_default=True,
              help='Pool patches, e.g. "16.10,16.11,16.12". "auto" picks the latest N from the DB.')
@click.option("--current-patch", default="auto", show_default=True,
              help='"auto" detects the latest patch from the DB.')
@click.option("--pool-patches", type=int, default=DEFAULT_POOL_PATCHES, show_default=True,
              help="How many recent patches to pool when --patches=auto.")
@click.option("--half-life-days", type=float, default=DEFAULT_HALF_LIFE_DAYS, show_default=True)
@click.option("--min-current-games", type=int, default=DEFAULT_MIN_CURRENT_GAMES, show_default=True,
              help="Skip refresh until the current patch has this many games (holdout needs ~12k).")
@click.option("--force/--no-force", default=False, show_default=True)
@click.option("--check-only/--refresh", default=False, show_default=True,
              help="Only evaluate the gate; do not retrain.")
@click.option("--dry-run/--run", default=False, show_default=True,
              help="Print the pipeline commands instead of running them.")
@click.option("--watch/--once", default=False, show_default=True)
@click.option("--interval-sec", type=int, default=300, show_default=True)
def main(
    db: Path,
    state_path: Path,
    out_dir: Path,
    parquet: Path,
    threshold: int,
    growth_ratio: float,
    queue_id: int,
    patches: str,
    current_patch: str,
    pool_patches: int,
    half_life_days: float,
    min_current_games: int,
    force: bool,
    check_only: bool,
    dry_run: bool,
    watch: bool,
    interval_sec: int,
) -> None:
    """Growth-gated, per-patch refresh of the local recommender models."""
    while True:
        try:
            result = refresh_models_once(
                db=db, state_path=state_path, out_dir=out_dir, parquet=parquet,
                threshold=threshold, growth_ratio=growth_ratio, force=force,
                queue_id=queue_id, patches=patches, current_patch=current_patch,
                pool_patches=pool_patches, half_life_days=half_life_days,
                min_current_games=min_current_games, check_only=check_only,
                dry_run=dry_run,
            )
            if result.get("refreshed"):
                click.echo(
                    "[model-refresh] refreshed "
                    f"patch={result['current_patch']} pool={result.get('pool')} "
                    f"elapsed={result.get('elapsed_sec')}s local_total={result['local_total']} "
                    f"reason={result['reason']}"
                )
            elif check_only or dry_run:
                click.echo(
                    "[model-refresh] check "
                    f"would_refresh={'yes' if result.get('would_refresh') else 'no'} "
                    f"patch={result['current_patch']} pool={result.get('pool')} "
                    f"local_total={result['local_total']} "
                    f"last_refreshed_total={result['last_refreshed_total']} "
                    f"threshold={result['threshold']} reason={result['reason']}"
                )
                for cmd in result.get("commands", []):
                    click.echo(f"    $ {cmd}")
            else:
                click.echo(
                    "[model-refresh] skip "
                    f"patch={result['current_patch']} reason={result['reason']} "
                    f"local_total={result['local_total']} "
                    f"last_refreshed_total={result['last_refreshed_total']} "
                    f"threshold={result['threshold']}"
                )
        except Exception as exc:  # keep the watch daemon alive across transient failures
            click.echo(f"[model-refresh] error: {type(exc).__name__}: {exc}", err=True)
            if not watch:
                traceback.print_exc()
                raise SystemExit(1)
            traceback.print_exc()
        if not watch:
            break
        force = False
        time.sleep(max(5, interval_sec))


if __name__ == "__main__":
    main()
