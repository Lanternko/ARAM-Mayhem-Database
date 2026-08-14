from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from aram_nn.site.db import insert_public_games
from aram_nn.site.static_publish import (
    CommandResult,
    DEFAULT_DOC_PATHS,
    DEFAULT_PLAYER_HISTORY_API_URL,
    decide_static_publish,
    publish_static_site_once,
    push_with_upstream_merge,
)
from aram_nn.site.static_publish_cli import acquire_publisher_lock


def game_row(game_id: str, *, created_ms: int = 1) -> dict:
    return {
        "game_id": game_id,
        "queue_id": 2400,
        "patch": "16.11.1",
        "blue_champs": [1, 2, 3, 4, 5],
        "red_champs": [6, 7, 8, 9, 10],
        "blue_wins": True,
        "duration_sec": 900,
        "created_ms": created_ms,
        "captured_at": "2026-05-23T00:00:00Z",
        "participants_json": [
            {"teamId": 100, "championId": 1, "augments": [1001, 1002]},
            {"teamId": 200, "championId": 6, "augments": [1003, 1004]},
        ],
    }


class StaticSitePublishTests(unittest.TestCase):
    def test_publisher_lock_rejects_concurrent_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            first = acquire_publisher_lock(state)
            try:
                with self.assertRaisesRegex(RuntimeError, "another static publisher"):
                    acquire_publisher_lock(state)
            finally:
                first.close()

    def test_publish_decision_waits_for_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row(f"TW_{idx}") for idx in range(5)])

            wait = decide_static_publish(
                db=db,
                state={"last_published_total": 0},
                threshold=10,
                force=False,
            )
            publish = decide_static_publish(
                db=db,
                state={"last_published_total": 0},
                threshold=5,
                force=False,
            )

            self.assertFalse(wait.should_publish)
            self.assertTrue(publish.should_publish)

    def test_publish_decision_uses_max_age_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row(f"TW_{idx}") for idx in range(6)])
            last_publish = 1_000_000
            state = {
                "last_published_total": 5,
                "last_publish_at_unix": last_publish,
            }

            fresh = decide_static_publish(
                db=db,
                state=state,
                threshold=10,
                growth_ratio=0.10,
                max_age_hours=12,
                force=False,
                now_unix=last_publish + 11.5 * 3600,
            )
            stale = decide_static_publish(
                db=db,
                state=state,
                threshold=10,
                growth_ratio=0.10,
                max_age_hours=12,
                force=False,
                now_unix=last_publish + 12 * 3600,
            )

            self.assertFalse(fresh.should_publish)
            self.assertIn("age 11.5h < max 12h", fresh.reason)
            self.assertTrue(stale.should_publish)
            self.assertIn("age 12.0h >= max 12h", stale.reason)

    def test_dry_run_prepares_only_static_site_outputs(self) -> None:
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            commands.append(cmd)
            if cmd[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return CommandResult(0, "main\n")
            if cmd[:3] == ["git", "status", "--short"]:
                return CommandResult(0, "")
            if cmd[:3] == ["git", "diff", "--quiet"]:
                return CommandResult(1, "")
            return CommandResult(0, "")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row(f"TW_{idx}") for idx in range(3)])

            result = publish_static_site_once(
                db=db,
                state_path=Path(tmp) / "state.json",
                threshold=3,
                dry_run=True,
                # Explicit pool -> resolution does not depend on the repo's parquet;
                # the mocked runner means the build is never actually executed.
                comp_fit_parquet=Path(tmp) / "pool.parquet",
                runner=runner,
            )

        self.assertEqual(result["reason"], "dry run")
        self.assertTrue(result["would_publish"])
        # The comp-fit radar and empirical-axes JSONs are required, separately-fetched
        # site artifacts: each must be in the staged set or the live site 404s -- the
        # radar falls back to the heuristic estimate, the 後期/滾雪球 bars read 0.
        self.assertEqual(
            result["changed_paths"],
            [
                "docs/index.html",
                "docs/api/tier-list.json",
                "docs/api/champions",
                "docs/api/champ-archetype-fit.json",
                "docs/api/champ-empirical-axes.json",
                "docs/assets/icons",
                "docs/assets/covers",
                # External app script: the shell references it by content-hash
                # ?v=, so it must ship in the same publish as index.html.
                "docs/assets/site.js",
                "docs/classic.html",
                # Clean-path deep-link shells, locale mirrors, share thumbnail and
                # the static info pages: each embeds the current game count / patch
                # / cache-bust, so they change with index.html and drift on the live
                # site whenever a publish leaves them behind.
                "docs/404.html",
                "docs/augments",
                "docs/changes",
                "docs/draft",
                "docs/game",
                "docs/en",
                "docs/zh-CN",
                "docs/p/player-history",
                "docs/og-image.png",
                "docs/about",
                "docs/privacy",
                "docs/contact",
                "docs/champion-roles.json",
            ],
        )
        self.assertEqual(result["comp_fit"]["built"], True)
        self.assertEqual(result["empirical_axes"]["built"], True)
        self.assertFalse(any(cmd[:2] == ["git", "add"] for cmd in commands))
        self.assertFalse(any(cmd[:2] == ["git", "commit"] for cmd in commands))

        def first_index(needle: str) -> int:
            return next(
                i for i, cmd in enumerate(commands)
                if any(needle in str(part) for part in cmd)
            )

        # The comp-fit and empirical-axes builds both read the rebuilt tier-list.json,
        # so each must run AFTER build_tier_list.py within the same publish.
        self.assertLess(
            first_index("scripts/build_tier_list.py"),
            first_index("scripts/build_champ_archetype_fit.py"),
        )
        self.assertLess(
            first_index("scripts/build_tier_list.py"),
            first_index("scripts/build_champ_empirical_axes.py"),
        )

    def test_player_history_allowlist_is_exact_and_build_passes_api_only_for_hidden_shell(self) -> None:
        self.assertIn(Path("docs/p/player-history"), DEFAULT_DOC_PATHS)
        self.assertNotIn(Path("docs/p"), DEFAULT_DOC_PATHS)
        self.assertEqual(DEFAULT_PLAYER_HISTORY_API_URL, "https://api.arammeta.com")

    def test_untracked_output_does_not_block_publish(self) -> None:
        """A brand-new (untracked) site artifact -- e.g. champ-archetype-fit.json on
        its first deploy -- must not trip the dirty guard; the publish regenerates
        and stages it itself.  Only modified TRACKED outputs should block."""

        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            if cmd[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return CommandResult(0, "main\n")
            if cmd[:3] == ["git", "status", "--short"]:
                return CommandResult(0, "?? docs/api/champ-archetype-fit.json\n")
            if cmd[:3] == ["git", "diff", "--quiet"]:
                return CommandResult(1, "")
            return CommandResult(0, "")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row("TW_1")])

            result = publish_static_site_once(
                db=db,
                state_path=Path(tmp) / "state.json",
                threshold=1,
                dry_run=True,
                comp_fit_parquet=Path(tmp) / "pool.parquet",
                runner=runner,
            )

        self.assertTrue(result["would_publish"])
        self.assertEqual(result["reason"], "dry run")

    def test_check_only_does_not_run_build_or_git(self) -> None:
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            commands.append(list(command))
            return CommandResult(0, "")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row(f"TW_{idx}") for idx in range(3)])

            result = publish_static_site_once(
                db=db,
                state_path=Path(tmp) / "state.json",
                threshold=3,
                check_only=True,
                runner=runner,
            )

        self.assertEqual(result["reason"], "check only: delta 3 >= 3")
        self.assertTrue(result["would_publish"])
        self.assertEqual(commands, [])

    def test_merges_upstream_commits_instead_of_wedging_on_push(self) -> None:
        """A commit pushed to main from elsewhere must not stall the publisher:
        fetch, merge it, then push.  Left unmerged this rejected every push for
        17 hours while the crawler kept collecting."""
        commands: list[list[str]] = []
        merged = False

        def runner(command: Sequence[str]) -> CommandResult:
            nonlocal merged
            cmd = list(command)
            commands.append(cmd)
            if cmd[:2] == ["git", "merge"]:
                merged = True
                return CommandResult(0, "")
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CommandResult(0, "0\n" if merged else "6\n")
            if cmd[:2] == ["git", "push"]:
                if not merged:
                    return CommandResult(1, "", "! [rejected] main -> main (non-fast-forward)")
                return CommandResult(0, "")
            return CommandResult(0, "")

        self.assertEqual(push_with_upstream_merge(runner, "main"), "merged 6 upstream commit(s) from origin/main")
        self.assertIn(["git", "fetch", "origin"], commands)
        self.assertIn(["git", "merge", "--no-edit", "origin/main"], commands)
        self.assertEqual(commands[-1], ["git", "push", "origin", "main"])
        # Nothing upstream -> no merge commit manufactured, just a plain push.
        commands.clear()
        self.assertIsNone(push_with_upstream_merge(runner, "main"))
        self.assertFalse(any(cmd[:2] == ["git", "merge"] for cmd in commands))

    def test_detached_worktree_push_targets_main(self) -> None:
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            commands.append(cmd)
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CommandResult(0, "0\n")
            return CommandResult(0, "")

        push_with_upstream_merge(runner, "main", detached=True)
        self.assertEqual(commands[-1], ["git", "push", "origin", "HEAD:main"])

    def test_detached_worktree_refuses_stale_build(self) -> None:
        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CommandResult(0, "1\n")
            return CommandResult(0, "")

        with self.assertRaisesRegex(RuntimeError, "advanced by 1 commit"):
            push_with_upstream_merge(runner, "main", detached=True)

    def test_merge_conflict_surfaces_instead_of_publishing(self) -> None:
        """A genuine conflict is a human's call -- abort and raise so the alert
        fires, rather than pushing a half-merged tree to the live site."""
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            commands.append(cmd)
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return CommandResult(0, "2\n")
            if cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
                return CommandResult(1, "", "CONFLICT (content): docs/index.html")
            return CommandResult(0, "")

        with self.assertRaisesRegex(RuntimeError, "cannot merge 2 upstream commit"):
            push_with_upstream_merge(runner, "main")
        self.assertIn(["git", "merge", "--abort"], commands)
        self.assertFalse(any(cmd[:2] == ["git", "push"] for cmd in commands))

    def test_refuses_to_overwrite_dirty_docs_outputs(self) -> None:
        def runner(command: Sequence[str]) -> CommandResult:
            cmd = list(command)
            if cmd[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return CommandResult(0, "main\n")
            if cmd[:3] == ["git", "status", "--short"]:
                return CommandResult(0, " M docs/index.html\n")
            return CommandResult(0, "")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "games.db"
            insert_public_games(db, [game_row("TW_1")])

            with self.assertRaisesRegex(RuntimeError, "dirty site output"):
                publish_static_site_once(
                    db=db,
                    state_path=Path(tmp) / "state.json",
                    threshold=1,
                    dry_run=True,
                    runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
