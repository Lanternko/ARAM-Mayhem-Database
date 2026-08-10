"""Build privacy-safe player x champion mastery statistics and a time holdout.

This is a local analysis artifact for GitHub Issue #16.  It never emits PUUIDs or
Riot IDs.  The public-facing data contract remains aggregate-only; ``pid`` is an
internal integer surrogate created by ``extract_participants.py``.

The model deliberately separates three signals:

* population champion strength (champion win rate);
* player baseline (the player's smoothed overall win rate); and
* player x champion mastery (the interaction, shrunk toward the player baseline).

The backtest uses a chronological cutoff and builds all priors from the earlier
games only.  This is descriptive evidence, not a causal estimate of what a
player would have won on a champion they did not receive.

Examples:
    python scripts/analyze_player_champion_mastery.py --out-dir data/ratings
    python scripts/analyze_player_champion_mastery.py --participants data/ratings/participants.parquet --out-dir outputs/mastery
"""
from __future__ import annotations

import glob
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl


GLOBAL_PRIOR = 0.5


def _latest(pattern: str) -> Path | None:
    paths = sorted(glob.glob(pattern))
    return Path(paths[-1]) if paths else None


def _resolve_path(value: str, pattern: str, *, required: bool = True) -> Path | None:
    if value:
        path = Path(value)
        if not path.exists():
            raise click.ClickException(f"file not found: {path}")
        return path
    path = _latest(pattern)
    if path is None and required:
        raise click.ClickException(f"no input matched {pattern}")
    return path


def _posterior(wins: pl.Expr, games: pl.Expr, prior: pl.Expr | float, strength: float) -> pl.Expr:
    return (wins + strength * prior) / (games + strength)


def build_player_champion_stats(
    participants: pl.DataFrame,
    *,
    k_champion: float = 200.0,
    k_player: float = 50.0,
    k_interaction: float = 10.0,
) -> pl.DataFrame:
    """Return one row per player/champion with empirical-Bayes shrinkage."""
    required = {"pid", "champ", "win"}
    missing = required - set(participants.columns)
    if missing:
        raise ValueError(f"participants missing columns: {sorted(missing)}")
    base = participants.select(
        pl.col("pid").cast(pl.Int64), pl.col("champ").cast(pl.Int64), pl.col("win").cast(pl.Float64)
    )
    global_wr = float(base["win"].mean()) if base.height else GLOBAL_PRIOR
    if not math.isfinite(global_wr):
        global_wr = GLOBAL_PRIOR

    player = base.group_by("pid").agg(
        pl.len().alias("player_games"), pl.col("win").sum().alias("player_wins")
    ).with_columns(
        _posterior(pl.col("player_wins"), pl.col("player_games"), global_wr, k_player).alias("player_wr")
    )
    champ = base.group_by("champ").agg(
        pl.len().alias("champ_games"), pl.col("win").sum().alias("champ_wins")
    ).with_columns(
        _posterior(pl.col("champ_wins"), pl.col("champ_games"), global_wr, k_champion).alias("champ_wr")
    )
    pc = base.group_by("pid", "champ").agg(
        pl.len().alias("games"), pl.col("win").sum().alias("wins")
    )
    out = pc.join(player, on="pid").join(champ, on="champ").with_columns(
        _posterior(pl.col("wins"), pl.col("games"), pl.col("player_wr"), k_interaction).alias("mastery_wr")
    ).with_columns(
        (pl.col("mastery_wr") - pl.col("player_wr")).alias("mastery_lift"),
        (pl.col("mastery_wr") - pl.col("champ_wr")).alias("player_vs_population_lift"),
        (pl.col("games") / (pl.col("games") + k_interaction)).alias("mastery_confidence"),
        pl.lit(global_wr).alias("global_wr"),
    )
    return out.select(
        "pid", "champ", "games", "wins", "player_games", "player_wr", "champ_games",
        "champ_wr", "mastery_wr", "mastery_lift", "player_vs_population_lift",
        "mastery_confidence", "global_wr",
    ).sort(["champ", "mastery_lift"], descending=[False, True])


def build_champion_concentration(participants: pl.DataFrame) -> pl.DataFrame:
    """Return player-level games and realised champion Herfindahl concentration."""
    counts = participants.group_by("pid", "champ").len().rename({"len": "champ_games"})
    totals = counts.group_by("pid").agg(pl.col("champ_games").sum().alias("games"))
    return counts.join(totals, on="pid").with_columns(
        (pl.col("champ_games") / pl.col("games")).pow(2).alias("share_sq")
    ).group_by("pid").agg(
        pl.first("games"), pl.col("share_sq").sum().alias("champion_herfindahl")
    )


