from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import click


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tierlist_render import (  # noqa: E402
    _draft_model_has_signal,
    _load_draft_champion_lr_fallback,
    hydrate_draft_comp_profiles,
    load_draft_composition_lr_payload,
    validate_draft_analysis_payload,
)


class DraftAnalysisPayloadTests(unittest.TestCase):
    def test_zero_coefficient_model_is_not_usable(self) -> None:
        self.assertFalse(
            _draft_model_has_signal(
                {"coef": [0.0, 0.0], "feature_names": ["champion:1", "champion:2"]}
            )
        )

    def test_champion_lr_fallback_uses_vocab_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            (model_dir / "lr_weights.json").write_text(
                json.dumps({"coef": [0.25, -0.5], "intercept": 0.1}),
                encoding="utf-8",
            )
            (model_dir / "champ_to_idx.json").write_text(
                json.dumps({"22": 1, "11": 0}),
                encoding="utf-8",
            )

            payload = _load_draft_champion_lr_fallback(
                model_dir,
                profiles={"11": {"scores": {}}, "22": {"scores": {}}},
                trained_through="16.15",
            )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["kind"], "champion_lr")
        self.assertEqual(payload["feature_names"], ["champion:11", "champion:22"])
        self.assertTrue(_draft_model_has_signal(payload))

    def test_profiles_hydrate_all_six_public_radar_axes(self) -> None:
        payload = {"champs": {"11": {"comp": {}}}}
        model = {
            "profiles": {
                "11": {
                    "physical_dpm": 100.0,
                    "magic_dpm": 200.0,
                    "true_dpm": 10.0,
                    "scores": {
                        "wave_clear_score": 1.0,
                        "cc_score": 2.0,
                        "engage_score": 3.0,
                        "damage_score": 4.0,
                        "poke_score": 5.0,
                        "sustain_score": 6.0,
                        "frontline_score": 7.0,
                    },
                }
            }
        }

        self.assertEqual(hydrate_draft_comp_profiles(payload, model), 1)
        comp = payload["champs"]["11"]["comp"]
        self.assertEqual(
            [comp[key] for key in ("front", "damage", "engage", "wave", "sustain", "cc")],
            [7.0, 4.0, 3.0, 1.0, 6.0, 2.0],
        )

    def test_validation_rejects_center_point_radar_payload(self) -> None:
        payload = {
            "champs": {"11": {"comp": {}}},
            "draftModel": None,
        }
        with self.assertRaisesRegex(click.ClickException, "axes have no usable signal"):
            validate_draft_analysis_payload(payload)

    def test_canonical_model_produces_usable_analysis_payload(self) -> None:
        model = load_draft_composition_lr_payload()
        self.assertIsNotNone(model)
        assert model is not None
        self.assertIn(model["kind"], {"composition_lr", "champion_lr"})
        self.assertTrue(_draft_model_has_signal(model))
        self.assertGreaterEqual(len(model["profiles"]), 150)

        payload = {
            "champs": {champion_id: {"comp": {}} for champion_id in model["profiles"]},
            "draftModel": model,
        }
        hydrate_draft_comp_profiles(payload, model)
        validate_draft_analysis_payload(payload)


if __name__ == "__main__":
    unittest.main()
