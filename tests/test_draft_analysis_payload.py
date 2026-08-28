from __future__ import annotations

import copy
import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

import click


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tierlist_render as render  # noqa: E402


class DraftModelValidationTests(unittest.TestCase):
    def _champion_model(self, coef: list[object] | None = None) -> dict:
        return {
            "kind": "champion_lr",
            "intercept": 0.1,
            "coef": [0.25, -0.2] if coef is None else coef,
            "feature_names": ["champion:1", "champion:2"],
            "champ_to_idx": {"1": 0, "2": 1},
            "profiles": {},
        }

    def test_zero_nonfinite_and_mismatched_coefficients_are_rejected(self) -> None:
        for coef in ([0.0, 0.0], [0.1, float("nan")], [0.1]):
            with self.subTest(coef=coef):
                with self.assertRaises(click.ClickException):
                    render.validate_draft_model_payload(self._champion_model(list(coef)))

    def test_fallback_uses_vocab_index_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            (model_dir / "champ_to_idx.json").write_text(
                json.dumps({"22": 1, "11": 0}), encoding="utf-8"
            )
            (model_dir / "lr_weights.json").write_text(
                json.dumps({"coef": [0.4, -0.3], "intercept": 0.05}),
                encoding="utf-8",
            )
            payload = render._load_draft_champion_lr_fallback(model_dir, {})
        self.assertEqual(payload["kind"], "champion_lr")
        self.assertEqual(payload["feature_names"], ["champion:11", "champion:22"])
        self.assertEqual(payload["champ_to_idx"], {"11": 0, "22": 1})
        self.assertEqual(payload["coef"], [0.4, -0.3])
        self.assertNotIn("composition", payload["fallback_reason"].lower().split(";")[-1])

    def test_noncanonical_feature_fails_closed_without_echo(self) -> None:
        model = self._champion_model()
        secret = "champion:1<script>private-token"
        model["feature_names"][0] = secret
        with self.assertRaises(click.ClickException) as caught:
            render.validate_draft_model_payload(model)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(str(caught.exception), render._DRAFT_SCHEMA_ERROR)

    def test_canonical_champion_feature_schema_is_accepted(self) -> None:
        render.validate_draft_model_payload(self._champion_model())

    def test_canonical_composition_feature_schema_is_accepted(self) -> None:
        from aram_nn.recommend import (
            AD_BINS,
            CORE_COLUMNS,
            ENGAGE_GROUPS,
            FRONT_GROUPS,
            LACK_THRESHOLDS,
            POKE_GROUPS,
            ROLE_COLUMNS,
            SCORE_COLUMNS,
            WAVE_GROUPS,
        )

        vocab = {"1": 0, "2": 1}
        names = render._draft_composition_feature_names(
            vocab,
            score_columns=SCORE_COLUMNS,
            role_columns=ROLE_COLUMNS,
            ad_bins=AD_BINS,
            front_groups=FRONT_GROUPS,
            wave_groups=WAVE_GROUPS,
            engage_groups=ENGAGE_GROUPS,
            poke_groups=POKE_GROUPS,
        )
        model = {
            "kind": "composition_lr",
            "intercept": 0.0,
            "coef": [0.01] * len(names),
            "feature_names": names,
            "champ_to_idx": vocab,
            "profiles": {},
            "meta": {
                "score_columns": list(SCORE_COLUMNS),
                "core_columns": list(CORE_COLUMNS),
                "role_columns": list(ROLE_COLUMNS),
                "lack_thresholds": {key: float(value) for key, value in LACK_THRESHOLDS.items()},
                "ad_bins": list(AD_BINS),
                "front_groups": list(FRONT_GROUPS),
                "wave_groups": list(WAVE_GROUPS),
                "engage_groups": list(ENGAGE_GROUPS),
                "poke_groups": list(POKE_GROUPS),
            },
        }
        render.validate_draft_model_payload(model)


