"""Probe a specific user's match history to identify the true queueId of Mayhem/Brawl.

Usage:
  $env:RIOT_API_KEY = "RGAPI-..."
  python scripts/probe_user.py --region tw --riot-id "name#TAG" --count 100
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="tw")
    ap.add_argument("--riot-id", required=True, help='Format: "GameName#TagLine"')
    ap.add_argument("--count", type=int, default=100, help="How many recent matches to scan")
    args = ap.parse_args()

    if "#" not in args.riot_id:
        print("[FATAL] --riot-id must be in 'GameName#TagLine' format", file=sys.stderr)
        return 1
    name, tag = args.riot_id.split("#", 1)
    name, tag = name.strip(), tag.strip()
    print(f"[0] resolving '{name}' #{tag} on {args.region}...")

    try:
        with RiotClient(region=args.region) as c:
            acct = c.account_by_riot_id(name, tag)
            puuid = acct.get("puuid")
            if not puuid:
                print(f"[FATAL] account lookup returned no puuid: {acct}", file=sys.stderr)
                return 1
            print(f"    puuid: {puuid[:16]}...  (game: {acct.get('gameName')}#{acct.get('tagLine')})")

            print(f"[1] fetching last {args.count} match ids (no queue filter)...")
            ids = c.match_ids_by_puuid(puuid, queue=None, count=args.count)
            print(f"    got {len(ids)} match ids")
            if not ids:
                print("[FATAL] no recent matches — account may not have played recently", file=sys.stderr)
                return 1

            print(f"[2] fetching match details (~{len(ids)} API calls, may take 1-3 min)...")
            queue_tally: Counter[int] = Counter()
            mode_tally: Counter[tuple] = Counter()
            patch_tally: Counter[str] = Counter()
            for i, mid in enumerate(ids):
                detail = c.match_detail(mid)
                info = detail.get("info") or {}
                qid = int(info.get("queueId", -1))
                queue_tally[qid] += 1
                mode_tally[(qid, info.get("gameMode", ""), info.get("mapId", -1))] += 1
                # gameVersion is like "16.9.701.1234" — keep major.minor
                gv = str(info.get("gameVersion", ""))
                if gv:
                    parts = gv.split(".")
                    patch = ".".join(parts[:2]) if len(parts) >= 2 else gv
                    patch_tally[patch] += 1
                if (i + 1) % 20 == 0:
                    print(f"    ... {i+1}/{len(ids)}")

            print("\n[3] queueId tally (sorted by count):")
            for qid, n in queue_tally.most_common():
                print(f"    queueId={qid:5d}  n={n}")
            print("\n[4] (queueId, gameMode, mapId):")
            for (qid, mode, mp), n in mode_tally.most_common():
                print(f"    qid={qid:5d}  mode={mode:<22s}  map={mp}  n={n}")
            print("\n[5] patch tally:")
            for patch, n in patch_tally.most_common():
                print(f"    {patch}  n={n}")
        return 0
    except RiotKeyExpired as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
