"""Retention policy for one-off SQLite copies left under data/site.

Why this exists
---------------
`data/lcu/games.db` is ~64 GB.  Ad-hoc tasks (a publish snapshot, an offline
backup feeding a build) copy it whole, finish, and leave the copy behind.  Two
such copies from 2026-08-12 and 2026-08-14 sat there for two weeks and held
116.9 GB between them -- more than half the free space on D:.  The daily
rotation in `data/site/backup_meta_pick_db.py` only covers `meta_pick.db`
(~100 KB) and never looks at these.

Why matching on names and age is not enough
-------------------------------------------
The two files that had to go were `games-spell-pair-publish-20260812-0249.db`
and `player_history/offline/games-backup-20260814.sqlite`.  The file that had
to stay was `player_history/service/snapshot-20260814.sqlite` -- same age, same
"snapshot/backup" vocabulary, and it tested FREE on an exclusive open while the
player-history API was live, because that API opens it per request rather than
holding a handle.  So neither the filename, the mtime, nor an open-handle probe
separates disposable from load-bearing.  Protection here is therefore explicit
and layered, and the whole thing is dry-run until told otherwise.

Usage:
  python scripts/prune_stale_db_snapshots.py            # dry run, prints plan
  python scripts/prune_stale_db_snapshots.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

# Only ever scanned root.  data/lcu is deliberately absent: that is where the
# live crawler DB and its -wal/-shm live, and nothing in it is ever disposable.
DEFAULT_ROOTS = (ROOT / "data" / "site",)

# Paths that must never be considered, whatever the config says.  Checked
# against the resolved path so a symlink or a `--root ..` cannot walk into them.
HARD_DENY_DIRS = (
    ROOT / "data" / "lcu",
    ROOT / ".git",
)
HARD_DENY_NAMES = ("games.db", "meta_pick.db")

# Directories whose contents are serving or intentionally rotated artifacts.
# `service/` holds the snapshot the player-history API reads; `backups/` is the
# meta_pick daily rotation, which owns its own retention.
DEFAULT_PROTECT_GLOBS = (
    "**/player_history/service/**",
    "**/backups/**",
)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_MAGIC = b"SQLite format 3\x00"

# Files that reference a candidate by name are treated as proof the candidate is
# still wired into something.  Manifests and state files are small; reading them
# all on every run costs nothing next to the 60 GB decisions being made.
REFERENCE_SUFFIXES = (".json", ".yml", ".yaml", ".txt", ".manifest")
REFERENCE_MAX_BYTES = 2_000_000


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def looks_like_sqlite(path: Path) -> bool:
    """Read the 16-byte header rather than trusting the suffix."""
    try:
        with path.open("rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def has_open_handle(path: Path) -> bool:
    """True when another process holds the file open.

    Necessary but not sufficient as a guard -- see the module docstring.  A
    False here only means "nobody is holding it *right now*".
    """
    try:
        with path.open("rb+"):
            return False
    except PermissionError:
        return True
    except OSError:
        # Missing/unreadable is handled by the caller; treat as "do not touch".
        return True


def collect_reference_text(roots: Iterable[Path]) -> str:
    """Concatenate small config/manifest/state files found under the roots."""
    chunks: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in REFERENCE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > REFERENCE_MAX_BYTES:
                    continue
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(chunks)


def sidecar_present(path: Path) -> bool:
    """A -wal/-shm next to a SQLite file means a connection was not closed cleanly."""
    return any(
        path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm")
    )


def classify(
    path: Path,
    *,
    now: float,
    min_size_bytes: int,
    max_age_days: float,
    protect_globs: tuple[str, ...],
    reference_text: str,
) -> tuple[bool, str]:
    """Return (deletable, reason).  Every skip carries the reason it was skipped."""
    resolved = path.resolve()

    for denied in HARD_DENY_DIRS:
        if is_under(resolved, denied):
            return False, f"hard-deny dir ({denied.name})"
    if resolved.name in HARD_DENY_NAMES:
        return False, "hard-deny name"

    posix = resolved.as_posix()
    for pattern in protect_globs:
        # fnmatch over the posix path: '**/x/**' behaves as a substring guard,
        # which is what is wanted for directory protection.
        if fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(posix, pattern.replace("**/", "*")):
            return False, f"protected glob ({pattern})"

    try:
        stat = resolved.stat()
    except OSError as exc:
        return False, f"stat failed ({exc.__class__.__name__})"

    if stat.st_size < min_size_bytes:
        return False, f"below size floor ({stat.st_size / 1024**3:.2f} GB)"

    age_days = (now - stat.st_mtime) / 86400.0
    if age_days < max_age_days:
        return False, f"too recent ({age_days:.1f}d)"

    if not looks_like_sqlite(resolved):
        return False, "not a SQLite file"

    if sidecar_present(resolved):
        return False, "-wal/-shm sidecar present"

    if resolved.name in reference_text:
        return False, "referenced by a manifest/state file"

    if has_open_handle(resolved):
        return False, "open handle held by another process"

    return True, f"stale copy ({stat.st_size / 1024**3:.1f} GB, {age_days:.0f}d old)"


def scan(
    roots: Iterable[Path],
    *,
    min_size_bytes: int,
    max_age_days: float,
    protect_globs: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.time()
    reference_text = collect_reference_text(roots)
    deletable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SQLITE_SUFFIXES:
                continue
            ok, reason = classify(
                path,
                now=now,
                min_size_bytes=min_size_bytes,
                max_age_days=max_age_days,
                protect_globs=protect_globs,
                reference_text=reference_text,
            )
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            row = {
                "path": str(path),
                "size_gb": round(size / 1024**3, 2),
                "reason": reason,
            }
            (deletable if ok else skipped).append(row)

    deletable.sort(key=lambda r: -r["size_gb"])
    skipped.sort(key=lambda r: -r["size_gb"])
    return deletable, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, action="append", default=None,
        help="Directory to scan (repeatable). Default: data/site",
    )
    parser.add_argument(
        "--min-size-gb", type=float, default=1.0,
        help="Only files at least this large are candidates (default: 1.0)",
    )
    parser.add_argument(
        # 7 days: both offending copies were build inputs whose derived artifact
        # was verified within hours. A week leaves room to notice a bad build
        # while keeping at most one stale copy on disk between runs.
        "--max-age-days", type=float, default=7.0,
        help="Only files older than this are candidates (default: 7)",
    )
    parser.add_argument(
        "--protect", action="append", default=None,
        help="Extra protection glob (repeatable), matched against the posix path",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without this the run only prints the plan.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the plan as JSON")
    args = parser.parse_args(argv)

    roots = tuple(args.root) if args.root else DEFAULT_ROOTS
    protect_globs = DEFAULT_PROTECT_GLOBS + tuple(args.protect or ())

    deletable, skipped = scan(
        roots,
        min_size_bytes=int(args.min_size_gb * 1024**3),
        max_age_days=args.max_age_days,
        protect_globs=protect_globs,
    )

    reclaimable = round(sum(r["size_gb"] for r in deletable), 2)

    if args.json:
        print(json.dumps(
            {
                "ts": utc_now().isoformat(),
                "applied": bool(args.apply),
                "reclaimable_gb": reclaimable,
                "deletable": deletable,
                "skipped": skipped,
            },
            ensure_ascii=False,
            indent=2,
        ))

    deleted_gb = 0.0
    failures: list[str] = []
    if args.apply:
        for row in deletable:
            target = Path(row["path"])
            try:
                target.unlink()
                deleted_gb += row["size_gb"]
            except OSError as exc:
                failures.append(f"{target}: {exc}")

    if not args.json:
        for row in skipped:
            print(f"keep   {row['size_gb']:>7.2f} GB  {row['path']}  [{row['reason']}]")
        for row in deletable:
            verb = "DELETED" if args.apply else "would delete"
            print(f"{verb} {row['size_gb']:>7.2f} GB  {row['path']}  [{row['reason']}]")
        for failure in failures:
            print(f"FAILED {failure}", file=sys.stderr)

    if args.apply:
        # Silence on a no-op run keeps the scheduled log from growing a line a day.
        if deletable or failures:
            print(f"ok pruned={len(deletable) - len(failures)} reclaimed_gb={deleted_gb:.2f}")
    else:
        print(f"dry-run candidates={len(deletable)} reclaimable_gb={reclaimable}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
