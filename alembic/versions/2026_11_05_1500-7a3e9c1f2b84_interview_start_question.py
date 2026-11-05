"""interview start question

Revision ID: 7a3e9c1f2b84
Revises: e19caa73df3d
Create Date: 2026-11-05 15:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "7a3e9c1f2b84"
down_revision: str | None = "e19caa73df3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("interview", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_question_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("interview", schema=None) as batch_op:
        batch_op.drop_column("start_question_id")
