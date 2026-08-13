"""add indexes for monitoring/analysis queries

Revision ID: c7f21a5b4e10
Revises: b31c7a4d9e02
Create Date: 2026-08-13 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

import app.db.types  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "c7f21a5b4e10"
down_revision: Union[str, None] = "b31c7a4d9e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Index the columns the monitoring dashboard filters and joins on.

    Foreign keys are not indexed automatically, so `message` was fully scanned
    for every monitoring request.
    """
    op.create_index("ix_interview_project_id_type", "interview", ["project_id", "type"])
    op.create_index("ix_message_interview_id", "message", ["interview_id"])
    op.create_index("ix_message_project_id", "message", ["project_id"])

    # SQLite's planner only uses an index well once it has table statistics;
    # measured on a 4.7k-interview / 343k-message copy of the dev database,
    # ANALYZE cut the monitoring endpoint from ~1550ms to ~1010ms on top of the
    # indexes above. Stats go stale as data grows -- `PRAGMA optimize` on
    # connection close is the usual way to keep them fresh.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ANALYZE")


def downgrade() -> None:
    op.drop_index("ix_message_project_id", table_name="message")
    op.drop_index("ix_message_interview_id", table_name="message")
    op.drop_index("ix_interview_project_id_type", table_name="interview")
