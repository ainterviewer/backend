"""per-user demo limits

Nullable overrides of the `Scope.DEMO` synthetic-interview budgets on `user`,
and the same on `invitation` to be copied onto the user at registration. NULL
means the configured default (`app.demo_limits`), so every existing row keeps
the budget it has today.

Revision ID: 74b1004912f9
Revises: 3c7a91e5d2b8
Create Date: 2026-10-20 16:01:09.526233

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
revision: str = "74b1004912f9"
down_revision: str | None = "3c7a91e5d2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("user_demo_concurrent_limit", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("user_demo_daily_limit", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("user_demo_lifetime_limit", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("demo_concurrent_limit", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("demo_daily_limit", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("demo_lifetime_limit", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("demo_lifetime_limit")
        batch_op.drop_column("demo_daily_limit")
        batch_op.drop_column("demo_concurrent_limit")

    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.drop_column("user_demo_lifetime_limit")
        batch_op.drop_column("user_demo_daily_limit")
        batch_op.drop_column("user_demo_concurrent_limit")
