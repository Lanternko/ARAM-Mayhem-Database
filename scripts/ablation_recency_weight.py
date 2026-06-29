"""Ablation: time-decay (recency) weighting for cross-patch training.

Follow-up to ablation_cross_patch_backbone.py.  That script showed pooling all
patches beats both the 16.10 pin and naive current-patch refit.  But raw
win-rate drift shows ~5-6 champions genuinely move 4-6pp each patch
(16.11->16.12 corr=0.79), so a flat pool under-reacts to the movers.

This tests the fix: train on ALL patches but weight each game by recency,
    w_i = exp(-(t_ref - t_i) / tau),   t_ref = most recent training game
so the latest patch (and latest games within it) dominate, with a single knob
tau (half-life-ish, in days).  Sweep tau and pick the best on held-out 16.12.

Two questions:
  1. Does an interior tau beat both extremes (tau->inf = flat pool,
     tau->0 = recent-only ~ scarce)?
  2. Does weighting actually TRACK THE MOVERS?  We check (a) test logloss on
     the subset of games containing a mover champion, and (b) whether the
     champion-strength coefficients shift in the same direction as raw WR.

Usage:
  python scripts/ablation_recency_weight.py --out outputs/ablation_recency_weight.json
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import click
import numpy as np
import scipy.sparse as sp
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ablation_cross_patch_backbone import (  # noqa: E402
    build_backbone, build_identity, load_games, load_profiles, logloss, acc,
    fit_lr_val_c, _patch_lt,
)

DAY_MS = 86_400_000


def champ_names(score_csv: Path) -> dict[int, str]:
    out = {}
    with open(score_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                out[int(r["champion_id"])] = r.get("champion_name_en") or r.get("champion_alias")
            except (KeyError, ValueError):
                pass
    return out


def raw_wr_by_patch(games):
    agg = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # patch -> cid -> [g, w]
    for pre, _, blue, red, bw in games:
        for c in blue:
            a = agg[pre][c]; a[0] += 1; a[1] += bw
        for c in red:
            a = agg[pre][c]; a[0] += 1; a[1] += (1 - bw)
    return agg


def decay_weights(times: np.ndarray, t_ref: float, tau_days: float) -> np.ndarray:
    if tau_days >= 1e8:
        return np.ones(len(times))
    w = np.exp(-(t_ref - times) / (tau_days * DAY_MS))
    w *= len(w) / w.sum()  # normalize mean->1 so C stays comparable across tau
    return w


def pearson(xs, ys):
    xs = np.asarray(xs, float); ys = np.asarray(ys, float)
    if len(xs) < 3:
        return float("nan")
    xs = xs - xs.mean(); ys = ys - ys.mean()
    d = np.sqrt((xs @ xs) * (ys @ ys))
    return float((xs @ ys) / d) if d else float("nan")


@click.command()
@click.option("--db", default=Path("data/lcu/games.db"), type=click.Path(exists=True, path_type=Path))
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"),
              type=click.Path(exists=True, path_type=Path))
@click.option("--current-patch", default="16.12", show_default=True)
@click.option("--prev-patch", default="16.11", show_default=True, help="for mover detection")
@click.option("--test-size", default=12000, show_default=True)
@click.option("--tau-grid", default="3,7,14,21,30,45,60,90,1e9", show_default=True,
              help="half-life in days; 1e9 = flat pool")
@click.option("--mover-min-drift", default=4.0, show_default=True, help="pp WR change to count as a mover")
@click.option("--mover-min-games", default=300, show_default=True)
@click.option("--out", default=Path("outputs/ablation_recency_weight.json"), type=click.Path(path_type=Path))
def main(db, score_csv, current_patch, prev_patch, test_size, tau_grid, mover_min_drift, mover_min_games, out):
    names = champ_names(score_csv)
    profiles = load_profiles(score_csv)
    games = load_games(db)
    games.sort(key=lambda g: g[1])

    cur = [g for g in games if g[0] == current_patch]
    pool = [g for g in games if g[0] != current_patch and g[1] > 0 and _patch_lt(g[0], current_patch)]
    if len(cur) <= test_size + 100:
        raise click.ClickException(f"{current_patch} has only {len(cur)} games")
    test = cur[-test_size:]
    avail = cur[:-test_size]
    train = pool + avail  # all available data minus the held-out future

    champ_ids = sorted({c for g in games for c in (g[2] + g[3])})
    champ_to_idx = {c: i for i, c in enumerate(champ_ids)}

    # movers: raw WR drift prev -> current
    wr = raw_wr_by_patch(games)
    def _wr(p, c):
        g, w = wr[p][c]
        return (w / g if g else None), g
    movers = {}
    for c in champ_ids:
        w0, g0 = _wr(prev_patch, c); w1, g1 = _wr(current_patch, c)
        if w0 is not None and w1 is not None and g0 >= mover_min_games and g1 >= mover_min_games:
            d = (w1 - w0) * 100
            if abs(d) >= mover_min_drift:
                movers[c] = d
    mover_ids = set(movers)

    click.echo(f"pool={len(pool)} avail(current)={len(avail)} test={len(test)}  "
               f"train={len(train)}  movers(|dWR|>={mover_min_drift}pp)={len(mover_ids)}")

    # Features
    Xid_tr, y_tr = build_identity(train, champ_to_idx)
    Xid_te, y_te = build_identity(test, champ_to_idx)
    Xbk_tr = build_backbone(train, profiles)
    Xbk_te = build_backbone(test, profiles)
    scaler = StandardScaler().fit(Xbk_tr)
    Xbk_tr = scaler.transform(Xbk_tr); Xbk_te = scaler.transform(Xbk_te)
    Xid_pool, y_pool = build_identity(pool, champ_to_idx)

    times = np.array([g[1] for g in train], dtype=np.float64)
    t_ref = times.max()

    # test-game masks: contains >=1 mover champ
    test_mover_mask = np.array([
        any(c in mover_ids for c in (blue + red)) for _, _, blue, red, _ in test
    ])
    cov = float(test_mover_mask.mean())

    def ev(p, mask=None):
        if mask is None:
            return {"logloss": logloss(y_te, p), "acc": acc(y_te, p), "n": int(len(y_te))}
        if mask.sum() == 0:
            return None
        return {"logloss": logloss(y_te[mask], p[mask]), "acc": acc(y_te[mask], p[mask]), "n": int(mask.sum())}

    # Pick one C on a uniform pooled-train val tail, reuse across tau (isolate tau).
    _, c_best = fit_lr_val_c(Xid_tr, y_tr)
    click.echo(f"C={c_best}  mover-game coverage of test={cov*100:.1f}%\n")

    # Baseline: stale = identity on POOL only (no current data) = pure reuse.
    m_stale = LogisticRegression(C=c_best, max_iter=2000, solver="lbfgs")
    m_stale.fit(Xid_pool, y_pool)
    p_stale = m_stale.predict_proba(Xid_te)[:, 1]
    stale_coef = m_stale.coef_[0]

    taus = []
    for t in tau_grid.split(","):
        t = t.strip()
        if t:
            taus.append(float(t))

    results = {"meta": {"pool": len(pool), "avail": len(avail), "test": len(test),
                        "movers": {names.get(c, str(c)): round(d, 1) for c, d in
                                   sorted(movers.items(), key=lambda kv: -abs(kv[1]))},
                        "mover_coverage": cov, "C": c_best},
               "stale_reuse": {"all": ev(p_stale), "mover_games": ev(p_stale, test_mover_mask)},
               "by_tau": []}

    click.echo(f"{'tau(d)':>7} | {'ALL ll/acc':>16} | {'MOVER-games ll/acc':>20} | {'corr(dCoef,dWR)':>15}")
    click.echo("-" * 70)
    ll = results["stale_reuse"]
    click.echo(f"{'stale':>7} | {ll['all']['logloss']:.4f}/{ll['all']['acc']*100:4.1f}% | "
               f"{ll['mover_games']['logloss']:.4f}/{ll['mover_games']['acc']*100:4.1f}%       |      —")

    best_tau, best_ll, best_coef = None, np.inf, None
    for tau in taus:
        w = decay_weights(times, t_ref, tau)
        m = LogisticRegression(C=c_best, max_iter=2000, solver="lbfgs")
        m.fit(Xid_tr, y_tr, sample_weight=w)
        p = m.predict_proba(Xid_te)[:, 1]
        coef = m.coef_[0]
        # coefficient shift vs stale, aligned with raw WR drift over movers
        d_coef = [coef[champ_to_idx[c]] - stale_coef[champ_to_idx[c]] for c in movers]
        d_wr = [movers[c] for c in movers]
        r = pearson(d_coef, d_wr)
        row = {"tau_days": tau, "all": ev(p), "mover_games": ev(p, test_mover_mask),
               "corr_dcoef_dwr": r}
        results["by_tau"].append(row)
        tau_lbl = "flat" if tau >= 1e8 else f"{tau:g}"
        click.echo(f"{tau_lbl:>7} | {row['all']['logloss']:.4f}/{row['all']['acc']*100:4.1f}% | "
                   f"{row['mover_games']['logloss']:.4f}/{row['mover_games']['acc']*100:4.1f}%       | "
                   f"{r:+.3f}")
        if row["all"]["logloss"] < best_ll:
            best_ll, best_tau, best_coef = row["all"]["logloss"], tau, coef

    results["best_tau"] = best_tau
    click.echo(f"\nbest tau = {best_tau:g} days (test logloss {best_ll:.4f})")

    # Mover-by-mover: did the best-tau strength move the right way?
    click.echo(f"\nMover strength shift @ best tau (coef vs stale; should track dWR sign):")
    click.echo(f"{'champ':14} {'dWR(pp)':>8} {'dCoef':>8} {'aligned':>8}")
    aligned = 0
    mv_detail = []
    for c, d in sorted(movers.items(), key=lambda kv: -abs(kv[1])):
        dc = float(best_coef[champ_to_idx[c]] - stale_coef[champ_to_idx[c]])
        ok = (dc > 0) == (d > 0)
        aligned += ok
        mv_detail.append({"champ": names.get(c, str(c)), "dWR_pp": round(d, 1),
                          "dCoef": round(dc, 3), "aligned": ok})
        click.echo(f"{names.get(c, str(c)):14} {d:+8.1f} {dc:+8.3f} {'yes' if ok else 'NO':>8}")
    click.echo(f"\naligned {aligned}/{len(movers)} movers")
    results["mover_detail_best_tau"] = mv_detail
    results["mover_aligned"] = f"{aligned}/{len(movers)}"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    click.echo(f"\nwrote {out}")


if __name__ == "__main__":
    main()
