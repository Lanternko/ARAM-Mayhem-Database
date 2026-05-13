"""Dump raw LCU data for diagnosis. Usage: python scripts/lcu_dump.py [--eog]"""
import json, sys
sys.path.insert(0, "src")
from aram_nn.lcu.process import get_credentials
from aram_nn.lcu.client import LCUClient, get_current_summoner, get_match_history, get_eog_stats

creds = get_credentials()
if not creds:
    print("[error] League client not found"); sys.exit(1)

with LCUClient(creds) as lcu:
    if "--eog" in sys.argv:
        eog = get_eog_stats(lcu)
        if not eog:
            print("[eog] endpoint returned nothing (EoG screen already closed?)")
        else:
            # Print top-level keys and teams structure only
            print("top-level keys:", list(eog.keys()))
            teams = eog.get("teams") or eog.get("team") or []
            print(f"\nteams count: {len(teams)}")
            for ti, team in enumerate(teams):
                print(f"\n  team[{ti}]: keys={list(team.keys())}")
                players = team.get("players") or team.get("stats") or []
                print(f"  players count: {len(players)}")
                for pi, p in enumerate(players[:2]):
                    print(f"    player[{pi}]: {json.dumps(p, ensure_ascii=False)[:200]}")
        sys.exit(0)

    s = get_current_summoner(lcu)
    puuid = s["puuid"]
    print(f"puuid: {puuid[:16]}...")

    games = get_match_history(lcu, puuid, begin=0, end=20)
    print(f"\n{len(games)} games in history\n")
    for g in games:
        gid      = g.get("gameId")
        qid      = g.get("queueId")
        mode     = g.get("gameMode")
        map_id   = g.get("mapId")
        duration = g.get("gameDuration")
        version  = g.get("gameVersion","")[:8]
        n_parts  = len(g.get("participants") or [])
        print(f"  gameId={gid}  queueId={qid:5}  mode={str(mode):<12}  "
              f"mapId={map_id}  dur={duration}s  ver={version}  participants={n_parts}")
