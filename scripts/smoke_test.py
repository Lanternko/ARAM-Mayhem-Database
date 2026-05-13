"""Smoke test: fetch 1 high-tier player's recent Mayhem match list, then 1 match detail.

Exits 0 on success, prints schema sanity for confirmation.
Run:
  set RIOT_API_KEY=RGAPI-xxxx
  python scripts/smoke_test.py --region kr
"""
from __future__ import annotations

import argparse
import json
import sys

from aram_nn.ingest.extract import MAYHEM_QUEUE_ID, extract_row
from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="kr")
    args = ap.parse_args()

    try:
        with RiotClient(region=args.region) as c:
            # Try Diamond IV first (broadest, most likely to play RGMs), fall back upward.
            tiers_to_try = [("DIAMOND", "IV"), ("DIAMOND", "III"), ("PLATINUM", "I"), ("MASTER", "I")]
            entries: list[dict] = []
            for tier, div in tiers_to_try:
                print(f"[1/4] league-exp {tier} {div} on {args.region}...")
                entries = c.league_entries(tier, division=div)
                print(f"      got {len(entries)} entries")
                if entries:
                    break
            if not entries:
                print("      FAIL: no entries returned across tried tiers", file=sys.stderr)
                return 1

            print(f"[2/4] scanning up to 30 PUUIDs for queue={MAYHEM_QUEUE_ID} matches...")
            ids: list[str] = []
            puuid = None
            for i, e in enumerate(entries[:30]):
                p = e.get("puuid")
                if not p:
                    continue
                got = c.match_ids_by_puuid(p, queue=MAYHEM_QUEUE_ID, count=20)
                if got:
                    puuid = p
                    ids = got
                    print(f"      found mayhem-player at idx {i}: puuid {p[:12]}... has {len(got)} matches")
                    break
            if not ids:
                print(f"      FAIL: 30 PUUIDs from {tiers_to_try[0]} had 0 Mayhem games.", file=sys.stderr)
                print("      Possible causes: (a) queue 2400 isn't current Mayhem queue id, (b) Mayhem off-rotation in this region.", file=sys.stderr)
                return 1
            mid = ids[0]
            print(f"      sample match id: {mid}")

            print(f"[3/4] match-detail {mid}...")
            detail = c.match_detail(mid)
            info = detail.get("info") or {}
            print(f"      queueId={info.get('queueId')}  gameVersion={info.get('gameVersion')}  "
                  f"duration={info.get('gameDuration')}s  participants={len(info.get('participants', []))}")

            print(f"[4/4] extract row...")
            row = extract_row(detail)
            if row is None:
                print("      FAIL: extract_row returned None (queue mismatch or malformed)", file=sys.stderr)
                print(json.dumps(info, indent=2)[:500])
                return 1
            print(f"      blue: {row['blue_champions']}  red: {row['red_champions']}  "
                  f"blue_wins={row['blue_wins']}  patch={row['patch']}")
        print("\n[OK] all 4 stages passed — ready for full ingest.")
        return 0
    except RiotKeyExpired as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
