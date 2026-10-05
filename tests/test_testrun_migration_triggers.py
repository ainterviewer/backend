"""The touch-last_updated triggers survive migrations, because Alembic owns them.

On SQLite, `op.batch_alter_table` rebuilds a table by copy-and-rename, which
silently drops every trigger on it. e5f2a91c4d80 did that to `testrun` and the
triggers stayed missing on prod and staging for six weeks. alembic/env.py now
uninstalls the triggers before every upgrade/downgrade and installs the full
set after, so no migration has to remember to, and each deploy repairs them.

These drive the real env.py through `alembic.command` against a temporary
SQLite file built from the current models.
"""

import argparse
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.crud import InterviewDataBase
from app.db.tables import Base
from app.db.triggers import (
    _SQLITE_ALL_TRIGGER_NAMES,
    install_triggers,
    missing_triggers,
)

BACKEND = Path(__file__).parent.parent
ALL = len(_SQLITE_ALL_TRIGGER_NAMES)
TESTRUN = [
    name for name in _SQLITE_ALL_TRIGGER_NAMES if name.startswith("trg_testrun_")
]


@pytest.fixture
def database(tmp_path):
    """A file database with the current schema and every trigger installed."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install_triggers(connection)
    yield engine, url
    engine.dispose()


def alembic_config(url: str, command_name: str | None = None) -> Config:
    # No alembic.ini: loading it runs its logging config, which disables the
    # app's loggers for the rest of the session. Only these two options matter,
    # and the url must never be the configured storage/db.sqlite.
    config = Config()
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    if command_name is not None:
        # What the alembic CLI passes; env.py reads the command off it.
        fn = getattr(command, command_name)
        config.cmd_opts = argparse.Namespace(cmd=(fn, [], []), x=None)
    return config


def triggers(engine, table: str | None = None) -> int:
    query = "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'"
    if table is not None:
        query += f" AND tbl_name = '{table}'"
    with engine.connect() as connection:
        return connection.execute(text(query)).scalar_one()


def drop_testrun_triggers(engine) -> None:
    with engine.begin() as connection:
        for name in TESTRUN:
            connection.execute(text(f"DROP TRIGGER {name}"))


def test_the_database_starts_with_every_trigger(database):
    engine, _ = database
    assert triggers(engine) == ALL
    assert triggers(engine, "testrun") == len(TESTRUN) == 6


def test_an_upgrade_with_nothing_to_apply_reinstalls_lost_triggers(database):
    engine, url = database
    command.stamp(alembic_config(url), "head")
    drop_testrun_triggers(engine)
    assert triggers(engine, "testrun") == 0

    command.upgrade(alembic_config(url, "upgrade"), "head")

    assert triggers(engine) == ALL


def test_the_migration_that_lost_them_in_production_no_longer_can(database):
    """e5f2a91c4d80 rebuilds testrun with batch_alter_table and never touches
    the triggers -- exactly the kind of migration env.py now covers."""
    engine, url = database
    command.stamp(alembic_config(url), "c3d18b40f2a7")

    command.upgrade(alembic_config(url, "upgrade"), "e5f2a91c4d80")
    assert triggers(engine) == ALL

    command.downgrade(alembic_config(url, "downgrade"), "c3d18b40f2a7")
    assert triggers(engine) == ALL


def test_read_only_commands_do_not_touch_the_triggers(database):
    engine, url = database
    command.stamp(alembic_config(url), "head")
    drop_testrun_triggers(engine)

    command.current(alembic_config(url, "current"))

    assert triggers(engine, "testrun") == 0


def test_missing_triggers_names_exactly_the_missing_ones(database):
    engine, _ = database
    with engine.begin() as connection:
        assert missing_triggers(connection) == []
        connection.execute(text("DROP TRIGGER trg_testrun_touch_project_update"))
        assert missing_triggers(connection) == ["trg_testrun_touch_project_update"]


def test_startup_logs_an_error_for_missing_triggers(database, caplog):
    engine, _ = database
    drop_testrun_triggers(engine)

    with Session(engine) as session, caplog.at_level(logging.ERROR):
        missing = InterviewDataBase(session).check_triggers()

    assert missing == TESTRUN
    assert "trg_testrun_touch_testsetup_insert" in caplog.text


def test_startup_is_quiet_when_every_trigger_is_there(database, caplog):
    engine, _ = database

    with Session(engine) as session, caplog.at_level(logging.ERROR):
        assert InterviewDataBase(session).check_triggers() == []

    assert caplog.text == ""
