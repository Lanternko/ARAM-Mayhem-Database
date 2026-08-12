from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from aram_nn.augment_residual import (  # noqa: E402
    CATEGORY_ORDER, AugmentResidualNN, apply_strength_table, checkpoint_payload,
    cross_fit_strength, fit_residual_model, fit_strength_table, metric_values,
    predict_logits, probability, validation_verdict,
)
from scripts import export_augment_round3 as exporter  # noqa: E402
from scripts import train_augment_round3 as trainer  # noqa: E402


SENTINEL = "sentinel.person@example.invalid/PUUID-SECRET/RiotName#TW2"


def roster(seed: int = 0) -> str:
    rows = []
    for index in range(10):
        rows.append({
            "teamId": 100 if index < 5 else 200,
            "championId": seed * 10 + index + 1,
            "augments": [1001 + index, 1101 + index, 1201 + index, 1301 + index],
            "puuid": SENTINEL, "summonerName": SENTINEL,
        })
    return json.dumps(rows)


def create_db(path: Path, games: list[tuple[str, str, int]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE games (game_id TEXT PRIMARY KEY, queue_id INTEGER, patch TEXT, "
        "created_ms INTEGER, blue_wins INTEGER, participants_json TEXT)"
    )
    connection.execute(
        "CREATE INDEX idx_games_queue_patch_created ON games(queue_id, patch, created_ms)"
    )
    for game_id, patch, created in games:
        connection.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?)",
            (game_id, 2400, patch, created, created % 2, roster(created % 10)),
        )
    connection.commit(); connection.close()


def write_games(path: Path, games: list[tuple[int, int, str]]) -> None:
    columns = {name: [] for name in exporter.SCHEMA.names}
    for gidx, created, patch in games:
        for participant in range(10):
            columns["gidx"].append(gidx); columns["created_ms"].append(created)
            columns["patch"].append(patch); columns["champ"].append(participant + 1)
            columns["win"].append(int(participant < 5)); columns["augments"].append([1001 + participant])
    pq.write_table(pa.Table.from_pydict(columns, schema=exporter.SCHEMA), path)


