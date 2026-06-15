"""Head-to-head: DeepSets variants vs the pooled LR, SAME split (+ multi-seed).

On train_composition_lr_pooled's EXACT time split + vocab (only the model
changes), compares ways to make DeepSets track the current patch:

  - flat                 train on all pooled games, uniform weight (full volume)
  - recency              exp recency-weighted from scratch (gentler lr)
  - pretrain->finetune   pretrain flat on ALL games for rich embeddings, then
                         fine-tune on the most-recent slice at low lr.  Gets
                         volume AND recency, which plain recency-weighting can't.

Pass --seeds 0,1,2,3,4 (and usually --skip-recency, which is settled to hurt) to
get mean±sd across seeds — to check whether DeepSets' edge over the LR survives
seed noise.

DeepSets is champion-identity only (learned embeddings, antisymmetric); the LR
it is compared against additionally uses engineered composition features.

  python scripts/train_deepsets_pooled.py --data data/raw/mayhem_pooled_auto.parquet
  python scripts/train_deepsets_pooled.py --data <uncapped> --seeds 0,1,2,3,4 --skip-recency
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_ability_nn import TeamDataset, build_vocab  # noqa: E402
from train_composition_lr_pooled import patch_prefix_col, filter_known  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aram_nn.models.deepsets import DeepSetsARAM  # noqa: E402

DAY_MS = 86_400_000


def metrics(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    p = np.clip(p, 1e-7, 1.0 - 1e-7)
    ll = float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))
    acc = float(np.mean((p >= 0.5) == (y >= 0.5)))
    return ll, acc


def tensors(df: pl.DataFrame, champ_to_idx: dict[int, int]):
    ds = TeamDataset(df, champ_to_idx)
    return (torch.tensor(ds.blue, dtype=torch.long),
            torch.tensor(ds.red, dtype=torch.long),
            torch.tensor(ds.labels, dtype=torch.float32))


def build_model(n_champs, embed_dim, hidden, dropout, seed):
    torch.manual_seed(seed)
    return DeepSetsARAM(n_champs, embed_dim=embed_dim, hidden=hidden, dropout=dropout)


def fit(model, tr, va, *, weighted, w_tr, epochs, batch, lr, wd, patience, tag):
    blue_tr, red_tr, y_tr = tr
    blue_va, red_va, y_va = va
    y_va_np = y_va.numpy()
    N = blue_tr.size(0)
    wt = w_tr if (weighted and w_tr is not None) else torch.ones(N, dtype=torch.float32)

    opt = AdamW(model.parameters(), lr=lr, weight_decay=wd)
    sched = CosineAnnealingLR(opt, T_max=max(epochs, 1))
    bce = nn.BCEWithLogitsLoss(reduction="none")
    best_ll, best_state, no_improve, last_epoch = float("inf"), None, 0, 0

    for epoch in range(1, epochs + 1):
        last_epoch = epoch
        model.train()
        perm = torch.randperm(N)
        for i in range(0, N, batch):
            idx = perm[i:i + batch]
            b, r, y, wb = blue_tr[idx], red_tr[idx], y_tr[idx], wt[idx]
            m = torch.rand(b.size(0)) < 0.5          # swap-team aug; weight = game's, unchanged
            mb = m.unsqueeze(1)
            b, r, y = torch.where(mb, r, b), torch.where(mb, b, r), torch.where(m, 1.0 - y, y)
            opt.zero_grad()
            per = bce(model(b, r), y)
            loss = (per * wb).sum() / wb.sum()
            loss.backward()
            opt.step()
        sched.step()

        model.eval()
        with torch.no_grad():
            pv = torch.sigmoid(model(blue_va, red_va)).numpy()
        ll, _ = metrics(y_va_np, pv)
        if ll < best_ll - 1e-5:
            best_ll, best_state, no_improve = ll, {k: v.clone() for k, v in model.state_dict().items()}, 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    return best_ll, last_epoch


def evaluate(model, te) -> tuple[float, float]:
    model.eval()
    with torch.no_grad():
        p = torch.sigmoid(model(te[0], te[1])).numpy()
    return metrics(te[2].numpy(), p)


def lr_reference(summary_path: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    if not summary_path.exists():
        return out
    try:
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        for k, v in (s.get("results") or {}).items():
            a = v.get("all") or {}
            if "acc" in a and "log_loss" in a:
                out[f"LR/{k}"] = (float(a["log_loss"]), float(a["acc"]))
    except Exception:
        pass
    return out


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--current-patch", default="16.12", show_default=True)
@click.option("--holdout", default=12000, show_default=True)
@click.option("--val-size", default=15000, show_default=True)
@click.option("--half-life-days", default=7.0, show_default=True)
@click.option("--epochs", default=80, show_default=True)
@click.option("--batch", default=4096, show_default=True)
@click.option("--lr", default=3e-3, show_default=True, type=float)
@click.option("--recency-lr", default=1e-3, show_default=True, type=float)
@click.option("--recency-patience", default=10, show_default=True)
@click.option("--finetune-games", default=50000, show_default=True)
@click.option("--finetune-val", default=5000, show_default=True)
@click.option("--finetune-lr", default=5e-4, show_default=True, type=float)
@click.option("--finetune-epochs", default=40, show_default=True)
@click.option("--finetune-patience", default=8, show_default=True)
@click.option("--weight-decay", default=5e-3, show_default=True, type=float)
@click.option("--dropout", default=0.2, show_default=True, type=float)
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--hidden", default=64, show_default=True)
@click.option("--patience", default=6, show_default=True)
@click.option("--seed", default=42, show_default=True)
@click.option("--seeds", default="", help="Comma-separated seeds for multi-seed confirmation; overrides --seed.")
@click.option("--skip-recency/--with-recency", default=False, show_default=True,
              help="Skip the recency-from-scratch variant (settled: it hurts the NN).")
@click.option("--lr-summary", default=Path("models/composition_lr_pooled_recency_7d/summary.json"),
              type=click.Path(path_type=Path), show_default=True)
@click.option("--out", default=Path("outputs/deepsets_pooled_compare.json"),
              type=click.Path(path_type=Path), show_default=True)
def main(data, current_patch, holdout, val_size, half_life_days, epochs, batch, lr,
         recency_lr, recency_patience, finetune_games, finetune_val, finetune_lr,
         finetune_epochs, finetune_patience, weight_decay, dropout, embed_dim, hidden,
         patience, seed, seeds, skip_recency, lr_summary, out):
    torch.set_num_threads(os.cpu_count() or 4)

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = patch_prefix_col(df).sort("game_creation_ms")
    cur = df.filter(pl.col("pp") == current_patch)
    non_cur = df.filter(pl.col("pp") != current_patch)
    if cur.height <= holdout + 100:
        raise click.ClickException(f"{current_patch} has {cur.height} games (<= holdout)")

    test_df = cur.tail(holdout)
    early_cur = cur.head(cur.height - holdout)
    val_df = non_cur.tail(val_size)
    train_df = pl.concat([non_cur.head(non_cur.height - val_size), early_cur]).sort("game_creation_ms")

    champ_to_idx = build_vocab(train_df)
    known = set(champ_to_idx)
    n_champs = len(champ_to_idx)
    val_df = filter_known(val_df, known)
    test_df = filter_known(test_df, known)
    click.echo(f"[data] train={train_df.height} val={val_df.height} test={test_df.height} "
               f"champs={n_champs} early_cur={early_cur.height} threads={torch.get_num_threads()}")

    tr = tensors(train_df, champ_to_idx)
    va = tensors(val_df, champ_to_idx)
    te = tensors(test_df, champ_to_idx)

    t = train_df["game_creation_ms"].to_numpy().astype(np.float64)
    w = np.exp(-(t.max() - t) / (half_life_days * DAY_MS))
    w *= len(w) / w.sum()
    w_tr = torch.tensor(w, dtype=torch.float32)

    arch = dict(embed_dim=embed_dim, hidden=hidden, dropout=dropout)
    seed_list = [int(s) for s in seeds.split(",") if s.strip()] or [seed]

    # fine-tune recent split (seed-independent): most-recent (K+V) train games,
    # early-stop on the last V (adjacent to the test) so the stop signal matches.
    K, V = finetune_games, finetune_val
    span = min(K + V, tr[0].size(0))
    rb, rr, ry = tr[0][-span:], tr[1][-span:], tr[2][-span:]
    ft_tr = (rb[:-V], rr[:-V], ry[:-V])
    ft_va = (rb[-V:], rr[-V:], ry[-V:])

    per_seed: dict[str, dict[str, list[float]]] = {}

    def record(name, acc, ll):
        d = per_seed.setdefault(name, {"acc": [], "ll": []})
        d["acc"].append(acc)
        d["ll"].append(ll)

    t0 = time.time()
    for sd in seed_list:
        np.random.seed(sd)
        m_flat = build_model(n_champs, **arch, seed=sd)
        fit(m_flat, tr, va, weighted=False, w_tr=None, epochs=epochs, batch=batch,
            lr=lr, wd=weight_decay, patience=patience, tag=f"flat.s{sd}")
        ll, acc = evaluate(m_flat, te)
        record("deepsets_flat", acc, ll)
        click.echo(f"[seed {sd}] flat              {acc*100:.2f}% / {ll:.4f}")

        if not skip_recency:
            m_rec = build_model(n_champs, **arch, seed=sd)
            fit(m_rec, tr, va, weighted=True, w_tr=w_tr, epochs=epochs, batch=batch,
                lr=recency_lr, wd=weight_decay, patience=recency_patience, tag=f"recency.s{sd}")
            ll, acc = evaluate(m_rec, te)
            record(f"deepsets_recency_{half_life_days:g}d", acc, ll)
            click.echo(f"[seed {sd}] recency           {acc*100:.2f}% / {ll:.4f}")

        m_ft = build_model(n_champs, **arch, seed=sd)
        m_ft.load_state_dict(m_flat.state_dict())
        fit(m_ft, ft_tr, ft_va, weighted=False, w_tr=None, epochs=finetune_epochs, batch=batch,
            lr=finetune_lr, wd=weight_decay, patience=finetune_patience, tag=f"finetune.s{sd}")
        ll, acc = evaluate(m_ft, te)
        record("deepsets_pretrain_finetune", acc, ll)
        click.echo(f"[seed {sd}] pretrain_finetune  {acc*100:.2f}% / {ll:.4f}\n")

    lr_ref = lr_reference(Path(lr_summary))

    def agg(xs):
        return sum(xs) / len(xs), (st.pstdev(xs) if len(xs) > 1 else 0.0)

    click.echo(f"[multi-seed]  seeds={seed_list}  same parquet/split/vocab, same {test_df.height} {current_patch} test")
    click.echo(f"  {'model':<28}{'test acc (mean±sd)':>22}{'test ll':>10}")
    click.echo("  " + "-" * 60)
    for k, (l, a) in lr_ref.items():
        click.echo(f"  {k:<28}{a*100:>20.2f}%  {l:>8.4f}")
    summary = {}
    for name, d in per_seed.items():
        am, asd = agg(d["acc"])
        lm, lsd = agg(d["ll"])
        summary[name] = {"acc_mean": am, "acc_std": asd, "ll_mean": lm, "ll_std": lsd,
                         "acc_per_seed": d["acc"], "ll_per_seed": d["ll"]}
        click.echo(f"  {name:<28}{am*100:>16.2f}±{asd*100:.2f}%  {lm:>8.4f}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "data": str(data), "current_patch": current_patch, "seeds": seed_list,
        "split": {"train": train_df.height, "val": val_df.height, "test": test_df.height},
        "half_life_days": half_life_days, "n_champs": n_champs,
        "elapsed_sec": round(time.time() - t0, 1),
        "deepsets": summary,
        "lr_reference": {k: {"log_loss": l, "acc": a} for k, (l, a) in lr_ref.items()},
    }, indent=2), encoding="utf-8")
    click.echo(f"\nwrote {out}  ({round(time.time() - t0, 1)}s)")


if __name__ == "__main__":
    main()
