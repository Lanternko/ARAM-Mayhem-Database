"""Apples-to-apples: same val/test, train on 16.10 only vs 16.9+16.10 combined.

Test/Val: last 30% of 16.10 (chronological cut).
Train_A: first 70% of 16.10 only.
Train_B: all of 16.9 + first 70% of 16.10.
"""
from __future__ import annotations

from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader

from aram_nn.data import ARAMDataset
from aram_nn.eval import log_loss_np, accuracy_np, ece_np
from aram_nn.models.deepsets import DeepSetsARAM
from aram_nn.models.logreg import train_and_eval as lr_train_eval


def make_loader(ds, bs, shuffle):
    return DataLoader(ds, batch_size=bs, shuffle=shuffle, drop_last=False)


def time_split_16_10(df: pl.DataFrame, val_frac=0.15, test_frac=0.15):
    df = df.with_columns(
        pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p")
    )
    df_16_10 = df.filter(pl.col("p") == "16.10").sort("game_creation_ms")
    df_16_9  = df.filter(pl.col("p") == "16.9").sort("game_creation_ms")

    n = df_16_10.height
    n_test = int(n * test_frac)
    n_val  = int(n * val_frac)
    n_train = n - n_test - n_val

    train_16_10 = df_16_10.slice(0, n_train)
    val   = df_16_10.slice(n_train, n_val)
    test  = df_16_10.slice(n_train + n_val, n_test)

    return df_16_9, train_16_10, val, test


def build_vocab(df: pl.DataFrame):
    ids: set[int] = set()
    for row in df["blue_champions"].to_list(): ids.update(row)
    for row in df["red_champions"].to_list():  ids.update(row)
    return {cid: i for i, cid in enumerate(sorted(ids))}


def filter_known(df, known):
    mask = (
        df["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        & df["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
    )
    return df.filter(mask)


def train_deepsets(model, train_loader, val_loader, device, epochs, lr, wd, patience, eval_every=5, swap_aug=True):
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=wd)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs)
    crit = nn.BCEWithLogitsLoss()
    best_ll = float("inf")
    best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    no_imp = 0
    for epoch in range(1, epochs + 1):
        model.train()
        tl, ns = 0.0, 0
        for blue, red, y in train_loader:
            blue, red, y = blue.to(device), red.to(device), y.to(device)
            if swap_aug:
                m = torch.rand(blue.size(0), device=device) < 0.5
                bs = torch.where(m.unsqueeze(1), red, blue)
                rs = torch.where(m.unsqueeze(1), blue, red)
                ys = torch.where(m, 1.0 - y, y)
                blue, red, y = bs, rs, ys
            optimizer.zero_grad()
            logits = model(blue, red)
            loss = crit(logits, y)
            loss.backward()
            optimizer.step()
            tl += loss.item() * blue.size(0)
            ns += blue.size(0)
        scheduler.step()

        if epoch % eval_every == 0 or epoch == 1:
            model.eval()
            with torch.no_grad():
                lls, accs, n = [], [], 0
                ys, ps = [], []
                for blue, red, y in val_loader:
                    blue, red = blue.to(device), red.to(device)
                    p = torch.sigmoid(model(blue, red)).cpu().numpy()
                    ps.append(p); ys.append(y.numpy())
                ps = np.concatenate(ps); ys = np.concatenate(ys)
                ll = log_loss_np(ys, ps)
            if ll < best_ll:
                best_ll = ll
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                no_imp = 0
            else:
                no_imp += 1
                if no_imp >= patience:
                    break
    model.load_state_dict(best_state)
    return model, best_ll


