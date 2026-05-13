#!/usr/bin/env python
"""Auto-rotating variant experiment runner. Designed to be called every 30 min.

Protocol (round-based):
  - Round boundary (next_variant_idx == 0): check if DB grew >=MIN_NEW_GAMES.
    If yes → export a *frozen* snapshot for this round, train variant 0.
    If no  → print leaderboard and exit (wait for more data).
  - Mid-round (next_variant_idx > 0): skip growth check, reuse frozen snapshot,
    train the next variant. All variants in a round train on identical data.
  - Round completion: wraps back to idx 0, growth check applies again.

Why single-patch and frozen snapshot:
  CLAUDE.md rule: never train on cross-patch data without patch feature.
  Frozen snapshot: comparing variants on different N_DB is noise, not signal.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
COLLECTOR_DB = Path(
    os.environ.get(
        "ARAM_NN_COLLECTOR_DB",
        str(ROOT / "data" / "lcu" / "games.db"),
    )
)
COLLECTOR_SCRIPT = Path(
    os.environ.get(
        "ARAM_NN_COLLECTOR_SCRIPT",
        str(ROOT / "scripts" / "lcu_collector.py"),
    )
)
DATA_DIR = ROOT / "data" / "raw"
MODELS_DIR = ROOT / "models"
LEADERBOARD = MODELS_DIR / "leaderboard.json"
FROZEN_PARQUET = DATA_DIR / "auto_frozen.parquet"
LOGS_DIR = ROOT / "logs"

PATCHES = ["16.10"]  # single patch — no cross-patch contamination
MIN_NEW_GAMES = 30   # min DB growth before a new round starts

VARIANTS: list[dict] = [
    # Low-regularization sweep — Codex finding: 12k games likely underfit at dropout>=0.2
    {"name": "embed4_h32_d00",       "embed_dim": 4,  "hidden": 32,  "dropout": 0.00, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    {"name": "embed8_h64_d01",        "embed_dim": 8,  "hidden": 64,  "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    {"name": "embed16_h64_d01",       "embed_dim": 16, "hidden": 64,  "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    {"name": "embed16_h128_d01",      "embed_dim": 16, "hidden": 128, "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    {"name": "embed32_h64_d01",       "embed_dim": 32, "hidden": 64,  "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    {"name": "embed32_h128_d01",      "embed_dim": 32, "hidden": 128, "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": True},
    # swap_aug=False ablations — architecture has antisymmetry hardcoded, aug may be redundant
    {"name": "embed8_h64_d01_naug",   "embed_dim": 8,  "hidden": 64,  "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": False},
    {"name": "embed16_h64_d01_naug",  "embed_dim": 16, "hidden": 64,  "dropout": 0.10, "weight_decay": 1e-3, "epochs": 150, "swap_aug": False},
]


def _count_mayhem() -> int:
    if not COLLECTOR_DB.exists():
        return 0
    try:
        con = sqlite3.connect(str(COLLECTOR_DB))
        row = con.execute("SELECT COUNT(*) FROM games WHERE queue_id=2400").fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[db] warning: {e}")
        return 0


def _load_lb() -> dict:
    if LEADERBOARD.exists():
        return json.loads(LEADERBOARD.read_text())
    return {"last_game_count": 0, "round": 0, "round_n_db": 0, "next_variant_idx": 0, "runs": []}


def _save_lb(lb: dict) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LEADERBOARD.write_text(json.dumps(lb, indent=2))


def _export_frozen(n_db: int) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(COLLECTOR_SCRIPT),
        "export",
        "--db", str(COLLECTOR_DB),
        "--queue", "2400",
        "--out", str(FROZEN_PARQUET),
        "--platform", "TW2",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        print(f"[export] ERROR: {r.stderr.strip()}")
        return False
    ok = FROZEN_PARQUET.exists()
    if ok:
        print(f"[export] frozen snapshot: {FROZEN_PARQUET.name}  ({n_db} Mayhem games)")
    return ok


def _train_variant(variant: dict) -> dict | None:
    out_dir = MODELS_DIR / f"auto_{variant['name']}"
    swap = variant.get("swap_aug", True)
    cmd = [
        sys.executable, "-m", "aram_nn.train",
        "--data", str(FROZEN_PARQUET),
        "--out", str(out_dir),
        "--embed-dim", str(variant["embed_dim"]),
        "--hidden",    str(variant["hidden"]),
        "--dropout",   str(variant["dropout"]),
        "--weight-decay", str(variant["weight_decay"]),
        "--epochs",    str(variant.get("epochs", 150)),
        "--patience",  "15",
        "--batch-size", "256",
        "--swap-aug",  "True" if swap else "False",
    ]
    for p in PATCHES:
        cmd += ["--patches", p]

    aug_tag = "" if swap else " noaug"
    print(
        f"\n[train] {variant['name']}{aug_tag}  "
        f"embed={variant['embed_dim']} h={variant['hidden']} "
        f"d={variant['dropout']} wd={variant['weight_decay']}\n"
    )
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"[train] FAILED (exit {r.returncode})")
        return None

    sp = out_dir / "summary.json"
    if not sp.exists():
        print("[train] summary.json missing after run")
        return None
    return json.loads(sp.read_text())


def _print_lb(lb: dict) -> None:
    runs = lb.get("runs", [])
    if not runs:
        print("[leaderboard] no completed runs yet\n")
        return
    sep = "=" * 78
    rnd = lb.get("round", 0)
    n = lb.get("round_n_db", lb.get("last_game_count", 0))
    print(f"\n{sep}")
    print(f"  LEADERBOARD  round={rnd}  n_db={n}  patch={PATCHES}  ({len(runs)} variants)")
    print(sep)
    hdr = f"  {'Variant':<26} {'swap':>4}  {'LR val':>7} {'LR tst':>7} {'DS val':>7} {'DS tst':>7}  {'Win':>3}"
    print(hdr)
    print(f"  {'-'*26} {'-'*4}  {'-'*7} {'-'*7} {'-'*7} {'-'*7}  {'-'*3}")
    for r in sorted(runs, key=lambda x: x.get("ds_val_acc", 0), reverse=True):
        bm = "LR" if r.get("best_model") == "lr" else "DS"
        aug = "Y" if r.get("swap_aug", True) else "N"
        print(
            f"  {r['variant']:<26} {aug:>4}  "
            f"{r.get('lr_val_acc', 0):>7.4f} {r.get('lr_test_acc', 0):>7.4f} "
            f"{r.get('ds_val_acc', 0):>7.4f} {r.get('ds_test_acc', 0):>7.4f}  {bm:>3}"
        )
    print(sep)
    print()


def main() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*50}")
    print(f"  [auto_train]  {ts}")
    print(f"{'='*50}")
    print(f"[paths] collector_db={COLLECTOR_DB}")
    print(f"[paths] frozen_parquet={FROZEN_PARQUET}")

    lb = _load_lb()
    idx = lb["next_variant_idx"] % len(VARIANTS)

    if idx == 0:
        # Round boundary: check data growth before starting new round
        n_db = _count_mayhem()
        last = lb.get("last_game_count", 0)
        delta = n_db - last
        print(f"[data] {n_db} Mayhem games  (round boundary, delta={delta:+d}, need >={MIN_NEW_GAMES})")
        if delta < MIN_NEW_GAMES:
            print(f"[skip] not enough new data to start round {lb.get('round', 0) + 1}")
            _print_lb(lb)
            return
        print(f"[round] starting round {lb.get('round', 0) + 1} with {n_db} games (patch={PATCHES})")
        if not _export_frozen(n_db):
            print("[abort] export failed")
            return
        lb["last_game_count"] = n_db
        lb["round"] = lb.get("round", 0) + 1
        lb["round_n_db"] = n_db
    else:
        # Mid-round: reuse frozen snapshot, no growth check
        n_db = lb.get("round_n_db", lb.get("last_game_count", 0))
        print(f"[data] mid-round {idx}/{len(VARIANTS)-1}, reusing frozen snapshot ({n_db} games)")
        if not FROZEN_PARQUET.exists():
            print("[warn] frozen parquet missing, re-exporting...")
            n_db = _count_mayhem()
            if not _export_frozen(n_db):
                print("[abort] export failed")
                return
            lb["round_n_db"] = n_db

    variant = VARIANTS[idx]
    lb["next_variant_idx"] = (idx + 1) % len(VARIANTS)
    _save_lb(lb)  # persist idx advance before training (crash-safe)

    summary = _train_variant(variant)

    if summary is not None:
        entry = {
            "variant":      variant["name"],
            "timestamp":    ts,
            "round":        lb.get("round", 1),
            "n_db":         n_db,
            "swap_aug":     variant.get("swap_aug", True),
            "lr_val_acc":   round(summary.get("lr_val_acc",     0.0), 6),
            "lr_test_acc":  round(summary.get("lr_test_acc",    0.0), 6),
            "ds_val_acc":   round(summary.get("tier1_val_acc",  0.0), 6),
            "ds_test_acc":  round(summary.get("tier1_test_acc", 0.0), 6),
            "best_model":   summary.get("best_model", "?"),
            "n_champs":     summary.get("n_champs", 0),
        }
        runs = lb.setdefault("runs", [])
        pos = next((i for i, r in enumerate(runs) if r["variant"] == variant["name"]), None)
        if pos is not None:
            runs[pos] = entry
        else:
            runs.append(entry)
        _save_lb(lb)

    _print_lb(lb)
    next_v = VARIANTS[lb["next_variant_idx"] % len(VARIANTS)]
    next_tag = "(round boundary — needs new data)" if lb["next_variant_idx"] % len(VARIANTS) == 0 else "(mid-round, fires next cycle)"
    print(f"[next] {next_v['name']}  {next_tag}\n")


if __name__ == "__main__":
    main()