class ExportTests(unittest.TestCase):
    def test_export_query_plan_has_no_temp_btree_global_sort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "games.db"
            create_db(db, [("g2", "16.12.1", 20), ("g1", "16.13.1", 10)])
            connection = sqlite3.connect(db)
            plan = connection.execute(
                "EXPLAIN QUERY PLAN " + exporter.EXPORT_ROWS_SQL,
                (2400, 20, "16.12", "16.12.%", "16.13", "16.13.%"),
            ).fetchall()
            connection.close()
            details = " ".join(str(row[3]).upper() for row in plan)
            self.assertIn("IDX_GAMES_QUEUE_PATCH_CREATED", details)
            self.assertNotIn("TEMP B-TREE", details)
            self.assertNotIn("SORT", details)

    def test_schema_meta_dense_ten_rows_and_no_pii_or_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); db = root / f"{SENTINEL.replace('/', '_')}.db"; out = root / "ended.parquet"
            create_db(db, [("private-1", "16.12.1", 10), ("private-2", "16.13.1", 20)])
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                meta = exporter.export_ended_patches(db, out)
            table = pq.read_table(out)
            self.assertEqual(table.schema, exporter.SCHEMA)
            self.assertEqual(set(meta), exporter.EXPORT_META_KEYS)
            self.assertEqual(table["gidx"].to_pylist(), [0] * 10 + [1] * 10)
            self.assertEqual(meta["participants"], meta["games"] * 10)
            serialized = stdout.getvalue() + stderr.getvalue() + json.dumps(meta)
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn(str(root.resolve()), serialized)
            self.assertEqual(meta["source"], db.name)
            self.assertEqual(meta["output"], out.name)

    def test_first_read_pins_transaction_snapshot_during_concurrent_insert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); db = root / "games.db"; out = root / "ended.parquet"
            create_db(db, [("g1", "16.12.1", 10), ("g2", "16.13.1", 20)])

            def insert_after_snapshot() -> None:
                writer = sqlite3.connect(db, timeout=5)
                writer.execute("INSERT INTO games VALUES (?,?,?,?,?,?)", ("late-private-id", 2400, "16.13.1", 30, 1, roster(3)))
                writer.commit(); writer.close()

            meta = exporter.export_ended_patches(db, out, after_snapshot=insert_after_snapshot)
            self.assertEqual(meta["games"], 2)
            self.assertEqual(meta["cutoff_created_ms"], 20)
            self.assertEqual(pq.read_table(out).num_rows, 20)

    def test_failure_is_sanitized_and_partial_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); db = root / f"{SENTINEL.replace('/', '_')}.db"; out = root / "x.parquet"
            sqlite3.connect(db).close()
            with self.assertRaisesRegex(exporter.Round3ExportError, "^E_EXPORT_FAILED$") as caught:
                exporter.export_ended_patches(db, out)
            self.assertNotIn(SENTINEL, str(caught.exception))
            self.assertFalse(out.with_suffix(".parquet.partial").exists())

    def test_materialize_exact_fingerprint_and_physical_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); ended = root / "ended.parquet"; current = root / "current.parquet"
            combined = root / "combined_16.12_16.15.parquet"
            dev = root / "dev.parquet"; frozen = root / "frozen.parquet"
            write_games(ended, [(0, 1, "16.12"), (1, 2, "16.13")])
            write_games(current, [
                (900000, 1786000000000, "16.14"),
                (exporter.VALIDATION_START[1], exporter.VALIDATION_START[0], "16.15"),
                (exporter.TEST_START[1], exporter.TEST_START[0], "16.15"),
                (exporter.TEST_START[1] + 1, exporter.TEST_START[0] + 1, "16.15"),
            ])
            combined_meta, dev_meta, frozen_meta = exporter.materialize_split(
                ended, current, combined, dev, frozen,
                expected_current_sha256=exporter.sha256(current),
            )
            self.assertEqual(set(combined_meta), exporter.SPLIT_META_KEYS)
            self.assertEqual(set(dev_meta), exporter.SPLIT_META_KEYS)
            self.assertEqual(combined_meta["artifact_kind"], "combined")
            self.assertEqual(dev_meta["validation_start_source"], list(exporter.VALIDATION_START))
            self.assertEqual(dev_meta["test_start_source"], list(exporter.TEST_START))
            combined_table, dev_table, frozen_table = (
                pq.read_table(combined), pq.read_table(dev), pq.read_table(frozen)
            )
            self.assertEqual(
                sorted(set(combined_table["gidx"].to_pylist())),
                list(range(combined_meta["games"])),
            )
            self.assertEqual(sorted(set(dev_table["gidx"].to_pylist())), list(range(dev_meta["games"])))
            self.assertEqual(sorted(set(frozen_table["gidx"].to_pylist())), list(range(frozen_meta["games"])))
            self.assertEqual(combined_table.num_rows, dev_table.num_rows + frozen_table.num_rows)
            self.assertEqual(combined_meta["output_sha256"], exporter.sha256(combined))
            self.assertEqual(dev_meta["output_sha256"], exporter.sha256(dev))
            self.assertEqual(frozen_meta["output_sha256"], exporter.sha256(frozen))
            for path in (combined, dev, frozen):
                counts = Counter(pq.read_table(path)["gidx"].to_pylist())
                self.assertEqual(set(counts.values()), {10})
            self.assertEqual(set(frozen_table["patch"].to_pylist()), {"16.15"})
            self.assertTrue(all(value < exporter.TEST_START[0] for value in dev_table.filter(pa.compute.equal(dev_table["patch"], "16.15"))["created_ms"].to_pylist()))
            self.assertTrue(all(value >= exporter.TEST_START[0] for value in frozen_table["created_ms"].to_pylist()))

    def test_wrong_split_fingerprint_cleans_partials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); ended = root / "ended.parquet"; current = root / "current.parquet"
            combined = root / "combined.parquet"; dev = root / "dev.parquet"; frozen = root / "frozen.parquet"
            write_games(ended, [(0, 1, "16.12")]); write_games(current, [(1, 2, "16.15")])
            with self.assertRaisesRegex(exporter.Round3ExportError, "E_SPLIT_FINGERPRINT"):
                exporter.materialize_split(
                    ended, current, combined, dev, frozen,
                    expected_current_sha256=exporter.sha256(current),
                )
            self.assertFalse(combined.with_suffix(".parquet.partial").exists())
            self.assertFalse(dev.with_suffix(".parquet.partial").exists())
            self.assertFalse(frozen.with_suffix(".parquet.partial").exists())

    def test_materialize_cli_requires_combined_output(self) -> None:
        result = CliRunner().invoke(exporter.main, [
            "materialize", "--ended-parquet", "ended.parquet",
            "--current-parquet", "current.parquet",
            "--dev-out", "dev.parquet", "--frozen-out", "frozen.parquet",
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--combined-out", result.output)


class FeatureTests(unittest.TestCase):
    def test_independent_odds_exact_numeric_parity_with_reference_formula(self) -> None:
        champions = np.asarray([1, 1, 2, 2, 1, 2], dtype=np.int64)
        labels = np.asarray([1, 0, 1, 0, 1, 0], dtype=np.float32)
        augments = np.asarray([
            [1, 2, 3, 4], [1, 3, 2, 4], [2, 3, 4, 1],
            [2, 4, 3, 1], [3, 1, 4, 2], [4, 2, 1, 3],
        ], dtype=np.int64)
        fit = np.asarray([True, True, True, True, False, False])
        table = fit_strength_table(champions, labels, augments, fit, n_champions=3, n_augments=6)
        actual, uncertainty = apply_strength_table(table, champions[4:], augments[4:])

        # Independent implementation of the canonical round-2 formula.
        c, y, a = champions[fit], labels[fit].astype(np.float64), augments[fit]
        global_rate = float(y.mean())
        champion_games = np.bincount(c, minlength=3).astype(np.float64)
        champion_wins = np.bincount(c, weights=y, minlength=3).astype(np.float64)
        champion_rate = (champion_wins + 100.0 * global_rate) / (champion_games + 100.0)
        pair_games = np.zeros((3, 6), dtype=np.float64)
        pair_wins = np.zeros((3, 6), dtype=np.float64)
        for slot in range(4):
            np.add.at(pair_games, (c, a[:, slot]), 1.0)
            np.add.at(pair_wins, (c, a[:, slot]), y)
        pair_rate = (pair_wins + 100.0 * champion_rate[:, None]) / (pair_games + 100.0)

        def reference_logit(value: np.ndarray) -> np.ndarray:
            clipped = np.clip(value, 1e-6, 1.0 - 1e-6)
            return np.log(clipped / (1.0 - clipped))

        eval_champions, eval_augments = champions[4:], augments[4:]
        champion_logits = reference_logit(champion_rate[eval_champions])
        effects = reference_logit(pair_rate[eval_champions[:, None], eval_augments]) - champion_logits[:, None]
        expected = champion_logits[:, None] + np.cumsum(effects, axis=1)
        np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)

        alpha = pair_wins + 100.0 * champion_rate[:, None]
        beta = pair_games - pair_wins + 100.0 * (1.0 - champion_rate[:, None])
        selected_alpha = alpha[eval_champions[:, None], eval_augments]
        selected_beta = beta[eval_champions[:, None], eval_augments]
        total = selected_alpha + selected_beta
        expected_variance = selected_alpha * selected_beta / (total * total * (total + 1.0))
        np.testing.assert_allclose(uncertainty[:, :, 0], np.log1p(pair_games[eval_champions[:, None], eval_augments]), rtol=0, atol=1e-7)
        np.testing.assert_allclose(uncertainty[:, :, 1], expected_variance, rtol=2e-6, atol=1e-9)
        np.testing.assert_array_equal(uncertainty[:, :, 2], pair_games[eval_champions[:, None], eval_augments] == 0)

    def test_oof_is_game_level_and_patch_isolated(self) -> None:
        game_ids = np.repeat(np.arange(10), 2)
        patches = np.asarray(["16.14"] * 10 + ["16.15"] * 10)
        champions = np.ones(20, dtype=np.int64)
        augments = np.asarray([[index // 2 + 1] * 4 for index in range(20)], dtype=np.int64)
        labels = np.asarray(([0, 1] * 5) + ([1, 0] * 5), dtype=np.float32)
        mask = np.ones(20, dtype=bool)
        base_a, uncertainty_a = cross_fit_strength(champions, labels, augments, game_ids, patches, mask)
        changed = labels.copy(); changed[patches == "16.15"] = 1.0 - changed[patches == "16.15"]
        base_b, _ = cross_fit_strength(champions, changed, augments, game_ids, patches, mask)
        np.testing.assert_allclose(base_a[patches == "16.14"], base_b[patches == "16.14"])
        self.assertTrue(np.all(uncertainty_a[:, 0, 2] == 1.0))

    def test_evaluation_table_ignores_nontraining_outcomes(self) -> None:
        champions = np.asarray([1, 1, 1, 1, 1, 1], dtype=np.int64)
        labels = np.asarray([0, 1, 0, 1, 0, 1], dtype=np.float32)
        augments = np.asarray([[1, 2, 3, 4]] * 6, dtype=np.int64)
        fit = np.asarray([True, True, True, True, False, False])
        table_a = fit_strength_table(champions, labels, augments, fit)
        changed = labels.copy(); changed[~fit] = 1.0 - changed[~fit]
        table_b = fit_strength_table(champions, changed, augments, fit)
        base_a, uncertainty_a = apply_strength_table(table_a, champions[~fit], augments[~fit])
        base_b, uncertainty_b = apply_strength_table(table_b, champions[~fit], augments[~fit])
        np.testing.assert_array_equal(base_a, base_b)
        np.testing.assert_array_equal(uncertainty_a, uncertainty_b)

    def test_uncertainty_support_monotonic_and_unseen(self) -> None:
        champions = np.ones(101, dtype=np.int64)
        labels = np.asarray([0, 1] * 50 + [1], dtype=np.float32)
        augments = np.ones((101, 4), dtype=np.int64)
        table = fit_strength_table(champions, labels, augments, np.ones(101, dtype=bool), n_augments=3)
        _, features = apply_strength_table(table, np.asarray([1, 1]), np.asarray([[1, 1, 1, 1], [2, 2, 2, 2]]))
        self.assertGreater(features[0, 0, 0], features[1, 0, 0])
        self.assertLess(features[0, 0, 1], features[1, 0, 1])
        self.assertEqual(features[0, 0, 2], 0.0); self.assertEqual(features[1, 0, 2], 1.0)

    def test_future_augments_do_not_change_prior_slots(self) -> None:
        champions = np.ones(20, dtype=np.int64); labels = np.asarray([0, 1] * 10, dtype=np.float32)
        training_augments = np.tile(np.asarray([[1, 2, 3, 4]]), (20, 1))
        table = fit_strength_table(champions, labels, training_augments, np.ones(20, dtype=bool), n_augments=7)
        candidates = np.asarray([[1, 2, 3, 4], [1, 2, 5, 6]], dtype=np.int64)
        base, uncertainty = apply_strength_table(table, np.asarray([1, 1]), candidates)
        np.testing.assert_allclose(base[0, :2], base[1, :2]); np.testing.assert_allclose(uncertainty[0, :2], uncertainty[1, :2])
        model = AugmentResidualNN(n_champions=2, n_augments=7, category_matrix=torch.zeros((7, len(CATEGORY_ORDER))), use_uncertainty=True)
        with torch.no_grad():
            output = model(torch.tensor([1, 1]), torch.from_numpy(candidates), torch.from_numpy(uncertainty))
        torch.testing.assert_close(output[0, :2], output[1, :2])

    def test_tiny_residual_training_smoke(self) -> None:
        rng = np.random.default_rng(2400)
        champions = np.ones(20, dtype=np.int64)
        augments = rng.integers(1, 5, size=(20, 4), dtype=np.int64)
        labels = np.asarray([0, 1] * 10, dtype=np.float32)
        base = np.zeros((20, 4), dtype=np.float32)
        uncertainty = np.zeros((20, 4, 3), dtype=np.float32)
        model = AugmentResidualNN(
            n_champions=2, n_augments=5,
            category_matrix=torch.zeros((5, len(CATEGORY_ORDER))),
            use_uncertainty=True,
        )
        state, history, temperature = fit_residual_model(
            model,
            champions=champions[:15], augments=augments[:15], labels=labels[:15],
            base_logits=base[:15], uncertainty=uncertainty[:15],
            sample_weights=np.ones(15, dtype=np.float32),
            validation=(champions[15:], augments[15:], labels[15:], base[15:], uncertainty[15:]),
            epochs=1, batch_size=8,
        )
        self.assertTrue(state); self.assertEqual(len(history), 1); self.assertGreater(temperature, 0)
        predictions = probability(predict_logits(model, champions[15:], augments[15:], base[15:], uncertainty[15:], batch_size=8), temperature)
        metrics = metric_values(labels[15:], predictions, augments[15:] > 0)
        self.assertEqual(set(metrics["per_slot"]), {"1", "2", "3", "4"})


class ContractAndVerdictTests(unittest.TestCase):
    def row(self, *, ll: float = 0.5, brier: float = 0.2, auc: float = 0.7, accuracy: float = 0.6, delta: float = -0.00003) -> dict[str, float]:
        return {"logloss": ll, "brier": brier, "auc": auc, "accuracy": accuracy, "paired_delta": delta}

    def baseline(self) -> dict[str, float]:
        return self.row(delta=0.0)

    def test_verdict_zero_and_one_eligible(self) -> None:
        zero = {"P0": self.baseline(), "P1": self.row(delta=0), "P2": self.row(delta=0), "P3": self.row(delta=0)}
        self.assertEqual(validation_verdict(zero)["verdict"], "STOP_NO_TEST")
        one = {**zero, "P2": self.row(ll=0.49)}
        self.assertEqual(validation_verdict(one)["verdict"], "SELECT_P2")

    def test_verdict_multiple_lexicographic_equality_and_complete_tie(self) -> None:
        multiple = {"P0": self.baseline(), "P1": self.row(ll=.49, brier=.19), "P2": self.row(ll=.48, brier=.2), "P3": self.row(ll=.48, brier=.19)}
        self.assertEqual(validation_verdict(multiple)["verdict"], "SELECT_P3")
        equality = {"P0": self.baseline(), "P1": self.row(delta=-.00002, accuracy=.5998, auc=.6999, brier=.20002), "P2": self.row(delta=0), "P3": self.row(delta=0)}
        self.assertEqual(validation_verdict(equality)["verdict"], "SELECT_P1")
        tie = {"P0": self.baseline(), "P1": self.row(), "P2": self.row(), "P3": self.row()}
        self.assertEqual(validation_verdict(tie)["verdict"], "SELECT_P1")

    def test_checkpoint_exact_keys_and_safe_reload(self) -> None:
        payload = checkpoint_payload({"weight": torch.ones(1)}, {"pilot": "P1"}, ("16.14", "16.15"))
        self.assertEqual(set(payload), trainer.CHECKPOINT_KEYS)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); pilot_dir = root / "p1"; pilot_dir.mkdir()
            path = pilot_dir / "P1.pt"; torch.save(payload, path)
            loaded = torch.load(path, weights_only=True)
            self.assertEqual(set(loaded), trainer.CHECKPOINT_KEYS)
            self.assertNotIn("participants", loaded); self.assertNotIn("timestamp", loaded)
            self.assertEqual(set(trainer._load_checkpoint(root, "P1")), trainer.CHECKPOINT_KEYS)
            self.assertFalse((root / "P1.pt").exists())

    def test_pilot_output_routing_and_collision_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pilots"
            for pilot in ("P0", "P1", "P2", "P3"):
                target = trainer._pilot_output_dir(root, pilot, create=True)
                self.assertEqual(target, (root / pilot.lower()).resolve())
                self.assertEqual(target.parent, root.resolve())
            collision_root = Path(temporary) / "collision"
            collision_root.mkdir(); (collision_root / "p0").write_text("not a directory")
            with self.assertRaisesRegex(trainer.Round3TrainError, "^E_OUTPUT_ROUTE$"):
                trainer._pilot_output_dir(collision_root, "P0", create=True)
            outside = Path(temporary) / "outside"; outside.mkdir()
            symlink_root = Path(temporary) / "symlink-root"
            try:
                symlink_root.symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(trainer.Round3TrainError, "^E_OUTPUT_ROUTE$"):
                    trainer._pilot_output_dir(symlink_root, "P0", create=True)

    def test_trainer_cli_has_no_test_option(self) -> None:
        runner = CliRunner()
        result = runner.invoke(trainer.main, ["pilot", "--test-parquet", "forbidden"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such option", result.output)
        self.assertNotIn(SENTINEL, result.output)


if __name__ == "__main__":
    unittest.main()
