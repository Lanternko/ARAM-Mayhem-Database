"""Local bridge for the Overwolf ARAM Mayhem augment probe.

The Overwolf app receives low-CPU game-event JSON.  This bridge gives the
existing Python tools a stable local file to read while we prototype the
integration path.

Run:
    python scripts/overwolf_augment_bridge.py
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "overwolf"


def _is_fragment_offer(payload: dict[str, Any]) -> bool:
    event = payload.get("event") or {}
    if event.get("type") != "offer":
        return False
    augments = [aug for aug in event.get("augments") or [] if isinstance(aug, dict)]
    if not augments:
        return False
    names = [str(aug.get("name") or "") for aug in augments]
    return all(("碎片" in name or "fragment" in name.lower() or "shard" in name.lower()) for name in names)


class AugmentBridgeHandler(BaseHTTPRequestHandler):
    out_dir: Path = DEFAULT_OUT_DIR

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

        self._send_json(
            200,
            {
                "ok": True,
                "latest": self._latest_json("latest_augments.json"),
                "probe": self._latest_json("latest_probe.json"),
            },
        )

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

        now = datetime.now(timezone.utc).isoformat()
        record = {
            "received_at": now,
            "payload": payload,
        }

        self.out_dir.mkdir(parents=True, exist_ok=True)
        if path == "/probe-log":
            latest_name = "latest_probe.json"
            log_name = "probe_events.jsonl"
        elif _is_fragment_offer(payload):
            latest_name = "latest_fragments.json"
            log_name = "augment_events.jsonl"
        else:
            latest_name = "latest_augments.json"
            log_name = "augment_events.jsonl"

        (self.out_dir / latest_name).write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (self.out_dir / log_name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        print(f"[bridge] {path} {now} {json.dumps(payload, ensure_ascii=False)}", flush=True)
        self._send_json(200, {"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive Overwolf augment events on localhost.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    AugmentBridgeHandler.out_dir = args.out_dir.resolve()
    server = ThreadingHTTPServer((args.host, args.port), AugmentBridgeHandler)
    print(f"[bridge] listening on http://{args.host}:{args.port}", flush=True)
    print(f"[bridge] writing to {AugmentBridgeHandler.out_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
