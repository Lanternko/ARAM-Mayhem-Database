"""A parquet written as durable parts, so a killed writer keeps its progress.

A parquet has no footer until its writer closes; a process stopped by the
heavy-job memory guard is killed outright, so a single-file export loses
everything it wrote.  The pooled export takes ~50 minutes and uses ~0.7 GB, and
on a desktop that is also used for games it was being stopped for memory other
programs took, starting from zero every time.

Here each part is a complete parquet that is renamed into place and then
recorded, together with a caller-defined cursor, in a manifest.  A later run
with the same plan picks the cursor up; ``assemble`` stitches the parts into one
file and removes them.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST = "manifest.json"
_VERSION = 1


class PartStore:
    def __init__(self, directory: Path, plan: dict[str, Any], *, max_age_sec: float,
                 now: float | None = None):
        self.directory = Path(directory)
        self.plan = json.loads(json.dumps(plan))
        now = time.time() if now is None else now
        state = self._load()
        self.resumed = (
            state is not None
            and state.get("version") == _VERSION
            and state.get("plan") == self.plan
            and 0 <= now - float(state.get("started_at_unix", 0)) <= max_age_sec
            and all(self._intact(part) for part in state.get("parts", []))
        )
        if self.resumed:
            self.state = state
            self._sweep(keep={part["file"] for part in state["parts"]}, manifest=False)
        else:
            self._sweep(keep=set(), manifest=True)
            self.state = {"version": _VERSION, "plan": self.plan, "started_at_unix": now,
                          "cursor": None, "extra": {}, "parts": []}

    @property
    def cursor(self) -> Any:
        return self.state["cursor"]

    @property
    def extra(self) -> dict[str, Any]:
        return self.state["extra"]

    @property
    def rows(self) -> int:
        return sum(part["rows"] for part in self.state["parts"])

    def new_part_path(self) -> Path:
        """Where to write the next part; it does not count until ``commit``."""
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"part-{len(self.state['parts']):05d}.parquet.tmp"

    def commit(self, tmp: Path | None, rows: int, cursor: Any) -> None:
        """Record ``tmp`` as finished and move the cursor, in that order on disk."""
        if tmp is not None:
            final = tmp.with_suffix("")
            os.replace(tmp, final)
            self.state["parts"].append(
                {"file": final.name, "rows": int(rows), "size": final.stat().st_size})
        self.state["cursor"] = cursor
        self.save()

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.directory / MANIFEST)

    def assemble(self, out: Path, schema: pa.Schema) -> int:
        """Write every part's row groups to ``out`` (via a sibling temp), then drop the parts."""
        tmp = out.with_name(out.name + ".partial")
        tmp.unlink(missing_ok=True)
        try:
            with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
                wrote = False
                for part in self.state["parts"]:
                    with pq.ParquetFile(self.directory / part["file"]) as source:
                        for group in range(source.metadata.num_row_groups):
                            writer.write_table(source.read_row_group(group, use_threads=False))
                            wrote = True
                if not wrote:
                    # Readers open this path unconditionally, so an empty pool
                    # still has to leave a readable file with the right schema.
                    writer.write_table(schema.empty_table())
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        rows = self.rows
        self._sweep(keep=set(), manifest=True)
        return rows

    def _load(self) -> dict[str, Any] | None:
        try:
            state = json.loads((self.directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def _intact(self, part: dict[str, Any]) -> bool:
        try:
            return (self.directory / part["file"]).stat().st_size == part["size"]
        except (OSError, KeyError, TypeError):
            return False

    def _sweep(self, *, keep: set[str], manifest: bool) -> None:
        """Remove our own leftovers by name; never recurse (the path could be a link)."""
        if not self.directory.is_dir():
            return
        for path in self.directory.iterdir():
            leftover = path.name.startswith("part-") or path.name == MANIFEST + ".tmp"
            if (leftover and path.name not in keep) or (manifest and path.name == MANIFEST):
                path.unlink(missing_ok=True)
        if manifest:
            try:
                self.directory.rmdir()
            except OSError:
                pass
