from __future__ import annotations

import json
import math
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
    growth_ratio: float
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


def _state_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _last_total_for_scope(
    state: dict[str, Any], *, patch_prefix: str | None, total_key: str
) -> tuple[int, str]:
    if patch_prefix:
        patches = state.get("patches")
        if isinstance(patches, dict):
            patch_state = patches.get(patch_prefix)
            if isinstance(patch_state, dict) and total_key in patch_state:
                return _state_int(patch_state.get(total_key)), "patch state"
        if state.get("last_patch_prefix") == patch_prefix:
            return _state_int(state.get(total_key)), "legacy state"
        return 0, "new patch baseline"
    return _state_int(state.get(total_key)), "global state"


def _effective_threshold(*, last_total: int, threshold: int, growth_ratio: float) -> int:
    absolute_threshold = max(0, int(threshold or 0))
    ratio_threshold = 0
    if growth_ratio > 0:
        ratio_threshold = max(1, math.ceil(max(0, last_total) * growth_ratio))
    return max(1, absolute_threshold, ratio_threshold)


def _decision_reason(
    *,
    delta: int,
    threshold: int,
    absolute_threshold: int,
    growth_ratio: float,
    baseline_source: str,
    patch_prefix: str | None,
    passed: bool,
) -> str:
    op = ">=" if passed else "<"
    reason = f"delta {delta} {op} {threshold}"
    if growth_ratio > 0 and threshold > max(0, int(absolute_threshold or 0)):
        reason += f" (growth {growth_ratio:.0%})"
    if baseline_source == "new patch baseline" and patch_prefix:
        reason += f"; first upload baseline for patch {patch_prefix}"
    return reason


def _record_patch_state(
    state: dict[str, Any], *, patch_prefix: str | None, payload: dict[str, Any]
) -> None:
    state.update(payload)
    if not patch_prefix:
        return
    patches = state.get("patches")
    if not isinstance(patches, dict):
        patches = {}
        state["patches"] = patches
    patch_state = patches.get(patch_prefix)
    if not isinstance(patch_state, dict):
        patch_state = {}
    patch_state.update(payload)
    patches[patch_prefix] = patch_state


def decide_sync(
    *,
    db: Path,
    state: dict[str, Any],
    threshold: int,
    force: bool,
    growth_ratio: float = 0.0,
    queue_id: int | None = None,
    patch_prefix: str | None = None,
) -> SyncDecision:
    local_total = count_games(db, queue_id=queue_id, patch_prefix=patch_prefix)
    last_uploaded_total, baseline_source = _last_total_for_scope(
        state,
        patch_prefix=patch_prefix,
        total_key="last_uploaded_total",
    )
    effective_threshold = _effective_threshold(
        last_total=last_uploaded_total,
        threshold=threshold,
        growth_ratio=growth_ratio,
    )
    if force:
        return SyncDecision(True, local_total, last_uploaded_total, effective_threshold, growth_ratio, "force")
    if local_total <= 0:
        return SyncDecision(False, local_total, last_uploaded_total, effective_threshold, growth_ratio, "no local games")
    delta = local_total - last_uploaded_total
    if delta >= effective_threshold:
        return SyncDecision(
            True,
            local_total,
            last_uploaded_total,
            effective_threshold,
            growth_ratio,
            _decision_reason(
                delta=delta,
                threshold=effective_threshold,
                absolute_threshold=threshold,
                growth_ratio=growth_ratio,
                baseline_source=baseline_source,
                patch_prefix=patch_prefix,
                passed=True,
            ),
        )
    return SyncDecision(
        False,
        local_total,
        last_uploaded_total,
        effective_threshold,
        growth_ratio,
        _decision_reason(
            delta=delta,
            threshold=effective_threshold,
            absolute_threshold=threshold,
            growth_ratio=growth_ratio,
            baseline_source=baseline_source,
            patch_prefix=patch_prefix,
            passed=False,
        ),
    )


def push_public_games(
    *,
    db: Path,
    api_url: str,
    state_path: Path,
    threshold: int = 0,
    growth_ratio: float = 0.10,
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
        growth_ratio=growth_ratio,
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
            "threshold": decision.threshold,
            "growth_ratio": decision.growth_ratio,
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

    _record_patch_state(
        state,
        patch_prefix=patch_prefix,
        payload={
            "last_uploaded_total": decision.local_total,
            "last_upload_at_unix": time.time(),
            "last_api_url": api_root,
            "last_queue_id": queue_id,
            "last_patch_prefix": patch_prefix,
            "last_result": totals,
            "last_threshold": decision.threshold,
            "last_growth_ratio": decision.growth_ratio,
        },
    )
    save_state(state_path, state)
    return {
        "pushed": True,
        "reason": decision.reason,
        "local_total": decision.local_total,
        "last_uploaded_total": decision.last_uploaded_total,
        "threshold": decision.threshold,
        "growth_ratio": decision.growth_ratio,
        **totals,
    }
