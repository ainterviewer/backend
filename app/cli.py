import json
from typing import Annotated, Optional

import typer
from typer import Typer

from . import __version__
from .main import app
from .utils import extend_openapi_schema

cli = Typer()


def version_callback(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@cli.callback()
def callback(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version", help="Show the version and exit.", callback=version_callback
        ),
    ] = None,
) -> None:
    pass


@cli.command()
def generate_openapi_scheme(output: str = "openapi.json"):
    from ainterviewer.interfaces import (
        OutgoingData,
        OutgoingHistoryMessage,
        OutgoingMessage,
        ReceivedData,
    )
    from ainterviewer.lpm.types import CustomTokens

    openapi = app.openapi()

    openapi["paths"] = {
        path: spec
        for path, spec in openapi["paths"].items()
        if path.startswith("/api/") or path.startswith("/ws/")
    }

    # TODO: Make sure that we should in fact extend the openapi schema, and not
    # just export them as separately
    openapi = extend_openapi_schema(
        openapi,
        models=[
            OutgoingData,
            OutgoingHistoryMessage,
            OutgoingMessage,
            ReceivedData,
            CustomTokens,
        ],
    )

    with open(output, "w") as f:
        f.write(json.dumps(openapi, indent=4))


if __name__ == "__main__":
    cli()
