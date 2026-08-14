"""Capability-gated loopback UI for private player-history queries."""

from __future__ import annotations

import hmac
import html
import json
import secrets
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .player_history_query import (
    PlayerHistoryQueryError,
    PlayerHistorySnapshotHandle,
)
from .player_history_security import validate_secret


_COOKIE_NAME = "aram_player_history_session"
_MAX_BODY_BYTES = 2048
_MAX_RIOT_ID_BYTES = 128
_RATE_LIMIT = 20
_RATE_WINDOW_SEC = 60.0


def _not_found() -> Response:
    return Response(status_code=404)


def _request_is_local(request: Request, port: int) -> bool:
    client = request.client
    if client is None or client.host != "127.0.0.1":
        return False
    host = request.headers.get("host", "").lower()
    return host in (f"localhost:{port}", f"127.0.0.1:{port}")


def _authorized(request: Request, session_capability: str, port: int) -> bool:
    if not _request_is_local(request, port):
        return False
    supplied = request.cookies.get(_COOKIE_NAME, "")
    return hmac.compare_digest(supplied.encode("utf-8"), session_capability.encode("utf-8"))


def _render_result(result: dict[str, object]) -> str:
    if result["status"] == "not_found":
        content = "<p id=\"result\">找不到可用紀錄。</p>"
    else:
        snapshot = result["snapshot"]
        assert isinstance(snapshot, dict)
        histories = result["histories"]
        assert isinstance(histories, list)
        rows = "".join(
            "<tr>"
            f"<td>{int(record['ordinal'])}</td>"
            f"<td>{html.escape(str(record['patch']))}</td>"
            f"<td>{int(record['champion_id'])}</td>"
            f"<td>{html.escape(str(record['outcome']))}</td>"
            f"<td>{html.escape(str(record['duration_bucket']))}</td>"
            "</tr>"
            for record in histories
            if isinstance(record, dict)
        )
        content = (
            f"<p id=\"result\">觀測場數：{int(result['observed_matches'])}；"
            f"低樣本：{'是' if result['low_sample'] else '否'}</p>"
            f"<p>資料集：{html.escape(str(snapshot['dataset_id']))}；"
            f"生成日：{html.escape(str(snapshot['generated_date']))}</p>"
            "<table><thead><tr><th>#</th><th>版本</th><th>英雄</th><th>結果</th><th>時長</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    return _page(content)


def _page(content: str = "") -> str:
    return (
        "<!doctype html><html lang=\"zh-Hant\"><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\"><title>本機對戰紀錄</title>"
        "<body><main><h1>本機對戰紀錄</h1>"
        "<form method=\"post\" action=\"/query\">"
        "<label>Riot ID <input name=\"riot_id\" autocomplete=\"off\" maxlength=\"128\" required></label>"
        "<button type=\"submit\">查詢</button></form>"
        f"{content}</main></body></html>"
    )


def create_player_history_local_app(
    *, snapshot_path: Path, lookup_secret: bytes, port: int
) -> tuple[FastAPI, str]:
    """Create one app and return its one-use bootstrap token to the runner."""

    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid_configuration")
    try:
        snapshot_handle = PlayerHistorySnapshotHandle.open(snapshot_path)
        lookup_secret = validate_secret(lookup_secret)
    except Exception:
        raise ValueError("invalid_configuration") from None
    session_capability = secrets.token_urlsafe(32)
    bootstrap_token = secrets.token_urlsafe(32)
    bootstrap_used = False
    query_times: deque[float] = deque()

    app = FastAPI(
        title="Local player history",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/bootstrap/{supplied_token}", include_in_schema=False)
    async def bootstrap(request: Request, supplied_token: str):
        nonlocal bootstrap_used
        matches = hmac.compare_digest(
            supplied_token.encode("utf-8"), bootstrap_token.encode("utf-8")
        )
        if not _request_is_local(request, port) or bootstrap_used or not matches:
            return _not_found()
        bootstrap_used = True
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            _COOKIE_NAME,
            session_capability,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        if not _authorized(request, session_capability, port):
            return _not_found()
        return HTMLResponse(_page())

    @app.post("/query", response_class=HTMLResponse, include_in_schema=False)
    async def query(request: Request):
        if not _authorized(request, session_capability, port):
            return _not_found()
        origin = request.headers.get("origin")
        if origin is not None and origin not in (
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        ):
            return _not_found()

        now = time.monotonic()
        while query_times and query_times[0] <= now - _RATE_WINDOW_SEC:
            query_times.popleft()
        if len(query_times) >= _RATE_LIMIT:
            return Response(status_code=429)
        query_times.append(now)

        try:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                if not content_length.isascii() or not content_length.isdecimal():
                    return _not_found()
                if int(content_length) > _MAX_BODY_BYTES:
                    return _not_found()
            body_buffer = bytearray()
            async for chunk in request.stream():
                if len(body_buffer) + len(chunk) > _MAX_BODY_BYTES:
                    return _not_found()
                body_buffer.extend(chunk)
            body = bytes(body_buffer)
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/json":
                decoded = json.loads(body)
                if type(decoded) is not dict or set(decoded) != {"riot_id"}:
                    return _not_found()
                riot_id = decoded["riot_id"]
            elif content_type == "application/x-www-form-urlencoded":
                from urllib.parse import parse_qs

                decoded_form = parse_qs(body.decode("utf-8", "strict"), strict_parsing=True)
                if set(decoded_form) != {"riot_id"} or len(decoded_form["riot_id"]) != 1:
                    return _not_found()
                riot_id = decoded_form["riot_id"][0]
            else:
                return _not_found()
            if type(riot_id) is not str or not riot_id or len(riot_id.encode("utf-8", "strict")) > _MAX_RIOT_ID_BYTES:
                return _not_found()
            result = snapshot_handle.query(
                riot_id=riot_id,
                lookup_secret=lookup_secret,
            )
        except PlayerHistoryQueryError:
            return Response("查詢失敗。", status_code=400)
        except Exception:
            return _not_found()
        return HTMLResponse(_render_result(result))

    return app, bootstrap_token


def run_player_history_local_app(*, snapshot_path: Path, lookup_secret: bytes, port: int) -> None:
    """Print one bootstrap URL, then serve only on IPv4 loopback."""

    import click
    import uvicorn

    app, bootstrap_token = create_player_history_local_app(
        snapshot_path=snapshot_path,
        lookup_secret=lookup_secret,
        port=port,
    )
    click.echo(f"http://127.0.0.1:{port}/bootstrap/{bootstrap_token}")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        access_log=False,
        log_level="warning",
    )
