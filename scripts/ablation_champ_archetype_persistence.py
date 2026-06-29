"""Ablation (go/no-go gate): champion x TEAM-ARCHETYPE interaction persistence.

Q3 of the comp-fit thread: "does a champion win more when his team leans toward a
particular comp archetype (dive / poke / sustain / front-to-back / ...)?"  Before
turning that into a feature or a UI, we must check it is signal, not winner's-curse
noise.  This reuses the exact residual-synergy methodology of the shipped
champion x teammate-ROLE test (scripts/ablation_champ_role_persistence.py):

  1. Fit an additive Champion LR on TRAIN and take residual = y - p_hat.  The
     champion main effects (and the linear part of "X is good") are absorbed, so
     the residual is the NON-additive remainder -- exactly the interaction term
     "does X over/under-perform when his team is archetype A".
  2. Characterize each team by which of the 6 comp archetypes it "has", computed
     from the FOUR OTHER teammates (anchor excluded) so we don't tautologically
     credit X for making his own team dive.  Archetypes reuse the SAME 6 hand-
     weighted blends the site radar ships (docs/index.html COMP_FIT_DEFS), scored
     on the SAME 9-dim capability vectors the payload ships (champ.comp).
  3. Accumulate signed residual into (champion, archetype) cells, then measure
     train->test correlation across qualified cells.

Reference baselines on the same data/split (from the role/pair ablations):
    champion x champion pair  : r ~ +0.17  (noise -> abandoned)
    champion x teammate-ROLE   : r ~ +0.37  (signal -> shipped)
If champion x ARCHETYPE lands near +0.37 it is worth building; near +0.17 it is
the same trap and should NOT ship un-pooled.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import click
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix, C_GRID  # noqa: E402
from train_ability_nn import load_split_data, TeamDataset, build_vocab  # noqa: E402


class _CrossPatchSplit:
    """Minimal SplitData stand-in for the cross-patch walk-forward gate."""
    def __init__(self, train, val, test, champ_to_idx):
        self.train, self.val, self.test, self.champ_to_idx = train, val, test, champ_to_idx


def load_cross_patch_split(data: Path, train_patches, test_patch, *, min_duration=300, val_frac=0.15):
    """Train on a pool of earlier patches, test on a later one -- the project's
    'real' gate (composition is patch-fragile: in-split overstates).  Vocab is
    built from TRAIN only; the LR's C is picked on a time-tail val of the train
    pool so model selection never peeks at the held-out patch."""
    import polars as pl

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= min_duration)
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
    )
    train_set, test_set = set(train_patches), {test_patch}
    df_train_all = df.filter(pl.col("patch_prefix").is_in(list(train_set))).sort("game_creation_ms")
    df_test = df.filter(pl.col("patch_prefix").is_in(list(test_set))).sort("game_creation_ms")
    if df_train_all.height == 0 or df_test.height == 0:
        raise click.ClickException(f"empty split: train={df_train_all.height} test={df_test.height}")

    n = df_train_all.height
    n_val = max(1, int(n * val_frac))
    df_train = df_train_all.slice(0, n - n_val)
    df_val = df_train_all.slice(n - n_val, n_val)

    champ_to_idx = build_vocab(df_train)
    known = set(champ_to_idx)

    def filter_known(d):
        mask = (
            d["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
            & d["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        )
        return d.filter(mask)

    return _CrossPatchSplit(
        TeamDataset(df_train, champ_to_idx),
        TeamDataset(filter_known(df_val), champ_to_idx),
        TeamDataset(filter_known(df_test), champ_to_idx),
        champ_to_idx,
    )

# The 9 capability dims the site radar percentile-ranks per champion.  Must match
# COMP_STAT_KEYS in docs/index.html so offline gate == online radar.
COMP_STAT_KEYS = ("front", "engage", "poke", "magic", "phys", "sustain", "cc", "wave", "damage")

# The 6 comp archetypes -- identical weights to docs/index.html COMP_FIT_DEFS.
# (user decision 2026-06-17: reuse the existing hand-tuned axes for Q3.)
COMP_FIT_DEFS = {
    "dive":      {"engage": 0.45, "cc": 0.30, "front": 0.25},
    "poke":      {"poke": 0.70, "wave": 0.30},
    "adc":       {"phys": 0.60, "damage": 0.40},
    "mage":      {"magic": 0.60, "damage": 0.40},
    "sustain":   {"sustain": 0.60, "poke": 0.25, "cc": 0.15},
    "frontback": {"front": 0.40, "damage": 0.35, "cc": 0.25},
}
ARCHETYPE_LABELS = {
    "dive": "Dive/衝排", "poke": "Poke/消耗", "adc": "AD carry/物理後排",
    "mage": "Mage core/法師核心", "sustain": "Sustain/續航", "frontback": "Front-to-back/前後排",
}


def load_comp_vectors(payload_path: Path) -> dict[int, dict[str, float]]:
    """champ.comp from the shipped tier-list payload -> {cid: {dim: raw value}}."""
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    champs = payload.get("champs") or {}
    out: dict[int, dict[str, float]] = {}
    for cid_str, info in champs.items():
        comp = (info or {}).get("comp") or {}
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        out[cid] = {k: float(comp.get(k, 0.0) or 0.0) for k in COMP_STAT_KEYS}
    return out


def build_cap_pct(comp_by_cid: dict[int, dict[str, float]]) -> dict[int, dict[str, float]]:
    """Percentile-rank each dim across all champions -- mirrors compNorm() in the JS:
    fraction of champions strictly weaker, divided by (n-1)."""
    cols = {k: sorted(c[k] for c in comp_by_cid.values()) for k in COMP_STAT_KEYS}
    n = len(comp_by_cid)
    out: dict[int, dict[str, float]] = {}
    for cid, comp in comp_by_cid.items():
        pct = {}
        for k in COMP_STAT_KEYS:
            arr = cols[k]
            v = comp[k]
            lt = 0
            while lt < n and arr[lt] < v:
                lt += 1
            pct[k] = 0.0 if n < 2 else max(0.0, min(1.0, lt / (n - 1)))
        out[cid] = pct
    return out


def archetype_scores(teammate_cids, cap_pct: dict[int, dict[str, float]]) -> dict[str, float]:
    """Per-archetype membership score for a set of teammates (anchor already excluded):
    average their capability percentiles, then apply each archetype's weight blend."""
    vecs = [cap_pct[c] for c in teammate_cids if c in cap_pct]
    if not vecs:
        return {a: 0.0 for a in COMP_FIT_DEFS}
    avg = {k: sum(v[k] for v in vecs) / len(vecs) for k in COMP_STAT_KEYS}
    return {a: sum(w * avg[k] for k, w in wts.items()) for a, wts in COMP_FIT_DEFS.items()}


