from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tierlist_render import (  # noqa: E402
    MIN_SKILL_SCALING_CHAMPIONS,
    SKILL_SCALING_SNAPSHOT,
    load_skill_scaling_snapshot,
)
import build_skill_scaling_rating as skill_builder  # noqa: E402


class SkillScalingSnapshotTests(unittest.TestCase):
    def test_canonical_snapshot_is_complete_and_loadable(self) -> None:
        loaded = load_skill_scaling_snapshot(queue_id=2400)

        self.assertGreaterEqual(len(loaded), MIN_SKILL_SCALING_CHAMPIONS)
        self.assertTrue(all(set(row) == {"pp", "z", "g"} for row in loaded.values()))
        self.assertTrue(all(row["g"] > 0 for row in loaded.values()))

        raw = json.loads(SKILL_SCALING_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["queue_id"], 2400)
        self.assertEqual(raw["region"], "TW")
        self.assertEqual(raw["patches"], ["16.11", "16.12", "16.13"])

    def test_missing_snapshot_fails_instead_of_silently_nulling_every_champion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaisesRegex(click.ClickException, "snapshot unavailable"):
                load_skill_scaling_snapshot(queue_id=2400, path=missing)

    def test_partial_mayhem_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "queue_id": 2400,
                        "champs": {"1": {"pp": 1.2, "z": 2.3, "g": 500}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(click.ClickException, "snapshot is incomplete"):
                load_skill_scaling_snapshot(queue_id=2400, path=path)

    def test_nonfinite_or_nonpositive_metrics_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-metrics.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "queue_id": 2400,
                        "champs": {
                            str(champion_id): {"pp": "NaN", "z": "Infinity", "g": 0}
                            for champion_id in range(MIN_SKILL_SCALING_CHAMPIONS)
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(click.ClickException, "invalid champion row"):
                load_skill_scaling_snapshot(queue_id=2400, path=path)

    def test_non_object_snapshot_root_is_rejected_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(click.ClickException, "unsupported.*schema"):
                load_skill_scaling_snapshot(queue_id=2400, path=path)

    def test_csv_restore_rejects_partial_invalid_and_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "snapshot.json"
            cases = {
                "partial.csv": "champ,scaling_pp,z,games\n1,1.0,2.0,500\n",
                "invalid.csv": "champ,scaling_pp,z,games\n1,NaN,Infinity,0\n",
                "duplicate.csv": (
                    "champ,scaling_pp,z,games\n"
                    + "".join(f"{cid},1.0,2.0,500\n" for cid in range(150))
                    + "1,1.0,2.0,500\n"
                ),
            }
            with mock.patch.object(skill_builder, "SITE_SNAPSHOT", out):
                for name, content in cases.items():
                    with self.subTest(name=name):
                        csv_path = tmp_path / name
                        csv_path.write_text(content, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            skill_builder.snapshot_from_analysis_csv(csv_path)
                        self.assertFalse(out.exists())

    def test_mayhem_snapshot_is_not_used_for_classic_aram(self) -> None:
        self.assertEqual(load_skill_scaling_snapshot(queue_id=450), {})


if __name__ == "__main__":
    unittest.main()
