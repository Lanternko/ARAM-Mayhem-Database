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


def _fetch(con, where, params=()):
    return con.execute(
        "SELECT game_id, patch, queue_id, duration_sec, blue_champs, red_champs, "
        "blue_wins, created_ms, participants_json FROM games "
        f"WHERE queue_id=2400 AND blue_champs IS NOT NULL AND red_champs IS NOT NULL "
        f"AND blue_wins IS NOT NULL AND participants_json IS NOT NULL AND {where}",
        params,
    ).fetchall()


@click.command()
@click.option("--db", default=Path("data/lcu/games.db"), type=click.Path(exists=True, path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--patches", default="16.10,16.11,16.12", show_default=True)
@click.option("--cap-oldest", default=160000, show_default=True,
              help="keep only the most-recent N games of the OLDEST patch (0 = all)")
def main(db, out, patches, cap_oldest):
    prefixes = [p.strip() for p in patches.split(",") if p.strip()]
    con = sqlite3.connect(str(db))

    rows = []
    for i, pre in enumerate(prefixes):
        if i == 0 and cap_oldest:
            r = _fetch(con, "patch LIKE ? ORDER BY created_ms DESC LIMIT ?", (pre + ".%", cap_oldest))
        else:
            r = _fetch(con, "patch LIKE ?", (pre + ".%",))
        click.echo(f"  {pre}: {len(r)} games")
        rows.extend(r)
    con.close()

    recs = []
    for game_id, patch, queue_id, duration_sec, b, rd, bw, created_ms, pj in rows:
        try:
            blue = [int(x) for x in json.loads(b)]
            red = [int(x) for x in json.loads(rd)]
        except Exception:
            continue
        if len(blue) != 5 or len(red) != 5:
            continue
        dur = int(duration_sec or 0)
        cm = int(created_ms or 0)
        recs.append({
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
        })

    df = pl.DataFrame(recs, schema={
        "match_id": pl.String, "patch": pl.String, "queue_id": pl.Int64,
        "platform": pl.String, "duration_sec": pl.Int64,
        "blue_champions": pl.List(pl.Int64), "red_champions": pl.List(pl.Int64),
        "blue_wins": pl.Boolean, "game_creation_ms": pl.Int64, "game_end_ms": pl.Int64,
        "max_leaver_gap_sec": pl.Int64, "participants_json": pl.String,
    })
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    click.echo(f"wrote {out}  rows={df.height}  size={out.stat().st_size/1e6:.0f}MB")


if __name__ == "__main__":
    main()
