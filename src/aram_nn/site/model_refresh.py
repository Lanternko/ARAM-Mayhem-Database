"""Growth-gated, per-patch refresh of the recommender's pooled models.

Mirrors static_publish.py but for the local recommender artifacts instead of
the GitHub Pages tier list: when the current patch has accumulated enough new
games, re-run the full pooled + recency pipeline so the champ-select advisor
tracks the live patch automatically (the same way the tier list auto-publishes).

The pipeline (all reading one freshly-exported pooled parquet):
  1. export_pooled_parquet        games.db -> pooled parquet (latest N patches)
  2. train_composition_lr_pooled  win-prob + swap-delta model.pkl
  3. build_pooled_champ_lr        champion-strength lr_weights.json (GUI z-score)
  4. build_single_team_calibration single_team_calibration.json (win% display)
  5. build_role_synergy           role_synergy.json (same-team chemistry)

Unlike the tier list this commits NOTHING: model files are gitignored and stay
local, so the refresh just regenerates them in place.  The running GUI picks up
new files on its next launch.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect, count_games
from .db import _patch_major_minor  # noqa: PLC2701 - internal reuse within the package
from .static_publish import (
    CommandRunner,
    _default_runner,
    _effective_threshold,
    _last_total_for_scope,
    _record_patch_state,
    _run_checked,
    _state_int,
    load_state,
    save_state,
)

DEFAULT_STATE_PATH = Path("data/site/model_refresh_state.json")
DEFAULT_OUT_DIR = Path("models/composition_lr_pooled_recency_7d")
DEFAULT_PARQUET = Path("data/raw/mayhem_pooled_auto.parquet")
DEFAULT_SCORE_CSV = Path("data/cache/champion_semantic_scores.csv")
DEFAULT_HALF_LIFE_DAYS = 7.0
DEFAULT_POOL_PATCHES = 3
# A full retrain is much heavier than a tier-list rebuild, so the recommender
# re-pools on a larger current-patch growth step than the tier-list's +10%.
DEFAULT_GROWTH_RATIO = 0.25
# Current patch must have at least this many games before the first refresh: the
# verification holdout in train_composition_lr_pooled needs ~12k current-patch
# games, so refreshing a brand-new patch earlier would fail the held-out split.
DEFAULT_MIN_CURRENT_GAMES = 15000

_TOTAL_KEY = "last_refreshed_total"

# Inputs the pipeline reads but never regenerates.  `data/` is gitignored, so a
# deleted cache file is unrecoverable from git and every tick dies instantly
# inside click's `exists=True` -- which is exactly how this refresher
# crash-looped 436 times unnoticed (2026-08-12..26).  Preflight turns that into
# one actionable line instead of a stack trace.
_REGEN_HINTS: dict[str, str] = {
    "db": "collector DB missing; run: python scripts/lcu_collector.py collect --queue 2400",
    "score_csv": (
        "semantic score cache missing; rebuild: "
        "python scripts/fetch_champion_abilities.py && "
        "python scripts/build_semantic_ability_scores.py"
    ),
}


def missing_pipeline_inputs(*, db: Path, score_csv: Path) -> list[str]:
    """Return actionable messages for every required input that is absent."""
    missing: list[str] = []
    for key, path in (("db", Path(db)), ("score_csv", Path(score_csv))):
        if not path.exists():
            missing.append(f"{path}: {_REGEN_HINTS[key]}")
    return missing


@dataclass(frozen=True)
class ModelRefreshDecision:
    should_refresh: bool
    local_total: int
    last_refreshed_total: int
    threshold: int
    growth_ratio: float
    current_patch: str | None
    reason: str


def recent_patch_prefixes(
    db_path: Path, *, queue_id: int = 2400, n: int = 3, min_games: int = 1000
) -> list[str]:
    """Return up to *n* most-recent major.minor prefixes, ascending (oldest first).

    Prefixes with at least *min_games* are preferred; if none qualify, falls
    back to the most-recent prefixes regardless of size.
    """
    if not Path(db_path).exists():
        return []
    con = connect(Path(db_path))
    try:
        rows = con.execute(
            "SELECT patch, COUNT(*) AS n, MAX(created_ms) AS latest "
            "FROM games WHERE queue_id = ? GROUP BY patch ORDER BY latest DESC",
            (queue_id,),
        ).fetchall()
    finally:
        con.close()
    stats: dict[str, tuple[int, int]] = {}
    for patch_full, count, latest_ms in rows:
        prefix = _patch_major_minor(str(patch_full))
        if not prefix:
            continue
        prev_n, prev_latest = stats.get(prefix, (0, 0))
        stats[prefix] = (prev_n + count, max(prev_latest, latest_ms or 0))
    if not stats:
        return []
    qualified = [(latest, pfx) for pfx, (c, latest) in stats.items() if c >= min_games]
    if not qualified:
        qualified = [(latest, pfx) for pfx, (_, latest) in stats.items()]
    qualified.sort(reverse=True)  # newest first
    newest_first = [pfx for _, pfx in qualified[:n]]
    return list(reversed(newest_first))  # ascending: oldest .. newest


def resolve_pool(
    db_path: Path, *, queue_id: int, patches: str, current_patch: str, pool_patches: int
) -> tuple[list[str], str, str, str]:
    """Return (patches, baseline, prev, current) for the training pipeline."""
    if patches and patches != "auto":
        pool = [p.strip() for p in patches.split(",") if p.strip()]
    else:
        pool = recent_patch_prefixes(db_path, queue_id=queue_id, n=pool_patches)
    if not pool:
        raise RuntimeError("could not resolve any patch prefixes from the DB")
    cur = current_patch if current_patch and current_patch != "auto" else pool[-1]
    if cur not in pool:
        pool = sorted(set(pool) | {cur})
    baseline = pool[0]
    prev = pool[-2] if len(pool) >= 2 else pool[0]
    return pool, baseline, prev, cur


def decide_model_refresh(
    *,
    db: Path,
    state: dict[str, Any],
    threshold: int,
    growth_ratio: float,
    force: bool,
    queue_id: int,
    current_patch: str,
    min_current_games: int,
) -> ModelRefreshDecision:
    local_total = count_games(db, queue_id=queue_id, patch_prefix=current_patch)
    last_total, _ = _last_total_for_scope(
        state, patch_prefix=current_patch, total_key=_TOTAL_KEY
    )
    eff = _effective_threshold(
        last_total=last_total, threshold=threshold, growth_ratio=growth_ratio
    )
    if force:
        return ModelRefreshDecision(
            True, local_total, last_total, eff, growth_ratio, current_patch, "force"
        )
    if local_total < min_current_games:
        return ModelRefreshDecision(
            False, local_total, last_total, eff, growth_ratio, current_patch,
            f"current patch {current_patch} warming up ({local_total} < {min_current_games})",
        )
    delta = local_total - last_total
    if delta >= eff:
        reason = f"delta {delta} >= {eff}"
        if growth_ratio > 0 and eff > max(0, int(threshold or 0)):
            reason += f" (growth {growth_ratio:.0%})"
        if last_total == 0:
            reason += f"; first refresh baseline for patch {current_patch}"
        return ModelRefreshDecision(
            True, local_total, last_total, eff, growth_ratio, current_patch, reason
        )
    return ModelRefreshDecision(
        False, local_total, last_total, eff, growth_ratio, current_patch,
        f"delta {delta} < {eff}",
    )


def pipeline_commands(
    *,
    db: Path,
    parquet: Path,
    out_dir: Path,
    pool: list[str],
    baseline: str,
    prev: str,
    current: str,
    half_life_days: float,
    score_csv: Path = DEFAULT_SCORE_CSV,
) -> list[list[str]]:
    py = sys.executable
    patches_csv = ",".join(pool)
    hl = str(half_life_days)
    # --score-csv / --scores-csv are passed explicitly (not left to the scripts'
    # own defaults) so preflight checks the same path the pipeline will open.
    return [
        [py, "scripts/export_pooled_parquet.py",
         "--db", str(db), "--out", str(parquet), "--patches", patches_csv],
        [py, "scripts/train_composition_lr_pooled.py",
         "--data", str(parquet), "--current-patch", current, "--prev-patch", prev,
         "--baseline-patch", baseline, "--half-life-days", hl, "--out", str(out_dir),
         "--score-csv", str(score_csv)],
        [py, "scripts/build_pooled_champ_lr.py",
         "--data", str(parquet), "--current-patch", current,
         "--half-life-days", hl, "--out-dir", str(out_dir)],
        [py, "scripts/build_single_team_calibration.py",
         "--data", str(parquet), "--model-dir", str(out_dir), "--half-life-days", hl],
        [py, "scripts/build_role_synergy.py",
         "--data", str(parquet), "--patches", patches_csv,
         "--out", str(out_dir / "role_synergy.json"),
         "--scores-csv", str(score_csv)],
    ]


def _consecutive_failures(state: dict[str, Any], *, patch_prefix: str | None) -> int:
    patches = state.get("patches")
    if patch_prefix and isinstance(patches, dict):
        patch_state = patches.get(patch_prefix)
        if isinstance(patch_state, dict) and "consecutive_failures" in patch_state:
            return _state_int(patch_state.get("consecutive_failures"))
    return _state_int(state.get("consecutive_failures"))


def _record_attempt_failure(
    state_path: Path,
    state: dict[str, Any],
    *,
    patch_prefix: str | None,
    error: str,
    result: str,
) -> int:
    """Persist a failed attempt so the failure is inspectable in the state file.

    Returns the new consecutive-failure count.  Writing state here never touches
    ``last_refreshed_total``, so the growth gate is unaffected: a failed attempt
    must not count as a refresh.
    """
    streak = _consecutive_failures(state, patch_prefix=patch_prefix) + 1
    _record_patch_state(
        state,
        patch_prefix=patch_prefix,
        payload={
            "last_result": result,
            "last_error": error,
            "last_error_at_unix": time.time(),
            "consecutive_failures": streak,
        },
    )
    try:
        save_state(state_path, state)
    except Exception:  # never let bookkeeping mask the real failure
        pass
    return streak


def refresh_models_once(
    *,
    db: Path = Path("data/lcu/games.db"),
    state_path: Path = DEFAULT_STATE_PATH,
    out_dir: Path = DEFAULT_OUT_DIR,
    parquet: Path = DEFAULT_PARQUET,
    score_csv: Path = DEFAULT_SCORE_CSV,
    threshold: int = 0,
    growth_ratio: float = DEFAULT_GROWTH_RATIO,
    force: bool = False,
    queue_id: int = 2400,
    patches: str = "auto",
    current_patch: str = "auto",
    pool_patches: int = DEFAULT_POOL_PATCHES,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    min_current_games: int = DEFAULT_MIN_CURRENT_GAMES,
    check_only: bool = False,
    dry_run: bool = False,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    """Run one gated refresh, recording any failed attempt into the state file.

    Every failure -- an unresolvable patch pool, a missing input, a pipeline
    command exiting non-zero -- lands in *state_path* before propagating, so a
    crash-looping refresher is inspectable instead of invisible.
    """
    state_path = Path(state_path)
    # Filled in as soon as they are known, so the failure recorder below can
    # attribute a crash to the right patch even if it happened early.
    progress: dict[str, Any] = {"patch": None, "state": None}
    try:
        return _refresh_models_once(
            db=db, state_path=state_path, out_dir=out_dir, parquet=parquet,
            score_csv=score_csv, threshold=threshold, growth_ratio=growth_ratio,
            force=force, queue_id=queue_id, patches=patches,
            current_patch=current_patch, pool_patches=pool_patches,
            half_life_days=half_life_days, min_current_games=min_current_games,
            check_only=check_only, dry_run=dry_run, runner=runner,
            progress=progress,
        )
    except Exception as exc:
        # State used to be written only on success, so a permanently failing
        # pipeline left last_refreshed_total frozen with no failure marker.
        state = progress["state"]
        if not isinstance(state, dict):
            state = load_state(state_path)
        _record_attempt_failure(
            state_path,
            state,
            patch_prefix=progress["patch"],
            error=f"{type(exc).__name__}: {exc}",
            result="failed",
        )
        raise


def _refresh_models_once(
    *,
    db: Path,
    state_path: Path,
    out_dir: Path,
    parquet: Path,
    score_csv: Path,
    threshold: int,
    growth_ratio: float,
    force: bool,
    queue_id: int,
    patches: str,
    current_patch: str,
    pool_patches: int,
    half_life_days: float,
    min_current_games: int,
    check_only: bool,
    dry_run: bool,
    runner: CommandRunner,
    progress: dict[str, Any],
) -> dict[str, Any]:
    pool, baseline, prev, current = resolve_pool(
        db, queue_id=queue_id, patches=patches,
        current_patch=current_patch, pool_patches=pool_patches,
    )
    progress["patch"] = current
    state = load_state(state_path)
    progress["state"] = state
    decision = decide_model_refresh(
        db=db, state=state, threshold=threshold, growth_ratio=growth_ratio,
        force=force, queue_id=queue_id, current_patch=current,
        min_current_games=min_current_games,
    )
    missing = missing_pipeline_inputs(db=db, score_csv=score_csv)
    base_info = {
        "refreshed": False,
        "would_refresh": decision.should_refresh,
        "reason": decision.reason,
        "local_total": decision.local_total,
        "last_refreshed_total": decision.last_refreshed_total,
        "threshold": decision.threshold,
        "growth_ratio": decision.growth_ratio,
        "current_patch": current,
        "pool": pool,
        "missing_inputs": missing,
    }
    if not decision.should_refresh:
        # The growth gate is untouched by preflight: a closed gate still reports
        # its own reason, with any missing input carried alongside as a warning.
        return base_info
    # Preflight runs only once the gate has already voted to refresh, so it can
    # never widen or narrow the gating decision itself.
    if missing and not (check_only or dry_run):
        reason = "blocked: missing required input(s) -> " + "; ".join(missing)
        streak = _record_attempt_failure(
            state_path,
            state,
            patch_prefix=current,
            error=reason,
            result="blocked",
        )
        return {
            **base_info,
            "blocked": True,
            "reason": reason,
            "consecutive_failures": streak,
        }
    if check_only:
        return {**base_info, "reason": "check only: " + decision.reason}
    if dry_run:
        return {
            **base_info,
            "reason": "dry run: " + decision.reason,
            "commands": [
                " ".join(cmd)
                for cmd in pipeline_commands(
                    db=db, parquet=parquet, out_dir=out_dir, pool=pool,
                    baseline=baseline, prev=prev, current=current,
                    half_life_days=half_life_days, score_csv=score_csv,
                )
            ],
        }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    for cmd in pipeline_commands(
        db=db, parquet=parquet, out_dir=out_dir, pool=pool, baseline=baseline,
        prev=prev, current=current, half_life_days=half_life_days,
        score_csv=score_csv,
    ):
        _run_checked(runner, cmd)
    elapsed = round(time.time() - started, 1)

    _record_patch_state(
        state,
        patch_prefix=current,
        payload={
            _TOTAL_KEY: decision.local_total,
            "last_refresh_at_unix": time.time(),
            "last_pool": pool,
            "last_current_patch": current,
            "last_half_life_days": half_life_days,
            "last_out_dir": str(out_dir),
            "last_elapsed_sec": elapsed,
            "last_result": "refreshed",
            "last_error": None,
            "last_error_at_unix": None,
            "consecutive_failures": 0,
        },
    )
    save_state(state_path, state)

    return {
        **base_info,
        "refreshed": True,
        "elapsed_sec": elapsed,
        "out_dir": str(out_dir),
        "baseline_patch": baseline,
        "prev_patch": prev,
    }
