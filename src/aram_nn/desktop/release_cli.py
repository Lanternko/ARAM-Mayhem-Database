from __future__ import annotations

from pathlib import Path
import click

from .release import publish_data


@click.command()
@click.option("--input-root", type=click.Path(path_type=Path), default=Path("."))
@click.option("--tag", default="latest", show_default=True, help="Latest stable desktop release or an explicit tag")
@click.option("--region", required=True)
@click.option("--time-cutoff", required=True)
@click.option("--output", type=click.Path(path_type=Path), default=Path("outputs/desktop-publish"))
def main(input_root: Path, tag: str, region: str, time_cutoff: str, output: Path) -> None:
    """Publish verified desktop data; preserve the release's application binary."""
    manifest = publish_data(input_root / "models/composition_lr_pooled_recency_7d",
                            input_root / "data/cache/champion_abilities.json",
                            input_root / "docs/api/tier-list.json", output,
                            region=region, time_cutoff=time_cutoff, tag=tag)
    click.echo(f"Published patch={manifest['data']['current_patch']} data={manifest['data']['version']}")