def eval_ds(model, loader, device):
    model.eval()
    ps, ys = [], []
    with torch.no_grad():
        for blue, red, y in loader:
            blue, red = blue.to(device), red.to(device)
            p = torch.sigmoid(model(blue, red)).cpu().numpy()
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

    df_train_b = pl.concat([df_16_9.with_columns(pl.col("game_creation_ms")), df_train_a]).sort("game_creation_ms")

    click.echo(f"\n[data]")
    click.echo(f"  16.9 (all):        n={df_16_9.height}")
    click.echo(f"  16.10 train (A):   n={df_train_a.height}")
    click.echo(f"  Combined train(B): n={df_train_b.height}  = 16.9 ({df_16_9.height}) + 16.10_train ({df_train_a.height})")
    click.echo(f"  val (16.10):       n={df_val.height}")
    click.echo(f"  test (16.10):      n={df_test.height}")

    blue_br_a = float(df_train_a["blue_wins"].mean())
    blue_br_b = float(df_train_b["blue_wins"].mean())
    click.echo(f"  blue_base_rate: A={blue_br_a:.3f}  B={blue_br_b:.3f}  val={df_val['blue_wins'].mean():.3f}  test={df_test['blue_wins'].mean():.3f}")

    # Constant baseline on val
    bl_val_a = log_loss_np(df_val["blue_wins"].cast(pl.Float32).to_numpy(), np.full(df_val.height, blue_br_a))
    bl_val_b = log_loss_np(df_val["blue_wins"].cast(pl.Float32).to_numpy(), np.full(df_val.height, blue_br_b))
    bl_test_a = log_loss_np(df_test["blue_wins"].cast(pl.Float32).to_numpy(), np.full(df_test.height, blue_br_a))
    bl_test_b = log_loss_np(df_test["blue_wins"].cast(pl.Float32).to_numpy(), np.full(df_test.height, blue_br_b))

    results = []

    for tag, df_train, bl_val, bl_test in [
        ("A: 16.10 only", df_train_a, bl_val_a, bl_test_a),
        ("B: 16.9+16.10", df_train_b, bl_val_b, bl_test_b),
    ]:
        click.echo(f"\n==================== {tag} ====================")
        c2i = build_vocab(df_train)
        df_val_f  = filter_known(df_val,  set(c2i))
        df_test_f = filter_known(df_test, set(c2i))
        click.echo(f"  n_champs={len(c2i)}  val_kept={df_val_f.height}/{df_val.height}  test_kept={df_test_f.height}/{df_test.height}")

        train_ds = ARAMDataset(df_train, c2i)
        val_ds   = ARAMDataset(df_val_f, c2i)
        test_ds  = ARAMDataset(df_test_f, c2i)

        # --- LR ---
        click.echo(f"\n  [LR] training ({tag})...")
        lr_res = lr_train_eval(train_ds, val_ds, test_ds, len(c2i))
        click.echo(
            f"    best C={lr_res['best_C']}  "
            f"val acc={lr_res['val/acc']:.4f}  log_loss={lr_res['val/log_loss']:.4f}  |  "
            f"test acc={lr_res['test/acc']:.4f}  log_loss={lr_res['test/log_loss']:.4f}"
        )
        results.append({"train": tag, "model": "Constant", "val_ll": bl_val, "val_acc": float(df_val["blue_wins"].mean() >= 0.5 if False else (df_val["blue_wins"].cast(pl.Float32).to_numpy() == (np.full(df_val.height, blue_val := float(df_val["blue_wins"].mean())) >= 0.5)).mean()), "test_ll": bl_test, "test_acc": float((df_test["blue_wins"].cast(pl.Float32).to_numpy() == (np.full(df_test.height, blue_val) >= 0.5)).mean())})
        results.append({"train": tag, "model": "LR",       "val_ll": lr_res['val/log_loss'], "val_acc": lr_res['val/acc'], "test_ll": lr_res['test/log_loss'], "test_acc": lr_res['test/acc']})

        # --- DeepSets (default) ---
        click.echo(f"  [DeepSets default] training ({tag})...")
        model = DeepSetsARAM(len(c2i), embed_dim=32, hidden=64, dropout=0.1).to(device)
        model, _ = train_deepsets(
            model,
            make_loader(train_ds, 256, True), make_loader(val_ds, 256, False),
            device, epochs=epochs, lr=3e-3, wd=1e-3, patience=patience,
        )
        v = eval_ds(model, make_loader(val_ds, 256, False), device)
        t = eval_ds(model, make_loader(test_ds, 256, False), device)
        click.echo(f"    val acc={v['acc']:.4f}  log_loss={v['log_loss']:.4f}  |  test acc={t['acc']:.4f}  log_loss={t['log_loss']:.4f}")
        results.append({"train": tag, "model": "DeepSets-default", "val_ll": v['log_loss'], "val_acc": v['acc'], "test_ll": t['log_loss'], "test_acc": t['acc']})

        # --- DeepSets (heavy reg) ---
        click.echo(f"  [DeepSets reg] training ({tag})...")
        model = DeepSetsARAM(len(c2i), embed_dim=16, hidden=32, dropout=0.4).to(device)
        model, _ = train_deepsets(
            model,
            make_loader(train_ds, 256, True), make_loader(val_ds, 256, False),
            device, epochs=epochs, lr=2e-3, wd=5e-2, patience=patience,
        )
        v = eval_ds(model, make_loader(val_ds, 256, False), device)
        t = eval_ds(model, make_loader(test_ds, 256, False), device)
        click.echo(f"    val acc={v['acc']:.4f}  log_loss={v['log_loss']:.4f}  |  test acc={t['acc']:.4f}  log_loss={t['log_loss']:.4f}")
        results.append({"train": tag, "model": "DeepSets-reg",     "val_ll": v['log_loss'], "val_acc": v['acc'], "test_ll": t['log_loss'], "test_acc": t['acc']})

    # ---- Final ----
    click.echo("\n\n==================== SUMMARY ====================")
    click.echo("Same val/test (last 30% of 16.10).  A=16.10 only, B=16.9+16.10.")
    click.echo()
    headers = ["train", "model", "val log_loss", "val acc", "test log_loss", "test acc"]
    fmt = []
    for r in results:
        fmt.append([
            r["train"], r["model"],
            f"{r['val_ll']:.4f}",  f"{r['val_acc']:.4f}",
            f"{r['test_ll']:.4f}", f"{r['test_acc']:.4f}",
        ])
    col_w = [max(len(h), max(len(rr[i]) for rr in fmt)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(col_w) + 2 * (len(headers) - 1)))
    for rr in fmt:
        click.echo("  " + "  ".join(rr[i].ljust(col_w[i]) for i in range(len(headers))))


if __name__ == "__main__":
    main()
