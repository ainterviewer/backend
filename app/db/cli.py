from typer import Typer

from ..dependencies import get_db
from ..platform_release import PlatformManifest

cli = Typer()


@cli.command(hidden=True)
def _(): ...


@cli.command()
def add_release_manifest(release_manifest: str):
    db = next(get_db())

    platform_release_manifest = PlatformManifest.model_validate_json(release_manifest)

    db.set_platform_release(platform_manifest=platform_release_manifest)


if __name__ == "__main__":
    cli()
