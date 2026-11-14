"""Build docs/api/augment-pools.json (Mayhem augment pools) from CommunityDragon.

Pool data only changes when Riot ships a patch, so this runs once per patch,
not on every data publish:

    python scripts/build_augment_pools.py                 # latest vs previous patch
    python scripts/build_augment_pools.py --version 16.18 --prev 16.17

From 16.19 the client ships empty pool files, so ``latest`` fails; pin the last
patch that has them and the page is marked as frozen at that patch.  Check the
frozen pools against current games so removed augments drop out and augments
added since then are listed with the champions seen holding them:

    python scripts/build_augment_pools.py --version 16.18 --prev 16.17         --observed-patch latest --observed-db D:/Projects/CODING/aram-winrate-nn/data/lcu/games.db

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
    """First patch after ``version`` that ships no pools, when latest ships none.

    From 16.19 Riot stopped shipping the pool tables in the client, so the page
    has to say its data stops at the last patch that had them.  The label is
    the first empty patch, not CDragon latest, so it stays put across patches.
    """
    if version == "latest" or parse_pools(fetch_json("latest", GROUPS_PATH)):
        return None
    latest = resolve_patch("latest")
    major, minor = (int(x) for x in version.split(".")[:2])
    while f"{major}.{minor + 1}" != latest:
        try:
            if not parse_pools(fetch_json(f"{major}.{minor + 1}", GROUPS_PATH)):
                break
        except Exception:  # missing patch folder (e.g. a year rollover)
            break
        minor += 1
    return f"{major}.{minor + 1}"


def latest_db_patch(db: Path, queue: int) -> str:
    """Major.minor patch of the most recently captured games of ``queue``."""
    import sqlite3
    from collections import Counter

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT patch FROM games WHERE queue_id=? ORDER BY rowid DESC LIMIT 2000", (queue,)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise SystemExit(f"no queue {queue} games in {db}")
    return Counter(".".join(str(r[0]).split(".")[:2]) for r in rows).most_common(1)[0][0]


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
    ap.add_argument("--observed-patch", default="pool",
                    help="patch whose games check the pools: 'pool' (same as --version), 'latest' "
                         "(newest in the DB) or a label like 16.22")
    ap.add_argument("--observed-prev", default="auto",
                    help="extra earlier patch of games; 'auto' (the pool's --prev patch when "
                         "--observed-patch is 'pool', else none), 'none' or a label")
    args = ap.parse_args(argv)

    patch = resolve_patch(args.version)
    prev = previous_patch(patch) if args.prev == "auto" else (None if args.prev == "none" else args.prev)
    observed = None
    if args.no_observed:
        pass
    elif not args.observed_db.exists():
        print(f"WARNING: {args.observed_db} missing; publishing unchecked pools", file=sys.stderr)
    else:
        if args.observed_patch == "pool":
            obs_patch = patch
        elif args.observed_patch == "latest":
            obs_patch = latest_db_patch(args.observed_db, args.queue)
        else:
            obs_patch = args.observed_patch
        if args.observed_prev == "auto":
            obs_prev = prev if obs_patch == patch else None
        else:
            obs_prev = None if args.observed_prev == "none" else args.observed_prev
        labels = [p for p in (obs_patch, obs_prev) if p]
        counts = [count_patch(args.observed_db, args.queue, p) for p in labels]
        observed = {"patches": labels, "games": sum(sum(c.games.values()) for c in counts) // 10}
        if obs_patch == patch:
            observed |= {"cur": counts[0], "prev": counts[1] if len(counts) > 1 else None}
        else:
            observed["counts"] = counts
        print(f"observed: {observed['games']} games in {observed['patches']} (queue {args.queue})")
    internal = build_payload(fetch_json, args.version, prev, observed=observed)
    payload = public_payload(internal)  # the page must not show raw pool names
    payload["patch"] = patch
    since = frozen_since(args.version)
    if since:
        payload["frozen"] = {"since": since}
        print(f"frozen: no pools from {since} on; page will say data stops at {patch}")
    n_desc = attach_descriptions(payload["augs"])
    print(f"descriptions: {n_desc}/{len(payload['augs'])} augments")
    obs = payload.get("observed")
    if obs:
        print(
            f"observed: dead={len(obs['dead'])} ({', '.join(d['en'] for d in obs['dead'])}) "
            f"blocked_pairs={sum(len(v) for v in obs['blocked'].values())} over {len(obs['blocked'])} champs "
            f"added={len(obs.get('added') or {})} "
            f"({', '.join(payload['augs'][a]['en'] for a in (obs.get('added') or {}))})"
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
