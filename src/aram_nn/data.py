"""Data loading, champion vocab, Dataset, and time-based train/val/test split."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import torch
from torch.utils.data import Dataset, DataLoader


# ---------- Champion vocab ----------

def build_vocab(df: pl.DataFrame) -> dict[int, int]:
    """Map raw Riot championId -> 0-based index, sorted for determinism."""
    ids: set[int] = set()
    for row in df["blue_champions"].to_list():
        ids.update(row)
    for row in df["red_champions"].to_list():
        ids.update(row)
    return {cid: idx for idx, cid in enumerate(sorted(ids))}


# ---------- Dataset ----------

class ARAMDataset(Dataset):
    def __init__(self, df: pl.DataFrame, champ_to_idx: dict[int, int]):
        self.blue = [[champ_to_idx[c] for c in row] for row in df["blue_champions"].to_list()]
        self.red  = [[champ_to_idx[c] for c in row] for row in df["red_champions"].to_list()]
        self.labels = df["blue_wins"].cast(pl.Float32).to_numpy()

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, i):
        blue = torch.tensor(self.blue[i], dtype=torch.long)
        red  = torch.tensor(self.red[i],  dtype=torch.long)
        y    = torch.tensor(self.labels[i], dtype=torch.float32)
        return blue, red, y


# ---------- Loading + split ----------

@dataclass
class Splits:
    train: ARAMDataset
    val:   ARAMDataset
    test:  ARAMDataset
    champ_to_idx: dict[int, int]
    n_champs: int
    blue_base_rate: float  # constant baseline for this split


def load_splits(
    parquet_path: str | Path,
    patches: list[str] | None = None,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
    min_duration: int = 300,
) -> Splits:
    df = pl.read_parquet(parquet_path)

    # Filter patches
    if patches:
        prefix_col = (
            df["patch"]
            .str.split(".")
            .list.slice(0, 2)
            .list.join(".")
        )
        df = df.filter(prefix_col.is_in(patches))

    # Basic quality filter
    df = df.filter(pl.col("duration_sec") >= min_duration)

    if df.height == 0:
        raise ValueError(f"No rows after filtering (patches={patches})")

    # Time-based split — sort ascending, cut from tail
    df = df.sort("game_creation_ms")
    n = df.height
    n_test = max(1, int(n * test_frac))
    n_val  = max(1, int(n * val_frac))
    n_train = n - n_test - n_val

    df_train = df.slice(0, n_train)
    df_val   = df.slice(n_train, n_val)
    df_test  = df.slice(n_train + n_val, n_test)

    # Vocab built only from train set.
    # Rows in val/test with champions unseen in train are dropped to avoid KeyError.
    champ_to_idx = build_vocab(df_train)
    known = set(champ_to_idx.keys())

    def _filter_known(d: pl.DataFrame) -> pl.DataFrame:
        mask = (
            d["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
            & d["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        )
        return d.filter(mask)

    df_val  = _filter_known(df_val)
    df_test = _filter_known(df_test)

    return Splits(
        train=ARAMDataset(df_train, champ_to_idx),
        val=ARAMDataset(df_val, champ_to_idx),
        test=ARAMDataset(df_test, champ_to_idx),
        champ_to_idx=champ_to_idx,
        n_champs=len(champ_to_idx),
        blue_base_rate=float(df_train["blue_wins"].mean()),
    )


def make_loader(dataset: ARAMDataset, batch_size: int, shuffle: bool) -> DataLoader:
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, drop_last=False)
