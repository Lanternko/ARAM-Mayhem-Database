from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .db import count_games, insert_public_games
from .payload import build_tier_list_payload, champion_augments


DEFAULT_SITE_DB = Path(os.environ.get("ARAM_SITE_DB", "data/site/games_public.db"))
ADMIN_TOKEN = os.environ.get("ARAM_SITE_ADMIN_TOKEN", "")


try:
    from fastapi import FastAPI, Header, HTTPException, Query
except Exception as exc:  # pragma: no cover - import-time guidance for optional web deps
    raise RuntimeError(
        "FastAPI is required for aram_nn.site.api. Install with `python -m pip install -e .`."
    ) from exc


app = FastAPI(title="ARAM Mayhem Database API", version="0.1.0")


def _require_admin_token(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        return
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


def _site_db() -> Path:
    return Path(os.environ.get("ARAM_SITE_DB", str(DEFAULT_SITE_DB)))


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


@app.get("/tier-list")
@app.get("/api/tier-list")
def get_tier_list(
    queue: int = Query(default=2400),
    patch: str = Query(default="16.10"),
    min_games: int = Query(default=50, ge=1),
    min_pair_games: int = Query(default=15, ge=1),
) -> dict[str, Any]:
    return build_tier_list_payload(
        db=_site_db(),
        queue_id=queue,
        patch_prefix=patch,
        min_games=min_games,
        min_pair_games=min_pair_games,
    )


@app.get("/champion/{champion_id}/augments")
@app.get("/api/champion/{champion_id}/augments")
def get_champion_augments(
    champion_id: int,
    queue: int = Query(default=2400),
    patch: str = Query(default="16.10"),
    min_games: int = Query(default=50, ge=1),
    min_pair_games: int = Query(default=15, ge=1),
) -> dict[str, Any]:
    payload = build_tier_list_payload(
        db=_site_db(),
        queue_id=queue,
        patch_prefix=patch,
        min_games=min_games,
        min_pair_games=min_pair_games,
    )
    result = champion_augments(payload, champion_id)
    if result is None:
        raise HTTPException(status_code=404, detail="champion not found in current tier-list")
    return result
