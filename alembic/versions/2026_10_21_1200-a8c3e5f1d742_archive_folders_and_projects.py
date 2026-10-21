"""archive folders and projects

A nullable `archived_at` on `projectfolder` and `project`. Archiving only hides
a folder or project from the dashboard by default; NULL means not archived, so
every existing row stays visible.

Revision ID: a8c3e5f1d742
Revises: 74b1004912f9
Create Date: 2026-10-21 12:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = "a8c3e5f1d742"
down_revision: str | None = "74b1004912f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projectfolder", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("archived_at", app.db.types.AppDateTime(), nullable=True)
        )

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("archived_at", app.db.types.AppDateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("archived_at")

    with op.batch_alter_table("projectfolder", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
