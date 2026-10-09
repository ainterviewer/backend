"""delete foreign key orphans again

Clears the rows that accumulated since c3d18b40f2a7, so that foreign keys can
be enforced. On production that is 878 messages and 219 tasks from a test
setup deleted while its synthetic run was still writing (2026-10-06); deletes
now end the live sessions first, and the foreign keys catch whatever slips
past.

Unlike c3d18b40f2a7, the sweep is not a hand-written table list. That list
went stale within weeks -- it names tables that have since been dropped and
misses `code`, `coding`, `message_comment`, `embedding` and
`interview_resume_token` -- so this one reads every foreign key from the schema
itself (`PRAGMA foreign_key_list`) and applies the rule c3d18b40f2a7 applied by
hand:

* A dangling reference on a column declared SET NULL, or on a nullable column
  with no ON DELETE action (`access_requests.processed_by_id`), is nulled.
  The row itself is fine; only the pointer is stale.
* Any other row whose parent is gone is deleted, which is what its declared
  CASCADE, or the NOT NULL on its column, says it cannot outlive.

It repeats until a pass changes nothing, so a row orphaned by an earlier
deletion in the same run -- a coding whose message was just removed -- is
caught without ordering the tables by hand. On a clean database the first pass
changes nothing, so re-running is safe.

SQLite only: PostgreSQL enforces foreign keys unconditionally and cannot hold
orphans in the first place.

Irreversible: the downgrade cannot restore deleted rows.

Revision ID: 4f8273d9e6f2
Revises: 0fa4a271248f
Create Date: 2026-10-09 12:16:14.553063

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import logging
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f8273d9e6f2"
down_revision: str | None = "0fa4a271248f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Far more than any real chain needs (project -> interview -> message ->
# coding is four); here so a mistake cannot loop forever.
_MAX_PASSES = 20


def _foreign_keys(connection) -> list[tuple[str, str, str, str, bool]]:
    """(table, column, parent table, parent column, null it) for every
    single-column foreign key in the database."""
    tables = [
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    ]
    keys = []
    for table in tables:
        nullable = {
            row[1]: not row[3]
            for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
        }
        for row in connection.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")'):
            _, _, parent, column, parent_column, _, on_delete, _ = row
            null_it = on_delete == "SET NULL" or (
                on_delete == "NO ACTION" and nullable[column]
            )
            keys.append((table, column, parent, parent_column or "id", null_it))
    return keys


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return

    keys = _foreign_keys(connection)
    total = 0

    for _ in range(_MAX_PASSES):
        changed = 0
        for table, column, parent, parent_column, null_it in keys:
            # NOT EXISTS rather than NOT IN: it does not go wrong if the
            # subquery ever yields a NULL.
            orphaned = (
                f'"{column}" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "{parent}" '
                f'WHERE "{parent}"."{parent_column}" = "{table}"."{column}")'
            )
            if null_it:
                sql = f'UPDATE "{table}" SET "{column}" = NULL WHERE {orphaned}'
                action = "nulled"
            else:
                sql = f'DELETE FROM "{table}" WHERE {orphaned}'
                action = "deleted"

            count = connection.exec_driver_sql(sql).rowcount
            if count:
                changed += count
                logger.info(
                    "%s %s orphaned %s row(s) (%s.%s -> missing %s)",
                    action,
                    count,
                    table,
                    table,
                    column,
                    parent,
                )
        total += changed
        if not changed:
            break
    else:
        raise RuntimeError(
            f"orphan sweep still changing rows after {_MAX_PASSES} passes"
        )

    logger.info("orphan cleanup touched %s row(s)", total)


def downgrade() -> None:
    """Irreversible: the deleted rows cannot be reconstructed."""
    logger.warning(
        "Migration 4f8273d9e6f2 deleted orphaned rows; nothing to restore on downgrade."
    )
