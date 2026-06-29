from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_semantic_ability_scores import build_scores_with_debug, basic_attack_floor  # noqa: E402


ABILITY_JSON = ROOT / "data" / "cache" / "champion_abilities.json"


class BasicAttackScoreTests(unittest.TestCase):
    def test_basic_attack_score_uses_native_range_and_ad(self) -> None:
        short_low_ad = {
            "alias": "ShortLowAdFixture",
            "tags": ["Mage"],
            "stats": {
                "attackrange": 425,
                "attackdamage": 48,
                "attackdamageperlevel": 2.4,
                "attackspeed": 0.60,
                "attackspeedperlevel": 1.5,
            },
            "abilities": [],
            "passive": {},
        }
        long_high_ad = {
            "alias": "LongHighAdFixture",
            "tags": ["Mage"],
            "stats": {
                "attackrange": 650,
                "attackdamage": 68,
                "attackdamageperlevel": 4.4,
                "attackspeed": 0.68,
                "attackspeedperlevel": 3.5,
            },
            "abilities": [],
            "passive": {},
        }
        self.assertGreater(basic_attack_floor(short_low_ad), 0.0)
        self.assertGreater(basic_attack_floor(long_high_ad), basic_attack_floor(short_low_ad))


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

    def test_sejuani_r_is_primary_engage_not_w(self) -> None:
        sejuani = self.by_alias["Sejuani"]
        sejuani_r = self.skill_by_key[("Sejuani", "R")]
        sejuani_w = self.skill_by_key[("Sejuani", "W")]
        self.assertEqual(sejuani_r["cc_type"], "single_hard")
        self.assertEqual(sejuani_w["cc_type"], "soft_slow")
        self.assertEqual(sejuani["engage_top_spells"].split(",")[0], "R")

    def test_review_notes_promote_missing_engage_spells_into_top3(self) -> None:
        expected_top3 = {
            "Soraka": "E",
            "Nami": "R",
            "Gangplank": "E",
            "Aphelios": "R",
            "Twitch": "R",
            "Lucian": "R",
            "Fiddlesticks": "R",
        }
        for alias, slot in expected_top3.items():
            with self.subTest(alias=alias, slot=slot):
                self.assertIn(slot, self.by_alias[alias]["engage_top_spells"].split(","))

    def test_fiddlesticks_r_and_e_have_engage_control_semantics(self) -> None:
        fiddlesticks = self.by_alias["Fiddlesticks"]
        fiddlesticks_r = self.skill_by_key[("Fiddlesticks", "R")]
        fiddlesticks_e = self.skill_by_key[("Fiddlesticks", "E")]
        self.assertEqual(fiddlesticks["engage_top_spells"].split(",")[0], "R")
        self.assertEqual(fiddlesticks_r["cc_type"], "aoe_hard")
        self.assertEqual(fiddlesticks_e["cc_type"], "utility_cc")

    def test_rammus_q_is_primary_engage(self) -> None:
        rammus = self.by_alias["Rammus"]
        rammus_q = self.skill_by_key[("Rammus", "Q")]
        self.assertEqual(rammus["engage_top_spells"].split(",")[0], "Q")
        self.assertEqual(rammus_q["shape"], "dash")
        self.assertEqual(rammus_q["cc_type"], "single_hard")

    def test_karthus_passive_is_scored_as_special_engage(self) -> None:
        karthus = self.by_alias["Karthus"]
        karthus_p = self.skill_by_key[("Karthus", "P")]
        self.assertIn("P", karthus["engage_top_spells"].split(","))
        self.assertEqual(karthus_p["spell_name_en"], "Death Defied")
        self.assertEqual(karthus_p["cc_type"], "none")
        self.assertFalse(karthus_p["supports_wave"])

    def test_karthus_wall_of_pain_is_soft_engage_setup(self) -> None:
        karthus = self.by_alias["Karthus"]
        karthus_w = self.skill_by_key[("Karthus", "W")]
        self.assertIn("W", karthus["engage_top_spells"].split(","))
        self.assertEqual(karthus_w["cc_type"], "soft_slow")
        self.assertEqual(karthus_w["engage_gate"], "soft_cc_only")
        self.assertFalse(karthus_w["supports_wave"])

    def test_anivia_wall_and_gangplank_global_and_keg_are_engage_tools(self) -> None:
        anivia = self.by_alias["Anivia"]
        anivia_w = self.skill_by_key[("Anivia", "W")]
        gangplank = self.by_alias["Gangplank"]
        gangplank_r = self.skill_by_key[("Gangplank", "R")]
        gangplank_e = self.skill_by_key[("Gangplank", "E")]
        self.assertIn("W", anivia["engage_top_spells"].split(","))
        self.assertEqual(anivia_w["cc_type"], "utility_cc")
        self.assertEqual(gangplank["engage_top_spells"].split(",")[0], "R")
        self.assertIn("E", gangplank["engage_top_spells"].split(","))
        self.assertAlmostEqual(float(gangplank_r["engage_manual_adjustment"]), 0.45)
        self.assertAlmostEqual(float(gangplank_e["engage_manual_adjustment"]), 0.35)
        self.assertEqual(gangplank_e["cc_type"], "soft_slow")

    def test_aurelion_sol_w_is_mobility_engage(self) -> None:
        aurelion_sol = self.by_alias["AurelionSol"]
        aurelion_sol_w = self.skill_by_key[("AurelionSol", "W")]
        self.assertIn("W", aurelion_sol["engage_top_spells"].split(","))
        self.assertEqual(aurelion_sol_w["shape"], "dash")
        self.assertEqual(aurelion_sol_w["engage_gate"], "mobility_only")

    def test_jinx_chompers_are_conditional_engage_setup(self) -> None:
        jinx = self.by_alias["Jinx"]
        jinx_e = self.skill_by_key[("Jinx", "E")]
        self.assertIn("E", jinx["engage_top_spells"].split(","))
        self.assertEqual(jinx_e["cc_type"], "root")
        self.assertEqual(jinx_e["engage_gate"], "hard_cc")

    def test_ziggs_satchel_charge_is_conditional_displacement_setup(self) -> None:
        ziggs = self.by_alias["Ziggs"]
        ziggs_w = self.skill_by_key[("Ziggs", "W")]
        self.assertIn("W", ziggs["engage_top_spells"].split(","))
        self.assertEqual(ziggs_w["cc_type"], "conditional_knockback")
        self.assertIn(ziggs_w["engage_gate"], {"conditional_hard_cc", "hard_cc"})

    def test_reviewed_control_ultimates_are_not_plain_mobility_or_damage(self) -> None:
        expected = {
            ("Bard", "R"): "aoe_hard",
            ("Renata", "R"): "aoe_hard",
            ("Shen", "E"): "aoe_hard",
            ("Kalista", "R"): "aoe_hard",
            ("Kled", "R"): "single_hard",
            ("Warwick", "R"): "single_hard",
            ("Camille", "R"): "single_hard",
        }
        for key, cc_type in expected.items():
            with self.subTest(key=key):
                skill = self.skill_by_key[key]
                self.assertEqual(skill["cc_type"], cc_type)
                self.assertNotEqual(skill["engage_gate"], "mobility_only")

    def test_broad_slowing_and_pull_language_is_tagged(self) -> None:
        expected = {
            ("XinZhao", "E"): "soft_slow",
            ("Nasus", "W"): "soft_slow",
            ("Seraphine", "E"): "soft_slow",
            ("Jinx", "W"): "soft_slow",
            ("AurelionSol", "E"): "soft_slow",
            ("Zaahen", "W"): "hook_pull",
            ("Mordekaiser", "R"): "single_hard",
            ("Viego", "R"): "aoe_hard",
        }
        for key, cc_type in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.skill_by_key[key]["cc_type"], cc_type)

    def test_wave_uses_top3_and_exposes_itemized_build_profile(self) -> None:
        xerath = self.by_alias["Xerath"]
        self.assertEqual(xerath["wave_top_spells"], "Q,W,E")
        self.assertEqual(xerath["build_profile"], "ap_burst")
        self.assertIn("Luden", xerath["build_items"])
        self.assertGreater(float(xerath["build_ap"]), 0.0)

    def test_manual_spell_adjustments_are_visible_in_skill_debug(self) -> None:
        xerath_q = self.skill_by_key[("Xerath", "Q")]
        karthus_p = self.skill_by_key[("Karthus", "P")]
        self.assertAlmostEqual(float(xerath_q["wave_manual_adjustment"]), 0.9)
        self.assertAlmostEqual(
            float(xerath_q["wave_skill_score"]),
            min(3.0, float(xerath_q["wave_base_score"]) + 0.9),
            places=2,
        )
        self.assertAlmostEqual(float(karthus_p["engage_manual_adjustment"]), 0.7)
        self.assertGreater(float(karthus_p["engage_skill_score"]), float(karthus_p["engage_base_score"]))

    def test_manual_poke_bucket_overrides_are_applied(self) -> None:
        self.assertAlmostEqual(float(self.by_alias["Jayce"]["poke_score"]), 2.55)
        self.assertAlmostEqual(float(self.by_alias["Kalista"]["poke_score"]), 0.9)
        self.assertIn("poke=manual_bucket:S:2.55", self.by_alias["Jayce"]["notes"])
        self.assertIn("poke=manual_bucket:C:0.9", self.by_alias["Kalista"]["notes"])

    def test_manual_wave_bucket_overrides_are_applied(self) -> None:
        self.assertAlmostEqual(float(self.by_alias["Taliyah"]["wave_clear_score"]), 1.8)
        self.assertAlmostEqual(float(self.by_alias["Nami"]["wave_clear_score"]), 0.25)
        self.assertIn("wave=manual_bucket:S:1.8", self.by_alias["Taliyah"]["notes"])
        self.assertIn("wave=manual_bucket:F:0.25", self.by_alias["Nami"]["notes"])


if __name__ == "__main__":
    unittest.main()
