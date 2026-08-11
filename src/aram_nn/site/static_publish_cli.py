from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO

import click

from .db import SITE_PATCH_MIN_GAMES, latest_patch_prefix
from .static_publish import DEFAULT_SITE_URL, DEFAULT_STATE_PATH, publish_static_site_once


def acquire_publisher_lock(state_path: Path) -> BinaryIO:
    """Hold a process-scoped lock so manual and watchdog builds cannot overlap."""

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists() or lock_path.stat().st_size == 0:
        lock_path.write_bytes(b"0")
    handle = lock_path.open("a+b")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"another static publisher already holds {lock_path}"
        ) from exc
    return handle


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--state", "state_path", type=click.Path(path_type=Path), default=DEFAULT_STATE_PATH, show_default=True)
@click.option("--threshold", type=int, default=0, show_default=True, help="Minimum absolute growth before publishing. 0 = ratio-only.")
@click.option("--growth-ratio", type=float, default=0.10, show_default=True, help="Publish after this fractional growth since the previous publish.")
@click.option(
    "--max-age-hours",
    type=float,
    default=0.0,
    show_default=True,
    help="Publish after this many hours even below the growth threshold. 0 disables the age fallback.",
)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patch-prefix", default="auto", show_default=True, help='"auto" detects the latest patch from the DB.')
@click.option(
    "--auto-patch-min-games",
    type=int,
    default=SITE_PATCH_MIN_GAMES,
    show_default=True,
    help="In auto mode, keep the newest mature patch until a newer patch reaches this many games.",
)
@click.option("--site-url", default=DEFAULT_SITE_URL, show_default=True)
@click.option("--branch", default="main", show_default=True)
@click.option("--force/--no-force", default=False, show_default=True)
@click.option("--check-only/--build", default=False, show_default=True, help="Only evaluate the publish threshold; do not rebuild files.")
@click.option("--dry-run/--publish", default=False, show_default=True)
@click.option(
    "--isolated-worktree/--main-worktree",
    default=True,
    show_default=True,
    help="Build and commit in a disposable worktree so local WIP cannot block or be overwritten.",
)
@click.option("--watch/--once", default=False, show_default=True)
@click.option("--interval-sec", type=int, default=300, show_default=True)
def main(
    db: Path,
    state_path: Path,
    threshold: int,
    growth_ratio: float,
    max_age_hours: float,
    queue_id: int,
    patch_prefix: str,
    auto_patch_min_games: int,
    site_url: str,
    branch: str,
    force: bool,
    check_only: bool,
    dry_run: bool,
    isolated_worktree: bool,
    watch: bool,
    interval_sec: int,
) -> None:
    """Rebuild, commit, and push the GitHub Pages static tier-list outputs."""
    # Keep the handle alive for the entire one-shot/watch process. The OS releases
    # the advisory lock even after a crash or forced termination.
    publisher_lock = acquire_publisher_lock(state_path.resolve())
    # Re-resolve "auto" on every watch iteration, not once at launch: a long-running
    # publisher otherwise stays pinned to whatever patch was newest when it started
    # and never flips to a newer patch until the process is restarted.
    auto_patch = patch_prefix == "auto"
    last_resolved: str | None = None
    while True:
        if auto_patch:
            resolved = latest_patch_prefix(
                db,
                queue_id=queue_id,
                min_games=max(1, auto_patch_min_games),
                fallback_latest=False,
            )
            if not resolved:
                click.echo(
                    "[static-site] error: no patch meets the auto-publish maturity floor "
                    f"({max(1, auto_patch_min_games):,} games)",
                    err=True,
                )
                if not watch:
                    raise SystemExit(1)
                # Transient empty / mid-write DB in watch mode: wait and retry rather
                # than killing the long-running publisher.
                time.sleep(max(5, interval_sec))
                continue
            if resolved != last_resolved:
                click.echo(f"[static-site] auto-detected patch prefix: {resolved}")
                last_resolved = resolved
            effective_patch = resolved
        else:
            effective_patch = patch_prefix or None
        try:
            result = publish_static_site_once(
                db=db,
                state_path=state_path,
                threshold=threshold,
                growth_ratio=growth_ratio,
                max_age_hours=max_age_hours,
                force=force,
                queue_id=queue_id,
                patch_prefix=effective_patch,
                site_url=site_url,
                branch=branch,
                check_only=check_only,
                dry_run=dry_run,
                isolated_worktree=isolated_worktree,
            )
        except Exception as exc:
            if not watch:
                raise
            click.echo(f"[static-site] blocked: {exc}", err=True)
            force = False
            time.sleep(max(5, interval_sec))
            continue
        if check_only:
            click.echo(
                "[static-site] check "
                f"would_publish={'yes' if result.get('would_publish') else 'no'} "
                f"reason={result['reason']} local_total={result['local_total']} "
                f"last_published_total={result['last_published_total']} "
                f"threshold={result['threshold']}"
            )
        elif result["published"]:
            click.echo(
                "[static-site] published "
                f"commit={result['commit']} local_total={result['local_total']} "
                f"threshold={result['threshold']} reason={result['reason']}"
            )
        else:
            click.echo(
                "[static-site] skip "
                f"reason={result['reason']} local_total={result['local_total']} "
                f"last_published_total={result['last_published_total']} "
                f"threshold={result['threshold']}"
            )
        if not watch:
            break
        force = False
        time.sleep(max(5, interval_sec))

    publisher_lock.close()


if __name__ == "__main__":
    main()
