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

The build itself no longer waits for a settle that needs a pass over the source
DB: it publishes without the snapshot and starts ``--background`` here, which
runs ``--resumable --max-seconds N`` slices until the patch is settled (see
aram_nn.site.settle_background).  Progress is checkpointed per 6h window of
games in data/patch_snapshots/resume/, so a killed slice continues where it
stopped; the log is data/patch_snapshots/settle-background.log.

    python scripts/settle_patch.py --resumable --max-seconds 300   # one slice by hand
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import click

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tierlist_engine import (  # noqa: E402
    SNAPSHOT_CHAMP_SECTION,
    SNAPSHOT_DESTINY_SECTION,
    SNAPSHOT_ITEM_SECTION,
    compute_settled_winrates,
    count_patch_games,
    ddragon_version_for_patch,
    legendary_item_ids,
    load_champion_metadata,
    load_item_metadata,
    previous_patch_prefix,
    settled_champ_destiny_items,
    settled_core_item_patch_stats,
)

from aram_nn import patch_snapshot  # noqa: E402
from aram_nn.site import chunk_cache, settle_background  # noqa: E402
from aram_nn.site.db import SITE_PATCH_MIN_GAMES, latest_patch_prefix  # noqa: E402
from aram_nn.site.patch_extract import closed_patch_replica, register_lazy_scan_db  # noqa: E402

ALL_SECTIONS = (SNAPSHOT_CHAMP_SECTION, SNAPSHOT_ITEM_SECTION, SNAPSHOT_DESTINY_SECTION)
CACHE_DIR = Path("data/cache")


def parse_patch_spec(spec: str) -> tuple[str, tuple[str, ...]]:
    """``"16.22"`` -> every section; ``"16.21:champ_counters"`` -> just those."""
    patch, _, wanted = spec.partition(":")
    sections = tuple(s for s in (part.strip() for part in wanted.split(",")) if s) or ALL_SECTIONS
    unknown = [s for s in sections if s not in ALL_SECTIONS]
    if unknown:
        raise click.BadParameter(f"unknown section(s) {unknown}; choose from {list(ALL_SECTIONS)}")
    return patch.strip(), sections


def settle_one(
    db: Path,
    queue_id: int,
    target: str,
    sections: tuple[str, ...],
    live: int,
    *,
    snapshot_dir: Path | None = None,
    log=click.echo,
) -> None:
    """Settle *sections* of one closed patch (each is a no-op while still fresh)."""
    if SNAPSHOT_CHAMP_SECTION in sections:
        compute_settled_winrates(
            db, queue_id, target, live_games=live, snapshot_dir=snapshot_dir, log=log
        )
    if SNAPSHOT_ITEM_SECTION in sections:
        version, _ = load_champion_metadata(None)
        item_meta = load_item_metadata(cache_dir=CACHE_DIR, ddragon_version=version)
        log(f"[settle] item catalogue: {len(item_meta)} entries (ddragon {version})")
        # champ_records only supply baseline terms that are re-derived per build,
        # so an empty list is fine here: the snapshot stores counters, not those.
        settled_core_item_patch_stats(
            db, queue_id, target, item_meta, [], live_games=live,
            snapshot_dir=snapshot_dir, log=log,
        )
    if SNAPSHOT_DESTINY_SECTION in sections:
        # Same catalogue the build asks for: the patch's own Data Dragon version.
        version = ddragon_version_for_patch(target, cache_dir=CACHE_DIR)
        if version:
            settled_champ_destiny_items(
                db, queue_id, target, legendary_item_ids(version, cache_dir=CACHE_DIR),
                live_games=live, snapshot_dir=snapshot_dir, log=log,
            )
        else:
            log(f"[settle] {target}: no Data Dragon version; skipped destiny-item counters")


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patch", "patch_specs", multiple=True,
              help='Patch to settle (repeatable), optionally "PATCH:section,section"; '
                   "default = the one before the current patch.")
@click.option("--status", "status_only", is_flag=True, help="List stored snapshots and exit.")
@click.option("--force", is_flag=True, help="Re-settle even if the stored snapshot is still fresh.")
@click.option("--skip-items", is_flag=True, help="Only settle champion counters (no Data Dragon fetch).")
@click.option("--resumable", is_flag=True,
              help="Checkpoint source-DB scans per window so an interrupted run continues.")
@click.option("--max-seconds", type=float, default=0.0,
              help="With --resumable: stop after this long and exit 75 if work remains (0 = no limit).")
@click.option("--background", is_flag=True,
              help="Worker loop: run --resumable slices under the heavy-job lock until settled.")
def main(
    db: Path,
    queue_id: int,
    patch_specs: tuple[str, ...],
    status_only: bool,
    force: bool,
    skip_items: bool,
    resumable: bool,
    max_seconds: float,
    background: bool,
) -> None:
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

    if background:
        if not patch_specs:
            raise SystemExit("[settle] --background needs at least one --patch")
        for spec in patch_specs:
            parse_patch_spec(spec)

        def stamped(line: str) -> None:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", flush=True)

        stamped(f"[settle-bg] worker started for {', '.join(patch_specs)}")
        raise SystemExit(settle_background.run_worker(
            settle_background.slice_command(
                Path(__file__).resolve(), db=db, queue_id=queue_id, patches=patch_specs,
            ),
            cwd=Path.cwd(),
            log=stamped,
        ))

    current = latest_patch_prefix(db, queue_id=queue_id, min_games=SITE_PATCH_MIN_GAMES)
    targets = [parse_patch_spec(spec) for spec in patch_specs]
    if not targets:
        targets = [(previous_patch_prefix(current) or "", ALL_SECTIONS)]
    if skip_items:
        targets = [(patch, (SNAPSHOT_CHAMP_SECTION,)) for patch, _ in targets]
        click.echo("[settle] champion counters only (--skip-items)")
    deadline = time.monotonic() + max_seconds if resumable and max_seconds > 0 else None

    for target, sections in targets:
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

        if resumable:
            chunk_cache.configure_resume(
                settle_background.resume_store_path(queue_id, target), deadline=deadline
            )
            # A replica kept from when this patch was current beats the source DB.
            register_lazy_scan_db(
                db, queue_id, target,
                lambda target=target: closed_patch_replica(db, queue_id, target, log=click.echo),
            )
        try:
            settle_one(db, queue_id, target, sections, live)
        except chunk_cache.FoldInterrupted as exc:
            click.echo(f"[settle] {target}: paused, progress saved ({exc})")
            raise SystemExit(settle_background.EXIT_MORE_WORK)
        finally:
            chunk_cache.configure_resume(None)
        if resumable:
            settle_background.remove_resume_store(queue_id, target)

        path = patch_snapshot.snapshot_path(target, queue_id=queue_id)
        size_mb = path.stat().st_size / 1e6 if path.exists() else 0.0
        stored = ", ".join(
            row["section"] for row in patch_snapshot.list_snapshots(queue_id=queue_id)
            if row["patch"] == target
        )
        click.echo(f"[settle] {target} settled -> {path} ({size_mb:.1f} MB; sections: {stored})")


if __name__ == "__main__":
    main()
