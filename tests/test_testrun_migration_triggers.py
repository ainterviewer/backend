"""Migrations that batch-alter `testrun` must leave its triggers in place.

On SQLite, `op.batch_alter_table` recreates the table, which drops every
trigger on it. The first version of c7bde657f2c4 did that and silently removed
the six triggers that bump testsetup/project `last_updated` when a test run
changes; 1a0e5c9b7d42 repairs the databases it ran on.

These run the migrations' own upgrade/downgrade functions against an in-memory
database that has the current schema and the triggers installed.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.db.tables import Base
from app.db.triggers import install_triggers

VERSIONS = Path(__file__).parent.parent / "alembic" / "versions"
TESTRUN_TRIGGERS = 6


def load_migration(revision: str) -> ModuleType:
    (path,) = VERSIONS.glob(f"*{revision}*.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def testrun_triggers(connection) -> int:
    return connection.execute(
        text(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'testrun'"
        )
    ).scalar_one()


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install_triggers(connection)
        yield connection


def run(connection, step) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        step()


def test_the_schema_starts_with_the_testrun_triggers(connection):
    assert testrun_triggers(connection) == TESTRUN_TRIGGERS


def test_adding_started_by_keeps_the_testrun_triggers(connection):
    migration = load_migration("c7bde657f2c4")

    # The schema already has the column, so take it off and put it back: both
    # directions recreate the table.
    run(connection, migration.downgrade)
    assert testrun_triggers(connection) == TESTRUN_TRIGGERS

    run(connection, migration.upgrade)
    assert testrun_triggers(connection) == TESTRUN_TRIGGERS


def test_the_repair_restores_triggers_a_batch_alter_dropped(connection):
    connection.execute(text("DROP TRIGGER trg_testrun_touch_testsetup_insert"))
    connection.execute(text("DROP TRIGGER trg_testrun_touch_project_update"))

    run(connection, load_migration("1a0e5c9b7d42").upgrade)

    assert testrun_triggers(connection) == TESTRUN_TRIGGERS
