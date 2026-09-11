"""Build docs/api/augment-pools.json (Mayhem augment pools) from CommunityDragon.

Pool data only changes when Riot ships a patch, so this runs once per patch,
not on every data publish:

    python scripts/build_augment_pools.py                 # latest vs previous patch
    python scripts/build_augment_pools.py --version 16.18 --prev 16.17
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from aram_nn.site.augment_pools import META_PATH, build_payload, fetch_json  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "api" / "augment-pools.json"


def resolve_patch(version: str) -> str:
    """CDragon path segment -> "major.minor" patch label (e.g. "16.18")."""
    if version != "latest":
        return version
    meta = fetch_json("latest", META_PATH)
    return ".".join(str(meta["version"]).split(".")[:2])


def previous_patch(patch: str) -> str | None:
    major, minor = (int(x) for x in patch.split(".")[:2])
    return f"{major}.{minor - 1}" if minor > 1 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="latest", help="CDragon version segment (default: latest)")
    ap.add_argument("--prev", default="auto", help="previous version to diff against; 'auto' or 'none'")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    patch = resolve_patch(args.version)
    prev = previous_patch(patch) if args.prev == "auto" else (None if args.prev == "none" else args.prev)
    payload = build_payload(fetch_json, args.version, prev)
    payload["patch"] = patch

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    n_res = sum(1 for p in payload["pools"] if p["hash"] and p["name"])
    n_hash = sum(1 for p in payload["pools"] if p["hash"])
    print(
        f"wrote {args.out} patch={patch} prev={prev} pools={len(payload['pools'])} "
        f"champs={len(payload['champs'])} hashed_resolved={n_res}/{n_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
