"""Train DeepSets with champion-level composition scores.

This is the same architecture as train_ability_nn.py, but the static champion
features are the compact score columns:

  wave_clear, CC, engage, damage, poke, sustain, frontline

When --empirical-combat is enabled, damage/CC/frontline are built only from the
train split's participants_json so validation/test metrics do not leak future
combat stats into champion priors.
"""
from __future__ import annotations

import json
import sys
import csv
from pathlib import Path

import click
import numpy as np
import torch
import torch.nn as nn

from aram_nn.eval import accuracy_np, ece_np, log_loss_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_ability_nn import (  # noqa: E402
    DeepSetsAbility,
    eval_model,
    eval_model_temperature,
    fit_temperature,
    load_split_data,
    make_ability_matrix,
    make_loader,
    train_one,
)
from build_empirical_champion_scores import (  # noqa: E402
    blended_percentile_scores,
    collect_empirical_stats_from_rows,
)
from train_semantic_tree import (  # noqa: E402
    SCORE_COLUMNS,
    train_frame_for_empirical_scores,
)
from champion_roles import ROLE_ORDER  # noqa: E402


ROLE_COLUMNS = ROLE_ORDER
SUBJECTIVE_FEATURE_COLUMNS = tuple(list(SCORE_COLUMNS) + [f"role_{name.lower()}" for name in ROLE_COLUMNS])
EMPIRICAL_PROFILE_COLUMNS = (
    "physical_damage_ratio",
    "magic_damage_ratio",
    "true_damage_ratio",
    "units_healed",
)
OBJECTIVE_FEATURE_COLUMNS = (
    "damage_score",
    "cc_score",
    "frontline_score",
    "sustain_score",
    "damage_share",
    "damage_per_min",
    "cc_share",
    "cc_per_min",
    "frontline_share",
    "frontline_per_min",
    "sustain_share",
    "sustain_per_min",
    "physical_damage_ratio",
    "magic_damage_ratio",
    "true_damage_ratio",
    "units_healed",
)
DERIVED_OBJECTIVE_FEATURE_COLUMNS = (
    "ad_ap_balance",
    "ad_ap_gap",
)
OBJECTIVE_FEATURE_CHOICES = OBJECTIVE_FEATURE_COLUMNS + DERIVED_OBJECTIVE_FEATURE_COLUMNS
TEAM_FEATURE_COLUMNS = (
    "team_ad_share",
    "team_ap_share",
    "team_true_share",
    "team_ad_ap_balance",
    "team_ad_ap_gap",
)
TEAM_SOURCE_COLUMNS = (
    "expected_physical_damage_per_min",
    "expected_magic_damage_per_min",
    "expected_true_damage_per_min",
)


def load_score_rows(path: Path) -> dict[int, dict[str, str]]:
    return {
        int(row["champion_id"]): row
        for row in csv.DictReader(path.open(encoding="utf-8-sig"))
    }


