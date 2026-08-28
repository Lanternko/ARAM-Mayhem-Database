"""Isolated public process for privacy-safe player-history lookup."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import click
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .player_history_query import PlayerHistoryQueryError, PlayerHistorySnapshotHandle
from .player_history_rate_limit import SQLiteFixedWindowRateLimiter, canonical_ip
from .player_history_security import (
    MAX_RSA_KEY_BITS,
    NORMALIZER_ID,
    normalize_riot_id_v1,
    validate_secret,
)
from .player_seed_quarantine import CandidateQuarantineStore


MAX_BODY_BYTES: Final[int] = 2048
MAX_RIOT_ID_BYTES: Final[int] = 128
QUERY_PATH: Final[str] = "/api/player-history/query"


class PlayerHistoryPublicConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlayerHistoryPublicConfig:
    snapshot_path: Path
    lookup_secret: bytes = field(repr=False)
    candidate_secret: bytes = field(repr=False)
    rate_secret: bytes = field(repr=False)
    candidate_public_key: rsa.RSAPublicKey = field(repr=False)
    public_key_path: Path
    quarantine_path: Path
    rate_path: Path
    allowed_origins: frozenset[str] = frozenset()
    trusted_proxy_peers: frozenset[str] = frozenset()
    rate_limit: int = 20
    rate_window_seconds: int = 3600
    clock_ms: Callable[[], int] = field(
        default=lambda: int(time.time() * 1000), repr=False, compare=False
    )


def _canonical_origin(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "," in value
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError
    try:
        value.encode("ascii", "strict")
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except (UnicodeError, ValueError):
        raise ValueError from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise ValueError
    return value


def _looks_forbidden_database(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return (
        len(parts) >= 3 and parts[-3:] == ("data", "lcu", "games.db")
    ) or path.name.casefold() in {"games.db", "games_public.db"}


def _validate_distinct_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for supplied in paths:
        candidate = Path(supplied)
        if candidate.is_symlink():
            raise ValueError
        path = candidate.resolve(strict=False)
        if path.name.endswith(("-wal", "-shm")):
            raise ValueError
        if path in resolved:
            raise ValueError
        for prior in resolved:
            if path.exists() and prior.exists() and os.path.samefile(path, prior):
                raise ValueError
        resolved.append(path)
    return tuple(resolved)


@dataclass(slots=True)
class PlayerHistoryPublicService:
    config: PlayerHistoryPublicConfig
    handle: PlayerHistorySnapshotHandle
    limiter: SQLiteFixedWindowRateLimiter
    quarantine: CandidateQuarantineStore
    limiter_lock: threading.Lock = field(repr=False)

    @classmethod
    def create(cls, config: PlayerHistoryPublicConfig) -> "PlayerHistoryPublicService":
        try:
            if type(config) is not PlayerHistoryPublicConfig:
                raise ValueError
            secrets = tuple(
                validate_secret(value)
                for value in (
                    config.lookup_secret,
                    config.candidate_secret,
                    config.rate_secret,
                )
            )
            if len(set(secrets)) != 3:
                raise ValueError
            if (
                not isinstance(config.candidate_public_key, rsa.RSAPublicKey)
                or not 3072 <= config.candidate_public_key.key_size <= MAX_RSA_KEY_BITS
                or not callable(config.clock_ms)
            ):
                raise ValueError
            snapshot, public_key, quarantine, rate = _validate_distinct_paths(
                (
                    config.snapshot_path,
                    config.public_key_path,
                    config.quarantine_path,
                    config.rate_path,
                )
            )
            if (
                not snapshot.is_file()
                or not public_key.is_file()
                or any(_looks_forbidden_database(path) for path in (snapshot, public_key, quarantine, rate))
            ):
                raise ValueError
            loaded_key = serialization.load_pem_public_key(public_key.read_bytes())
            if not isinstance(loaded_key, rsa.RSAPublicKey):
                raise ValueError
            if loaded_key.public_numbers() != config.candidate_public_key.public_numbers():
                raise ValueError
            origins = frozenset(_canonical_origin(value) for value in config.allowed_origins)
            trusted = frozenset(canonical_ip(value) for value in config.trusted_proxy_peers)
            handle = PlayerHistorySnapshotHandle.open(snapshot)
            limiter = SQLiteFixedWindowRateLimiter(
                rate,
                config.rate_secret,
                limit=config.rate_limit,
                window_seconds=config.rate_window_seconds,
            )
            store = CandidateQuarantineStore(
                quarantine,
                config.candidate_secret,
                config.candidate_public_key,
                dataset_id=str(handle.snapshot["dataset_id"]),
                normalizer_id=NORMALIZER_ID,
            )
            validated = PlayerHistoryPublicConfig(
                snapshot_path=snapshot,
                lookup_secret=config.lookup_secret,
                candidate_secret=config.candidate_secret,
                rate_secret=config.rate_secret,
                candidate_public_key=config.candidate_public_key,
                public_key_path=public_key,
                quarantine_path=quarantine,
                rate_path=rate,
                allowed_origins=origins,
                trusted_proxy_peers=trusted,
                rate_limit=config.rate_limit,
                rate_window_seconds=config.rate_window_seconds,
                clock_ms=config.clock_ms,
            )
            return cls(validated, handle, limiter, store, threading.Lock())
        except Exception:
            raise PlayerHistoryPublicConfigurationError("invalid_configuration") from None


def _headers(request: Request, name: bytes) -> list[bytes]:
    return [value for key, value in request.scope.get("headers", ()) if key.lower() == name]


def _request_origin(request: Request, allowed: frozenset[str]) -> str | None:
    origins = _headers(request, b"origin")
    if not origins:
        return None
    if len(origins) != 1:
        raise ValueError
    try:
        origin = origins[0].decode("ascii", "strict")
        if "," in origin or _canonical_origin(origin) != origin or origin not in allowed:
            raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError from None
    return origin


def _parse_preflight(request: Request, allowed: frozenset[str]) -> str:
    origins = _headers(request, b"origin")
    if not origins:
        raise LookupError
    try:
        origin = _request_origin(request, allowed)
    except ValueError:
        raise PermissionError from None
    if origin is None:
        raise LookupError

    methods = _headers(request, b"access-control-request-method")
    requested_headers = _headers(request, b"access-control-request-headers")
    if len(methods) != 1 or len(requested_headers) != 1:
        raise LookupError
    try:
        method = methods[0].decode("ascii", "strict")
        header_text = requested_headers[0].decode("ascii", "strict")
    except UnicodeError:
        raise LookupError from None
    if method != "POST" or not header_text:
        raise LookupError

    tokens = header_text.split(",")
    normalized: list[str] = []
    allowed_token_bytes = frozenset(
        b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    )
    for raw_token in tokens:
        token = raw_token.strip(" \t")
        try:
            encoded = token.encode("ascii", "strict")
        except UnicodeError:
            raise LookupError from None
        if not encoded or any(value not in allowed_token_bytes for value in encoded):
            raise LookupError
        normalized.append(token.casefold())
    if len(normalized) != 1 or set(normalized) != {"content-type"}:
        raise LookupError
    return origin


def _apply_cors(response: Response, origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"


def _client_ip(request: Request, trusted: frozenset[str]) -> str:
    if request.client is None:
        raise ValueError
    peer = canonical_ip(request.client.host)
    if peer not in trusted:
        return peer
    forwarded = _headers(request, b"x-forwarded-for")
    if not forwarded:
        return peer
    if len(forwarded) != 1:
        raise ValueError
    try:
        value = forwarded[0].decode("ascii", "strict")
    except UnicodeError:
        raise ValueError from None
    return canonical_ip(value)


def _reject_constant(_value: str) -> object:
    raise ValueError


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _read_body(request: Request) -> bytes:
    lengths = _headers(request, b"content-length")
    if len(lengths) > 1:
        raise ValueError
    if lengths:
        try:
            text = lengths[0].decode("ascii", "strict")
            if not text.isdecimal() or int(text) > MAX_BODY_BYTES:
                raise ValueError
        except UnicodeError:
            raise ValueError from None
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > MAX_BODY_BYTES:
            raise ValueError
        buffer.extend(chunk)
    return bytes(buffer)


def _parse_query_body(body: bytes, content_types: list[bytes]) -> str:
    if len(content_types) != 1:
        raise ValueError
    try:
        content_type = content_types[0].decode("ascii", "strict")
    except UnicodeError:
        raise ValueError from None
    if content_type.lower() != "application/json":
        raise ValueError
    try:
        value = json.loads(
            body.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError from None
    if type(value) is not dict or set(value) != {"riot_id"}:
        raise ValueError
    riot_id = value["riot_id"]
    if type(riot_id) is not str:
        raise ValueError
    try:
        encoded = riot_id.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError from None
    if not encoded or len(encoded) > MAX_RIOT_ID_BYTES:
        raise ValueError
    return riot_id


def _fixed_error(status_code: int) -> JSONResponse:
    labels = {400: "bad_request", 403: "forbidden", 429: "rate_limited", 503: "unavailable"}
    return JSONResponse({"status": labels[status_code]}, status_code=status_code)


def create_player_history_public_app(config: PlayerHistoryPublicConfig) -> FastAPI:
    service = PlayerHistoryPublicService.create(config)
    app = FastAPI(
        title="Player history public service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.player_history_service = service

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            response_origin = _request_origin(request, service.config.allowed_origins)
        except ValueError:
            response_origin = None
        try:
            response = await call_next(request)
        except Exception:
            response = _fixed_error(503)
        if response_origin is not None:
            _apply_cors(response, response_origin)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.options(QUERY_PATH, include_in_schema=False)
    async def preflight(request: Request) -> Response:
        if request.scope.get("query_string", b""):
            return _fixed_error(400)
        try:
            origin = _parse_preflight(request, service.config.allowed_origins)
        except PermissionError:
            return _fixed_error(403)
        except LookupError:
            return _fixed_error(400)
        response = Response(status_code=204)
        _apply_cors(response, origin)
        response.headers["Access-Control-Allow-Methods"] = "POST"
        response.headers["Access-Control-Allow-Headers"] = "content-type"
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    @app.post(QUERY_PATH, include_in_schema=False)
    async def query(request: Request) -> Response:
        try:
            _request_origin(request, service.config.allowed_origins)
        except ValueError:
            return _fixed_error(403)
        if request.scope.get("query_string", b""):
            return _fixed_error(400)
        try:
            client_ip = _client_ip(request, service.config.trusted_proxy_peers)
        except ValueError:
            return _fixed_error(400)
        with service.limiter_lock:
            allowed, retry_after = service.limiter.charge(
                client_ip, now_ms=service.config.clock_ms()
            )
        if not allowed:
            response = _fixed_error(429)
            response.headers["Retry-After"] = str(retry_after)
            return response
        try:
            body = await _read_body(request)
            riot_id = _parse_query_body(body, _headers(request, b"content-type"))
            normalized = normalize_riot_id_v1(riot_id)
        except Exception:
            return _fixed_error(400)
        try:
            result = service.handle.query(
                riot_id=riot_id,
                lookup_secret=service.config.lookup_secret,
            )
        except PlayerHistoryQueryError as exc:
            return _fixed_error(400 if exc.code == "invalid_query" else 503)

        if result["status"] == "not_found" or (
            result["status"] == "ready" and result["low_sample"] is True
        ):
            await asyncio.to_thread(
                service.quarantine.admit,
                normalized,
                now_ms=service.config.clock_ms(),
            )
        return JSONResponse(result)

    return app


def _required_env(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value:
        raise PlayerHistoryPublicConfigurationError("invalid_configuration")
    return value


def _secret_env(environment: Mapping[str, str], name: str) -> bytes:
    try:
        text = _required_env(environment, name)
        if len(text) != 64 or text.lower() != text:
            raise ValueError
        return validate_secret(bytes.fromhex(text))
    except Exception:
        raise PlayerHistoryPublicConfigurationError("invalid_configuration") from None


def load_player_history_public_config(
    environment: Mapping[str, str] | None = None,
) -> tuple[PlayerHistoryPublicConfig, str, int]:
    env = os.environ if environment is None else environment
    try:
        public_key_path = Path(_required_env(env, "ARAM_PLAYER_HISTORY_RSA_PUBLIC_PEM"))
        key_data = public_key_path.read_bytes()
        key = serialization.load_pem_public_key(key_data)
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError
        config = PlayerHistoryPublicConfig(
            snapshot_path=Path(_required_env(env, "ARAM_PLAYER_HISTORY_SNAPSHOT")),
            lookup_secret=_secret_env(env, "ARAM_PLAYER_HISTORY_LOOKUP_SECRET_HEX"),
            candidate_secret=_secret_env(env, "ARAM_PLAYER_HISTORY_CANDIDATE_SECRET_HEX"),
            rate_secret=_secret_env(env, "ARAM_PLAYER_HISTORY_RATE_SECRET_HEX"),
            candidate_public_key=key,
            public_key_path=public_key_path,
            quarantine_path=Path(_required_env(env, "ARAM_PLAYER_HISTORY_QUARANTINE_DB")),
            rate_path=Path(_required_env(env, "ARAM_PLAYER_HISTORY_RATE_DB")),
            allowed_origins=frozenset(
                item for item in env.get("ARAM_PLAYER_HISTORY_ALLOWED_ORIGINS", "").split(",") if item
            ),
            trusted_proxy_peers=frozenset(
                item for item in env.get("ARAM_PLAYER_HISTORY_TRUSTED_PROXY_PEERS", "").split(",") if item
            ),
            rate_limit=int(env.get("ARAM_PLAYER_HISTORY_RATE_LIMIT_PER_HOUR", "20")),
        )
        host = env.get("ARAM_PLAYER_HISTORY_HOST", "127.0.0.1")
        port = int(env.get("ARAM_PLAYER_HISTORY_PORT", "8766"))
        if not host or not 1 <= port <= 65535:
            raise ValueError
        return config, host, port
    except PlayerHistoryPublicConfigurationError:
        raise
    except Exception:
        raise PlayerHistoryPublicConfigurationError("invalid_configuration") from None


@click.command("player-history-public")
def main() -> None:
    """Run the isolated public service from fail-closed environment config."""

    try:
        config, host, port = load_player_history_public_config()
        app = create_player_history_public_app(config)
    except PlayerHistoryPublicConfigurationError as exc:
        raise click.ClickException(str(exc)) from None
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        limit_concurrency=32,
        backlog=64,
        workers=1,
    )
