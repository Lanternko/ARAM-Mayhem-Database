"""Win rate by champion role x chosen summoner spell (Mayhem).

Mark/Dash (id 32) is forced on every ARAM/Mayhem player, so the "choice" is the
*other* spell.  We tally each role's win rate per chosen spell and compare it to
that role's overall baseline.  This is correlational (spell pick confounds with
player/champion), so read it as "what winners tend to bring", not pure causation.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_role_synergy import load_role_by_champ  # noqa: E402

DB = Path("data/lcu/games.db")
SCORES_CSV = Path("data/cache/champion_semantic_scores.csv")
MARK_ID = 32  # SummonerSnowball — forced in ARAM/Mayhem

SPELL_NAMES = {
    1: "Cleanse 净化", 3: "Exhaust 虚弱", 4: "Flash 闪现", 6: "Ghost 幽灵疾步",
    7: "Heal 治疗", 11: "Smite 惩戒", 13: "Clarity 清晰术", 14: "Ignite 点燃",
    21: "Barrier 屏障", 30: "To the King!", 31: "Poro Toss", 32: "Mark 雪球",
    39: "Mark 雪球(变体)",
}


def wilson_lb(wins: int, games: int, z: float = 1.96) -> float:
    if games == 0:
        return 0.0
    p = wins / games
    denom = 1 + z * z / games
    centre = p + z * z / (2 * games)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * games)) / games)
    return (centre - margin) / denom


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp950
    except Exception:
        pass
    role_by_champ = load_role_by_champ(SCORES_CSV)
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT blue_wins, participants_json FROM games "
        "WHERE queue_id=2400 AND participants_json LIKE '%\"spells\"%'"
    ).fetchall()
    con.close()

    role_games: Counter[str] = Counter()
    role_wins: Counter[str] = Counter()
    cell_games: Counter[tuple[str, int]] = Counter()
    cell_wins: Counter[tuple[str, int]] = Counter()
    n_games = 0

    for blue_wins, pj in rows:
        parts = json.loads(pj or "[]")
        if not parts:
            continue
        n_games += 1
        blue_win = int(bool(blue_wins))
        for p in parts:
            cid = int(p.get("championId", 0) or 0)
            tid = int(p.get("teamId", 0) or 0)
            role = role_by_champ.get(cid)
            if not role or tid not in (100, 200):
                continue
            won = blue_win if tid == 100 else (1 - blue_win)
            role_games[role] += 1
            role_wins[role] += won
            for spell in p.get("spells") or []:
                spell = int(spell)
                if spell <= 0:
                    continue  # each player picks 2 spells; tally presence of each
                cell_games[(role, spell)] += 1
                cell_wins[(role, spell)] += won

    print(f"games_with_spells={n_games}  player_slots={sum(role_games.values())}\n")
    for role in ("Mage", "Marksman"):
        base_g = role_games[role]
        base_w = role_wins[role]
        base_wr = base_w / base_g if base_g else 0.0
        cn = "法师" if role == "Mage" else "射手"
        print(f"=== {role} ({cn}) — baseline WR {base_wr:.3f} over {base_g} player-games ===")
        print(f"{'spell':22s} {'games':>7s} {'pick%':>6s} {'win%':>7s} {'wilsonLB':>9s} {'vs base':>8s}")
        ranked = []
        for (r, spell), g in cell_games.items():
            if r != role or g < 100:  # ignore tiny cells
                continue
            w = cell_wins[(r, spell)]
            ranked.append((wilson_lb(w, g), spell, g, w, w / g))
        for lb, spell, g, w, wr in sorted(ranked, reverse=True):
            name = SPELL_NAMES.get(spell, f"id {spell}")
            pick = g / base_g * 100 if base_g else 0.0
            print(f"{name:22s} {g:>7d} {pick:>5.1f}% {wr*100:>6.1f}% {lb:>9.3f} {(wr-base_wr)*100:>+7.1f}")
        print()


if __name__ == "__main__":
    main()