def _performance_mastery(performance: pl.DataFrame, *, k_interaction: float = 10.0) -> pl.DataFrame:
    """Compute champ-controlled player x champion damage-per-gold residuals."""
    required = {"gidx", "pid", "champ", "team", "gold", "dmg_champ"}
    missing = required - set(performance.columns)
    if missing:
        raise ValueError(f"performance missing columns: {sorted(missing)}")
    perf = performance.with_columns(
        (pl.col("dmg_champ").cast(pl.Float64) / pl.col("gold").cast(pl.Float64).clip(lower_bound=1))
        .clip(upper_bound=10.0)
        .alias("dpg")
    )
    champ_mean = perf.group_by("champ").agg(pl.col("dpg").mean().alias("champ_dpg"))
    perf = perf.join(champ_mean, on="champ").with_columns(
        (pl.col("dpg") - pl.col("champ_dpg")).alias("dpg_resid")
    )
    player = perf.group_by("pid").agg(pl.col("dpg_resid").mean().alias("player_dpg_resid"))
    pc = perf.group_by("pid", "champ").agg(
        pl.len().alias("performance_games"), pl.col("dpg_resid").mean().alias("pc_dpg_resid")
    )
    return pc.join(player, on="pid").with_columns(
        (pl.col("pc_dpg_resid") - pl.col("player_dpg_resid")).alias("performance_mastery_lift"),
        (pl.col("performance_games") / (pl.col("performance_games") + k_interaction)).alias("performance_confidence"),
    )


def _logit(values: np.ndarray) -> np.ndarray:
    p = np.clip(values.astype(float), 1e-5, 1 - 1e-5)
    return np.log(p / (1 - p))


def _metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    p = np.clip(p.astype(float), 1e-6, 1 - 1e-6)
    y = y.astype(float)
    ll = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    return {"n": int(y.size), "log_loss": ll, "brier": float(np.mean((p - y) ** 2)), "mean_pred": float(p.mean()), "mean_actual": float(y.mean())}


def chronological_backtest(
    participants: pl.DataFrame,
    *,
    performance: pl.DataFrame | None = None,
    test_frac: float = 0.2,
    k_champion: float = 200.0,
    k_player: float = 50.0,
    k_interaction: float = 10.0,
) -> dict[str, Any]:
    """Compare champion-only, champion+player, and mastery features on future rows."""
    if "created_ms" not in participants.columns:
        raise ValueError("participants must include created_ms for a chronological backtest")
    cutoff = float(participants.select(pl.col("created_ms").quantile(1 - test_frac)).item())
    train = participants.filter(pl.col("created_ms") <= cutoff)
    test = participants.filter(pl.col("created_ms") > cutoff)
    if train.height == 0 or test.height == 0:
        raise ValueError("chronological split produced an empty train or test set")
    stats = build_player_champion_stats(train, k_champion=k_champion, k_player=k_player, k_interaction=k_interaction)
    perf_stats = None
    if performance is not None and "created_ms" in performance.columns:
        perf_train = performance.filter(pl.col("created_ms") <= cutoff)
        if perf_train.height:
            perf_stats = _performance_mastery(perf_train, k_interaction=k_interaction)
    global_wr = float(train["win"].mean())
    champ = stats.select("champ", "champ_wr").unique("champ")
    player = stats.select("pid", "player_wr").unique("pid")
    mastery = stats.select("pid", "champ", "mastery_wr").unique(["pid", "champ"])
    if perf_stats is not None:
        mastery = mastery.join(
            perf_stats.select("pid", "champ", "performance_mastery_lift"),
            on=["pid", "champ"], how="left",
        )
    scored = (test.select("pid", "champ", "win")
        .join(champ, on="champ", how="left")
        .join(player, on="pid", how="left")
        .join(mastery, on=["pid", "champ"], how="left")
        .with_columns(
            pl.col("champ_wr").fill_null(global_wr),
            pl.col("player_wr").fill_null(global_wr),
        )
        .with_columns(pl.col("mastery_wr").fill_null(pl.col("player_wr"))))
    if "performance_mastery_lift" not in scored.columns:
        scored = scored.with_columns(pl.lit(0.0).alias("performance_mastery_lift"))
    else:
        scored = scored.with_columns(pl.col("performance_mastery_lift").fill_null(0.0))
    y = scored["win"].to_numpy()
    c = scored["champ_wr"].to_numpy()
    u = scored["player_wr"].to_numpy()
    m = scored["mastery_wr"].to_numpy()
    # Conservative, symmetric exploratory blends.  These are deliberately not
    # presented as production calibration; the point of this report is to test
    # whether the extra signal helps an honest future holdout at all.
    p_cp = 1 / (1 + np.exp(-(0.7 * _logit(c) + 0.3 * _logit(u))))
    p_mastery = 1 / (1 + np.exp(-(0.70 * _logit(c) + 0.25 * _logit(u) + 0.05 * _logit(m))))
    perf_value = scored["performance_mastery_lift"].to_numpy().astype(float)
    perf_sd = float(np.nanstd(perf_value)) or 1.0
    p_performance = 1 / (1 + np.exp(-(0.68 * _logit(c) + 0.25 * _logit(u) + 0.05 * _logit(m) + 0.02 * np.clip(perf_value / perf_sd, -3, 3))))
    return {
        "cutoff_created_ms": int(cutoff),
        "train_rows": int(train.height),
        "test_rows": int(test.height),
        "train_games": int(train.select("created_ms").n_unique()),
        "test_games": int(test.select("created_ms").n_unique()),
        "models": {
            "champion_only": _metrics(y, c),
            "champion_plus_player": _metrics(y, p_cp),
            "champion_player_mastery": _metrics(y, p_mastery),
            "champion_player_performance_mastery": _metrics(y, p_performance),
        },
    }


