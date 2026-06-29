"""Ablation: does a cross-version characteristic backbone help when the
CURRENT patch has little data?

Motivation
----------
The recommender splits into two conceptual axes:
  * champion *strength*  -> version-volatile, learned from win/loss (champ identity)
  * champion *kit*       -> CC / tankiness / damage / role, derived from ability
                            TEXT (cross-version by construction, never touches W/L)

Every prior composition ablation was run inside a single data-rich patch
(16.10, 215k train), where champion identity is a near-sufficient statistic and
the additive kit features look redundant (+0.0pp).  That conclusion is true
*in-distribution* but says nothing about the scenario we actually care about:
a brand-new patch where identity is barely estimable.

This script tests the new-patch regime directly.  Protocol:
  POOL      = every game on patches strictly before 16.12  (lots of data)
  EARLY(K)  = first K games of 16.12, time-sorted          (the scarce trickle)
  TEST      = last `--test-size` games of 16.12, held out   (the future)
All models are scored on the SAME 16.12 TEST window, swept over K.

Models
------
  ident_stale     identity-only, trained on POOL                (stale strength)
  ident_scarce    identity-only, trained on EARLY(K)            (naive per-patch)
  ident_pooled    identity-only, trained on POOL + EARLY(K)
  backbone_only   kit/role/non-additive, trained on POOL        (no roster knowledge)
  backbone+ident  PROPOSAL: backbone(POOL) frozen as an offset,
                  + identity residual fit on EARLY(K) with L2 shrinkage

Hypothesis: at small K, backbone_only / backbone+ident beat ident_scarce
(noise) and stay competitive with ident_stale; as K grows, the identity
models catch up — reproducing "identity is near-sufficient" only in the limit.

Features are 100% static (text-derived scores + static roles + a build-based AD
proxy).  The LCU games.db stores only championId/teamId/augments, so empirical
combat stats are intentionally not used — which is exactly the point: the
backbone must transfer on kit knowledge alone.

Usage:
  python scripts/ablation_cross_patch_backbone.py \
      --db data/lcu/games.db --out outputs/ablation_cross_patch_backbone.json
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click
import numpy as np
import scipy.sparse as sp
from scipy.optimize import minimize
from scipy.special import expit, log_expit
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from champion_roles import ROLE_ORDER, role_tags_for_alias  # noqa: E402

SCORE_COLUMNS = (
    "wave_clear_score", "cc_score", "engage_score", "damage_score",
    "poke_score", "sustain_score", "frontline_score",
)
LACK_THRESHOLDS = {
    "wave_clear_score": 3.0, "cc_score": 3.0, "engage_score": 2.2,
    "damage_score": 5.5, "poke_score": 2.0, "sustain_score": 1.5,
    "frontline_score": 1.8,
}
AD_BIN_EDGES = (0.35, 0.45, 0.55, 0.65)  # -> 5 bins
N_AD_BINS = len(AD_BIN_EDGES) + 1
C_GRID = (0.03, 0.1, 0.3, 1.0)
LAM_GRID = (0.3, 1.0, 3.0, 10.0, 30.0)  # L2 strength for the shrunk identity residual


# ----------------------------- static profiles -----------------------------

class Champ:
    __slots__ = ("scores", "role_vec", "ad")

    def __init__(self, scores, role_vec, ad):
        self.scores = scores      # np.ndarray[7]
        self.role_vec = role_vec  # np.ndarray[6]
        self.ad = ad              # float in [0,1]


def load_profiles(score_csv: Path) -> dict[int, Champ]:
    import csv
    out: dict[int, Champ] = {}
    with open(score_csv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                cid = int(row["champion_id"])
            except (KeyError, ValueError):
                continue
            scores = np.array([float(row.get(c, 0.0) or 0.0) for c in SCORE_COLUMNS], dtype=np.float64)
            alias = row.get("champion_alias") or ""
            tags = [t for t in (row.get("tags") or "").replace("|", ",").split(",") if t]
            roles = set(role_tags_for_alias(alias, tags))
            role_vec = np.array([1.0 if r in roles else 0.0 for r in ROLE_ORDER], dtype=np.float64)
            ap = float(row.get("build_ap") or 0.0)
            bonus_ad = float(row.get("build_bonus_ad") or 0.0)
            ad = bonus_ad / (bonus_ad + ap) if (bonus_ad + ap) > 0 else 0.5
            out[cid] = Champ(scores, role_vec, ad)
    return out


def _ad_bin(ad_share: float) -> int:
    for i, edge in enumerate(AD_BIN_EDGES):
        if ad_share < edge:
            return i
    return N_AD_BINS - 1


# ----------------------------- data loading ---------------------------------

def patch_prefix(patch: str) -> str:
    return ".".join((patch or "").split(".")[:2])


def load_games(db: Path):
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT patch, created_ms, blue_champs, red_champs, blue_wins "
        "FROM games WHERE queue_id=2400 AND blue_champs IS NOT NULL "
        "AND red_champs IS NOT NULL AND blue_wins IS NOT NULL"
    ).fetchall()
    con.close()
    out = []
    for patch, created_ms, b, r, bw in rows:
        try:
            blue = [int(x) for x in json.loads(b)]
            red = [int(x) for x in json.loads(r)]
        except Exception:
            continue
        if len(blue) != 5 or len(red) != 5:
            continue
        out.append((patch_prefix(patch), int(created_ms or 0), blue, red, int(bw)))
    return out


# ----------------------------- feature builders -----------------------------

def build_identity(games, champ_to_idx):
    """Sparse signed champion-identity matrix: +1 blue, -1 red."""
    n = len(games)
    nc = len(champ_to_idx)
    rows, cols, data = [], [], []
    y = np.empty(n, dtype=np.float64)
    for i, (_, _, blue, red, bw) in enumerate(games):
        for cid in blue:
            j = champ_to_idx.get(cid)
            if j is not None:
                rows.append(i); cols.append(j); data.append(1.0)
        for cid in red:
            j = champ_to_idx.get(cid)
            if j is not None:
                rows.append(i); cols.append(j); data.append(-1.0)
        y[i] = bw
    X = sp.csr_matrix((data, (rows, cols)), shape=(n, nc), dtype=np.float64)
    return X, y


def _team_back_features(ids, profiles):
    score_sum = np.zeros(len(SCORE_COLUMNS))
    role_sum = np.zeros(len(ROLE_ORDER))
    ad_vals = []
    for cid in ids:
        p = profiles.get(cid)
        if p is None:
            continue
        score_sum += p.scores
        role_sum += p.role_vec
        ad_vals.append(p.ad)
    lacks = np.array([1.0 if score_sum[k] < LACK_THRESHOLDS[c] else 0.0
                      for k, c in enumerate(SCORE_COLUMNS)])
    ad_share = float(np.mean(ad_vals)) if ad_vals else 0.5
    role_ad = np.zeros(len(ROLE_ORDER) * N_AD_BINS)
    b = _ad_bin(ad_share)
    role_ad[b * len(ROLE_ORDER):(b + 1) * len(ROLE_ORDER)] = role_sum
    # blocks: score_sum(7) role_sum(6) lacks(7) ad_share(1) role_ad(30)
    return np.concatenate([score_sum, role_sum, lacks, [ad_share], role_ad])


def build_backbone(games, profiles):
    """Dense signed backbone matrix (blue features - red features)."""
    feats = []
    for _, _, blue, red, _ in games:
        feats.append(_team_back_features(blue, profiles) - _team_back_features(red, profiles))
    return np.asarray(feats, dtype=np.float64)


# ----------------------------- fitting / scoring -----------------------------

def logloss(y, p):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def acc(y, p):
    return float(np.mean((p >= 0.5) == (y >= 0.5)))


def _time_val_split(n, frac=0.15):
    cut = int(n * (1 - frac))
    return slice(0, cut), slice(cut, n)


def fit_lr_val_c(X, y, c_grid=C_GRID):
    """Fit LogisticRegression, pick C on a time-based val tail. X already time-sorted."""
    n = X.shape[0]
    if n < 200:
        c_grid = (0.1,)
    tr, va = _time_val_split(n)
    Xtr = X[tr]; ytr = y[tr]; Xva = X[va]; yva = y[va]
    best, best_ll = None, np.inf
    for c in c_grid:
        m = LogisticRegression(C=c, max_iter=2000, solver="lbfgs")
        m.fit(Xtr, ytr)
        ll = logloss(yva, m.predict_proba(Xva)[:, 1])
        if ll < best_ll:
            best_ll, best = ll, c
    model = LogisticRegression(C=best, max_iter=2000, solver="lbfgs")
    model.fit(X, y)
    return model, best


def fit_identity_offset_l2(Xid, y, offset, lam):
    """Logistic fit of identity residual on top of a fixed `offset` logit.

    minimize  -sum log P(y | offset + b0 + Xid@w)  + lam * ||w||^2
    (intercept b0 unpenalized).  Convex; L-BFGS with analytic gradient.
    """
    n, nc = Xid.shape
    Xcsr = Xid.tocsr()

    def nll_grad(theta):
        b0 = theta[0]
        w = theta[1:]
        z = offset + b0 + Xcsr.dot(w)
        # NLL with log-sigmoid for stability
        ll = -np.sum(y * log_expit(z) + (1 - y) * log_expit(-z))
        ll += lam * np.dot(w, w)
        p = expit(z)
        g_z = p - y
        g_w = Xcsr.T.dot(g_z) + 2.0 * lam * w
        g_b0 = np.sum(g_z)
        return ll, np.concatenate([[g_b0], g_w])

    theta0 = np.zeros(nc + 1)
    res = minimize(nll_grad, theta0, jac=True, method="L-BFGS-B",
                   options={"maxiter": 500})
    theta = res.x
    return theta[0], theta[1:]


def predict_offset_identity(Xid, b0, w, offset):
    return expit(offset + b0 + Xid.dot(w))


# ----------------------------- main experiment -------------------------------

@click.command()
@click.option("--db", default=Path("data/lcu/games.db"), type=click.Path(exists=True, path_type=Path))
@click.option("--score-csv", default=Path("data/cache/champion_semantic_scores.csv"),
              type=click.Path(exists=True, path_type=Path))
@click.option("--current-patch", default="16.12", show_default=True)
@click.option("--test-size", default=12000, show_default=True, help="held-out tail of current patch")
@click.option("--k-grid", default="0,500,1000,2000,5000,10000,20000", show_default=True)
@click.option("--max-pool", default=0, show_default=True, help="0 = use all pre-current games")
@click.option("--out", default=Path("outputs/ablation_cross_patch_backbone.json"), type=click.Path(path_type=Path))
def main(db, score_csv, current_patch, test_size, k_grid, max_pool, out):
    profiles = load_profiles(score_csv)
    games = load_games(db)
    games.sort(key=lambda g: g[1])  # by created_ms

    pool = [g for g in games if g[0] != current_patch and g[1] > 0
            and _patch_lt(g[0], current_patch)]
    cur = [g for g in games if g[0] == current_patch]
    cur.sort(key=lambda g: g[1])
    if max_pool and len(pool) > max_pool:
        pool = pool[-max_pool:]  # most recent pre-current games

    if len(cur) <= test_size + 100:
        raise click.ClickException(f"current patch {current_patch} has only {len(cur)} games")
    test = cur[-test_size:]
    avail = cur[:-test_size]  # EARLY(K) drawn from here
    ks = [int(x) for x in k_grid.split(",") if x.strip()]
    ks = [k for k in ks if k <= len(avail)]

    champ_ids = sorted({c for g in games for c in (g[2] + g[3])})
    champ_to_idx = {c: i for i, c in enumerate(champ_ids)}
    missing = sorted({c for c in champ_ids if c not in profiles})

    click.echo(f"pool={len(pool)}  current({current_patch}) avail={len(avail)} test={len(test)}  "
               f"champs={len(champ_ids)} missing_profile={len(missing)}")

    # Precompute features once per split.
    Xid_pool, y_pool = build_identity(pool, champ_to_idx)
    Xbk_pool = build_backbone(pool, profiles)
    Xid_test, y_test = build_identity(test, champ_to_idx)
    Xbk_test = build_backbone(test, profiles)
    Xid_avail, y_avail = build_identity(avail, champ_to_idx)
    Xbk_avail = build_backbone(avail, profiles)

    # Standardize backbone with a scaler fit on POOL (training side, no test leak)
    # so the L2 penalty treats score-sums and 0/1 lacks on a comparable scale.
    scaler = StandardScaler().fit(Xbk_pool)
    Xbk_pool = scaler.transform(Xbk_pool)
    Xbk_test = scaler.transform(Xbk_test)
    Xbk_avail = scaler.transform(Xbk_avail)

    base_rate = float(np.mean(y_test))
    floor_ll = logloss(y_test, np.full_like(y_test, np.mean(y_pool)))
    click.echo(f"TEST blue base rate={base_rate:.4f}  const-pool-prior logloss={floor_ll:.4f}")

    # K-independent models (fit once).
    m_stale, c_stale = fit_lr_val_c(Xid_pool, y_pool)
    p_stale = m_stale.predict_proba(Xid_test)[:, 1]

    m_back, c_back = fit_lr_val_c(Xbk_pool, y_pool)
    p_back = m_back.predict_proba(Xbk_test)[:, 1]
    # frozen backbone logit as offset on avail/test
    off_test = m_back.decision_function(Xbk_test)

    results = {
        "meta": {
            "current_patch": current_patch, "pool": len(pool), "avail": len(avail),
            "test": len(test), "base_rate": base_rate, "floor_logloss": floor_ll,
            "missing_profile_ids": missing, "c_stale": c_stale, "c_back": c_back,
        },
        "static_models": {
            "ident_stale": {"logloss": logloss(y_test, p_stale), "acc": acc(y_test, p_stale)},
            "backbone_only": {"logloss": logloss(y_test, p_back), "acc": acc(y_test, p_back)},
        },
        "by_k": [],
    }
    click.echo(f"\n[K-independent]  ident_stale  ll={results['static_models']['ident_stale']['logloss']:.4f} "
               f"acc={results['static_models']['ident_stale']['acc']:.4f}   "
               f"backbone_only ll={results['static_models']['backbone_only']['logloss']:.4f} "
               f"acc={results['static_models']['backbone_only']['acc']:.4f}")

    header = (f"{'K':>7} | {'ident_scarce':>16} | {'ident_pooled':>16} | "
              f"{'pooled_full':>16} | {'backbone+ident':>16}")
    click.echo("\n(cells = logloss / accuracy ; lower logloss is better)")
    click.echo(header)
    click.echo("-" * len(header))

    for k in ks:
        row = {"k": k}
        early = avail[:k] if k > 0 else []
        Xid_e = Xid_avail[:k]; y_e = y_avail[:k]
        Xbk_e = Xbk_avail[:k]

        # ident_scarce: identity on EARLY(K) only
        if k >= 50:
            m_s, _ = fit_lr_val_c(Xid_e, y_e)
            p = m_s.predict_proba(Xid_test)[:, 1]
            row["ident_scarce"] = {"logloss": logloss(y_test, p), "acc": acc(y_test, p)}
        else:
            row["ident_scarce"] = None

        # ident_pooled: identity on POOL + EARLY(K)
        if k > 0:
            Xid_pe = sp.vstack([Xid_pool, Xid_e]).tocsr()
            y_pe = np.concatenate([y_pool, y_e])
            m_p = LogisticRegression(C=c_stale, max_iter=2000, solver="lbfgs")
            m_p.fit(Xid_pe, y_pe)
            p = m_p.predict_proba(Xid_test)[:, 1]
            row["ident_pooled"] = {"logloss": logloss(y_test, p), "acc": acc(y_test, p)}
        else:
            row["ident_pooled"] = dict(results["static_models"]["ident_stale"])

        # pooled_full: identity (+) backbone together, trained on POOL + EARLY(K).
        # Answers "does the kit backbone add anything ON TOP OF pooled identity?"
        Xfull_pool = sp.hstack([Xid_pool, sp.csr_matrix(Xbk_pool)]).tocsr()
        Xfull_test = sp.hstack([Xid_test, sp.csr_matrix(Xbk_test)]).tocsr()
        if k > 0:
            Xfull_e = sp.hstack([Xid_e, sp.csr_matrix(Xbk_e)]).tocsr()
            Xfull_pe = sp.vstack([Xfull_pool, Xfull_e]).tocsr()
            y_pe = np.concatenate([y_pool, y_e])
        else:
            Xfull_pe, y_pe = Xfull_pool, y_pool
        m_f = LogisticRegression(C=c_stale, max_iter=2000, solver="lbfgs")
        m_f.fit(Xfull_pe, y_pe)
        p = m_f.predict_proba(Xfull_test)[:, 1]
        row["pooled_full"] = {"logloss": logloss(y_test, p), "acc": acc(y_test, p)}

        # backbone(POOL) offset + identity residual shrunk on EARLY(K)
        if k >= 50:
            off_e = m_back.decision_function(Xbk_e)
            best, best_ll, best_lam = None, np.inf, None
            tr, va = _time_val_split(k)
            for lam in LAM_GRID:
                b0, w = fit_identity_offset_l2(Xid_e[tr], y_e[tr], off_e[tr], lam)
                pva = predict_offset_identity(Xid_e[va], b0, w, off_e[va])
                ll = logloss(y_e[va], pva)
                if ll < best_ll:
                    best_ll, best, best_lam = ll, (b0, w), lam
            b0, w = fit_identity_offset_l2(Xid_e, y_e, off_e, best_lam)
            p = predict_offset_identity(Xid_test, b0, w, off_test)
            row["backbone_ident"] = {"logloss": logloss(y_test, p), "acc": acc(y_test, p), "lam": best_lam}
        else:
            # K too small to fit identity -> pure backbone
            row["backbone_ident"] = {"logloss": results["static_models"]["backbone_only"]["logloss"],
                                     "acc": results["static_models"]["backbone_only"]["acc"], "lam": None}

        def _fmt(m):
            return f"{m['logloss']:.4f}/{m['acc']*100:4.1f}%" if m else "      n/a       "
        click.echo(f"{k:>7} | {_fmt(row['ident_scarce']):>16} | {_fmt(row['ident_pooled']):>16} | "
                   f"{_fmt(row['pooled_full']):>16} | {_fmt(row['backbone_ident']):>16}")
        results["by_k"].append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    click.echo(f"\nwrote {out}")


def _patch_lt(a: str, b: str) -> bool:
    """True if patch-prefix a is strictly older than b (e.g. 16.11 < 16.12)."""
    def parts(x):
        return tuple(int(t) for t in x.split(".") if t.isdigit())
    try:
        return parts(a) < parts(b)
    except Exception:
        return False


if __name__ == "__main__":
    main()