class DraftProfileHydrationTests(unittest.TestCase):
    @staticmethod
    def _profile(base: float) -> dict:
        return {
            "physical_dpm": 100.0 + base,
            "magic_dpm": 200.0 + base,
            "true_dpm": 10.0 + base,
            "scores": {
                "frontline_score": 1.0 + base,
                "damage_score": 2.0 + base,
                "engage_score": 3.0 + base,
                "wave_clear_score": 4.0 + base,
                "poke_score": 5.0 + base,
                "sustain_score": 6.0 + base,
                "cc_score": 7.0 + base,
            },
            "roles": {},
        }

    def _payload(self) -> dict:
        model = {
            "kind": "champion_lr",
            "intercept": 0.0,
            "coef": [0.2, -0.2],
            "feature_names": ["champion:1", "champion:2"],
            "champ_to_idx": {"1": 0, "2": 1},
            "profiles": {"1": self._profile(0.0), "2": self._profile(1.0)},
        }
        return {"champs": {"1": {"comp": {}}, "2": {"comp": {}}}, "draftModel": model}

    def test_hydration_maps_all_public_axes_and_damage_fields(self) -> None:
        payload = self._payload()
        render.hydrate_draft_champion_profiles(payload, payload["draftModel"])
        self.assertEqual(
            payload["champs"]["1"]["comp"],
            {
                "phys": 100.0,
                "magic": 200.0,
                "true": 10.0,
                "front": 1.0,
                "damage": 2.0,
                "engage": 3.0,
                "wave": 4.0,
                "poke": 5.0,
                "sustain": 6.0,
                "cc": 7.0,
            },
        )
        render.validate_draft_public_payload(payload)

    def test_invalid_model_damage_keeps_existing_public_mix_as_a_trio(self) -> None:
        payload = self._payload()
        payload["champs"]["1"]["comp"] = {
            "phys": 0.5,
            "magic": 0.4,
            "true": 0.1,
        }
        payload["draftModel"]["profiles"]["1"].update({
            "physical_dpm": -56_306_504.119,
            "magic_dpm": 1_234.0,
            "true_dpm": 81_612.002,
        })

        render.hydrate_draft_champion_profiles(payload, payload["draftModel"])

        self.assertEqual(
            {key: payload["champs"]["1"]["comp"][key] for key in ("phys", "magic", "true")},
            {"phys": 0.5, "magic": 0.4, "true": 0.1},
        )
        self.assertEqual(payload["champs"]["1"]["comp"]["front"], 1.0)

    def test_all_center_payload_is_rejected(self) -> None:
        payload = self._payload()
        payload["draftModel"]["profiles"]["2"] = copy.deepcopy(
            payload["draftModel"]["profiles"]["1"]
        )
        render.hydrate_draft_champion_profiles(payload, payload["draftModel"])
        with self.assertRaises(click.ClickException):
            render.validate_draft_public_payload(payload)

    def test_canonical_bundle_is_usable_and_matches_fixture(self) -> None:
        model = render.load_draft_composition_lr_payload(
            ROOT / "models" / "composition_lr_pooled_recency_7d"
        )
        self.assertIsNotNone(model)
        assert model is not None
        self.assertIn(model["kind"], {"composition_lr", "champion_lr"})
        self.assertGreaterEqual(len(model["profiles"]), 150)
        payload = {
            "champs": {
                cid: {"comp": {"phys": 0.5, "magic": 0.5, "true": 0.0}}
                for cid in model["profiles"]
            },
            "draftModel": model,
        }
        render.hydrate_draft_champion_profiles(payload, model)
        render.validate_draft_public_payload(payload)
        for champ in payload["champs"].values():
            self.assertGreaterEqual(champ["comp"]["phys"], 0.0)
            self.assertGreaterEqual(champ["comp"]["magic"], 0.0)
            self.assertGreaterEqual(champ["comp"]["true"], 0.0)

        ally = [67, 157, 136, 63, 245]
        enemy = [904, 134, 18, 15, 166]
        weights = {
            name.removeprefix("champion:"): coef
            for name, coef in zip(model["feature_names"], model["coef"])
            if name.startswith("champion:")
        }
        logit = model["intercept"]
        logit += sum(weights[str(cid)] for cid in ally)
        logit -= sum(weights[str(cid)] for cid in enemy)
        probability = 1.0 / (1.0 + math.exp(-logit))
        self.assertAlmostEqual(probability, 0.6072904411, places=8)


class DraftStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (SCRIPTS / "templates" / "site.js").read_text(encoding="utf-8")
        cls.css = (SCRIPTS / "templates" / "site.css").read_text(encoding="utf-8")

    def test_js_supports_both_models_and_parameterized_analysis(self) -> None:
        self.assertIn("model.kind !== 'composition_lr' && model.kind !== 'champion_lr'", self.js)
        self.assertIn("function metaPickAnalysisHtml(yourIds, bestIds, copy, options)", self.js)
        self.assertIn("metaPickAnalysisHtml(yourIds, bestIds, copy)", self.js)
        self.assertIn("metaPickAnalysisHtml(enemyPicks, teamPicks, copy, {", self.js)
        self.assertIn("game-analysis is-draft-analysis", self.js)

    def test_draft_route_empties_legacy_metric_and_guards_final_wr(self) -> None:
        self.assertIn("if (metricsEl) metricsEl.innerHTML = '';", self.js)
        self.assertIn("showFinalWr: fullDraft && mu.finalWr != null", self.js)
        self.assertIn("const showFinalWr = opts.showFinalWr", self.js)
        self.assertIn("Final WR compares only", self.js)
        self.assertIn("最終勝率只比較", self.js)

    def test_css_has_theme_and_mobile_width_contract(self) -> None:
        for token in (
            "--radar-grid", "--radar-label", "--radar-compare-stroke",
            "--radar-compare-fill", "--radar-compare-dot",
        ):
            self.assertIn(token, self.css)
        self.assertIn('[data-theme="light"] .game-an-grade.is-grade-b', self.css)
        self.assertIn('[data-theme="light"] .game-an-grade.is-grade-c', self.css)
        self.assertIn(".draft-metrics:empty { display: none; }", self.css)
        self.assertIn("@media (max-width: 900px)", self.css)
        self.assertIn("width: calc(100% + 32px);", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("width: calc(100% + 24px);", self.css)
        mobile_match = re.search(
            r"@media \(max-width: 900px\) \{\s*\.view-draft\.is-active \{([^}]*)\}",
            self.css,
        )
        self.assertIsNotNone(mobile_match)
        assert mobile_match is not None
        self.assertNotIn("100vw", mobile_match.group(1))


if __name__ == "__main__":
    unittest.main()
