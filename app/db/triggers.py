"""Database triggers maintained as live behavior, not frozen migration history.

alembic/env.py owns them: every `alembic upgrade` or `downgrade` uninstalls them
before running migrations and installs the full set defined here afterwards, in
the same transaction. Migrations therefore need not (and should not) manage
them, and a batch_alter_table that rebuilds a table cannot lose them. To change
a trigger, edit the SQL here; the next deploy's `alembic upgrade head` applies
it, even with no new migration.

`missing_triggers` reports any that a database lacks; the app logs it on startup.

The SQLite triggers stamp `last_updated` with `app_now()`, a function the app
registers on every connection (`register_trigger_functions`) that returns
`ainterviewer.utils.now()` rendered exactly as SQLAlchemy stores it. They used
to write `CURRENT_TIMESTAMP` -- UTC, whole seconds -- beside app-written
Copenhagen wall-clock time, so a parent bumped by a trigger could show a
`last_updated` hours before its own `created_at`; revision 6b1d0e7a9c35
converted the values written that way. SQLite has no time-zone database, so the
clock has to come from Python.

The cost: a connection without the function -- the `sqlite3` shell, a DB
browser, an engine that skipped `register_trigger_functions` -- fails any write
to `projectcontent`, `testsetup` or `testrun` with "no such function:
app_now" rather than stamping the wrong clock. Alembic's engine registers it
too: env.py uninstalls the triggers before a run, but older migrations call
`install_triggers` mid-run, so they can fire during later ones. (The PostgreSQL
functions use `NOW()`, which follows the session time zone.)
"""

import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection, Engine

from ainterviewer.utils import now

logger = logging.getLogger(__name__)

# ----- the clock the SQLite triggers stamp with --------------------------------

APP_NOW = "app_now"


def _app_now() -> str:
    # The format SQLAlchemy stores a DateTime in on SQLite -- always six
    # fractional digits. SQLite compares these columns as strings, so a
    # trigger-written value must be shaped exactly like an app-written one.
    return now().strftime("%Y-%m-%d %H:%M:%S.%f")


def register_trigger_functions(engine: Engine) -> None:
    """Make `app_now()`, which the SQLite triggers call, exist on every new
    connection of `engine`.

    A no-op on other dialects.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _connection_record):
        try:
            # Not deterministic: it is a clock.
            dbapi_connection.create_function(APP_NOW, 0, _app_now)
        except Exception:
            logger.exception("Failed to register %s on a new connection", APP_NOW)


# ----- touch project.last_updated when its content changes --------------------

_PG_FUNCTION_CONTENT_TO_PROJECT = """
CREATE OR REPLACE FUNCTION touch_project_last_updated_from_content()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE project
    SET last_updated = NOW()
    WHERE id = COALESCE(NEW.project_id, OLD.project_id);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

_PG_DROP_TRIGGER_CONTENT_TO_PROJECT = (
    "DROP TRIGGER IF EXISTS trg_projectcontent_touch_project ON projectcontent;"
)

_PG_CREATE_TRIGGER_CONTENT_TO_PROJECT = """
CREATE TRIGGER trg_projectcontent_touch_project
AFTER INSERT OR UPDATE OR DELETE ON projectcontent
FOR EACH ROW
EXECUTE FUNCTION touch_project_last_updated_from_content();
"""

_PG_DROP_FUNCTION_CONTENT_TO_PROJECT = (
    "DROP FUNCTION IF EXISTS touch_project_last_updated_from_content();"
)

_SQLITE_TRIGGER_NAMES_CONTENT_TO_PROJECT = (
    "trg_projectcontent_touch_project_insert",
    "trg_projectcontent_touch_project_update",
    "trg_projectcontent_touch_project_delete",
)

_SQLITE_CREATE_STATEMENTS_CONTENT_TO_PROJECT = (
    """
    CREATE TRIGGER trg_projectcontent_touch_project_insert
    AFTER INSERT ON projectcontent
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_projectcontent_touch_project_update
    AFTER UPDATE ON projectcontent
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_projectcontent_touch_project_delete
    AFTER DELETE ON projectcontent
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = OLD.project_id;
    END;
    """,
)

# `projectlocalization` held per-language content until revision 916e657898af
# moved it to `projectcontent` and dropped the table. Its triggers are still
# dropped by name, for a database restored from before then.
_SQLITE_LEGACY_TRIGGER_NAMES = (
    "trg_projectlocalization_touch_project_insert",
    "trg_projectlocalization_touch_project_update",
    "trg_projectlocalization_touch_project_delete",
)
_PG_DROP_LEGACY_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_projectlocalization_touch_project "
    "ON projectlocalization;"
)
_PG_DROP_LEGACY_FUNCTION = (
    "DROP FUNCTION IF EXISTS touch_project_last_updated_from_localization();"
)


# ----- touch testsetup.last_updated when a testrun changes --------------------

