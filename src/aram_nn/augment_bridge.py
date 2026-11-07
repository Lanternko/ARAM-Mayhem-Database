"""Localhost receiver for the Overwolf ARAM: Mayhem augment probe.

Why Overwolf: the in-game augment offer only exists inside the game process.
Blitz reads it with a Riot-allowlisted DLL injected into ``League of
Legends.exe`` (it walks the ``KiwiAugmentSelection`` HUD tree with per-patch
offsets).  Doing that ourselves would trip Vanguard.  Overwolf's Game Events
Provider performs the same sanctioned read and republishes it to any Overwolf
app as the ``augments`` / ``picked_augment`` keys, so the probe app in
``overwolf/augment-probe`` forwards those keys here.

The recommender embeds this server (``start_bridge_thread``) so the receiver is
alive whenever the GUI is open; ``scripts/overwolf_augment_bridge.py`` runs it
standalone.  Event volume is tiny (a few offers per game), so the cost is one
idle socket.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
# /probe-log receives every raw GEP info update.  Persisting it grew
# probe_events.jsonl to 15 MB in two days, so it is opt-in.
PROBE_LOG_ENV = "ARAM_OVERWOLF_PROBE_LOG"


def is_fragment_offer(payload: dict[str, Any]) -> bool:
    event = payload.get("event") or {}
    if not isinstance(event, dict) or event.get("type") != "offer":
        return False
    augments = [aug for aug in event.get("augments") or [] if isinstance(aug, dict)]
    if not augments:
        return False
    names = [str(aug.get("name") or "") for aug in augments]
    return all(("碎片" in name or "fragment" in name.lower() or "shard" in name.lower()) for name in names)


class AugmentBridgeHandler(BaseHTTPRequestHandler):
    out_dir: Path = Path("data") / "overwolf"
    persist_probe_log: bool = False

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler name
        self._send_json(200, {"ok": True})

    def _latest_json(self, filename: str) -> Any:
        latest_path = self.out_dir / filename
        if not latest_path.exists():
            return None
        try:
            return json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path.rstrip("/") not in {"", "/latest", "/health"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        self._send_json(200, {
            "ok": True,
            "service": "aram-augment-bridge",
            "latest": self._latest_json("latest_augments.json"),
            "probe": self._latest_json("latest_probe.json"),
        })

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path.rstrip("/")
        if path not in {"/augment-event", "/probe-log"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"bad_json: {exc}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "payload_not_object"})
            return

        record = {"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if path == "/probe-log":
            _write_json(self.out_dir / "latest_probe.json", record)
            if self.persist_probe_log:
                _append_jsonl(self.out_dir / "probe_events.jsonl", record)
        else:
            record_augment_event(self.out_dir, payload, record)
        self._send_json(200, {"ok": True})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        return


def record_augment_event(out_dir: Path, payload: dict[str, Any], record: dict | None = None) -> dict:
    """Persist one augment event where the GUI poller reads it.

    Shared by the Overwolf bridge and the screen-OCR watcher so both sources
    produce the same record shape.
    """
    if record is None:
        record = {"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    out_dir.mkdir(parents=True, exist_ok=True)
    latest_name = "latest_fragments.json" if is_fragment_offer(payload) else "latest_augments.json"
    _write_json(out_dir / latest_name, record)
    _append_jsonl(out_dir / "augment_events.jsonl", record)
    return record


def _write_json(path: Path, record: dict) -> None:
    # Write-then-replace so the GUI poller never reads a half-written file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Fail to bind when the port is taken.

    HTTPServer enables SO_REUSEADDR, which on Windows lets a second process bind
    the same port and silently split the Overwolf posts between two receivers.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(
    out_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    persist_probe_log: bool | None = None,
) -> ThreadingHTTPServer:
    if persist_probe_log is None:
        persist_probe_log = os.environ.get(PROBE_LOG_ENV, "").strip() not in {"", "0", "false"}
    handler = type("BoundAugmentBridgeHandler", (AugmentBridgeHandler,), {
        "out_dir": Path(out_dir).resolve(),
        "persist_probe_log": bool(persist_probe_log),
    })
    return _ExclusiveHTTPServer((host, port), handler)


def start_bridge_thread(
    out_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer | None:
    """Serve the bridge on a daemon thread.

    Returns None when the port is already bound — normally a standalone bridge
    or another recommender window, either of which writes the same files.
    """
    try:
        server = make_server(out_dir, host, port)
    except OSError:
        return None
    thread = threading.Thread(target=server.serve_forever, name="augment-bridge", daemon=True)
    thread.start()
    return server