def calibrate_thresholds(dataset, idx_to_cid, cap_pct, quantile):
    """Per-archetype membership cutoff from TRAIN leave-one-out team scores.
    A team 'has' archetype A iff the 4-other-teammates score is at/above this cut,
    so each team triggers ~(1-quantile) of the 6 archetypes -- multi-membership,
    and unbiased across axes with different base levels."""
    per_arch = defaultdict(list)
    for blue, red in zip(dataset.blue, dataset.red):
        for team in (blue, red):
            cids = [idx_to_cid[i] for i in team]
            for c in cids:
                others = [x for x in cids if x != c]
                sc = archetype_scores(others, cap_pct)
                for a, v in sc.items():
                    per_arch[a].append(v)
    return {a: float(np.quantile(vals, quantile)) for a, vals in per_arch.items()}


def accumulate(dataset, residual, idx_to_cid, cap_pct, thresholds):
    """Signed residual per (champion-idx, archetype) over games where the champion's
    FOUR teammates give the team membership in that archetype.  Also a continuous
    variant: residual weighted by centered archetype score (threshold-free)."""
    ssum = defaultdict(float)
    cnt = defaultdict(int)
    cont_sum = defaultdict(float)  # residual * (score - thr), threshold-free robustness
    for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
        r = residual[i]
        for team, sign in ((blue, 1.0), (red, -1.0)):
            sr = r * sign
            cids = [idx_to_cid[j] for j in team]
            for c_idx, c_cid in zip(team, cids):
                others = [x for x in cids if x != c_cid]
                sc = archetype_scores(others, cap_pct)
                for a, v in sc.items():
                    cont_sum[(c_idx, a)] += sr * (v - thresholds[a])
                    if v >= thresholds[a]:
                        ssum[(c_idx, a)] += sr
                        cnt[(c_idx, a)] += 1
    return ssum, cnt, cont_sum


def champ_archetype_drill(dataset, residual, idx_to_cid, cap_pct, thresholds, champ_idx):
    """For one champion: residual split by whether his team has each archetype (in/out)."""
    out = {}
    for a in COMP_FIT_DEFS:
        cells = {"in": [0.0, 0], "out": [0.0, 0]}
        for i, (blue, red) in enumerate(zip(dataset.blue, dataset.red)):
            for team, sign in ((blue, 1.0), (red, -1.0)):
                if champ_idx not in team:
                    continue
                cids = [idx_to_cid[j] for j in team]
                others = [x for x in cids if x != idx_to_cid[champ_idx]]
                v = archetype_scores(others, cap_pct)[a]
                bucket = "in" if v >= thresholds[a] else "out"
                cells[bucket][0] += residual[i] * sign
                cells[bucket][1] += 1
        out[a] = {b: {"resid_pp": round(c[0] / c[1] * 100, 2) if c[1] else None, "n": c[1]}
                  for b, c in cells.items()}
    return out