_PG_FUNCTION_TESTRUN_TO_TESTSETUP = """
CREATE OR REPLACE FUNCTION touch_testsetup_last_updated_from_testrun()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE testsetup
    SET last_updated = NOW()
    WHERE id = COALESCE(NEW.test_setup_id, OLD.test_setup_id);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

_PG_DROP_TRIGGER_TESTRUN_TO_TESTSETUP = (
    "DROP TRIGGER IF EXISTS trg_testrun_touch_testsetup ON testrun;"
)

_PG_CREATE_TRIGGER_TESTRUN_TO_TESTSETUP = """
CREATE TRIGGER trg_testrun_touch_testsetup
AFTER INSERT OR UPDATE OR DELETE ON testrun
FOR EACH ROW
EXECUTE FUNCTION touch_testsetup_last_updated_from_testrun();
"""

_PG_DROP_FUNCTION_TESTRUN_TO_TESTSETUP = (
    "DROP FUNCTION IF EXISTS touch_testsetup_last_updated_from_testrun();"
)

_SQLITE_TRIGGER_NAMES_TESTRUN_TO_TESTSETUP = (
    "trg_testrun_touch_testsetup_insert",
    "trg_testrun_touch_testsetup_update",
    "trg_testrun_touch_testsetup_delete",
)

_SQLITE_CREATE_STATEMENTS_TESTRUN_TO_TESTSETUP = (
    """
    CREATE TRIGGER trg_testrun_touch_testsetup_insert
    AFTER INSERT ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE testsetup
        SET last_updated = app_now()
        WHERE id = NEW.test_setup_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_testsetup_update
    AFTER UPDATE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE testsetup
        SET last_updated = app_now()
        WHERE id = NEW.test_setup_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_testsetup_delete
    AFTER DELETE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE testsetup
        SET last_updated = app_now()
        WHERE id = OLD.test_setup_id;
    END;
    """,
)


# ----- touch project.last_updated when a testsetup changes --------------------

_PG_FUNCTION_TESTSETUP_TO_PROJECT = """
CREATE OR REPLACE FUNCTION touch_project_last_updated_from_testsetup()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE project
    SET last_updated = NOW()
    WHERE id = COALESCE(NEW.project_id, OLD.project_id);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

_PG_DROP_TRIGGER_TESTSETUP_TO_PROJECT = (
    "DROP TRIGGER IF EXISTS trg_testsetup_touch_project ON testsetup;"
)

_PG_CREATE_TRIGGER_TESTSETUP_TO_PROJECT = """
CREATE TRIGGER trg_testsetup_touch_project
AFTER INSERT OR UPDATE OR DELETE ON testsetup
FOR EACH ROW
EXECUTE FUNCTION touch_project_last_updated_from_testsetup();
"""

_PG_DROP_FUNCTION_TESTSETUP_TO_PROJECT = (
    "DROP FUNCTION IF EXISTS touch_project_last_updated_from_testsetup();"
)

_SQLITE_TRIGGER_NAMES_TESTSETUP_TO_PROJECT = (
    "trg_testsetup_touch_project_insert",
    "trg_testsetup_touch_project_update",
    "trg_testsetup_touch_project_delete",
)

_SQLITE_CREATE_STATEMENTS_TESTSETUP_TO_PROJECT = (
    """
    CREATE TRIGGER trg_testsetup_touch_project_insert
    AFTER INSERT ON testsetup
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testsetup_touch_project_update
    AFTER UPDATE ON testsetup
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testsetup_touch_project_delete
    AFTER DELETE ON testsetup
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = OLD.project_id;
    END;
    """,
)


# ----- touch project.last_updated when a testrun changes ----------------------

