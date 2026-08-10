"""Focused tests for Meta Pick 5-round leaderboard MVP."""
from __future__ import annotations

import itertools
import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aram_nn.site.db import connect as db_connect
from aram_nn.site.meta_pick import (
    TOTAL_COMBOS,
    MetaPickError,
    PatchMismatch,
    SnapshotUnavailable,
    canonical_ids,
    clear_snapshot_cache,
    list_leaderboard,
    load_snapshot,
    nickname_key,
    normalize_nickname_display,
    rank_among,
    recompute_run,
    rescore_stale_runs,
    score_all_teams,
    score_round,
    score_team,
    submit_run,
    upsert_best_run,
    utc_now_iso,
)


def _champ(
    wr: float,
    *,
    tags: list[str] | None = None,
    pairs: list[dict] | None = None,
    comp: dict | None = None,
) -> dict:
    base_comp = {
        "phys": 500.0,
        "magic": 500.0,
        "true": 100.0,
        "wave": 1.0,
        "cc": 1.0,
        "engage": 1.0,
        "damage": 1.5,
        "poke": 1.0,
        "sustain": 1.0,
        "front": 1.0,
    }
    if comp:
        base_comp.update(comp)
    return {
        "wr": wr,
        "g": 200,
        "tags": tags or [],
        "pairs": pairs or [],
        "comp": base_comp,
    }


def mini_snapshot(*, patch: str = "16.10") -> dict:
    """10 champions with deterministic WRs + a few pair lifts."""
    # ids 1..10; higher id → higher base WR so ranking is stable.
    champs: dict[str, dict] = {}
    for i in range(1, 11):
        wr = 0.45 + i * 0.01  # 0.46 .. 0.55
        pairs = []
        # Symmetric-ish lifts between consecutive high ids.
        if i < 10:
            pairs.append({"id": i + 1, "lift": 0.02, "g": 50})
        if i > 1:
            pairs.append({"id": i - 1, "lift": 0.02, "g": 50})
        champs[str(i)] = _champ(wr, pairs=pairs)
    return {
        "patch_prefix": patch,
        "champs": champs,
        "recommendation_composition": {
            "weight": 0.25,
            "clamp": 0.05,
            "lack_thresholds": {
                "wave": 3.0,
                "cc": 3.0,
                "engage": 2.2,
                "damage": 5.5,
                "poke": 2.0,
                "sustain": 1.5,
                "front": 1.8,
            },
            "table_weights": {
                "ad_front": 0.55,
                "poke_front": 0.30,
                "wave_engage": 0.15,
                "all_lacks": 0.15,
                "mage_ad": 0.20,
                "marksman_ad": 0.20,
            },
            "tables": {
                "ad_front": {"1 front|45-55% AD": 0.01},
                "poke_front": {"1 front|poke ok": 0.0},
                "wave_engage": {"wave ok|engage ok": 0.0},
                "all_lacks": {"0": 0.0, "1": -0.005, "2+": -0.01},
                "mage_ad": {},
                "marksman_ad": {},
            },
            "damage_mix": {
                "target_ad_share": 0.4,
                "weight": 0.18,
                "clamp": 0.025,
            },
        },
    }


def five_rounds(pool: list[str], picks: list[str]) -> list[dict]:
    return [{"pool_ids": list(pool), "picked_ids": list(picks)} for _ in range(5)]


class NicknameTests(unittest.TestCase):
    def test_normalize_trim_collapse_length(self) -> None:
        self.assertEqual(normalize_nickname_display("  ab  "), "ab")
        self.assertEqual(normalize_nickname_display("a  b"), "a b")
        with self.assertRaises(MetaPickError):
            normalize_nickname_display("a")
        with self.assertRaises(MetaPickError):
            normalize_nickname_display("x" * 17)
        # 16 code points ok (emoji counts as one each in Python str len for most).
        self.assertEqual(len(normalize_nickname_display("英雄" * 8)), 16)

    def test_nickname_key_nfkc_casefold(self) -> None:
        a = nickname_key(normalize_nickname_display("Foo"))
        b = nickname_key(normalize_nickname_display("foo"))
        self.assertEqual(a, b)
        # Fullwidth latin folds under NFKC + casefold.
        wide = nickname_key(normalize_nickname_display("Ｆｏｏ"))
        self.assertEqual(wide, a)