def fit_champion_lr(x_train, y_train, x_val, y_val):
    best_c, best_ll = C_GRID[0], float("inf")
    for c in C_GRID:
        m = LogisticRegression(C=c, max_iter=2000, solver="lbfgs")
        m.fit(x_train, y_train)
        ll = log_loss(y_val, m.predict_proba(x_val)[:, 1])
        if ll < best_ll:
            best_c, best_ll = c, ll
    m = LogisticRegression(C=best_c, max_iter=2000, solver="lbfgs")
    m.fit(x_train, y_train)
    return m


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"),
              type=click.Path(exists=True, path_type=Path), show_default=True,
              help="Within-patch parquet (default). For --cross-patch pass the pooled file.")
@click.option("--patch-prefix", default="16.10", show_default=True,
              help="Within-patch mode only: the single patch to time-split.")
@click.option("--cross-patch", is_flag=True, default=False,
              help="Walk-forward gate: train on --train-patches, test on --test-patch (the 'real' gate).")
@click.option("--train-patches", default="16.10,16.11", show_default=True,
              help="Comma-separated patch prefixes for the cross-patch train pool.")
@click.option("--test-patch", default="16.12", show_default=True)
@click.option("--payload", default=Path("docs/api/tier-list.json"), type=click.Path(exists=True, path_type=Path),
              show_default=True, help="Shipped payload; champ.comp vectors are read from here.")
@click.option("--membership-quantile", default=0.667, show_default=True,
              help="A team 'has' archetype A iff its 4-other score is at/above this train quantile.")
