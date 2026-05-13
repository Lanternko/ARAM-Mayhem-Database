"""One-shot: capture the current EoG stats block and save to games.db."""
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "src")
from aram_nn.lcu.process import get_credentials
from aram_nn.lcu.client import LCUClient, get_eog_stats, get_match_history, get_current_summoner

DB = Path("data/lcu/games.db")
CREATE = """CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY, queue_id INTEGER NOT NULL, patch TEXT NOT NULL,
    blue_champs TEXT NOT NULL, red_champs TEXT NOT NULL, blue_wins INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL, created_ms INTEGER NOT NULL, captured_at TEXT NOT NULL
);"""
MODE_QUEUE = {"KIWI": 2400, "ARAM": 450}

creds = get_credentials()
if not creds:
    print("[error] League client not found"); sys.exit(1)

with LCUClient(creds) as lcu:
    eog = get_eog_stats(lcu)
    if not eog:
        print("[error] EoG screen not active — close and reopen match history?"); sys.exit(1)

    game_id  = str(eog.get("gameId", ""))
    duration = int(eog.get("gameLength", 0))
    mode     = eog.get("gameMode", "")
    queue_id = MODE_QUEUE.get(mode, -1)

    if queue_id < 0:
        print(f"[skip] gameMode={mode!r} not in target queues"); sys.exit(0)
    if duration < 300:
        print(f"[skip] too short ({duration}s)"); sys.exit(0)

    teams = eog.get("teams") or []
    blue_champs, red_champs, blue_wins = [], [], None
    for team in teams:
        tid     = team.get("teamId")
        winning = bool(team.get("isWinningTeam", False))
        champs  = sorted(int(p["championId"]) for p in (team.get("players") or []))
        if len(champs) != 5:
            print(f"[error] team {tid} has {len(champs)} players"); sys.exit(1)
        if tid == 100:
            blue_champs = champs
            blue_wins   = 1 if winning else 0
        elif tid == 200:
            red_champs = champs
    if not blue_champs or not red_champs or blue_wins is None:
        print("[error] could not determine teams/winner"); sys.exit(1)

    # Get patch from match history (same game or most recent)
    puuid = (get_current_summoner(lcu) or {}).get("puuid", "")
    patch = "unknown"
    if puuid:
        for g in (get_match_history(lcu, puuid, begin=0, end=5) or []):
            if str(g.get("gameId","")) == game_id or patch == "unknown":
                ver = g.get("gameVersion","")
                parts = ver.split(".")
                patch = ".".join(parts[:3]) if len(parts) >= 3 else ver
                if str(g.get("gameId","")) == game_id:
                    break

    created_ms = int(eog.get("endOfGameTimestamp", 0)) - duration * 1000

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.execute(CREATE)
    con.execute("INSERT OR IGNORE INTO games VALUES (?,?,?,?,?,?,?,?,?)",
        (game_id, queue_id, patch, json.dumps(blue_champs), json.dumps(red_champs),
         blue_wins, duration, created_ms, datetime.now(timezone.utc).isoformat()))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    con.close()

    label = "Mayhem" if queue_id == 2400 else "ARAM"
    print(f"[saved] {label}  game_id={game_id}  patch={patch}  "
          f"blue={blue_champs}  red={red_champs}  "
          f"blue_wins={bool(blue_wins)}  dur={duration}s  total={total}")
