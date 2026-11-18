"""Vectorized team-score training must reproduce the per-team reference code.

The bundle is embedded in the published payload, so the array rewrite was
required to be bit-identical to the old per-game loops.  These tests pin the
pieces against straightforward reference implementations.
"""
from __future__ import annotations

import math
import random
import sqlite3
from collections import Counter

import numpy as np

from aram_nn.site import team_score_training as ts
from aram_nn.site.meta_pick import team_composition


def _games(rng: random.Random, n: int, pool: list[int]) -> ts.TeamGames:
    blue, red, created, wins = [], [], [], []
    for i in range(n):
        ten = rng.sample(pool, 10)
        blue.append(sorted(ten[:5]))
        red.append(sorted(ten[5:]))
        created.append(1_000 + i)
        wins.append(float(rng.random() < 0.5))
    return ts.TeamGames(
        created_ms=np.asarray(created, dtype=np.int64),
        blue=np.asarray(blue, dtype=np.int64),
        red=np.asarray(red, dtype=np.int64),
        blue_wins=np.asarray(wins, dtype=np.float64),
    )


def _snapshot(rng: random.Random, pool: list[int]) -> dict:
    champs: dict[str, dict] = {}
    for cid in pool:
        if cid % 11 == 0:
            continue  # missing from the snapshot entirely
        comp = {key: round(rng.uniform(0, 3), 3) for key in ts._SUM_KEYS}
        if cid % 7 == 0:
            comp["poke"] = "not-a-number"
        if cid % 5 == 0:
            comp["magic"] = None
        if cid % 13 == 0:
            comp["phys"] = 0.0
            comp["magic"] = 0.0
        tags = rng.choice([["Mage"], ["Marksman"], ["Tank"], ["Mage", "Marksman"], []])
        champs[str(cid)] = {"tags": tags, "comp": comp}
    thresholds = {"wave": 4.0, "cc": 5.5, "engage": "bad", "damage": 6.0, "poke": 3.0, "sustain": 0, "front": 2.5}
    return {"champs": champs, "recommendation_composition": {"lack_thresholds": thresholds}}


def _reference_keys(team, snapshot) -> dict[str, str]:
    comp = team_composition([str(cid) for cid in team], snapshot)
    return {
        "ad_front": f"{comp['frontGroup']}|{comp['adBin']}",
        "poke_front": f"{comp['frontGroup']}|{comp['pokeGroup']}",
        "wave_engage": f"{comp['waveGroup']}|{comp['engageGroup']}",
        "all_lacks": str(comp["allLacksGroup"]),
        "mage_ad": f"{comp['mageGroup']}|{comp['adBin']}",
        "marksman_ad": f"{comp['marksmanGroup']}|{comp['adBin']}",
    }


def test_composition_codes_match_team_composition():
    rng = random.Random(7)
    pool = list(range(1, 90))
    games = _games(rng, 400, pool)
    snapshot = _snapshot(rng, pool)
    index = ts._champion_index(games)
    teams, _won = ts._sides(games, index)
    codes = ts._composition_codes(teams, snapshot, index)
    cids = index.cids
    for row in range(teams.shape[0]):
        expected = _reference_keys(cids[teams[row]].tolist(), snapshot)
        got = {name: ts._TABLE_LABELS[name][int(codes[name][row])] for name in ts.TABLE_NAMES}
        assert got == expected, row


def test_builtin_sum_matches_python_sum():
    rng = random.Random(3)
    columns = [
        np.asarray([rng.uniform(-1, 1) * 10 ** rng.randint(-8, 8) for _ in range(2000)])
        for _ in range(6)
    ]
    got = ts._builtin_sum(columns)
    for i in range(2000):
        assert got[i] == sum(float(column[i]) for column in columns)


def test_solo_and_pair_stats_match_reference():
    rng = random.Random(11)
    pool = list(range(1, 40))
    games = _games(rng, 600, pool)
    index = ts._champion_index(games)
    teams, won = ts._sides(games, index)
    stats = ts._solo_and_pair_stats(teams, won, index)

    champ_games: Counter = Counter()
    champ_wins: Counter = Counter()
    pair_games: Counter = Counter()
    pair_wins: Counter = Counter()
    for g in range(len(games)):
        for team, w in ((games.blue[g].tolist(), int(games.blue_wins[g])),
                        (games.red[g].tolist(), 1 - int(games.blue_wins[g]))):
            for i, cid in enumerate(team):
                champ_games[cid] += 1
                champ_wins[cid] += w
                for mate in team[i + 1:]:
                    pair_games[(cid, mate)] += 1
                    pair_wins[(cid, mate)] += w
    for pos, cid in enumerate(index.cids.tolist()):
        n = champ_games[cid]
        assert stats.solo_wr[pos] == (champ_wins[cid] + 100.0) / (n + 200)
    assert set(stats.pair_lift) == set(pair_games)
    for (a, b), n in pair_games.items():
        raw_a = champ_wins[a] / champ_games[a]
        raw_b = champ_wins[b] / champ_games[b]
        expected = 1.0 / (1.0 + math.exp(-(ts._logit(raw_a) + ts._logit(raw_b) - ts._logit(0.5))))
        assert stats.pair_lift[(a, b)] == (pair_wins[(a, b)] / n - expected, n)


def test_bundle_trains_from_db(tmp_path):
    rng = random.Random(5)
    pool = list(range(1, 60))
    games = _games(rng, 3000, pool)
    db = tmp_path / "games.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE games (game_id INTEGER, queue_id INTEGER, patch TEXT, created_ms INTEGER,"
        " blue_champs TEXT, red_champs TEXT, blue_wins INTEGER)"
    )
    rows = [
        (i, 2400, "16.22.1", int(games.created_ms[i]),
         "[" + ",".join(map(str, games.blue[i].tolist()[::-1])) + "]",  # unsorted on disk
         str(games.red[i].tolist()), int(games.blue_wins[i]))
        for i in range(len(games))
    ]
    rows.append((99_999, 2400, "16.22.1", 5, "[1,2,3]", "[4,5,6,7,8]", 1))  # 3v5: skipped
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    loaded = ts.load_team_games(db, queue_id=2400, patch_prefix="16.22")
    assert len(loaded) == len(games)
    assert np.array_equal(loaded.blue, games.blue)

    champ_profiles = {cid: {"physical_dpm": cid % 4, "magic_dpm": 3 - cid % 4, "front": cid % 3} for cid in pool}
    bundle = ts.train_team_score_bundle(
        db,
        queue_id=2400,
        patch_prefix="16.22",
        champ_profiles=champ_profiles,
        champ_meta={cid: {"tags": ["Mage"] if cid % 2 else ["Tank"]} for cid in pool},
        lack_thresholds={"front": 2.0},
        table_weights={name: 1.0 for name in ts.TABLE_NAMES},
        composition_clamp=0.05,
        min_synergy_games=5,
        min_training_games=1000,
    )
    split = bundle["team_score"]["split"]
    assert split["train_games"] + split["validation_games"] + split["test_games"] == len(games)
    assert isinstance(split["test_end_ms"], int)
    assert set(bundle["composition"]["tables"]) == set(ts.TABLE_NAMES)