class ScoringTests(unittest.TestCase):
    def test_exactly_252_combinations(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        scores, best, best_ids = score_all_teams(pool, snap)
        self.assertEqual(len(scores), TOTAL_COMBOS)
        self.assertEqual(len(list(itertools.combinations(pool, 5))), 252)
        self.assertTrue(best_ids)
        self.assertGreaterEqual(best, min(scores) - 1e-12)

    def test_order_invariance(self) -> None:
        snap = mini_snapshot()
        a = ["1", "2", "3", "4", "5"]
        b = ["5", "3", "1", "4", "2"]
        self.assertEqual(canonical_ids(b), canonical_ids(a))
        self.assertAlmostEqual(score_team(a, snap), score_team(b, snap), places=12)

    def test_asymmetric_pair_lift_bidirectional(self) -> None:
        """One-sided top-pairs rows still contribute (JS pairLiftBetween parity)."""
        snap = mini_snapshot()
        # Only champ 10 lists a lift toward 1; reverse row is absent.
        snap["champs"]["1"]["pairs"] = []
        snap["champs"]["10"]["pairs"] = [{"id": 1, "lift": 0.04, "g": 80}]
        for cid in ("2", "3", "4", "5"):
            snap["champs"][cid]["pairs"] = []
        team = ["1", "2", "3", "4", "10"]
        # Mean WR of {1,2,3,4,10} = (0.46+0.47+0.48+0.49+0.55)/5 = 0.49
        # One known pair lift 0.04 among C(5,2)=10 pairs → mean lift 0.004
        # (missing edges count as 0; do NOT average only known edges).
        score = score_team(team, snap)
        score_rev = score_team(list(reversed(team)), snap)
        self.assertAlmostEqual(score, score_rev, places=12)
        snap_no = mini_snapshot()
        for cid in ("1", "2", "3", "4", "5", "10"):
            snap_no["champs"][cid]["pairs"] = []
        base = score_team(team, snap_no)
        # Same roster → same composition term; delta must be exactly 0.04/10.
        self.assertAlmostEqual(score - base, 0.04 / 10.0, places=12)
        self.assertGreater(score, base + 1e-9)

    def test_logit_v2_calibrates_pair_and_composition_signals(self) -> None:
        snap = mini_snapshot()
        team = ["1", "2", "3", "4", "10"]
        for cid in ("1", "2", "3", "4", "10"):
            snap["champs"][cid]["pairs"] = []
        snap["champs"]["10"]["pairs"] = [{"id": 1, "lift": 0.04, "g": 80}]
        # One frontline champion activates the 1-front|45-55% AD table cell.
        snap["champs"]["1"]["comp"]["front"] = 2.1
        snap["team_score"] = {
            "kind": "logit_v2",
            "score_version": "unit-v2",
            "pair_prior_games": 100,
            "pair_logit_weight": 4.0,
            "composition_logit_weight": 1.0,
        }

        base = 0.49
        pair_signal = (0.04 * 80 / 180) / 10
        composition_signal = 0.55 * 0.01
        expected = 1 / (
            1
            + math.exp(
                -(
                    math.log(base / (1 - base))
                    + 4.0 * pair_signal
                    + composition_signal
                )
            )
        )
        self.assertAlmostEqual(score_team(team, snap), expected, places=12)

    def test_rank_recomputation_and_est_wr_clamp(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        # Best-ish pick: highest wr champs
        best_pick = ["6", "7", "8", "9", "10"]
        worst_pick = ["1", "2", "3", "4", "5"]
        good = score_round(pool, best_pick, snap)
        bad = score_round(pool, worst_pick, snap)
        self.assertEqual(good.total, 252)
        self.assertLessEqual(good.rank, bad.rank)
        self.assertGreaterEqual(good.user_score, 0.35)
        self.assertLessEqual(good.user_score, 0.65)
        # rank_among: better scores push rank down
        self.assertEqual(rank_among(1.0, [0.5, 0.6, 1.0]), 1)
        self.assertEqual(rank_among(0.5, [0.9, 0.8, 0.5]), 3)

    def test_client_score_fields_ignored(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        picks = ["6", "7", "8", "9", "10"]
        payload = {
            "nickname": "Tester",
            "patch": "16.10",
            "avg_rank": 1.0,  # client lie
            "rounds": [
                {
                    "pool_ids": pool,
                    "picked_ids": picks,
                    "rank": 1,  # client lie
                    "score": 0.99,
                }
                for _ in range(5)
            ],
        }
        run = recompute_run(payload, snap)
        self.assertEqual(len(run["ranks"]), 5)
        # ranks come from server recompute, not the client's 1
        self.assertTrue(all(isinstance(r, int) and 1 <= r <= 252 for r in run["ranks"]))
        self.assertAlmostEqual(run["avg_rank"], sum(run["ranks"]) / 5)


class ValidationTests(unittest.TestCase):
    def test_stale_patch_409(self) -> None:
        snap = mini_snapshot(patch="16.10")
        payload = {
            "nickname": "okname",
            "patch": "16.9",
            "rounds": five_rounds([str(i) for i in range(1, 11)], ["1", "2", "3", "4", "5"]),
        }
        with self.assertRaises(PatchMismatch) as ctx:
            recompute_run(payload, snap)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_malformed_replay(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        # picked not subset
        bad = {
            "nickname": "okname",
            "patch": "16.10",
            "rounds": [
                {"pool_ids": pool, "picked_ids": ["1", "2", "3", "4", "99"]}
                for _ in range(5)
            ],
        }
        with self.assertRaises(MetaPickError):
            recompute_run(bad, snap)
        # wrong size
        bad2 = {
            "nickname": "okname",
            "patch": "16.10",
            "rounds": [{"pool_ids": pool[:9], "picked_ids": ["1", "2", "3", "4", "5"]}]
            * 5,
        }
        with self.assertRaises(MetaPickError):
            recompute_run(bad2, snap)
        # duplicate pool id
        dup_pool = pool[:9] + ["1"]
        bad3 = {
            "nickname": "okname",
            "patch": "16.10",
            "rounds": five_rounds(dup_pool, ["1", "2", "3", "4", "5"]),
        }
        with self.assertRaises(MetaPickError):
            recompute_run(bad3, snap)
        # unknown champion
        pool_u = [str(i) for i in range(1, 10)] + ["999"]
        bad4 = {
            "nickname": "okname",
            "patch": "16.10",
            "rounds": five_rounds(pool_u, ["1", "2", "3", "4", "5"]),
        }
        with self.assertRaises(MetaPickError):
            recompute_run(bad4, snap)


class DbLeaderboardTests(unittest.TestCase):
    def test_stale_rows_rescore_once_per_score_version(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            submit_run(
                db,
                {
                    "nickname": "Versioned",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
                },
                snap,
            )
            snap_v2 = mini_snapshot()
            snap_v2["team_score"] = {
                "kind": "logit_v2",
                "score_version": "unit-v2",
                "pair_prior_games": 100,
                "pair_logit_weight": 4.0,
                "composition_logit_weight": 1.0,
            }
            first = rescore_stale_runs(db, snap_v2)
            second = rescore_stale_runs(db, snap_v2)
            self.assertEqual(first["updated"], 1)
            self.assertEqual(second["updated"], 0)
            con = db_connect(db)
            try:
                version = con.execute(
                    "SELECT score_version FROM meta_pick_runs"
                ).fetchone()[0]
            finally:
                con.close()
            self.assertEqual(version, "unit-v2")

    def test_same_nickname_multiple_distinct_runs(self) -> None:
        """Same nick can hold many board rows; each distinct 5-round replay inserts."""
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        good_picks = ["6", "7", "8", "9", "10"]
        bad_picks = ["1", "2", "3", "4", "5"]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            good = recompute_run(
                {
                    "nickname": "Ace",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, good_picks),
                },
                snap,
            )
            bad = recompute_run(
                {
                    "nickname": "Ace",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, bad_picks),
                },
                snap,
            )
            self.assertNotEqual(good["run_fp"], bad["run_fp"])
            with mock.patch(
                "aram_nn.site.meta_pick.utc_now_iso",
                return_value="2026-07-13T12:00:00.000001Z",
            ) as mock_now:
                first = upsert_best_run(db, good)
                mock_now.assert_called_once()
            self.assertTrue(first.updated)
            self.assertEqual(first.entry["created_at"], "2026-07-13T12:00:00.000001Z")
            self.assertNotIn("nickname_key", first.entry)
            self.assertIsNone(first.retained)

            held: list = []

            def tracking_connect(path):
                con = db_connect(path)
                held.append(con)
                return con

            def now_under_tx() -> str:
                self.assertTrue(held, "utc_now_iso before connect")
                self.assertTrue(
                    held[-1].in_transaction,
                    "utc_now_iso must run under BEGIN IMMEDIATE",
                )
                return "2026-07-13T12:00:00.999999Z"

            with mock.patch(
                "aram_nn.site.meta_pick.connect", side_effect=tracking_connect
            ), mock.patch(
                "aram_nn.site.meta_pick.utc_now_iso", side_effect=now_under_tx
            ) as mock_now:
                second = upsert_best_run(db, bad)
                mock_now.assert_called_once()
            self.assertTrue(second.updated)
            self.assertEqual(second.entry["nickname"], "Ace")
            self.assertEqual(second.entry["created_at"], "2026-07-13T12:00:00.999999Z")
            self.assertNotEqual(first.entry["id"], second.entry["id"])
            self.assertAlmostEqual(second.entry["avg_rank"], bad["avg_rank"])

            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["total"], 2)
            nicks = {e["nickname"] for e in board["entries"]}
            self.assertEqual(nicks, {"Ace"})
            # Better avg_rank sorts first.
            self.assertLessEqual(
                board["entries"][0]["avg_rank"], board["entries"][1]["avg_rank"]
            )

            # Identical replay (same run_fp) under same nick → 409, no third row.
            with self.assertRaises(MetaPickError) as ctx:
                upsert_best_run(db, good)
            self.assertEqual(ctx.exception.status_code, 409)
            board2 = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board2["total"], 2)

    def test_legacy_flag_payload_ignored(self) -> None:
        """Clients may still send flag; server ignores it and omits from entry."""
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            out = submit_run(
                db,
                {
                    "nickname": "路燈",
                    "flag": "tw",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
                },
                snap,
            )
            self.assertNotIn("flag", out["entry"])
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertNotIn("flag", board["entries"][0])
            # Invalid legacy flag must not reject the run.
            out2 = submit_run(
                db,
                {
                    "nickname": "路燈2",
                    "flag": "ZZ",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                },
                snap,
            )
            self.assertTrue(out2["updated"])
            self.assertNotIn("flag", out2["entry"])

    def test_main_id_stored_on_entry(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            out = submit_run(
                db,
                {
                    "nickname": "主角哥",
                    "main_id": "7",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
                },
                snap,
            )
            self.assertEqual(out["entry"]["main_id"], "7")
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["entries"][0]["main_id"], "7")
            # Empty main is fine.
            out2 = submit_run(
                db,
                {
                    "nickname": "無頭像",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                },
                snap,
            )
            self.assertEqual(out2["entry"]["main_id"], "")
            # Unknown champion rejected.
            with self.assertRaises(MetaPickError):
                submit_run(
                    db,
                    {
                        "nickname": "壞ID",
                        "main_id": "99999",
                        "patch": "16.10",
                        "rounds": five_rounds(pool, ["2", "3", "4", "5", "6"]),
                    },
                    snap,
                )

    def test_same_run_different_nickname_rejected(self) -> None:
        """Changing nickname must not re-upload the same 5-round replay."""
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        picks = ["6", "7", "8", "9", "10"]
        rounds = five_rounds(pool, picks)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            first = submit_run(
                db,
                {"nickname": "我的鍋", "patch": "16.10", "rounds": rounds},
                snap,
            )
            self.assertTrue(first["updated"])
            with self.assertRaises(MetaPickError) as ctx:
                submit_run(
                    db,
                    {"nickname": "test", "patch": "16.10", "rounds": rounds},
                    snap,
                )
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("already submitted", ctx.exception.detail.lower())
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["total"], 1)
            self.assertEqual(board["entries"][0]["nickname"], "我的鍋")

    def test_legacy_nick_unique_migrates_to_multi_entry(self) -> None:
        """DBs with UNIQUE(nickname_key, patch) drop that constraint on open."""
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            con = db_connect(db)
            try:
                con.execute(
                    """
                    CREATE TABLE meta_pick_runs (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        nickname        TEXT NOT NULL,
                        nickname_key    TEXT NOT NULL,
                        patch           TEXT NOT NULL,
                        avg_rank        REAL NOT NULL,
                        ranks_json      TEXT NOT NULL,
                        rounds_json     TEXT NOT NULL,
                        total_combos    INTEGER NOT NULL DEFAULT 252,
                        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                        UNIQUE(nickname_key, patch)
                    )
                    """
                )
                con.execute(
                    """
                    INSERT INTO meta_pick_runs (
                        nickname, nickname_key, patch, avg_rank, ranks_json,
                        rounds_json, total_combos, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Ace",
                        nickname_key("Ace"),
                        "16.10",
                        10.0,
                        "[10,10,10,10,10]",
                        "[]",
                        252,
                        "2026-01-01T00:00:00.000001Z",
                    ),
                )
                con.commit()
            finally:
                con.close()

            submit_run(
                db,
                {
                    "nickname": "Ace",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                },
                snap,
            )
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["total"], 2)
            self.assertEqual({e["nickname"] for e in board["entries"]}, {"Ace"})

    def test_sort_avg_rank_then_created_at_desc(self) -> None:
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            # Two different nicknames, different quality
            submit_run(
                db,
                {
                    "nickname": "Better",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
                },
                snap,
            )
            submit_run(
                db,
                {
                    "nickname": "Worse",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                },
                snap,
            )
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["total"], 2)
            self.assertEqual(board["entries"][0]["nickname"], "Better")
            self.assertLessEqual(
                board["entries"][0]["avg_rank"], board["entries"][1]["avg_rank"]
            )
            for entry in board["entries"]:
                self.assertNotIn("nickname_key", entry)

    def test_equal_avg_newer_first_tie_break(self) -> None:
        """Equal avg_rank → newer created_at first; then id DESC."""
        snap = mini_snapshot()
        pool = [str(i) for i in range(1, 11)]
        # Different replays (distinct run_fp) forced to the same avg for sort-order test.
        older = recompute_run(
            {
                "nickname": "OldTimer",
                "patch": "16.10",
                "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
            },
            snap,
        )
        newer = recompute_run(
            {
                "nickname": "NewTimer",
                "patch": "16.10",
                "rounds": five_rounds(pool, ["5", "6", "7", "8", "9"]),
            },
            snap,
        )
        newer["avg_rank"] = older["avg_rank"]
        newer["ranks"] = list(older["ranks"])
        self.assertNotEqual(older["run_fp"], newer["run_fp"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "site.db"
            with mock.patch(
                "aram_nn.site.meta_pick.utc_now_iso",
                return_value="2026-01-01T00:00:00.000001Z",
            ):
                upsert_best_run(db, older)
            with mock.patch(
                "aram_nn.site.meta_pick.utc_now_iso",
                return_value="2026-01-01T00:00:00.000002Z",
            ):
                upsert_best_run(db, newer)
            board = list_leaderboard(db, patch="16.10", limit=10)
            self.assertEqual(board["total"], 2)
            self.assertEqual(
                board["entries"][0]["avg_rank"], board["entries"][1]["avg_rank"]
            )
            self.assertEqual(board["entries"][0]["nickname"], "NewTimer")
            self.assertEqual(board["entries"][1]["nickname"], "OldTimer")
            self.assertGreater(
                board["entries"][0]["created_at"], board["entries"][1]["created_at"]
            )


class SnapshotTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_snapshot_cache()

    def test_missing_snapshot_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with self.assertRaises(SnapshotUnavailable) as ctx:
                load_snapshot(path)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_snapshot_requires_patch_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            path.write_text(json.dumps({"champs": {"1": _champ(0.5)}}), encoding="utf-8")
            with self.assertRaises(SnapshotUnavailable):
                load_snapshot(path)

    def test_snapshot_cache_hit_and_reload_on_change(self) -> None:
        clear_snapshot_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            path.write_text(json.dumps(mini_snapshot(patch="16.10")), encoding="utf-8")
            first = load_snapshot(path)
            second = load_snapshot(path)
            self.assertIs(first, second)
            self.assertEqual(first["patch_prefix"], "16.10")

            # Change identity via content size (and patch) so cache must reload
            # without relying on flaky mtime sleeps.
            replacement = mini_snapshot(patch="16.11")
            replacement["cache_bust"] = "x" * 64
            path.write_text(json.dumps(replacement), encoding="utf-8")
            third = load_snapshot(path)
            self.assertIsNot(third, first)
            self.assertEqual(third["patch_prefix"], "16.11")
            fourth = load_snapshot(path)
            self.assertIs(third, fourth)

    def test_utc_now_iso_has_microseconds(self) -> None:
        ts = utc_now_iso()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class ApiTests(unittest.TestCase):
    def test_post_and_get_and_cors(self) -> None:
        snap = mini_snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snap_path = tmp_path / "tier-list.json"
            snap_path.write_text(json.dumps(snap), encoding="utf-8")
            db_path = tmp_path / "site.db"
            env = {
                "ARAM_SITE_DB": str(db_path),
                "ARAM_META_PICK_SNAPSHOT": str(snap_path),
                "ARAM_SITE_CORS_ORIGINS": "https://arammeta.com,http://localhost:5500",
                "ARAM_META_PICK_RATE_LIMIT_PER_HOUR": "0",
                "ARAM_SITE_ADMIN_TOKEN": "",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                # Re-import api module so middleware / limiter pick up env.
                import importlib

                import aram_nn.site.api as api_mod

                importlib.reload(api_mod)
                from fastapi.testclient import TestClient

                client = TestClient(api_mod.app)
                pool = [str(i) for i in range(1, 11)]
                body = {
                    "nickname": "Boarder",
                    "main_id": "1",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["6", "7", "8", "9", "10"]),
                    "avg_rank": 1.0,
                }
                res = client.post("/api/meta-pick/runs", json=body)
                self.assertEqual(res.status_code, 200, res.text)
                data = res.json()
                self.assertTrue(data["ok"])
                self.assertIn("avg_rank", data)
                self.assertEqual(len(data["ranks"]), 5)
                self.assertTrue(data["updated"])
                self.assertNotIn("flag", data["entry"])
                self.assertEqual(data["entry"]["main_id"], "1")

                stale = client.post(
                    "/api/meta-pick/runs",
                    json={**body, "patch": "99.9"},
                )
                self.assertEqual(stale.status_code, 409)

                board = client.get("/api/meta-pick/leaderboard")
                self.assertEqual(board.status_code, 200)
                b = board.json()
                self.assertEqual(b["patch"], "16.10")
                self.assertGreaterEqual(b["total"], 1)
                self.assertEqual(b["entries"][0]["nickname"], "Boarder")
                self.assertNotIn("nickname_key", b["entries"][0])
                self.assertNotIn("nickname_key", data["entry"])
                self.assertRegex(
                    data["entry"]["created_at"],
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
                )

                # CORS preflight / response headers when origin allowed
                opt = client.options(
                    "/api/meta-pick/leaderboard",
                    headers={
                        "Origin": "https://arammeta.com",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                # Starlette may return 200 on OPTIONS with CORS middleware
                self.assertIn(opt.status_code, (200, 204))
                get_cors = client.get(
                    "/api/meta-pick/leaderboard",
                    headers={"Origin": "https://arammeta.com"},
                )
                self.assertEqual(
                    get_cors.headers.get("access-control-allow-origin"),
                    "https://arammeta.com",
                )

                # Second distinct run under same nick inserts a second row.
                worse_body = {
                    "nickname": "Boarder",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                }
                worse_res = client.post("/api/meta-pick/runs", json=worse_body)
                self.assertEqual(worse_res.status_code, 200, worse_res.text)
                worse_data = worse_res.json()
                self.assertTrue(worse_data["updated"])
                self.assertNotIn("nickname_key", worse_data["entry"])
                self.assertNotEqual(worse_data["entry"]["id"], data["entry"]["id"])
                self.assertGreaterEqual(
                    worse_data["avg_rank"], data["avg_rank"]
                )
                board2 = client.get("/api/meta-pick/leaderboard")
                self.assertEqual(board2.status_code, 200)
                self.assertEqual(board2.json()["total"], 2)


class RenderContractTests(unittest.TestCase):
    def test_fifth_round_reveal_does_not_auto_settle(self) -> None:
        """Fifth reveal stays visible; settled only via #game-show-settle click."""
        root = Path(__file__).resolve().parents[1]
        src = (root / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        m = re.search(
            r"function metaPickRecordRoundIfNeeded\(\)\s*\{",
            src,
        )
        self.assertIsNotNone(m, "metaPickRecordRoundIfNeeded missing")
        assert m is not None
        start = m.end()
        # Brace-match the function body.
        depth = 1
        i = start
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        body = src[start : i - 1]
        # Comments may mention settled; only assignment would auto-hide the fifth reveal.
        self.assertIsNone(
            re.search(r"settled\s*=\s*true", body),
            "metaPickRecordRoundIfNeeded must not set settled=true (fifth reveal UX)",
        )
        self.assertIn("game-show-settle", src)
        # Button handler is the intentional settle path.
        self.assertRegex(
            src,
            r"game-show-settle[\s\S]{0,200}?metaPickSession\.settled\s*=\s*true",
        )
        # Fallback guard may still set settled when advancing past 5 rounds.
        self.assertIn("function metaPickNextRound()", src)

    def test_worse_submit_copy_uses_retained_best_avg(self) -> None:
        """updated=false must display entry/retained avg, not rejected run avg."""
        root = Path(__file__).resolve().parents[1]
        src = (root / "scripts" / "templates" / "site.js").read_text(encoding="utf-8")
        # Locate submit success branch.
        self.assertIn("data.updated === false", src)
        self.assertIn("data.entry.avg_rank", src)
        self.assertIn("data.retained.avg_rank", src)
        # When keeping best, must not use bare data.avg_rank for the kept message.
        m = re.search(
            r"if\s*\(\s*data\s*&&\s*data\.updated\s*===\s*false\s*\)\s*\{([\s\S]*?)\}"
            r"\s*else\s*\{",
            src,
        )
        self.assertIsNotNone(m, "updated===false branch not found")
        assert m is not None
        kept_branch = m.group(1)
        self.assertIn("entry.avg_rank", kept_branch)
        # Rejected-run avg must not be the primary kept display source.
        self.assertNotRegex(
            kept_branch,
            r"data\.avg_rank\s*!=\s*null",
        )

    def test_payload_includes_patch_prefix_and_api_base_injection(self) -> None:
        import sys

        root = Path(__file__).resolve().parents[1]
        scripts = root / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from tierlist_render import render_html

        # Minimal render: empty records still builds shell with JS inject.
        html = render_html(
            records=[],
            champ_meta={},
            champ_profiles={},
            champ_picks={},
            champ_sets={},
            champ_item_builds={},
            champ_single_items={},
            champ_boot_items={},
            champ_spell_items={},
            champ_item_clusters={},
            champ_augment_types={},
            champ_synergy={},
            aug_meta={},
            patch_changes={},
            queue_id=2400,
            patch_prefix="16.10",
            ddragon_version="15.1.1",
            total_games=0,
            min_games_per_pair=15,
            min_synergy_games=40,
            site_url="",
            payload_url="",
            meta_pick_api_url="https://api.example.com",
        )
        self.assertIn('"https://api.example.com"', html)
        self.assertNotIn("__META_PICK_API_BASE__", html)
        # Inline payload path embeds DATA; patch_prefix on payload object.
        self.assertIn('"patch_prefix": "16.10"', html.replace("'", '"') if False else html)
        # json.dumps uses double quotes
        self.assertIn('"patch_prefix": "16.10"', html)
        self.assertIn("class='classic-mode-link'", html)
        self.assertIn("href='/classic.html'", html)
        self.assertIn("data-href-zh-cn='/zh-CN/classic.html'", html)
        self.assertIn("data-href-en='/en/classic.html'", html)
        self.assertIn("data-i18n-en='Classic Mode'", html)

        html_empty = render_html(
            records=[],
            champ_meta={},
            champ_profiles={},
            champ_picks={},
            champ_sets={},
            champ_item_builds={},
            champ_single_items={},
            champ_boot_items={},
            champ_spell_items={},
            champ_item_clusters={},
            champ_augment_types={},
            champ_synergy={},
            aug_meta={},
            patch_changes={},
            queue_id=2400,
            patch_prefix="16.10",
            ddragon_version="15.1.1",
            total_games=0,
            min_games_per_pair=15,
            min_synergy_games=40,
            meta_pick_api_url="",
        )
        # Empty base injects empty JSON string
        self.assertIn('const META_PICK_API_BASE = "";', html_empty)

    def test_production_build_defaults_to_public_meta_pick_api(self) -> None:
        import sys

        root = Path(__file__).resolve().parents[1]
        scripts = root / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from build_tier_list import (
            PRODUCTION_META_PICK_API_URL,
            resolve_meta_pick_api_url,
        )

        self.assertEqual(
            resolve_meta_pick_api_url("https://arammeta.com/", ""),
            PRODUCTION_META_PICK_API_URL,
        )
        self.assertEqual(
            resolve_meta_pick_api_url(
                "https://arammeta.com", "https://api.arammeta.com/"
            ),
            PRODUCTION_META_PICK_API_URL,
        )

    def test_production_build_rejects_another_meta_pick_api(self) -> None:
        import sys

        import click

        root = Path(__file__).resolve().parents[1]
        scripts = root / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from build_tier_list import resolve_meta_pick_api_url

        with self.assertRaises(click.ClickException):
            resolve_meta_pick_api_url(
                "https://arammeta.com/", "https://wrong.example.com"
            )

        self.assertEqual(resolve_meta_pick_api_url("", ""), "")


if __name__ == "__main__":
    unittest.main()
