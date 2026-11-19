"""Export a multi-patch Mayhem parquet from games.db for cross-patch training.

The existing ml_compare parquets are all 16.10-era and the built-in
`lcu_collector export` drops created_ms / duration_sec (which the training
pipeline requires).  This writes the exact schema load_split_data +
train_frame_for_empirical_scores expect, pooling recent patches.

Uses the PUBLIC participants_json only (championId/teamId/augments/items/stats)
— never participants_private_json — so no PUUID/name leaks.

  python scripts/export_pooled_parquet.py --out data/raw/mayhem_pooled_16_10_12.parquet
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import click
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from aram_nn.parquet_parts import PartStore

SCHEMA = {
    "match_id": pl.String, "patch": pl.String, "queue_id": pl.Int64,
    "platform": pl.String, "duration_sec": pl.Int64,
    "blue_champions": pl.List(pl.Int64), "red_champions": pl.List(pl.Int64),
    "blue_wins": pl.Boolean, "game_creation_ms": pl.Int64, "game_end_ms": pl.Int64,
    "max_leaver_gap_sec": pl.Int64, "participants_json": pl.String,
}


_BASE_WHERE = (
    "queue_id=2400 AND blue_champs IS NOT NULL AND red_champs IS NOT NULL "
    "AND blue_wins IS NOT NULL AND participants_json IS NOT NULL"
)


def _cap_cutoff(con, prefix, cap):
    """created_ms of the cap-th newest game of one patch, or None if fewer.

    Applying the cap as ORDER BY created_ms DESC LIMIT N on the full row made
    sqlite carry each row's 11KB participants_json through its sorter: a 2.1GB
    temp file, and the bulk of the export's runtime.  Sorting the timestamp by
    itself covers the same rows with 8-byte keys, and -- because the column
    lives in idx_games_queue_patch_created -- without faulting in the JSON's
    overflow pages at all.
    """
    row = con.execute(
        f"SELECT created_ms FROM games WHERE {_BASE_WHERE} AND patch LIKE ? "
        "ORDER BY created_ms DESC LIMIT 1 OFFSET ?",
        (prefix + ".%", max(0, cap - 1)),
    ).fetchone()
    return row[0] if row else None


def _iter_rows(con, prefix, *, cutoff=None, after=None, chunk=2000):
    """Stream one patch's rows in (patch, created_ms, rowid) order.

    participants_json averages ~11KB, so a three-patch pool is ~8GB of JSON
    before any Python object overhead.  fetchall() held that once as rows,
    again as dicts, and a third time while polars built the frame; the 16.17
    refresh died on `memory allocation of 16777216 bytes failed` after seven
    consecutive attempts.  Reading in chunks keeps only one batch live.

    The order is the one idx_games_queue_patch_created already yields, spelled
    out so that `after` -- the key of the last row a previous run kept -- can
    seek straight to where that run stopped.  The patch range is `LIKE
    'prefix.%'` written as bounds the index can use.
    """
    where = "patch >= ? AND patch < ?"
    params = [prefix + ".", prefix + "/"]
    if cutoff is not None:
        where += " AND created_ms >= ?"
        params.append(cutoff)
    if after is not None:
        where += " AND (patch, created_ms, rowid) > (?, ?, ?)"
        params.extend(after)
    cur = con.execute(
        "SELECT game_id, patch, queue_id, duration_sec, blue_champs, red_champs, "
        "blue_wins, created_ms, participants_json, rowid FROM games "
        f"WHERE {_BASE_WHERE} AND {where} ORDER BY patch, created_ms, rowid",
        params,
    )
    cur.arraysize = chunk
    try:
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                return
            yield from rows
    finally:
        cur.close()


def _record(row):
    game_id, patch, queue_id, duration_sec, b, rd, bw, created_ms, pj = row
    try:
        blue = [int(x) for x in json.loads(b)]
        red = [int(x) for x in json.loads(rd)]
    except Exception:
        return None
    if len(blue) != 5 or len(red) != 5:
        return None
    dur = int(duration_sec or 0)
    cm = int(created_ms or 0)
    return {
        "match_id": str(game_id),
        "patch": patch,
        "queue_id": int(queue_id),
        "platform": "",
        "duration_sec": dur,
        "blue_champions": blue,
        "red_champions": red,
        "blue_wins": bool(bw),
        "game_creation_ms": cm,
        "game_end_ms": cm + dur * 1000,
        "max_leaver_gap_sec": 0,
        "participants_json": pj,
    }


@click.command()
@click.option("--db", default=Path("data/lcu/games.db"), type=click.Path(exists=True, path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--patches", default="16.10,16.11,16.12", show_default=True)
@click.option("--cap-oldest", default=160000, show_default=True,
              help="keep only the most-recent N games of the OLDEST patch (0 = all)")
@click.option("--batch-rows", default=25000, show_default=True,
              help="games buffered per parquet row group; lower it if memory is tight")
@click.option("--part-rows", default=100000, show_default=True,
              help="games per durable part; a stopped export loses at most one part")
@click.option("--resume-max-age-hours", default=24.0, show_default=True,
              help="continue a stopped export of the same pool if it began within this long")
def main(db, out, patches, cap_oldest, batch_rows, part_rows, resume_max_age_hours):
    prefixes = [p.strip() for p in patches.split(",") if p.strip()]
    con = sqlite3.connect(str(db))
    out.parent.mkdir(parents=True, exist_ok=True)
    arrow_schema = pl.DataFrame([], schema=SCHEMA).to_arrow().schema
    # Finished parts live beside the target and are stitched into it only once
    # the whole pool is in: a run stopped partway (the heavy-job guard kills the
    # process when other programs take the machine's memory) keeps what it
    # wrote, and never publishes a parquet that reads cleanly and is quietly
    # short of games.
    store = PartStore(
        out.with_name(out.name + ".parts"),
        {"db": str(Path(db).resolve()), "patches": prefixes, "cap_oldest": cap_oldest},
        max_age_sec=resume_max_age_hours * 3600,
    )
    cursor = store.cursor or {"prefix": 0, "after": None, "kept": 0, "done": []}
    if store.resumed:
        click.echo(f"  resuming: {store.rows} games already exported")

    writer = None
    tmp = None
    part_count = 0
    batch: list[dict] = []

    def flush():
        nonlocal writer, tmp, part_count
        if not batch:
            return
        # Build Arrow buffers directly. Repeated Polars -> Arrow conversion
        # retained multi-GiB allocator arenas during a full production export.
        table = pa.Table.from_pylist(batch, schema=arrow_schema)
        if writer is None:
            tmp = store.new_part_path()
            writer = pq.ParquetWriter(tmp, arrow_schema, compression="zstd")
        writer.write_table(table)
        part_count += len(batch)
        batch.clear()

    def close_part(next_cursor):
        nonlocal writer, tmp, part_count
        flush()
        if writer is not None:
            writer.close()
            writer = None
        store.commit(tmp, part_count, next_cursor)
        tmp = None
        part_count = 0

    try:
        if "cutoff" not in store.extra:
            # Fixed for the life of the parts: a resumed run must not move the
            # boundary the earlier parts were cut at.
            store.extra["cutoff"] = (
                _cap_cutoff(con, prefixes[0], cap_oldest) if prefixes and cap_oldest else None)
            store.save()
        cutoff = store.extra["cutoff"]
        start = cursor["prefix"]
        done = list(cursor["done"])
        for i in range(start, len(prefixes)):
            pre = prefixes[i]
            kept = cursor["kept"] if i == start else 0
            # Games sharing the cutoff timestamp all stay.  A LIMIT would have
            # broken that tie arbitrarily; keeping them makes the cap
            # deterministic across runs, at the cost of a few extra games.
            rows = _iter_rows(
                con, pre, cutoff=cutoff if i == 0 else None,
                after=cursor["after"] if i == start else None,
            )
            # Close the generator on the way out rather than leaving it to the
            # collector: on an error path its cursor would otherwise be closed
            # after the connection is, and the ResourceWarning that produces
            # lands in stderr -- which is exactly what the refresher quotes back
            # as the failure reason.
            try:
                for row in rows:
                    record = _record(row[:9])
                    if record is None:
                        continue
                    batch.append(record)
                    kept += 1
                    if len(batch) >= batch_rows:
                        flush()
                        if part_count >= part_rows:
                            close_part({"prefix": i, "after": [row[1], row[7], row[9]],
                                        "kept": kept, "done": done})
            finally:
                rows.close()
            done = [*done, kept]
            close_part({"prefix": i + 1, "after": None, "kept": 0, "done": done})
            click.echo(f"  {pre}: {kept} games")
        total = store.assemble(out, arrow_schema)
    finally:
        if writer is not None:
            writer.close()
        con.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    click.echo(f"wrote {out}  rows={total}  size={out.stat().st_size/1e6:.0f}MB")


if __name__ == "__main__":
    main()