@click.option("--min-games", default=300, show_default=True)
@click.option("--out", default=Path("outputs/ablation_champ_archetype_persistence.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, patch_prefix, cross_patch, train_patches, test_patch, payload, membership_quantile, min_games, out):
    print("[1/5] loading champ.comp vectors + percentile norm ...", flush=True)
    comp_by_cid = load_comp_vectors(payload)
    cap_pct = build_cap_pct(comp_by_cid)

    if cross_patch:
        tp = [p.strip() for p in train_patches.split(",") if p.strip()]
        print(f"[2/5] loading CROSS-PATCH split: train {tp} -> test {test_patch} ...", flush=True)
        splits = load_cross_patch_split(data, tp, test_patch)
        split_desc = f"cross-patch train={'+'.join(tp)} test={test_patch}"
    else:
        print(f"[2/5] loading within-patch time split ({patch_prefix}) ...", flush=True)
        splits = load_split_data(data, patch_prefix)
        split_desc = f"within-patch {patch_prefix} time-split"
    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    names = {int(c): (info or {}).get("alias") or (info or {}).get("name") or str(c)
             for c, info in json.loads(Path(payload).read_text(encoding="utf-8")).get("champs", {}).items()}
    missing = [idx_to_cid[i] for i in range(n_champs) if idx_to_cid[i] not in cap_pct]
    if missing:
        print(f"      warn: {len(missing)} champs in vocab lack comp vectors (treated as 0): {missing[:10]}")

    y_train = np.asarray(splits.train.labels, dtype=np.float64)
    y_val = np.asarray(splits.val.labels, dtype=np.float64)
    y_test = np.asarray(splits.test.labels, dtype=np.float64)

    print("[3/5] fitting additive Champion LR -> residuals ...", flush=True)
    model = fit_champion_lr(champion_matrix(splits.train, n_champs), y_train,
                            champion_matrix(splits.val, n_champs), y_val)
    res_train = y_train - model.predict_proba(champion_matrix(splits.train, n_champs))[:, 1]
    res_test = y_test - model.predict_proba(champion_matrix(splits.test, n_champs))[:, 1]

    print("[4/5] calibrating archetype thresholds + accumulating cells ...", flush=True)
    thresholds = calibrate_thresholds(splits.train, idx_to_cid, cap_pct, membership_quantile)
    tr_sum, tr_cnt, tr_cont = accumulate(splits.train, res_train, idx_to_cid, cap_pct, thresholds)
    te_sum, te_cnt, te_cont = accumulate(splits.test, res_test, idx_to_cid, cap_pct, thresholds)

    print("[5/5] persistence + named drill-downs ...", flush=True)
    rows = []
    for key, c_tr in tr_cnt.items():
        c_te = te_cnt.get(key, 0)
        if c_tr < min_games or c_te < min_games:
            continue
        rows.append({
            "champ": names.get(idx_to_cid[key[0]], str(idx_to_cid[key[0]])),
            "archetype": key[1],
            "train_pp": tr_sum[key] / c_tr * 100,
            "train_games": c_tr,
            "test_pp": te_sum[key] / c_te * 100,
            "test_games": c_te,
            "train_cont": tr_cont[key] / c_tr * 100,
            "test_cont": te_cont.get(key, 0.0) / c_te * 100,
        })

    tr = np.array([r["train_pp"] for r in rows])
    te = np.array([r["test_pp"] for r in rows])
    corr = float(np.corrcoef(tr, te)[0, 1]) if len(rows) > 2 else float("nan")
    cont_corr = (float(np.corrcoef([r["train_cont"] for r in rows], [r["test_cont"] for r in rows])[0, 1])
                 if len(rows) > 2 else float("nan"))
    rows_sorted = sorted(rows, key=lambda r: r["train_pp"], reverse=True)
    top, bot = rows_sorted[:20], rows_sorted[-20:]
    summary = {
        "split": split_desc,
        "membership_quantile": membership_quantile,
        "min_games": min_games,
        "qualified_cells": len(rows),
        "avg_cell_cooccurrence_train": float(np.mean(list(tr_cnt.values()))) if tr_cnt else 0.0,
        "train_test_correlation_binary": round(corr, 4),
        "train_test_correlation_continuous": round(cont_corr, 4),
        "ref_pair_correlation": 0.1693,
        "ref_role_correlation": 0.37,
        "top20_train_pp_mean": round(float(np.mean([r["train_pp"] for r in top])), 2),
        "top20_test_pp_mean": round(float(np.mean([r["test_pp"] for r in top])), 2),
        "bottom20_train_pp_mean": round(float(np.mean([r["train_pp"] for r in bot])), 2),
        "bottom20_test_pp_mean": round(float(np.mean([r["test_pp"] for r in bot])), 2),
        "thresholds": {a: round(v, 4) for a, v in thresholds.items()},
    }

    # Concrete interpretable cells: an engage tank, a hypercarry ADC, a sustain enchanter.
    named = {}
    name_to_idx = {names.get(idx_to_cid[i], "").lower(): i for i in range(n_champs)}
    for who in ("malphite", "jinx", "soraka", "kaisa", "amumu"):
        idx = name_to_idx.get(who)
        if idx is None:
            named[who] = "not in vocab"
            continue
        named[who] = {
            "train": champ_archetype_drill(splits.train, res_train, idx_to_cid, cap_pct, thresholds, idx),
            "test": champ_archetype_drill(splits.test, res_test, idx_to_cid, cap_pct, thresholds, idx),
        }

    result = {**summary, "top15_cells": rows_sorted[:15], "named_cells": named}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False,
                              default=lambda o: round(o, 2) if isinstance(o, float) else o), encoding="utf-8")

    print(f"\nqualified champion x archetype cells (>= {min_games} both): {len(rows)}")
    print(f"avg cell co-occurrence (train): {summary['avg_cell_cooccurrence_train']:.0f}")
    print(f"corr(train,test) binary   = {corr:+.4f}")
    print(f"corr(train,test) contin-wt = {cont_corr:+.4f}")
    print(f"   reference:  champion x champion = +0.1693   |   champion x ROLE = +0.37")
    verdict = ("GO  (~role-level signal)" if corr >= 0.30 else
               "MAYBE (between pair noise and role signal)" if corr >= 0.22 else
               "NO-GO (pair-noise tier -- do not ship un-pooled)")
    print(f"   verdict: {verdict}")
    print(f"top-20 train {summary['top20_train_pp_mean']:+.2f}pp -> test {summary['top20_test_pp_mean']:+.2f}pp"
          f"   |   bot-20 train {summary['bottom20_train_pp_mean']:+.2f}pp -> test {summary['bottom20_test_pp_mean']:+.2f}pp")
    print(f"\n{'champ x archetype':<32}{'train':>9}{'n_tr':>8}{'test':>9}{'n_te':>8}")
    for r in rows_sorted[:15]:
        label = f"{r['champ']} x {r['archetype']}"
        print(f"{label[:30]:<32}{r['train_pp']:>+8.2f}%{r['train_games']:>8}{r['test_pp']:>+8.2f}%{r['test_games']:>8}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
