"""Ranking of the item patch-change board must not be led by thin samples."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from tierlist_engine import _item_rank_score  # noqa: E402


def row(delta, current, baseline=None):
    return {
        "delta": delta,
        "current_games": current,
        "baseline_games": baseline if baseline is not None else current,
    }


def test_thin_sample_loses_to_smaller_delta_with_volume():
    # The shipped board had 闇影戰戟 (2,644 uses, -1.21%) outranking 虛偽光彩
    # (92,862, -0.85%) purely on raw delta.
    thin = row(-0.0121, 2644, 3123)
    heavy = row(-0.0085, 92862, 99719)
    assert _item_rank_score(heavy) < _item_rank_score(thin)


def test_equal_delta_prefers_the_larger_sample():
    assert _item_rank_score(row(-0.01, 100000)) < _item_rank_score(row(-0.01, 1000))


def test_volume_in_one_patch_only_does_not_confer_confidence():
    # Harmonic mean: an item must have volume in BOTH patches to rank.
    lopsided = row(-0.01, 200000, 50)
    balanced = row(-0.01, 8000, 8000)
    assert _item_rank_score(balanced) < _item_rank_score(lopsided)


def test_sign_is_preserved():
    assert _item_rank_score(row(0.02, 50000)) > 0
    assert _item_rank_score(row(-0.02, 50000)) < 0


def test_zero_sample_scores_zero():
    assert _item_rank_score(row(-0.5, 0, 0)) == 0.0
