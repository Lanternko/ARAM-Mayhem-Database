"""Solo-team experiments: pairwise LR features + LR-residual NN stacking.

All models trained on combined 16.9+16.10, evaluated on last 30% of 16.10.
Mirrored solo rows (each match -> 2 rows, base rate = 0.5).

Models compared on identical splits:
  1. LR-solo                  (172 features)
  2. LR-solo + within-pairs   (172 + C(172,2)=14,878 features, sparse)
  3. DeepSetsSolo (reg)       (NN baseline)
  4. Residual: NN(team) + LR_logit, BCE on total prediction
       -> NN learns to refine LR. Output = sigmoid(LR_logit + NN_delta)
"""
from __future__ import annotations

from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from scipy.sparse import csr_matrix, hstack as sp_hstack
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import Dataset, DataLoader

from aram_nn.eval import log_loss_np, accuracy_np, ece_np


# ---------- Data ----------

def time_split(df: pl.DataFrame):
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    d10 = df.filter(pl.col("p") == "16.10").sort("game_creation_ms")
    d9  = df.filter(pl.col("p") == "16.9").sort("game_creation_ms")
    n = d10.height
    n_test = int(n * 0.15); n_val = int(n * 0.15); n_tr = n - n_test - n_val
    return d9, d10.slice(0, n_tr), d10.slice(n_tr, n_val), d10.slice(n_tr + n_val, n_test)


def build_vocab(df: pl.DataFrame):
    s = set()
    for r in df["blue_champions"].to_list(): s.update(r)
    for r in df["red_champions"].to_list():  s.update(r)
    return {c: i for i, c in enumerate(sorted(s))}


