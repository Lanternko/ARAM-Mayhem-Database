from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .db import count_games, public_game_batch


@dataclass(frozen=True)
class SyncDecision:
    should_push: bool
    local_total: int
    last_uploaded_total: int
    threshold: int
    reason: str


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def decide_sync(
    *,
    db: Path,
    state: dict[str, Any],
    threshold: int,
    force: bool,
    queue_id: int | None = None,
    patch_prefix: str | None = None,
) -> SyncDecision:
    local_total = count_games(db, queue_id=queue_id, patch_prefix=patch_prefix)
    last_uploaded_total = int(state.get("last_uploaded_total") or 0)
    if force:
        return SyncDecision(True, local_total, last_uploaded_total, threshold, "force")
    if local_total <= 0:
        return SyncDecision(False, local_total, last_uploaded_total, threshold, "no local games")
    delta = local_total - last_uploaded_total
    if delta >= threshold:
        return SyncDecision(True, local_total, last_uploaded_total, threshold, f"delta {delta} >= {threshold}")
    return SyncDecision(False, local_total, last_uploaded_total, threshold, f"delta {delta} < {threshold}")


def push_public_games(
    *,
    db: Path,
    api_url: str,
    state_path: Path,
    threshold: int = 10_000,
    batch_size: int = 1000,
    force: bool = False,
    queue_id: int | None = 2400,
    patch_prefix: str | None = None,
    token: str = "",
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    state = load_state(state_path)
    decision = decide_sync(
        db=db,
        state=state,
        threshold=threshold,
        force=force,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
    )
    if not decision.should_push:
        return {
            "pushed": False,
            "reason": decision.reason,
            "local_total": decision.local_total,
            "last_uploaded_total": decision.last_uploaded_total,
        }

    api_root = api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    totals = {"received": 0, "inserted": 0, "skipped": 0}
    with httpx.Client(timeout=timeout_sec, headers=headers) as client:
        for batch in public_game_batch(
            db,
            queue_id=queue_id,
            patch_prefix=patch_prefix,
            chunk_size=batch_size,
        ):
            response = client.post(f"{api_root}/games/bulk", json={"games": batch})
            response.raise_for_status()
            payload = response.json()
            for key in totals:
                totals[key] += int(payload.get(key) or 0)

    state.update(
        {
            "last_uploaded_total": decision.local_total,
            "last_upload_at_unix": time.time(),
            "last_api_url": api_root,
            "last_queue_id": queue_id,
            "last_patch_prefix": patch_prefix,
            "last_result": totals,
        }
    )
    save_state(state_path, state)
    return {
        "pushed": True,
        "reason": decision.reason,
        "local_total": decision.local_total,
        "last_uploaded_total": decision.last_uploaded_total,
        **totals,
    }