def _rank_summary(stats: pl.DataFrame, concentration: pl.DataFrame, players_path: Path | None, ranks_db: Path | None) -> dict[str, Any]:
    if players_path is None or ranks_db is None or not ranks_db.exists():
        return {"available": False, "reason": "players parquet or ranks db not supplied"}
    players = pl.read_parquet(players_path).select("pid", "puuid")
    con = sqlite3.connect(f"file:{ranks_db}?mode=ro", uri=True)
    rows = con.execute("SELECT lcu_puuid, solo_tier, solo_div, solo_lp, status FROM player_ranks").fetchall()
    con.close()
    tiers = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    div = {"IV": 0, "III": 1, "II": 2, "I": 3, None: 3}
    rank_rows = []
    for puuid, tier, division, lp, status in rows:
        if status != "ok" or tier not in tiers:
            continue
        ordinal = tiers.index(tier) * 4 + div.get(division, 3)
        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            ordinal += min(int(lp or 0), 1500) / 500
        rank_rows.append({"puuid": puuid, "rank_ordinal": ordinal})
    ranked = players.join(pl.DataFrame(rank_rows), on="puuid", how="inner")
    player_stats = stats.group_by("pid").agg(
        pl.col("mastery_lift").mean().alias("mean_mastery_lift"),
        pl.col("performance_mastery_lift").mean().alias("mean_performance_mastery_lift")
        if "performance_mastery_lift" in stats.columns else pl.lit(None).alias("mean_performance_mastery_lift"),
    )
    joined = ranked.join(concentration, on="pid", how="left").join(player_stats, on="pid", how="left")
    if joined.height < 3:
        return {"available": True, "n_ranked": int(joined.height), "correlations": {}}
    from scipy.stats import spearmanr
    out: dict[str, Any] = {"available": True, "n_ranked": int(joined.height), "correlations": {}}
    for col in ("champion_herfindahl", "mean_mastery_lift", "mean_performance_mastery_lift"):
        vals = joined.select("rank_ordinal", col).drop_nulls()
        if vals.height < 3:
            continue
        rho, p = spearmanr(vals["rank_ordinal"].to_numpy(), vals[col].to_numpy())
        out["correlations"][col] = {"rho": float(rho), "p_value": float(p), "n": int(vals.height)}
    return out


@click.command()
@click.option("--participants", default="", help="participants parquet; defaults to latest data/ratings snapshot")
@click.option("--performance", default="", help="performance parquet; optional")
@click.option("--players", default="", help="players parquet; defaults to latest data/ratings snapshot")
@click.option("--ranks-db", default="data/ratings/player_ranks.db", type=click.Path(path_type=Path))
@click.option("--out-dir", default="data/ratings/mastery_analysis", type=click.Path(path_type=Path))
@click.option("--test-frac", default=0.2, type=click.FloatRange(0.05, 0.5))
@click.option("--min-player-games", default=10, type=int, show_default=True, help="reporting threshold for player-level rows")
@click.option("--min-player-champ-games", default=5, type=int, show_default=True, help="reporting threshold for mastery rows")
def main(participants, performance, players, ranks_db, out_dir, test_frac, min_player_games, min_player_champ_games):
    participants_path = _resolve_path(participants, "data/ratings/participants__q2400__*.parquet")
    performance_path = _resolve_path(performance, "data/ratings/performance__q2400__*.parquet", required=False)
    players_path = _resolve_path(players, "data/ratings/players__q2400__*.parquet", required=False)
    assert participants_path is not None
    part = pl.read_parquet(participants_path)
    stats = build_player_champion_stats(part)
    stats = stats.filter((pl.col("player_games") >= min_player_games) & (pl.col("games") >= min_player_champ_games))
    concentration = build_champion_concentration(part)
    if performance_path is not None:
        perf = _performance_mastery(pl.read_parquet(performance_path))
        stats = stats.join(perf, on=["pid", "champ"], how="left")
    backtest = chronological_backtest(part, performance=pl.read_parquet(performance_path) if performance_path is not None else None, test_frac=test_frac)
    rank = _rank_summary(stats, concentration, players_path, ranks_db if Path(ranks_db).exists() else None)
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    stats.write_parquet(out / "player_champion_mastery.parquet")
    concentration.write_parquet(out / "player_champion_concentration.parquet")
    report = {
        "participants": str(participants_path),
        "performance": str(performance_path) if performance_path else None,
        "rows": int(part.height),
        "players": int(part["pid"].n_unique()),
        "champions": int(part["champ"].n_unique()),
        "reported_mastery_rows": int(stats.height),
        "min_player_games": min_player_games,
        "min_player_champ_games": min_player_champ_games,
        "backtest": backtest,
        "rank": rank,
        "privacy": {"public_identifiers_emitted": False, "identifier_key": "local pid only"},
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
