"""Sanity tests for the NN pipeline.

If any of these fail, there's a bug or data leak.

Tests:
  T1. Antisymmetry:  logit(blue, red) == -logit(red, blue)  (exact, up to float)
  T2. Permutation invariance:  logit(perm(blue), red) == logit(blue, red)  (exact)
  T3. Untrained model: predict_proba ~= 0.5 +- noise on random input
  T4. Label shuffle:   train on label-permuted data; cannot beat 50% on val
                       (if it beats 50%, there's a data leak)
  T5. Planted signal:  generate synthetic data where champ c has true_logit_w[c].
                       Confirm LR recovers weights with correlation > 0.9.
                       Confirm DeepSets reaches comparable val log_loss.
  T6. Overfit-tiny:    DeepSets must be able to memorize a 64-row train set
                       (val loss matters less; train loss must drop near 0).
                       If it can't, optimizer/architecture is broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl
import torch
import torch.nn as nn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from torch.optim import AdamW
from torch.utils.data import DataLoader

from aram_nn.data import ARAMDataset, build_vocab
from aram_nn.eval import log_loss_np, accuracy_np
from aram_nn.models.deepsets import DeepSetsARAM


def green(s): return f"\033[32m{s}\033[0m"
def red(s):   return f"\033[31m{s}\033[0m"
def head(s):  print(f"\n{'='*60}\n{s}\n{'='*60}")


def t1_antisymmetry():
    head("T1: Antisymmetry  logit(b,r) == -logit(r,b)")
    torch.manual_seed(0)
    m = DeepSetsARAM(n_champs=172, embed_dim=32, hidden=64, dropout=0.0).eval()
    blue = torch.randint(0, 172, (8, 5))
    red  = torch.randint(0, 172, (8, 5))
    with torch.no_grad():
        lbr = m(blue, red)
        lrb = m(red, blue)
    diff = (lbr + lrb).abs().max().item()
    print(f"  max |logit(b,r) + logit(r,b)| = {diff:.2e}")
    ok = diff < 1e-5
    print("  " + (green("PASS") if ok else red("FAIL")))
    return ok


def t2_permutation_invariance():
    head("T2: Permutation invariance  logit(perm(b), r) == logit(b, r)")
    torch.manual_seed(0)
    m = DeepSetsARAM(n_champs=172, embed_dim=32, hidden=64, dropout=0.0).eval()
    blue = torch.randint(0, 172, (8, 5))
    red  = torch.randint(0, 172, (8, 5))
    blue_p = blue[:, torch.randperm(5)]
    red_p  = red[:,  torch.randperm(5)]
    with torch.no_grad():
        l1 = m(blue, red)
        l2 = m(blue_p, red_p)
    diff = (l1 - l2).abs().max().item()
    print(f"  max |logit(b,r) - logit(perm(b),perm(r))| = {diff:.2e}")
    ok = diff < 1e-5
    print("  " + (green("PASS") if ok else red("FAIL")))
    return ok


def t3_untrained_near_half():
    head("T3: Untrained model outputs ~0.5")
    torch.manual_seed(0)
    m = DeepSetsARAM(n_champs=172, embed_dim=32, hidden=64, dropout=0.0).eval()
    blue = torch.randint(0, 172, (10000, 5))
    red  = torch.randint(0, 172, (10000, 5))
    with torch.no_grad():
        probs = torch.sigmoid(m(blue, red)).numpy()
    mean = probs.mean()
    std  = probs.std()
    print(f"  mean prob = {mean:.4f}  std = {std:.4f}  (expect ~0.5, small std)")
    ok = abs(mean - 0.5) < 0.02 and std < 0.05
    print("  " + (green("PASS") if ok else red("FAIL")))
    return ok


def _train_quick(df_train, df_val, c2i, epochs=20, seed=42):
    """Minimal training loop returning final val log_loss + acc."""
    torch.manual_seed(seed); np.random.seed(seed)
    train_ds = ARAMDataset(df_train, c2i)
    val_ds   = ARAMDataset(df_val,   c2i)
    train_loader = DataLoader(train_ds, batch_size=256, shuffle=True)
    val_loader   = DataLoader(val_ds,   batch_size=256, shuffle=False)
    m = DeepSetsARAM(len(c2i), embed_dim=32, hidden=64, dropout=0.1)
    opt = AdamW(m.parameters(), lr=3e-3, weight_decay=1e-3)
    crit = nn.BCEWithLogitsLoss()
    best_ll = float("inf")
    for ep in range(1, epochs + 1):
        m.train()
        for blue, red, y in train_loader:
            opt.zero_grad()
            loss = crit(m(blue, red), y)
            loss.backward(); opt.step()
        m.eval()
        with torch.no_grad():
            ps, ys = [], []
            for blue, red, y in val_loader:
                ps.append(torch.sigmoid(m(blue, red)).numpy()); ys.append(y.numpy())
            ps = np.concatenate(ps); ys = np.concatenate(ys)
            ll = log_loss_np(ys, ps); ac = accuracy_np(ys, ps)
            if ll < best_ll: best_ll = ll
    return best_ll, ac


def t4_label_shuffle(parquet_path):
    head("T4: Label-shuffle test  (shuffled labels must collapse to 50%)")
    df = pl.read_parquet(parquet_path).filter(pl.col("duration_sec") >= 300)
    # restrict to single patch for speed
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    df = df.filter(pl.col("p") == "16.10").sort("game_creation_ms")
    n = df.height
    n_test = int(n * 0.15); n_val = int(n * 0.15); n_tr = n - n_test - n_val
    df_train = df.slice(0, n_tr)
    df_val   = df.slice(n_tr, n_val)

    # Shuffle labels in train ONLY (val stays correct so we measure what the model learns)
    rng = np.random.default_rng(0)
    perm = rng.permutation(df_train.height)
    df_train_shuffled = df_train.with_columns(
        pl.Series("blue_wins", df_train["blue_wins"].to_numpy()[perm])
    )
    c2i = build_vocab(df_train_shuffled)
    known = set(c2i.keys())
    mask = (
        df_val["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        & df_val["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
    )
    df_val_f = df_val.filter(mask)

    print(f"  train={df_train_shuffled.height} (labels shuffled)  val={df_val_f.height}")
    ll, ac = _train_quick(df_train_shuffled, df_val_f, c2i, epochs=15)
    base = float(df_val_f["blue_wins"].mean())
    constant_ll = log_loss_np(df_val_f["blue_wins"].cast(pl.Float32).to_numpy(),
                              np.full(df_val_f.height, base))
    print(f"  val log_loss = {ll:.4f}  acc = {ac:.4f}  | constant baseline = {constant_ll:.4f}  base rate = {base:.4f}")
    # PASS if shuffled-label model cannot meaningfully beat constant baseline.
    # Tolerance: if val log_loss > constant - 0.005, we're OK.
    ok = ll > constant_ll - 0.005 and ac < base + 0.02
    print("  " + (green("PASS") if ok else red("FAIL  <-- POSSIBLE DATA LEAK")))
    return ok


def t5_planted_signal(n_champs=50, n_train=5000, n_val=1500):
    head("T5: Planted signal test  (LR should recover known per-champ weights)")
    rng = np.random.default_rng(0)
    true_w = rng.normal(0, 0.5, size=n_champs)  # per-champ logit contribution
    # Simulate: each match samples 10 distinct champs; blue = first 5, red = last 5
    def gen(n):
        bl, rd, y = [], [], []
        for _ in range(n):
            picks = rng.choice(n_champs, size=10, replace=False)
            blue, red = picks[:5], picks[5:]
            logit = true_w[blue].sum() - true_w[red].sum()
            p = 1 / (1 + np.exp(-logit))
            label = float(rng.random() < p)
            bl.append(blue.tolist()); rd.append(red.tolist()); y.append(label)
        return bl, rd, y

    bl_tr, rd_tr, y_tr_ = gen(n_train)
    bl_va, rd_va, y_va_ = gen(n_val)

    # LR feature: +1 blue, -1 red
    def to_X(bl, rd):
        X = np.zeros((len(bl), n_champs), dtype=np.float32)
        for i, (b, r) in enumerate(zip(bl, rd)):
            for c in b: X[i, c] += 1
            for c in r: X[i, c] -= 1
        return X
    X_tr = to_X(bl_tr, rd_tr); X_va = to_X(bl_va, rd_va)
    y_tr = np.array(y_tr_, dtype=np.float32); y_va = np.array(y_va_, dtype=np.float32)

    clf = LogisticRegression(C=1.0, max_iter=2000).fit(X_tr, y_tr)
    recovered = clf.coef_[0]
    # Correlation between recovered weights and true weights
    cor = np.corrcoef(recovered, true_w)[0, 1]
    val_ll = log_loss(y_va, clf.predict_proba(X_va)[:, 1])
    print(f"  corr(recovered_w, true_w) = {cor:.4f}  (expect > 0.9)")
    print(f"  LR val log_loss = {val_ll:.4f}")

    # Now train DeepSets on the same synthetic data and check it also learns the signal
    c2i = {i: i for i in range(n_champs)}
    df_tr = pl.DataFrame({"blue_champions": bl_tr, "red_champions": rd_tr,
                          "blue_wins": y_tr_, "patch": ["fake"]*n_train,
                          "duration_sec": [600]*n_train, "game_creation_ms": list(range(n_train))})
    df_va = pl.DataFrame({"blue_champions": bl_va, "red_champions": rd_va,
                          "blue_wins": y_va_, "patch": ["fake"]*n_val,
                          "duration_sec": [600]*n_val, "game_creation_ms": list(range(n_val))})
    ds_ll, ds_ac = _train_quick(df_tr, df_va, c2i, epochs=30)
    print(f"  DeepSets val log_loss = {ds_ll:.4f}  acc = {ds_ac:.4f}")

    ok = cor > 0.9 and ds_ll < 0.65
    print("  " + (green("PASS") if ok else red("FAIL  <-- model can't even recover planted signal")))
    return ok


def t6_overfit_tiny():
    head("T6: Overfit-tiny test  (DeepSets must memorize 64 rows)")
    torch.manual_seed(0); np.random.seed(0)
    n, n_champs = 64, 30
    rng = np.random.default_rng(0)
    bl = rng.integers(0, n_champs, size=(n, 5))
    rd = rng.integers(0, n_champs, size=(n, 5))
    y = rng.integers(0, 2, size=n).astype(np.float32)

    m = DeepSetsARAM(n_champs, embed_dim=16, hidden=32, dropout=0.0)
    opt = AdamW(m.parameters(), lr=5e-3, weight_decay=0.0)
    crit = nn.BCEWithLogitsLoss()
    blue_t = torch.tensor(bl, dtype=torch.long)
    red_t  = torch.tensor(rd, dtype=torch.long)
    y_t    = torch.tensor(y,  dtype=torch.float32)

    for ep in range(500):
        m.train()
        opt.zero_grad()
        loss = crit(m(blue_t, red_t), y_t)
        loss.backward(); opt.step()
    final_train_loss = loss.item()
    with torch.no_grad():
        probs = torch.sigmoid(m(blue_t, red_t)).numpy()
    train_acc = ((probs >= 0.5) == y.astype(bool)).mean()
    print(f"  after 500 epochs:  train_loss={final_train_loss:.4f}  train_acc={train_acc:.4f}")
    ok = final_train_loss < 0.1 and train_acc > 0.95
    print("  " + (green("PASS") if ok else red("FAIL  <-- optimizer or model broken")))
    return ok


def main():
    parquet = Path("data/raw/mayhem_27k.parquet")
    if not parquet.exists():
        print(f"missing: {parquet}"); sys.exit(1)

    results = {
        "T1 antisymmetry":         t1_antisymmetry(),
        "T2 permutation invar":    t2_permutation_invariance(),
        "T3 untrained ~ 0.5":      t3_untrained_near_half(),
        "T4 label shuffle":        t4_label_shuffle(parquet),
        "T5 planted signal":       t5_planted_signal(),
        "T6 overfit tiny":         t6_overfit_tiny(),
    }
    head("SUMMARY")
    for k, v in results.items():
        print(f"  {k:30s}  {green('PASS') if v else red('FAIL')}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
