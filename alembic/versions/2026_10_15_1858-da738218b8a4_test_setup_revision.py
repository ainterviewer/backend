"""test setup revision

`testsetup.revision`, moved on every save of what the setup page edits
(background info, fixed answers, fixed personas), so a save based on an
older one is refused rather than overwriting someone else's. Starts at 1.

Revision ID: da738218b8a4
Revises: d874205f7f00
Create Date: 2026-10-15 18:58:27.605674

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
revision: str = "da738218b8a4"
down_revision: str | None = "d874205f7f00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("testsetup", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "revision", sa.Integer(), server_default=sa.text("1"), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("testsetup", schema=None) as batch_op:
        batch_op.drop_column("revision")
