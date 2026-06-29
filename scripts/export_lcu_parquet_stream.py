"""Stream-export LCU games SQLite rows to the training parquet schema."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA = pa.schema(
    [
        ("match_id", pa.string()),
        ("patch", pa.string()),
        ("queue_id", pa.int64()),
        ("platform", pa.string()),
        ("duration_sec", pa.int64()),
        ("blue_champions", pa.list_(pa.int64())),
        ("red_champions", pa.list_(pa.int64())),
        ("blue_wins", pa.bool_()),
        ("game_creation_ms", pa.int64()),
        ("game_end_ms", pa.int64()),
        ("max_leaver_gap_sec", pa.int64()),
        ("participants_json", pa.string()),
    ]
)


def parse_champs(raw: str) -> list[int]:
    values = json.loads(raw)
    return sorted(int(value) for value in values)


def row_to_record(row: tuple[Any, ...], platform: str) -> dict[str, Any]:
    game_id, queue_id, patch, blue_json, red_json, blue_wins, duration_sec, created_ms, participants_json = row
    duration = int(duration_sec or 0)
    created = int(created_ms or 0)
    return {
        "match_id": f"LCU_{game_id}",
        "patch": str(patch or ""),
        "queue_id": int(queue_id),
        "platform": platform,
        "duration_sec": duration,
        "blue_champions": parse_champs(str(blue_json)),
        "red_champions": parse_champs(str(red_json)),
        "blue_wins": bool(blue_wins),
        "game_creation_ms": created,
        "game_end_ms": created + duration * 1000,
        "max_leaver_gap_sec": 0,
        "participants_json": participants_json or "[]",
    }


def export(args: argparse.Namespace) -> None:
    db = Path(args.db)
    out = Path(args.out)
    if not db.exists():
        raise SystemExit(f"database not found: {db}")

    filters = ["queue_id = ?"]
    params: list[Any] = [args.queue]
    if args.patch_prefix:
        filters.append("patch LIKE ?")
        params.append(f"{args.patch_prefix}%")
    where = " AND ".join(filters)
    query = f"""
        SELECT game_id, queue_id, patch, blue_champs, red_champs,
               blue_wins, duration_sec, created_ms, participants_json
        FROM games
        WHERE {where}
    """

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    con = sqlite3.connect(str(db))
    cur = con.execute(query, params)
    writer: pq.ParquetWriter | None = None
    total = 0
    blue_wins = 0
    try:
        while True:
            batch = cur.fetchmany(args.batch_size)
            if not batch:
                break
            records = [row_to_record(row, args.platform) for row in batch]
            table = pa.Table.from_pylist(records, schema=SCHEMA)
            if writer is None:
                writer = pq.ParquetWriter(out, SCHEMA, compression="zstd")
            writer.write_table(table)
            total += len(records)
            blue_wins += sum(1 for record in records if record["blue_wins"])
            print(f"[stream-export] wrote {total} rows", flush=True)
    finally:
        if writer is not None:
            writer.close()
        con.close()

    if total == 0:
        raise SystemExit("no matching rows exported")
    print(f"[stream-export] done rows={total} blue_wr={blue_wins / total:.3f} out={out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/lcu/games.db")
    parser.add_argument("--out", required=True)
    parser.add_argument("--queue", type=int, default=2400)
    parser.add_argument("--patch-prefix", default="")
    parser.add_argument("--platform", default="TW2")
    parser.add_argument("--batch-size", type=int, default=10000)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
