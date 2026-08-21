import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
from app.db._extra import PydanticJSONB
from app.db.tables import Base

target_metadata = Base.metadata

logger = logging.getLogger("alembic.runtime.migration")

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


EXCLUDED_TABLES = {"_sqliteai_vector"}


def include_object(object, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in EXCLUDED_TABLES)


def render_item(type_, obj, autogen_context):
    """Apply custom rendering for PydanticType."""
    if type_ == "type" and isinstance(obj, PydanticJSONB):
        return "sa.JSON()"
    return False


def _target_url() -> str | None:
    """The database to migrate.

    `-x db_url=...` overrides the url configured in alembic.ini, which is what
    makes it possible to rehearse a migration against a copy of a database
    instead of the real one:

        uv run alembic -x db_url="sqlite:////tmp/db-copy.sqlite" upgrade head

    Without this, the flag is accepted and silently ignored, and the migration
    runs against the configured database -- which is exactly how a rehearsal
    once deleted rows from the live database instead of the copy.
    """
    return context.get_x_argument(as_dictionary=True).get("db_url") or (
        config.get_main_option("sqlalchemy.url")
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _target_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    section = config.get_section(config.config_ini_section, {})
    url = _target_url()
    if url is not None:
        section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    logger.info("running migrations against %s", connectable.url)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
