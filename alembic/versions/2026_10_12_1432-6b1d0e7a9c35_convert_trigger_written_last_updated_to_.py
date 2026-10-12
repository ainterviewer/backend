"""convert trigger-written last_updated to the app clock

The touch-last_updated triggers used to stamp `CURRENT_TIMESTAMP` -- UTC, whole
seconds -- while the app writes `ainterviewer.utils.now()`: wall-clock time in
the configured zone (Europe/Copenhagen), with microseconds. A parent bumped by
a trigger could therefore show a `last_updated` hours before its `created_at`.
The triggers now call `app_now()` (app/db/triggers.py); this converts the
values the old ones left behind.

They can be told apart by shape alone. SQLAlchemy always renders a DateTime
with six fractional digits, even at a whole second, and `CURRENT_TIMESTAMP`
never has any -- so a 19-character value was written by a trigger. Only
`project` and `testsetup` are bumped by triggers, so only they are touched.
Re-running is safe: a converted value has fractional digits and is skipped.

A converted value is never earlier than its row's `created_at`. The old
triggers truncated to the second, so the bump a project gets from its default
localization, inserted in the same second it was created, came out a fraction
of a second before creation even in the right zone. No bump can precede the
row, so `created_at` is the honest lower bound.

The downgrade does nothing. The converted values are correct under either
version of the triggers, and nothing distinguishes them afterwards.

Revision ID: 6b1d0e7a9c35
Revises: 4f8273d9e6f2
Create Date: 2026-10-12 14:32:22.423353

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op

from ainterviewer.settings import settings as lib_settings

# revision identifiers, used by Alembic.
revision: str = "6b1d0e7a9c35"
down_revision: str | None = "4f8273d9e6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLES = ("project", "testsetup")
_TRIGGER_FORMAT = "%Y-%m-%d %H:%M:%S"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return

    for table in _TABLES:
        rows = connection.exec_driver_sql(
            f"SELECT id, created_at, last_updated FROM {table} "
            "WHERE length(last_updated) = 19"
        ).fetchall()
        for row_id, created_at, value in rows:
            local = (
                datetime.strptime(value, _TRIGGER_FORMAT)
                .replace(tzinfo=UTC)
                .astimezone(lib_settings.tzinfo)
                .strftime("%Y-%m-%d %H:%M:%S.%f")
            )
            # Both rendered alike, so they compare correctly as strings.
            connection.exec_driver_sql(
                f"UPDATE {table} SET last_updated = ? WHERE id = ?",
                (max(local, created_at or local), row_id),
            )
        logger.info("converted %d trigger-written %s.last_updated", len(rows), table)


def downgrade() -> None:
    pass
