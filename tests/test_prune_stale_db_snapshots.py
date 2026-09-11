from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prune_stale_db_snapshots_under_test",
    ROOT / "scripts" / "prune_stale_db_snapshots.py",
)
assert SPEC and SPEC.loader
PRUNE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PRUNE)

GB = 1024**3
# Tests exercise the comparison, not the filesystem: a byte-scale floor keeps the
# suite from writing multi-GB files into the system temp dir (an earlier version
# did, and filled C: to 1.7 GB free mid-run).
TEST_SIZE_FLOOR = 4096
BIG = TEST_SIZE_FLOOR * 4
SMALL = TEST_SIZE_FLOOR // 4


def _make_db(path: Path, *, size_bytes: int, age_days: float) -> Path:
    """A sparse file with a real SQLite header, so size checks are cheap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(PRUNE.SQLITE_MAGIC)
        if size_bytes > len(PRUNE.SQLITE_MAGIC):
            fh.truncate(size_bytes)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _scan(root: Path, **kwargs):
    params = {
        "min_size_bytes": TEST_SIZE_FLOOR,
        "max_age_days": 7.0,
        "protect_globs": PRUNE.DEFAULT_PROTECT_GLOBS,
    }
    params.update(kwargs)
    return PRUNE.scan([root], **params)


def test_stale_large_copy_is_deletable(tmp_path: Path) -> None:
    target = _make_db(tmp_path / "games-backup-20260814.sqlite", size_bytes=BIG, age_days=13)
    deletable, _ = _scan(tmp_path)
    assert [row["path"] for row in deletable] == [str(target)]


def test_serving_snapshot_under_service_is_protected(tmp_path: Path) -> None:
    """The 2026-08-14 near-miss: same age and vocabulary as the disposable copy."""
    _make_db(
        tmp_path / "player_history" / "service" / "snapshot-20260814.sqlite",
        size_bytes=BIG,
        age_days=13,
    )
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "protected glob" in skipped[0]["reason"]


def test_manifest_reference_protects_a_file_outside_protected_dirs(tmp_path: Path) -> None:
    _make_db(tmp_path / "offline" / "keepme-20260814.sqlite", size_bytes=BIG, age_days=13)
    (tmp_path / "offline" / "build.manifest.json").write_text(
        '{"source": "keepme-20260814.sqlite"}', encoding="utf-8"
    )
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "referenced by a manifest" in skipped[0]["reason"]


def test_recent_copy_is_kept(tmp_path: Path) -> None:
    _make_db(tmp_path / "games-backup-fresh.sqlite", size_bytes=BIG, age_days=1)
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "too recent" in skipped[0]["reason"]


def test_small_copy_is_kept(tmp_path: Path) -> None:
    _make_db(tmp_path / "meta_pick-2026-07-15.db", size_bytes=SMALL, age_days=40)
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "below size floor" in skipped[0]["reason"]


def test_wal_sidecar_blocks_deletion(tmp_path: Path) -> None:
    target = _make_db(tmp_path / "half-closed.sqlite", size_bytes=BIG, age_days=30)
    target.with_name(target.name + "-wal").write_bytes(b"")
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "sidecar" in skipped[0]["reason"]


def test_non_sqlite_payload_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "not-really.db"
    with path.open("wb") as fh:
        fh.write(b"this is not a database")
        fh.truncate(BIG)
    stamp = time.time() - 30 * 86400
    os.utime(path, (stamp, stamp))
    deletable, skipped = _scan(tmp_path)
    assert deletable == []
    assert "not a SQLite file" in skipped[0]["reason"]


def test_hard_deny_covers_the_live_crawler_db() -> None:
    ok, reason = PRUNE.classify(
        ROOT / "data" / "lcu" / "games.db",
        now=time.time(),
        min_size_bytes=1 * GB,
        max_age_days=7.0,
        protect_globs=PRUNE.DEFAULT_PROTECT_GLOBS,
        reference_text="",
    )
    assert ok is False
    assert "hard-deny" in reason


def test_default_roots_exclude_data_lcu() -> None:
    assert all("lcu" not in str(root) for root in PRUNE.DEFAULT_ROOTS)