_PG_FUNCTION_TESTRUN_TO_PROJECT = """
CREATE OR REPLACE FUNCTION touch_project_last_updated_from_testrun()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE project p
    SET last_updated = NOW()
    FROM testsetup ts
    WHERE ts.id = COALESCE(NEW.test_setup_id, OLD.test_setup_id)
      AND p.id = ts.project_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

_PG_DROP_TRIGGER_TESTRUN_TO_PROJECT = (
    "DROP TRIGGER IF EXISTS trg_testrun_touch_project ON testrun;"
)

_PG_CREATE_TRIGGER_TESTRUN_TO_PROJECT = """
CREATE TRIGGER trg_testrun_touch_project
AFTER INSERT OR UPDATE OR DELETE ON testrun
FOR EACH ROW
EXECUTE FUNCTION touch_project_last_updated_from_testrun();
"""

_PG_DROP_FUNCTION_TESTRUN_TO_PROJECT = (
    "DROP FUNCTION IF EXISTS touch_project_last_updated_from_testrun();"
)

_SQLITE_TRIGGER_NAMES_TESTRUN_TO_PROJECT = (
    "trg_testrun_touch_project_insert",
    "trg_testrun_touch_project_update",
    "trg_testrun_touch_project_delete",
)

_SQLITE_CREATE_STATEMENTS_TESTRUN_TO_PROJECT = (
    """
    CREATE TRIGGER trg_testrun_touch_project_insert
    AFTER INSERT ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = (SELECT project_id FROM testsetup WHERE id = NEW.test_setup_id);
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_project_update
    AFTER UPDATE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = (SELECT project_id FROM testsetup WHERE id = NEW.test_setup_id);
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_project_delete
    AFTER DELETE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = app_now()
        WHERE id = (SELECT project_id FROM testsetup WHERE id = OLD.test_setup_id);
    END;
    """,
)

# Every trigger this module maintains, per dialect -- what `install_triggers`
# creates and what `missing_triggers` expects to find.
_SQLITE_ALL_TRIGGER_NAMES = (
    _SQLITE_TRIGGER_NAMES_CONTENT_TO_PROJECT
    + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_TESTSETUP
    + _SQLITE_TRIGGER_NAMES_TESTSETUP_TO_PROJECT
    + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_PROJECT
)
_PG_ALL_TRIGGER_NAMES = (
    "trg_projectcontent_touch_project",
    "trg_testrun_touch_testsetup",
    "trg_testsetup_touch_project",
    "trg_testrun_touch_project",
)


def _has_table(connection: Connection, name: str) -> bool:
    """Older migrations call `install_triggers` mid-run, before tables added
    later exist; their triggers are installed by the run that creates them."""
    return inspect(connection).has_table(name)


def install_triggers(connection: Connection) -> None:
    """(Re)install all DB triggers. Safe to call on existing or fresh DBs."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        if _has_table(connection, "projectcontent"):
            connection.execute(text(_PG_DROP_TRIGGER_CONTENT_TO_PROJECT))
            connection.execute(text(_PG_FUNCTION_CONTENT_TO_PROJECT))
            connection.execute(text(_PG_CREATE_TRIGGER_CONTENT_TO_PROJECT))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_TESTSETUP))
        connection.execute(text(_PG_FUNCTION_TESTRUN_TO_TESTSETUP))
        connection.execute(text(_PG_CREATE_TRIGGER_TESTRUN_TO_TESTSETUP))

        connection.execute(text(_PG_DROP_TRIGGER_TESTSETUP_TO_PROJECT))
        connection.execute(text(_PG_FUNCTION_TESTSETUP_TO_PROJECT))
        connection.execute(text(_PG_CREATE_TRIGGER_TESTSETUP_TO_PROJECT))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_PROJECT))
        connection.execute(text(_PG_FUNCTION_TESTRUN_TO_PROJECT))
        connection.execute(text(_PG_CREATE_TRIGGER_TESTRUN_TO_PROJECT))
        return

    if dialect == "sqlite":
        all_statements = (
            (
                _SQLITE_CREATE_STATEMENTS_CONTENT_TO_PROJECT
                if _has_table(connection, "projectcontent")
                else ()
            )
            + _SQLITE_CREATE_STATEMENTS_TESTRUN_TO_TESTSETUP
            + _SQLITE_CREATE_STATEMENTS_TESTSETUP_TO_PROJECT
            + _SQLITE_CREATE_STATEMENTS_TESTRUN_TO_PROJECT
        )
        for name in _SQLITE_ALL_TRIGGER_NAMES + _SQLITE_LEGACY_TRIGGER_NAMES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        for stmt in all_statements:
            connection.execute(text(stmt))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")


def uninstall_triggers(connection: Connection) -> None:
    """Remove all DB triggers. Used by migration downgrades."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        if _has_table(connection, "projectcontent"):
            connection.execute(text(_PG_DROP_TRIGGER_CONTENT_TO_PROJECT))
        connection.execute(text(_PG_DROP_FUNCTION_CONTENT_TO_PROJECT))
        if _has_table(connection, "projectlocalization"):
            connection.execute(text(_PG_DROP_LEGACY_TRIGGER))
        connection.execute(text(_PG_DROP_LEGACY_FUNCTION))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_TESTSETUP))
        connection.execute(text(_PG_DROP_FUNCTION_TESTRUN_TO_TESTSETUP))

        connection.execute(text(_PG_DROP_TRIGGER_TESTSETUP_TO_PROJECT))
        connection.execute(text(_PG_DROP_FUNCTION_TESTSETUP_TO_PROJECT))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_PROJECT))
        connection.execute(text(_PG_DROP_FUNCTION_TESTRUN_TO_PROJECT))
        return

    if dialect == "sqlite":
        for name in _SQLITE_ALL_TRIGGER_NAMES + _SQLITE_LEGACY_TRIGGER_NAMES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")


def missing_triggers(connection: Connection) -> list[str]:
    """The triggers this module maintains that the database does not have.

    Empty when they are all in place. Migrations reinstall the full set on every
    upgrade (see alembic/env.py), so a non-empty result means the database was
    changed outside Alembic -- a restored backup, a manual fix -- and running
    `alembic upgrade head` puts them back.
    """
    dialect = connection.dialect.name

    if dialect == "postgresql":
        expected = _PG_ALL_TRIGGER_NAMES
        query = "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
    elif dialect == "sqlite":
        expected = _SQLITE_ALL_TRIGGER_NAMES
        query = "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    else:
        raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")

    present = set(connection.execute(text(query)).scalars())
    return [name for name in expected if name not in present]
