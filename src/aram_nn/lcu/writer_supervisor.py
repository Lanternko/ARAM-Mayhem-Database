"""Process lifecycle and fair pipe servicing for :mod:`writer_transport`."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
import time
from typing import Any, Sequence

from .writer_protocol import MAX_FRAME_BYTES, ProtocolError, decode_frame
from .writer_service import WriterService
from .writer_transport import (
    STATE_CLOSED,
    STATE_DRAINING,
    STATE_READY,
    STATE_RUNNING,
    STATE_STOP_CLAIMS,
    WriterClient,
    WriterUnavailableError,
    _GlobalBudget,
    is_new_player_claim,
)

_CONTROL_MAX_BYTES = 4096
_BATCH_MAX_REQUESTS = 200
_BATCH_MAX_BYTES = 16 * 1024 * 1024
_BATCH_MAX_SECONDS = 0.025
_PRIORITY_COMMANDS = frozenset(
    {
        "player_claim",
        "game_claim",
        "commit_game",
        "release_game",
        "mark_game_done",
        "finalize_player",
        "requeue_player",
        "snowball_queue",
        "snowball_player",
    }
)
_METRIC_NAMES = (
    "frames_received",
    "frames_succeeded",
    "frames_rejected",
    "bytes_received",
    "bytes_sent",
    "channels_closed",
    "peak_pending",
)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _control(connection: Connection, value: dict[str, Any]) -> None:
    connection.send_bytes(_json_bytes(value))


def _read_control(connection: Connection) -> dict[str, Any]:
    raw = connection.recv_bytes(_CONTROL_MAX_BYTES)
    value = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError("invalid control frame")
    return value


def _error_frame(status: str, request_id: str | None = None) -> bytes:
    response: dict[str, Any] = {"ok": False, "status": status}
    if request_id:
        response["request_id"] = request_id
    return _json_bytes(response)


def _metric_add(metrics: Any, index: int, amount: int = 1) -> None:
    with metrics.get_lock():
        metrics[index] += amount


def _metric_peak(metrics: Any, pending: int) -> None:
    with metrics.get_lock():
        metrics[6] = max(int(metrics[6]), pending)


def _writer_process_main(
    db_path: str,
    channels: Sequence[tuple[str, Connection]],
    control: Connection,
    lifecycle: Any,
    metrics: Any,
    drain_timeout: float,
) -> None:
    service: WriterService | None = None
    open_channels = [True] * len(channels)
    pending: list[deque[tuple[bytes, str | None, str | None]]] = [deque() for _ in channels]
    cursor = 0
    priority_streak = 0
    drain_deadline: float | None = None
    quiet_rounds = 0
    try:
        service = WriterService(db_path)
        lifecycle.value = STATE_READY
        _control(control, {"state": "READY"})
        lifecycle.value = STATE_RUNNING

        while True:
            try:
                control_ready = control.poll(0)
            except (BrokenPipeError, EOFError, OSError):
                control_ready = False
                lifecycle.value = STATE_DRAINING
                drain_deadline = time.monotonic() + drain_timeout
            if control_ready:
                try:
                    command = _read_control(control).get("op")
                except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    command = "DRAIN"
                if command == "STOP_CLAIMS":
                    lifecycle.value = STATE_STOP_CLAIMS
                    _control(control, {"state": "STOP_CLAIMS"})
                elif command == "DRAIN":
                    lifecycle.value = STATE_DRAINING
                    drain_deadline = time.monotonic() + drain_timeout

            batch_started = time.monotonic()
            batch_count = 0
            batch_bytes = 0
            received_this_round = False
            while (
                batch_count < _BATCH_MAX_REQUESTS
                and batch_bytes < _BATCH_MAX_BYTES
                and time.monotonic() - batch_started < _BATCH_MAX_SECONDS
            ):
                # Collect at most one request per channel per pass.  Starting
                # at the moving cursor gives sparse producers prompt service.
                for offset in range(len(channels)):
                    index = (cursor + offset) % len(channels)
                    if not open_channels[index] or pending[index]:
                        continue
                    connection = channels[index][1]
                    if not connection.poll(0):
                        continue
                    try:
                        frame = connection.recv_bytes(MAX_FRAME_BYTES)
                    except (EOFError, OSError):
                        open_channels[index] = False
                        _metric_add(metrics, 5)
                        try:
                            connection.close()
                        except OSError:
                            pass
                        continue
                    request_id: str | None = None
                    command: str | None = None
                    operation = ""
                    try:
                        message = decode_frame(frame)
                        request_id = str(message["request_id"])
                        command = str(message["command"])
                        operation = str(message.get("operation") or "")
                    except ProtocolError:
                        pass
                    pending[index].append((frame, request_id, command, operation))
                    received_this_round = True
                    _metric_add(metrics, 0)
                    _metric_add(metrics, 3, len(frame))
                _metric_peak(metrics, sum(len(queue) for queue in pending))

                available = [index for index, queue in enumerate(pending) if queue]
                if not available:
                    break
                high = [index for index in available if pending[index][0][2] in _PRIORITY_COMMANDS]
                candidates = high if high and priority_streak < 8 else available
                selected = next(
                    (index for offset in range(len(channels)) for index in [(cursor + offset) % len(channels)] if index in candidates),
                    candidates[0],
                )
                frame, request_id, command, operation = pending[selected].popleft()
                cursor = (selected + 1) % max(1, len(channels))
                priority_streak = priority_streak + 1 if selected in high else 0
                connection = channels[selected][1]

                if command is None:
                    response = _error_frame("INVALID_FRAME")
                    _metric_add(metrics, 2)
                elif lifecycle.value >= STATE_STOP_CLAIMS and is_new_player_claim(
                    str(command), operation
                ):
                    response = _error_frame("CLAIMS_STOPPED", request_id)
                    _metric_add(metrics, 2)
                else:
                    try:
                        # This return is the acknowledgement boundary: the
                        # service has committed before these bytes are sent.
                        response = service.handle_bytes(frame, channel_id=channels[selected][0])
                        _metric_add(metrics, 1)
                    except Exception:
                        response = _error_frame("INTERNAL_ERROR", request_id)
                        _metric_add(metrics, 2)
                try:
                    connection.send_bytes(response)
                    _metric_add(metrics, 4, len(response))
                except (BrokenPipeError, EOFError, OSError):
                    open_channels[selected] = False
                    _metric_add(metrics, 5)
                    try:
                        connection.close()
                    except OSError:
                        pass
                batch_count += 1
                batch_bytes += len(frame)

            if lifecycle.value == STATE_DRAINING:
                any_pending = any(pending)
                any_readable = any(
                    open_channels[index] and connection.poll(0)
                    for index, (_, connection) in enumerate(channels)
                )
                quiet_rounds = 0 if any_pending or any_readable or received_this_round else quiet_rounds + 1
                if quiet_rounds >= 2 or (drain_deadline is not None and time.monotonic() >= drain_deadline):
                    break
            elif not any(open_channels):
                break

            if not received_this_round and not any(pending):
                time.sleep(0.005)
    finally:
        if service is not None:
            service.close()
        for _, connection in channels:
            try:
                connection.close()
            except OSError:
                pass
        lifecycle.value = STATE_CLOSED
        try:
            _control(control, {"state": "CLOSED"})
        except (BrokenPipeError, EOFError, OSError):
            pass
        try:
            control.close()
        except OSError:
            pass


@dataclass(frozen=True)
class WriterMetrics:
    frames_received: int
    frames_succeeded: int
    frames_rejected: int
    bytes_received: int
    bytes_sent: int
    channels_closed: int
    peak_pending: int
    reserved_requests: int
    reserved_bytes: int


class WriterSupervisor:
    """Create one writer process and a fixed set of producer clients."""

    def __init__(
        self,
        db_path: str | Path,
        worker_count: int,
        *,
        drain_timeout: float = 30.0,
        startup_timeout: float = 10.0,
        client_timeout: float | None = 30.0,
        context: multiprocessing.context.BaseContext | None = None,
    ) -> None:
        if not 1 <= int(worker_count) <= 400:
            raise ValueError("worker_count must be between 1 and 400")
        if not 0 < float(drain_timeout) <= 30.0:
            raise ValueError("drain_timeout must be in (0, 30]")
        self.db_path = str(Path(db_path))
        self.worker_count = int(worker_count)
        self.drain_timeout = float(drain_timeout)
        self.startup_timeout = float(startup_timeout)
        self.client_timeout = client_timeout
        self._context = context or multiprocessing.get_context("spawn")
        self._lifecycle = self._context.Value("i", STATE_CLOSED, lock=False)
        self._metrics = self._context.Array("Q", len(_METRIC_NAMES), lock=True)
        self._budget = _GlobalBudget(self._context)
        self._process: multiprocessing.Process | None = None
        self._control: Connection | None = None
        self._clients: tuple[WriterClient, ...] = ()

    @property
    def state(self) -> str:
        value = int(self._lifecycle.value)
        return {STATE_READY: "READY", STATE_RUNNING: "RUNNING", STATE_STOP_CLAIMS: "STOP_CLAIMS", STATE_DRAINING: "DRAINING", STATE_CLOSED: "CLOSED"}.get(value, "CLOSED")

    @property
    def clients(self) -> tuple[WriterClient, ...]:
        if not self._clients:
            raise WriterUnavailableError("WRITER_NOT_STARTED")
        return self._clients

    @property
    def is_alive(self) -> bool:
        """Whether the sole writer process is still running."""
        return self._process is not None and self._process.is_alive()

    def client(self, worker_index: int) -> WriterClient:
        return self.clients[worker_index]

    def start(self) -> tuple[WriterClient, ...]:
        if self._process is not None:
            raise RuntimeError("writer supervisor already started")
        producer_connections: list[Connection] = []
        writer_channels: list[tuple[str, Connection]] = []
        for index in range(self.worker_count):
            producer, writer = self._context.Pipe(duplex=True)
            producer_connections.append(producer)
            writer_channels.append((f"worker-{index}", writer))
        parent_control, writer_control = self._context.Pipe(duplex=True)
        self._lifecycle.value = STATE_READY
        process = self._context.Process(
            target=_writer_process_main,
            name="aram-single-db-writer",
            args=(self.db_path, writer_channels, writer_control, self._lifecycle, self._metrics, self.drain_timeout),
        )
        process.start()
        for _, connection in writer_channels:
            connection.close()
        writer_control.close()
        self._process = process
        self._control = parent_control
        self._clients = tuple(
            WriterClient(
                connection,
                channel_id=f"worker-{index}",
                lifecycle=self._lifecycle,
                budget=self._budget,
                lock=self._context.Lock(),
                default_timeout=self.client_timeout,
            )
            for index, connection in enumerate(producer_connections)
        )
        if not parent_control.poll(self.startup_timeout):
            self.abort()
            raise WriterUnavailableError("WRITER_START_TIMEOUT")
        try:
            ready = _read_control(parent_control)
        except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.abort()
            raise WriterUnavailableError("WRITER_START_FAILED") from exc
        if ready.get("state") != "READY" or not process.is_alive():
            self.abort()
            raise WriterUnavailableError("WRITER_START_FAILED")
        self._lifecycle.value = STATE_RUNNING
        return self._clients

    def stop_claims(self, *, timeout: float = 10.0) -> None:
        if self._process is None or self._control is None:
            return
        if self.state in {"STOP_CLAIMS", "DRAINING", "CLOSED"}:
            return
        self._lifecycle.value = STATE_STOP_CLAIMS
        try:
            _control(self._control, {"op": "STOP_CLAIMS"})
            if not self._control.poll(timeout):
                raise WriterUnavailableError("STOP_CLAIMS_TIMEOUT")
            response = _read_control(self._control)
        except (BrokenPipeError, EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._lifecycle.value = STATE_CLOSED
            raise WriterUnavailableError("WRITER_UNAVAILABLE") from exc
        if response.get("state") != "STOP_CLAIMS":
            raise WriterUnavailableError("INVALID_WRITER_CONTROL_RESPONSE")

    def shutdown(self, *, timeout: float | None = None) -> None:
        if self._process is None:
            self._lifecycle.value = STATE_CLOSED
            return
        if self._control is None:
            raise WriterUnavailableError("WRITER_UNAVAILABLE")
        if self.state == "RUNNING":
            self.stop_claims(timeout=min(10.0, self.drain_timeout))
        if self.state != "CLOSED":
            self._lifecycle.value = STATE_DRAINING
            try:
                _control(self._control, {"op": "DRAIN"})
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._lifecycle.value = STATE_CLOSED
                raise WriterUnavailableError("WRITER_UNAVAILABLE") from exc
        wait_seconds = self.drain_timeout + 2.0 if timeout is None else float(timeout)
        self._process.join(wait_seconds)
        if self._process.is_alive():
            raise WriterUnavailableError("WRITER_DRAIN_TIMEOUT")
        self._lifecycle.value = STATE_CLOSED
        self._close_parent_handles()

    def abort(self) -> None:
        """Terminate an unhealthy writer; clients subsequently fail closed."""
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(5.0)
        self._lifecycle.value = STATE_CLOSED
        self._close_parent_handles()

    def _close_parent_handles(self) -> None:
        for client in self._clients:
            client.close()
        if self._control is not None:
            try:
                self._control.close()
            except OSError:
                pass

    def metrics(self) -> WriterMetrics:
        with self._metrics.get_lock():
            values = [int(value) for value in self._metrics]
        reserved_count, reserved_bytes = self._budget.snapshot()
        return WriterMetrics(*values, reserved_count, reserved_bytes)

    def __enter__(self) -> "WriterSupervisor":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.shutdown()
        else:
            self.abort()


__all__ = ["WriterMetrics", "WriterSupervisor"]