def build_score_feature_map(
    score_csv: Path,
    *,
    train_df,
    empirical_combat: bool,
    empirical_min_games: int,
    replace_sustain: bool,
    feature_set: str,
    subjective_features: tuple[str, ...],
    objective_features: tuple[str, ...],
    team_features: tuple[str, ...],
) -> tuple[dict[int, np.ndarray], list[str], dict[str, np.ndarray], dict[str, object] | None]:
    rows = load_score_rows(score_csv)
    stats = {}
    empirical_meta: dict[str, object] | None = None
    damage_scores: dict[int, float] = {}
    cc_scores: dict[int, float] = {}
    frontline_scores: dict[int, float] = {}
    sustain_scores: dict[int, float] = {}
    max_units_healed = 1.0

    needs_empirical = empirical_combat or feature_set == "objective" or bool(team_features)
    if needs_empirical:
        raw_rows = train_df.select(["blue_wins", "duration_sec", "participants_json"]).iter_rows()
        stats = collect_empirical_stats_from_rows(raw_rows)
        damage_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="damage_share", metric_b="damage_per_min"
        )
        cc_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="cc_share", metric_b="cc_per_min"
        )
        frontline_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="frontline_share", metric_b="frontline_per_min"
        )
        sustain_scores = blended_percentile_scores(
            stats, min_games=empirical_min_games, metric_a="sustain_share", metric_b="sustain_per_min"
        )
        max_units_healed = max((row.get("units_healed", 1.0) for row in stats.values()), default=1.0)
        eligible = [cid for cid, row in stats.items() if row.get("games", 0) >= empirical_min_games]
        empirical_meta = {
            "train_rows_used": int(train_df.height),
            "combat_stat_champions": len(stats),
            "eligible_champions": len(eligible),
            "min_games": empirical_min_games,
            "replace_sustain": replace_sustain,
            "feature_set": feature_set,
            "empirical_profile_columns": list(EMPIRICAL_PROFILE_COLUMNS),
        }

    selected_subjective = set(subjective_features)
    selected_objective = tuple(objective_features) or OBJECTIVE_FEATURE_COLUMNS
    if feature_set == "objective":
        feature_names = list(selected_objective)
        if selected_subjective:
            feature_names.extend(f"subjective_{name}" for name in subjective_features)
    else:
        feature_names = (
            list(SCORE_COLUMNS)
            + [f"role_{name.lower()}" for name in ROLE_COLUMNS]
            + (list(EMPIRICAL_PROFILE_COLUMNS) if empirical_combat else [])
        )
        if selected_subjective:
            feature_names = [
                name
                for name in feature_names
                if name in selected_subjective or name in EMPIRICAL_PROFILE_COLUMNS
            ]
    feature_map: dict[int, np.ndarray] = {}
    team_source_map: dict[int, np.ndarray] = {}
    for cid, row in rows.items():
        stat = stats.get(cid, {})
        has_empirical = stat.get("games", 0) >= empirical_min_games
        physical_ratio = float(stat.get("physical_damage_ratio", 0.0)) if has_empirical else 0.0
        magic_ratio = float(stat.get("magic_damage_ratio", 0.0)) if has_empirical else 0.0
        true_ratio = float(stat.get("true_damage_ratio", 0.0)) if has_empirical else 0.0
        damage_per_min = float(stat.get("damage_per_min", 0.0)) if has_empirical else 0.0
        team_source_map[cid] = np.asarray(
            [
                damage_per_min * physical_ratio,
                damage_per_min * magic_ratio,
                damage_per_min * true_ratio,
            ],
            dtype=np.float32,
        )

        if feature_set == "objective":
            ad_ap_gap = abs(physical_ratio - magic_ratio)
            objective_named_values = {
                "damage_score": float(damage_scores.get(cid, 0.0)) if has_empirical else 0.0,
                "cc_score": float(cc_scores.get(cid, 0.0)) if has_empirical else 0.0,
                "frontline_score": float(frontline_scores.get(cid, 0.0)) if has_empirical else 0.0,
                "sustain_score": float(sustain_scores.get(cid, 0.0)) if has_empirical else 0.0,
                "damage_share": float(stat.get("damage_share", 0.0)) if has_empirical else 0.0,
                "damage_per_min": float(stat.get("damage_per_min", 0.0)) if has_empirical else 0.0,
                "cc_share": float(stat.get("cc_share", 0.0)) if has_empirical else 0.0,
                "cc_per_min": float(stat.get("cc_per_min", 0.0)) if has_empirical else 0.0,
                "frontline_share": float(stat.get("frontline_share", 0.0)) if has_empirical else 0.0,
                "frontline_per_min": float(stat.get("frontline_per_min", 0.0)) if has_empirical else 0.0,
                "sustain_share": float(stat.get("sustain_share", 0.0)) if has_empirical else 0.0,
                "sustain_per_min": float(stat.get("sustain_per_min", 0.0)) if has_empirical else 0.0,
                "physical_damage_ratio": physical_ratio,
                "magic_damage_ratio": magic_ratio,
                "true_damage_ratio": true_ratio,
                "ad_ap_balance": 1.0 - ad_ap_gap,
                "ad_ap_gap": ad_ap_gap,
                "units_healed": min(
                    float(stat.get("units_healed", 1.0)) / max(max_units_healed, 1.0),
                    1.0,
                )
                if has_empirical
                else 0.0,
            }
            values = [objective_named_values[name] for name in selected_objective]
            if selected_subjective:
                tags = set((row.get("tags") or "").split("|"))
                subjective_named_values = {
                    **{name: float(row[name]) for name in SCORE_COLUMNS},
                    **{f"role_{name.lower()}": 1.0 if name in tags else 0.0 for name in ROLE_COLUMNS},
                }
                values.extend(subjective_named_values[name] for name in subjective_features)
            feature_map[cid] = np.asarray(values, dtype=np.float32)
            continue

        values = [float(row[col]) for col in SCORE_COLUMNS]
        if empirical_combat and has_empirical:
            col_idx = {name: i for i, name in enumerate(SCORE_COLUMNS)}
            if cid in damage_scores:
                values[col_idx["damage_score"]] = damage_scores[cid]
            if cid in cc_scores:
                values[col_idx["cc_score"]] = cc_scores[cid]
            if cid in frontline_scores:
                values[col_idx["frontline_score"]] = frontline_scores[cid]
            if replace_sustain and cid in sustain_scores:
                values[col_idx["sustain_score"]] = sustain_scores[cid]

        tags = set((row.get("tags") or "").split("|"))
        values.extend(1.0 if role in tags else 0.0 for role in ROLE_COLUMNS)

        if empirical_combat:
            values.extend(
                [
                    float(stat.get("physical_damage_ratio", 0.0)),
                    float(stat.get("magic_damage_ratio", 0.0)),
                    float(stat.get("true_damage_ratio", 0.0)),
                    min(float(stat.get("units_healed", 1.0)) / max(max_units_healed, 1.0), 1.0),
                ]
            )

        if selected_subjective:
            named_values = dict(zip(
                list(SCORE_COLUMNS)
                + [f"role_{name.lower()}" for name in ROLE_COLUMNS]
                + (list(EMPIRICAL_PROFILE_COLUMNS) if empirical_combat else []),
                values,
                strict=True,
            ))
            values = [named_values[name] for name in feature_names]

        feature_map[cid] = np.asarray(values, dtype=np.float32)

    return feature_map, feature_names, team_source_map, empirical_meta


