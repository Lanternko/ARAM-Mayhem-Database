"""Probe what queueIds Diamond IV players actually play.

Fetch last N matches for K Diamond IV PUUIDs without queue filter,
then fetch details and tally queueId / gameMode. This tells us:
  - whether queue 2400 (officially ARAM: Mayhem) is currently used,
  - what the actual queue id is for Brawl / any Mayhem-like mode.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from aram_nn.ingest.riot_client import RiotClient, RiotKeyExpired


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="kr")
    ap.add_argument("--tier", default="DIAMOND", help="CHALLENGER/GRANDMASTER/MASTER/DIAMOND/PLATINUM/GOLD")
    ap.add_argument("--division", default="IV")
    ap.add_argument("--n-players", type=int, default=5)
    ap.add_argument("--matches-per-player", type=int, default=20)
    args = ap.parse_args()

    try:
        with RiotClient(region=args.region) as c:
            print(f"[1] {args.tier} {args.division} entries on {args.region}...")
            entries = c.league_entries(args.tier, division=args.division)
            puuids = [e["puuid"] for e in entries[: args.n_players] if e.get("puuid")]
            print(f"    using {len(puuids)} PUUIDs")

            all_match_ids: list[str] = []
            for p in puuids:
                ids = c.match_ids_by_puuid(p, queue=None, count=args.matches_per_player)
                all_match_ids.extend(ids)
            all_match_ids = list(dict.fromkeys(all_match_ids))
            print(f"[2] collected {len(all_match_ids)} unique match ids (no queue filter)")

            queue_tally: Counter[int] = Counter()
            mode_tally: Counter[tuple] = Counter()
            for i, mid in enumerate(all_match_ids):
                detail = c.match_detail(mid)
                info = detail.get("info") or {}
                qid = int(info.get("queueId", -1))
                mode = info.get("gameMode", "")
                gtype = info.get("gameType", "")
                map_id = info.get("mapId", -1)
                queue_tally[qid] += 1
                mode_tally[(qid, mode, gtype, map_id)] += 1
                if (i + 1) % 10 == 0:
                    print(f"    ... fetched {i+1}/{len(all_match_ids)}")

            print("\n[3] queueId tally:")
            for qid, n in queue_tally.most_common():
                print(f"    queueId={qid:5d}  n={n}")
            print("\n[4] (queueId, gameMode, gameType, mapId) detail:")
            for (qid, mode, gtype, map_id), n in mode_tally.most_common():
                print(f"    qid={qid:5d}  mode={mode:<20s}  type={gtype:<20s}  map={map_id}  n={n}")
        return 0
    except RiotKeyExpired as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
