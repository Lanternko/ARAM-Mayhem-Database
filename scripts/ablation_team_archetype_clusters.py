"""Q2 of the comp-fit thread: data-driven TEAM-ARCHETYPE clustering.

Q1 asked whether the models differ (ablation_recommender_backtest.py).
Q3 asked whether champion x team-archetype interaction persists out-of-sample
(ablation_champ_archetype_persistence.py) -- but it LABELED teams with the 6
hand-weighted COMP_FIT_DEFS + a quantile cut, taken on faith ("user decision
2026-06-17: reuse the existing hand-tuned axes").  Q2 closes that gap: instead
of trusting the 6 hand blends, CLUSTER the real Mayhem teams on their own
capability vectors and ask three things --

  1. Is there real cluster structure, or are team comps a continuum?
     (silhouette across k)
  2. If we force k = (number of hand archetypes), do the data clusters line up
     with the 6 hand archetypes?  (adjusted Rand / NMI between cluster labels
     and the hand-argmax labels)
  3. Does archetype membership relate to winning -- raw cluster win-rate AND,
     controlling for champion identity, the per-cluster residual (the same
     y - p_hat trick Q3 uses, so "archetype wins" is separated from "this
     cluster happens to hold strong champions").

A team is the mean of its FIVE members' percentile-ranked capability vectors
(Q3 used the 4-others to avoid self-crediting the anchor; here we classify the
team as a whole, so all five count).  The capability vectors and the percentile
norm are imported verbatim from the Q3 script so the offline clusters use the
exact same axes the online radar ships.

Leakage: KMeans centroids and the Champion-LR are fit on TRAIN only; cluster
win-rate / residual are reported on the held-out split.  The champ.comp vectors
themselves are the shipped global payload (same choice Q3 made) -- acceptable
for a descriptive archetype study, noted here so it isn't mistaken for a
predictive leak.

Reference (same data/split, from the role/archetype ablations):
    champion x champion pair  : r ~ +0.17 (noise)
    champion x teammate-ROLE  : r ~ +0.37 (signal -> shipped)
    champion x ARCHETYPE      : r ~ +0.47 in-split / +0.38 cross-patch (Q3 GO)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import click
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import (
    adjusted_rand_score,
    normalized_mutual_info_score,
    silhouette_score,
)
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_composition_signals import champion_matrix  # noqa: E402
from train_ability_nn import load_split_data  # noqa: E402
from ablation_champ_archetype_persistence import (  # noqa: E402
    ARCHETYPE_LABELS,
    COMP_FIT_DEFS,
    COMP_STAT_KEYS,
    build_cap_pct,
    fit_champion_lr,
    load_comp_vectors,
    load_cross_patch_split,
)


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """95% Wilson interval for a binomial proportion."""
    if n == 0:
        return (None, None)
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def build_vec_table(idx_to_cid, cap_pct, n_champs):
    """(n_champs x 9) percentile-ranked capability vectors indexed by vocab idx.
    Champions absent from the payload get a zero row (and are counted)."""
    tbl = np.zeros((n_champs, len(COMP_STAT_KEYS)), dtype=np.float64)
    missing = 0
    for idx in range(n_champs):
        comp = cap_pct.get(idx_to_cid[idx])
        if comp is None:
            missing += 1
            continue
        tbl[idx] = [comp[k] for k in COMP_STAT_KEYS]
    return tbl, missing


def team_matrix(dataset, vec_tbl):
    """Pool both teams of every game into team-level samples.

    Returns (X, won, gidx, sign, cids_idx) where each row is one 5-champion team:
      X        (2N x 9)  mean percentile capability vector of the 5 members
      won      (2N,)     did this team win the game
      gidx     (2N,)     index of the source game row (to look up its residual)
      sign     (2N,)     +1 for the blue team, -1 for the red team
      cids_idx (2N x 5)  member vocab indices (for representative-champion lift)
    """
    blue = np.asarray(dataset.blue, dtype=np.int64)  # N x 5
    red = np.asarray(dataset.red, dtype=np.int64)
    y = np.asarray(dataset.labels, dtype=np.float64)  # 1.0 = blue won
    n = len(y)
    X = np.vstack([vec_tbl[blue].mean(axis=1), vec_tbl[red].mean(axis=1)])
    won = np.concatenate([y == 1.0, y == 0.0]).astype(bool)
    gidx = np.concatenate([np.arange(n), np.arange(n)])
    sign = np.concatenate([np.ones(n), -np.ones(n)])
    cids_idx = np.vstack([blue, red])
    return X, won, gidx, sign, cids_idx


def hand_labels(X):
    """argmax of the 6 hand blends on team mean percentile vectors.
    Returns (label_idx (n,), score_matrix (n x 6), archetype_order)."""
    keys = list(COMP_FIT_DEFS)
    kidx = {k: i for i, k in enumerate(COMP_STAT_KEYS)}
    scores = np.zeros((X.shape[0], len(keys)))
    for j, a in enumerate(keys):
        for stat, w in COMP_FIT_DEFS[a].items():
            scores[:, j] += w * X[:, kidx[stat]]
    return scores.argmax(axis=1), scores, keys


def centroid_hand_blend(centroid_pct):
    """Score one centroid (9-dim percentile space) under each hand blend."""
    kidx = {k: i for i, k in enumerate(COMP_STAT_KEYS)}
    return {
        a: float(sum(w * centroid_pct[kidx[s]] for s, w in wts.items()))
        for a, wts in COMP_FIT_DEFS.items()
    }


def rep_champions(labels, cids_idx, k, idx_to_cid, names, top=8, min_app=30):
    """Champions over-represented in a cluster by lift = P(champ|cluster)/P(champ)."""
    n_teams = len(labels)
    overall = defaultdict(int)
    per_cluster = [defaultdict(int) for _ in range(k)]
    cluster_size = np.bincount(labels, minlength=k)
    for t in range(n_teams):
        lab = labels[t]
        for ci in cids_idx[t]:
            overall[ci] += 1
            per_cluster[lab][int(ci)] += 1
    out = []
    for c in range(k):
        size = int(cluster_size[c])
        rows = []
        for ci, cnt in per_cluster[c].items():
            if cnt < min_app or size == 0:
                continue
            p_in = cnt / size
            p_all = overall[ci] / n_teams
            lift = p_in / p_all if p_all > 0 else 0.0
            rows.append((lift, cnt, ci))
        rows.sort(reverse=True)
        out.append([
            {"champ": names.get(idx_to_cid[ci], str(idx_to_cid[ci])), "lift": round(l, 2), "n": int(cnt)}
            for l, cnt, ci in rows[:top]
        ])
    return out


@click.command()
@click.option("--data", default=Path("data/raw/mayhem_lcu_ml_compare_2026_05_25_live.parquet"),
              type=click.Path(exists=True, path_type=Path), show_default=True,
              help="Within-patch parquet (default). For --cross-patch pass the pooled file.")
@click.option("--patch-prefix", default="16.10", show_default=True,
              help="Within-patch mode only: the single patch to time-split.")
@click.option("--cross-patch", is_flag=True, default=False,
              help="Walk-forward: train clusters on --train-patches, score on --test-patch.")
@click.option("--train-patches", default="16.10,16.11", show_default=True)
@click.option("--test-patch", default="16.12", show_default=True)
@click.option("--payload", default=Path("docs/api/tier-list.json"),
              type=click.Path(exists=True, path_type=Path), show_default=True,
              help="Shipped payload; champ.comp vectors are read from here.")
@click.option("--k-min", default=2, show_default=True)
@click.option("--k-max", default=10, show_default=True)
@click.option("--k-detail", default=len(COMP_FIT_DEFS), show_default=True,
              help="Cluster count for the deep-dive vs hand archetypes (default = #hand archetypes).")
@click.option("--silhouette-sample", default=8000, show_default=True,
              help="Sample size for the O(n^2) silhouette score.")
@click.option("--random-state", default=0, show_default=True)
@click.option("--out", default=Path("outputs/ablation_team_archetype_clusters.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, patch_prefix, cross_patch, train_patches, test_patch, payload,
         k_min, k_max, k_detail, silhouette_sample, random_state, out):
    rng = np.random.default_rng(random_state)

    print("[1/6] loading champ.comp vectors + percentile norm ...", flush=True)
    comp_by_cid = load_comp_vectors(payload)
    cap_pct = build_cap_pct(comp_by_cid)

    if cross_patch:
        tp = [p.strip() for p in train_patches.split(",") if p.strip()]
        print(f"[2/6] loading CROSS-PATCH split: train {tp} -> test {test_patch} ...", flush=True)
        splits = load_cross_patch_split(data, tp, test_patch)
        split_desc = f"cross-patch train={'+'.join(tp)} test={test_patch}"
    else:
        print(f"[2/6] loading within-patch time split ({patch_prefix}) ...", flush=True)
        splits = load_split_data(data, patch_prefix)
        split_desc = f"within-patch {patch_prefix} time-split"

    n_champs = len(splits.champ_to_idx)
    idx_to_cid = {idx: cid for cid, idx in splits.champ_to_idx.items()}
    names = {int(c): (info or {}).get("alias") or (info or {}).get("name") or str(c)
             for c, info in json.loads(Path(payload).read_text(encoding="utf-8")).get("champs", {}).items()}
    vec_tbl, missing = build_vec_table(idx_to_cid, cap_pct, n_champs)
    if missing:
        print(f"      warn: {missing} champs in vocab lack comp vectors (zero rows)")

    print("[3/6] building pooled team capability matrices ...", flush=True)
    Xtr, won_tr, g_tr, s_tr, cids_tr = team_matrix(splits.train, vec_tbl)
    Xte, won_te, g_te, s_te, cids_te = team_matrix(splits.test, vec_tbl)

    print("[4/6] champion-identity residual (train-fit LR) ...", flush=True)
    y_tr = np.asarray(splits.train.labels, dtype=np.float64)
    y_val = np.asarray(splits.val.labels, dtype=np.float64)
    y_te = np.asarray(splits.test.labels, dtype=np.float64)
    lr = fit_champion_lr(champion_matrix(splits.train, n_champs), y_tr,
                         champion_matrix(splits.val, n_champs), y_val)
    res_tr_game = y_tr - lr.predict_proba(champion_matrix(splits.train, n_champs))[:, 1]
    res_te_game = y_te - lr.predict_proba(champion_matrix(splits.test, n_champs))[:, 1]
    res_tr_team = res_tr_game[g_tr] * s_tr
    res_te_team = res_te_game[g_te] * s_te

    scaler = StandardScaler().fit(Xtr)
    Ztr, Zte = scaler.transform(Xtr), scaler.transform(Xte)

    print(f"[5/6] silhouette sweep k={k_min}..{k_max} on {len(Ztr):,} train teams ...", flush=True)
    sample = rng.choice(len(Ztr), size=min(silhouette_sample, len(Ztr)), replace=False)
    sweep = []
    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, random_state=random_state, n_init=4, max_iter=200).fit(Ztr)
        sil = float(silhouette_score(Ztr[sample], km.labels_[sample]))
        sweep.append({"k": k, "silhouette": round(sil, 4), "inertia": round(float(km.inertia_), 1)})
        print(f"      k={k:2d}  silhouette={sil:+.4f}  inertia={km.inertia_:,.0f}", flush=True)
    best = max(sweep, key=lambda r: r["silhouette"])

    print(f"[6/6] detail clustering at k={k_detail} vs {len(COMP_FIT_DEFS)} hand archetypes ...", flush=True)
    km_d = KMeans(n_clusters=k_detail, random_state=random_state, n_init=10, max_iter=300).fit(Ztr)
    lab_tr = km_d.labels_
    lab_te = km_d.predict(Zte)
    centroids_pct = scaler.inverse_transform(km_d.cluster_centers_)  # k x 9 percentile space

    hand_lab_tr, _hand_scores, hand_keys = hand_labels(Xtr)
    ari = float(adjusted_rand_score(hand_lab_tr, lab_tr))
    nmi = float(normalized_mutual_info_score(hand_lab_tr, lab_tr))
    hand_dist = {hand_keys[j]: int((hand_lab_tr == j).sum()) for j in range(len(hand_keys))}
    reps = rep_champions(lab_tr, cids_tr, k_detail, idx_to_cid, names)

    clusters = []
    for c in range(k_detail):
        m_tr = lab_tr == c
        m_te = lab_te == c
        cen = centroids_pct[c]
        blend = centroid_hand_blend(cen)
        nearest = max(blend, key=blend.get)
        n_te = int(m_te.sum())
        wins_te = int(won_te[m_te].sum())
        wr = wins_te / n_te if n_te else None
        lo, hi = wilson_ci(wins_te, n_te)
        clusters.append({
            "id": c,
            "size_train": int(m_tr.sum()),
            "size_frac_train": round(float(m_tr.mean()), 4),
            "centroid_pct": {k_: round(float(cen[i]), 3) for i, k_ in enumerate(COMP_STAT_KEYS)},
            "top_dims": [k_ for k_, _ in sorted(zip(COMP_STAT_KEYS, cen), key=lambda kv: kv[1], reverse=True)[:3]],
            "nearest_hand_archetype": nearest,
            "nearest_hand_label": ARCHETYPE_LABELS[nearest],
            "hand_blend_scores": {a: round(v, 3) for a, v in blend.items()},
            "test_n": n_te,
            "test_win_rate": round(wr, 4) if wr is not None else None,
            "test_wr_ci95": [round(lo, 4), round(hi, 4)] if n_te else None,
            "test_residual_pp": round(float(res_te_team[m_te].mean()) * 100, 3) if n_te else None,
            "train_residual_pp": round(float(res_tr_team[m_tr].mean()) * 100, 3) if m_tr.sum() else None,
            "rep_champions": reps[c],
        })

    matched_hand = sorted({c["nearest_hand_archetype"] for c in clusters})
    res_vals = [c["test_residual_pp"] for c in clusters if c["test_residual_pp"] is not None]
    wr_vals = [c["test_win_rate"] for c in clusters if c["test_win_rate"] is not None]
    spread_resid = round(max(res_vals) - min(res_vals), 3) if res_vals else None
    spread_wr = round((max(wr_vals) - min(wr_vals)) * 100, 3) if wr_vals else None

    structure = ("clear clusters" if best["silhouette"] >= 0.50 else
                 "weak clusters" if best["silhouette"] >= 0.25 else
                 "continuum (no clean clusters)")
    alignment = ("strong (clusters ~= hand axes)" if ari >= 0.50 else
                 "moderate" if ari >= 0.25 else
                 "weak (data clusters disagree with hand axes)")

    result = {
        "split": split_desc,
        "n_train_teams": int(len(Ztr)),
        "n_test_teams": int(len(Zte)),
        "n_hand_archetypes": len(COMP_FIT_DEFS),
        "k_detail": k_detail,
        "best_silhouette_k": best["k"],
        "best_silhouette": best["silhouette"],
        "k_sweep": sweep,
        "k6_vs_hand_adjusted_rand": round(ari, 4),
        "k6_vs_hand_nmi": round(nmi, 4),
        "hand_archetypes_matched_by_a_cluster": matched_hand,
        "n_distinct_hand_archetypes_matched": len(matched_hand),
        "hand_argmax_distribution_train": hand_dist,
        "test_win_rate_spread_pp": spread_wr,
        "test_residual_spread_pp": spread_resid,
        "structure_verdict": structure,
        "alignment_verdict": alignment,
        "clusters": clusters,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    # ----- console summary -----
    print(f"\n===== Q2 team-archetype clustering ({split_desc}) =====")
    print(f"teams: {len(Ztr):,} train / {len(Zte):,} test")
    print(f"structure: best silhouette {best['silhouette']:+.4f} @ k={best['k']}  -> {structure}")
    print(f"alignment @ k={k_detail}: ARI {ari:+.4f}  NMI {nmi:+.4f}  -> {alignment}")
    print(f"  distinct hand archetypes a cluster maps to: {len(matched_hand)}/{len(COMP_FIT_DEFS)}  {matched_hand}")
    print(f"  win-rate spread across clusters: {spread_wr:+.2f}pp   residual spread: {spread_resid:+.2f}pp")
    print(f"\n{'cluster -> nearest hand':<30}{'size%':>7}{'top dims':>26}{'test WR':>9}{'resid':>8}")
    for c in sorted(clusters, key=lambda d: (d["test_residual_pp"] is not None, d["test_residual_pp"]), reverse=True):
        wr = f"{c['test_win_rate'] * 100:.1f}%" if c["test_win_rate"] is not None else "--"
        rp = f"{c['test_residual_pp']:+.2f}" if c["test_residual_pp"] is not None else "--"
        label = f"{c['id']}: {c['nearest_hand_archetype']}"
        print(f"{label:<30}{c['size_frac_train'] * 100:>6.1f}%{','.join(c['top_dims']):>26}{wr:>9}{rp:>8}")
        top3 = ", ".join(f"{r['champ']}({r['lift']:.1f})" for r in c["rep_champions"][:4])
        print(f"      reps: {top3}")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
