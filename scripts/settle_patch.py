"""Freeze ("settle") a closed patch's aggregates so builds stop rescanning it.

A patch is settled the moment it stops being the current one -- flip day -- and
re-settled only after its game count grows past
``patch_snapshot.RESETTLE_GROWTH_RATIO`` (the LCU's decaying tail of stragglers;
see the module docstring there).  The tier-list build does this on demand, so
running this by hand is only useful to (a) settle right after a patch flip
instead of waiting for the next build, or (b) inspect / force the snapshots.

    python scripts/settle_patch.py --status
    python scripts/settle_patch.py                 # settle the patch before the current one
    python scripts/settle_patch.py --patch 16.13 --force
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tierlist_engine import (  # noqa: E402
    compute_settled_winrates,
    count_patch_games,
    load_champion_metadata,
    load_item_metadata,
    previous_patch_prefix,
    settled_core_item_patch_stats,
)

from aram_nn import patch_snapshot  # noqa: E402
from aram_nn.site.db import SITE_PATCH_MIN_GAMES, latest_patch_prefix  # noqa: E402


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patch", default="", help='Patch to settle; default = the one before the current patch.')
@click.option("--status", "status_only", is_flag=True, help="List stored snapshots and exit.")
@click.option("--force", is_flag=True, help="Re-settle even if the stored snapshot is still fresh.")
@click.option("--skip-items", is_flag=True, help="Only settle champion counters (no Data Dragon fetch).")
def main(db: Path, queue_id: int, patch: str, status_only: bool, force: bool, skip_items: bool) -> None:
    if status_only:
        rows = patch_snapshot.list_snapshots(queue_id=queue_id)
        if not rows:
            click.echo("[settle] no snapshots yet")
            return
        for row in rows:
            live = count_patch_games(db, queue_id, row["patch"])
            state = patch_snapshot.section_status(
                row["patch"], queue_id=queue_id, section=row["section"], live_games=live,
            )
            click.echo(
                f"[settle] {row['patch']:>6} {row['section']:<18} "
                f"settled {row['games_at_settle']:>8,} @ {row['settled_at'][:10]}  "
                f"live {live:>8,}  -> {state.state}"
            )
        return

    current = latest_patch_prefix(db, queue_id=queue_id, min_games=SITE_PATCH_MIN_GAMES)
    target = patch or previous_patch_prefix(current)
    if not target:
        raise SystemExit("[settle] could not resolve a patch to settle")
    if current and target == current:
        # Settling the live patch would freeze numbers that are still moving --
        # the whole point is that only closed patches are safe to freeze.
        raise SystemExit(f"[settle] refusing to settle {target}: it is the current patch")

    live = count_patch_games(db, queue_id, target)
    if not live:
        raise SystemExit(f"[settle] {target} has no games in queue {queue_id}")
    click.echo(f"[settle] target patch {target} ({live:,} games); current patch {current}")

    if force:
        path = patch_snapshot.snapshot_path(target, queue_id=queue_id)
        if path.exists():
            path.unlink()
            click.echo(f"[settle] removed {path} (--force)")

    compute_settled_winrates(db, queue_id, target, live_games=live, log=click.echo)

    if skip_items:
        click.echo("[settle] skipped core-item counters (--skip-items)")
    else:
        version, _ = load_champion_metadata(None)
        item_meta = load_item_metadata(cache_dir=Path("data/cache"), ddragon_version=version)
        click.echo(f"[settle] item catalogue: {len(item_meta)} entries (ddragon {version})")
        # champ_records only supply baseline terms that are re-derived per build,
        # so an empty list is fine here: the snapshot stores counters, not those.
        settled_core_item_patch_stats(
            db, queue_id, target, item_meta, [], live_games=live, log=click.echo
        )

    path = patch_snapshot.snapshot_path(target, queue_id=queue_id)
    size_mb = path.stat().st_size / 1e6 if path.exists() else 0.0
    sections = ", ".join(
        row["section"] for row in patch_snapshot.list_snapshots(queue_id=queue_id)
        if row["patch"] == target
    )
    click.echo(f"[settle] {target} settled -> {path} ({size_mb:.1f} MB; sections: {sections})")


if __name__ == "__main__":
    main()
