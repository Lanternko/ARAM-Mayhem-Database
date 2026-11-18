"""Reuse a publish stage's artifact while none of its inputs have changed.

The comp-fit radar and the empirical ability axes are pure functions of the
pooled parquet (rewritten only by the +25% model refresh), a few parameters and
their own code -- yet every publish cycle recomputed them from 4M+ games, which
was most of a cycle's wall time.  A stage describes its inputs to ``stage_key``;
when the key matches the one stored beside the last artifact, the stored bytes
are copied out instead.

One entry per stage name: the key line and the artifact travel in ONE file that
is replaced atomically, so a crash can never pair a new key with old bytes.
Nothing here raises -- a broken cache only costs a recompute.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DIR = Path("data/cache/stage_memo")
DISABLE_ENV = "ARAM_NO_STAGE_MEMO"


def file_identity(path: Path) -> dict[str, Any]:
    """Cheap identity of a large input: a rewrite changes size or mtime."""
    stat = Path(path).stat()
    return {"name": Path(path).name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def stage_key(*, code: Iterable[Path], **inputs: Any) -> str:
    """Digest of the stage's source files and its JSON-serializable *inputs*."""
    digest = hashlib.sha256()
    for path in code:
        digest.update(Path(path).name.encode())
        digest.update(Path(path).read_bytes())
    digest.update(json.dumps(inputs, sort_keys=True, default=str).encode())
    return digest.hexdigest()


def _entry(name: str, cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{name}.memo"


def _replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def restore(name: str, key: str, out: Path, *, cache_dir: Path = DEFAULT_DIR) -> bool:
    """Write the stored artifact to *out* if it was built from the same inputs."""
    if os.environ.get(DISABLE_ENV):
        return False
    try:
        stored_key, sep, artifact = _entry(name, cache_dir).read_bytes().partition(b"\n")
        if not sep or stored_key.decode("ascii", "replace") != key or not artifact:
            return False
        _replace(Path(out), artifact)
        return True
    except OSError:
        return False


def store(name: str, key: str, out: Path, *, cache_dir: Path = DEFAULT_DIR) -> None:
    """Remember *out* as the artifact for *key* (replacing the previous entry)."""
    if os.environ.get(DISABLE_ENV):
        return
    try:
        _replace(_entry(name, cache_dir), key.encode("ascii") + b"\n" + Path(out).read_bytes())
    except OSError:
        pass
