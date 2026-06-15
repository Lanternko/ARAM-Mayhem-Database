"""Build anchor-conditional champion x teammate-ROLE synergy JSON.

Replaces the raw champion x champion pair synergy (winner's-curse noise, r~0.17)
with the role-pooled signal (r~0.37; scripts/ablation_champ_role_persistence.py).
Champion primary roles come from the curated site spec (scripts/champion_roles.py),
resolved via the local id->alias map in champion_semantic_scores.csv so this runs
headless (no LCU / DDragon needed) inside the model-refresh pipeline.

  python scripts/build_role_synergy.py \
      --data data/raw/mayhem_pooled_16_10_12.parquet \
      --out models/composition_lr_pooled_recency_7d/role_synergy.json
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
from champion_roles import role_tags_for_alias  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.role_synergy import build_champ_role_synergy, save_role_synergy  # noqa: E402


def load_role_by_champ(scores_csv: Path) -> dict[int, str]:
    """championId -> curated primary role, from the local id->alias map."""
    role_by_champ: dict[int, str] = {}
    with Path(scores_csv).open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                cid = int(row["champion_id"])
            except (KeyError, ValueError):
                continue
            alias = (row.get("champion_alias") or "").strip()
            ddragon_tags = [t for t in (row.get("tags") or "").replace(",", "|").split("|") if t.strip()]
            tags = role_tags_for_alias(alias, ddragon_tags)
            if tags:
                role_by_champ[cid] = tags[0]
    return role_by_champ


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path),
              help="Pooled parquet from scripts/export_pooled_parquet.py")
@click.option("--scores-csv", default=Path("data/cache/champion_semantic_scores.csv"),
              type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--queue", "queue_id", type=int, default=2400, show_default=True)
@click.option("--patches", default="16.10,16.11,16.12", show_default=True,
              help="Recorded as metadata only; the parquet already defines the pool.")
@click.option("--min-cell", type=int, default=150, show_default=True,
              help="Min games in the present bucket (anchor's team HAS the role).")
@click.option("--min-rest", type=int, default=None,
              help="Min games in the rest bucket (anchor's team LACKS the role). Default = min-cell.")
@click.option("--shrink-k", type=float, default=150.0, show_default=True)
@click.option("--persistence-factor", type=float, default=0.5, show_default=True,
              help="Global train->test regression haircut (~ ablation r).")
@click.option("--out", "out_path", type=click.Path(path_type=Path),
              default=Path("models/composition_lr_pooled_recency_7d/role_synergy.json"),
              show_default=True)
def main(data, scores_csv, queue_id, patches, min_cell, min_rest, shrink_k, persistence_factor, out_path):
    role_by_champ = load_role_by_champ(scores_csv)
    eff_min_rest = min_cell if min_rest is None else min_rest
    click.echo(
        f"[role-syn] data={data}  champs_with_role={len(role_by_champ)}  "
        f"min_cell={min_cell}  min_rest={eff_min_rest}  shrink_k={shrink_k:g}  "
        f"persistence={persistence_factor:g}"
    )
    stats = build_champ_role_synergy(
        data,
        role_by_champ=role_by_champ,
        queue_id=queue_id,
        patch_prefix=patches,
        min_cell=min_cell,
        min_rest=min_rest,
        shrink_k=shrink_k,
        persistence_factor=persistence_factor,
    )
    save_role_synergy(stats, out_path)

    deltas = sorted(stats.rows.values(), key=lambda r: r.delta)
    n = len(deltas)
    click.echo(
        f"[role-syn] wrote {out_path}  cells={n:,}  roles={list(stats.roles)}  "
        f"matches={stats.total_matches:,}"
    )
    if n:
        lo, hi = deltas[0], deltas[-1]
        click.echo(
            f"[role-syn] delta range (post-shrink): "
            f"{lo.anchor_id}x{lo.role} {lo.delta*100:+.2f}pp .. "
            f"{hi.anchor_id}x{hi.role} {hi.delta*100:+.2f}pp"
        )


if __name__ == "__main__":
    main()
