"""Build docs/api/augment-pools.json (Mayhem augment pools) from CommunityDragon.

Pool data only changes when Riot ships a patch, so this runs once per patch,
not on every data publish:

    python scripts/build_augment_pools.py                 # latest vs previous patch
    python scripts/build_augment_pools.py --version 16.18 --prev 16.17

From 16.19 the client ships empty pool files, so ``latest`` fails; pin the last
patch that has them and the page is marked as frozen at that patch.

With games.db present, the pools are checked against real Mayhem games of this
patch and the previous one: stale augments are dropped and champion-specific
server blocks are published (see ``aram_nn.site.augment_pool_observed``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from aram_nn.site.augment_pool_observed import count_patch  # noqa: E402
from aram_nn.site.augment_pools import (  # noqa: E402
    GROUPS_PATH,
    META_PATH,
    build_payload,
    fetch_json,
    parse_pools,
    public_payload,
)

DEFAULT_OUT = REPO / "docs" / "api" / "augment-pools.json"
DEFAULT_DB = REPO / "data" / "lcu" / "games.db"


def resolve_patch(version: str) -> str:
    """CDragon path segment -> "major.minor" patch label (e.g. "16.18")."""
    if version != "latest":
        return version
    meta = fetch_json("latest", META_PATH)
    return ".".join(str(meta["version"]).split(".")[:2])


def frozen_since(version: str) -> str | None:
    """Patch label of CDragon latest when it ships no pools but ``version`` does.

    From 16.19 Riot stopped shipping the pool tables in the client, so the page
    has to say its data stops at the last patch that had them.
    """
    if version == "latest" or parse_pools(fetch_json("latest", GROUPS_PATH)):
        return None
    return resolve_patch("latest")


def previous_patch(patch: str) -> str | None:
    major, minor = (int(x) for x in patch.split(".")[:2])
    return f"{major}.{minor - 1}" if minor > 1 else None


def attach_descriptions(augs: dict) -> int:
    """Add zh/en description text for the page's hover tip.

    The tier-list payload only has augments that have been picked, so a few pool
    augments need their own text. Uses the site's resolver (latest CDragon
    strings), cached outside the repo. Returns how many got a zh description.
    """
    import tempfile

    import tierlist_engine as te  # scripts/ is sys.path[0] when run as a script

    cache = Path(tempfile.gettempdir()) / "arammeta-augment-pools"
    cache.mkdir(parents=True, exist_ok=True)
    zh = te.load_augment_descriptions(cache, locale="zh_tw", cache_name="lol_stringtable_zh_tw.json")
    zh.update(getattr(te, "AUGMENT_DESC_OVERRIDES", {}))
    en = te.load_augment_descriptions(cache, locale="en_us", cache_name="lol_stringtable_en_us.json")
    for aid, row in augs.items():
        row["desc_zh"] = zh.get(int(aid), "")
        row["desc_en"] = en.get(int(aid), "")
    return sum(1 for row in augs.values() if row["desc_zh"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="latest", help="CDragon version segment (default: latest)")
    ap.add_argument("--prev", default="auto", help="previous version to diff against; 'auto' or 'none'")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--observed-db", type=Path, default=DEFAULT_DB,
                    help="games.db to check pools against (a worktree has none: pass the primary checkout's)")
    ap.add_argument("--no-observed", action="store_true", help="publish the raw file pools")
    ap.add_argument("--queue", type=int, default=2400)
    args = ap.parse_args(argv)

    patch = resolve_patch(args.version)
    prev = previous_patch(patch) if args.prev == "auto" else (None if args.prev == "none" else args.prev)
    observed = None
    if args.no_observed:
        pass
    elif not args.observed_db.exists():
        print(f"WARNING: {args.observed_db} missing; publishing unchecked pools", file=sys.stderr)
    else:
        counts = [count_patch(args.observed_db, args.queue, p) if p else None for p in (patch, prev)]
        observed = {
            "cur": counts[0],
            "prev": counts[1],
            "patches": [p for p in (patch, prev) if p],
            "games": sum(sum(c.games.values()) for c in counts if c) // 10,
        }
        print(f"observed: {observed['games']} games in {observed['patches']} (queue {args.queue})")
    internal = build_payload(fetch_json, args.version, prev, observed=observed)
    payload = public_payload(internal)  # the page must not show raw pool names
    payload["patch"] = patch
    since = frozen_since(args.version)
    if since:
        payload["frozen"] = {"since": since}
        print(f"frozen: CDragon latest ({since}) ships no pools; page will say data stops at {patch}")
    n_desc = attach_descriptions(payload["augs"])
    print(f"descriptions: {n_desc}/{len(payload['augs'])} augments")
    obs = payload.get("observed")
    if obs:
        print(
            f"observed: dead={len(obs['dead'])} ({', '.join(d['en'] for d in obs['dead'])}) "
            f"blocked_pairs={sum(len(v) for v in obs['blocked'].values())} over {len(obs['blocked'])} champs"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    n_res = sum(1 for p in internal["pools"] if p["hash"] and p["name"])
    n_hash = sum(1 for p in internal["pools"] if p["hash"])
    print(
        f"wrote {args.out} patch={patch} prev={prev} pools={len(payload['pools'])} "
        f"champs={len(payload['champs'])} hashed_resolved={n_res}/{n_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
