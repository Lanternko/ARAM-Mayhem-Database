from __future__ import annotations

import click
import uvicorn


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, type=int, show_default=True)
@click.option("--reload/--no-reload", default=False, show_default=True)
def main(host: str, port: int, reload: bool) -> None:
    """Run the ARAM Mayhem website API."""
    uvicorn.run("aram_nn.site.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
