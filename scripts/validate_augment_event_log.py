"""Validate the Issue #16 augment offer/picked event data contract."""
from __future__ import annotations

import json
from pathlib import Path

import click

from aram_nn.augment_events import validate_jsonl


@click.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), help="Optional JSON summary output.")
def main(path: Path, out: Path | None) -> None:
    report = validate_jsonl(str(path))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    click.echo(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
