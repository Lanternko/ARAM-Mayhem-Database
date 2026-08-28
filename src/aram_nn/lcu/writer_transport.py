"""Bounded byte-only transport for the single SQLite writer process.

Each producer owns one inherited duplex ``multiprocessing.Connection``.  The
transport deliberately uses only ``send_bytes``/``recv_bytes``; channel
identity is assigned by the parent and never read from a request frame.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import multiprocessing
from multiprocessing.connection import Connection
from typing import Any, Mapping

from .writer_protocol import MAX_FRAME_BYTES, encode_frame

MAX_UNACKNOWLEDGED_PER_WORKER = 16
MAX_QUEUED_PER_WORKER = 200
MAX_QUEUED_BYTES_PER_WORKER = 16 * 1024 * 1024
MAX_QUEUED_GLOBAL = 400
MAX_QUEUED_BYTES_GLOBAL = 32 * 1024 * 1024

STATE_READY = 0
STATE_RUNNING = 1
STATE_STOP_CLAIMS = 2
STATE_DRAINING = 3
STATE_CLOSED = 4

_STATE_NAMES = {
    STATE_READY: "READY",
    STATE_RUNNING: "RUNNING",
    STATE_STOP_CLAIMS: "STOP_CLAIMS",
    STATE_DRAINING: "DRAINING",
    STATE_CLOSED: "CLOSED",
}
def is_new_player_claim(command: str, operation: str = "") -> bool:
    """True for requests that take a new player off the frontier.

    ``game_claim`` is not included: after STOP_CLAIMS a producer must still
    finish games for a player it already holds.
    """
    if command == "player_claim":
        return True
    return command == "snowball_queue" and operation == "claim_next"


class WriterTransportError(RuntimeError):
    """Base class for transport failures safe to expose to a producer."""


class WriterUnavailableError(WriterTransportError):
    """The writer cannot return a trustworthy acknowledgement."""


class WriterBackpressureError(WriterTransportError):
    """A bounded transport capacity limit was reached."""


class WriterLifecycleError(WriterTransportError):
    """A request is not allowed in the current lifecycle state."""


class _GlobalBudget:
    """A process-shared exact count/byte reservation."""

    def __init__(self, context: multiprocessing.context.BaseContext) -> None:
        self._lock = context.Lock()
        self._count = context.Value("I", 0, lock=False)
        self._bytes = context.Value("Q", 0, lock=False)

    def reserve(self, size: int) -> bool:
        with self._lock:
            if self._count.value >= MAX_QUEUED_GLOBAL:
                return False
            if self._bytes.value + size > MAX_QUEUED_BYTES_GLOBAL:
                return False
            self._count.value += 1
            self._bytes.value += size
            return True

    def release(self, size: int) -> None:
        with self._lock:
            self._count.value = max(0, self._count.value - 1)
            self._bytes.value = max(0, self._bytes.value - size)

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return int(self._count.value), int(self._bytes.value)


@dataclass(frozen=True)
class ClientLimits:
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_unacknowledged: int = MAX_UNACKNOWLEDGED_PER_WORKER
    max_queued: int = MAX_QUEUED_PER_WORKER
    max_queued_bytes: int = MAX_QUEUED_BYTES_PER_WORKER


class WriterClient:
    """Synchronous fail-closed client for one parent-bound writer channel.

    Calls are serialized even when a producer uses the object from several
    threads.  Consequently this API keeps at most one request unacknowledged
    on its channel, below all per-worker queue ceilings.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        channel_id: str,
        lifecycle: Any,
        budget: _GlobalBudget,
        lock: Any,
        default_timeout: float | None = 30.0,
    ) -> None:
        self._connection = connection
        self._channel_id = channel_id
        self._lifecycle = lifecycle
        self._budget = budget
        self._lock = lock
        self._default_timeout = default_timeout
        self._failed = False
        self.limits = ClientLimits()

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def lifecycle_state(self) -> str:
        return _STATE_NAMES.get(int(self._lifecycle.value), "CLOSED")

    @property
    def closed(self) -> bool:
        return self._failed or self.lifecycle_state == "CLOSED"

    def submit(
        self,
        message: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        frame = encode_frame(message)
        command = str(message.get("command", ""))
        expected_request_id = str(message.get("request_id", ""))
        return self._exchange(
            frame,
            command=command,
            operation=str(message.get("operation") or ""),
            expected_request_id=expected_request_id,
            timeout=timeout,
        )

    call = submit
    request = submit

    def _exchange(
        self,
        frame: bytes,
        *,
        command: str,
        expected_request_id: str | None,
        timeout: float | None,
        operation: str = "",
    ) -> dict[str, Any]:
        if len(frame) > MAX_FRAME_BYTES:
            raise WriterBackpressureError("FRAME_TOO_LARGE")
        if self._failed:
            raise WriterUnavailableError("WRITER_UNAVAILABLE")
        state = int(self._lifecycle.value)
        if state >= STATE_DRAINING:
            raise WriterLifecycleError("WRITER_DRAINING")
        if state >= STATE_STOP_CLAIMS and is_new_player_claim(command, operation):
            raise WriterLifecycleError("CLAIMS_STOPPED")
        wait_seconds = self._default_timeout if timeout is None else timeout
        if wait_seconds is not None and wait_seconds < 0:
            raise ValueError("timeout must be non-negative or None")

        # The multiprocessing lock keeps an inherited client safe if one
        # producer fans work out to several local threads.
        with self._lock:
            if not self._budget.reserve(len(frame)):
                raise WriterBackpressureError("GLOBAL_QUEUE_LIMIT")
            try:
                self._connection.send_bytes(frame)
                if wait_seconds is not None and not self._connection.poll(wait_seconds):
                    self._fail_closed()
                    raise WriterUnavailableError("WRITER_RESPONSE_TIMEOUT")
                response_frame = self._connection.recv_bytes(MAX_FRAME_BYTES)
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._fail_closed()
                raise WriterUnavailableError("WRITER_UNAVAILABLE") from exc
            finally:
                # A reservation describes an unacknowledged producer request.
                # It is no longer reusable after success or fail-closed loss.
                self._budget.release(len(frame))

        try:
            response = json.loads(response_frame.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._fail_closed()
            raise WriterUnavailableError("INVALID_WRITER_RESPONSE") from exc
        if not isinstance(response, dict):
            self._fail_closed()
            raise WriterUnavailableError("INVALID_WRITER_RESPONSE")
        if expected_request_id and response.get("request_id") != expected_request_id:
            self._fail_closed()
            raise WriterUnavailableError("MISMATCHED_WRITER_RESPONSE")
        return response

    def _fail_closed(self) -> None:
        self._failed = True
        try:
            self._connection.close()
        except OSError:
            pass

    def close(self) -> None:
        self._fail_closed()

    def __enter__(self) -> "WriterClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "ClientLimits",
    "MAX_QUEUED_BYTES_GLOBAL",
    "MAX_QUEUED_BYTES_PER_WORKER",
    "MAX_QUEUED_GLOBAL",
    "MAX_QUEUED_PER_WORKER",
    "MAX_UNACKNOWLEDGED_PER_WORKER",
    "STATE_CLOSED",
    "STATE_DRAINING",
    "STATE_READY",
    "STATE_RUNNING",
    "STATE_STOP_CLAIMS",
    "WriterBackpressureError",
    "WriterClient",
    "WriterLifecycleError",
    "WriterTransportError",
    "WriterUnavailableError",
    "is_new_player_claim",
]
