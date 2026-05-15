"""Tier 2: DeepSets + patch embedding, cross-patch training on full Mayhem 18k.

Comparison on identical time-based split:
  - Constant baseline
  - LR (no patch awareness)
  - DeepSets no-patch (cross-patch, patch info ignored)
  - DeepSets + patch embedding (Tier 2)

Patch goes into the symmetric channel only (same patch for both teams),
so swap-team antisymmetry is preserved.

Usage:
  python scripts/train_tier2.py \
    --data data/raw/mayhem_18k.parquet \
    --min-patch-count 500 \
    --out models/tier2_mayhem
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import Dataset, DataLoader

from aram_nn.eval import TemperatureScaler, evaluate, log_loss_np, accuracy_np, ece_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval
from aram_nn.data import ARAMDataset


# ---------- Dataset with patch ----------

class ARAMDatasetWithPatch(Dataset):
    def __init__(self, df: pl.DataFrame, champ_to_idx, patch_to_idx):
        self.blue = [[champ_to_idx[c] for c in row] for row in df["blue_champions"].to_list()]
        self.red  = [[champ_to_idx[c] for c in row] for row in df["red_champions"].to_list()]
        self.patch = [patch_to_idx[p] for p in df["patch_prefix"].to_list()]
        self.labels = df["blue_wins"].cast(pl.Float32).to_numpy()

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        blue  = torch.tensor(self.blue[i],  dtype=torch.long)
        red   = torch.tensor(self.red[i],   dtype=torch.long)
        patch = torch.tensor(self.patch[i], dtype=torch.long)
        y     = torch.tensor(self.labels[i], dtype=torch.float32)
        return blue, red, patch, y


def make_loader_p(ds, batch_size, shuffle):
    return DataLoader(ds, batch_size=batch_size, shuffle=shuffle, drop_last=False)


# ---------- Model ----------

def _mlp(in_dim, hidden, out_dim, dropout):
    return nn.Sequential(
        nn.Linear(in_dim, hidden),
        nn.LayerNorm(hidden),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(hidden, hidden // 2),
        nn.GELU(),
        nn.Linear(hidden // 2, out_dim),
    )


class DeepSetsPatch(nn.Module):
    """DeepSets with patch embedding in the symmetric channel.

    logit = (f([diff, total, patch]) - f([-diff, total, patch])) / 2
    Antisymmetric under swap-teams (diff flips, total/patch shared).
    """
    def __init__(self, n_champs, n_patches, embed_dim=32, patch_dim=8, hidden=64, dropout=0.1):
        super().__init__()
        self.champ_embed = nn.Embedding(n_champs, embed_dim)
        self.patch_embed = nn.Embedding(n_patches, patch_dim)
        self.mlp = _mlp(2 * embed_dim + patch_dim, hidden, 1, dropout)

    def _raw(self, diff, total, p):
        h = torch.cat([diff, total, p], dim=-1)
        return self.mlp(h).squeeze(-1)

    def forward(self, blue, red, patch):
        e_b = self.champ_embed(blue).sum(dim=1)
        e_r = self.champ_embed(red).sum(dim=1)
        p   = self.patch_embed(patch)
        diff  = e_b - e_r
        total = e_b + e_r
        return (self._raw(diff, total, p) - self._raw(-diff, total, p)) / 2.0

    @torch.no_grad()
    def predict_proba(self, blue, red, patch):
        return torch.sigmoid(self.forward(blue, red, patch))


class DeepSetsNoPatch(nn.Module):
    """Identical architecture but with patch input discarded. Apples-to-apples baseline."""
    def __init__(self, n_champs, embed_dim=32, hidden=64, dropout=0.1):
        super().__init__()
        self.champ_embed = nn.Embedding(n_champs, embed_dim)
        self.mlp = _mlp(2 * embed_dim, hidden, 1, dropout)

    def _raw(self, diff, total):
        h = torch.cat([diff, total], dim=-1)
        return self.mlp(h).squeeze(-1)

    def forward(self, blue, red, patch=None):
        e_b = self.champ_embed(blue).sum(dim=1)
        e_r = self.champ_embed(red).sum(dim=1)
        diff  = e_b - e_r
        total = e_b + e_r
        return (self._raw(diff, total) - self._raw(-diff, total)) / 2.0

    @torch.no_grad()
    def predict_proba(self, blue, red, patch=None):
        return torch.sigmoid(self.forward(blue, red, patch))


# ---------- Split ----------

@dataclass
class SplitsP:
    train: ARAMDatasetWithPatch
    val:   ARAMDatasetWithPatch
    test:  ARAMDatasetWithPatch
    train_no_patch: ARAMDataset
    val_no_patch:   ARAMDataset
    test_no_patch:  ARAMDataset
    champ_to_idx: dict
    patch_to_idx: dict
    n_champs: int
    n_patches: int
    blue_base_rate: float
    train_patches_summary: dict
    val_patches_summary: dict
    test_patches_summary: dict


def load_splits_with_patch(
    parquet_path: Path,
    min_patch_count: int = 500,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
    min_duration: int = 300,
) -> SplitsP:
    df = pl.read_parquet(parquet_path)
    df = df.filter(pl.col("duration_sec") >= min_duration)

    # Patch prefix column
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
    )

    # Keep only patches with >= min_patch_count games (noise filter)
    counts = df.group_by("patch_prefix").len().rename({"len": "n"})
    keep = counts.filter(pl.col("n") >= min_patch_count)["patch_prefix"].to_list()
    df = df.filter(pl.col("patch_prefix").is_in(keep))

    if df.height == 0:
        raise ValueError(f"No patches with >= {min_patch_count} games")

    df = df.sort("game_creation_ms")
    n = df.height
    n_test = max(1, int(n * test_frac))
    n_val  = max(1, int(n * val_frac))
    n_train = n - n_test - n_val
    df_train = df.slice(0, n_train)
    df_val   = df.slice(n_train, n_val)
    df_test  = df.slice(n_train + n_val, n_test)

    # Build vocabs on train only
    champ_ids: set[int] = set()
    for row in df_train["blue_champions"].to_list(): champ_ids.update(row)
    for row in df_train["red_champions"].to_list():  champ_ids.update(row)
    champ_to_idx = {cid: i for i, cid in enumerate(sorted(champ_ids))}
    patch_to_idx = {p: i for i, p in enumerate(sorted(df_train["patch_prefix"].unique().to_list()))}

    known_c = set(champ_to_idx.keys())
    known_p = set(patch_to_idx.keys())

    def _filter(d):
        mask = (
            d["blue_champions"].list.eval(pl.element().is_in(list(known_c))).list.all()
            & d["red_champions"].list.eval(pl.element().is_in(list(known_c))).list.all()
            & d["patch_prefix"].is_in(list(known_p))
        )
        return d.filter(mask)

    df_val_f  = _filter(df_val)
    df_test_f = _filter(df_test)

    def patch_summary(d):
        return {row[0]: row[1] for row in d.group_by("patch_prefix").len().rename({"len": "n"}).iter_rows()}

    return SplitsP(
        train=ARAMDatasetWithPatch(df_train, champ_to_idx, patch_to_idx),
        val=ARAMDatasetWithPatch(df_val_f, champ_to_idx, patch_to_idx),
        test=ARAMDatasetWithPatch(df_test_f, champ_to_idx, patch_to_idx),
        train_no_patch=ARAMDataset(df_train, champ_to_idx),
        val_no_patch=ARAMDataset(df_val_f, champ_to_idx),
        test_no_patch=ARAMDataset(df_test_f, champ_to_idx),
        champ_to_idx=champ_to_idx,
        patch_to_idx=patch_to_idx,
        n_champs=len(champ_to_idx),
        n_patches=len(patch_to_idx),
        blue_base_rate=float(df_train["blue_wins"].mean()),
        train_patches_summary=patch_summary(df_train),
        val_patches_summary=patch_summary(df_val_f),
        test_patches_summary=patch_summary(df_test_f),
    )


# ---------- Training helpers ----------

@torch.no_grad()
def collect_probs_p(model, loader, device, with_patch: bool):
    model.eval()
    probs, labels = [], []
    for batch in loader:
        if with_patch:
            blue, red, patch, y = batch
            blue, red, patch = blue.to(device), red.to(device), patch.to(device)
            p = model.predict_proba(blue, red, patch).cpu().numpy()
        else:
            blue, red, y = batch
            blue, red = blue.to(device), red.to(device)
            p = model.predict_proba(blue, red).cpu().numpy()
        probs.append(p)
        labels.append(y.numpy())
    return np.concatenate(probs), np.concatenate(labels)


def eval_model(model, loader, device, with_patch: bool, label: str):
    probs, labels = collect_probs_p(model, loader, device, with_patch)
    return {
        f"{label}/log_loss": log_loss_np(labels, probs),
        f"{label}/acc":      accuracy_np(labels, probs),
        f"{label}/ece":      ece_np(labels, probs),
    }, probs, labels


def train_deepsets(
    model, train_loader, val_loader, device,
    with_patch: bool,
    epochs: int, lr: float, weight_decay: float, patience: int, swap_aug: bool,
    eval_every: int = 5,
):
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.BCEWithLogitsLoss()
    best_val_ll = float("inf")
    best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    epochs_no_improve = 0

    for epoch in range(1, epochs + 1):
        model.train()
        tl, ns = 0.0, 0
        for batch in train_loader:
            if with_patch:
                blue, red, patch, y = batch
                blue, red, patch, y = blue.to(device), red.to(device), patch.to(device), y.to(device)
            else:
                blue, red, y = batch
                blue, red, y = blue.to(device), red.to(device), y.to(device)

            if swap_aug:
                mask = torch.rand(blue.size(0), device=device) < 0.5
                blue_s = torch.where(mask.unsqueeze(1), red, blue)
                red_s  = torch.where(mask.unsqueeze(1), blue, red)
                y_s    = torch.where(mask, 1.0 - y, y)
                blue, red, y = blue_s, red_s, y_s

            optimizer.zero_grad()
            if with_patch:
                logits = model(blue, red, patch)
            else:
                logits = model(blue, red)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            tl += loss.item() * blue.size(0)
            ns += blue.size(0)
        scheduler.step()

        if epoch % eval_every == 0 or epoch == 1:
            metrics, _, _ = eval_model(model, val_loader, device, with_patch, "val")
            ll = metrics["val/log_loss"]
            click.echo(f"  epoch {epoch:3d}  train_loss={tl/ns:.4f}  val_log_loss={ll:.4f}  val_acc={metrics['val/acc']:.4f}")
            if ll < best_val_ll:
                best_val_ll = ll
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                epochs_no_improve = 0
            else:
                epochs_no_improve += 1
                if epochs_no_improve >= patience:
                    click.echo(f"  early stop at epoch {epoch}")
                    break

    model.load_state_dict(best_state)
    return model, best_val_ll


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--min-patch-count", default=500, show_default=True, type=int)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--patch-dim", default=8, show_default=True)
@click.option("--hidden", default=64, show_default=True)
@click.option("--dropout", default=0.2, show_default=True, type=float)
@click.option("--lr", default=3e-3, show_default=True, type=float)
@click.option("--epochs", default=120, show_default=True, type=int)
@click.option("--batch-size", default=256, show_default=True, type=int)
@click.option("--patience", default=8, show_default=True, type=int)
@click.option("--weight-decay", default=5e-3, show_default=True, type=float)
@click.option("--seed", default=42, show_default=True, type=int)
def main(data, min_patch_count, out, embed_dim, patch_dim, hidden, dropout, lr,
         epochs, batch_size, patience, weight_decay, seed):
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    s = load_splits_with_patch(data, min_patch_count=min_patch_count)
    click.echo(
        f"[data] train={len(s.train)}  val={len(s.val)}  test={len(s.test)}"
        f"  n_champs={s.n_champs}  n_patches={s.n_patches}  blue_base_rate={s.blue_base_rate:.3f}"
    )
    click.echo(f"  train patches: {s.train_patches_summary}")
    click.echo(f"  val   patches: {s.val_patches_summary}")
    click.echo(f"  test  patches: {s.test_patches_summary}")

    train_loader_p = make_loader_p(s.train, batch_size, shuffle=True)
    val_loader_p   = make_loader_p(s.val,   batch_size, shuffle=False)
    test_loader_p  = make_loader_p(s.test,  batch_size, shuffle=False)

    # ---- Constant ----
    dummy = np.full(len(s.val), s.blue_base_rate)
    bl_ll = log_loss_np(s.val.labels, dummy)
    click.echo(f"\n[baseline/constant]  val log_loss={bl_ll:.4f}  (always predict {s.blue_base_rate:.3f})")

    # ---- LR ----
    click.echo("\n[LR baseline] training (cross-patch, no patch feature)...")
    lr_results = lr_train_eval(s.train_no_patch, s.val_no_patch, s.test_no_patch, s.n_champs)
    click.echo(
        f"  best C={lr_results['best_C']}\n"
        f"  val   log_loss={lr_results['val/log_loss']:.4f}  acc={lr_results['val/acc']:.4f}\n"
        f"  test  log_loss={lr_results['test/log_loss']:.4f}  acc={lr_results['test/acc']:.4f}"
    )

    # ---- DeepSets no-patch (apples-to-apples baseline) ----
    click.echo("\n[DeepSets no-patch] training...")
    m_np = DeepSetsNoPatch(s.n_champs, embed_dim, hidden, dropout).to(device)
    click.echo(f"  params={sum(p.numel() for p in m_np.parameters()):,}")
    from aram_nn.data import make_loader as make_loader_np
    train_loader_np = make_loader_np(s.train_no_patch, batch_size, shuffle=True)
    val_loader_np   = make_loader_np(s.val_no_patch,   batch_size, shuffle=False)
    test_loader_np  = make_loader_np(s.test_no_patch,  batch_size, shuffle=False)

    m_np, best_ll_np = train_deepsets(
        m_np, train_loader_np, val_loader_np, device,
        with_patch=False, epochs=epochs, lr=lr, weight_decay=weight_decay,
        patience=patience, swap_aug=True,
    )
    np_val,  _, _ = eval_model(m_np, val_loader_np,  device, False, "val")
    np_test, _, _ = eval_model(m_np, test_loader_np, device, False, "test")

    # ---- DeepSets + patch (Tier 2) ----
    click.echo("\n[Tier 2 DeepSets+patch] training...")
    m_p = DeepSetsPatch(s.n_champs, s.n_patches, embed_dim, patch_dim, hidden, dropout).to(device)
    click.echo(f"  params={sum(p.numel() for p in m_p.parameters()):,}")

    m_p, best_ll_p = train_deepsets(
        m_p, train_loader_p, val_loader_p, device,
        with_patch=True, epochs=epochs, lr=lr, weight_decay=weight_decay,
        patience=patience, swap_aug=True,
    )

    # Temperature scaling (only Tier 2)
    click.echo("\n[calibration] temperature scaling on val (Tier 2)...")
    # Build a wrapper since TemperatureScaler expects (blue, red) signature.
    class _Wrap(nn.Module):
        def __init__(self, inner): super().__init__(); self.inner = inner
        def forward(self, blue, red, patch): return self.inner(blue, red, patch)
        @torch.no_grad()
        def predict_proba(self, blue, red, patch): return torch.sigmoid(self.forward(blue, red, patch))

    # Simple inline temp-scale (uses patch loader directly)
    with torch.no_grad():
        all_logits, all_y = [], []
        m_p.eval()
        for blue, red, patch, y in val_loader_p:
            all_logits.append(m_p(blue.to(device), red.to(device), patch.to(device)).cpu())
            all_y.append(y)
        logits_t = torch.cat(all_logits).to(device)
        labels_t = torch.cat(all_y).to(device)
    T = nn.Parameter(torch.ones(1, device=device))
    opt = torch.optim.LBFGS([T], lr=0.01, max_iter=200)
    crit = nn.BCEWithLogitsLoss()
    def closure():
        opt.zero_grad()
        loss = crit(logits_t / T.clamp(min=1e-2), labels_t)
        loss.backward()
        return loss
    opt.step(closure)
    T_val = float(T.detach().cpu().item())
    click.echo(f"  T={T_val:.4f}")

    def eval_with_T(loader):
        with torch.no_grad():
            all_logits, all_y = [], []
            m_p.eval()
            for blue, red, patch, y in loader:
                all_logits.append(m_p(blue.to(device), red.to(device), patch.to(device)).cpu().numpy())
                all_y.append(y.numpy())
        logits = np.concatenate(all_logits)
        y = np.concatenate(all_y)
        probs_raw = 1.0 / (1.0 + np.exp(-logits))
        probs_cal = 1.0 / (1.0 + np.exp(-logits / max(T_val, 1e-2)))
        return {
            "raw": (log_loss_np(y, probs_raw), accuracy_np(y, probs_raw), ece_np(y, probs_raw)),
            "cal": (log_loss_np(y, probs_cal), accuracy_np(y, probs_cal), ece_np(y, probs_cal)),
        }

    p_val  = eval_with_T(val_loader_p)
    p_test = eval_with_T(test_loader_p)

    # ---- Final results ----
    click.echo("\n[results]")
    rows = [
        ("val",  "Constant",            bl_ll, s.blue_base_rate, None),  # dummy acc = base rate
        ("val",  "LR",                  lr_results['val/log_loss'],  lr_results['val/acc'],  None),
        ("val",  "DeepSets no-patch",   np_val['val/log_loss'],      np_val['val/acc'],      np_val['val/ece']),
        ("val",  "DeepSets+patch (raw)",p_val['raw'][0],             p_val['raw'][1],        p_val['raw'][2]),
        ("val",  "DeepSets+patch (cal)",p_val['cal'][0],             p_val['cal'][1],        p_val['cal'][2]),
        ("test", "Constant",            log_loss_np(s.test.labels, np.full(len(s.test), s.blue_base_rate)), s.blue_base_rate, None),
        ("test", "LR",                  lr_results['test/log_loss'], lr_results['test/acc'], None),
        ("test", "DeepSets no-patch",   np_test['test/log_loss'],    np_test['test/acc'],    np_test['test/ece']),
        ("test", "DeepSets+patch (raw)",p_test['raw'][0],            p_test['raw'][1],       p_test['raw'][2]),
        ("test", "DeepSets+patch (cal)",p_test['cal'][0],            p_test['cal'][1],       p_test['cal'][2]),
    ]
    headers = ["split", "model", "log_loss", "acc", "ece"]
    fmt_rows = [[r[0], r[1], f"{r[2]:.4f}", f"{r[3]:.4f}", "-" if r[4] is None else f"{r[4]:.4f}"] for r in rows]
    col_w = [max(len(h), max(len(rr[i]) for rr in fmt_rows)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(col_w) + 2 * (len(headers) - 1)))
    for rr in fmt_rows:
        click.echo("  " + "  ".join(rr[i].ljust(col_w[i]) for i in range(len(headers))))

    # ---- Save ----
    out.mkdir(parents=True, exist_ok=True)
    torch.save({
        "model": m_p.state_dict(), "temperature": T_val,
        "champ_to_idx": s.champ_to_idx, "patch_to_idx": s.patch_to_idx,
    }, out / "tier2_checkpoint.pt")
    (out / "lr_model.pkl").write_bytes(pickle.dumps(lr_results["model"]))
    (out / "summary.json").write_text(json.dumps({
        "n_champs": s.n_champs, "n_patches": s.n_patches,
        "train_patches": s.train_patches_summary,
        "val_patches": s.val_patches_summary,
        "test_patches": s.test_patches_summary,
        "blue_base_rate": s.blue_base_rate,
        "lr_val_log_loss": lr_results['val/log_loss'],   "lr_val_acc": lr_results['val/acc'],
        "lr_test_log_loss": lr_results['test/log_loss'], "lr_test_acc": lr_results['test/acc'],
        "ds_no_patch_val_log_loss": np_val['val/log_loss'],   "ds_no_patch_val_acc": np_val['val/acc'],
        "ds_no_patch_test_log_loss": np_test['test/log_loss'],"ds_no_patch_test_acc": np_test['test/acc'],
        "ds_patch_val_log_loss_raw": p_val['raw'][0],  "ds_patch_val_acc": p_val['raw'][1],
        "ds_patch_test_log_loss_raw": p_test['raw'][0],"ds_patch_test_acc": p_test['raw'][1],
        "ds_patch_val_log_loss_cal": p_val['cal'][0],
        "ds_patch_test_log_loss_cal": p_test['cal'][0],
        "temperature": T_val,
    }, indent=2))
    click.echo(f"[saved] {out}/tier2_checkpoint.pt  {out}/lr_model.pkl  {out}/summary.json")


if __name__ == "__main__":
    main()
