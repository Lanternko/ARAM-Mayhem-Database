"""Probe LCU endpoints to find where full 10-player team composition is stored."""
import json, sys
sys.path.insert(0, "src")
from aram_nn.lcu.process import get_credentials
from aram_nn.lcu.client import LCUClient, get_current_summoner, get_match_history

creds = get_credentials()
if not creds:
    print("[error] League client not found"); sys.exit(1)

with LCUClient(creds) as lcu:
    s = get_current_summoner(lcu)
    puuid = s["puuid"]
    games = get_match_history(lcu, puuid, begin=0, end=3)
    if not games:
        print("[error] no games in history"); sys.exit(1)

    # Take first Mayhem game
    game = next((g for g in games if g.get("queueId") == 2400), games[0])
    game_id = game.get("gameId")
    print(f"Probing game_id={game_id}  queueId={game.get('queueId')}")

    # 1. Check how many participants the list endpoint actually has
    parts = game.get("participants") or []
    print(f"\n[list endpoint] participants count = {len(parts)}")
    if parts:
        print(f"  first participant keys: {list(parts[0].keys())}")
        print(f"  first participant: {json.dumps(parts[0], ensure_ascii=False)[:400]}")

    # 2. Try per-game detail endpoint
    print(f"\n[/lol-match-history/v1/games/{game_id}]")
    detail = lcu.get(f"/lol-match-history/v1/games/{game_id}")
    if detail:
        n = len((detail.get("participants") or []))
        print(f"  participants count = {n}")
        if n:
            print(f"  first: {json.dumps((detail['participants'] or [])[0], ensure_ascii=False)[:400]}")
        else:
            print(f"  keys: {list(detail.keys())}")
    else:
        print("  -> None (endpoint doesn't exist or returned error)")

    # 3. Try participantIdentities path
    print(f"\n[participantIdentities in list entry]")
    pids = game.get("participantIdentities") or []
    print(f"  count = {len(pids)}")

    # 4. Try the older v3 endpoint style
    print(f"\n[/lol-match-history/v3/matchlist/account/{puuid}]")
    v3 = lcu.get(f"/lol-match-history/v3/matchlist/account/{puuid}", beginIndex=0, endIndex=3)
    if v3:
        print(f"  keys: {list(v3.keys()) if isinstance(v3, dict) else type(v3)}")
    else:
        print("  -> None")

    # 5. Dump the full raw list-entry to see ALL keys
    print(f"\n[full raw game entry keys]")
    print(f"  {sorted(game.keys())}")
    # Check if there's any team-related key
    for k in sorted(game.keys()):
        v = game[k]
        if isinstance(v, (list, dict)) and v:
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:300]}")
