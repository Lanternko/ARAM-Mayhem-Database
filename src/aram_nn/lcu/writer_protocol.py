"""Strict wire protocol used by the single SQLite writer.

The writer is deliberately a very small boundary.  Messages are UTF-8 JSON
objects and are validated before they reach any database code.  In particular,
this module never attempts Python object deserialisation (``pickle`` and the
like are not part of the protocol).

``channel_id`` is transport state and is intentionally not represented in a
frame.  The caller passes it to :class:`~writer_service.WriterService` out of
band.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 16
MAX_STRING_BYTES = 64 * 1024
MAX_ARRAY_ITEMS = 512
MAX_OBJECT_FIELDS = 64
MAX_TOTAL_ITEMS = 4096
PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """A safe protocol error.

    Error text is intentionally generic.  It must be safe to return to a
    remote client and therefore never includes the offending frame or values.
    ``code`` is stable enough for callers to classify malformed requests.
    """

    def __init__(self, code: str = "INVALID_FRAME") -> None:
        self.code = str(code)
        super().__init__(self.code)


_META_FIELDS = frozenset({"version", "command", "request_id"})
_COMMAND_FIELDS: dict[str, frozenset[str]] = {
    # Claims use a natural item key and an optional caller supplied clock.  The
    # writer returns the lease token and generation; those values are never
    # accepted from a claim request.
    "player_claim": frozenset({"version", "command", "request_id", "puuid", "now_ms", "lease_ms"}),
    "game_claim": frozenset({"version", "command", "request_id", "game_id", "now_ms", "lease_ms"}),
    "commit_game": frozenset(
        {
            "version",
            "command",
            "request_id",
            "game_id",
            "token",
            "generation",
            "record",
            "participants",
            "participants_private",
            "participant_puuids",
            "captured_at",
            "watermark",
            "now_ms",
        }
    ),
    "release_game": frozenset(
        {"version", "command", "request_id", "game_id", "token", "generation", "now_ms"}
    ),
    "mark_game_done": frozenset(
        {"version", "command", "request_id", "game_id", "token", "generation", "now_ms"}
    ),
    "finalize_player": frozenset(
        {
            "version",
            "command",
            "request_id",
            "puuid",
            "token",
            "generation",
            "new_games_found",
            "claimed_match_created_ms",
            "observed_match_created_ms",
            "requeue_cooldown_ms",
            "new_games_by_queue",
            "source",
            "seed_family",
            "worker_id",
            "current_patch",
            "history_game_count",
            "target_game_count",
            "claim_lane",
            "classic_affinity",
            "classic_rank",
            "classic_games_24h",
            "classic_games_recent",
            "classic_last_seen_ms",
            "classic_revisit_interval_ms",
            "classic_revisit_min_ms",
            "now_ms",
        }
    ),
    "requeue_player": frozenset(
        {
            "version",
            "command",
            "request_id",
            "puuid",
            "token",
            "generation",
            "delay_ms",
            "now_ms",
            "reason",
        }
    ),
    # Snowball uses a fixed capability surface rather than accepting arbitrary
    # SQL/table names from a producer.  ``operation`` is validated by the
    # writer for each command and the field sets below are intentionally
    # bounded to the values needed by the crawler.
    "snowball_init": frozenset({
        "version", "command", "request_id", "claim_timeout_ms", "worker_id",
    }),
    "snowball_runtime": frozenset({
        "version", "command", "request_id", "operation", "key", "value",
        "seed_family", "delta",
    }),
    "snowball_queue": frozenset({
        "version", "command", "request_id", "operation", "puuid", "depth",
        "source", "priority", "discovered_from_game_id",
        "discovered_match_created_ms", "requeue", "eligible_at_ms",
        "seed_family", "discovered_queue_id", "classic_affinity_rank",
        "requeue_cooldown_ms", "initial_delay_ms", "classic_revisit_min_ms",
        "classic_claim_percent", "claim_timeout_ms", "cap", "cooldown_ms",
        "sources", "now_ms", "worker_id",
    }),
    "snowball_bridge": frozenset({
        "version", "command", "request_id", "operation", "public_puuid",
        "riot_id", "lcu_puuid", "resolve_status",
    }),
    "snowball_player": frozenset({
        "version", "command", "request_id", "operation", "puuid", "token",
        "generation", "delay_ms", "now_ms", "new_games_found",
        "claimed_match_created_ms", "observed_match_created_ms",
        "requeue_cooldown_ms", "new_games_by_queue", "source", "seed_family",
        "worker_id", "current_patch", "history_game_count", "target_game_count",
        "claim_lane", "classic_affinity", "classic_rank", "classic_games_24h",
        "classic_games_recent", "classic_last_seen_ms",
        "classic_revisit_interval_ms", "classic_revisit_min_ms", "reason",
    }),
    "ping": frozenset({"version", "command", "request_id"}),
    "ready": frozenset({"version", "command", "request_id"}),
    "shutdown": frozenset({"version", "command", "request_id"}),
}

_COMMAND_ALIASES = {
    "player-claim": "player_claim",
    "game-claim": "game_claim",
    "commit-game": "commit_game",
    "release-game": "release_game",
    "finalize-player": "finalize_player",
    "requeue-player": "requeue_player",
}

_COMMON_STRING_FIELDS = {
    "command",
    "request_id",
    "puuid",
    "game_id",
    "token",
    "captured_at",
    "source",
    "seed_family",
    "worker_id",
    "current_patch",
    "claim_lane",
    "classic_affinity",
    "reason",
    "operation",
    "public_puuid",
    "riot_id",
    "lcu_puuid",
    "resolve_status",
}
_COMMON_INT_FIELDS = {
    "version",
    "now_ms",
    "lease_ms",
    "generation",
    "new_games_found",
    "claimed_match_created_ms",
    "observed_match_created_ms",
    "requeue_cooldown_ms",
    "history_game_count",
    "target_game_count",
    "classic_revisit_interval_ms",
    "delay_ms",
    "claim_timeout_ms",
    "priority",
    "depth",
    "discovered_match_created_ms",
    "eligible_at_ms",
    "discovered_queue_id",
    "classic_affinity_rank",
    "requeue_cooldown_ms",
    "initial_delay_ms",
    "classic_revisit_min_ms",
    "classic_claim_percent",
    "cap",
    "cooldown_ms",
    "delta",
    "classic_rank",
    "classic_games_24h",
    "classic_games_recent",
    "classic_last_seen_ms",
}


def _fail(code: str = "INVALID_FRAME") -> None:
    raise ProtocolError(code)


def _is_int(value: Any) -> bool:
    # bool is an int subclass, but accepting it in a wire schema is almost
    # always a caller bug and makes validation ambiguous.
    return isinstance(value, int) and not isinstance(value, bool)


def _check_string(value: Any, *, max_bytes: int = MAX_STRING_BYTES) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > max_bytes:
        _fail("INVALID_TYPE")


def _walk(value: Any, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    if depth > MAX_DEPTH:
        _fail("LIMIT_EXCEEDED")
    counter[0] += 1
    if counter[0] > MAX_TOTAL_ITEMS:
        _fail("LIMIT_EXCEEDED")
    if isinstance(value, str):
        _check_string(value)
    elif isinstance(value, Mapping):
        if len(value) > MAX_OBJECT_FIELDS:
            _fail("LIMIT_EXCEEDED")
        for key, child in value.items():
            _check_string(key, max_bytes=1024)
            _walk(child, depth + 1, counter)
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_ARRAY_ITEMS:
            _fail("LIMIT_EXCEEDED")
        for child in value:
            _walk(child, depth + 1, counter)
    elif value is None or isinstance(value, (bool, int, float)):
        # JSON permits these scalar values.  NaN/Infinity are rejected by the
        # decoder below, so no non-standard number can enter this path.
        return
    else:
        _fail("INVALID_TYPE")


def _check_exact_fields(message: Mapping[str, Any], command: str) -> None:
    allowed = _COMMAND_FIELDS.get(command)
    if allowed is None:
        _fail("UNKNOWN_COMMAND")
    if set(message) - allowed:
        _fail("UNKNOWN_FIELD")
    # Metadata are mandatory for every command.  Optional command fields have
    # command-specific checks below.
    if not _META_FIELDS.issubset(message):
        _fail("MISSING_FIELD")


def _check_optional_string(message: Mapping[str, Any], key: str, *, max_bytes: int = MAX_STRING_BYTES) -> None:
    if key in message:
        _check_string(message[key], max_bytes=max_bytes)


def _check_optional_int(message: Mapping[str, Any], key: str, *, minimum: int | None = None) -> None:
    if key in message:
        value = message[key]
        if not _is_int(value) or (minimum is not None and value < minimum):
            _fail("INVALID_TYPE")


def _check_command_values(message: Mapping[str, Any], command: str) -> None:
    version = message.get("version")
    if not _is_int(version) or version != PROTOCOL_VERSION:
        _fail("UNSUPPORTED_VERSION")
    _check_string(message.get("command"), max_bytes=64)
    _check_string(message.get("request_id"), max_bytes=256)
    if not message["request_id"]:
        _fail("INVALID_TYPE")

    for key in _COMMON_STRING_FIELDS & set(message):
        _check_optional_string(message, key, max_bytes=4096 if key in {"puuid", "game_id", "token"} else MAX_STRING_BYTES)
    for key in _COMMON_INT_FIELDS & set(message):
        _check_optional_int(message, key)

    if command in {"finalize_player", "requeue_player"}:
        if not isinstance(message.get("puuid"), str) or not message["puuid"]:
            _fail("MISSING_FIELD" if "puuid" not in message else "INVALID_TYPE")
    if command == "player_claim" and "puuid" in message:
        if not isinstance(message["puuid"], str) or not message["puuid"]:
            _fail("INVALID_TYPE")
    if command in {"game_claim", "commit_game", "release_game", "mark_game_done"}:
        if not isinstance(message.get("game_id"), str) or not message["game_id"]:
            _fail("MISSING_FIELD" if "game_id" not in message else "INVALID_TYPE")
    if command in {"commit_game", "release_game", "mark_game_done", "finalize_player", "requeue_player"}:
        if not isinstance(message.get("token"), str) or not message["token"]:
            _fail("MISSING_FIELD" if "token" not in message else "INVALID_TYPE")
        if not _is_int(message.get("generation")) or message["generation"] < 1:
            _fail("MISSING_FIELD" if "generation" not in message else "INVALID_TYPE")
    if command == "commit_game":
        record = message.get("record")
        if record is not None and not isinstance(record, Mapping):
            _fail("INVALID_TYPE")
        for key in ("participants", "participants_private", "participant_puuids"):
            if key in message and not isinstance(message[key], list):
                _fail("INVALID_TYPE")
        if "watermark" in message and not isinstance(message["watermark"], Mapping):
            _fail("INVALID_TYPE")
    if command == "finalize_player":
        if "new_games_by_queue" in message and not isinstance(message["new_games_by_queue"], Mapping):
            _fail("INVALID_TYPE")
    if command in {"player_claim", "game_claim"}:
        _check_optional_int(message, "lease_ms", minimum=1)
    if command == "requeue_player":
        _check_optional_int(message, "delay_ms", minimum=0)
    if command.startswith("snowball_") and command != "snowball_init":
        if not isinstance(message.get("operation"), str) or not message["operation"]:
            _fail("MISSING_FIELD" if "operation" not in message else "INVALID_TYPE")
        if command == "snowball_runtime" and message.get("operation") in {"get", "set", "delete"}:
            if not isinstance(message.get("key"), str) or not message["key"]:
                _fail("MISSING_FIELD" if "key" not in message else "INVALID_TYPE")
        if command == "snowball_queue" and message.get("operation") in {"enqueue", "upsert", "suggested_reseed"}:
            if not isinstance(message.get("puuid"), str) or not message["puuid"]:
                _fail("MISSING_FIELD" if "puuid" not in message else "INVALID_TYPE")


def validate_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a plain JSON-compatible message.

    A copy is returned so callers cannot mutate a validated object while it is
    being processed.  Unknown fields are rejected at the top level and in the
    command payload where command-specific records are used by the writer.
    """
    if not isinstance(message, Mapping):
        _fail("INVALID_TYPE")
    try:
        _walk(message)
    except ProtocolError:
        raise
    except Exception:
        _fail()
    raw_command = message.get("command")
    if not isinstance(raw_command, str):
        _fail("MISSING_FIELD" if "command" not in message else "INVALID_TYPE")
    command = _COMMAND_ALIASES.get(raw_command, raw_command)
    if command != raw_command:
        # Keep one canonical command spelling on the wire.  This also prevents
        # an alias from creating a second idempotency namespace.
        _fail("UNKNOWN_COMMAND")
    _check_exact_fields(message, command)
    _check_command_values(message, command)
    try:
        return json.loads(json.dumps(dict(message), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError):
        _fail("INVALID_TYPE")


def _reject_constant(value: str) -> Any:
    raise ProtocolError("INVALID_JSON")


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode one strict JSON frame, rejecting malformed or oversized input."""
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        _fail("INVALID_TYPE")
    payload = bytes(frame)
    if len(payload) > MAX_FRAME_BYTES:
        _fail("FRAME_TOO_LARGE")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        _fail("INVALID_JSON")
    if not isinstance(value, dict):
        _fail("INVALID_TYPE")
    return validate_message(value)


def encode_frame(message: Mapping[str, Any]) -> bytes:
    """Validate and encode a message as compact UTF-8 JSON bytes."""
    validated = validate_message(message)
    try:
        payload = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("INVALID_TYPE")
    if len(payload) > MAX_FRAME_BYTES:
        _fail("FRAME_TOO_LARGE")
    return payload


# Friendly aliases used by transport adapters and tests.
serialize_frame = encode_frame
parse_frame = decode_frame
validate_frame = decode_frame


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_DEPTH",
    "ProtocolError",
    "PROTOCOL_VERSION",
    "decode_frame",
    "encode_frame",
    "parse_frame",
    "serialize_frame",
    "validate_frame",
    "validate_message",
]
