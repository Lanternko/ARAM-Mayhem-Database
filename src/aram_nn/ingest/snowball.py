"""Snowball ingest: BFS from a seed Riot ID outward through co-participants.

Algorithm:
  frontier = deque([seed_puuid])
  visited_puuid = {seed_puuid}
  collected_matches = {}  # match_id -> row
  while collected_matches < target and frontier:
      puuid = frontier.popleft()
      match_ids = get last N queue=450 matches for puuid
      for mid in match_ids:
          if mid in collected_matches: continue
          detail = fetch detail
          row = extract_row(detail)
          if row passes filters (patch, duration, queue=450):
              collected_matches[mid] = row
              for participant in detail.info.participants:
                  if participant.puuid not in visited_puuid:
                      visited_puuid.add(participant.puuid)
                      frontier.append(participant.puuid)
"""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import click
import polars as pl
from tqdm import tqdm

from .extract import extract_row
from .riot_client import RiotClient, RiotKeyExpired

ARAM_QUEUE_ID = 450


def _passes_filters(row: dict, target_patch_prefix: str | None, min_duration: int, max_leaver_gap: int) -> bool:
    if row["queue_id"] != ARAM_QUEUE_ID:
        return False
    if row["duration_sec"] < min_duration:
        return False
    if row["max_leaver_gap_sec"] > max_leaver_gap:
        return False
    if target_patch_prefix:
        patch = row["patch"]
        parts = patch.split(".")
        prefix = ".".join(parts[:2]) if len(parts) >= 2 else patch
        if prefix != target_patch_prefix:
            return False
    return True


@click.command()
@click.option("--region", default="tw", show_default=True)
@click.option("--seed-riot-id", required=True, help='Format: "Name#TAG"')
@click.option("--target-matches", default=500, type=int, show_default=True,
              help="Stop after collecting this many passing matches.")
@click.option("--matches-per-puuid", default=30, type=int, show_default=True,
              help="How many recent queue=450 match ids to pull per PUUID.")
@click.option("--patch", default="", show_default=True,
              help='Patch prefix to keep (e.g. "16.9"). Omit or leave empty to keep all patches.')
@click.option("--min-duration", default=300, type=int, show_default=True)
@click.option("--max-leaver-gap", default=120, type=int, show_default=True,
              help="Reject matches where any player has timePlayed < duration - this many sec.")
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--checkpoint-every", default=200, type=int, show_default=True)
def main(
    region: str,
    seed_riot_id: str,
    target_matches: int,
    matches_per_puuid: int,
    patch: str,
    min_duration: int,
    max_leaver_gap: int,
    out: Path,
    checkpoint_every: int,
):
    if "#" not in seed_riot_id:
        click.echo("[fatal] --seed-riot-id must be 'Name#TAG'", err=True)
        sys.exit(1)
    name, tag = seed_riot_id.split("#", 1)
    name, tag = name.strip(), tag.strip()
    patch_prefix = patch or None

    out.parent.mkdir(parents=True, exist_ok=True)

    rows: dict[str, dict] = {}
    visited_puuid: set[str] = set()
    rejected_counts = {"queue": 0, "duration": 0, "leaver": 0, "patch": 0, "parse": 0}

    with RiotClient(region=region) as c:
        try:
            acct = c.account_by_riot_id(name, tag)
        except RiotKeyExpired as e:
            click.echo(f"[fatal] {e}", err=True)
            sys.exit(2)
        seed_puuid = acct.get("puuid")
        if not seed_puuid:
            click.echo(f"[fatal] could not resolve {seed_riot_id}: {acct}", err=True)
            sys.exit(1)
        click.echo(f"[seed] {acct.get('gameName')}#{acct.get('tagLine')}  puuid {seed_puuid[:12]}...")

        frontier: deque[str] = deque([seed_puuid])
        visited_puuid.add(seed_puuid)

        pbar = tqdm(total=target_matches, desc="matches")
        last_checkpoint = 0
        try:
            while frontier and len(rows) < target_matches:
                puuid = frontier.popleft()
                try:
                    mids = c.match_ids_by_puuid(puuid, queue=ARAM_QUEUE_ID, count=matches_per_puuid)
                except RiotKeyExpired as e:
                    click.echo(f"\n[fatal] {e}", err=True)
                    break

                new_puuids: set[str] = set()
                already_seen_mids: set[str] = set(rows.keys())
                for mid in mids:
                    if mid in already_seen_mids or len(rows) >= target_matches:
                        continue
                    already_seen_mids.add(mid)
                    try:
                        detail = c.match_detail(mid)
                    except RiotKeyExpired as e:
                        click.echo(f"\n[fatal] {e}", err=True)
                        frontier.clear()
                        break
                    if not detail:
                        continue

                    # Always harvest PUUIDs before any filter — this is how snowball expands
                    # even when this particular match doesn't pass the patch/duration filter.
                    for p in (detail.get("info") or {}).get("participants", []):
                        pp = p.get("puuid")
                        if pp and pp not in visited_puuid:
                            visited_puuid.add(pp)
                            new_puuids.add(pp)

                    row = extract_row(detail)
                    if row is None:
                        rejected_counts["parse"] += 1
                        continue
                    if row["queue_id"] != ARAM_QUEUE_ID:
                        rejected_counts["queue"] += 1
                        continue
                    if row["duration_sec"] < min_duration:
                        rejected_counts["duration"] += 1
                        continue
                    if row["max_leaver_gap_sec"] > max_leaver_gap:
                        rejected_counts["leaver"] += 1
                        continue
                    if patch_prefix:
                        parts = row["patch"].split(".")
                        prefix = ".".join(parts[:2]) if len(parts) >= 2 else row["patch"]
                        if prefix != patch_prefix:
                            rejected_counts["patch"] += 1
                            continue

                    rows[row["match_id"]] = row
                    pbar.update(1)

                    if len(rows) - last_checkpoint >= checkpoint_every:
                        _write(out, rows)
                        last_checkpoint = len(rows)

                # Randomize a bit to avoid pulling only from one social cluster
                frontier.extend(sorted(new_puuids))
        finally:
            pbar.close()

    click.echo(f"\n[summary] collected {len(rows)} matches")
    click.echo(f"          visited PUUIDs: {len(visited_puuid)}")
    click.echo(f"          frontier left: {len(frontier)}")
    click.echo(f"          rejected: {rejected_counts}")
    if not rows:
        click.echo("[fatal] no rows", err=True)
        sys.exit(1)
    _write(out, rows)


def _write(out: Path, rows: dict[str, dict]) -> None:
    df = pl.DataFrame(list(rows.values()))
    df.write_parquet(out, compression="zstd")
    size_mb = df.estimated_size("mb")
    click.echo(f"\n[wrote] {out}  ({df.height} rows, {size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
