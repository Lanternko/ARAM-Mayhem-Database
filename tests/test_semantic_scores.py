from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_semantic_ability_scores import build_scores_with_debug  # noqa: E402


ABILITY_JSON = ROOT / "data" / "cache" / "champion_abilities.json"


@unittest.skipUnless(ABILITY_JSON.exists(), "champion_abilities.json is required for semantic score tests")
class SemanticScoreOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rows, skill_rows = build_scores_with_debug(ABILITY_JSON)
        cls.by_alias = {row["champion_alias"]: row for row in rows}
        cls.skill_by_key = {
            (row["champion_alias"], row["spell_slot"]): row
            for row in skill_rows
        }

    def test_blitzcrank_engage_above_lux(self) -> None:
        self.assertGreater(
            float(self.by_alias["Blitzcrank"]["engage_score"]),
            float(self.by_alias["Lux"]["engage_score"]),
        )

    def test_sivir_wave_above_vayne(self) -> None:
        self.assertGreater(
            float(self.by_alias["Sivir"]["wave_clear_score"]),
            float(self.by_alias["Vayne"]["wave_clear_score"]),
        )

    def test_leona_engage_above_xerath(self) -> None:
        self.assertGreater(
            float(self.by_alias["Leona"]["engage_score"]),
            float(self.by_alias["Xerath"]["engage_score"]),
        )

    def test_ziggs_wave_above_leona(self) -> None:
        self.assertGreater(
            float(self.by_alias["Ziggs"]["wave_clear_score"]),
            float(self.by_alias["Leona"]["wave_clear_score"]),
        )

    def test_xerath_wave_above_quinn_and_lee_sin(self) -> None:
        xerath = float(self.by_alias["Xerath"]["wave_clear_score"])
        self.assertGreater(xerath, float(self.by_alias["Quinn"]["wave_clear_score"]))
        self.assertGreater(xerath, float(self.by_alias["LeeSin"]["wave_clear_score"]))

    def test_xerath_wave_above_malphite(self) -> None:
        self.assertGreater(
            float(self.by_alias["Xerath"]["wave_clear_score"]),
            float(self.by_alias["Malphite"]["wave_clear_score"]),
        )

    def test_xerath_wave_above_rumble(self) -> None:
        xerath = float(self.by_alias["Xerath"]["wave_clear_score"])
        self.assertGreater(xerath, float(self.by_alias["Rumble"]["wave_clear_score"]))

    def test_lux_e_and_ziggs_e_get_prep_bonus(self) -> None:
        lux_e = self.skill_by_key[("Lux", "E")]
        ziggs_e = self.skill_by_key[("Ziggs", "E")]
        self.assertGreater(float(lux_e["prep_bonus"]), 0.0)
        self.assertGreater(float(ziggs_e["prep_bonus"]), 0.0)

    def test_lux_e_is_not_utility_only(self) -> None:
        lux_e = self.skill_by_key[("Lux", "E")]
        self.assertTrue(lux_e["supports_wave"])
        self.assertGreater(float(lux_e["wave_skill_score"]), 1.0)

    def test_tryndamere_wave_floor_above_leona(self) -> None:
        self.assertGreater(
            float(self.by_alias["Tryndamere"]["wave_clear_score"]),
            float(self.by_alias["Leona"]["wave_clear_score"]),
        )

    def test_varus_malzahar_syndra_and_velkoz_have_real_wave(self) -> None:
        self.assertGreater(float(self.by_alias["Varus"]["wave_clear_score"]), 1.0)
        self.assertGreater(float(self.by_alias["Malzahar"]["wave_clear_score"]), 1.0)
        self.assertGreater(float(self.by_alias["Syndra"]["wave_clear_score"]), 1.0)
        self.assertGreater(float(self.by_alias["Velkoz"]["wave_clear_score"]), 1.0)

    def test_specific_wave_ordering_feedback_cases(self) -> None:
        self.assertGreater(float(self.by_alias["TwistedFate"]["wave_clear_score"]), float(self.by_alias["Soraka"]["wave_clear_score"]))
        self.assertGreater(float(self.by_alias["Diana"]["wave_clear_score"]), float(self.by_alias["Blitzcrank"]["wave_clear_score"]))
        self.assertGreater(float(self.by_alias["Ashe"]["wave_clear_score"]), float(self.by_alias["XinZhao"]["wave_clear_score"]))
        self.assertGreater(float(self.by_alias["Corki"]["wave_clear_score"]), float(self.by_alias["XinZhao"]["wave_clear_score"]))
        self.assertGreater(float(self.by_alias["Rumble"]["wave_clear_score"]), float(self.by_alias["Kayn"]["wave_clear_score"]))
        self.assertGreater(float(self.by_alias["Anivia"]["wave_clear_score"]), float(self.by_alias["Soraka"]["wave_clear_score"]))

    def test_zoe_w_not_elite_wave_and_ahri_q_above_w(self) -> None:
        self.assertLess(float(self.skill_by_key[("Zoe", "W")]["wave_skill_score"]), 0.5)
        self.assertGreater(
            float(self.skill_by_key[("Ahri", "Q")]["wave_skill_score"]),
            float(self.skill_by_key[("Ahri", "W")]["wave_skill_score"]),
        )

    def test_xerath_q_above_w_and_sion_e_not_elite(self) -> None:
        self.assertGreater(
            float(self.skill_by_key[("Xerath", "Q")]["wave_skill_score"]),
            float(self.skill_by_key[("Xerath", "W")]["wave_skill_score"]),
        )
        self.assertLess(float(self.skill_by_key[("Sion", "E")]["wave_skill_score"]), 1.2)

    def test_khazix_q_and_r_are_not_treated_as_engage_cc(self) -> None:
        khazix_q = self.skill_by_key[("Khazix", "Q")]
        khazix_r = self.skill_by_key[("Khazix", "R")]
        self.assertEqual(khazix_q["cc_type"], "none")
        self.assertEqual(khazix_q["engage_gate"], "none")
        self.assertEqual(khazix_r["cc_type"], "none")
        self.assertEqual(khazix_r["engage_gate"], "none")

    def test_taliyah_q_is_not_hard_cc(self) -> None:
        taliyah_q = self.skill_by_key[("Taliyah", "Q")]
        self.assertEqual(taliyah_q["cc_type"], "soft_slow")
        self.assertEqual(taliyah_q["cast_state"], "worked_ground")
        self.assertLess(float(taliyah_q["engage_skill_score"]), 0.5)

    def test_malphite_q_is_single_target_non_wave_spell(self) -> None:
        malphite_q = self.skill_by_key[("Malphite", "Q")]
        self.assertEqual(malphite_q["shape"], "targeted")
        self.assertFalse(malphite_q["supports_wave"])
        self.assertLess(float(malphite_q["wave_skill_score"]), 0.2)

    def test_ahri_e_is_real_engage_cc(self) -> None:
        ahri_e = self.skill_by_key[("Ahri", "E")]
        self.assertEqual(ahri_e["cc_type"], "single_hard")
        self.assertEqual(ahri_e["engage_gate"], "hard_cc")
        self.assertGreater(float(ahri_e["engage_skill_score"]), 1.0)

    def test_zilean_q_is_conditional_hard_cc(self) -> None:
        zilean_q = self.skill_by_key[("Zilean", "Q")]
        self.assertEqual(zilean_q["cast_state"], "conditional_trigger")
        self.assertEqual(zilean_q["engage_gate"], "conditional_hard_cc")

    def test_wave_uses_top3_and_exposes_itemized_build_profile(self) -> None:
        xerath = self.by_alias["Xerath"]
        self.assertEqual(xerath["wave_top_spells"], "Q,W,E")
        self.assertEqual(xerath["build_profile"], "ap_burst")
        self.assertIn("Luden", xerath["build_items"])
        self.assertGreater(float(xerath["build_ap"]), 0.0)


if __name__ == "__main__":
    unittest.main()
