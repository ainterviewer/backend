"""delete orphaned rows

Removes rows whose parent no longer exists. These accumulated because SQLite
only enforces foreign keys on connections that ran `PRAGMA foreign_keys=ON`,
and the app set that pragma on a single connection at database creation instead
of on every pooled connection (fixed in app/db/pragmas.py). `ON DELETE CASCADE`
therefore never fired at runtime, so deleting a user, project or interview left
its children behind.

This is a prerequisite for enabling `foreign_keys=ON` and for any move to
PostgreSQL, which enforces foreign keys unconditionally and would reject this
data on import.

Two deliberate decisions:

* Projects whose owner was deleted are removed along with everything under
  them. `project.owner_id` declares ON DELETE CASCADE, so this is what the
  schema always intended; those projects had no collaborators either, which
  made them unreachable in the dashboard.
* Dangling references on nullable FKs declared SET NULL (interview.experiment_id,
  interview.participant_id, collaborator.added_by_id) are nulled rather than
  having their row deleted, again matching the declared intent.

The sweep is ordered parents-first so that rows orphaned by an earlier
statement are caught by a later one in the same pass, and every statement is a
no-op on a clean database, so re-running is safe.

Irreversible: the downgrade cannot restore deleted rows.

Revision ID: c3d18b40f2a7
Revises: a1c4e9f30b57
Create Date: 2026-08-21 12:00:00.000000

"""

import logging
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d18b40f2a7"
down_revision: str | None = "a1c4e9f30b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# (child table, child column, parent table) -- delete the child row when no
# parent with that id exists. Ordered parents-first: deleting a project here
# orphans its interviews, which the interview entry below then catches.
_DELETE_ORPHANS: tuple[tuple[str, str, str], ...] = (
    # Projects whose owner or folder is gone (ON DELETE CASCADE on both).
    ("project", "owner_id", "user"),
    ("project", "folder_id", "projectfolder"),
    # Test setups and runs, then the interviews belonging to a dead run.
    ("testsetup", "project_id", "project"),
    ("testrun", "test_setup_id", "testsetup"),
    ("interview", "project_id", "project"),
    ("interview", "test_run_id", "testrun"),
    # Everything else hanging off a project.
    ("projectlocalization", "project_id", "project"),
    ("analysis_category", "project_id", "project"),
    ("experiment_project", "project_id", "project"),
    ("experiment_project", "experiment_id", "experiment"),
    ("assistance_session", "project_id", "project"),
    ("assistance_session", "user_id", "user"),
    ("assistance_message_chunk", "session_id", "assistance_session"),
    ("participant", "folder_id", "projectfolder"),
    ("project_participant", "project_id", "project"),
    ("project_participant", "participant_id", "participant"),
    # Interview children.
    ("message", "project_id", "project"),
    ("message", "interview_id", "interview"),
    ("task", "project_id", "project"),
    ("task", "interview_id", "interview"),
    ("interviewee", "project_id", "project"),
    ("interviewee", "interview_id", "interview"),
    # Annotations, after the messages they hang off.
    ("message_annotation", "message_id", "message"),
    ("message_annotation", "user_id", "user"),
    ("annotation_value", "annotation_id", "message_annotation"),
    # User-scoped leftovers.
    ("collaborator", "user_id", "user"),
    ("collaborator", "folder_id", "projectfolder"),
    ("refresh_token", "user_id", "user"),
    ("verification_code", "user_id", "user"),
)

# (table, nullable column, parent table) -- null the reference instead of
# deleting the row, matching the SET NULL these foreign keys declare.
_NULL_ORPHANS: tuple[tuple[str, str, str], ...] = (
    ("interview", "experiment_id", "experiment"),
    ("interview", "participant_id", "project_participant"),
    ("collaborator", "added_by_id", "user"),
    ("access_requests", "processed_by_id", "user"),
)


def upgrade() -> None:
    connection = op.get_bind()
    total = 0

    for table, column, parent in _DELETE_ORPHANS:
        # NOT EXISTS rather than NOT IN: it is portable and does not go wrong
        # if the subquery ever yields a NULL.
        result = connection.exec_driver_sql(
            f"DELETE FROM {table} WHERE {column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {parent} WHERE {parent}.id = {table}.{column})"
        )
        if result.rowcount:
            total += result.rowcount
            logger.info(
                "deleted %s orphaned %s row(s) (%s.%s -> missing %s)",
                result.rowcount,
                table,
                table,
                column,
                parent,
            )

    for table, column, parent in _NULL_ORPHANS:
        result = connection.exec_driver_sql(
            f"UPDATE {table} SET {column} = NULL "
            f"WHERE {column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {parent} WHERE {parent}.id = {table}.{column})"
        )
        if result.rowcount:
            total += result.rowcount
            logger.info(
                "nulled %s dangling %s.%s reference(s) (missing %s)",
                result.rowcount,
                table,
                column,
                parent,
            )

    logger.info("orphan cleanup touched %s row(s)", total)


def downgrade() -> None:
    """Irreversible: the deleted rows cannot be reconstructed."""
    logger.warning(
        "Migration c3d18b40f2a7 deleted orphaned rows; nothing to restore on downgrade."
    )
