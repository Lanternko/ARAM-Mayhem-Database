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


def test_failed_command_retains_exit_code_and_both_output_streams():
    from aram_nn.site.static_publish import _run_checked
    with pytest.raises(RuntimeError) as caught:
        _run_checked(lambda cmd: CommandResult(137, "last completed patch", "allocation failed"), ["python", "export.py"])
    message = str(caught.value)
    assert "exit=137" in message
    assert "last completed patch" in message
    assert "allocation failed" in message


def _export_writing_runner(env, launched, *, fail_on: str | None = None):
    """Fake pipeline: the export step writes the parquet, one step may fail."""
    def runner(cmd):
        launched.append(cmd[1])
        if "export_pooled_parquet" in cmd[1]:
            env["parquet"].write_bytes(b"parquet-%d" % len(launched))
        if fail_on and fail_on in cmd[1]:
            return CommandResult(1, "", "resource pressure")
        return CommandResult(0, "", "")
    return runner


def test_retry_after_training_abort_reuses_the_finished_export(env):
    env["score_csv"].write_text("champion_id,x\n", encoding="utf-8")
    first: list[str] = []
    with pytest.raises(RuntimeError):
        _run(env, _export_writing_runner(env, first, fail_on="train_composition"))
    assert [Path(c).stem for c in first] == ["export_pooled_parquet", "train_composition_lr_pooled"]
    exported = env["parquet"].stat()

    retry: list[str] = []
    result = _run(env, _export_writing_runner(env, retry))

    assert result["refreshed"] is True and result["reused_export"] is True
    assert [Path(c).stem for c in retry] == [
        "train_composition_lr_pooled", "build_pooled_champ_lr",
        "build_single_team_calibration", "build_role_synergy"]
    # Untouched file: the publisher's stage memo stays valid across retries.
    assert env["parquet"].stat().st_mtime_ns == exported.st_mtime_ns
    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["last_refreshed_total"] == 40_000
    assert "exported_parquet" not in state  # next growth-triggered refresh exports afresh


@pytest.mark.parametrize("spoil", ["rewritten", "stale", "other_pool", "deleted"])
def test_export_is_not_reused_when_it_is_not_the_recorded_one(env, spoil):
    env["score_csv"].write_text("champion_id,x\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _run(env, _export_writing_runner(env, [], fail_on="train_composition"))
    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert state["exported_parquet"]["pool"] == ["16.16"]

    if spoil == "rewritten":
        env["parquet"].write_bytes(b"someone else wrote this file")
    elif spoil == "deleted":
        env["parquet"].unlink()
    elif spoil == "stale":
        state["exported_parquet"]["exported_at_unix"] -= mr.EXPORT_REUSE_MAX_AGE_SEC + 60
    elif spoil == "other_pool":
        state["exported_parquet"]["pool"] = ["16.15", "16.16"]
    env["state"].write_text(json.dumps(state), encoding="utf-8")

    retry: list[str] = []
    result = _run(env, _export_writing_runner(env, retry))

    assert result["reused_export"] is False
    assert Path(retry[0]).stem == "export_pooled_parquet" and len(retry) == 5


def test_successful_refresh_does_not_leave_an_export_to_reuse(env):
    env["score_csv"].write_text("champion_id,x\n", encoding="utf-8")
    _run(env, _export_writing_runner(env, []))
    again: list[str] = []
    result = _run(env, _export_writing_runner(env, again), force=True)
    assert result["reused_export"] is False and len(again) == 5
