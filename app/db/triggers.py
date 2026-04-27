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


def install_triggers(connection: Connection) -> None:
    """(Re)install all DB triggers. Safe to call on existing or fresh DBs."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        connection.execute(text(_PG_DROP_TRIGGER))
        connection.execute(text(_PG_FUNCTION))
        connection.execute(text(_PG_CREATE_TRIGGER))
        return

    if dialect == "sqlite":
        for name in _SQLITE_TRIGGER_NAMES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        for stmt in _SQLITE_CREATE_STATEMENTS:
            connection.execute(text(stmt))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")


def uninstall_triggers(connection: Connection) -> None:
    """Remove all DB triggers. Used by migration downgrades."""
    dialect = connection.dialect.name

    if dialect == "postgresql":
        connection.execute(text(_PG_DROP_TRIGGER))
        connection.execute(text(_PG_DROP_FUNCTION))
        return

    if dialect == "sqlite":
        for name in _SQLITE_TRIGGER_NAMES:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name};"))
        return

    raise NotImplementedError(f"Unsupported dialect for triggers: {dialect}")
