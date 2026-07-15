from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .db import count_games, insert_public_games, latest_patch_prefix
from .meta_pick import (
    InProcessRateLimiter,
    MetaPickError,
    PatchMismatch,
    SnapshotUnavailable,
    client_key_from_request,
    default_snapshot_path,
    list_leaderboard,
    load_snapshot,
    rate_limit_per_hour,
    snapshot_patch,
    submit_run,
)
from .payload import build_tier_list_payload, champion_augments


DEFAULT_SITE_DB = Path(os.environ.get("ARAM_SITE_DB", "data/site/games_public.db"))
ADMIN_TOKEN = os.environ.get("ARAM_SITE_ADMIN_TOKEN", "")


try:
    from fastapi import FastAPI, Header, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
except Exception as exc:  # pragma: no cover - import-time guidance for optional web deps
    raise RuntimeError(
        "FastAPI is required for aram_nn.site.api. Install with `python -m pip install -e .`."
    ) from exc


app = FastAPI(title="ARAM Mayhem Database API", version="0.1.0")

# CORS only when ARAM_SITE_CORS_ORIGINS is set (comma-separated). Production
# typically: ARAM_SITE_CORS_ORIGINS=https://arammeta.com
_cors_raw = os.environ.get("ARAM_SITE_CORS_ORIGINS", "").strip()
if _cors_raw:
    _origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
    if _origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

# Best-effort single-process submit limiter (not shared across workers).
_submit_limiter = InProcessRateLimiter(rate_limit_per_hour())


def _require_admin_token(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        return
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


def _site_db() -> Path:
    return Path(os.environ.get("ARAM_SITE_DB", str(DEFAULT_SITE_DB)))


def _snapshot_path() -> Path:
    return default_snapshot_path()


def _http_meta_pick_error(exc: MetaPickError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    db = _site_db()
    return {"ok": True, "db": str(db), "games": count_games(db)}


@app.post("/games/bulk")
@app.post("/api/games/bulk")
def post_games_bulk(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin_token(authorization)
    games = payload.get("games")
    if not isinstance(games, list):
        raise HTTPException(status_code=400, detail="payload.games must be a list")
    try:
        result = insert_public_games(_site_db(), games)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "received": result.received,
        "inserted": result.inserted,
        "skipped": result.skipped,
        "total_games": count_games(_site_db()),
    }


def _resolve_patch(patch: str | None, queue: int) -> str | None:
    if patch and patch != "auto":
        return patch
    return latest_patch_prefix(_site_db(), queue_id=queue)


@app.get("/tier-list")
@app.get("/api/tier-list")
def get_tier_list(
    queue: int = Query(default=2400),
    patch: str | None = Query(default=None),
    min_games: int = Query(default=50, ge=1),
    min_pair_games: int = Query(default=15, ge=1),
) -> dict[str, Any]:
    return build_tier_list_payload(
        db=_site_db(),
        queue_id=queue,
        patch_prefix=_resolve_patch(patch, queue),
        min_games=min_games,
        min_pair_games=min_pair_games,
    )


@app.get("/champion/{champion_id}/augments")
@app.get("/api/champion/{champion_id}/augments")
def get_champion_augments(
    champion_id: int,
    queue: int = Query(default=2400),
    patch: str | None = Query(default=None),
    min_games: int = Query(default=50, ge=1),
    min_pair_games: int = Query(default=15, ge=1),
) -> dict[str, Any]:
    payload = build_tier_list_payload(
        db=_site_db(),
        queue_id=queue,
        patch_prefix=_resolve_patch(patch, queue),
        min_games=min_games,
        min_pair_games=min_pair_games,
    )
    result = champion_augments(payload, champion_id)
    if result is None:
        raise HTTPException(status_code=404, detail="champion not found in current tier-list")
    return result


@app.post("/meta-pick/runs")
@app.post("/api/meta-pick/runs")
def post_meta_pick_run(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Accept a 5-round Meta Pick run; recompute ranks server-side; upsert best."""
    key = client_key_from_request(request)
    if not _submit_limiter.allow(key):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body required")
    try:
        snapshot = load_snapshot(_snapshot_path())
        return submit_run(_site_db(), payload, snapshot)
    except SnapshotUnavailable as exc:
        raise _http_meta_pick_error(exc) from exc
    except PatchMismatch as exc:
        raise _http_meta_pick_error(exc) from exc
    except MetaPickError as exc:
        raise _http_meta_pick_error(exc) from exc


@app.get("/meta-pick/leaderboard")
@app.get("/api/meta-pick/leaderboard")
def get_meta_pick_leaderboard(
    patch: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Global Meta Pick leaderboard for a patch (default = snapshot patch)."""
    try:
        snapshot = load_snapshot(_snapshot_path())
    except SnapshotUnavailable as exc:
        raise _http_meta_pick_error(exc) from exc
    snap_patch = snapshot_patch(snapshot)
    use_patch = (patch or "").strip() or snap_patch
    return list_leaderboard(
        _site_db(),
        patch=use_patch,
        limit=limit,
        snapshot=snapshot,
    )
