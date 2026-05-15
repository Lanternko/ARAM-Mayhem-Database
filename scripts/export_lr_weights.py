"""Extract LR coefficients from a trained pickle to a JSON file.

Why have this at all when the recommender can read pickles directly:
  The JSON form is smaller (~4 KB vs ~10 KB), human-inspectable, and
  loads slightly faster.  Useful for shipping the model to environments
  that don't even have numpy's binary-array unpickling path warmed up.

Uses the same sklearn-free unpickler as aram_nn.recommend, so this script
itself never imports sklearn and won't trip the scipy MemoryError on
Python 3.13.

Usage:
  python scripts/export_lr_weights.py \
      --lr-model models/tier2_mayhem/lr_model.pkl \
      --out      models/tier2_mayhem/lr_weights.json
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from aram_nn.recommend import _load_pickle_no_sklearn


@click.command()
@click.option("--lr-model", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path))
def main(lr_model: Path, out: Path) -> None:
    click.echo(f"[export] reading {lr_model} (sklearn-free)")
    coef, intercept = _load_pickle_no_sklearn(lr_model)

    payload = {"coef": coef.tolist(), "intercept": intercept}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload))
    click.echo(f"[saved] {out}  ({coef.shape[0]} coefficients, intercept={intercept:+.4f})")


if __name__ == "__main__":
    main()
