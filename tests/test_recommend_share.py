"""Tests for the recommender's shared-bench clipboard text.

The "copy pool" button used to paste each champion's `win_prob` — the local
team's predicted win probability with the candidate swapped into *your* cell.
That number is shifted by your team's baseline and only valid for your slot,
so it is misleading to share with teammates (a sub-50% champion could show as
57% just because the rest of your team was strong).

The share now pastes each champion's real global meta win rate from the
tier-list payload (`champ["wr"]`), which is cell-independent and matches the
public tier list.  These tests lock in that contract.
"""
from __future__ import annotations

import types
import unittest

# recommend_gui imports tkinter at module load; skip the suite gracefully on
# the rare headless environment where Tk is unavailable.
try:
    import scripts.recommend_gui as rg
    from aram_nn.recommend import Suggestion

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-dependent
    rg = None  # type: ignore[assignment]
    Suggestion = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def _suggestion(cid: int, win_prob: float, *, is_known: bool = True) -> Suggestion:
    """Minimal Suggestion; only fields the share-text builder reads matter."""
    return Suggestion(
        champion_id=cid,
        source="bench",
        win_prob=win_prob,
        delta=0.0,
        prob_delta_lr=0.0,
        synergy_delta=0.0,
        synergy_se=0.0,
        anchors_covered=0,
        score=0.0,
        z_score=0.0,
        is_known=is_known,
    )


def _app(champs: dict[int, dict], names: dict[int, str]) -> rg.RecommenderApp:
    """RecommenderApp instance without running __init__ (no Tk root needed)."""
    app = rg.RecommenderApp.__new__(rg.RecommenderApp)
    app.id_to_name = names
    app.augment_advisor = types.SimpleNamespace(champs=champs)
    return app


@unittest.skipIf(rg is None, f"recommend_gui import failed: {_IMPORT_ERROR}")
class ChampMetaWrTests(unittest.TestCase):
    def test_reads_payload_win_rate(self) -> None:
        app = _app({157: {"wr": 0.575}}, {157: "Yasuo"})
        self.assertAlmostEqual(app._champ_meta_wr(157), 0.575)

    def test_missing_champion_returns_none(self) -> None:
        app = _app({157: {"wr": 0.575}}, {157: "Yasuo"})
        self.assertIsNone(app._champ_meta_wr(999))

    def test_missing_wr_field_returns_none(self) -> None:
        app = _app({157: {"g": 100}}, {157: "Yasuo"})
        self.assertIsNone(app._champ_meta_wr(157))


@unittest.skipIf(rg is None, f"recommend_gui import failed: {_IMPORT_ERROR}")
class BenchTopClipboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.names = {157: "Yasuo", 40: "Janna", 412: "Thresh", 999: "Mystery"}
        self.champs = {157: {"wr": 0.575}, 40: {"wr": 0.524}, 412: {"wr": 0.442}}
        self.app = _app(self.champs, self.names)

    def test_shows_meta_wr_not_baseline_shifted_win_prob(self) -> None:
        # win_prob (0.647) is the OLD shared number; it must NOT appear.
        text = self.app._bench_top_clipboard_text([_suggestion(157, 0.647)])
        self.assertIn("57.5%", text)       # real meta WR
        self.assertNotIn("64.7%", text)    # baseline-shifted win_prob

    def test_keeps_team_fit_ordering_even_when_wr_is_non_monotonic(self) -> None:
        # Ordered by win_prob desc (team fit), so Thresh (higher win_prob but
        # lower real WR) ranks above Janna — the displayed WR is intentionally
        # not sorted.
        pool = [_suggestion(157, 0.66), _suggestion(412, 0.60), _suggestion(40, 0.55)]
        lines = self.app._bench_top_clipboard_text(pool).splitlines()[1:]
        self.assertEqual(lines[0], "1. Yasuo 57.5%")
        self.assertEqual(lines[1], "2. Thresh 44.2%")
        self.assertEqual(lines[2], "3. Janna 52.4%")

    def test_missing_payload_wr_shows_dash(self) -> None:
        text = self.app._bench_top_clipboard_text([_suggestion(999, 0.70)])
        self.assertIn("1. Mystery —", text)

    def test_drops_champions_unknown_to_the_model(self) -> None:
        pool = [_suggestion(157, 0.60), _suggestion(40, 0.55, is_known=False)]
        text = self.app._bench_top_clipboard_text(pool)
        self.assertIn("Yasuo", text)
        self.assertNotIn("Janna", text)

    def test_empty_pool(self) -> None:
        self.assertIn("n/a", self.app._bench_top_clipboard_text([]))


if __name__ == "__main__":
    unittest.main()
