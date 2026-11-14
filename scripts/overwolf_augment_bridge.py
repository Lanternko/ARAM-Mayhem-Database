"""Standalone receiver for the Overwolf ARAM Mayhem augment probe.

The recommender GUI already embeds this server; run this only when you want
augment events captured without the GUI open.

Run:
    python scripts/overwolf_augment_bridge.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from aram_nn.augment_bridge import DEFAULT_HOST, DEFAULT_PORT, make_server  # noqa: E402

DEFAULT_OUT_DIR = REPO_ROOT / "data" / "overwolf"


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive Overwolf augment events on localhost.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--probe-log", action="store_true",
                        help="also persist every raw GEP update to probe_events.jsonl")
    args = parser.parse_args()

    server = make_server(args.out_dir, args.host, args.port, persist_probe_log=args.probe_log or None)
    print(f"[bridge] listening on http://{args.host}:{args.port}", flush=True)
    print(f"[bridge] writing to {args.out_dir.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
