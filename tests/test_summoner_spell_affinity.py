import json
import sqlite3

import pytest

from scripts.tierlist_engine import compute_champ_spell_affinities


def test_summoner_spells_are_ranked_as_two_spell_loadouts(tmp_path) -> None:
    db_path = tmp_path / "games.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE games (queue_id INTEGER, patch TEXT, blue_wins INTEGER, participants_json TEXT)"
    )

    def add_games(spells: list[object], count: int) -> None:
        for game_idx in range(count):
            participants = [{
                "championId": 1,
                "teamId": 100,
                "spells": spells,
            }]
            con.execute(
                "INSERT INTO games VALUES (?, ?, ?, ?)",
                (2400, "16.15", game_idx % 2, json.dumps(participants)),
            )

    add_games([6, 4], 40)
    add_games([32, 4], 30)
    add_games([4], 10)  # An incomplete capture is not a valid loadout.
    add_games([6, 4, 4], 10)  # Extra duplicate slots do not become a valid pair.
    add_games([4, 6, 0], 10)  # Extra empty slots do not become a valid pair.
    add_games([4, 4], 10)  # Both slots must be distinct spells.
    add_games(["bad", 4], 10)  # Malformed captures are ignored, not fatal.
    con.commit()
    con.close()

    spell_meta = {
        4: {"name_zh": "閃現", "name_en": "Flash", "icon": "flash.png"},
        6: {"name_zh": "鬼步", "name_en": "Ghost", "icon": "ghost.png"},
        32: {"name_zh": "標記", "name_en": "Mark", "icon": "mark.png"},
    }
    affinity = compute_champ_spell_affinities(
        db_path,
        2400,
        "16.15",
        spell_meta,
        [{"champion_id": 1, "raw_wr": 0.5}],
        min_games=1,
    )

    rows = affinity[1]["top"]
    assert {row["slug"] for row in rows} == {"4+6", "4+32"}
    assert sum(row["pick_rate"] for row in rows) == pytest.approx(1.0)

    flash_ghost = next(row for row in rows if row["slug"] == "4+6")
    assert flash_ghost["games"] == 40
    assert flash_ghost["name_en"] == "Flash + Ghost"
    assert [spell["id"] for spell in flash_ghost["items"]] == [4, 6]
