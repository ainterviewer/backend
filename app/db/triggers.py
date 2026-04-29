"""Database triggers maintained as live behavior, not frozen migration history.

Migrations import from this module so the latest definitions get applied. To
change a trigger, edit the SQL here and write a new migration that calls
`install_triggers` (the install functions drop existing triggers first, so they
are safe to re-run).

See alembic migration #9c7f4f2f91ab for how to apply the triggers to the database.
A new similar migration should be made whenever the triggers change, so they will
be applied to the databases automatically on deployment.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

# ----- touch project.last_updated when a localization changes -----------------

_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION touch_project_last_updated_from_localization()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE project
    SET last_updated = NOW()
    WHERE id = COALESCE(NEW.project_id, OLD.project_id);

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

_PG_DROP_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_projectlocalization_touch_project "
    "ON projectlocalization;"
)

_PG_CREATE_TRIGGER = """
CREATE TRIGGER trg_projectlocalization_touch_project
AFTER INSERT OR UPDATE OR DELETE ON projectlocalization
FOR EACH ROW
EXECUTE FUNCTION touch_project_last_updated_from_localization();
"""

_PG_DROP_FUNCTION = (
    "DROP FUNCTION IF EXISTS touch_project_last_updated_from_localization();"
)


_SQLITE_TRIGGER_NAMES = (
    "trg_projectlocalization_touch_project_insert",
    "trg_projectlocalization_touch_project_update",
    "trg_projectlocalization_touch_project_delete",
)

_SQLITE_CREATE_STATEMENTS = (
    """
    CREATE TRIGGER trg_projectlocalization_touch_project_insert
    AFTER INSERT ON projectlocalization
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_projectlocalization_touch_project_update
    AFTER UPDATE ON projectlocalization
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_projectlocalization_touch_project_delete
    AFTER DELETE ON projectlocalization
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = OLD.project_id;
    END;
    """,
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
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.test_setup_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_testsetup_update
    AFTER UPDATE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE testsetup
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.test_setup_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_testsetup_delete
    AFTER DELETE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE testsetup
        SET last_updated = CURRENT_TIMESTAMP
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
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testsetup_touch_project_update
    AFTER UPDATE ON testsetup
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = NEW.project_id;
    END;
    """,
    """
    CREATE TRIGGER trg_testsetup_touch_project_delete
    AFTER DELETE ON testsetup
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
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
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = (SELECT project_id FROM testsetup WHERE id = NEW.test_setup_id);
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_project_update
    AFTER UPDATE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = (SELECT project_id FROM testsetup WHERE id = NEW.test_setup_id);
    END;
    """,
    """
    CREATE TRIGGER trg_testrun_touch_project_delete
    AFTER DELETE ON testrun
    FOR EACH ROW
    BEGIN
        UPDATE project
        SET last_updated = CURRENT_TIMESTAMP
        WHERE id = (SELECT project_id FROM testsetup WHERE id = OLD.test_setup_id);
    END;
    """,
)


def install_triggers(connection: Connection) -> None:
    """(Re)install all DB triggers. Safe to call on existing or fresh DBs."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        connection.execute(text(_PG_DROP_TRIGGER))
        connection.execute(text(_PG_FUNCTION))
        connection.execute(text(_PG_CREATE_TRIGGER))

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
        all_names = (
            _SQLITE_TRIGGER_NAMES
            + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_TESTSETUP
            + _SQLITE_TRIGGER_NAMES_TESTSETUP_TO_PROJECT
            + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_PROJECT
        )
        all_statements = (
            _SQLITE_CREATE_STATEMENTS
            + _SQLITE_CREATE_STATEMENTS_TESTRUN_TO_TESTSETUP
            + _SQLITE_CREATE_STATEMENTS_TESTSETUP_TO_PROJECT
            + _SQLITE_CREATE_STATEMENTS_TESTRUN_TO_PROJECT
        )
        for name in all_names:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        for stmt in all_statements:
            connection.execute(text(stmt))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")


def uninstall_triggers(connection: Connection) -> None:
    """Remove all DB triggers. Used by migration downgrades."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        connection.execute(text(_PG_DROP_TRIGGER))
        connection.execute(text(_PG_DROP_FUNCTION))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_TESTSETUP))
        connection.execute(text(_PG_DROP_FUNCTION_TESTRUN_TO_TESTSETUP))

        connection.execute(text(_PG_DROP_TRIGGER_TESTSETUP_TO_PROJECT))
        connection.execute(text(_PG_DROP_FUNCTION_TESTSETUP_TO_PROJECT))

        connection.execute(text(_PG_DROP_TRIGGER_TESTRUN_TO_PROJECT))
        connection.execute(text(_PG_DROP_FUNCTION_TESTRUN_TO_PROJECT))
        return

    if dialect == "sqlite":
        all_names = (
            _SQLITE_TRIGGER_NAMES
            + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_TESTSETUP
            + _SQLITE_TRIGGER_NAMES_TESTSETUP_TO_PROJECT
            + _SQLITE_TRIGGER_NAMES_TESTRUN_TO_PROJECT
        )
        for name in all_names:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")
