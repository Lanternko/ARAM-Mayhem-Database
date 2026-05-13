"""Backfill historical games from LCU match history into games.db.

Uses /lol-match-history/v1/games/{gameId} which returns all 10 participants,
unlike the list endpoint that only returns the local player.
"""
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")
from aram_nn.lcu.process import get_credentials
from aram_nn.lcu.client import LCUClient, get_current_summoner, get_match_history, get_game_detail

DB = Path("data/lcu/games.db")
TARGET_QUEUES = {450, 2400}
_MODE_TO_QUEUE = {"KIWI": 2400, "ARAM": 450}

CREATE = """CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY, queue_id INTEGER NOT NULL, patch TEXT NOT NULL,
    blue_champs TEXT NOT NULL, red_champs TEXT NOT NULL, blue_wins INTEGER NOT NULL,
    duration_sec INTEGER NOT NULL, created_ms INTEGER NOT NULL, captured_at TEXT NOT NULL
);"""


def _parse_detail(game: dict) -> dict | None:
    game_id = str(game.get("gameId", ""))
    if not game_id:
        return None

    queue_id = game.get("queueId", -1)
    if queue_id not in TARGET_QUEUES:
        mode = game.get("gameMode", "")
        queue_id = _MODE_TO_QUEUE.get(mode, -1)
    if queue_id not in TARGET_QUEUES:
        return None

    duration = int(game.get("gameDuration", 0))
    if duration < 300:
        return None

    participants = game.get("participants") or []
    if len(participants) != 10:
        return None

    blue_champs = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 100)
    red_champs  = sorted(int(p["championId"]) for p in participants if p.get("teamId") == 200)
    if len(blue_champs) != 5 or len(red_champs) != 5:
        return None

    # win from teams array ("Win"/"Fail" string or bool)
    blue_wins = None
    for team in (game.get("teams") or []):
        if team.get("teamId") == 100:
            w = team.get("win")
            if isinstance(w, bool):
                blue_wins = 1 if w else 0
            elif isinstance(w, str):
                blue_wins = 1 if w.lower() == "win" else 0
            break
    # fallback: participant stats
    if blue_wins is None:
        for p in participants:
            if p.get("teamId") == 100:
                w = (p.get("stats") or {}).get("win")
                if w is not None:
                    blue_wins = 1 if w else 0
                    break
    if blue_wins is None:
        return None

    ver = game.get("gameVersion", "")
    parts = ver.split(".")
    patch = ".".join(parts[:3]) if len(parts) >= 3 else (ver or "unknown")

    return {
        "game_id":      game_id,
        "queue_id":     queue_id,
        "patch":        patch,
        "blue_champs":  blue_champs,
        "red_champs":   red_champs,
        "blue_wins":    blue_wins,
        "duration_sec": duration,
        "created_ms":   int(game.get("gameCreation", 0)),
        "captured_at":  datetime.now(timezone.utc).isoformat(),
    }


creds = get_credentials()
if not creds:
    print("[error] League client not found"); sys.exit(1)

DB.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(DB))
con.execute(CREATE)
con.commit()

already = {row[0] for row in con.execute("SELECT game_id FROM games").fetchall()}
print(f"DB already has {len(already)} games")

with LCUClient(creds) as lcu:
    s = get_current_summoner(lcu)
    puuid = s["puuid"]
    print(f"Connected as {s.get('gameName') or s.get('displayName')}  puuid={puuid[:12]}…")

    history = get_match_history(lcu, puuid, begin=0, end=20)
    mayhem_ids = [str(g["gameId"]) for g in history
                  if g.get("queueId") in TARGET_QUEUES or
                     _MODE_TO_QUEUE.get(g.get("gameMode",""), -1) in TARGET_QUEUES]
    print(f"Found {len(mayhem_ids)} target-queue games in history (last 20)")

    saved = skipped = failed = 0
    for gid in mayhem_ids:
        if gid in already:
            skipped += 1
            continue
        detail = get_game_detail(lcu, gid)
        if not detail:
            print(f"  [warn] could not fetch detail for {gid}")
            failed += 1
            continue
        record = _parse_detail(detail)
        if not record:
            print(f"  [skip] {gid} — filtered (wrong queue / too short / parse fail)")
            skipped += 1
            continue
        con.execute("INSERT OR IGNORE INTO games VALUES (?,?,?,?,?,?,?,?,?)", (
            record["game_id"], record["queue_id"], record["patch"],
            json.dumps(record["blue_champs"]), json.dumps(record["red_champs"]),
            record["blue_wins"], record["duration_sec"],
            record["created_ms"], record["captured_at"],
        ))
        con.commit()
        already.add(gid)
        label = "Mayhem" if record["queue_id"] == 2400 else "ARAM"
        print(f"  [saved] {label}  {gid}  patch={record['patch']}  "
              f"blue={record['blue_champs']}  red={record['red_champs']}  "
              f"blue_wins={bool(record['blue_wins'])}  dur={record['duration_sec']}s")
        saved += 1

total = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
con.close()
print(f"\nDone.  saved={saved}  skipped={skipped}  failed={failed}  total_in_db={total}")
