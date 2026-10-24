import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tierlist_engine as eng  # noqa: E402


def _slot(games, wins):
    return {"games": games, "wins": wins, "raw_wr": wins / games, "smoothed_wr": wins / games}


def _row(cid, aid, slots, games=None, wins=None, baseline=0.5, smoothed=0.5):
    games = games if games is not None else sum(s["games"] for s in slots if s)
    wins = wins if wins is not None else sum(s["wins"] for s in slots if s)
    return {
        "champion_id": cid,
        "augment_id": aid,
        "games": games,
        "wins": wins,
        "baseline_wr": baseline,
        "smoothed_wr": smoothed,
        "slots": slots,
    }


def test_global_slots_measure_lift_against_each_rounds_baseline():
    # Round 4 baseline is 45% across augments; augment 2 at 45% in round 4 is even.
    records = [
        _row(1, 1, [_slot(10000, 5000), _slot(10000, 5000), _slot(10000, 5000), _slot(10000, 4500)]),
        _row(1, 2, [_slot(10000, 6000), _slot(10000, 5500), _slot(10000, 5000), _slot(10000, 4500)]),
    ]
    stats = eng.build_augment_global_stats(records, {1: {}, 2: {}}, appearance_games=10000)
    slots = stats[2]["slots"]
    assert [s["exact"] for s in slots] == [True, True, True, True]
    assert slots[0]["lift"] > 0.04
    assert abs(slots[3]["lift"]) < 0.005
    assert slots[0]["lift"] > slots[1]["lift"] > slots[2]["lift"]


def test_thin_global_round_is_topped_up_from_previous_patch_and_not_exact():
    cur = [_row(1, 1, [_slot(5000, 2500), _slot(50, 25), None, None])]
    prev = [_row(1, 1, [_slot(5000, 2500), _slot(5000, 3500), None, None])]
    stats = eng.build_augment_global_stats(
        cur, {1: {}}, appearance_games=1000, prev_champ_aug_records=prev
    )
    s2 = stats[1]["slots"][1]
    assert s2["g"] == eng.AUGMENT_SLOT_CURRENT_MIN_GAMES
    assert s2["prevMix"] > 0
    assert s2["exact"] is False
    assert stats[1]["slots"][2] is None


def test_champion_slots_borrow_previous_patch_and_flag_show():
    cur = [
        _row(7, 9, [_slot(400, 240), _slot(20, 10), None, None], baseline=0.5, smoothed=0.55),
        _row(8, 9, [_slot(4000, 2000), _slot(4000, 2000), _slot(4000, 2000), _slot(4000, 2000)]),
    ]
    prev = [_row(7, 9, [None, _slot(300, 150), _slot(30, 20), None])]
    eng.attach_champ_aug_slot_evidence(cur, prev)
    s1, s2, s3, s4 = cur[0]["slots"]
    assert s1["show"] and s1["prev_mix"] == 0 and s1["lift"] > 0
    assert s2["eff_games"] == eng.CHAMP_AUG_SLOT_CURRENT_MIN_GAMES and s2["prev_mix"] > 0 and s2["show"]
    # Round 3 exists only in the previous patch and is too thin to print.
    assert s3["eff_games"] == 30 and not s3["show"]
    assert s4 is None


def test_attach_without_previous_patch_keeps_current_cells():
    cur = [_row(7, 9, [_slot(200, 100), None, None, None])]
    eng.attach_champ_aug_slot_evidence(cur, None)
    assert cur[0]["slots"][0]["eff_games"] == 200
    assert cur[0]["slots"][1] is None
