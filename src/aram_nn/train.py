"""Training script: LR baseline → Tier 1 DeepSets → temperature scaling → final eval.

Usage:
  python -m aram_nn.train \
    --data data/raw/tw_aram_all_patch.parquet \
    --patches 15.19 15.20 15.21 15.22 \
    --out models/tier1_run1
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import click
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from tqdm import tqdm

from aram_nn.data import load_splits, make_loader
from aram_nn.eval import TemperatureScaler, evaluate, log_loss_np
from aram_nn.models.deepsets import DeepSetsARAM
from aram_nn.models.logreg import train_and_eval as lr_train_eval


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patches", multiple=True, default=(), show_default=True, help="Patch prefixes to include (e.g. 15.19); omit for all patches")
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--hidden", default=64, show_default=True)
@click.option("--dropout", default=0.1, show_default=True, type=float)
@click.option("--lr", default=3e-3, show_default=True, type=float)
@click.option("--epochs", default=80, show_default=True, type=int)
@click.option("--batch-size", default=256, show_default=True, type=int)
@click.option("--patience", default=10, show_default=True, type=int)
@click.option("--swap-aug", default=True, show_default=True, type=bool, help="Swap teams as consistency aug")
@click.option("--weight-decay", default=1e-3, show_default=True, type=float)
@click.option("--seed", default=42, show_default=True, type=int)
def main(
    data: Path, patches: tuple[str, ...], out: Path,
    embed_dim: int, hidden: int, dropout: float, lr: float,
    epochs: int, batch_size: int, patience: int,
    swap_aug: bool, weight_decay: float, seed: int,
):
    torch.manual_seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    # ---- Data ----
    splits = load_splits(data, patches=list(patches))
    click.echo(
        f"[data] train={len(splits.train)}  val={len(splits.val)}  test={len(splits.test)}"
        f"  n_champs={splits.n_champs}  blue_base_rate={splits.blue_base_rate:.3f}"
    )

    train_loader = make_loader(splits.train, batch_size, shuffle=True)
    val_loader   = make_loader(splits.val,   batch_size, shuffle=False)
    test_loader  = make_loader(splits.test,  batch_size, shuffle=False)

    # ---- Constant baseline ----
    base_rate = splits.blue_base_rate
    import numpy as np
    dummy_probs = np.full(len(splits.val), base_rate)
    dummy_labels = splits.val.labels
    bl_ll = log_loss_np(dummy_labels, dummy_probs)
    click.echo(f"\n[baseline/constant]  val log_loss={bl_ll:.4f}  (always predict {base_rate:.3f})")

    # ---- LR baseline ----
    click.echo("\n[LR baseline] training...")
    lr_results = lr_train_eval(splits.train, splits.val, splits.test, splits.n_champs)
    click.echo(
        f"  best C={lr_results['best_C']}\n"
        f"  train log_loss={lr_results['train/log_loss']:.4f}  acc={lr_results['train/acc']:.4f}\n"
        f"  val   log_loss={lr_results['val/log_loss']:.4f}  acc={lr_results['val/acc']:.4f}\n"
        f"  test  log_loss={lr_results['test/log_loss']:.4f}  acc={lr_results['test/acc']:.4f}"
    )

    # ---- Tier 1: DeepSets ----
    click.echo("\n[Tier 1] training DeepSets...")
    model = DeepSetsARAM(splits.n_champs, embed_dim, hidden, dropout).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    click.echo(f"  params={n_params:,}")

    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.BCEWithLogitsLoss()

    best_val_ll = float("inf")
    # initialise with current (random-init) state so best_state is never None
    best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    epochs_no_improve = 0   # counts eval checks, not raw epochs
    eval_every = 5

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, n_seen = 0.0, 0
        for blue, red, y in train_loader:
            blue, red, y = blue.to(device), red.to(device), y.to(device)

            if swap_aug:
                # 50% chance swap teams + flip label (consistency regularisation)
                mask = torch.rand(blue.size(0), device=device) < 0.5
                blue_s = torch.where(mask.unsqueeze(1), red, blue)
                red_s  = torch.where(mask.unsqueeze(1), blue, red)
                y_s    = torch.where(mask, 1.0 - y, y)
                blue, red, y = blue_s, red_s, y_s

            optimizer.zero_grad()
            logits = model(blue, red)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * blue.size(0)
            n_seen += blue.size(0)
        scheduler.step()

        if epoch % eval_every == 0 or epoch == 1:
            val_metrics = evaluate(model, val_loader, device, "val")
            val_ll = val_metrics["val/log_loss"]
            click.echo(
                f"  epoch {epoch:3d}  train_loss={total_loss/n_seen:.4f}"
                f"  val_log_loss={val_ll:.4f}  val_acc={val_metrics['val/acc']:.4f}"
            )
            if val_ll < best_val_ll:
                best_val_ll = val_ll
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                epochs_no_improve = 0
            else:
                epochs_no_improve += 1
                if epochs_no_improve >= patience:
                    click.echo(f"  early stop at epoch {epoch} ({patience} checks without improvement)")
                    break

    model.load_state_dict(best_state)
    click.echo(f"\n[Tier 1] best val log_loss={best_val_ll:.4f}")

    # ---- Temperature scaling ----
    click.echo("\n[calibration] temperature scaling on val...")
    scaler = TemperatureScaler(model).fit(val_loader, device)
    T = scaler.temperature.item()
    click.echo(f"  T={T:.4f}")

    # ---- Final eval ----
    click.echo("\n[results]")
    headers = ["split", "model", "log_loss", "acc", "ece"]
    rows = []
    for loader, split_name in [(val_loader, "val"), (test_loader, "test")]:
        raw = evaluate(model,  loader, device, split_name)
        cal = evaluate(scaler, loader, device, split_name)
        rows.append([split_name, "DeepSets (raw)",  f"{raw[f'{split_name}/log_loss']:.4f}", f"{raw[f'{split_name}/acc']:.4f}", f"{raw[f'{split_name}/ece']:.4f}"])
        rows.append([split_name, "DeepSets (cal.)", f"{cal[f'{split_name}/log_loss']:.4f}", f"{cal[f'{split_name}/acc']:.4f}", f"{cal[f'{split_name}/ece']:.4f}"])

    col_w = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    header_str = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))
    click.echo("  " + header_str)
    click.echo("  " + "-" * len(header_str))
    for row in rows:
        click.echo("  " + "  ".join(v.ljust(col_w[i]) for i, v in enumerate(row)))

    # ---- Save ----
    out.mkdir(parents=True, exist_ok=True)
    torch.save({"model": model.state_dict(), "temperature": T, "champ_to_idx": splits.champ_to_idx}, out / "checkpoint.pt")

    # Save LR model (often best on small datasets)
    (out / "lr_model.pkl").write_bytes(pickle.dumps(lr_results["model"]))
    champ_idx_path = out / "champ_to_idx.json"
    champ_idx_path.write_text(json.dumps({str(k): int(v) for k, v in splits.champ_to_idx.items()}))

    lr_val_acc = lr_results["val/acc"]
    ds_val_acc  = evaluate(model, val_loader,  device, "val")["val/acc"]
    ds_test_acc = evaluate(model, test_loader, device, "test")["test/acc"]
    best_model = "lr" if lr_val_acc >= ds_val_acc else "deepsets"
    click.echo(f"\n[best model] {best_model.upper()}  (lr_val_acc={lr_val_acc:.4f}  ds_val_acc={ds_val_acc:.4f}  ds_test_acc={ds_test_acc:.4f})")

    summary = {
        "n_champs": splits.n_champs, "n_params": n_params,
        "patches": list(patches), "blue_base_rate": base_rate,
        "best_model": best_model,
        "lr_val_log_loss": lr_results["val/log_loss"],
        "lr_val_acc": lr_val_acc,
        "lr_test_acc": lr_results["test/acc"],
        "tier1_best_val_log_loss": best_val_ll,
        "tier1_val_acc": ds_val_acc,
        "tier1_test_acc": ds_test_acc,
        "temperature": T,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    click.echo(f"[saved] {out}/checkpoint.pt  {out}/lr_model.pkl  {out}/summary.json")


if __name__ == "__main__":
    main()
