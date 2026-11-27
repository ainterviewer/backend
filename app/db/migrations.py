"""The Alembic config the app uses when it runs migrations itself."""

from alembic.config import Config

from ..paths import APP_DIR


def alembic_config() -> Config:
    """A Config for this repo's migrations, without loading alembic.ini.

    Loading the ini runs its logging config (`fileConfig` in alembic/env.py),
    which disables every logger already created in the process. That is
    harmless for the `alembic` CLI, which exits afterwards, but not inside the
    app: app/boot.py migrates and then serves in the same process, and
    `create_db_and_tables` stamps a new database from within it. The migration
    tests skip the ini for the same reason.

    Only `script_location` is needed: with no url on the Config, env.py
    migrates the database the app's settings name.
    """
    config = Config()
    config.set_main_option("script_location", str(APP_DIR.parent / "alembic"))
    return config
