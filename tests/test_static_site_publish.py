from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from aram_nn.site.db import insert_public_games
from aram_nn.site.static_publish import (
    CommandResult,
    decide_static_publish,
    publish_static_site_once,
)


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
                "docs/api/champ-archetype-fit.json",
                "docs/api/champ-empirical-axes.json",
                "docs/assets/icons",
                "docs/assets/covers",
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