def filter_known(df, known):
    mask = (
        df["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        & df["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
    )
    return df.filter(mask)


def mirror_solo(df, c2i):
    """Each match -> 2 solo rows. Returns list[list[int]] teams and np.ndarray labels."""
    teams, labels = [], []
    bl = df["blue_champions"].to_list()
    rd = df["red_champions"].to_list()
    yy = df["blue_wins"].to_list()
    for b, r, y in zip(bl, rd, yy):
        teams.append([c2i[c] for c in b]); labels.append(float(y))
        teams.append([c2i[c] for c in r]); labels.append(float(1 - y))
    return teams, np.array(labels, dtype=np.float32)


# ---------- LR feature builders ----------

def build_solo_X(teams, n_champs):
    """Dense: shape (N, n_champs), 1 if champ in team."""
    X = np.zeros((len(teams), n_champs), dtype=np.float32)
    for i, t in enumerate(teams):
        for c in t: X[i, c] = 1.0
    return X


def build_solo_X_pairs(teams, n_champs):
    """Sparse CSR: [solo_features | within-team-pair features].

    Pair index: for (a, b) with a < b, idx = a*n_champs + b - (a+1)(a+2)/2
    But simpler: just use (a, b) -> linear index via upper triangular enumeration.
    """
    # Build pair_idx map
    pair_idx = {}
    k = 0
    for a in range(n_champs):
        for b in range(a + 1, n_champs):
            pair_idx[(a, b)] = k; k += 1
    n_pairs = k

    rows, cols, data = [], [], []
    for i, t in enumerate(teams):
        # solo
        for c in t:
            rows.append(i); cols.append(c); data.append(1.0)
        # pairs
        tt = sorted(t)
        for ai in range(5):
            for bi in range(ai + 1, 5):
                a, b = tt[ai], tt[bi]
                if a == b: continue
                rows.append(i); cols.append(n_champs + pair_idx[(a, b)]); data.append(1.0)
    X = csr_matrix((data, (rows, cols)), shape=(len(teams), n_champs + n_pairs), dtype=np.float32)
    return X, n_pairs


# ---------- Dataset for NN ----------

class SoloTeamLogitDataset(Dataset):
    """Returns (team_idx tensor, lr_logit, label). lr_logit can be 0 for plain NN."""
    def __init__(self, teams, labels, lr_logits=None):
        self.teams = teams
        self.labels = labels
        self.lr_logits = lr_logits if lr_logits is not None else np.zeros(len(labels), dtype=np.float32)

    def __len__(self): return len(self.labels)
    def __getitem__(self, i):
        return (torch.tensor(self.teams[i], dtype=torch.long),
                torch.tensor(self.lr_logits[i], dtype=torch.float32),
                torch.tensor(self.labels[i], dtype=torch.float32))


class DeepSetsSoloDelta(nn.Module):
    """NN that outputs a delta logit; final prediction = sigmoid(lr_logit + delta).
    If you don't have lr_logit, pass 0 and this is identical to plain DeepSetsSolo.
    """
    def __init__(self, n_champs, embed_dim=16, hidden=32, dropout=0.4, init_scale=0.01):
        super().__init__()
        self.embed = nn.Embedding(n_champs, embed_dim)
        self.mlp = nn.Sequential(
            nn.Linear(embed_dim, hidden),
            nn.LayerNorm(hidden),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, hidden // 2),
            nn.GELU(),
            nn.Linear(hidden // 2, 1),
        )
        # Shrink final layer to make initial delta ~ 0 (model starts as "no correction")
        with torch.no_grad():
            self.mlp[-1].weight.mul_(init_scale)
            self.mlp[-1].bias.zero_()

    def delta(self, team):
        e = self.embed(team).sum(dim=1)
        return self.mlp(e).squeeze(-1)

    def forward(self, team, lr_logit):
        return lr_logit + self.delta(team)

    @torch.no_grad()
    def predict_proba(self, team, lr_logit):
        return torch.sigmoid(self.forward(team, lr_logit))


def train_nn(model, train_loader, val_loader, device, epochs=100, lr=2e-3, wd=5e-2, patience=8, eval_every=5):
    opt = AdamW(model.parameters(), lr=lr, weight_decay=wd)
    sched = CosineAnnealingLR(opt, T_max=epochs)
    crit = nn.BCEWithLogitsLoss()
    best_ll = float("inf"); best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    no_imp = 0
    for ep in range(1, epochs + 1):
        model.train(); tl, ns = 0.0, 0
        for team, lr_l, y in train_loader:
            team, lr_l, y = team.to(device), lr_l.to(device), y.to(device)
            opt.zero_grad()
            loss = crit(model(team, lr_l), y)
            loss.backward(); opt.step()
            tl += loss.item() * team.size(0); ns += team.size(0)
        sched.step()
        if ep % eval_every == 0 or ep == 1:
            v = _eval(model, val_loader, device); ll = v["log_loss"]
            click.echo(f"    epoch {ep:3d}  train_loss={tl/ns:.4f}  val_ll={ll:.4f}  val_acc={v['acc']:.4f}")
            if ll < best_ll:
                best_ll = ll; best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}; no_imp = 0
            else:
                no_imp += 1
                if no_imp >= patience: click.echo(f"    early stop at epoch {ep}"); break
    model.load_state_dict(best_state)
    return model


def _eval(model, loader, device):
    model.eval()
    ps, ys = [], []
    with torch.no_grad():
        for team, lr_l, y in loader:
            p = torch.sigmoid(model(team.to(device), lr_l.to(device))).cpu().numpy()
            ps.append(p); ys.append(y.numpy())
    ps = np.concatenate(ps); ys = np.concatenate(ys)
    return {"log_loss": log_loss_np(ys, ps), "acc": accuracy_np(ys, ps), "ece": ece_np(ys, ps)}


# ---------- main ----------

@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--epochs", default=100, type=int)
@click.option("--patience", default=8, type=int)
def main(data, epochs, patience):
    torch.manual_seed(42); np.random.seed(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    d9, d10_tr, d_val, d_test = time_split(df)
    df_train = pl.concat([d9, d10_tr]).sort("game_creation_ms")
    click.echo(f"\n[data] train={df_train.height}  val={d_val.height}  test={d_test.height}")

    c2i = build_vocab(df_train)
    n_champs = len(c2i)
    d_val_f  = filter_known(d_val,  set(c2i))
    d_test_f = filter_known(d_test, set(c2i))

    teams_tr,  y_tr  = mirror_solo(df_train,  c2i)
    teams_val, y_val = mirror_solo(d_val_f,  c2i)
    teams_te,  y_te  = mirror_solo(d_test_f, c2i)
    click.echo(f"  solo rows: train={len(teams_tr)}  val={len(teams_val)}  test={len(teams_te)}")
    click.echo(f"  base rate (must be 0.5): train={y_tr.mean():.4f} val={y_val.mean():.4f} test={y_te.mean():.4f}")

    results = []

    # ============ 1. LR-solo (baseline) ============
    click.echo("\n=== 1. LR-solo (172 features) ===")
    X_tr = build_solo_X(teams_tr,  n_champs)
    X_va = build_solo_X(teams_val, n_champs)
    X_te = build_solo_X(teams_te,  n_champs)
    best_C, best_ll = 0.01, float("inf")
    for C in [0.001, 0.01, 0.1, 1.0]:
        clf_ = LogisticRegression(C=C, max_iter=2000, solver="lbfgs").fit(X_tr, y_tr)
        ll = log_loss(y_val, clf_.predict_proba(X_va)[:, 1])
        if ll < best_ll: best_ll, best_C = ll, C
    lr_solo = LogisticRegression(C=best_C, max_iter=2000, solver="lbfgs").fit(X_tr, y_tr)
    p_val_lr  = lr_solo.predict_proba(X_va)[:, 1]
    p_test_lr = lr_solo.predict_proba(X_te)[:, 1]
    click.echo(f"  best C={best_C}")
    click.echo(f"  val  log_loss={log_loss_np(y_val, p_val_lr):.4f}  acc={accuracy_np(y_val, p_val_lr):.4f}")
    click.echo(f"  test log_loss={log_loss_np(y_te,  p_test_lr):.4f}  acc={accuracy_np(y_te,  p_test_lr):.4f}")
    results.append(("LR-solo (172 feat)",
                    log_loss_np(y_val, p_val_lr), accuracy_np(y_val, p_val_lr),
                    log_loss_np(y_te,  p_test_lr), accuracy_np(y_te,  p_test_lr)))

    # ============ 2. LR-pairwise (172 + within-pairs) ============
    click.echo("\n=== 2. LR-pairwise (within-team pair features) ===")
    Xp_tr, n_pairs = build_solo_X_pairs(teams_tr,  n_champs)
    Xp_va, _       = build_solo_X_pairs(teams_val, n_champs)
    Xp_te, _       = build_solo_X_pairs(teams_te,  n_champs)
    click.echo(f"  pair features: {n_pairs}  (total feat = {n_champs + n_pairs})")
    best_C, best_ll = 0.01, float("inf")
    for C in [0.0001, 0.001, 0.01, 0.05, 0.1, 1.0]:
        clf_ = LogisticRegression(C=C, max_iter=3000, solver="liblinear", penalty="l2").fit(Xp_tr, y_tr)
        ll = log_loss(y_val, clf_.predict_proba(Xp_va)[:, 1])
        click.echo(f"    C={C}  val log_loss={ll:.4f}")
        if ll < best_ll: best_ll, best_C = ll, C
    lr_pair = LogisticRegression(C=best_C, max_iter=3000, solver="liblinear", penalty="l2").fit(Xp_tr, y_tr)
    p_val_p  = lr_pair.predict_proba(Xp_va)[:, 1]
    p_test_p = lr_pair.predict_proba(Xp_te)[:, 1]
    click.echo(f"  best C={best_C}")
    click.echo(f"  val  log_loss={log_loss_np(y_val, p_val_p):.4f}  acc={accuracy_np(y_val, p_val_p):.4f}")
    click.echo(f"  test log_loss={log_loss_np(y_te,  p_test_p):.4f}  acc={accuracy_np(y_te,  p_test_p):.4f}")
    results.append(("LR-pairwise (172+pairs)",
                    log_loss_np(y_val, p_val_p), accuracy_np(y_val, p_val_p),
                    log_loss_np(y_te,  p_test_p), accuracy_np(y_te,  p_test_p)))

    # ============ 3. DeepSetsSolo (reg) — baseline NN ============
    click.echo("\n=== 3. DeepSetsSolo (reg, no LR prior) ===")
    train_loader = DataLoader(SoloTeamLogitDataset(teams_tr,  y_tr), batch_size=256, shuffle=True)
    val_loader   = DataLoader(SoloTeamLogitDataset(teams_val, y_val), batch_size=256, shuffle=False)
    test_loader  = DataLoader(SoloTeamLogitDataset(teams_te,  y_te), batch_size=256, shuffle=False)
    nn_solo = DeepSetsSoloDelta(n_champs, embed_dim=16, hidden=32, dropout=0.4, init_scale=1.0).to(device)
    click.echo(f"  params={sum(p.numel() for p in nn_solo.parameters()):,}")
    nn_solo = train_nn(nn_solo, train_loader, val_loader, device, epochs=epochs, patience=patience)
    v = _eval(nn_solo, val_loader, device); t = _eval(nn_solo, test_loader, device)
    click.echo(f"  val  log_loss={v['log_loss']:.4f}  acc={v['acc']:.4f}")
    click.echo(f"  test log_loss={t['log_loss']:.4f}  acc={t['acc']:.4f}")
    results.append(("DeepSetsSolo (reg)", v['log_loss'], v['acc'], t['log_loss'], t['acc']))

    # ============ 4. Residual NN on LR ============
    click.echo("\n=== 4. Residual NN (NN_delta + LR_logit) ===")
    # Compute LR logits (from the BEST LR — use LR-solo for clean baseline; could swap to LR-pairwise)
    def to_logit(p): return np.log(np.clip(p, 1e-7, 1 - 1e-7) / np.clip(1 - p, 1e-7, 1 - 1e-7))
    lr_logit_tr  = to_logit(lr_solo.predict_proba(X_tr)[:, 1]).astype(np.float32)
    lr_logit_val = to_logit(p_val_lr).astype(np.float32)
    lr_logit_te  = to_logit(p_test_lr).astype(np.float32)
    train_loader_r = DataLoader(SoloTeamLogitDataset(teams_tr,  y_tr,  lr_logit_tr),  batch_size=256, shuffle=True)
    val_loader_r   = DataLoader(SoloTeamLogitDataset(teams_val, y_val, lr_logit_val), batch_size=256, shuffle=False)
    test_loader_r  = DataLoader(SoloTeamLogitDataset(teams_te,  y_te,  lr_logit_te),  batch_size=256, shuffle=False)
    nn_res = DeepSetsSoloDelta(n_champs, embed_dim=16, hidden=32, dropout=0.4, init_scale=0.01).to(device)
    click.echo(f"  params={sum(p.numel() for p in nn_res.parameters()):,}  (init_scale=0.01 — starts ~ LR)")
    nn_res = train_nn(nn_res, train_loader_r, val_loader_r, device, epochs=epochs, patience=patience)
    v = _eval(nn_res, val_loader_r, device); t = _eval(nn_res, test_loader_r, device)
    click.echo(f"  val  log_loss={v['log_loss']:.4f}  acc={v['acc']:.4f}")
    click.echo(f"  test log_loss={t['log_loss']:.4f}  acc={t['acc']:.4f}")
    results.append(("Residual NN on LR", v['log_loss'], v['acc'], t['log_loss'], t['acc']))

    # ============ Also: Residual NN on LR-pairwise ============
    click.echo("\n=== 5. Residual NN on LR-pairwise ===")
    p_train_lrp = lr_pair.predict_proba(Xp_tr)[:, 1]
    lrp_logit_tr  = to_logit(p_train_lrp).astype(np.float32)
    lrp_logit_val = to_logit(p_val_p).astype(np.float32)
    lrp_logit_te  = to_logit(p_test_p).astype(np.float32)
    train_loader_rp = DataLoader(SoloTeamLogitDataset(teams_tr,  y_tr,  lrp_logit_tr),  batch_size=256, shuffle=True)
    val_loader_rp   = DataLoader(SoloTeamLogitDataset(teams_val, y_val, lrp_logit_val), batch_size=256, shuffle=False)
    test_loader_rp  = DataLoader(SoloTeamLogitDataset(teams_te,  y_te,  lrp_logit_te),  batch_size=256, shuffle=False)
    nn_resp = DeepSetsSoloDelta(n_champs, embed_dim=16, hidden=32, dropout=0.4, init_scale=0.01).to(device)
    nn_resp = train_nn(nn_resp, train_loader_rp, val_loader_rp, device, epochs=epochs, patience=patience)
    v = _eval(nn_resp, val_loader_rp, device); t = _eval(nn_resp, test_loader_rp, device)
    click.echo(f"  val  log_loss={v['log_loss']:.4f}  acc={v['acc']:.4f}")
    click.echo(f"  test log_loss={t['log_loss']:.4f}  acc={t['acc']:.4f}")
    results.append(("Residual NN on LR-pairwise", v['log_loss'], v['acc'], t['log_loss'], t['acc']))

    # ============ Summary ============
    click.echo("\n\n========== SUMMARY (single-team, mirrored solo) ==========")
    h = ["model", "val log_loss", "val acc", "test log_loss", "test acc"]
    fmt = [[r[0], f"{r[1]:.4f}", f"{r[2]:.4f}", f"{r[3]:.4f}", f"{r[4]:.4f}"] for r in results]
    cw = [max(len(hh), max(len(rr[i]) for rr in fmt)) for i, hh in enumerate(h)]
    click.echo("  " + "  ".join(hh.ljust(cw[i]) for i, hh in enumerate(h)))
    click.echo("  " + "-" * (sum(cw) + 2*(len(h)-1)))
    for rr in fmt: click.echo("  " + "  ".join(rr[i].ljust(cw[i]) for i in range(len(h))))


if __name__ == "__main__":
    main()
