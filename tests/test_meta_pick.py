"""Focused tests for Meta Pick 5-round leaderboard MVP."""
from __future__ import annotations

import itertools
import json
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
        # Composition may add a small table term; assert lift is not dropped.
        score = score_team(team, snap)
        score_rev = score_team(list(reversed(team)), snap)
        self.assertAlmostEqual(score, score_rev, places=12)
        # Without any pairs, only base WR (+comp). With the 0.04 lift present,
        # score must exceed the zero-lift version.
        snap_no = mini_snapshot()
        for cid in ("1", "2", "3", "4", "5", "10"):
            snap_no["champs"][cid]["pairs"] = []
        self.assertGreater(score, score_team(team, snap_no) + 1e-9)

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
    def test_best_only_upsert_and_equal_refresh(self) -> None:
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
            with mock.patch(
                "aram_nn.site.meta_pick.utc_now_iso",
                return_value="2026-07-13T12:00:00.000001Z",
            ) as mock_now:
                first = upsert_best_run(db, good)
                mock_now.assert_called_once()
            self.assertTrue(first.updated)
            self.assertEqual(first.entry["created_at"], "2026-07-13T12:00:00.000001Z")
            self.assertNotIn("nickname_key", first.entry)
            # Worse score must not stamp a timestamp (no utc_now_iso call).
            with mock.patch("aram_nn.site.meta_pick.utc_now_iso") as mock_now:
                worse = upsert_best_run(db, bad)
                mock_now.assert_not_called()
            self.assertFalse(worse.updated)
            self.assertAlmostEqual(worse.entry["avg_rank"], good["avg_rank"])
            self.assertIsNotNone(worse.retained)
            assert worse.retained is not None
            self.assertAlmostEqual(worse.retained["avg_rank"], good["avg_rank"])
            self.assertEqual(worse.entry["created_at"], first.entry["created_at"])
            self.assertNotIn("nickname_key", worse.entry)
            # Equal score refreshes timestamp + display without flaky sleeps.
            # utc_now_iso must run under the write transaction (after BEGIN).
            equal = dict(good)
            equal["nickname"] = "ACE"  # display may change; key same
            equal["nickname_key"] = nickname_key("ACE")
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
                refreshed = upsert_best_run(db, equal)
                mock_now.assert_called_once()
            self.assertTrue(refreshed.updated)
            self.assertEqual(refreshed.entry["nickname"], "ACE")
            self.assertEqual(refreshed.entry["created_at"], "2026-07-13T12:00:00.999999Z")
            self.assertGreater(
                refreshed.entry["created_at"], first.entry["created_at"]
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

                # Worse resubmit: updated=false keeps best; public entry has no key.
                worse_body = {
                    "nickname": "Boarder",
                    "patch": "16.10",
                    "rounds": five_rounds(pool, ["1", "2", "3", "4", "5"]),
                }
                worse_res = client.post("/api/meta-pick/runs", json=worse_body)
                self.assertEqual(worse_res.status_code, 200, worse_res.text)
                worse_data = worse_res.json()
                self.assertFalse(worse_data["updated"])
                self.assertNotIn("nickname_key", worse_data["entry"])
                self.assertAlmostEqual(
                    worse_data["entry"]["avg_rank"], data["avg_rank"]
                )
                # Current run avg may be worse than retained entry.
                self.assertGreaterEqual(
                    worse_data["avg_rank"], worse_data["entry"]["avg_rank"]
                )


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


if __name__ == "__main__":
    unittest.main()
