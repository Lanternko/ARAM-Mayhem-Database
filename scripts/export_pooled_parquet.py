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
import os
import sqlite3
from pathlib import Path

import click
import polars as pl
import pyarrow.parquet as pq

SCHEMA = {
    "match_id": pl.String, "patch": pl.String, "queue_id": pl.Int64,
    "platform": pl.String, "duration_sec": pl.Int64,
    "blue_champions": pl.List(pl.Int64), "red_champions": pl.List(pl.Int64),
    "blue_wins": pl.Boolean, "game_creation_ms": pl.Int64, "game_end_ms": pl.Int64,
    "max_leaver_gap_sec": pl.Int64, "participants_json": pl.String,
}


def _iter_rows(con, where, params=(), *, chunk=2000):
    """Stream rows instead of materializing the pool.

    participants_json averages ~11KB, so a three-patch pool is ~8GB of JSON
    before any Python object overhead.  fetchall() held that once as rows,
    again as dicts, and a third time while polars built the frame; the 16.17
    refresh died on `memory allocation of 16777216 bytes failed` after seven
    consecutive attempts.  Reading in chunks keeps only one batch live.
    """
    cur = con.execute(
        "SELECT game_id, patch, queue_id, duration_sec, blue_champs, red_champs, "
        "blue_wins, created_ms, participants_json FROM games "
        f"WHERE queue_id=2400 AND blue_champs IS NOT NULL AND red_champs IS NOT NULL "
        f"AND blue_wins IS NOT NULL AND participants_json IS NOT NULL AND {where}",
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
def main(db, out, patches, cap_oldest, batch_rows):
    prefixes = [p.strip() for p in patches.split(",") if p.strip()]
    con = sqlite3.connect(str(db))
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename on success.  A row-group writer that
    # dies partway still closes out a valid footer, so publishing in place would
    # hand the training steps a parquet that reads cleanly and is quietly short
    # of games -- worse than the zero-byte file the old whole-frame write left.
    tmp = out.with_name(out.name + ".partial")
    tmp.unlink(missing_ok=True)

    writer = None
    batch: list[dict] = []
    total = 0

    def flush():
        nonlocal writer, total
        if not batch:
            return
        table = pl.DataFrame(batch, schema=SCHEMA).to_arrow()
        if writer is None:
            writer = pq.ParquetWriter(tmp, table.schema, compression="zstd")
        writer.write_table(table)
        total += len(batch)
        batch.clear()

    try:
        for i, pre in enumerate(prefixes):
            if i == 0 and cap_oldest:
                rows = _iter_rows(
                    con, "patch LIKE ? ORDER BY created_ms DESC LIMIT ?",
                    (pre + ".%", cap_oldest),
                )
            else:
                rows = _iter_rows(con, "patch LIKE ?", (pre + ".%",))
            kept = 0
            # Close the generator on the way out rather than leaving it to the
            # collector: on an error path its cursor would otherwise be closed
            # after the connection is, and the ResourceWarning that produces
            # lands in stderr -- which is exactly what the refresher quotes back
            # as the failure reason.
            try:
                for row in rows:
                    record = _record(row)
                    if record is None:
                        continue
                    batch.append(record)
                    kept += 1
                    if len(batch) >= batch_rows:
                        flush()
            finally:
                rows.close()
            click.echo(f"  {pre}: {kept} games")
        flush()
        if writer is None:
            # Downstream steps open this path unconditionally, so an empty pool
            # still has to leave a readable file with the right schema.
            empty = pl.DataFrame([], schema=SCHEMA).to_arrow()
            writer = pq.ParquetWriter(tmp, empty.schema, compression="zstd")
            writer.write_table(empty)
        if writer is not None:
            writer.close()
            writer = None
        os.replace(tmp, out)
    finally:
        if writer is not None:
            writer.close()
        con.close()
        tmp.unlink(missing_ok=True)

    click.echo(f"wrote {out}  rows={total}  size={out.stat().st_size/1e6:.0f}MB")


if __name__ == "__main__":
    main()
