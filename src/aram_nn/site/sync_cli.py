from __future__ import annotations

import os
import time
from pathlib import Path

import click

from .sync import push_public_games


@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"), show_default=True)
@click.option("--api-url", required=True, help="Backend root URL, e.g. https://example.com/api or http://127.0.0.1:8000")
@click.option("--state", "state_path", type=click.Path(path_type=Path), default=Path("data/site/sync_state.json"), show_default=True)
@click.option("--threshold", type=int, default=0, show_default=True, help="Minimum absolute growth before upload. 0 = ratio-only.")
@click.option("--growth-ratio", type=float, default=0.10, show_default=True, help="Upload after this fractional growth since the previous upload.")
@click.option("--batch-size", type=int, default=1000, show_default=True)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patch-prefix", default="", help="Optional patch prefix filter. Empty = all patches.")
@click.option("--token", envvar="ARAM_SITE_ADMIN_TOKEN", default="")
@click.option("--force/--no-force", default=False, show_default=True)
@click.option("--watch/--once", default=False, show_default=True)
@click.option("--interval-sec", type=int, default=300, show_default=True)
def main(
    db: Path,
    api_url: str,
    state_path: Path,
    threshold: int,
    growth_ratio: float,
    batch_size: int,
    queue_id: int,
    patch_prefix: str,
    token: str,
    force: bool,
    watch: bool,
    interval_sec: int,
) -> None:
    """Push public game rows to the website backend at growth watermarks."""
    if token:
        os.environ["ARAM_SITE_ADMIN_TOKEN"] = token

    while True:
        result = push_public_games(
            db=db,
            api_url=api_url,
            state_path=state_path,
            threshold=threshold,
            growth_ratio=growth_ratio,
            batch_size=batch_size,
            force=force,
            queue_id=queue_id,
            patch_prefix=patch_prefix or None,
            token=token,
        )
        if result["pushed"]:
            click.echo(
                "[sync-site] pushed "
                f"received={result['received']} inserted={result['inserted']} "
                f"skipped={result['skipped']} local_total={result['local_total']} "
                f"threshold={result['threshold']}"
            )
        else:
            click.echo(
                f"[sync-site] skip  reason={result['reason']} "
                f"local_total={result['local_total']} "
                f"last_uploaded_total={result['last_uploaded_total']} "
                f"threshold={result['threshold']}"
            )
        if not watch:
            break
        force = False
        time.sleep(max(5, interval_sec))


if __name__ == "__main__":
    main()