def make_team_source_matrix(
    champ_to_idx: dict[int, int],
    team_source_map: dict[int, np.ndarray],
) -> torch.Tensor:
    mat = np.zeros((len(champ_to_idx), len(TEAM_SOURCE_COLUMNS)), dtype=np.float32)
    for cid, idx in champ_to_idx.items():
        vec = team_source_map.get(cid)
        if vec is not None:
            mat[idx] = vec
    return torch.tensor(mat, dtype=torch.float32)


def _score_mlp(in_dim: int, hidden: int, dropout: float) -> nn.Sequential:
    return nn.Sequential(
        nn.Linear(in_dim, hidden),
        nn.LayerNorm(hidden),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(hidden, hidden // 2),
        nn.GELU(),
        nn.Linear(hidden // 2, 1),
    )


class DeepSetsScoreWithTeamFeatures(nn.Module):
    def __init__(
        self,
        n_champs: int,
        score_features: torch.Tensor,
        team_source_features: torch.Tensor,
        team_feature_names: tuple[str, ...],
        *,
        embed_dim: int,
        score_dim: int,
        hidden: int,
        dropout: float,
    ):
        super().__init__()
        self.embed = nn.Embedding(n_champs, embed_dim)
        self.register_buffer("score_features", score_features)
        self.register_buffer("team_source_features", team_source_features)
        self.team_feature_names = tuple(team_feature_names)
        self.score_proj = nn.Sequential(
            nn.Linear(score_features.shape[1], score_dim),
            nn.LayerNorm(score_dim),
            nn.GELU(),
            nn.Dropout(dropout),
        )
        repr_dim = embed_dim + score_dim
        team_dim = len(self.team_feature_names)
        self.mlp = _score_mlp(2 * repr_dim + 2 * team_dim, hidden, dropout)

    def champion_repr(self, champ_ids: torch.Tensor) -> torch.Tensor:
        emb = self.embed(champ_ids)
        static = self.score_proj(self.score_features[champ_ids])
        return torch.cat([emb, static], dim=-1)

    def team_summary(self, champ_ids: torch.Tensor) -> torch.Tensor:
        source = self.team_source_features[champ_ids].sum(dim=1)
        physical = source[:, 0]
        magic = source[:, 1]
        true = source[:, 2]
        ad_ap_den = (physical + magic).clamp_min(1e-6)
        all_den = (physical + magic + true).clamp_min(1e-6)
        ad_share = physical / ad_ap_den
        ap_share = magic / ad_ap_den
        true_share = true / all_den
        ad_ap_gap = torch.abs(ad_share - ap_share)
        named = {
            "team_ad_share": ad_share,
            "team_ap_share": ap_share,
            "team_true_share": true_share,
            "team_ad_ap_balance": 1.0 - ad_ap_gap,
            "team_ad_ap_gap": ad_ap_gap,
        }
        return torch.stack([named[name] for name in self.team_feature_names], dim=-1)

    def _raw_logit(
        self,
        diff: torch.Tensor,
        total: torch.Tensor,
        team_diff: torch.Tensor,
        team_total: torch.Tensor,
    ) -> torch.Tensor:
        return self.mlp(torch.cat([diff, total, team_diff, team_total], dim=-1)).squeeze(-1)

    def forward(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        e_b = self.champion_repr(blue).sum(dim=1)
        e_r = self.champion_repr(red).sum(dim=1)
        diff = e_b - e_r
        total = e_b + e_r
        team_b = self.team_summary(blue)
        team_r = self.team_summary(red)
        team_diff = team_b - team_r
        team_total = team_b + team_r
        return (
            self._raw_logit(diff, total, team_diff, team_total)
            - self._raw_logit(-diff, total, -team_diff, team_total)
        ) / 2.0

    @torch.no_grad()
    def predict_proba(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward(blue, red))


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--score-csv",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_semantic_scores.csv"),
    show_default=True,
)
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--empirical-combat/--static-combat",
    default=True,
    show_default=True,
    help="Replace damage/CC/frontline with train-split participant stats and add empirical profiles.",
)
@click.option("--empirical-min-games", default=20, show_default=True, type=int)
@click.option(
    "--replace-sustain/--keep-static-sustain",
    default=True,
    show_default=True,
    help="Also replace sustain_score with train-split total_heal stats.",
)
@click.option(
    "--feature-set",
    type=click.Choice(["subjective", "objective", "full"]),
    default="full",
    show_default=True,
    help="Static feature group for DeepSets+scores.",
)
@click.option(
    "--subjective-feature",
    "subjective_features",
    type=click.Choice(SUBJECTIVE_FEATURE_COLUMNS),
    multiple=True,
    default=(),
    help="Restrict subjective features to the selected column(s). Repeatable.",
)
@click.option(
    "--objective-feature",
    "objective_features",
    type=click.Choice(OBJECTIVE_FEATURE_CHOICES),
    multiple=True,
    default=(),
    help="Restrict objective features to the selected column(s). Repeatable.",
)
@click.option(
    "--team-feature",
    "team_features",
    type=click.Choice(TEAM_FEATURE_COLUMNS),
    multiple=True,
    default=(),
    help="Add derived team-level damage-mix feature(s). Repeatable.",
)
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--score-dim", default=12, show_default=True)
@click.option("--hidden", default=96, show_default=True)
@click.option("--dropout", default=0.25, show_default=True, type=float)
@click.option("--lr", default=2e-3, show_default=True, type=float)
@click.option("--weight-decay", default=8e-3, show_default=True, type=float)
@click.option("--epochs", default=45, show_default=True, type=int)
@click.option("--batch-size", default=512, show_default=True, type=int)
@click.option("--patience", default=5, show_default=True, type=int)
@click.option("--eval-every", default=3, show_default=True, type=int)
@click.option("--seed", default=42, show_default=True, type=int)
def main(
    data: Path,
    score_csv: Path,
    patch_prefix: str,
    out: Path,
    empirical_combat: bool,
    empirical_min_games: int,
    replace_sustain: bool,
    feature_set: str,
    subjective_features: tuple[str, ...],
    objective_features: tuple[str, ...],
    team_features: tuple[str, ...],
    embed_dim: int,
    score_dim: int,
    hidden: int,
    dropout: float,
    lr: float,
    weight_decay: float,
    epochs: int,
    batch_size: int,
    patience: int,
    eval_every: int,
    seed: int,
) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    train_df_for_scores = train_frame_for_empirical_scores(data, patch_prefix)
    if feature_set == "subjective":
        empirical_combat = False
    elif feature_set == "objective":
        empirical_combat = True
    score_map, score_names, team_source_map, empirical_meta = build_score_feature_map(
        score_csv,
        train_df=train_df_for_scores,
        empirical_combat=empirical_combat,
        empirical_min_games=empirical_min_games,
        replace_sustain=replace_sustain,
        feature_set=feature_set,
        subjective_features=subjective_features,
        objective_features=objective_features,
        team_features=team_features,
    )
    if empirical_combat and empirical_meta is not None:
        click.echo(
            "[empirical] train-split combat overlay: "
            f"rows={empirical_meta['train_rows_used']} "
            f"eligible_champions={empirical_meta['eligible_champions']}/"
            f"{empirical_meta['combat_stat_champions']} "
            f"min_games={empirical_min_games} replace_sustain={replace_sustain}"
        )

    splits = load_split_data(data, patch_prefix)
    score_matrix, missing = make_ability_matrix(splits.champ_to_idx, score_map)
    team_source_matrix = make_team_source_matrix(splits.champ_to_idx, team_source_map)
    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={len(splits.champ_to_idx)} blue_base_rate={splits.blue_base_rate:.4f}"
    )
    click.echo(f"[scores] features={score_matrix.shape[1]} missing_champions={missing or 'none'}")
    if team_features:
        click.echo(f"[team] features={list(team_features)} source={list(TEAM_SOURCE_COLUMNS)}")

    train_loader = make_loader(splits.train, batch_size, True)
    val_loader = make_loader(splits.val, batch_size, False)
    test_loader = make_loader(splits.test, batch_size, False)

    val_const = {
        "log_loss": log_loss_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "acc": accuracy_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "ece": ece_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
    }
    test_const = {
        "log_loss": log_loss_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "acc": accuracy_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "ece": ece_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
    }

    lr_results = lr_train_eval(
        splits.train_lr,
        splits.val_lr,
        splits.test_lr,
        len(splits.champ_to_idx),
    )
    click.echo(
        f"[LR] val_log_loss={lr_results['val/log_loss']:.4f} val_acc={lr_results['val/acc']:.4f} "
        f"test_log_loss={lr_results['test/log_loss']:.4f} test_acc={lr_results['test/acc']:.4f}"
    )

    click.echo("\n[DeepSets embedding-only]")
    base_model = DeepSetsAbility(
        len(splits.champ_to_idx),
        None,
        embed_dim=embed_dim,
        ability_dim=score_dim,
        hidden=hidden,
        dropout=dropout,
    ).to(device)
    click.echo(f"  params={sum(p.numel() for p in base_model.parameters()):,}")
    base_model, base_best_val = train_one(
        base_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    base_val = eval_model(base_model, val_loader, device)
    base_test = eval_model(base_model, test_loader, device)

    click.echo("\n[DeepSets + score features]")
    if team_features:
        score_model = DeepSetsScoreWithTeamFeatures(
            len(splits.champ_to_idx),
            score_matrix,
            team_source_matrix,
            team_features,
            embed_dim=embed_dim,
            score_dim=score_dim,
            hidden=hidden,
            dropout=dropout,
        ).to(device)
    else:
        score_model = DeepSetsAbility(
            len(splits.champ_to_idx),
            score_matrix,
            embed_dim=embed_dim,
            ability_dim=score_dim,
            hidden=hidden,
            dropout=dropout,
        ).to(device)
    click.echo(f"  params={sum(p.numel() for p in score_model.parameters()):,}")
    score_model, score_best_val = train_one(
        score_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    score_val = eval_model(score_model, val_loader, device)
    score_test = eval_model(score_model, test_loader, device)
    temperature = fit_temperature(score_model, val_loader, device)
    score_val_cal = eval_model_temperature(score_model, val_loader, device, temperature)
    score_test_cal = eval_model_temperature(score_model, test_loader, device, temperature)

    rows = [
        ("val", "Constant", val_const),
        ("val", "LR", {"log_loss": lr_results["val/log_loss"], "acc": lr_results["val/acc"], "ece": None}),
        ("val", "DeepSets", base_val),
        ("val", "DeepSets+scores", score_val),
        ("val", "DeepSets+scores cal", score_val_cal),
        ("test", "Constant", test_const),
        ("test", "LR", {"log_loss": lr_results["test/log_loss"], "acc": lr_results["test/acc"], "ece": None}),
        ("test", "DeepSets", base_test),
        ("test", "DeepSets+scores", score_test),
        ("test", "DeepSets+scores cal", score_test_cal),
    ]
    click.echo("\n[results]")
    headers = ["split", "model", "log_loss", "acc", "ece"]
    table = []
    for split, model_name, result in rows:
        ece = result["ece"]
        table.append(
            [
                split,
                model_name,
                f"{result['log_loss']:.4f}",
                f"{result['acc']:.4f}",
                "-" if ece is None else f"{ece:.4f}",
            ]
        )
    widths = [max(len(h), max(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in table:
        click.echo("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))

    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "data": str(data),
        "score_csv": str(score_csv),
        "patch_prefix": patch_prefix,
        "seed": seed,
        "n_champs": len(splits.champ_to_idx),
        "score_columns": score_names,
        "feature_set": feature_set,
        "subjective_features": list(subjective_features),
        "objective_features": list(objective_features),
        "team_features": list(team_features),
        "team_feature_source_columns": list(TEAM_SOURCE_COLUMNS),
        "score_feature_count": int(score_matrix.shape[1]),
        "missing_score_champions": missing,
        "empirical_combat": empirical_combat,
        "empirical_meta": empirical_meta,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "blue_base_rate": splits.blue_base_rate,
        "base_best_val_log_loss": base_best_val,
        "score_best_val_log_loss": score_best_val,
        "score_temperature": temperature,
        "results": {f"{split}/{name}": result for split, name, result in rows},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    torch.save(
        {
            "base_model": base_model.state_dict(),
            "score_model": score_model.state_dict(),
            "score_temperature": temperature,
            "champ_to_idx": splits.champ_to_idx,
            "score_feature_names": score_names,
            "score_matrix": score_matrix.cpu(),
            "team_feature_names": list(team_features),
            "team_source_columns": list(TEAM_SOURCE_COLUMNS),
            "team_source_matrix": team_source_matrix.cpu(),
        },
        out / "checkpoint.pt",
    )
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'checkpoint.pt'}")


if __name__ == "__main__":
    main()
