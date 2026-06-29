"""Build the shipped (champion x team-archetype) empirical comp-fit artifact.

Productionizes the validated Q3 signal (scripts/ablation_champ_archetype_persistence.py,
cross-patch train->test r=+0.38): for each champion, how much it over/under-performs
when its FOUR teammates (leave-one-out, so the champion isn't credited for making its
own team that archetype) lean toward each of the 6 comp archetypes.

This drives the site's "comp fit" radar with the EMPIRICAL signal (what comp to build
AROUND this champion) instead of the old heuristic blend (tautological: a re-projection
of the champion's own ability bars).  Jinx is the canonical case: heuristic says "AD
carry comp" (her highest axis) but empirically that is her WORST fit (-4pp, redundant
carries); her best is dive (+2.4pp).

Discipline mirrors role_synergy.py: the per-cell delta is the mean residual of an
additive Champion LR (champion main effects removed -> the value is the non-additive
interaction, not "X is good" or "dive wins"), shrunk by n/(n+k) and a persistence
haircut so low-support cells (which flip sign cross-patch) collapse toward 0.  A champion
with no archetype cell clearing --min-cell carries `qualified=false` and the frontend
falls back to the heuristic radar, flagged "estimated".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix  # noqa: E402
from train_ability_nn import TeamDataset, build_vocab  # noqa: E402
from ablation_champ_archetype_persistence import (  # noqa: E402
    COMP_FIT_DEFS, ARCHETYPE_LABELS, accumulate, build_cap_pct,
    calibrate_thresholds, fit_champion_lr, load_comp_vectors,
)

SCHEMA_KIND = "champ_archetype_fit"


def load_all(data: Path, patches, *, min_duration=300, val_frac=0.15):
    """Whole-window dataset (vocab over ALL games) + a time-tail val for the LR's
    C-selection.  Residuals are taken on every game; the per-cell estimate uses the
    full window, unlike the ablation which holds out a test split for the gate."""
    import polars as pl

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= min_duration)
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
    )
    if patches:
        df = df.filter(pl.col("patch_prefix").is_in(list(patches)))
    if df.height == 0:
        raise click.ClickException("no rows after filters")
    df = df.sort("game_creation_ms")
    champ_to_idx = build_vocab(df)
    n = df.height
    n_val = max(1, int(n * val_frac))
    full = TeamDataset(df, champ_to_idx)
    tr = TeamDataset(df.slice(0, n - n_val), champ_to_idx)
    val = TeamDataset(df.slice(n - n_val, n_val), champ_to_idx)
    patch_list = sorted(set(df["patch_prefix"].to_list()))
    return full, tr, val, champ_to_idx, patch_list, n


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_pooled_16_10_12.parquet"),
              type=click.Path(exists=True, path_type=Path), show_default=True,
              help="Pooled cross-patch parquet -> most stable per-cell support.")
@click.option("--patches", default="", show_default=True,
              help="Comma-separated patch prefixes to include; empty = every patch in the file.")
@click.option("--payload", default=Path("docs/api/tier-list.json"),
              type=click.Path(exists=True, path_type=Path), show_default=True)
@click.option("--membership-quantile", default=0.667, show_default=True)
@click.option("--shrink-k", default=150.0, show_default=True,
              help="n/(n+k) shrink: kills low-support cells (same k as role_synergy).")
@click.option("--persistence-factor", default=0.5, show_default=True,
              help="Conservative train->test regression-to-mean haircut (role_synergy uses 0.5).")
@click.option("--min-cell", default=300, show_default=True,
              help="Below this game count an archetype cell is unqualified (frontend falls back).")
@click.option("--out", default=Path("docs/api/champ-archetype-fit.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, patches, payload, membership_quantile, shrink_k, persistence_factor, min_cell, out):
    patch_set = [p.strip() for p in patches.split(",") if p.strip()]
    print("[1/4] comp vectors + percentile norm ...", flush=True)
    comp_by_cid = load_comp_vectors(payload)
    cap_pct = build_cap_pct(comp_by_cid)
    champ_meta = json.loads(Path(payload).read_text(encoding="utf-8")).get("champs", {})
    names = {int(k): (v.get("alias") or v.get("name") or k) for k, v in champ_meta.items()}

    print("[2/4] loading whole window + fitting additive Champion LR ...", flush=True)
    full, tr, val, champ_to_idx, patch_list, n_games = load_all(data, patch_set)
    n_champs = len(champ_to_idx)
    idx_to_cid = {i: c for c, i in champ_to_idx.items()}
    model = fit_champion_lr(champion_matrix(tr, n_champs), np.asarray(tr.labels, dtype=np.float64),
                            champion_matrix(val, n_champs), np.asarray(val.labels, dtype=np.float64))
    res_full = np.asarray(full.labels, dtype=np.float64) - model.predict_proba(champion_matrix(full, n_champs))[:, 1]

    print("[3/4] thresholds + accumulating champion x archetype cells ...", flush=True)
    thresholds = calibrate_thresholds(full, idx_to_cid, cap_pct, membership_quantile)
    ssum, cnt, _ = accumulate(full, res_full, idx_to_cid, cap_pct, thresholds)

    print("[4/4] shrink + persistence haircut -> artifact ...", flush=True)
    champs_out: dict[str, dict] = {}
    qualified_cells = 0
    for cid in sorted(idx_to_cid.values()):
        idx = champ_to_idx[cid]
        cell = {}
        any_q = False
        for a in COMP_FIT_DEFS:
            n = cnt.get((idx, a), 0)
            raw_pp = (ssum.get((idx, a), 0.0) / n * 100) if n else 0.0
            shrunk = raw_pp * (n / (n + shrink_k)) * persistence_factor
            q = n >= min_cell
            any_q = any_q or q
            if q:
                qualified_cells += 1
            cell[a] = {"delta": round(shrunk, 3), "raw_pp": round(raw_pp, 3), "n": int(n), "q": q}
        champs_out[str(cid)] = {"fit": cell, "qualified": any_q}

    artifact = {
        "kind": SCHEMA_KIND,
        "version": 1,
        "source_parquet": str(data),
        "patches": patch_list,
        "n_games": n_games,
        "params": {
            "membership_quantile": membership_quantile,
            "shrink_k": shrink_k,
            "persistence_factor": persistence_factor,
            "min_cell": min_cell,
        },
        "archetypes": list(COMP_FIT_DEFS.keys()),
        "archetype_labels": ARCHETYPE_LABELS,
        "thresholds": {a: round(v, 4) for a, v in thresholds.items()},
        "champs": champs_out,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    n_qual_champ = sum(1 for v in champs_out.values() if v["qualified"])
    print(f"\npatches={patch_list}  games={n_games}  champs={len(champs_out)}  "
          f"qualified champs={n_qual_champ}  qualified cells={qualified_cells}")
    print(f"written: {out}\n")
    print("sanity (shrunk delta pp by archetype; + = build this comp around him, - = avoid):")
    for who in ("Jinx", "Malphite", "Soraka", "Amumu", "Kaisa"):
        cid = next((c for c, nm in names.items() if nm.lower() == who.lower()), None)
        if cid is None or str(cid) not in champs_out:
            print(f"  {who}: n/a"); continue
        fit = champs_out[str(cid)]["fit"]
        ordered = sorted(COMP_FIT_DEFS, key=lambda a: fit[a]["delta"], reverse=True)
        parts = "  ".join(f"{a}:{fit[a]['delta']:+.2f}(n={fit[a]['n']})" for a in ordered)
        print(f"  {who:9} {parts}")


if __name__ == "__main__":
    main()
