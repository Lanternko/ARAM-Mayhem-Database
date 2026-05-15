"""Single-team prediction: given one team's 5 champs, P(team wins | unknown opponent).

Each match -> 2 training rows (one per team). Base rate is exactly 50% by construction.

Comparison (combined 16.9+16.10 train, eval on 16.10 val/test):
  - Constant (0.5)
  - LR on binary one-hot (172-dim, +1 if champ in team)
  - DeepSetsSolo: embed → sum → MLP → logit
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import Dataset, DataLoader

from aram_nn.eval import log_loss_np, accuracy_np, ece_np


class SoloDataset(Dataset):
    """Each match -> 2 rows: (blue, blue_wins) and (red, 1-blue_wins)."""
    def __init__(self, df: pl.DataFrame, champ_to_idx):
        teams, labels = [], []
        b = df["blue_champions"].to_list()
        r = df["red_champions"].to_list()
        y = df["blue_wins"].to_list()
        for bl, rd, win in zip(b, r, y):
            teams.append([champ_to_idx[c] for c in bl]); labels.append(float(win))
            teams.append([champ_to_idx[c] for c in rd]); labels.append(float(1 - win))
        self.teams = teams
        self.labels = np.array(labels, dtype=np.float32)

    def __len__(self): return len(self.labels)

    def __getitem__(self, i):
        return (torch.tensor(self.teams[i], dtype=torch.long),
                torch.tensor(self.labels[i], dtype=torch.float32))


class DeepSetsSolo(nn.Module):
    """sum-embed → MLP → logit. No antisymmetry (single team)."""
    def __init__(self, n_champs, embed_dim=32, hidden=64, dropout=0.2):
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

    def forward(self, team):
        e = self.embed(team).sum(dim=1)   # (B, D)
        return self.mlp(e).squeeze(-1)

    @torch.no_grad()
    def predict_proba(self, team):
        return torch.sigmoid(self.forward(team))


def to_lr_matrix(ds: SoloDataset, n_champs: int):
    X = np.zeros((len(ds), n_champs), dtype=np.float32)
    y = np.empty(len(ds), dtype=np.float32)
    for i in range(len(ds)):
        team, label = ds[i]
        for c in team.numpy(): X[i, c] = 1.0
        y[i] = float(label)
    return X, y


def time_split_16_10(df: pl.DataFrame):
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p")
    )
    df_16_10 = df.filter(pl.col("p") == "16.10").sort("game_creation_ms")
    df_16_9  = df.filter(pl.col("p") == "16.9").sort("game_creation_ms")
    n = df_16_10.height
    n_test = int(n * 0.15); n_val = int(n * 0.15); n_train = n - n_test - n_val
    return (
        df_16_9,
        df_16_10.slice(0, n_train),
        df_16_10.slice(n_train, n_val),
        df_16_10.slice(n_train + n_val, n_test),
    )


def build_vocab(df):
    ids = set()
    for r in df["blue_champions"].to_list(): ids.update(r)
    for r in df["red_champions"].to_list():  ids.update(r)
    return {cid: i for i, cid in enumerate(sorted(ids))}


def filter_known(df, known):
    mask = (
        df["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        & df["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
    )
    return df.filter(mask)


def train_solo(model, train_loader, val_loader, device, epochs, lr, wd, patience, eval_every=5):
    opt = AdamW(model.parameters(), lr=lr, weight_decay=wd)
    sched = CosineAnnealingLR(opt, T_max=epochs)
    crit = nn.BCEWithLogitsLoss()
    best_ll = float("inf"); best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    no_imp = 0
    for epoch in range(1, epochs + 1):
        model.train(); tl, ns = 0.0, 0
        for team, y in train_loader:
            team, y = team.to(device), y.to(device)
            opt.zero_grad()
            loss = crit(model(team), y); loss.backward(); opt.step()
            tl += loss.item() * team.size(0); ns += team.size(0)
        sched.step()
        if epoch % eval_every == 0 or epoch == 1:
            model.eval()
            with torch.no_grad():
                ps, ys = [], []
                for team, y in val_loader:
                    p = torch.sigmoid(model(team.to(device))).cpu().numpy()
                    ps.append(p); ys.append(y.numpy())
                ps = np.concatenate(ps); ys = np.concatenate(ys)
                ll = log_loss_np(ys, ps)
            click.echo(f"    epoch {epoch:3d}  train_loss={tl/ns:.4f}  val_log_loss={ll:.4f}  val_acc={accuracy_np(ys, ps):.4f}")
            if ll < best_ll:
                best_ll = ll; best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}; no_imp = 0
            else:
                no_imp += 1
                if no_imp >= patience:
                    click.echo(f"    early stop at epoch {epoch}"); break
    model.load_state_dict(best_state)
    return model


def eval_solo(model, loader, device):
    model.eval()
    ps, ys = [], []
    with torch.no_grad():
        for team, y in loader:
            p = torch.sigmoid(model(team.to(device))).cpu().numpy()
            ps.append(p); ys.append(y.numpy())
    ps = np.concatenate(ps); ys = np.concatenate(ys)
    return {"log_loss": log_loss_np(ys, ps), "acc": accuracy_np(ys, ps), "ece": ece_np(ys, ps)}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--epochs", default=120)
@click.option("--patience", default=8)
@click.option("--seed", default=42)
def main(data, epochs, patience, seed):
    torch.manual_seed(seed); np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df_16_9, df_train_a, df_val, df_test = time_split_16_10(df)
    df_train_b = pl.concat([df_16_9, df_train_a]).sort("game_creation_ms")

    click.echo(f"\n[data]")
    click.echo(f"  matches: 16.9={df_16_9.height}  16.10_train={df_train_a.height}  val={df_val.height}  test={df_test.height}")
    click.echo(f"  combined train (B): {df_train_b.height} matches -> {2*df_train_b.height} solo rows")
    click.echo(f"  val: {df_val.height} matches -> {2*df_val.height} solo rows  (label=0.5 by construction)")

    all_results = []
    for tag, df_train in [("A: 16.10 only", df_train_a), ("B: 16.9+16.10", df_train_b)]:
        click.echo(f"\n==================== {tag} ====================")
        c2i = build_vocab(df_train)
        df_val_f  = filter_known(df_val,  set(c2i))
        df_test_f = filter_known(df_test, set(c2i))

        train_ds = SoloDataset(df_train,  c2i)
        val_ds   = SoloDataset(df_val_f,  c2i)
        test_ds  = SoloDataset(df_test_f, c2i)
        click.echo(f"  solo rows: train={len(train_ds)}  val={len(val_ds)}  test={len(test_ds)}")
        click.echo(f"  base rate (should be 0.5): train={train_ds.labels.mean():.4f}  val={val_ds.labels.mean():.4f}  test={test_ds.labels.mean():.4f}")

        bl_val_ll  = log_loss_np(val_ds.labels,  np.full(len(val_ds),  0.5))
        bl_test_ll = log_loss_np(test_ds.labels, np.full(len(test_ds), 0.5))
        all_results.append({"train": tag, "model": "Constant(0.5)", "val_ll": bl_val_ll, "val_acc": 0.5, "test_ll": bl_test_ll, "test_acc": 0.5})

        # --- LR ---
        click.echo(f"  [LR] training...")
        X_tr, y_tr = to_lr_matrix(train_ds, len(c2i))
        X_va, y_va = to_lr_matrix(val_ds,   len(c2i))
        X_te, y_te = to_lr_matrix(test_ds,  len(c2i))
        best_C, best_ll = 0.01, float("inf")
        for C in [0.001, 0.01, 0.1, 1.0, 10.0]:
            clf_ = LogisticRegression(C=C, max_iter=2000, solver="lbfgs")
            clf_.fit(X_tr, y_tr)
            ll = log_loss(y_va, clf_.predict_proba(X_va)[:, 1])
            if ll < best_ll: best_ll, best_C = ll, C
        clf = LogisticRegression(C=best_C, max_iter=2000, solver="lbfgs").fit(X_tr, y_tr)
        for split, X, y in [("val", X_va, y_va), ("test", X_te, y_te)]:
            p = clf.predict_proba(X)[:, 1]
            click.echo(f"    {split}  log_loss={log_loss_np(y, p):.4f}  acc={accuracy_np(y, p):.4f}  ece={ece_np(y, p):.4f}  (best C={best_C})")
        p_val  = clf.predict_proba(X_va)[:, 1]
        p_test = clf.predict_proba(X_te)[:, 1]
        all_results.append({"train": tag, "model": "LR-solo", "val_ll": log_loss_np(y_va, p_val), "val_acc": accuracy_np(y_va, p_val), "test_ll": log_loss_np(y_te, p_test), "test_acc": accuracy_np(y_te, p_test)})

        # --- DeepSetsSolo (default) ---
        click.echo(f"  [DeepSetsSolo default] training...")
        m = DeepSetsSolo(len(c2i), embed_dim=32, hidden=64, dropout=0.2).to(device)
        click.echo(f"    params={sum(p.numel() for p in m.parameters()):,}")
        m = train_solo(m,
            DataLoader(train_ds, 256, shuffle=True),
            DataLoader(val_ds,   256, shuffle=False),
            device, epochs=epochs, lr=3e-3, wd=1e-3, patience=patience)
        v = eval_solo(m, DataLoader(val_ds, 256, shuffle=False), device)
        t = eval_solo(m, DataLoader(test_ds, 256, shuffle=False), device)
        click.echo(f"    val  log_loss={v['log_loss']:.4f} acc={v['acc']:.4f} ece={v['ece']:.4f}")
        click.echo(f"    test log_loss={t['log_loss']:.4f} acc={t['acc']:.4f} ece={t['ece']:.4f}")
        all_results.append({"train": tag, "model": "DeepSetsSolo-default", "val_ll": v['log_loss'], "val_acc": v['acc'], "test_ll": t['log_loss'], "test_acc": t['acc']})

        # --- DeepSetsSolo (heavy reg) ---
        click.echo(f"  [DeepSetsSolo reg] training...")
        m = DeepSetsSolo(len(c2i), embed_dim=16, hidden=32, dropout=0.4).to(device)
        click.echo(f"    params={sum(p.numel() for p in m.parameters()):,}")
        m = train_solo(m,
            DataLoader(train_ds, 256, shuffle=True),
            DataLoader(val_ds,   256, shuffle=False),
            device, epochs=epochs, lr=2e-3, wd=5e-2, patience=patience)
        v = eval_solo(m, DataLoader(val_ds, 256, shuffle=False), device)
        t = eval_solo(m, DataLoader(test_ds, 256, shuffle=False), device)
        click.echo(f"    val  log_loss={v['log_loss']:.4f} acc={v['acc']:.4f} ece={v['ece']:.4f}")
        click.echo(f"    test log_loss={t['log_loss']:.4f} acc={t['acc']:.4f} ece={t['ece']:.4f}")
        all_results.append({"train": tag, "model": "DeepSetsSolo-reg",     "val_ll": v['log_loss'], "val_acc": v['acc'], "test_ll": t['log_loss'], "test_acc": t['acc']})

    # ---- Summary ----
    click.echo("\n\n==================== SUMMARY ====================")
    click.echo("Single-team prediction. Same val/test (last 30% of 16.10, mirrored).")
    headers = ["train", "model", "val log_loss", "val acc", "test log_loss", "test acc"]
    fmt = [[r["train"], r["model"], f"{r['val_ll']:.4f}", f"{r['val_acc']:.4f}", f"{r['test_ll']:.4f}", f"{r['test_acc']:.4f}"] for r in all_results]
    col_w = [max(len(h), max(len(rr[i]) for rr in fmt)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(col_w) + 2 * (len(headers) - 1)))
    for rr in fmt:
        click.echo("  " + "  ".join(rr[i].ljust(col_w[i]) for i in range(len(headers))))


if __name__ == "__main__":
    main()
