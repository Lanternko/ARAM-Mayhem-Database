"""Scrape ARAM: Mayhem (queueId=2400) matches from high-tier Solo/Duo players.

Usage:
  set RIOT_API_KEY=...
  python -m aram_nn.ingest.cli --region kr --tiers CHALLENGER GRANDMASTER MASTER \
       --matches-per-puuid 50 --max-matches 5000 --out data/raw/kr_mayhem.parquet
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import polars as pl
from tqdm import tqdm

from .extract import MAYHEM_QUEUE_ID, extract_row
from .riot_client import RiotClient, RiotKeyExpired


def _collect_puuids(client: RiotClient, tiers: list[str]) -> list[str]:
    puuids: list[str] = []
    seen: set[str] = set()
    for tier in tiers:
        click.echo(f"[league-exp] fetching {tier}...")
        page = 1
        while True:
            entries = client.league_entries(tier, page=page)
            if not entries:
                break
            for e in entries:
                p = e.get("puuid")
                if p and p not in seen:
                    seen.add(p)
                    puuids.append(p)
            # CHALLENGER/GM are single-page in practice; MASTER paginates.
            if len(entries) < 200 or tier in ("CHALLENGER", "GRANDMASTER"):
                break
            page += 1
        click.echo(f"  total puuids so far: {len(puuids)}")
    return puuids


@click.command()
@click.option("--region", default="kr", show_default=True)
@click.option(
    "--tiers",
    multiple=True,
    default=("CHALLENGER", "GRANDMASTER", "MASTER"),
    show_default=True,
)
@click.option("--matches-per-puuid", default=50, show_default=True, type=int)
@click.option("--max-matches", default=5000, show_default=True, type=int,
              help="Stop after writing this many unique matches.")
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--raw-dir", default=None, type=click.Path(file_okay=False, path_type=Path),
              help="If set, also dump every raw match JSON here (debug/cache).")
def main(
    region: str,
    tiers: tuple[str, ...],
    matches_per_puuid: int,
    max_matches: int,
    out: Path,
    raw_dir: Path | None,
):
    out.parent.mkdir(parents=True, exist_ok=True)
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    seen_match_ids: set[str] = set()

    with RiotClient(region=region) as client:
        try:
            puuids = _collect_puuids(client, list(tiers))
        except RiotKeyExpired as e:
            click.echo(f"[fatal] {e}", err=True)
            sys.exit(2)
        click.echo(f"[league-exp] {len(puuids)} unique PUUIDs across {tiers}")

        # Phase 1: gather match ids
        all_match_ids: list[str] = []
        for puuid in tqdm(puuids, desc="match-ids"):
            try:
                ids = client.match_ids_by_puuid(puuid, queue=MAYHEM_QUEUE_ID, count=matches_per_puuid)
            except RiotKeyExpired as e:
                click.echo(f"[fatal] {e}", err=True)
                sys.exit(2)
            for mid in ids:
                if mid not in seen_match_ids:
                    seen_match_ids.add(mid)
                    all_match_ids.append(mid)
            if len(all_match_ids) >= max_matches:
                break
        click.echo(f"[match-ids] {len(all_match_ids)} unique mayhem matches queued")

        # Phase 2: fetch details
        target = min(len(all_match_ids), max_matches)
        for mid in tqdm(all_match_ids[:target], desc="match-details"):
            try:
                detail = client.match_detail(mid)
            except RiotKeyExpired as e:
                click.echo(f"[fatal] {e}", err=True)
                sys.exit(2)
            if not detail:
                continue
            if raw_dir:
                (raw_dir / f"{mid}.json").write_text(json.dumps(detail), encoding="utf-8")
            row = extract_row(detail)
            if row is not None:
                rows.append(row)

    click.echo(f"[done] {len(rows)} valid rows extracted from {len(seen_match_ids)} fetched matches")
    if not rows:
        click.echo("No rows — nothing to write.", err=True)
        sys.exit(1)

    df = pl.DataFrame(rows)
    df.write_parquet(out, compression="zstd")
    click.echo(f"[wrote] {out}  ({df.height} rows, {df.estimated_size('mb'):.1f} MB)")


if __name__ == "__main__":
    main()
