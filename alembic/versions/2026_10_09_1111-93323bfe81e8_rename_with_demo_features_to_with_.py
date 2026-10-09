"""rename with_demo_features to with_preview_features

"Demo" kept being read as `Scope.DEMO`, the restricted account tier, when
the flag means the opposite: early access to unfinished features. A rename,
not a drop and add, so every user keeps the value an admin gave them.

Revision ID: 93323bfe81e8
Revises: d5f6e39dd77e
Create Date: 2026-10-09 11:11:45.330821

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "93323bfe81e8"
down_revision: str | None = "d5f6e39dd77e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "with_demo_features", new_column_name="with_preview_features"
        )


def downgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "with_preview_features", new_column_name="with_demo_features"
        )
