"""Preflight + failure bookkeeping for the recommender model refresh.

Regression cover for the 2026-08-12..26 silent crash-loop: a deleted
`data/cache/champion_semantic_scores.csv` made every tick die inside click's
`exists=True`, with nothing in the out log and no failure marker in the state
file.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aram_nn.site import model_refresh as mr
from aram_nn.site.static_publish import CommandResult


def _make_db(path: Path, *, patch: str = "16.16", games: int = 40_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE games (game_id TEXT PRIMARY KEY, queue_id INTEGER, "
        "patch TEXT, created_ms INTEGER)"
    )
    con.executemany(
        "INSERT INTO games VALUES (?, 2400, ?, ?)",
        [(str(i), f"{patch}.1", 1_700_000_000_000 + i) for i in range(games)],
    )
    con.commit()
    con.close()
    return path


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": _make_db(tmp_path / "games.db"),
        "state": tmp_path / "state.json",
        "out_dir": tmp_path / "models",
        "parquet": tmp_path / "pooled.parquet",
        "score_csv": tmp_path / "scores.csv",
    }


def _run(env: dict[str, Path], runner, **kwargs):
    return mr.refresh_models_once(
        db=env["db"], state_path=env["state"], out_dir=env["out_dir"],
        parquet=env["parquet"], score_csv=env["score_csv"], runner=runner,
        **kwargs,
    )


def test_missing_score_csv_blocks_before_launching_pipeline(env):
    launched: list[list[str]] = []

    def runner(cmd):
        launched.append(list(cmd))
        return CommandResult(0, "", "")

    result = _run(env, runner)

    assert result["blocked"] is True
    assert launched == []  # never even started step 1
    assert "champion_semantic_scores" not in result["reason"]  # tmp path, not default
    assert str(env["score_csv"]) in result["reason"]
    assert "build_semantic_ability_scores.py" in result["reason"]  # actionable

    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["last_result"] == "blocked"
    assert state["consecutive_failures"] == 1
    assert state["last_error_at_unix"] > 0
    # A blocked attempt must not count as a refresh.
    assert "last_refreshed_total" not in state


def test_consecutive_failures_accumulate_then_reset_on_success(env):
    env["score_csv"].write_text("champion_id,x\n", encoding="utf-8")

    def failing(cmd):
        return CommandResult(1, "", "boom")

    for expected in (1, 2, 3):
        with pytest.raises(RuntimeError):
            _run(env, failing)
        state = json.loads(env["state"].read_text(encoding="utf-8"))
        assert state["consecutive_failures"] == expected
        assert state["last_result"] == "failed"
        assert "boom" in state["last_error"]
        assert state["patches"]["16.16"]["consecutive_failures"] == expected

    _run(env, lambda cmd: CommandResult(0, "", ""))

    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["consecutive_failures"] == 0
    assert state["last_result"] == "refreshed"
    assert state["last_error"] is None
    assert state["last_refreshed_total"] == 40_000


def test_gate_still_closes_on_growth_and_warmup_with_inputs_missing(env):
    """Preflight must not change the growth / min-current-games gating."""
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(list(cmd))
        return CommandResult(0, "", "")

    # Warm-up floor: below min_current_games the gate closes on its own terms,
    # and the missing input is reported as a warning rather than a block.
    result = _run(env, runner, min_current_games=100_000)
    assert result["would_refresh"] is False
    assert "warming up" in result["reason"]
    assert result["missing_inputs"]
    assert "blocked" not in result
    assert not env["state"].exists()  # a closed gate records nothing

    # Growth ratio: a state at the current total leaves delta below threshold.
    mr.save_state(
        env["state"],
        {"patches": {"16.16": {"last_refreshed_total": 40_000}}},
    )
    result = _run(env, runner, growth_ratio=0.25)
    assert result["would_refresh"] is False
    assert result["reason"].startswith("delta 0 <")
    assert calls == []


def test_pipeline_passes_the_preflighted_score_csv(env):
    cmds = mr.pipeline_commands(
        db=env["db"], parquet=env["parquet"], out_dir=env["out_dir"],
        pool=["16.14", "16.15", "16.16"], baseline="16.14", prev="16.15",
        current="16.16", half_life_days=7.0, score_csv=env["score_csv"],
    )
    train = next(c for c in cmds if "scripts/train_composition_lr_pooled.py" in c)
    synergy = next(c for c in cmds if "scripts/build_role_synergy.py" in c)
    assert train[train.index("--score-csv") + 1] == str(env["score_csv"])
    assert synergy[synergy.index("--scores-csv") + 1] == str(env["score_csv"])
