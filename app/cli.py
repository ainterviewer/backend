import json
from typing import Annotated, Optional

import typer
from typer import Typer

from ainterviewer.settings import Settings as LibSettings
from app.platform_release import PlatformManifest

from . import __version__
from .main import app
from .openapi import build_openapi_schema
from .settings import Settings
from .utils import merge_config_schemas

cli = Typer(
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


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
    """Write the SDK-facing OpenAPI schema to a file.

    The frontend normally generates its SDK straight from the running backend's
    `/api/openapi.json`; this command is the offline equivalent.
    """
    with open(output, "w") as f:
        f.write(json.dumps(build_openapi_schema(app), indent=4))


@cli.command()
def export_config_schema():
    schema = merge_config_schemas([Settings, LibSettings])

    # Add the $schema declaration for JSON Schema draft-07 (what SchemaStore uses)
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"

    with open("config.schema.json", "w") as f:
        json.dump(schema, f, indent=2)


@cli.command()
def export_manifest_schema():
    schema = PlatformManifest.model_json_schema()

    # Add the $schema declaration for JSON Schema draft-07 (what SchemaStore uses)
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"

    with open("manifest.schema.json", "w") as f:
        json.dump(schema, f, indent=2)


if __name__ == "__main__":
    cli()
